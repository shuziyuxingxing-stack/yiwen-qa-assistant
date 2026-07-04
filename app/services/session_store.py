import json
import secrets
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from threading import RLock
from typing import Any

from app.adapters.yiwen import YiwenSession


BRIDGE_ONLINE_WINDOW_SECONDS = 12.0


@dataclass
class UserAccount:
    user_id: str
    access_token: str
    display_name: str | None = None
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)

    def status_payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "access_token": self.access_token,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "UserAccount":
        return cls(
            user_id=str(payload["user_id"]),
            access_token=str(payload["access_token"]),
            display_name=payload.get("display_name"),
            created_at=float(payload.get("created_at") or time.time()),
            last_seen_at=float(payload.get("last_seen_at") or time.time()),
        )


@dataclass
class StoredYiwenSession:
    user_id: str
    bearer_token: str
    cookies: dict[str, str] = field(default_factory=dict)
    username: str | None = None
    default_chat_id: str | None = None
    default_agent_id: str | None = None
    bridge_enabled: bool = False
    bridge_connected_at: float | None = None
    bridge_last_seen_at: float | None = None
    bridge_real_name: str | None = None
    bridge_current_url: str | None = None
    bridge_last_error: str | None = None

    def to_adapter_session(self) -> YiwenSession:
        return YiwenSession(
            bearer_token=self.bearer_token,
            cookies=dict(self.cookies),
            username=self.username,
        )

    def is_bridge_online(self) -> bool:
        if not self.bridge_enabled or not self.bridge_last_seen_at:
            return False
        return (time.time() - self.bridge_last_seen_at) <= BRIDGE_ONLINE_WINDOW_SECONDS

    def status_payload(self) -> dict[str, Any]:
        bridge_age_seconds = None
        if self.bridge_last_seen_at is not None:
            bridge_age_seconds = max(0.0, time.time() - self.bridge_last_seen_at)

        return {
            "user_id": self.user_id,
            "username": self.username,
            "has_session": True,
            "has_bearer_token": bool(self.bearer_token),
            "cookie_keys": sorted(self.cookies.keys()),
            "default_chat_id": self.default_chat_id,
            "default_agent_id": self.default_agent_id,
            "bridge_enabled": self.bridge_enabled,
            "bridge_online": self.is_bridge_online(),
            "bridge_connected_at": self.bridge_connected_at,
            "bridge_last_seen_at": self.bridge_last_seen_at,
            "bridge_age_seconds": bridge_age_seconds,
            "bridge_real_name": self.bridge_real_name,
            "bridge_current_url": self.bridge_current_url,
            "bridge_last_error": self.bridge_last_error,
        }


