#!/usr/bin/env python3
"""Bulk-deactivate Slack members using Firefox's authenticated web session.

The script defaults to a read-only dry run. It uses Slack's unsupported
internal users.admin endpoints, matching the Bulk Slack User Deactivation
browser extension. These endpoints can change without notice.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TOKEN_PATTERN = re.compile(rb"xoxc-[A-Za-z0-9-]{20,}")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class PlanItem:
    email: str
    user_id: str | None
    status: str


@dataclass(frozen=True)
class ExecutionResult:
    email: str
    user_id: str | None
    result: str
    error: str | None = None


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def read_emails(path: Path) -> list[str]:
    """Read a header-based or single-column CSV, preserving first-seen order."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ValueError("CSV is empty")

    first_row = [cell.strip().casefold() for cell in rows[0]]
    if "email" in first_row:
        email_index = first_row.index("email")
        data_rows = rows[1:]
    else:
        email_index = 0
        data_rows = rows

    emails: list[str] = []
    seen: set[str] = set()
    for line_number, row in enumerate(data_rows, start=2 if data_rows is not rows else 1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if email_index >= len(row):
            raise ValueError(f"Missing email in CSV row {line_number}")
        email = normalize_email(row[email_index])
        if not EMAIL_PATTERN.fullmatch(email):
            raise ValueError(f"Invalid email in CSV row {line_number}: {row[email_index]!r}")
        if email not in seen:
            seen.add(email)
            emails.append(email)

    if not emails:
        raise ValueError("CSV contains no email addresses")
    return emails


def default_firefox_profile(profiles_ini: Path | None = None) -> Path:
    profiles_ini = profiles_ini or Path.home() / ".mozilla/firefox/profiles.ini"
    parser = configparser.ConfigParser()
    if not parser.read(profiles_ini):
        raise FileNotFoundError(f"Firefox profiles file not found: {profiles_ini}")

    base = profiles_ini.parent
    selected: str | None = None
    for section in parser.sections():
        if section.startswith("Install") and parser.has_option(section, "Default"):
            selected = parser.get(section, "Default")
            break
    if selected is None:
        for section in parser.sections():
            if section.startswith("Profile") and parser.getboolean(section, "Default", fallback=False):
                selected = parser.get(section, "Path")
                break
    if selected is None:
        raise RuntimeError("Firefox has no default profile")

    profile = Path(selected)
    if not profile.is_absolute():
        profile = base / profile
    if not profile.is_dir():
        raise FileNotFoundError(f"Firefox profile not found: {profile}")
    return profile


def find_slack_tokens(profile: Path) -> list[str]:
    """Find Slack client-token candidates without logging their values."""
    storage = profile / "storage/default"
    roots = [path for path in storage.glob("https+++*slack.com*") if path.is_dir()]
    tokens: set[str] = set()
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                tokens.update(match.decode("ascii") for match in TOKEN_PATTERN.findall(path.read_bytes()))
            except (OSError, UnicodeError):
                continue
    if not tokens:
        raise RuntimeError(
            "No Slack client credential was found in the Firefox profile. "
            "Open the target Slack workspace in Firefox and reload its admin page."
        )
    return sorted(tokens)


def read_slack_cookie(profile: Path) -> str:
    """Read the newest Firefox Slack session cookie without copying or printing it."""
    database = profile / "cookies.sqlite"
    if not database.exists():
        raise FileNotFoundError(f"Firefox cookie database not found: {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM moz_cookies "
            "WHERE name = 'd' AND host LIKE '%slack.com' "
            "ORDER BY CASE WHEN host = '.slack.com' THEN 0 "
            "WHEN host = 'slack.com' THEN 1 ELSE 2 END, lastAccessed DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if not row or not row[0]:
        raise RuntimeError("No Slack session cookie was found in Firefox")
    return str(row[0])


class SlackInternalClient:
    def __init__(self, token: str, cookie: str, host: str = "slack.com", timeout: float = 30):
        self._token = token
        self._cookie = cookie
        self.host = host
        self.timeout = timeout

    def api(self, method: str, parameters: dict[str, Any], host: str | None = None) -> dict[str, Any]:
        payload = dict(parameters)
        payload.setdefault("token", self._token)
        request = urllib.request.Request(
            f"https://{host or self.host}/api/{method}",
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Cookie": f"d={self._cookie}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    result = json.load(response)
                if not isinstance(result, dict):
                    raise RuntimeError(f"Slack returned a non-object response for {method}")
                return result
            except urllib.error.HTTPError as error:
                if error.code not in RETRYABLE_HTTP_STATUSES or attempt == 3:
                    raise RuntimeError(f"Slack HTTP error for {method}: {error.code}") from error
                retry_after = error.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2**attempt
                time.sleep(delay)
            except urllib.error.URLError as error:
                if attempt == 3:
                    raise RuntimeError(f"Slack network error for {method}: {error.reason}") from error
                time.sleep(2**attempt)
        raise AssertionError("retry loop exhausted")

    def auth_test(self) -> dict[str, Any]:
        return self.api("auth.test", {}, host="slack.com")

    def fetch_members(self, email: str) -> list[dict[str, Any]]:
        expected = normalize_email(email)
        matches: list[dict[str, Any]] = []
        cursor = ""
        while True:
            response = self.api(
                "users.list",
                {"limit": 200, "cursor": cursor},
                host="slack.com",
            )
            if not response.get("ok"):
                raise RuntimeError(
                    "Slack member lookup failed: "
                    + str(response.get("error", "unknown_error"))
                )
            for member in response.get("members", []):
                if not isinstance(member, dict):
                    continue
                member_email = normalize_email(
                    str((member.get("profile") or {}).get("email", ""))
                )
                if member_email == expected:
                    matches.append(member)
            cursor = str(
                (response.get("response_metadata") or {}).get("next_cursor") or ""
            )
            if not cursor:
                return matches

    def deactivate(self, user_id: str) -> dict[str, Any]:
        response = self.api("users.admin.setInactive", {"user": user_id})
        if not response.get("ok"):
            raise RuntimeError(
                "Slack deactivation failed: " + str(response.get("error", "unknown_error"))
            )
        return response


def workspace_matches(expected: str, auth: dict[str, Any]) -> bool:
    expected_key = expected.strip().casefold().removesuffix(".slack.com")
    url_host = urllib.parse.urlparse(str(auth.get("url", ""))).hostname or ""
    candidates = {
        str(auth.get("team", "")).casefold(),
        str(auth.get("team_id", "")).casefold(),
        url_host.casefold(),
        url_host.casefold().removesuffix(".slack.com"),
    }
    return expected_key in candidates


def select_session(profile: Path, expected_workspace: str) -> tuple[SlackInternalClient, dict[str, Any]]:
    cookie = read_slack_cookie(profile)
    matches: list[tuple[SlackInternalClient, dict[str, Any]]] = []
    for token in find_slack_tokens(profile):
        client = SlackInternalClient(token, cookie)
        auth = client.auth_test()
        if not auth.get("ok"):
            continue
        if workspace_matches(expected_workspace, auth):
            host = urllib.parse.urlparse(str(auth.get("url", ""))).hostname
            if not host or not host.endswith(".slack.com"):
                raise RuntimeError("Slack returned an invalid workspace URL")
            client.host = host
            matches.append((client, auth))
    if not matches:
        raise RuntimeError(
            f"No Firefox Slack credential matched workspace {expected_workspace!r}. "
            "Open that workspace in Firefox and reload its admin page."
        )
    if len(matches) > 1:
        raise RuntimeError(f"Multiple Firefox Slack credentials matched {expected_workspace!r}")
    return matches[0]


def exact_member_match(items: Iterable[dict[str, Any]], email: str) -> dict[str, Any] | None:
    expected = normalize_email(email)
    exact = [
        item
        for item in items
        if normalize_email(str((item.get("profile") or {}).get("email", ""))) == expected
    ]
    return exact[0] if len(exact) == 1 else None


def classify_member(member: dict[str, Any], current_user_id: str, allow_privileged: bool) -> str:
    if member.get("id") == current_user_id:
        return "self"
    if member.get("deleted"):
        return "already_inactive"
    if member.get("is_bot") or member.get("is_app_user"):
        return "bot"
    if member.get("is_primary_owner"):
        return "primary_owner"
    privileged = any(
        member.get(field)
        for field in ("is_admin", "is_owner")
    )
    if privileged and not allow_privileged:
        return "privileged"
    return "ready"


def build_plan(
    client: SlackInternalClient,
    emails: Iterable[str],
    current_user_id: str,
    allow_privileged: bool,
) -> list[PlanItem]:
    plan: list[PlanItem] = []
    for email in emails:
        member = exact_member_match(client.fetch_members(email), email)
        if member is None:
            plan.append(PlanItem(email, None, "not_found"))
            continue
        user_id = str(member.get("id") or "")
        if not user_id:
            plan.append(PlanItem(email, None, "not_found"))
            continue
        plan.append(
            PlanItem(
                email,
                user_id,
                classify_member(member, current_user_id, allow_privileged),
            )
        )
    return plan


def validate_apply_confirmation(
    apply: bool,
    confirmation: str | None,
    workspace_name: str,
    workspace_id: str,
) -> None:
    if not apply:
        return
    valid = {workspace_name.casefold(), workspace_id.casefold()}
    if confirmation is None or confirmation.strip().casefold() not in valid:
        raise ValueError(
            "Apply confirmation failed: --confirm must equal the authenticated "
            "workspace name or ID"
        )


def execute_plan(
    client: SlackInternalClient,
    plan: Iterable[PlanItem],
    apply: bool,
    delay: float,
) -> list[ExecutionResult]:
    results: list[ExecutionResult] = []
    ready_seen = False
    for item in plan:
        if item.status != "ready":
            results.append(
                ExecutionResult(item.email, item.user_id, f"skipped_{item.status}")
            )
            continue
        if not apply:
            results.append(ExecutionResult(item.email, item.user_id, "would_deactivate"))
            continue
        if ready_seen and delay:
            time.sleep(delay)
        ready_seen = True
        try:
            client.deactivate(str(item.user_id))
            results.append(ExecutionResult(item.email, item.user_id, "deactivated"))
        except RuntimeError as error:
            results.append(ExecutionResult(item.email, item.user_id, "failed", str(error)))
    return results


def print_plan(plan: Iterable[PlanItem]) -> None:
    print("\nPlan:")
    for item in plan:
        print(f"  {item.status:16} {item.email} {item.user_id or '-'}")


def print_results(results: Iterable[ExecutionResult]) -> None:
    print("\nResults:")
    counts: dict[str, int] = {}
    for item in results:
        counts[item.result] = counts.get(item.result, 0) + 1
        suffix = f" ({item.error})" if item.error else ""
        print(f"  {item.result:20} {item.email}{suffix}")
    print("\nSummary: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="CSV with an email column or emails in column one")
    parser.add_argument(
        "--workspace",
        required=True,
        help="Required workspace name, ID, or subdomain safety check",
    )
    parser.add_argument("--profile", type=Path, help="Firefox profile directory")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually deactivate ready users; omitted means dry-run",
    )
    parser.add_argument(
        "--confirm",
        help="With --apply, must exactly match the authenticated workspace name or ID",
    )
    parser.add_argument(
        "--allow-privileged",
        action="store_true",
        help="Allow deactivating admins/owners (primary owner still cannot be deactivated)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between deactivation requests (default: 1.0)",
    )
    parser.add_argument(
        "--max-deactivations",
        type=int,
        default=100,
        help="Abort before applying if more than this many users are ready (default: 100)",
    )
    args = parser.parse_args(argv)
    if args.delay < 0.1:
        parser.error("--delay must be at least 0.1 seconds")
    if args.max_deactivations < 1:
        parser.error("--max-deactivations must be at least 1")
    if args.apply and not args.confirm:
        parser.error("--confirm is required with --apply")
    if args.confirm and not args.apply:
        parser.error("--confirm is only valid with --apply")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profile = args.profile.expanduser() if args.profile else default_firefox_profile()
    emails = read_emails(args.csv)
    client, auth = select_session(profile, args.workspace)

    workspace_name = str(auth.get("team") or "")
    workspace_id = str(auth.get("team_id") or "")
    current_user_id = str(auth.get("user_id") or "")
    validate_apply_confirmation(args.apply, args.confirm, workspace_name, workspace_id)

    print(
        f"Authenticated workspace: {workspace_name} ({workspace_id}); "
        f"mode: {'APPLY' if args.apply else 'DRY RUN'}"
    )
    plan = build_plan(client, emails, current_user_id, args.allow_privileged)
    print_plan(plan)
    ready_count = sum(item.status == "ready" for item in plan)
    if args.apply and ready_count > args.max_deactivations:
        raise ValueError(
            f"Plan has {ready_count} ready users, exceeding --max-deactivations "
            f"{args.max_deactivations}; nothing was changed"
        )
    results = execute_plan(client, plan, args.apply, args.delay)
    print_results(results)
    return 1 if any(result.result == "failed" for result in results) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
