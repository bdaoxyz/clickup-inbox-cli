from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote, urlencode

from .client import InboxAPIError, InboxClient
from .comments import _THREAD_FIELDS


class RepliesClient(InboxClient):
    api_name = "Replies API"

    def list_threads(
        self,
        *,
        read_status: str = "unread",
        limit: int = 15,
        cursor: str = "",
    ) -> dict[str, Any]:
        """List the chat threads shown in the Home Replies shortcut."""
        _validate_read_status(read_status)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not isinstance(cursor, str):
            raise ValueError("cursor must be a string")
        parameters = {"read_status": read_status, "limit": limit}
        if cursor:
            parameters["cursor"] = cursor
        path = (
            f"/chat/v1/workspaces/{self.credentials.workspace_id}"
            f"/chat/threads?{urlencode(parameters)}"
        )
        response = self._request("GET", path)
        _validate_threads_response(response)
        return response

    def get_parent_comments(self, ids: list[str]) -> dict[str, Any]:
        """Fetch parent message previews and complete thread-count metadata."""
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids):
            raise ValueError("parent comment IDs must be a list of nonempty strings")
        if not ids:
            return {"comments": [], "comment_threads": []}
        query = urlencode([("fields", field) for field in _THREAD_FIELDS])
        path = (
            f"/comment-service/v3/workspaces/{self.credentials.workspace_id}"
            f"/comments/bulk?{query}"
        )
        response = self._request("POST", path, {"ids": ids})
        _validate_parent_response(response)
        return response


def _validate_read_status(read_status: str) -> None:
    if read_status not in ("unread", "read"):
        raise ValueError("read status must be unread or read")


def _validate_threads_response(response: Mapping[str, Any]) -> None:
    threads = response.get("chat_threads")
    if not isinstance(threads, list):
        raise InboxAPIError("ClickUp Replies API returned invalid threads")
    for thread in threads:
        if not isinstance(thread, dict) or any(
            not isinstance(thread.get(field), str) or not thread[field]
            for field in ("parent_comment_id", "root_parent_id")
        ) or type(thread.get("root_parent_type")) is not int:
            raise InboxAPIError("ClickUp Replies API returned invalid threads")
        for field in ("read_thread_comment_ids", "unread_thread_comment_ids"):
            ids = thread.get(field)
            if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids):
                raise InboxAPIError("ClickUp Replies API returned invalid reply IDs")
        for field in ("has_more_read", "has_more_unread"):
            if type(thread.get(field)) is not bool:
                raise InboxAPIError("ClickUp Replies API returned invalid thread pagination metadata")
    for field in ("next_cursor", "prev_cursor"):
        if response.get(field) is not None and not isinstance(response[field], str):
            raise InboxAPIError("ClickUp Replies API returned invalid pagination metadata")


def _validate_parent_response(response: Mapping[str, Any]) -> None:
    comments = response.get("comments")
    if not isinstance(comments, list):
        raise InboxAPIError("ClickUp Replies API returned invalid parent comments")
    for comment in comments:
        if not isinstance(comment, dict) or not isinstance(comment.get("object_id"), str) or not comment["object_id"]:
            raise InboxAPIError("ClickUp Replies API returned invalid parent comments")
        if not isinstance(comment.get("status"), str) or not comment["status"]:
            raise InboxAPIError("ClickUp Replies API returned invalid parent comment status")
        if comment["status"] == "found" and not isinstance(comment.get("data"), dict):
            raise InboxAPIError("ClickUp Replies API returned invalid parent comment data")
    if "comment_threads" in response:
        threads = response["comment_threads"]
        if not isinstance(threads, list) or any(not isinstance(item, dict) for item in threads):
            raise InboxAPIError("ClickUp Replies API returned invalid comment threads")


def flatten_threads(
    response: Mapping[str, Any],
    parent_response: Mapping[str, Any],
    workspace_id: str,
    read_status: str,
) -> list[dict[str, Any]]:
    _validate_read_status(read_status)
    _validate_threads_response(response)
    _validate_parent_response(parent_response)
    parents = {item["object_id"]: item for item in parent_response["comments"]}
    metadata = {
        item["parent_comment_id"]: item
        for item in parent_response.get("comment_threads", [])
        if isinstance(item.get("parent_comment_id"), str)
    }
    rows = []
    for thread in response["chat_threads"]:
        parent_id = thread["parent_comment_id"]
        root_id = thread["root_parent_id"]
        root_type = thread["root_parent_type"]
        parent = parents.get(parent_id, {})
        preview = parent.get("data", {}) if parent.get("status") == "found" else {}
        counts = metadata.get(parent_id, {})
        channel_id = root_id if root_type == 8 else ""
        task_id = root_id if root_type == 1 else ""
        url = ""
        if channel_id:
            url = (
                f"https://app.clickup.com/{quote(str(workspace_id), safe='')}/chat/r/"
                f"{quote(channel_id, safe='')}/t/{quote(parent_id, safe='')}"
            )
        elif task_id:
            url = (
                f"https://app.clickup.com/t/{quote(str(workspace_id), safe='')}/"
                f"{quote(task_id, safe='')}"
            )
        rows.append({
            "id": parent_id,
            "parent_comment_id": parent_id,
            "root_parent_id": root_id,
            "root_parent_type": root_type,
            "channel_id": channel_id,
            "message_id": parent_id if channel_id else "",
            "task_id": task_id,
            "text": preview.get("text_content", ""),
            "author_id": preview.get("userid"),
            "created_at": preview.get("date", ""),
            "updated_at": counts.get("thread_latest_comment_date", ""),
            "read_status": read_status,
            "read_reply_ids": thread["read_thread_comment_ids"],
            "unread_reply_ids": thread["unread_thread_comment_ids"],
            "has_more_read": thread["has_more_read"],
            "has_more_unread": thread["has_more_unread"],
            "reply_count": counts.get("thread_comment_count"),
            "unread_count": counts.get("thread_unread_comment_count"),
            "last_read_at": counts.get("thread_last_read_date", ""),
            "parent_status": parent.get("status", "missing"),
            "url": url,
        })
    return rows