@dataclass
class BridgeTask:
    task_id: str
    user_id: str
    message: str
    model: str
    search_source: str
    chat_id: str | None = None
    agent_id: str | None = None
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    answer: str | None = None
    error: str | None = None
    raw: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "message": self.message,
            "model": self.model,
            "search_source": self.search_source,
            "chat_id": self.chat_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SessionStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._users_by_id: dict[str, UserAccount] = {}
        self._user_ids_by_token: dict[str, str] = {}
        self._yiwen_sessions: dict[str, StoredYiwenSession] = {}
        self._bridge_tasks: dict[str, BridgeTask] = {}
        self._load_users()

    @staticmethod
    def _state_dir() -> Path:
        path = Path(__file__).resolve().parents[2] / ".state"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _users_path(cls) -> Path:
        return cls._state_dir() / "users.json"

    def _load_users(self) -> None:
        path = self._users_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in payload.get("users", []):
            try:
                account = UserAccount.from_payload(item)
            except (KeyError, TypeError, ValueError):
                continue
            self._users_by_id[account.user_id] = account
            self._user_ids_by_token[account.access_token] = account.user_id

    def _save_users(self) -> None:
        payload = {"users": [account.to_payload() for account in self._users_by_id.values()]}
        self._users_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def login_user(self, user_id: str, display_name: str | None = None) -> UserAccount:
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("user_id cannot be empty")

        with self._lock:
            existing = self._users_by_id.get(normalized_user_id)
            now = time.time()
            if existing:
                raise ValueError("user_id already exists; use the existing bearer token or create a new local session")

            account = UserAccount(
                user_id=normalized_user_id,
                display_name=display_name,
                access_token=secrets.token_urlsafe(32),
                created_at=now,
                last_seen_at=now,
            )
            self._users_by_id[account.user_id] = account
            self._user_ids_by_token[account.access_token] = account.user_id
            self._save_users()
            return account


    def create_local_user(self, display_name: str | None = None) -> UserAccount:
        with self._lock:
            now = time.time()
            while True:
                user_id = "local-" + secrets.token_urlsafe(12)
                if user_id not in self._users_by_id:
                    break
            account = UserAccount(
                user_id=user_id,
                display_name=display_name,
                access_token=secrets.token_urlsafe(32),
                created_at=now,
                last_seen_at=now,
            )
            self._users_by_id[account.user_id] = account
            self._user_ids_by_token[account.access_token] = account.user_id
            self._save_users()
            return account
    def get_user_by_token(self, access_token: str) -> UserAccount | None:
        with self._lock:
            user_id = self._user_ids_by_token.get(access_token)
            if not user_id:
                return None
            account = self._users_by_id.get(user_id)
            if account:
                account.last_seen_at = time.time()
                self._save_users()
            return account

    def get_user(self, user_id: str) -> UserAccount | None:
        with self._lock:
            return self._users_by_id.get(user_id)

    @staticmethod
    def parse_cookie_header(cookie_header: str | None) -> dict[str, str]:
        if not cookie_header:
            return {}
        parsed = SimpleCookie()
        parsed.load(cookie_header)
        return {key: morsel.value for key, morsel in parsed.items() if morsel.value}

    def upsert_yiwen(
        self,
        user_id: str,
        bearer_token: str,
        *,
        cookies: dict[str, str] | None = None,
        cookie_header: str | None = None,
        username: str | None = None,
        chat_id: str | None = None,
        agent_id: str | None = None,
        bridge_enabled: bool | None = None,
        bridge_connected_at: float | None = None,
        bridge_last_seen_at: float | None = None,
        bridge_real_name: str | None = None,
        bridge_current_url: str | None = None,
        bridge_last_error: str | None = None,
    ) -> StoredYiwenSession:
        parsed_from_header = self.parse_cookie_header(cookie_header)
        merged_input_cookies = {**parsed_from_header, **(cookies or {})}

        with self._lock:
            existing = self._yiwen_sessions.get(user_id)
            merged_cookies = dict(existing.cookies) if existing else {}
            merged_cookies.update(merged_input_cookies)

            record = StoredYiwenSession(
                user_id=user_id,
                bearer_token=bearer_token or (existing.bearer_token if existing else ""),
                cookies=merged_cookies,
                username=username or (existing.username if existing else None),
                default_chat_id=chat_id or (existing.default_chat_id if existing else None),
                default_agent_id=agent_id or (existing.default_agent_id if existing else None),
                bridge_enabled=(bridge_enabled if bridge_enabled is not None else (existing.bridge_enabled if existing else False)),
                bridge_connected_at=(bridge_connected_at if bridge_connected_at is not None else (existing.bridge_connected_at if existing else None)),
                bridge_last_seen_at=(bridge_last_seen_at if bridge_last_seen_at is not None else (existing.bridge_last_seen_at if existing else None)),
                bridge_real_name=bridge_real_name or (existing.bridge_real_name if existing else None),
                bridge_current_url=bridge_current_url or (existing.bridge_current_url if existing else None),
                bridge_last_error=(bridge_last_error if bridge_last_error is not None else (existing.bridge_last_error if existing else None)),
            )
            self._yiwen_sessions[user_id] = record
            return record

    def touch_yiwen_bridge(
        self,
        user_id: str,
        *,
        current_url: str | None = None,
        current_chat_id: str | None = None,
        current_agent_id: str | None = None,
        last_error: str | None = None,
    ) -> StoredYiwenSession | None:
        with self._lock:
            existing = self._yiwen_sessions.get(user_id)
            if not existing:
                return None
            now = time.time()
            existing.bridge_enabled = True
            existing.bridge_last_seen_at = now
            if existing.bridge_connected_at is None:
                existing.bridge_connected_at = now
            if current_url:
                existing.bridge_current_url = current_url
            if current_chat_id:
                existing.default_chat_id = current_chat_id
            if current_agent_id:
                existing.default_agent_id = current_agent_id
            existing.bridge_last_error = last_error
            return existing

    def is_yiwen_bridge_online(self, user_id: str) -> bool:
        with self._lock:
            existing = self._yiwen_sessions.get(user_id)
            return bool(existing and existing.is_bridge_online())

    def update_yiwen_defaults(self, user_id: str, *, chat_id: str | None = None, agent_id: str | None = None) -> StoredYiwenSession | None:
        with self._lock:
            existing = self._yiwen_sessions.get(user_id)
            if not existing:
                return None
            if chat_id:
                existing.default_chat_id = chat_id
            if agent_id:
                existing.default_agent_id = agent_id
            return existing

    def get_yiwen(self, user_id: str) -> StoredYiwenSession | None:
        with self._lock:
            return self._yiwen_sessions.get(user_id)

    def get_yiwen_status(self, user_id: str) -> dict[str, Any]:
        session = self.get_yiwen(user_id)
        if not session:
            return {"user_id": user_id, "has_session": False, "message": "当前用户还没有登记逸问会话。"}
        return session.status_payload()

    def create_bridge_task(self, *, user_id: str, message: str, model: str, search_source: str, chat_id: str | None = None, agent_id: str | None = None) -> BridgeTask:
        task = BridgeTask(
            task_id=secrets.token_urlsafe(12),
            user_id=user_id,
            message=message,
            model=model,
            search_source=search_source,
            chat_id=chat_id,
            agent_id=agent_id,
        )
        with self._lock:
            self._bridge_tasks[task.task_id] = task
        return task

    def claim_pending_bridge_task(self, user_id: str) -> BridgeTask | None:
        with self._lock:
            pending = [task for task in self._bridge_tasks.values() if task.user_id == user_id and task.status == "pending"]
            if not pending:
                return None
            task = sorted(pending, key=lambda item: item.created_at)[0]
            task.status = "claimed"
            task.updated_at = time.time()
            return task

    def complete_bridge_task(self, *, task_id: str, answer: str | None, error: str | None, raw: dict[str, Any] | None, chat_id: str | None = None, agent_id: str | None = None) -> BridgeTask | None:
        with self._lock:
            task = self._bridge_tasks.get(task_id)
            if not task:
                return None
            task.answer = answer
            task.error = error
            task.raw = raw
            if chat_id:
                task.chat_id = chat_id
            if agent_id:
                task.agent_id = agent_id
            task.status = "done" if not error else "error"
            task.updated_at = time.time()
            return task

    def get_bridge_task(self, task_id: str) -> BridgeTask | None:
        with self._lock:
            return self._bridge_tasks.get(task_id)

