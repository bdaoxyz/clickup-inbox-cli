from __future__ import annotations

import base64
import json
import os
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from .client import DEFAULT_API_BASE, ConfigurationError, SessionCredentials


KEYRING_SERVICE = "clickup-inbox-cli"
KEYRING_ACCOUNT = "session"
SESSION_ENV_VARS = (
    "CLICKUP_WORKSPACE_ID",
    "CLICKUP_INBOX_AUTHORIZATION",
    "CLICKUP_INBOX_CSRF",
    "CLICKUP_INBOX_SESSION_ID",
)


class CredentialStore:
    """Shared interface for persistent ClickUp request credentials."""

    def prepare(self) -> None:
        """Prepare storage before an authentication flow starts."""

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize credential lifecycle operations when the backend requires it."""
        yield

    def load_session(self) -> tuple[SessionCredentials, Path | None]:
        values = self._load_values()
        try:
            credentials = SessionCredentials(
                workspace_id=values["workspace_id"],
                authorization=values["authorization"],
                csrf=values["csrf"],
                session_id=values["session_id"],
            )
        except (KeyError, TypeError) as exc:
            raise ConfigurationError(
                "Saved ClickUp Inbox session is invalid; run 'clickup-inbox auth login'"
            ) from exc
        profile_dir = values.get("profile_dir")
        profile = (
            Path(profile_dir)
            if isinstance(profile_dir, str) and profile_dir
            else None
        )
        return credentials, profile

    def load(self) -> SessionCredentials:
        return self.load_session()[0]

    def load_profile_dir(self) -> Path | None:
        return self.load_session()[1]

    def _load_values(self) -> dict[str, Any]:
        raise NotImplementedError


class SessionStore(CredentialStore):
    """Persist the short-lived request credential bundle in the OS keychain."""

    def __init__(self, backend: Any | None = None):
        if backend is None:
            try:
                import keyring
            except ImportError as exc:
                raise ConfigurationError(
                    "Keychain support is not installed; run: pip install -e '.[keychain]'"
                ) from exc
            backend = keyring
        self.backend = backend

    def save(
        self, credentials: SessionCredentials, *, profile_dir: Path | None = None
    ) -> None:
        payload = _serialize_session(credentials, profile_dir)
        self.backend.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, payload)

    def _load_values(self) -> dict[str, Any]:
        payload = self.backend.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        if not payload:
            raise ConfigurationError(
                "No saved ClickUp Inbox session; run 'clickup-inbox auth login'"
            )
        return _parse_session(payload)

    def delete(self) -> None:
        if self.backend.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) is not None:
            self.backend.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)

    @property
    def description(self) -> str:
        return "the OS Keychain"


class FileSessionStore(CredentialStore):
    """Persist request credentials in a private local file for headless hosts."""

    def __init__(self, path: Path):
        self.path = path.expanduser()

    def prepare(self) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent_mode = self.path.parent.stat().st_mode & 0o777
        except OSError as exc:
            raise ConfigurationError(
                f"Could not prepare the ClickUp Inbox session directory: {exc}"
            ) from exc
        if parent_mode & 0o077:
            raise ConfigurationError(
                f"Session directory permissions must be 0700 or stricter: {self.path.parent}"
            )

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.prepare()
        descriptor = -1
        try:
            import fcntl

            descriptor = os.open(
                self.path.with_suffix(f"{self.path.suffix}.lock"),
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except (ImportError, OSError) as exc:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise ConfigurationError(
                f"Could not lock the ClickUp Inbox session file: {exc}"
            ) from exc

        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            release_error: OSError | None = None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError as exc:
                release_error = exc
            try:
                os.close(descriptor)
            except OSError as exc:
                release_error = release_error or exc
            if release_error is not None and not body_failed:
                raise ConfigurationError(
                    f"Could not unlock the ClickUp Inbox session file: {release_error}"
                ) from release_error

    def save(
        self, credentials: SessionCredentials, *, profile_dir: Path | None = None
    ) -> None:
        self.prepare()
        descriptor = -1
        temporary_path: Path | None = None
        primary_error: OSError | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent, prefix=f".{self.path.name}.", text=True
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(_serialize_session(credentials, profile_dir))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            directory_descriptor = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            primary_error = exc
            raise ConfigurationError(
                f"Could not write the ClickUp Inbox session file: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    if primary_error is None:
                        raise ConfigurationError(
                            f"Could not close the ClickUp Inbox session file: {exc}"
                        ) from exc
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError as exc:
                    if primary_error is None:
                        raise ConfigurationError(
                            f"Could not clean up the ClickUp Inbox session file: {exc}"
                        ) from exc

    def _load_values(self) -> dict[str, Any]:
        try:
            metadata = self.path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ConfigurationError(
                    "Saved ClickUp Inbox session must be a regular file"
                )
            if metadata.st_mode & 0o077:
                raise ConfigurationError(
                    "Saved ClickUp Inbox session file permissions must be 0600 or stricter"
                )
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise ConfigurationError(
                    "Saved ClickUp Inbox session file must be owned by the current user"
                )
            return _parse_session(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(
                "No saved ClickUp Inbox session; run 'clickup-inbox auth login'"
            ) from exc
        except OSError as exc:
            raise ConfigurationError(
                f"Could not read the ClickUp Inbox session file: {exc}"
            ) from exc

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise ConfigurationError(
                f"Could not remove the ClickUp Inbox session file: {exc}"
            ) from exc

    @property
    def description(self) -> str:
        return f"the private session file {self.path}"


def default_session_file() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "clickup-inbox-cli" / "session.json"


def build_session_store(
    kind: str = "auto",
    *,
    session_file: Path | None = None,
    platform: str | None = None,
) -> CredentialStore:
    selected = kind.lower()
    if selected == "auto":
        selected = "keychain" if (platform or sys.platform) == "darwin" else "file"
    if selected == "keychain":
        return SessionStore()
    if selected == "file":
        return FileSessionStore(session_file or default_session_file())
    raise ConfigurationError("credential store must be auto, keychain, or file")


def _serialize_session(
    credentials: SessionCredentials, profile_dir: Path | None
) -> str:
    return json.dumps(
        {
            "workspace_id": credentials.workspace_id,
            "authorization": credentials.authorization,
            "csrf": credentials.csrf,
            "session_id": credentials.session_id,
            "profile_dir": str(profile_dir.resolve()) if profile_dir else None,
        },
        separators=(",", ":"),
    )


def _parse_session(payload: str) -> dict[str, Any]:
    try:
        values = json.loads(payload)
        if not isinstance(values, dict):
            raise TypeError
        return values
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "Saved ClickUp Inbox session is invalid; run 'clickup-inbox auth login'"
        ) from exc


def load_credentials(
    env: Mapping[str, str] | None = None,
    store: CredentialStore | None = None,
    *,
    store_kind: str = "auto",
    session_file: Path | None = None,
    now: float | None = None,
    refresh_before: float = 300.0,
    refresher: Any | None = None,
) -> SessionCredentials:
    values = os.environ if env is None else env
    if any(values.get(name) for name in SESSION_ENV_VARS):
        return SessionCredentials.from_env(values)
    session_store = store or build_session_store(
        store_kind, session_file=session_file
    )
    with session_store.locked():
        credentials, profile_dir = session_store.load_session()
        expiry = token_expiry(credentials.authorization)
        current_time = time.time() if now is None else now
        if expiry is not None and expiry <= current_time + refresh_before:
            refresh = refresher or capture_browser_session
            credentials = refresh(
                credentials.workspace_id,
                profile_dir=profile_dir,
                headless=True,
                timeout=60.0,
            )
            session_store.save(credentials, profile_dir=profile_dir)
        return credentials


def token_expiry(authorization: str) -> int | None:
    token = authorization.removeprefix("Bearer ").removeprefix("bearer ")
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        expiry = payload.get("exp")
        return int(expiry) if expiry is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def default_profile_dir() -> Path:
    if sys.platform != "darwin":
        return default_session_file().parent / "chromium-profile"
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "clickup-inbox-cli"
        / "chromium-profile"
    )


def capture_browser_session(
    workspace_id: str,
    *,
    profile_dir: Path | None = None,
    headless: bool,
    timeout: float = 300.0,
) -> SessionCredentials:
    """Use a persistent Chrome profile to obtain fresh request credentials.

    The browser retains its own session data. This helper captures only the
    short-lived headers needed by the Inbox endpoint and stores no cookies.
    """
    if not workspace_id.isdigit():
        raise ConfigurationError("workspace ID must contain only digits")
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ConfigurationError(
            "Browser support is not installed; run: pip install -e '.[browser]'"
        ) from exc

    resolved_profile = profile_dir or default_profile_dir()
    resolved_profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_profile.chmod(0o700)
    captured: list[SessionCredentials] = []
    inbox_url = f"https://app.clickup.com/{workspace_id}/inbox?tab=primary"
    api_origin = urlsplit(DEFAULT_API_BASE)
    request_path_prefix = f"/inbox/v3/workspaces/{workspace_id}/notifications/"

    def inspect_request(request: Any) -> None:
        candidate = urlsplit(request.url)
        if (
            candidate.scheme != api_origin.scheme
            or candidate.netloc != api_origin.netloc
            or not candidate.path.startswith(request_path_prefix)
        ):
            return
        credentials = _credentials_from_headers(workspace_id, request.all_headers())
        if credentials is not None:
            captured[:] = [credentials]

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(resolved_profile), channel="chrome", headless=headless
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.on("request", inspect_request)
                page.goto(inbox_url, wait_until="domcontentloaded", timeout=60_000)
                deadline = time.monotonic() + timeout
                while not captured:
                    if time.monotonic() >= deadline:
                        mode_hint = "auth login" if headless else "complete the ClickUp login"
                        raise ConfigurationError(
                            f"No authenticated Inbox request was observed; {mode_hint} and retry"
                        )
                    page.wait_for_timeout(200)
                return captured[0]
            finally:
                context.close()
    except PlaywrightError as exc:
        raise ConfigurationError(f"Could not run the ClickUp authentication browser: {exc}") from exc


def _credentials_from_headers(
    workspace_id: str, headers: Mapping[str, str]
) -> SessionCredentials | None:
    normalized = {name.lower(): value for name, value in headers.items()}
    authorization = normalized.get("authorization")
    csrf = normalized.get("x-csrf")
    session_id = normalized.get("sessionid")
    if not authorization or not csrf or not session_id:
        return None
    return SessionCredentials(workspace_id, authorization, csrf, session_id)
