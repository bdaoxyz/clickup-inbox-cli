import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from clickup_inbox_cli.cli import run
from clickup_inbox_cli.client import SessionCredentials


def thread(parent_id):
    return {
        "parent_comment_id": parent_id,
        "root_parent_id": "channel-1",
        "root_parent_type": 8,
        "read_thread_comment_ids": ["reply-1"],
        "unread_thread_comment_ids": [],
        "has_more_read": False,
        "has_more_unread": False,
    }


def preview(parent_id, text="Please review"):
    return {
        "comments": [{"object_id": parent_id, "status": "found", "data": {
            "id": parent_id, "text_content": text, "userid": 7, "date": "123",
        }}],
        "comment_threads": [{"parent_comment_id": parent_id, "thread_comment_count": 1,
                             "thread_unread_comment_count": 0}],
    }


class RepliesCLITests(unittest.TestCase):
    def invoke(self, argv, responses):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("clickup_inbox_cli.cli.load_credentials", return_value=
                   SessionCredentials("123", "Bearer token", "csrf", "session")), patch(
            "clickup_inbox_cli.client._open_without_redirects",
            side_effect=[io.BytesIO(json.dumps(response).encode()) for response in responses],
        ) as opener, redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = run(argv)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue(), opener

    def test_read_json_has_parent_preview_and_mcp_ids(self):
        code, out, err, opener = self.invoke(
            ["replies", "read", "--limit", "2", "--cursor", "a+/=", "--json"],
            [{"chat_threads": [thread("parent-1")], "next_cursor": "next"}, preview("parent-1")],
        )
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        row = result["threads"][0]
        self.assertEqual((row["id"], row["message_id"], row["channel_id"]),
                         ("parent-1", "parent-1", "channel-1"))
        self.assertEqual(row["text"], "Please review")
        self.assertEqual(row["read_status"], "read")
        self.assertEqual(result["next_cursor"], "next")
        request = opener.call_args_list[0].args[0]
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)
        self.assertEqual(parse_qs(urlsplit(request.full_url).query),
                         {"read_status": ["read"], "limit": ["2"], "cursor": ["a+/="]})

    def test_empty_unread_view_skips_preview_request(self):
        code, out, err, opener = self.invoke(
            ["replies", "unread", "--json"], [{"chat_threads": []}],
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"threads": [], "next_cursor": None})
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(parse_qs(urlsplit(opener.call_args.args[0].full_url).query)
                         ["read_status"], ["unread"])

    def test_all_uses_response_cursor_and_bulk_fetches_each_page(self):
        code, out, err, opener = self.invoke(
            ["replies", "read", "--all", "--json"],
            [{"chat_threads": [thread("p1")], "next_cursor": "page-2"}, preview("p1"),
             {"chat_threads": [thread("p2")]}, preview("p2")],
        )
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual([row["id"] for row in result["threads"]], ["p1", "p2"])
        self.assertIsNone(result["next_cursor"])
        requests = [call.args[0] for call in opener.call_args_list]
        self.assertNotIn("cursor", parse_qs(urlsplit(requests[0].full_url).query))
        self.assertEqual(parse_qs(urlsplit(requests[2].full_url).query)["cursor"], ["page-2"])
        self.assertEqual(json.loads(requests[3].data), {"ids": ["p2"]})

    def test_repeated_cursor_stops_without_partial_stdout(self):
        code, out, err, opener = self.invoke(
            ["replies", "read", "--all", "--json"],
            [{"chat_threads": [], "next_cursor": "same"}] * 2,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("repeated pagination cursor", err)
        self.assertEqual(opener.call_count, 2)

    def test_later_page_failure_does_not_emit_partial_results(self):
        code, out, err, _ = self.invoke(
            ["replies", "read", "--all", "--json"],
            [{"chat_threads": [thread("p1")], "next_cursor": "next"}, preview("p1"),
             {"chat_threads": None}],
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Replies API", err)

    def test_table_sanitizes_terminal_controls_and_reports_more_pages(self):
        code, out, err, _ = self.invoke(
            ["replies", "read"],
            [{"chat_threads": [thread("p1")], "next_cursor": "next"},
             preview("p1", "review\x1b]0;owned\x07\nplease")],
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("review", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("--all", out)

    def test_table_keeps_unavailable_parent_with_unknown_counts(self):
        code, out, err, _ = self.invoke(
            ["replies", "read"],
            [{"chat_threads": [thread("p1")]},
             {"comments": [{"object_id": "p1", "status": "not_found"}]}],
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[1].split()[:4], ["read", "-", "-", "channel-1"])
        self.assertIn("parent unavailable: not_found", out)


if __name__ == "__main__":
    unittest.main()
