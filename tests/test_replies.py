import io
import json
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from clickup_inbox_cli.client import InboxAPIError, SessionCredentials
from clickup_inbox_cli.comments import _THREAD_FIELDS
from clickup_inbox_cli.replies import RepliesClient, flatten_threads


def thread(parent="message", root="channel", root_type=8, **extra):
    return {
        "parent_comment_id": parent, "root_parent_id": root, "root_parent_type": root_type,
        "read_thread_comment_ids": ["read-reply"], "unread_thread_comment_ids": ["unread-reply"],
        "has_more_read": True, "has_more_unread": False, **extra,
    }


class RepliesTests(unittest.TestCase):
    def client(self):
        return RepliesClient(SessionCredentials("123", "Bearer opaque-secret", "csrf", "session"))

    def request(self, response, method="list_threads", **options):
        with patch("clickup_inbox_cli.client._open_without_redirects",
                   return_value=io.BytesIO(json.dumps(response).encode())) as opener:
            result = getattr(self.client(), method)(**options)
        return opener.call_args.args[0], result

    def test_default_listing_is_authenticated_get_without_body(self):
        sent, result = self.request({"chat_threads": [thread()]})
        self.assertEqual(sent.method, "GET")
        self.assertIsNone(sent.data)
        url = urlsplit(sent.full_url)
        self.assertEqual(url.path, "/chat/v1/workspaces/123/chat/threads")
        self.assertEqual(parse_qs(url.query), {"read_status": ["unread"], "limit": ["15"]})
        self.assertEqual(sent.headers["Authorization"], "Bearer opaque-secret")
        self.assertEqual(sent.headers["X-csrf"], "csrf")
        self.assertEqual(sent.headers["X-workspace-id"], "123")
        self.assertEqual(sent.headers["Sessionid"], "session")
        self.assertEqual(result["chat_threads"][0]["parent_comment_id"], "message")

    def test_read_listing_preserves_encoded_response_cursor(self):
        cursor = "page/+&?= cursor"
        sent, result = self.request({"chat_threads": [], "next_cursor": "next-page", "prev_cursor": None},
                                    read_status="read", limit=3, cursor=cursor)
        self.assertEqual(parse_qs(urlsplit(sent.full_url).query),
                         {"read_status": ["read"], "limit": ["3"], "cursor": [cursor]})
        self.assertEqual(result["next_cursor"], "next-page")

    def test_invalid_controls_fail_before_network(self):
        with patch("clickup_inbox_cli.client._open_without_redirects") as opener:
            for options in ({"read_status": "all"}, {"limit": 0}, {"limit": 101},
                            {"limit": True}, {"limit": 1.5}, {"cursor": 3}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    self.client().list_threads(**options)
            opener.assert_not_called()

    def test_parent_preview_request_reuses_thread_metadata_fields(self):
        response = {"comments": [{"object_id": "message", "status": "found", "data": {"id": "message"}}],
                    "comment_threads": [{"parent_comment_id": "message", "thread_comment_count": 5}]}
        sent, result = self.request(response, "get_parent_comments", ids=["message", "second"])
        self.assertEqual(sent.method, "POST")
        url = urlsplit(sent.full_url)
        self.assertEqual(url.path, "/comment-service/v3/workspaces/123/comments/bulk")
        self.assertEqual(parse_qs(url.query), {"fields": list(_THREAD_FIELDS)})
        self.assertEqual(json.loads(sent.data), {"ids": ["message", "second"]})
        self.assertEqual(result, response)

    def test_empty_parent_list_skips_network_and_bad_ids_are_rejected(self):
        with patch("clickup_inbox_cli.client._open_without_redirects") as opener:
            self.assertEqual(self.client().get_parent_comments([]), {"comments": [], "comment_threads": []})
            for ids in ("message", [""], [None], [3]):
                with self.subTest(ids=ids), self.assertRaises(ValueError):
                    self.client().get_parent_comments(ids)
            opener.assert_not_called()

    def test_listing_rejects_malformed_required_structures(self):
        responses = [{}, {"chat_threads": None}, {"chat_threads": [None]},
                     {"chat_threads": [{}]}, {"chat_threads": [thread(root_parent_type="8")]},
                     {"chat_threads": [thread(parent_comment_id="")]},
                     {"chat_threads": [thread(unread_thread_comment_ids=None)]},
                     {"chat_threads": [thread(read_thread_comment_ids=[3])]},
                     {"chat_threads": [thread(has_more_read="true")]},
                     {"chat_threads": [], "next_cursor": 1}, {"chat_threads": [], "prev_cursor": []}]
        for response in responses:
            with self.subTest(response=response), self.assertRaisesRegex(InboxAPIError, "Replies API"):
                self.request(response)

    def test_parent_preview_rejects_malformed_wrappers(self):
        responses = [{}, {"comments": [None]}, {"comments": [{}]},
                     {"comments": [{"object_id": "message", "status": "found", "data": None}]},
                     {"comments": [], "comment_threads": [None]},
                     {"comments": [], "comment_threads": {}}]
        for response in responses:
            with self.subTest(response=response), self.assertRaisesRegex(InboxAPIError, "Replies API"):
                self.request(response, "get_parent_comments", ids=["message"])

    def test_transport_errors_use_replies_label(self):
        error = HTTPError("https://example.invalid", 401, "private response", {}, io.BytesIO())
        try:
            with patch("clickup_inbox_cli.client._open_without_redirects", side_effect=error):
                with self.assertRaisesRegex(InboxAPIError, "Replies API returned HTTP 401"):
                    self.client().list_threads()
        finally:
            error.close()

    def test_chat_normalization_keeps_reply_ids_and_full_metadata(self):
        response = {"chat_threads": [thread(parent="message/1", root="channel/1")]}
        parents = {"comments": [{"object_id": "message/1", "status": "found", "data": {
            "id": "message/1", "text_content": "Discuss the launch", "userid": 27, "date": "123"}}],
            "comment_threads": [{"parent_comment_id": "message/1", "thread_comment_count": 12,
                                 "thread_latest_comment_date": "456", "thread_unread_comment_count": 4,
                                 "thread_last_read_date": "234"}]}
        self.assertEqual(flatten_threads(response, parents, "123", "unread"), [{
            "id": "message/1", "parent_comment_id": "message/1", "root_parent_id": "channel/1",
            "root_parent_type": 8, "channel_id": "channel/1", "message_id": "message/1", "task_id": "",
            "text": "Discuss the launch", "author_id": 27, "created_at": "123", "updated_at": "456",
            "read_status": "unread", "read_reply_ids": ["read-reply"], "unread_reply_ids": ["unread-reply"],
            "has_more_read": True, "has_more_unread": False, "reply_count": 12, "unread_count": 4,
            "last_read_at": "234", "parent_status": "found",
            "url": "https://app.clickup.com/123/chat/r/channel%2F1/t/message%2F1",
        }])

    def test_unavailable_parent_preserves_task_and_other_root_threads(self):
        response = {"chat_threads": [thread("task-comment", "task-1", 1), thread("other", "doc-1", 4)]}
        parents = {"comments": [{"object_id": "task-comment", "status": "not_found"}], "comment_threads": []}
        rows = flatten_threads(response, parents, "123", "read")
        self.assertEqual(rows[0]["parent_status"], "not_found")
        self.assertEqual(rows[0]["text"], "")
        self.assertEqual(rows[0]["task_id"], "task-1")
        self.assertEqual(rows[0]["channel_id"], "")
        self.assertEqual(rows[0]["message_id"], "")
        self.assertEqual(rows[0]["url"], "https://app.clickup.com/t/123/task-1")
        self.assertEqual(rows[0]["read_reply_ids"], ["read-reply"])
        self.assertEqual(rows[0]["read_status"], "read")
        self.assertIsNone(rows[0]["reply_count"])
        self.assertIsNone(rows[0]["unread_count"])
        self.assertEqual(rows[0]["last_read_at"], "")
        self.assertEqual(rows[1]["parent_status"], "missing")
        self.assertEqual(rows[1]["task_id"], "")
        self.assertEqual(rows[1]["url"], "")


if __name__ == "__main__":
    unittest.main()
