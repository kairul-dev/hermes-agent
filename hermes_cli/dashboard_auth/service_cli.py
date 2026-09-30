"""Local operator provisioning: python -m hermes_cli.dashboard_auth.service_cli.

This entry deliberately does not import the interactive launcher/bootstrap.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import sqlite3

from .local_service import ServiceStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Provision generic loopback service identities")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--principal", required=True)
    create.add_argument("--expires", required=True, help="ISO-8601 timestamp with timezone")
    create.add_argument("--grants", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    sub.add_parser("list")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("credential_id")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("credential_id")
    args = parser.parse_args(argv)
    store = ServiceStore()
    try:
        if args.command == "create":
            expires = datetime.fromisoformat(args.expires.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise ValueError("Expiry requires a timezone")
            result = store.create(args.principal, int(expires.timestamp()), json.loads(args.grants.read_text(encoding="utf-8")), args.output)
        elif args.command == "revoke":
            store.revoke(args.credential_id)
            result = store.metadata(args.credential_id)
        else:
            result = store.metadata(args.credential_id if args.command == "inspect" else None)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error):
        print("Service identity operation failed; check grants, expiry and private file permissions", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
