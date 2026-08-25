import json
import stat
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from clickup_inbox_cli.auth import (
    FileSessionStore,
    SessionStore,
    _credentials_from_headers,
    build_session_store,
    capture_browser_session,
    load_credentials,
    token_expiry,
)
from clickup_inbox_cli.client import ConfigurationError, SessionCredentials


class AuthTests(unittest.TestCase):
    def test_session_store_round_trips_credentials_without_logging_fields(self):
        backend = Mock()
        backend.get_password.return_value = json.dumps(
            {
                "workspace_id": "123",
                "authorization": "Bearer token",
                "csrf": "csrf-secret",
                "session_id": "session-secret",
            }
        )
        store = SessionStore(backend=backend)

        credentials = store.load()

        self.assertEqual(
            credentials,
            SessionCredentials("123", "Bearer token", "csrf-secret", "session-secret"),
        )
        backend.get_password.assert_called_once_with("clickup-inbox-cli", "session")

    def test_session_store_reports_missing_or_invalid_credentials(self):
        for stored in (None, "not-json", json.dumps({"workspace_id": "123"})):
            with self.subTest(stored=stored):
                backend = Mock()
                backend.get_password.return_value = stored
                with self.assertRaises(ConfigurationError):
                    SessionStore(backend=backend).load()

    def test_session_store_saves_and_deletes_one_keychain_item(self):
        backend = Mock()
        store = SessionStore(backend=backend)
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")

        store.save(credentials)
        payload = json.loads(backend.set_password.call_args.args[2])
        self.assertEqual(payload["workspace_id"], "123")
        self.assertEqual(payload["authorization"], "Bearer token")
        self.assertIsNone(payload["profile_dir"])

        store.delete()
        backend.delete_password.assert_called_once_with("clickup-inbox-cli", "session")

    def test_session_store_round_trips_profile_directory(self):
        backend = Mock()
        backend.get_password.return_value = json.dumps(
            {
                "workspace_id": "123",
                "authorization": "Bearer token",
                "csrf": "csrf",
                "session_id": "session",
                "profile_dir": "/tmp/clickup-profile",
            }
        )

        self.assertEqual(
            SessionStore(backend=backend).load_profile_dir(),
            Path("/tmp/clickup-profile"),
        )

    def test_file_store_round_trips_credentials_with_private_permissions(self):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")

        with tempfile.TemporaryDirectory() as temporary_directory:
            session_file = Path(temporary_directory) / "state" / "session.json"
            store = FileSessionStore(session_file)
            profile = Path(temporary_directory) / "chrome-profile"

            store.save(credentials, profile_dir=profile)

            self.assertEqual(store.load(), credentials)
            self.assertEqual(store.load_profile_dir(), profile.resolve())
            self.assertEqual(stat.S_IMODE(session_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(session_file.parent.stat().st_mode), 0o700)
            store.delete()
            self.assertFalse(session_file.exists())

    def test_file_store_lock_is_private(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_file = Path(temporary_directory) / "state" / "session.json"

            with FileSessionStore(session_file).locked():
                lock_file = session_file.with_suffix(".json.lock")
                self.assertTrue(lock_file.is_file())
                self.assertEqual(stat.S_IMODE(lock_file.stat().st_mode), 0o600)

    def test_auto_store_uses_file_off_macos(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_file = Path(temporary_directory) / "session.json"

            store = build_session_store(
                "auto", session_file=session_file, platform="linux"
            )

        self.assertIsInstance(store, FileSessionStore)
        self.assertEqual(store.path, session_file)

    @patch("clickup_inbox_cli.auth.SessionStore")
    def test_auto_store_uses_keychain_on_macos(self, session_store):
        store = build_session_store("auto", platform="darwin")

        self.assertIs(store, session_store.return_value)
        session_store.assert_called_once_with()

    def test_file_store_prepares_private_parent_before_browser_profile(self):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")

        with tempfile.TemporaryDirectory() as temporary_directory:
            application_state = Path(temporary_directory) / "clickup-inbox-cli"
            store = FileSessionStore(application_state / "session.json")

            store.prepare()
            (application_state / "chromium-profile").mkdir(parents=True)
            store.save(credentials)

            self.assertEqual(stat.S_IMODE(application_state.stat().st_mode), 0o700)
            self.assertEqual(store.load(), credentials)

    def test_failed_atomic_replace_preserves_previous_session(self):
        original = SessionCredentials("123", "Bearer original", "csrf", "session")
        replacement = SessionCredentials("123", "Bearer replacement", "csrf", "session")

        with tempfile.TemporaryDirectory() as temporary_directory:
            session_file = Path(temporary_directory) / "state" / "session.json"
            store = FileSessionStore(session_file)
            store.save(original)

            with patch("clickup_inbox_cli.auth.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(ConfigurationError, "Could not write"):
                    store.save(replacement)

            self.assertEqual(store.load(), original)
            self.assertEqual(list(session_file.parent.glob(".session.json.*")), [])

    def test_file_store_wraps_delete_errors(self):
        store = FileSessionStore(Path("/tmp/clickup-session.json"))

        with patch.object(Path, "unlink", side_effect=OSError("read only")):
            with self.assertRaisesRegex(ConfigurationError, "Could not remove"):
                store.delete()

    def test_file_store_refuses_a_shared_parent_directory(self):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")

        with tempfile.TemporaryDirectory() as temporary_directory:
            shared_directory = Path(temporary_directory) / "shared"
            shared_directory.mkdir(mode=0o755)
            shared_directory.chmod(0o755)

            with self.assertRaisesRegex(ConfigurationError, "permissions"):
                FileSessionStore(shared_directory / "session.json").save(credentials)

            self.assertEqual(stat.S_IMODE(shared_directory.stat().st_mode), 0o755)

    def test_invalid_store_name_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "credential store"):
            build_session_store("vault", platform="linux")

    def test_token_expiry_decodes_jwt_without_exposing_it(self):
        # {"exp": 1790000000}, encoded without a signature because this only parses metadata.
        authorization = "Bearer eyJhbGciOiJub25lIn0.eyJleHAiOjE3OTAwMDAwMDB9."

        self.assertEqual(token_expiry(authorization), 1790000000)
        self.assertIsNone(token_expiry("Bearer opaque-token"))

    def test_request_headers_require_complete_session_bundle(self):
        self.assertEqual(
            _credentials_from_headers(
                "123",
                {
                    "Authorization": "Bearer token",
                    "X-CSRF": "csrf",
                    "sessionId": "session",
                },
            ),
            SessionCredentials("123", "Bearer token", "csrf", "session"),
        )
        self.assertIsNone(
            _credentials_from_headers("123", {"Authorization": "Bearer token"})
        )

    def test_expiring_keychain_session_refreshes_headlessly(self):
        store = Mock()
        stored = SessionCredentials(
            "123",
            "Bearer eyJhbGciOiJub25lIn0.eyJleHAiOjE3OTAwMDAwMDB9.",
            "old-csrf",
            "old-session",
        )
        store.load_session.return_value = (
            stored,
            Path("/custom/clickup-profile"),
        )
        store.locked.return_value = nullcontext()
        refreshed = SessionCredentials(
            "123", "Bearer refreshed", "new-csrf", "new-session"
        )
        refresher = Mock(return_value=refreshed)

        result = load_credentials(
            env={}, store=store, now=1790000000, refresher=refresher
        )

        self.assertEqual(result, refreshed)
        refresher.assert_called_once_with(
            "123",
            profile_dir=Path("/custom/clickup-profile"),
            headless=True,
            timeout=60.0,
        )
        store.save.assert_called_once_with(
            refreshed, profile_dir=Path("/custom/clickup-profile")
        )
        store.locked.assert_called_once_with()

    def test_valid_keychain_session_does_not_start_browser(self):
        store = Mock()
        stored = SessionCredentials(
            "123",
            "Bearer eyJhbGciOiJub25lIn0.eyJleHAiOjE3OTAwMDAwMDB9.",
            "csrf",
            "session",
        )
        store.load_session.return_value = (stored, None)
        store.locked.return_value = nullcontext()
        refresher = Mock()

        self.assertEqual(
            load_credentials(env={}, store=store, now=1780000000, refresher=refresher),
            stored,
        )
        refresher.assert_not_called()
        store.locked.assert_called_once_with()

    @patch("playwright.sync_api.sync_playwright")
    def test_browser_capture_accepts_only_exact_clickup_api_origin(self, sync_playwright):
        request_handler = None
        page = Mock()

        def register(_event, handler):
            nonlocal request_handler
            request_handler = handler

        page.on.side_effect = register
        page.wait_for_timeout.side_effect = lambda _milliseconds: (
            request_handler(
                Mock(
                    url="https://evil.example/?next=https://frontdoor-prod-us-east-2-1.clickup.com/inbox/v3/workspaces/123/notifications/x",
                    all_headers=Mock(
                        return_value={
                            "authorization": "Bearer stolen",
                            "x-csrf": "stolen",
                            "sessionid": "stolen",
                        }
                    ),
                )
            ),
            request_handler(
                Mock(
                    url="https://frontdoor-prod-us-east-2-1.clickup.com/inbox/v3/workspaces/123/notifications/bundles",
                    all_headers=Mock(
                        return_value={
                            "authorization": "Bearer token",
                            "x-csrf": "csrf",
                            "sessionid": "session",
                        }
                    ),
                )
            ),
        )
        context = Mock(pages=[page])
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = context
        sync_playwright.return_value.__enter__.return_value = playwright

        with tempfile.TemporaryDirectory() as profile:
            result = capture_browser_session(
                "123", profile_dir=Path(profile), headless=True, timeout=1
            )

        self.assertEqual(result.authorization, "Bearer token")
        playwright.chromium.launch_persistent_context.assert_called_once_with(
            profile, channel="chrome", headless=True
        )
        context.close.assert_called_once()

    @patch("playwright.sync_api.sync_playwright")
    def test_browser_capture_timeout_closes_context(self, sync_playwright):
        page = Mock()
        context = Mock(pages=[page])
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = context
        sync_playwright.return_value.__enter__.return_value = playwright

        with tempfile.TemporaryDirectory() as profile:
            with self.assertRaisesRegex(ConfigurationError, "No authenticated Inbox"):
                capture_browser_session(
                    "123", profile_dir=Path(profile), headless=False, timeout=0
                )

        context.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
