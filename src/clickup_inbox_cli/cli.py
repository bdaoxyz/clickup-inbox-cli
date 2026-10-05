from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import __version__
from .client import (
    ConfigurationError,
    InboxAPIError,
    InboxClient,
    SessionCredentials,
    flatten_bundles,
)
from .comments import AssignedCommentsClient, flatten_comments
from .replies import RepliesClient, flatten_threads
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
        description="Experimental CLI for ClickUp Inbox, Assigned Comments, and Replies.",
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

    comments_parser = subparsers.add_parser(
        "comments", help="List Assigned Comments using the saved browser session"
    )
    comment_views = comments_parser.add_subparsers(dest="comment_view", required=True)
    for view, help_text in (
        ("assigned", "Comments assigned to me"),
        ("delegated", "Comments delegated by me to others"),
    ):
        view_parser = comment_views.add_parser(view, help=help_text)
        view_parser.add_argument("--limit", type=int, default=100, help="Page size (1–100)")
        view_parser.add_argument("--cursor", default="", help="Response pagination cursor")
        view_parser.add_argument("--all", action="store_true", help="Fetch all remaining pages")
        view_parser.add_argument(
            "--resolved", action="store_true", help="Only resolved comments (default: unresolved)"
        )
        view_parser.add_argument(
            "--user-id", type=int, help="Override the current user ID inferred from the session"
        )
        view_parser.add_argument("--json", action="store_true", help="Emit JSON")

    replies_parser = subparsers.add_parser(
        "replies", help="List Home Replies using the saved browser session"
    )
    reply_views = replies_parser.add_subparsers(dest="reply_view", required=True)
    for view in ("unread", "read"):
        view_parser = reply_views.add_parser(view, help=f"Threads with {view} replies")
        view_parser.add_argument("--limit", type=int, default=15, help="Page size (1–100)")
        view_parser.add_argument("--cursor", default="", help="Response pagination cursor")
        view_parser.add_argument("--all", action="store_true", help="Fetch all remaining pages")
        view_parser.add_argument("--json", action="store_true", help="Emit JSON")

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


def render_comments_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No assigned comments found."
    lines = [f"{'STATUS':<10}  {'ASSIGNEE':<12}  {'TASK / PARENT':<16}  COMMENT"]
    for row in rows:
        status = "resolved" if row["resolved"] else "open"
        assignee = sanitize_terminal_text(row["assignee_id"] or row["group_assignee_id"] or "")
        parent = sanitize_terminal_text(row["task_id"] or row["parent_id"])
        text = sanitize_terminal_text(row["text"])
        lines.append(f"{status:<10}  {assignee:<12}  {parent:<16}  {text}")
    return "\n".join(lines)


def render_replies_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No reply threads found."
    lines = [f"{'STATUS':<8}  {'REPLIES':>7}  {'UNREAD':>6}  {'CHANNEL / TASK':<18}  PARENT MESSAGE"]
    for row in rows:
        status = sanitize_terminal_text(row["read_status"])
        count = sanitize_terminal_text(row["reply_count"] if row["reply_count"] is not None else "-")
        unread = sanitize_terminal_text(row["unread_count"] if row["unread_count"] is not None else "-")
        parent = sanitize_terminal_text(row["channel_id"] or row["task_id"] or row["root_parent_id"])
        text = sanitize_terminal_text(row["text"])
        if row["parent_status"] != "found":
            text = f"(parent unavailable: {sanitize_terminal_text(row['parent_status'])})"
        lines.append(f"{status:<8}  {count:>7}  {unread:>6}  {parent:<18}  {text}")
    return "\n".join(lines)


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "auth":
            return _run_auth(args)

        credentials = load_credentials(
            store_kind=args.credential_store, session_file=args.session_file
        )
        if args.command == "comments":
            return _run_comments(args, credentials)
        if args.command == "replies":
            return _run_replies(args, credentials)
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


def _run_comments(args: argparse.Namespace, credentials: SessionCredentials) -> int:
    client = AssignedCommentsClient(credentials)
    rows: list[dict[str, object]] = []
    next_cursor = None
    for response in _iter_pages(
        lambda cursor: client.list_comments(
            view=args.comment_view,
            limit=args.limit,
            cursor=cursor,
            resolved=args.resolved,
            user_id=args.user_id,
        ),
        cursor=args.cursor, all_pages=args.all, api_name=client.api_name,
    ):
        rows.extend(flatten_comments(response, credentials.workspace_id))
        next_cursor = response.get("next_cursor") or None
    if args.json:
        print(json.dumps({"comments": rows, "next_cursor": next_cursor}, indent=2))
    else:
        print(render_comments_table(rows))
        if next_cursor:
            print("More comments available; use --all or --json to get the next cursor.")
    return 0


def _run_replies(args: argparse.Namespace, credentials: SessionCredentials) -> int:
    client = RepliesClient(credentials)
    rows: list[dict[str, object]] = []
    next_cursor = None
    for response in _iter_pages(
        lambda cursor: client.list_threads(
            read_status=args.reply_view, limit=args.limit, cursor=cursor,
        ),
        cursor=args.cursor, all_pages=args.all, api_name=client.api_name,
    ):
        parents = client.get_parent_comments(
            [thread["parent_comment_id"] for thread in response["chat_threads"]]
        )
        rows.extend(flatten_threads(response, parents, credentials.workspace_id, args.reply_view))
        next_cursor = response.get("next_cursor") or None
    if args.json:
        print(json.dumps({"threads": rows, "next_cursor": next_cursor}, indent=2))
    else:
        print(render_replies_table(rows))
        if next_cursor:
            print("More reply threads available; use --all or --json to get the next cursor.")
    return 0


def _iter_pages(
    fetch_page: Callable[[str], dict[str, Any]],
    *, cursor: str, all_pages: bool, api_name: str,
) -> Iterator[dict[str, Any]]:
    seen_cursors = {cursor}
    while True:
        response = fetch_page(cursor)
        yield response
        next_cursor = response.get("next_cursor") or None
        if not all_pages or not next_cursor:
            return
        if next_cursor in seen_cursors:
            raise InboxAPIError(f"{api_name} returned a repeated pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


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
