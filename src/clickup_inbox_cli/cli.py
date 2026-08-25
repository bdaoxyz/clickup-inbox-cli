from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import __version__
from .client import (
    ConfigurationError,
    InboxAPIError,
    InboxClient,
    SessionCredentials,
    flatten_bundles,
)
from .auth import (
    build_session_store,
    capture_browser_session,
    default_profile_dir,
    load_credentials,
    token_expiry,
)


STATE_ACTIONS = {
    "read": ("Mark an Inbox bundle read", "mark_read", "Inbox bundle marked read."),
    "unread": (
        "Mark an Inbox bundle unread",
        "mark_unread",
        "Inbox bundle marked unread.",
    ),
    "clear": ("Move an Inbox bundle to Cleared", "clear", "Inbox bundle cleared."),
    "unclear": (
        "Restore a cleared Inbox bundle",
        "unclear",
        "Inbox bundle restored.",
    ),
    "unsnooze": (
        "Return a snoozed bundle to Primary",
        "unsnooze",
        "Inbox bundle unsnoozed.",
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clickup-inbox",
        description="Experimental CLI for ClickUp's private Inbox API.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--credential-store",
        choices=("auto", "keychain", "file"),
        default=os.environ.get("CLICKUP_INBOX_CREDENTIAL_STORE", "auto"),
        help="Credential storage backend (default: Keychain on macOS, file elsewhere)",
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        default=(
            Path(os.environ["CLICKUP_INBOX_SESSION_FILE"])
            if os.environ.get("CLICKUP_INBOX_SESSION_FILE")
            else None
        ),
        help="Private credential file path for the file backend",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List Primary Inbox bundles")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--cursor", default="", help="Pagination cursor")
    list_parser.add_argument("--unread", action="store_true", help="Only unread bundles")
    list_parser.add_argument(
        "--folder", choices=("primary", "later", "cleared"), default="primary"
    )
    list_parser.add_argument("--json", action="store_true", help="Emit JSON")

    for name, (help_text, _method_name, _success_message) in STATE_ACTIONS.items():
        action_parser = subparsers.add_parser(name, help=help_text)
        action_parser.add_argument("bundle_snapshot_id")

    snooze_parser = subparsers.add_parser("snooze", help="Move a bundle to Later")
    snooze_parser.add_argument("bundle_snapshot_id")
    snooze_parser.add_argument(
        "--until", required=True, help="Timezone-aware ISO-8601 date and time"
    )

    auth_parser = subparsers.add_parser("auth", help="Manage persistent authentication")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)
    login_parser = auth_subparsers.add_parser("login", help="Create or renew browser login")
    login_parser.add_argument(
        "--workspace-id", default=os.environ.get("CLICKUP_WORKSPACE_ID")
    )
    login_parser.add_argument("--profile-dir", type=Path)
    login_parser.add_argument("--timeout", type=float, default=300.0)
    refresh_parser = auth_subparsers.add_parser(
        "refresh", help="Renew credentials using the saved browser session"
    )
    refresh_parser.add_argument("--profile-dir", type=Path)
    refresh_parser.add_argument("--timeout", type=float, default=60.0)
    auth_subparsers.add_parser("status", help="Show saved session status")
    auth_subparsers.add_parser("logout", help="Remove short-lived credentials")
    return parser


def sanitize_terminal_text(value: object) -> str:
    return "".join(character if character.isprintable() else " " for character in str(value))


def render_table(rows: list[dict[str, object]]) -> str:
    if rows:
        header = f"{'UNREAD':>6}  {'STATUS':<10}  {'UPDATED':<24}  TITLE"
        lines = [header]
        for row in rows:
            title = sanitize_terminal_text(row["title"])
            lines.append(
                f"{row['unread']:>6}  {sanitize_terminal_text(row['status']):<10.10}  "
                f"{sanitize_terminal_text(row['updated_at']):<24.24}  {title}"
            )
    else:
        lines = ["No Inbox notification bundles found."]
    return "\n".join(lines)


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "auth":
            return _run_auth(args)

        credentials = load_credentials(
            store_kind=args.credential_store, session_file=args.session_file
        )
        client = InboxClient(credentials)
        if args.command == "list":
            response = client.list_bundles(
                limit=args.limit,
                cursor=args.cursor,
                unread_only=args.unread,
                folder=args.folder,
            )
            rows = flatten_bundles(response)
            if args.json:
                pagination = response.get("pagination", {})
                next_cursor = pagination.get("nextCursor") if pagination else None
                print(
                    json.dumps(
                        {"bundles": rows, "next_cursor": next_cursor or None}, indent=2
                    )
                )
            else:
                print(render_table(rows))
            return 0
        if args.command == "snooze":
            client.snooze(args.bundle_snapshot_id, _normalize_timestamp(args.until))
            print("Inbox bundle snoozed.")
            return 0
        if args.command in STATE_ACTIONS:
            _help_text, method_name, message = STATE_ACTIONS[args.command]
            getattr(client, method_name)(args.bundle_snapshot_id)
            print(message)
            return 0
    except (ConfigurationError, InboxAPIError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


def _run_auth(args: argparse.Namespace) -> int:
    store = build_session_store(
        args.credential_store, session_file=args.session_file
    )
    if args.auth_command == "login":
        if not args.workspace_id:
            raise ConfigurationError(
                "workspace ID is required; pass --workspace-id or set CLICKUP_WORKSPACE_ID"
            )
        print("Opening the dedicated ClickUp login profile…", file=sys.stderr)
        profile_dir = args.profile_dir or default_profile_dir()
        with store.locked():
            store.prepare()
            credentials = capture_browser_session(
                args.workspace_id,
                profile_dir=profile_dir,
                headless=False,
                timeout=args.timeout,
            )
            store.save(credentials, profile_dir=profile_dir)
        print(f"ClickUp Inbox session saved in {store.description}.")
        return 0
    if args.auth_command == "refresh":
        with store.locked():
            saved, stored_profile = store.load_session()
            profile_dir = args.profile_dir or stored_profile or default_profile_dir()
            credentials = capture_browser_session(
                saved.workspace_id,
                profile_dir=profile_dir,
                headless=True,
                timeout=args.timeout,
            )
            store.save(credentials, profile_dir=profile_dir)
        print("ClickUp Inbox session refreshed.")
        return 0
    if args.auth_command == "status":
        credentials = store.load()
        expiry = token_expiry(credentials.authorization)
        if expiry is None:
            print(f"Saved ClickUp Inbox session for workspace {credentials.workspace_id}.")
        else:
            expires_at = datetime.fromtimestamp(expiry, tz=timezone.utc)
            state = "expired" if expiry <= datetime.now(tz=timezone.utc).timestamp() else "valid"
            print(
                f"Saved ClickUp Inbox session for workspace {credentials.workspace_id}: "
                f"{state}, access token expires {expires_at.isoformat()}."
            )
        return 0
    if args.auth_command == "logout":
        with store.locked():
            store.delete()
        print(f"Saved ClickUp Inbox credentials removed from {store.description}.")
        return 0
    return 1


def _normalize_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--until must be a valid ISO-8601 date and time") from exc
    if parsed.tzinfo is None:
        raise ValueError("--until must include a timezone")
    utc_value = parsed.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
