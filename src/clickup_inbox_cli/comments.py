from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from .client import ConfigurationError, InboxAPIError, InboxClient


_THREAD_FIELDS = (
    "thread_comment_count",
    "thread_latest_comment_date",
    "thread_has_unread_comments",
    "thread_has_unread_mentions",
    "thread_user_ids",
    "thread_unread_comment_count",
    "thread_last_read_date",
    "thread_follower_ids",
    "thread_group_follower_member_ids",
    "thread_unfollower_ids",
    "thread_ai_models",
)


class AssignedCommentsClient(InboxClient):
    api_name = "Assigned Comments API"

    def list_comments(
        self,
        *,
        view: str = "assigned",
        limit: int = 100,
        cursor: str = "",
        resolved: bool = False,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """List assigned or delegated comments across all assignment dates."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if view not in ("assigned", "delegated"):
            raise ValueError("view must be assigned or delegated")
        if not isinstance(cursor, str):
            raise ValueError("cursor must be a string")
        if type(resolved) is not bool:
            raise ValueError("resolved must be a boolean")
        if user_id is not None and not _positive_user_id(user_id):
            raise ValueError("user ID must be a positive integer")
        if user_id is None:
            user_id = _session_user_id(self.credentials.authorization)

        filters: dict[str, Any] = {
            "parent": {"include_replies": True},
            "resolved": resolved,
            "assigned_to": {"user_id": user_id},
        }
        if view == "delegated":
            filters["assigned_to"] = {"anyone": True, "except_user_ids": [user_id, -2]}
            filters["assigned_by"] = user_id
        payload: dict[str, Any] = {"filters": filters, "limit": limit}
        if cursor:
            payload["cursor"] = cursor
        query = urlencode([("fields", field) for field in _THREAD_FIELDS])
        path = (
            f"/comment-service/v3/workspaces/{self.credentials.workspace_id}"
            f"/comments/search?{query}"
        )
        response = self._request("POST", path, payload)
        _validate_listing_response(response)
        return response


def _positive_user_id(value: object) -> bool:
    return type(value) is int and value > 0


def _session_user_id(authorization: str) -> int:
    """Read only the filtering identity; the server still authenticates the token."""
    try:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            token = authorization
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        payload_bytes = base64.b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True
        )
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict) or not _positive_user_id(payload.get("user")):
            raise ValueError
        return payload["user"]
    except (ValueError, UnicodeError, binascii.Error):
        raise ConfigurationError(
            "Could not infer your ClickUp user ID from the session; provide --user-id."
        ) from None


def _validate_listing_response(response: Mapping[str, Any]) -> None:
    comments = response.get("comments")
    if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
        raise InboxAPIError("ClickUp Assigned Comments API returned invalid comments")
    for field in ("next_cursor", "prev_cursor"):
        if response.get(field) is not None and not isinstance(response[field], str):
            raise InboxAPIError("ClickUp Assigned Comments API returned invalid pagination metadata")
    if "comment_threads" in response:
        threads = response["comment_threads"]
        if not isinstance(threads, list) or any(not isinstance(item, dict) for item in threads):
            raise InboxAPIError("ClickUp Assigned Comments API returned invalid comment threads")


def flatten_comments(response: Mapping[str, Any], workspace_id: str) -> list[dict[str, Any]]:
    _validate_listing_response(response)
    reply_counts = {
        thread.get("parent_comment_id"): thread.get("thread_comment_count", 0)
        for thread in response.get("comment_threads", [])
        if isinstance(thread.get("parent_comment_id"), str)
    }
    rows: list[dict[str, Any]] = []
    for comment in response["comments"]:
        root_parent_id = comment.get("root_parent_id", "")
        root_parent_type = comment.get("root_parent_type")
        task_id = root_parent_id if root_parent_type == 1 else ""
        rows.append({
            "id": comment.get("id", ""),
            "text": comment.get("text_content", ""),
            "parent_id": comment.get("parent", ""),
            "parent_type": comment.get("type"),
            "root_parent_id": root_parent_id,
            "root_parent_type": root_parent_type,
            "task_id": task_id,
            "assignee_id": comment.get("assignee"),
            "group_assignee_id": comment.get("group_assignee"),
            "assigned_by_id": comment.get("assigned_by"),
            "author_id": comment.get("userid"),
            "resolved": comment.get("resolved", False),
            "created_at": comment.get("date", ""),
            "assigned_at": comment.get("date_assigned", ""),
            "resolved_at": comment.get("date_resolved", ""),
            "reply_count": reply_counts.get(comment.get("id"), 0),
            "url": (
                f"https://app.clickup.com/t/{quote(str(workspace_id), safe='')}"
                f"/{quote(str(task_id), safe='')}"
                if task_id else ""
            ),
        })
    return rows
