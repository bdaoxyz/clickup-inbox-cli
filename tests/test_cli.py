import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from clickup_inbox_cli.cli import render_table, run
from clickup_inbox_cli.client import InboxAPIError, SessionCredentials


class CLITests(unittest.TestCase):
    def test_render_table_does_not_expose_bundle_ids(self):
        output = render_table(
            [
                {
                    "id": "private-bundle-id",
                    "task_id": "task-id",
                    "title": "Example task",
                    "unread": 2,
                    "status": "unread",
                    "updated_at": "2026-08-25T12:00:00Z",
                }
            ]
        )

        self.assertIn("Example task", output)
        self.assertNotIn("private-bundle-id", output)

    def test_render_table_neutralizes_terminal_controls(self):
        output = render_table(
            [
                {
                    "title": "hello\x1b]0;owned\x07\r\nworld",
                    "unread": 1,
                    "status": "un\x1bread",
                    "updated_at": "today\r",
                }
            ]
        )

        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)
        self.assertNotIn("\r", output)

    @patch("clickup_inbox_cli.cli.InboxClient")
    @patch("clickup_inbox_cli.cli.load_credentials")
    def test_run_json_forwards_listing_options(self, load_credentials, client_class):
        load_credentials.return_value = SessionCredentials(
            "123", "Bearer token", "csrf", "session"
        )
        client_class.return_value.list_bundles.return_value = {
            "resources": [],
            "notificationBundleGroups": [],
            "pagination": {"nextCursor": "next-page"},
        }
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            code = run(
                [
                    "list",
                    "--limit",
                    "5",
                    "--cursor",
                    "current-page",
                    "--unread",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(
            client_class.return_value.list_bundles.call_args.kwargs,
            {
                "limit": 5,
                "cursor": "current-page",
                "unread_only": True,
                "folder": "primary",
            },
        )
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"bundles": [], "next_cursor": "next-page"},
        )

    @patch("clickup_inbox_cli.cli.InboxClient")
    @patch("clickup_inbox_cli.cli.load_credentials")
    def test_run_forwards_non_primary_folder(self, load_credentials, client_class):
        load_credentials.return_value = SessionCredentials(
            "123", "Bearer token", "csrf", "session"
        )
        client_class.return_value.list_bundles.return_value = {
            "resources": [],
            "notificationBundleGroups": [],
        }

        with redirect_stdout(io.StringIO()):
            code = run(["list", "--folder", "later"])

        self.assertEqual(code, 0)
        self.assertEqual(
            client_class.return_value.list_bundles.call_args.kwargs["folder"], "later"
        )

    @patch("clickup_inbox_cli.cli.load_credentials")
    def test_run_reports_safe_api_error(self, load_credentials):
        load_credentials.side_effect = InboxAPIError("safe failure")
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            code = run(["list"])

        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "error: safe failure\n")

    @patch("clickup_inbox_cli.cli.load_credentials")
    @patch("clickup_inbox_cli.cli.InboxClient")
    def test_run_executes_state_action(self, client_class, load_credentials):
        load_credentials.return_value = SessionCredentials(
            "123", "Bearer token", "csrf", "session"
        )
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            code = run(["read", "snapshot-id"])

        self.assertEqual(code, 0)
        client_class.return_value.mark_read.assert_called_once_with("snapshot-id")
        self.assertEqual(stdout.getvalue(), "Inbox bundle marked read.\n")

    @patch("clickup_inbox_cli.cli.build_session_store")
    @patch("clickup_inbox_cli.cli.capture_browser_session")
    def test_auth_login_is_headed_and_persists_profile(
        self, capture_browser_session, build_session_store
    ):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")
        capture_browser_session.return_value = credentials
        profile = Path("/tmp/custom-clickup-profile")

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = run(
                [
                    "auth",
                    "login",
                    "--workspace-id",
                    "123",
                    "--profile-dir",
                    str(profile),
                    "--timeout",
                    "5",
                ]
            )

        self.assertEqual(code, 0)
        build_session_store.return_value.locked.assert_called_once_with()
        build_session_store.return_value.prepare.assert_called_once_with()
        capture_browser_session.assert_called_once_with(
            "123", profile_dir=profile, headless=False, timeout=5.0
        )
        build_session_store.return_value.save.assert_called_once_with(
            credentials, profile_dir=profile
        )

    @patch("clickup_inbox_cli.cli.build_session_store")
    @patch("clickup_inbox_cli.cli.capture_browser_session")
    def test_auth_refresh_is_headless_and_reuses_saved_profile(
        self, capture_browser_session, build_session_store
    ):
        saved = SessionCredentials("123", "Bearer old", "old-csrf", "old-session")
        refreshed = SessionCredentials("123", "Bearer new", "new-csrf", "new-session")
        profile = Path("/tmp/saved-clickup-profile")
        build_session_store.return_value.load_session.return_value = (saved, profile)
        capture_browser_session.return_value = refreshed

        with redirect_stdout(io.StringIO()):
            code = run(["auth", "refresh", "--timeout", "7"])

        self.assertEqual(code, 0)
        build_session_store.return_value.locked.assert_called_once_with()
        capture_browser_session.assert_called_once_with(
            "123", profile_dir=profile, headless=True, timeout=7.0
        )
        build_session_store.return_value.save.assert_called_once_with(
            refreshed, profile_dir=profile
        )

    @patch("clickup_inbox_cli.cli.build_session_store")
    def test_auth_logout_is_serialized_with_refresh(self, build_session_store):
        with redirect_stdout(io.StringIO()):
            code = run(["auth", "logout"])

        self.assertEqual(code, 0)
        build_session_store.return_value.locked.assert_called_once_with()
        build_session_store.return_value.delete.assert_called_once_with()

    @patch("clickup_inbox_cli.cli.InboxClient")
    @patch("clickup_inbox_cli.cli.load_credentials")
    def test_vm_file_store_is_used_for_normal_commands(
        self, load_credentials, client_class
    ):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")
        load_credentials.return_value = credentials
        client_class.return_value.list_bundles.return_value = {
            "resources": [],
            "notificationBundleGroups": [],
        }

        with redirect_stdout(io.StringIO()):
            code = run(
                [
                    "--credential-store",
                    "file",
                    "--session-file",
                    "/tmp/clickup-session.json",
                    "list",
                ]
            )

        self.assertEqual(code, 0)
        load_credentials.assert_called_once_with(
            store_kind="file", session_file=Path("/tmp/clickup-session.json")
        )

    @patch("clickup_inbox_cli.cli.InboxClient")
    @patch("clickup_inbox_cli.cli.load_credentials")
    def test_snooze_normalizes_timezone_and_rejects_naive_time(
        self, load_credentials, client_class
    ):
        load_credentials.return_value = SessionCredentials(
            "123", "Bearer token", "csrf", "session"
        )

        with redirect_stdout(io.StringIO()):
            code = run(
                ["snooze", "snapshot", "--until", "2026-08-25T10:00:00-05:00"]
            )
        self.assertEqual(code, 0)
        client_class.return_value.snooze.assert_called_once_with(
            "snapshot", "2026-08-25T15:00:00.000Z"
        )

        with redirect_stderr(io.StringIO()):
            code = run(["snooze", "snapshot", "--until", "2026-08-25T10:00:00"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
