# utilities

A repository of utility scripts.

## Slack bulk user deactivation

`slack_bulk_deactivate.py` bulk-deactivates Slack members from a CSV file by
reusing the authenticated Slack web session in a local Firefox profile. It
uses Slack's unsupported internal `users.admin.setInactive` endpoint, which
may change without notice.

The script is intentionally a dry run unless `--apply` is supplied. Apply mode
also requires `--confirm` to exactly match the authenticated workspace name or
ID. It refuses to deactivate the authenticated user, bots, admins, and owners
by default, and it never prints the recovered Slack credential or cookie.

CSV format:

```csv
email
person@example.com
another@example.com
```

Preview changes:

```bash
python3 slack_bulk_deactivate.py users.csv --workspace WorkspaceName
```

Apply the reviewed plan:

```bash
python3 slack_bulk_deactivate.py users.csv \
  --workspace WorkspaceName \
  --apply \
  --confirm WorkspaceName
```

Run the offline unit tests:

```bash
python3 -m unittest -v test_slack_bulk_deactivate.py
```

## QR code generator

`qr_code.py` generates a QR-code image from a URL.
