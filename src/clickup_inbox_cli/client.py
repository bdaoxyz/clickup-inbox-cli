from __future__ import annotations

import json
import os
from http.client import HTTPException
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__


DEFAULT_API_BASE = "https://frontdoor-prod-us-east-2-1.clickup.com"


class ConfigurationError(ValueError):
    """Raised when required session configuration is absent."""


class InboxAPIError(RuntimeError):
    """Raised when ClickUp rejects or fails an Inbox request."""


@dataclass(frozen=True)
class SessionCredentials:
    workspace_id: str
    authorization: str
    csrf: str
    session_id: str
    api_base: str = DEFAULT_API_BASE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SessionCredentials":
        values = os.environ if env is None else env
        required = {
            "workspace_id": "CLICKUP_WORKSPACE_ID",
            "authorization": "CLICKUP_INBOX_AUTHORIZATION",
            "csrf": "CLICKUP_INBOX_CSRF",
            "session_id": "CLICKUP_INBOX_SESSION_ID",
        }
        missing = [name for name in required.values() if not values.get(name)]
        if missing:
            raise ConfigurationError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        authorization = values[required["authorization"]]
        if not authorization.lower().startswith("bearer "):
            authorization = f"Bearer {authorization}"

        return cls(
            workspace_id=values[required["workspace_id"]],
            authorization=authorization,
            csrf=values[required["csrf"]],
            session_id=values[required["session_id"]],
            api_base=DEFAULT_API_BASE,
        )


