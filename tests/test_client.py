import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from clickup_inbox_cli.client import (
    ConfigurationError,
    InboxAPIError,
    InboxClient,
    SessionCredentials,
    flatten_bundles,
)


class ClientTests(unittest.TestCase):
    def test_credentials_require_all_session_fields(self):
        with self.assertRaises(ConfigurationError) as caught:
            SessionCredentials.from_env({"CLICKUP_WORKSPACE_ID": "123"})

        self.assertIn("CLICKUP_INBOX_AUTHORIZATION", str(caught.exception))
        self.assertIn("CLICKUP_INBOX_CSRF", str(caught.exception))
        self.assertIn("CLICKUP_INBOX_SESSION_ID", str(caught.exception))

    def test_credentials_normalize_bearer_scheme(self):
        credentials = SessionCredentials.from_env(
            {
                "CLICKUP_WORKSPACE_ID": "123",
                "CLICKUP_INBOX_AUTHORIZATION": "secret-token",
                "CLICKUP_INBOX_CSRF": "csrf",
                "CLICKUP_INBOX_SESSION_ID": "session",
            }
        )

        self.assertEqual(credentials.authorization, "Bearer secret-token")

    def test_list_bundles_matches_observed_request_contract(self):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")
        client = InboxClient(credentials)
        response = {
            "resources": [],
            "notificationBundleGroups": [],
            "pagination": {"nextCursor": "next-page"},
        }

        fake_response = io.BytesIO(json.dumps(response).encode())
        with patch(
            "clickup_inbox_cli.client._open_without_redirects",
            return_value=fake_response,
        ) as request:
            self.assertEqual(
                client.list_bundles(
                    limit=7, cursor="current-page", unread_only=True
                ),
                response,
            )

        sent = request.call_args.args[0]
        payload = json.loads(sent.data)
        self.assertTrue(
            sent.full_url.endswith(
                "/inbox/v3/workspaces/123/notifications/bundles/search"
            )
        )
        self.assertEqual(
            payload["filteredBy"],
            {
                "bundleType": "messages",
                "status": "uncleared",
                "assignedToMe": False,
                "mentioned": False,
                "unread": True,
                "reminders": False,
                "saved": False,
            },
        )
        self.assertEqual(
            payload["pagination"], {"nextCursor": "current-page", "limit": 7}
        )
        self.assertEqual(sent.headers["User-agent"], "clickup-inbox-cli/0.3.0")

    def test_list_bundles_supports_later_and_cleared_folders(self):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")
        client = InboxClient(credentials)
        response = {"resources": [], "notificationBundleGroups": []}

        for folder, expected_status in (("later", "snoozed"), ("cleared", "cleared")):
            with self.subTest(folder=folder), patch(
                "clickup_inbox_cli.client._open_without_redirects",
                return_value=io.BytesIO(json.dumps(response).encode()),
            ) as request:
                client.list_bundles(folder=folder)

            payload = json.loads(request.call_args.args[0].data)
            self.assertEqual(payload["filteredBy"]["status"], expected_status)
            self.assertNotIn("bundleType", payload["filteredBy"])

    def test_bundle_state_mutations_match_observed_contracts(self):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")
        client = InboxClient(credentials)
        snapshot_id = "wsid=123#uid=7##re=clickup:task:123:abc#s=uncleared"

        cases = [
            ("mark_read", (), "PUT", "/notifications/bundles/read", {"bundleSnapshotId": snapshot_id}),
            ("mark_unread", (), "PUT", "/notifications/bundles/unread", {"bundleSnapshotId": snapshot_id}),
            ("unsnooze", (), "PUT", "/notifications/bundles/unsnooze", {"bundleSnapshotId": snapshot_id}),
            (
                "snooze",
                ("2026-08-26T13:00:00.000Z",),
                "PUT",
                "/notifications/bundles/snooze",
                {"bundleSnapshotId": snapshot_id, "snoozedUntil": "2026-08-26T13:00:00.000Z"},
            ),
            ("clear", (), "PUT", "/notifications/bundles/wsid%3D123%23uid%3D7%23%23re%3Dclickup%3Atask%3A123%3Aabc%23s%3Duncleared/clear", {}),
            ("unclear", (), "PUT", "/notifications/bundles/wsid%3D123%23uid%3D7%23%23re%3Dclickup%3Atask%3A123%3Aabc%23s%3Duncleared/unclear", {}),
        ]

        for method_name, extra_args, expected_method, path_suffix, expected_payload in cases:
            with self.subTest(method=method_name), patch(
                "clickup_inbox_cli.client._open_without_redirects",
                return_value=io.BytesIO(b""),
            ) as request:
                getattr(client, method_name)(snapshot_id, *extra_args)

            sent = request.call_args.args[0]
            self.assertEqual(sent.method, expected_method)
            self.assertTrue(sent.full_url.endswith(path_suffix))
            self.assertEqual(json.loads(sent.data), expected_payload)

    def test_mutations_reject_empty_snapshot_ids(self):
        client = InboxClient(SessionCredentials("123", "Bearer token", "csrf", "session"))

        for method_name in ("mark_read", "mark_unread", "clear", "unclear", "unsnooze"):
            with self.subTest(method=method_name), self.assertRaises(ValueError):
                getattr(client, method_name)("")

    def test_list_rejects_malformed_success_response(self):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")
        client = InboxClient(credentials)
        for response in ({}, {"resources": None, "notificationBundleGroups": []}):
            with self.subTest(response=response), patch(
                "clickup_inbox_cli.client._open_without_redirects",
                return_value=io.BytesIO(json.dumps(response).encode()),
            ):
                with self.assertRaises(InboxAPIError):
                    client.list_bundles()

    def test_transport_and_json_errors_are_safely_wrapped(self):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")
        client = InboxClient(credentials)
        failures = [
            HTTPError("https://example.invalid", 401, "no", {}, None),
            URLError("offline"),
            TimeoutError("timed out"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch(
                "clickup_inbox_cli.client._open_without_redirects",
                side_effect=failure,
            ):
                with self.assertRaises(InboxAPIError):
                    client.list_bundles()

        for body in (b"not-json", b"[]"):
            with self.subTest(body=body), patch(
                "clickup_inbox_cli.client._open_without_redirects",
                return_value=io.BytesIO(body),
            ):
                with self.assertRaises(InboxAPIError):
                    client.list_bundles()

    def test_limit_boundaries_are_enforced(self):
        credentials = SessionCredentials("123", "Bearer token", "csrf", "session")
        client = InboxClient(credentials)
        for limit in (0, 101):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                client.list_bundles(limit=limit)

    def test_flatten_bundles_resolves_task_title(self):
        response = {
            "resources": [
                {
                    "entityResourceName": "clickup:task:123:abc",
                    "type": "task",
                    "name": "Example task",
                }
            ],
            "notificationBundleGroups": [
                {
                    "type": "time-range",
                    "notificationBundles": [
                        {
                            "id": "bundle-1",
                            "rootEntityResourceName": "clickup:task:123:abc",
                            "unreadCount": 3,
                            "status": "unread",
                            "mostRecentNotificationTime": "2026-08-25T12:00:00Z",
                            "hasAssignment": True,
                            "hasMention": False,
                        }
                    ],
                }
            ],
        }

        self.assertEqual(
            flatten_bundles(response),
            [
                {
                    "id": "bundle-1",
                    "task_id": "abc",
                    "title": "Example task",
                    "unread": 3,
                    "status": "unread",
                    "updated_at": "2026-08-25T12:00:00Z",
                    "has_assignment": True,
                    "has_mention": False,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
