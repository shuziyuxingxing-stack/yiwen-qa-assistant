import json
import os
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from app.adapters.yiwen import YiwenAdapter, YiwenSession


@dataclass
class SharedYiwenRuntimeStatus:
    configured: bool = False
    valid: bool | None = None
    expired: bool = False
    last_checked_at: float | None = None
    last_success_at: float | None = None
    last_error_at: float | None = None
    last_error: str | None = None
    token_len: int = 0
    cookie_count: int = 0
    default_agent_id: str = "619ae0c8ffb246d9b669017763359b81"
    runtime_chat_id: str | None = None
    session_source: str = "env"
    runtime_updated_at: float | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "valid": self.valid,
            "expired": self.expired,
            "last_checked_at": self.last_checked_at,
            "last_success_at": self.last_success_at,
            "last_error_at": self.last_error_at,
            "last_error": self.last_error,
            "token_len": self.token_len,
            "cookie_count": self.cookie_count,
            "default_agent_id": self.default_agent_id,
            "runtime_chat_id": self.runtime_chat_id,
            "session_source": self.session_source,
            "runtime_updated_at": self.runtime_updated_at,
        }


class SharedYiwenSessionManager:
    def __init__(self) -> None:
        self.status = SharedYiwenRuntimeStatus()
        self._runtime_session: YiwenSession | None = None
        self._load_runtime_session()

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parents[2]

    @classmethod
    def _env_path(cls) -> Path:
        return cls._project_root() / ".env"

    @classmethod
    def _state_dir(cls) -> Path:
        path = cls._project_root() / ".state"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _runtime_session_path(cls) -> Path:
        return cls._state_dir() / "shared_yiwen_session.json"

    @classmethod
    def _chat_auth_path(cls) -> Path:
        return cls._state_dir() / "chat-auth.json"

    @classmethod
    def _chat_oauth_session_path(cls) -> Path:
        return cls._state_dir() / "chat-oauth-session.json"

    @staticmethod
    def _normalize_bearer(value: str) -> str:
        value = value.strip()
        if value.lower().startswith("bearer "):
            value = value.split(None, 1)[1].strip()
        if value.startswith("{"):
            try:
                payload = json.loads(value)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                token = payload.get("token") or payload.get("accessToken") or payload.get("bearer_token")
                if isinstance(token, str) and token.strip():
                    return token.strip()
        return value

    @staticmethod
    def _normalize_chat_id(value: str | None) -> str | None:
        if not value:
            return None
        value = value.strip()
        if not value or value.lower() == "empty":
            return None
        return value

    @staticmethod
    def _normalize_cookie_header(value: str) -> str:
        value = value.strip()
        if value.lower().startswith("cookie:"):
            return value.split(":", 1)[1].strip()
        return value

    @staticmethod
    def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
        cookie_obj = SimpleCookie()
        cookie_obj.load(cookie_header)
        return {key: morsel.value for key, morsel in cookie_obj.items()}

    def _load_file_env(self) -> dict[str, str]:
        env_path = self._env_path()
        result: dict[str, str] = {}
        if not env_path.exists():
            return result
        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
        return result

    def _get(self, key: str, default: str = "") -> str:
        file_env = self._load_file_env()
        return os.getenv(key, file_env.get(key, default))

    def _save_runtime_session(self) -> None:
        if not self._runtime_session:
            return
        payload = {
            "bearer_token": self._runtime_session.bearer_token,
            "cookies": self._runtime_session.cookies,
            "username": self._runtime_session.username,
            "default_agent_id": self.status.default_agent_id,
            "runtime_chat_id": self.status.runtime_chat_id,
            "runtime_updated_at": self.status.runtime_updated_at,
        }
        self._runtime_session_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_runtime_session(self) -> None:
        path = self._runtime_session_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        token = str(payload.get("bearer_token") or "").strip()
        if not token:
            return
        cookies = payload.get("cookies") if isinstance(payload.get("cookies"), dict) else {}
        self._runtime_session = YiwenSession(
            bearer_token=self._normalize_bearer(token),
            cookies={str(k): str(v) for k, v in cookies.items()},
            username=payload.get("username"),
        )
        self.status.configured = True
        self.status.valid = None
        self.status.expired = False
        self.status.token_len = len(self._runtime_session.bearer_token)
        self.status.cookie_count = len(self._runtime_session.cookies)
        self.status.default_agent_id = str(payload.get("default_agent_id") or self.status.default_agent_id)
        self.status.runtime_chat_id = self._normalize_chat_id(payload.get("runtime_chat_id"))
        self.status.session_source = "runtime_file"
        self.status.runtime_updated_at = payload.get("runtime_updated_at")

    def save_chat_oauth_session(self, *, state: str | None, cookies: dict[str, str], authorize_url: str) -> None:
        payload = {
            "state": state,
            "cookies": cookies,
            "authorize_url": authorize_url,
            "updated_at": time.time(),
        }
        self._chat_oauth_session_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_chat_oauth_cookies(self, state: str | None = None) -> dict[str, str]:
        path = self._chat_oauth_session_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        if state and payload.get("state") and payload.get("state") != state:
            return {}
        cookies = payload.get("cookies")
        if not isinstance(cookies, dict):
            return {}
        return {str(k): str(v) for k, v in cookies.items()}

    def save_chat_auth_state(self, *, token: str, username: str | None = None, real_name: str | None = None) -> None:
        payload = {
            "token": self._normalize_bearer(token),
            "username": username or "unknown",
            "realName": real_name or username or "unknown",
            "obtainedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "jwtExpiresAt": None,
        }
        self._chat_auth_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    def get_default_agent_id(self) -> str:
        agent_id = self.status.default_agent_id or self._get("YIWEN_SHARED_DEFAULT_AGENT_ID", "619ae0c8ffb246d9b669017763359b81").strip()
        return agent_id or "619ae0c8ffb246d9b669017763359b81"

    def get_default_chat_id(self) -> str | None:
        self.status.runtime_chat_id = self._normalize_chat_id(self.status.runtime_chat_id)
        if self.status.runtime_chat_id:
            return self.status.runtime_chat_id
        return self._normalize_chat_id(self._get("YIWEN_SHARED_DEFAULT_CHAT_ID", ""))

    def set_runtime_chat_id(self, chat_id: str | None) -> None:
        normalized_chat_id = self._normalize_chat_id(chat_id)
        if normalized_chat_id:
            self.status.runtime_chat_id = normalized_chat_id
            self._save_runtime_session()

    def set_runtime_session(
        self,
        *,
        bearer_token: str,
        cookies: dict[str, str] | None = None,
        cookie_header: str | None = None,
        username: str | None = None,
        chat_id: str | None = None,
        agent_id: str | None = None,
    ) -> YiwenSession:
        parsed_cookies = dict(cookies or {})
        if cookie_header:
            parsed_cookies.update(self._parse_cookie_header(self._normalize_cookie_header(cookie_header)))
        session = YiwenSession(
            bearer_token=self._normalize_bearer(bearer_token),
            cookies=parsed_cookies,
            username=username,
        )
        self._runtime_session = session
        now = time.time()
        self.status.configured = True
        self.status.valid = True
        self.status.expired = False
        self.status.last_checked_at = now
        self.status.last_success_at = now
        self.status.last_error = None
        self.status.token_len = len(session.bearer_token)
        self.status.cookie_count = len(session.cookies)
        self.status.session_source = "runtime_bridge"
        self.status.runtime_updated_at = now
        normalized_chat_id = self._normalize_chat_id(chat_id)
        if normalized_chat_id:
            self.status.runtime_chat_id = normalized_chat_id
        if agent_id:
            self.status.default_agent_id = agent_id
        self._save_runtime_session()
        return session

    def get_session(self) -> YiwenSession | None:
        if self._runtime_session:
            self.status.configured = True
            self.status.token_len = len(self._runtime_session.bearer_token)
            self.status.cookie_count = len(self._runtime_session.cookies)
            if self.status.session_source == "env":
                self.status.session_source = "runtime_file"
            return self._runtime_session

        self.status.session_source = "env"
        bearer_token = self._normalize_bearer(self._get("YIWEN_SHARED_BEARER_TOKEN", ""))
        cookie_header = self._normalize_cookie_header(self._get("YIWEN_SHARED_COOKIE", ""))
        cookies: dict[str, str] = {}
        if cookie_header:
            try:
                cookies = self._parse_cookie_header(cookie_header)
            except Exception:
                cookies = {}

        self.status.configured = bool(bearer_token)
        self.status.token_len = len(bearer_token)
        self.status.cookie_count = len(cookies)
        self.status.default_agent_id = self.get_default_agent_id()

        if not bearer_token:
            return None
        return YiwenSession(
            bearer_token=bearer_token,
            cookies=cookies,
            username=self._get("YIWEN_SHARED_USERNAME", "") or None,
        )

    def mark_success(self, chat_id: str | None = None) -> None:
        now = time.time()
        self.status.valid = True
        self.status.expired = False
        self.status.last_success_at = now
        self.status.last_checked_at = now
        self.status.last_error = None
        normalized_chat_id = self._normalize_chat_id(chat_id)
        if normalized_chat_id:
            self.status.runtime_chat_id = normalized_chat_id
        self._save_runtime_session()

    def mark_failure(self, error: Exception | str) -> None:
        now = time.time()
        message = str(error)
        self.status.valid = False
        self.status.expired = True
        self.status.last_error_at = now
        self.status.last_checked_at = now
        self.status.last_error = message

    async def check(self, adapter: YiwenAdapter) -> dict[str, Any]:
        session = self.get_session()
        if not session:
            self.mark_failure("shared Yiwen account is not configured")
            self.status.configured = False
            return self.status.to_payload()
        try:
            created = await adapter.create_chat(session, self.get_default_agent_id())
            self.mark_success(created.chat_id)
        except Exception as exc:
            self.mark_failure(exc)
        return self.status.to_payload()

    def to_payload(self) -> dict[str, Any]:
        self.get_session()
        return self.status.to_payload()


shared_yiwen_manager = SharedYiwenSessionManager()


def load_shared_yiwen_session() -> YiwenSession | None:
    return shared_yiwen_manager.get_session()


def get_shared_default_agent_id() -> str:
    return shared_yiwen_manager.get_default_agent_id()


def get_shared_default_chat_id() -> str | None:
    return shared_yiwen_manager.get_default_chat_id()