class InboxClient:
    def __init__(self, credentials: SessionCredentials, timeout: float = 20.0):
        self.credentials = credentials
        self.timeout = timeout

    def list_bundles(
        self,
        *,
        limit: int = 20,
        cursor: str = "",
        unread_only: bool = False,
        folder: str = "primary",
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        statuses = {"primary": "uncleared", "later": "snoozed", "cleared": "cleared"}
        if folder not in statuses:
            raise ValueError("folder must be primary, later, or cleared")

        filtered_by = {
            "status": statuses[folder],
            "assignedToMe": False,
            "mentioned": False,
            "unread": unread_only,
            "reminders": False,
            "saved": False,
        }
        if folder == "primary":
            filtered_by = {"bundleType": "messages", **filtered_by}

        payload = {
            "filteredBy": filtered_by,
            "pagination": {"nextCursor": cursor, "limit": limit},
            "sortedBy": {"direction": "descending"},
            "needsMemberMap": False,
        }
        path = (
            f"/inbox/v3/workspaces/{self.credentials.workspace_id}"
            "/notifications/bundles/search"
        )
        response = self._request("POST", path, payload)
        _validate_listing_response(response)
        return response

    def mark_read(self, bundle_snapshot_id: str) -> None:
        self._put_snapshot("read", bundle_snapshot_id)

    def mark_unread(self, bundle_snapshot_id: str) -> None:
        self._put_snapshot("unread", bundle_snapshot_id)

    def snooze(self, bundle_snapshot_id: str, snoozed_until: str) -> None:
        _validate_snapshot_id(bundle_snapshot_id)
        if not snoozed_until:
            raise ValueError("snoozed until timestamp is required")
        self._request(
            "PUT",
            self._bundle_path("snooze"),
            {"bundleSnapshotId": bundle_snapshot_id, "snoozedUntil": snoozed_until},
        )

    def unsnooze(self, bundle_snapshot_id: str) -> None:
        self._put_snapshot("unsnooze", bundle_snapshot_id)

    def clear(self, bundle_snapshot_id: str) -> None:
        self._put_path_action("clear", bundle_snapshot_id)

    def unclear(self, bundle_snapshot_id: str) -> None:
        self._put_path_action("unclear", bundle_snapshot_id)

    def _put_snapshot(self, action: str, bundle_snapshot_id: str) -> None:
        _validate_snapshot_id(bundle_snapshot_id)
        self._request(
            "PUT",
            self._bundle_path(action),
            {"bundleSnapshotId": bundle_snapshot_id},
        )

    def _put_path_action(self, action: str, bundle_snapshot_id: str) -> None:
        _validate_snapshot_id(bundle_snapshot_id)
        encoded_id = quote(bundle_snapshot_id, safe="")
        self._request("PUT", f"{self._bundle_path(encoded_id)}/{action}", {})

    def _bundle_path(self, suffix: str) -> str:
        return (
            f"/inbox/v3/workspaces/{self.credentials.workspace_id}"
            f"/notifications/bundles/{suffix}"
        )

    def _request(
        self, method: str, path: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.credentials.api_base}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": self.credentials.authorization,
                "Content-Type": "application/json",
                "Referer": "https://app.clickup.com/",
                "X-CSRF": self.credentials.csrf,
                "X-Workspace-ID": self.credentials.workspace_id,
                "sessionId": self.credentials.session_id,
                "User-Agent": f"clickup-inbox-cli/{__version__}",
            },
        )
        try:
            with _open_without_redirects(request, timeout=self.timeout) as response:
                raw_body = response.read()
                result = json.loads(raw_body) if raw_body.strip() else {}
        except HTTPError as exc:
            raise InboxAPIError(f"ClickUp Inbox API returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise InboxAPIError(f"Could not reach ClickUp Inbox API: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise InboxAPIError("ClickUp Inbox API returned invalid JSON") from exc
        except (HTTPException, OSError, UnicodeError) as exc:
            raise InboxAPIError("ClickUp Inbox API response could not be read") from exc

        if not isinstance(result, dict):
            raise InboxAPIError("ClickUp Inbox API returned an unexpected response")
        return result


def _validate_snapshot_id(bundle_snapshot_id: str) -> None:
    if not bundle_snapshot_id or len(bundle_snapshot_id) > 8192:
        raise ValueError("a valid bundle snapshot ID is required")


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _open_without_redirects(request: Request, timeout: float):
    """Do not forward session credentials through an HTTP redirect."""
    return build_opener(_NoRedirects()).open(request, timeout=timeout)


def _validate_listing_response(response: Mapping[str, Any]) -> None:
    resources = response.get("resources")
    groups = response.get("notificationBundleGroups")
    if not isinstance(resources, list) or not isinstance(groups, list):
        raise InboxAPIError("ClickUp Inbox API returned an unexpected response")
    if "pagination" in response and not isinstance(response["pagination"], dict):
        raise InboxAPIError("ClickUp Inbox API returned invalid pagination metadata")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(
            group.get("notificationBundles"), list
        ):
            raise InboxAPIError("ClickUp Inbox API returned invalid bundle groups")


def flatten_bundles(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    resources = {
        resource.get("entityResourceName"): resource
        for resource in response.get("resources", [])
        if isinstance(resource, dict) and resource.get("entityResourceName")
    }
    rows: list[dict[str, Any]] = []
    for group in response.get("notificationBundleGroups", []):
        if not isinstance(group, dict):
            continue
        for bundle in group.get("notificationBundles", []):
            if not isinstance(bundle, dict):
                continue
            resource = resources.get(bundle.get("rootEntityResourceName"), {})
            root_entity = bundle.get("rootEntityResourceName", "")
            rows.append(
                {
                    "id": bundle.get("id", ""),
                    "task_id": _task_id_from_entity_resource_name(root_entity),
                    "title": resource.get("name", root_entity),
                    "unread": bundle.get("unreadCount", 0),
                    "status": bundle.get("status", ""),
                    "updated_at": bundle.get("mostRecentNotificationTime", ""),
                    "has_assignment": bool(bundle.get("hasAssignment")),
                    "has_mention": bool(bundle.get("hasMention")),
                }
            )
    return rows


def _task_id_from_entity_resource_name(value: object) -> str:
    parts = str(value).split(":")
    if len(parts) == 4 and parts[:2] == ["clickup", "task"]:
        return parts[3]
    return ""
