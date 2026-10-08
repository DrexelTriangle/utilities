import csv
import tempfile
import unittest
from pathlib import Path

import slack_bulk_deactivate as app


class FakeClient:
    def __init__(self):
        self.deactivated = []

    def deactivate(self, user_id: str) -> dict:
        self.deactivated.append(user_id)
        return {"ok": True}


class CsvTests(unittest.TestCase):
    def test_read_emails_normalizes_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["email"])
                writer.writerow([" Alice@Example.com "])
                writer.writerow(["alice@example.com"])
                writer.writerow(["bob@example.com"])

            self.assertEqual(
                app.read_emails(path),
                ["alice@example.com", "bob@example.com"],
            )

    def test_read_emails_rejects_invalid_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.csv"
            path.write_text("email\nnot-an-email\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid email"):
                app.read_emails(path)


class MatchingTests(unittest.TestCase):
    def test_fetch_members_uses_paginated_users_list(self):
        class CapturingClient(app.SlackInternalClient):
            def __init__(self):
                self.calls = []

            def api(self, method, parameters, host=None):
                self.calls.append((method, parameters))
                if len(self.calls) == 1:
                    return {
                        "ok": True,
                        "members": [
                            {"id": "U1", "profile": {"email": "alice@example.com"}},
                            {"id": "U2", "profile": {"email": "other@example.com"}},
                        ],
                        "response_metadata": {"next_cursor": "next"},
                    }
                return {
                    "ok": True,
                    "members": [
                        {"id": "U3", "profile": {"email": "Alice@Example.com"}}
                    ],
                    "response_metadata": {"next_cursor": ""},
                }

        client = CapturingClient()
        members = client.fetch_members("alice@example.com")

        self.assertEqual([call[0] for call in client.calls], ["users.list", "users.list"])
        self.assertEqual([member["id"] for member in members], ["U1", "U3"])

    def test_exact_member_match_is_case_insensitive(self):
        items = [
            {"id": "U1", "profile": {"email": "Alice@Example.com"}},
            {"id": "U2", "profile": {"email": "other@example.com"}},
        ]

        self.assertEqual(
            app.exact_member_match(items, "alice@example.com")["id"],
            "U1",
        )

    def test_fuzzy_only_result_is_not_accepted(self):
        items = [{"id": "U1", "profile": {"email": "alice+old@example.com"}}]

        self.assertIsNone(app.exact_member_match(items, "alice@example.com"))


class PlanningTests(unittest.TestCase):
    def test_classify_member_blocks_self_bots_and_privileged_users(self):
        self.assertEqual(app.classify_member({"id": "ME"}, "ME", False), "self")
        self.assertEqual(
            app.classify_member({"id": "B", "is_bot": True}, "ME", False),
            "bot",
        )
        self.assertEqual(
            app.classify_member({"id": "A", "is_admin": True}, "ME", False),
            "privileged",
        )
        self.assertEqual(
            app.classify_member({"id": "A", "is_admin": True}, "ME", True),
            "ready",
        )
        self.assertEqual(
            app.classify_member(
                {"id": "P", "is_primary_owner": True}, "ME", True
            ),
            "primary_owner",
        )
        self.assertEqual(
            app.classify_member({"id": "D", "deleted": True}, "ME", False),
            "already_inactive",
        )

    def test_dry_run_never_deactivates(self):
        client = FakeClient()
        plan = [app.PlanItem("a@example.com", "U1", "ready")]

        results = app.execute_plan(client, plan, apply=False, delay=0)

        self.assertEqual(client.deactivated, [])
        self.assertEqual(results[0].result, "would_deactivate")

    def test_apply_only_deactivates_ready_items(self):
        client = FakeClient()
        plan = [
            app.PlanItem("a@example.com", "U1", "ready"),
            app.PlanItem("b@example.com", "U2", "privileged"),
            app.PlanItem("missing@example.com", None, "not_found"),
        ]

        results = app.execute_plan(client, plan, apply=True, delay=0)

        self.assertEqual(client.deactivated, ["U1"])
        self.assertEqual([result.result for result in results], [
            "deactivated",
            "skipped_privileged",
            "skipped_not_found",
        ])

    def test_apply_confirmation_must_match_workspace(self):
        with self.assertRaisesRegex(ValueError, "confirmation"):
            app.validate_apply_confirmation(True, "wrong", "Taut", "T123")
        app.validate_apply_confirmation(True, "Taut", "Taut", "T123")
        app.validate_apply_confirmation(True, "T123", "Taut", "T123")
        app.validate_apply_confirmation(False, None, "Taut", "T123")

    def test_workspace_match_accepts_name_id_and_subdomain(self):
        auth = {
            "team": "Taut",
            "team_id": "T123",
            "url": "https://taut-world.slack.com/",
        }

        self.assertTrue(app.workspace_matches("Taut", auth))
        self.assertTrue(app.workspace_matches("T123", auth))
        self.assertTrue(app.workspace_matches("taut-world.slack.com", auth))
        self.assertFalse(app.workspace_matches("other", auth))

    def test_apply_requires_confirmation_during_argument_parsing(self):
        with self.assertRaises(SystemExit):
            app.parse_args(["users.csv", "--workspace", "Taut", "--apply"])

    def test_cli_rejects_unsafe_delay_and_deactivation_limit(self):
        with self.assertRaises(SystemExit):
            app.parse_args(["users.csv", "--workspace", "Taut", "--delay", "0"])
        with self.assertRaises(SystemExit):
            app.parse_args(
                ["users.csv", "--workspace", "Taut", "--max-deactivations", "0"]
            )


if __name__ == "__main__":
    unittest.main()
