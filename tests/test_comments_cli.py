import base64
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from urllib.error import HTTPError
from unittest.mock import patch

from clickup_inbox_cli.cli import run
from clickup_inbox_cli.client import SessionCredentials


def credentials():
    payload = base64.urlsafe_b64encode(b'{"user":7}').decode().rstrip("=")
    return SessionCredentials("123", f"Bearer header.{payload}.signature", "csrf", "session")


def comment(comment_id, text):
    return {
        "id": comment_id, "text_content": text, "parent": "task-1",
        "type": 1, "root_parent_id": "task-1", "root_parent_type": 1,
        "assignee": 7, "assigned_by": 8, "userid": 8, "resolved": False,
    }


class CommentsCLITests(unittest.TestCase):
    def invoke(self, argv, responses):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("clickup_inbox_cli.cli.load_credentials", return_value=credentials()), patch(
            "clickup_inbox_cli.client._open_without_redirects",
            side_effect=[io.BytesIO(json.dumps(response).encode()) for response in responses],
        ) as opener, redirect_stdout(stdout), redirect_stderr(stderr):
            code = run(argv)
        return code, stdout.getvalue(), stderr.getvalue(), opener

    def test_resolve_and_reopen_send_only_resolved_state(self):
        for action, resolved in (("resolve", True), ("reopen", False)):
            with self.subTest(action=action):
                code, out, err, opener = self.invoke(
                    ["comments", action, "90130329498708"], [{}],
                )
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out, f"Comment {'resolved' if resolved else 'reopened'}.\n")
                self.assertEqual(opener.call_count, 1)
                sent = opener.call_args.args[0]
                self.assertEqual(json.loads(sent.data), {"resolved": resolved})
                self.assertEqual(sent.headers["X-workspace-id"], "123")

    def test_comment_state_json_output(self):
        for action, resolved in (("resolve", True), ("reopen", False)):
            with self.subTest(action=action):
                code, out, err, opener = self.invoke(
                    ["comments", action, "90130329498708", "--json"], [{}],
                )
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(json.loads(out), {
                    "id": "90130329498708", "resolved": resolved,
                })
                self.assertEqual(opener.call_count, 1)

    def test_failed_comment_update_reports_error_without_success(self):
        error = HTTPError("https://example.invalid", 403, "private response", {}, io.BytesIO())
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("clickup_inbox_cli.cli.load_credentials", return_value=credentials()), patch(
            "clickup_inbox_cli.client._open_without_redirects", side_effect=error,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = run(["comments", "resolve", "90130329498708"])
        error.close()
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("HTTP 403", stderr.getvalue())
        self.assertNotIn("private response", stderr.getvalue())

    def test_assigned_json_includes_comment_text_and_next_cursor(self):
        code, out, err, opener = self.invoke(
            ["comments", "assigned", "--json", "--limit", "2"],
            [{"comments": [comment("c1", "Please review")], "next_cursor": "next-page"}],
        )
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["comments"][0]["text"], "Please review")
        self.assertEqual(result["comments"][0]["id"], "c1")
        self.assertEqual(result["next_cursor"], "next-page")
        payload = json.loads(opener.call_args.args[0].data)
        self.assertEqual(payload["filters"]["assigned_to"], {"user_id": 7})
        self.assertEqual(payload["limit"], 2)

    def test_delegated_resolved_cursor_and_explicit_identity(self):
        code, out, err, opener = self.invoke(
            ["comments", "delegated", "--resolved", "--cursor", "page-2", "--user-id", "9", "--json"],
            [{"comments": []}],
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"comments": [], "next_cursor": None})
        payload = json.loads(opener.call_args.args[0].data)
        self.assertEqual(payload["filters"]["assigned_by"], 9)
        self.assertEqual(payload["filters"]["assigned_to"], {"anyone": True, "except_user_ids": [9, -2]})
        self.assertIs(payload["filters"]["resolved"], True)
        self.assertEqual(payload["cursor"], "page-2")

    def test_all_follows_response_cursor_and_collects_every_page(self):
        code, out, err, opener = self.invoke(
            ["comments", "delegated", "--all", "--json"],
            [
                {"comments": [comment("c1", "One")], "next_cursor": "page-2"},
                {"comments": [comment("c2", "Two")]},
            ],
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([row["id"] for row in json.loads(out)["comments"]], ["c1", "c2"])
        self.assertIsNone(json.loads(out)["next_cursor"])
        requests = [json.loads(call.args[0].data) for call in opener.call_args_list]
        self.assertNotIn("cursor", requests[0])
        self.assertEqual(requests[1]["cursor"], "page-2")

    def test_all_rejects_repeated_cursor_without_printing_partial_results(self):
        code, out, err, opener = self.invoke(
            ["comments", "assigned", "--all", "--json"],
            [{"comments": [], "next_cursor": "same"}] * 2,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("repeated pagination cursor", err)
        self.assertEqual(opener.call_count, 2)

    def test_table_neutralizes_terminal_controls(self):
        code, out, err, _ = self.invoke(
            ["comments", "assigned"],
            [{"comments": [comment("c1", "review\x1b]0;owned\x07\nplease")]}],
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("review", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)


if __name__ == "__main__":
    unittest.main()
