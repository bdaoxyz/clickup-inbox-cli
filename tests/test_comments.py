import base64
import io
import json
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from clickup_inbox_cli.client import ConfigurationError, InboxAPIError, SessionCredentials
from clickup_inbox_cli.comments import AssignedCommentsClient, flatten_comments


def jwt_for(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"Bearer header.{encoded}.signature"


class AssignedCommentsTests(unittest.TestCase):
    def client(self, authorization=None):
        return AssignedCommentsClient(
            SessionCredentials("123", authorization or jwt_for({"user": 4219961}), "csrf", "session")
        )

    def request(self, client, response=None, **kwargs):
        with patch(
            "clickup_inbox_cli.client._open_without_redirects",
            return_value=io.BytesIO(json.dumps(response or {"comments": []}).encode()),
        ) as opener:
            result = client.list_comments(**kwargs)
        return opener.call_args.args[0], result

    def test_assigned_request_contract_and_session_headers(self):
        client = self.client()
        sent, _ = self.request(client)
        self.assertEqual(sent.method, "POST")
        url = urlsplit(sent.full_url)
        self.assertEqual(url.path, "/comment-service/v3/workspaces/123/comments/search")
        self.assertEqual(parse_qs(url.query)["fields"], [
            "thread_comment_count", "thread_latest_comment_date", "thread_has_unread_comments",
            "thread_has_unread_mentions", "thread_user_ids", "thread_unread_comment_count",
            "thread_last_read_date", "thread_follower_ids", "thread_group_follower_member_ids",
            "thread_unfollower_ids", "thread_ai_models",
        ])
        self.assertEqual(json.loads(sent.data), {
            "filters": {"parent": {"include_replies": True}, "resolved": False,
                        "assigned_to": {"user_id": 4219961}},
            "limit": 100,
        })
        self.assertEqual(sent.headers["Authorization"], client.credentials.authorization)
        self.assertEqual(sent.headers["X-csrf"], "csrf")
        self.assertEqual(sent.headers["X-workspace-id"], "123")
        self.assertEqual(sent.headers["Sessionid"], "session")

    def test_delegated_resolved_and_top_level_cursor_contract(self):
        response = {"comments": [{"id": "comment", "cursor": "comment-cursor"}],
                    "next_cursor": "response-cursor", "prev_cursor": None}
        sent, result = self.request(self.client(), response, view="delegated", resolved=True,
                                    limit=7, cursor="response-cursor")
        self.assertEqual(json.loads(sent.data), {
            "filters": {"parent": {"include_replies": True}, "resolved": True,
                        "assigned_to": {"anyone": True, "except_user_ids": [4219961, -2]},
                        "assigned_by": 4219961},
            "limit": 7, "cursor": "response-cursor",
        })
        self.assertEqual(result["next_cursor"], "response-cursor")

    def test_opaque_authorization_can_use_explicit_identity(self):
        sent, _ = self.request(self.client("Bearer opaque-secret"), user_id=27)
        self.assertEqual(json.loads(sent.data)["filters"]["assigned_to"], {"user_id": 27})
        self.assertEqual(sent.headers["Authorization"], "Bearer opaque-secret")

    def test_user_identity_supports_case_insensitive_bearer_scheme(self):
        authorization = jwt_for({"user": 27}).replace("Bearer", "bEaReR")
        sent, _ = self.request(self.client(authorization))
        self.assertEqual(json.loads(sent.data)["filters"]["assigned_to"], {"user_id": 27})

    def test_invalid_identity_fails_before_network_and_never_prints_token(self):
        authorizations = ["Bearer private-secret", jwt_for({"user": "4219961"}),
                          jwt_for({"user": True}), jwt_for({"user": -1}), jwt_for([]),
                          "Bearer x.invalid-base64!.s"]
        with patch("clickup_inbox_cli.client._open_without_redirects") as opener:
            for authorization in authorizations:
                with self.subTest(authorization=authorization), self.assertRaises(ConfigurationError) as caught:
                    self.client(authorization).list_comments()
                self.assertIn("--user-id", str(caught.exception))
                self.assertNotIn(authorization, str(caught.exception))
            opener.assert_not_called()

    def test_invalid_options_fail_before_network(self):
        cases = [{"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.2},
                 {"user_id": True}, {"user_id": 0}, {"user_id": "27"},
                 {"view": "other"}, {"cursor": 12}, {"resolved": "false"}]
        with patch("clickup_inbox_cli.client._open_without_redirects") as opener:
            for options in cases:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    self.client().list_comments(**options)
            opener.assert_not_called()

    def test_malformed_success_responses_are_rejected(self):
        responses = [{}, {"comments": None}, {"comments": ["bad"]},
                     {"comments": [], "next_cursor": 12}, {"comments": [], "prev_cursor": {}},
                     {"comments": [], "comment_threads": {}},
                     {"comments": [], "comment_threads": [None]}]
        for response in responses:
            with self.subTest(response=response), patch(
                "clickup_inbox_cli.client._open_without_redirects",
                return_value=io.BytesIO(json.dumps(response).encode()),
            ), self.assertRaisesRegex(InboxAPIError, "Assigned Comments API"):
                self.client().list_comments()

    def test_transport_errors_use_assigned_comments_label(self):
        error = HTTPError("https://example.invalid", 401, "private response", {}, io.BytesIO())
        with patch("clickup_inbox_cli.client._open_without_redirects", side_effect=
                   error):
            with self.assertRaisesRegex(InboxAPIError, "Assigned Comments API returned HTTP 401"):
                self.client().list_comments()
        error.close()

    def test_normalization_uses_root_task_for_replies_and_preserves_assignment(self):
        response = {
            "comments": [
                {"id": "root", "text_content": "Please review", "parent": "task-1", "type": 1,
                 "root_parent_id": "task-1", "root_parent_type": 1, "assignee": 27,
                 "group_assignee": None, "assigned_by": 8, "userid": 9, "resolved": False,
                 "date": "123", "date_assigned": "456", "date_resolved": None},
                {"id": "reply", "text_content": "Follow up", "parent": "root", "type": 2,
                 "root_parent_id": "task-1", "root_parent_type": 1, "assignee": None,
                 "group_assignee": {"id": "group"}, "resolved": True},
                {"id": "doc", "root_parent_id": "doc-1", "root_parent_type": 4},
            ],
            "comment_threads": [{"parent_comment_id": "root", "thread_comment_count": 3}],
        }
        rows = flatten_comments(response, "123")
        self.assertEqual(rows[0], {
            "id": "root", "text": "Please review", "parent_id": "task-1", "parent_type": 1,
            "root_parent_id": "task-1", "root_parent_type": 1, "task_id": "task-1",
            "assignee_id": 27, "group_assignee_id": None, "assigned_by_id": 8, "author_id": 9,
            "resolved": False, "created_at": "123", "assigned_at": "456", "resolved_at": None,
            "reply_count": 3, "url": "https://app.clickup.com/t/123/task-1",
        })
        self.assertEqual(rows[1]["task_id"], "task-1")
        self.assertEqual(rows[1]["parent_id"], "root")
        self.assertEqual(rows[1]["group_assignee_id"], {"id": "group"})
        self.assertEqual(rows[1]["reply_count"], 0)
        self.assertEqual(rows[2]["task_id"], "")
        self.assertEqual(rows[2]["url"], "")


if __name__ == "__main__":
    unittest.main()
