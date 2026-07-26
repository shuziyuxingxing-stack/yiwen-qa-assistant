from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from app.core.runtime import PROJECT_ROOT, resolve_sysu_anything_cli


class SysuAnythingCliError(RuntimeError):
    def __init__(self, message: str, *, stdout: str = "", stderr: str = "", returncode: int | None = None) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@dataclass
class SysuAnythingChatResult:
    chat_id: str | None
    answer: str
    raw: dict[str, Any]


@dataclass
class SysuAnythingChatHistoryItem:
    chat_id: str
    title: str
    agent_name: str | None
    updated_at: str | None
    raw: dict[str, Any]


class SysuAnythingChatService:
    def __init__(self) -> None:
        self.project_root = PROJECT_ROOT
        self.state_dir = self.project_root / ".state" / "sysu-anything-chat"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.chrome_profile_dir = self.state_dir / "chrome-profile"
        self.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
        self.cli_path = resolve_sysu_anything_cli()
        self.node_bin = os.getenv("SYSU_ANYTHING_NODE", "node")
        self.keepalive_interval_seconds = int(os.getenv("YIWEN_KEEPALIVE_SECONDS", "300"))
        self.auto_import_from_chrome = os.getenv("YIWEN_AUTO_IMPORT_CHROME", "1").strip().lower() not in {"0", "false", "no"}
        self.chrome_debug_port = int(os.getenv("YIWEN_CHROME_DEBUG_PORT", "9222"))
        self._debug_ports: dict[str, int] = {}
        self._debug_port_lock = RLock()
        self._keepalive_task: asyncio.Task | None = None
        self._last_keepalive: dict[str, Any] = {
            "running": False,
            "last_checked_at": None,
            "last_success_at": None,
            "last_error_at": None,
            "last_error": None,
            "last_auto_import_at": None,
            "last_auto_import_error": None,
        }

    def _resolve_state_dir(self, state_dir: Path | str | None = None) -> Path:
        resolved = Path(state_dir) if state_dir is not None else self.state_dir
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved.resolve()

    @staticmethod
    def _state_key(state_dir: Path) -> str:
        return str(state_dir).casefold()

    @staticmethod
    def _allocate_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def _local_port_is_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            return False

    def _registered_debug_port(self, state_dir: Path) -> int | None:
        with self._debug_port_lock:
            return self._debug_ports.get(self._state_key(state_dir))

    def _remember_debug_port(self, state_dir: Path, port: int) -> None:
        with self._debug_port_lock:
            self._debug_ports[self._state_key(state_dir)] = port

    def auth_file(self, state_dir: Path | str | None = None) -> Path:
        return self._resolve_state_dir(state_dir) / "chat-auth.json"

    def session_file(self, state_dir: Path | str | None = None) -> Path:
        return self._resolve_state_dir(state_dir) / "chat-session.json"

    def has_auth(self, state_dir: Path | str | None = None) -> bool:
        try:
            payload = json.loads(self.auth_file(state_dir).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(str(payload.get("token") or "").strip())

    def load_auth_state(self, state_dir: Path | str | None = None) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.auth_file(state_dir).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not payload.get("token"):
            return None
        return payload

    async def _run_json(self, *args: str, timeout: float = 90.0, state_dir: Path | str | None = None) -> dict[str, Any]:
        if not self.cli_path.exists():
            raise SysuAnythingCliError(f"sysu-anything CLI not found: {self.cli_path}")
        resolved_state_dir = self._resolve_state_dir(state_dir)
        cmd = [self.node_bin, str(self.cli_path), *args, "--state-dir", str(resolved_state_dir), "--json"]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise SysuAnythingCliError(f"sysu-anything command timed out: {' '.join(args)}") from exc
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            message = stderr or stdout or f"sysu-anything exited with {process.returncode}"
            raise SysuAnythingCliError(message, stdout=stdout, stderr=stderr, returncode=process.returncode)
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SysuAnythingCliError(
                f"sysu-anything did not return JSON for {' '.join(args)}",
                stdout=stdout,
                stderr=stderr,
                returncode=process.returncode,
            ) from exc
        if not isinstance(parsed, dict):
            raise SysuAnythingCliError("sysu-anything JSON output is not an object", stdout=stdout, stderr=stderr)
        return parsed

    async def auth_url(self, *, state_dir: Path | str | None = None) -> dict[str, Any]:
        return await self._run_json("chat", "auth-url", timeout=45.0, state_dir=state_dir)

    async def replay_callback(self, callback_url: str, *, state_dir: Path | str | None = None) -> dict[str, Any]:
        return await self._run_json("chat", "replay-callback", "--url", callback_url, timeout=90.0, state_dir=state_dir)

    async def import_chrome_debug(
        self,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        skip_validate: bool = False,
        state_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        resolved_state_dir = self._resolve_state_dir(state_dir)
        resolved_port = port or self._registered_debug_port(resolved_state_dir) or self.chrome_debug_port
        args = ["chat", "import-chrome-debug", "--host", host, "--port", str(resolved_port)]
        if skip_validate:
            args.append("--skip-validate")
        return await self._run_json(*args, timeout=90.0, state_dir=resolved_state_dir)

    async def validate_auth(self, *, agent_id: str = "default", state_dir: Path | str | None = None) -> dict[str, Any]:
        return await self._run_json("chat", "agent", "--id", agent_id, timeout=45.0, state_dir=state_dir)

    async def send(
        self,
        *,
        message: str,
        chat_id: str | None = None,
        agent_id: str | None = None,
        model: str = "V3",
        search_source: str = "sysuKB",
        state_dir: Path | str | None = None,
    ) -> SysuAnythingChatResult:
        args = ["chat", "send", "--message", message, "--model", model]
        if chat_id:
            args.extend(["--chat-id", chat_id])
        if agent_id:
            args.extend(["--agent", agent_id])
        if search_source:
            args.extend(["--search-source", search_source])
        payload = await self._run_json(*args, timeout=180.0, state_dir=state_dir)
        return self._parse_send_payload(payload)

    async def send_with_recovery(
        self,
        *,
        message: str,
        chat_id: str | None = None,
        agent_id: str | None = None,
        model: str = "V3",
        search_source: str = "sysuKB",
        state_dir: Path | str | None = None,
    ) -> SysuAnythingChatResult:
        try:
            return await self.send(
                message=message,
                chat_id=chat_id,
                agent_id=agent_id,
                model=model,
                search_source=search_source,
                state_dir=state_dir,
            )
        except SysuAnythingCliError as first_error:
            await self.recover_from_chrome(reason=str(first_error), state_dir=state_dir)
            return await self.send(
                message=message,
                chat_id=chat_id,
                agent_id=agent_id,
                model=model,
                search_source=search_source,
                state_dir=state_dir,
            )



    @staticmethod
    def _clean_history_title(value: str) -> str:
        title = " ".join(str(value or "").split())
        for marker in ("以下是本助手检索到的", "以下是本助手辅助知识库", "[内置辅助知识库命中]", "[外部辅助知识库命中]"):
            index = title.find(marker)
            if index > 0:
                title = title[:index].strip()
        return title[:40] if title else title
    async def list_chats(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
        state_dir: Path | str | None = None,
    ) -> list[SysuAnythingChatHistoryItem]:
        args = ["chat", "chats", "--page", str(max(1, page)), "--size", str(max(1, min(size, 50)))]
        if keyword:
            args.extend(["--keyword", keyword])
        payload = await self._run_json(*args, timeout=60.0, state_dir=state_dir)
        records = payload.get("records") or payload.get("list") or payload.get("items")
        if not isinstance(records, list) and isinstance(payload.get("data"), dict):
            data = payload["data"]
            records = data.get("records") or data.get("list") or data.get("items")
        if not isinstance(records, list):
            records = []
        items: list[SysuAnythingChatHistoryItem] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            chat_id = str(record.get("id") or record.get("chatId") or record.get("chat_id") or "").strip()
            if not chat_id:
                continue
            title = self._clean_history_title(str(record.get("chatTitle") or record.get("title") or record.get("name") or "Untitled chat"))
            if title.strip() in {"", "新会话", "Untitled chat"}:
                try:
                    title = self._message_title_from_records(await self.list_messages(chat_id=chat_id, size=8, state_dir=state_dir)) or title
                except Exception:
                    title = title or chat_id
            updated_at = record.get("updateTime") or record.get("updateDate") or record.get("createTime") or record.get("createDate")
            items.append(SysuAnythingChatHistoryItem(
                chat_id=chat_id,
                title=title,
                agent_name=record.get("agentName") if isinstance(record.get("agentName"), str) else None,
                updated_at=str(updated_at) if updated_at else None,
                raw=record,
            ))
        return items


    async def list_messages(
        self,
        *,
        chat_id: str,
        page: int = 1,
        size: int = 10,
        state_dir: Path | str | None = None,
    ) -> list[dict[str, Any]]:
        payload = await self._run_json(
            "chat",
            "messages",
            "--chat-id",
            chat_id,
            "--page",
            str(max(1, page)),
            "--size",
            str(max(1, min(size, 50))),
            timeout=60.0,
            state_dir=state_dir,
        )
        records = payload.get("records") or payload.get("list") or payload.get("items")
        if not isinstance(records, list) and isinstance(payload.get("data"), dict):
            data = payload["data"]
            records = data.get("records") or data.get("list") or data.get("items")
        return records if isinstance(records, list) else []

    @staticmethod
    def _message_title_from_records(records: list[dict[str, Any]]) -> str | None:
        for record in records:
            if not isinstance(record, dict):
                continue
            role = str(record.get("role") or record.get("messageType") or record.get("type") or "").lower()
            if role and not any(token in role for token in ("user", "human", "question", "input")):
                continue
            candidates = [
                record.get("inputContent"),
                record.get("question"),
                record.get("content"),
                record.get("message"),
                record.get("text"),
            ]
            for value in candidates:
                if isinstance(value, dict):
                    value = value.get("content") or value.get("text") or value.get("message")
                if isinstance(value, str):
                    title = " ".join(value.split())
                    if title:
                        return SysuAnythingChatService._clean_history_title(title)
        return None
    def _parse_send_payload(self, payload: dict[str, Any]) -> SysuAnythingChatResult:
        completion = payload.get("completion") if isinstance(payload.get("completion"), dict) else {}
        answer = str(completion.get("outputContent") or completion.get("content") or "").strip()
        return SysuAnythingChatResult(
            chat_id=str(payload.get("chatId") or completion.get("chatId") or "") or None,
            answer=answer,
            raw=payload,
        )

    async def recover_from_chrome(self, *, reason: str | None = None, state_dir: Path | str | None = None) -> dict[str, Any]:
        now = time.time()
        self._last_keepalive["last_error_at"] = now
        if reason:
            self._last_keepalive["last_error"] = reason
        result = await self.import_chrome_debug(skip_validate=False, state_dir=state_dir)
        self._last_keepalive["last_auto_import_at"] = time.time()
        self._last_keepalive["last_auto_import_error"] = None
        self._last_keepalive["last_success_at"] = time.time()
        self._last_keepalive["last_error"] = None
        return result

    def launch_chrome_debug(self, *, port: int | None = None, state_dir: Path | str | None = None) -> dict[str, Any]:
        chrome_path = self._find_chrome()
        if not chrome_path:
            raise SysuAnythingCliError("Chrome or Edge was not found; cannot launch the Yiwen login browser.")
        resolved_state_dir = self._resolve_state_dir(state_dir)
        existing_port = self._registered_debug_port(resolved_state_dir)
        if port is None and existing_port and self._local_port_is_open(existing_port):
            return {
                "started": True,
                "reused": True,
                "port": existing_port,
                "profile_dir": str(resolved_state_dir / "chrome-profile"),
                "state_dir": str(resolved_state_dir),
                "url": "https://chat.sysu.edu.cn/znt/chat/empty",
                "message": "The existing local Yiwen authorization window is ready.",
            }
        resolved_port = port or (self._allocate_local_port() if state_dir is not None else self.chrome_debug_port)
        chrome_profile_dir = resolved_state_dir / "chrome-profile"
        chrome_profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            str(chrome_path),
            f"--remote-debugging-port={resolved_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={chrome_profile_dir}",
            "--no-first-run",
            "--new-window",
            "https://chat.sysu.edu.cn/znt/chat/empty",
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(args, cwd=str(self.project_root), creationflags=creationflags)
        self._remember_debug_port(resolved_state_dir, resolved_port)
        return {
            "started": True,
            "pid": proc.pid,
            "port": resolved_port,
            "profile_dir": str(chrome_profile_dir),
            "state_dir": str(resolved_state_dir),
            "url": "https://chat.sysu.edu.cn/znt/chat/empty",
            "message": "Finish Yiwen login in the opened browser, then import the login state on this page.",
        }

    async def keepalive_once(self) -> dict[str, Any]:
        now = time.time()
        self._last_keepalive["last_checked_at"] = now
        if not self.has_auth():
            self._last_keepalive["last_error_at"] = now
            self._last_keepalive["last_error"] = "missing chat-auth.json"
            if self.auto_import_from_chrome:
                return await self._try_auto_import_then_validate(now)
            return self.keepalive_status()
        try:
            await self.validate_auth()
            self._last_keepalive["last_success_at"] = time.time()
            self._last_keepalive["last_error"] = None
            return self.keepalive_status()
        except Exception as exc:
            self._last_keepalive["last_error_at"] = time.time()
            self._last_keepalive["last_error"] = str(exc)
            if self.auto_import_from_chrome:
                return await self._try_auto_import_then_validate(time.time())
            return self.keepalive_status()

    async def _try_auto_import_then_validate(self, now: float) -> dict[str, Any]:
        try:
            await self.import_chrome_debug(skip_validate=False)
            self._last_keepalive["last_auto_import_at"] = time.time()
            self._last_keepalive["last_auto_import_error"] = None
            await self.validate_auth()
            self._last_keepalive["last_success_at"] = time.time()
            self._last_keepalive["last_error"] = None
        except Exception as exc:
            self._last_keepalive["last_auto_import_error"] = str(exc)
            self._last_keepalive["last_error_at"] = time.time()
            self._last_keepalive["last_error"] = str(exc)
        return self.keepalive_status()

    def start_keepalive(self) -> None:
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._last_keepalive["running"] = True
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def stop_keepalive(self) -> None:
        self._last_keepalive["running"] = False
        if not self._keepalive_task:
            return
        self._keepalive_task.cancel()
        try:
            await self._keepalive_task
        except asyncio.CancelledError:
            pass

    async def _keepalive_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.keepalive_once()
            except Exception as exc:
                self._last_keepalive["last_error_at"] = time.time()
                self._last_keepalive["last_error"] = str(exc)
            await asyncio.sleep(max(30, self.keepalive_interval_seconds))

    def keepalive_status(self) -> dict[str, Any]:
        return {
            **self._last_keepalive,
            "interval_seconds": self.keepalive_interval_seconds,
            "auto_import_from_chrome": self.auto_import_from_chrome,
            "chrome_debug_port": self.chrome_debug_port,
        }

    @staticmethod
    def _find_chrome() -> Path | None:
        candidates: list[Path] = []
        env_path = os.getenv("SYSU_ANYTHING_CHROME", "").strip()
        if env_path:
            candidates.append(Path(env_path))
        candidates.extend([
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ])
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def status(self, state_dir: Path | str | None = None) -> dict[str, Any]:
        resolved_state_dir = self._resolve_state_dir(state_dir)
        auth = self.load_auth_state(resolved_state_dir)
        auth_file = self.auth_file(resolved_state_dir)
        return {
            "configured": bool(auth),
            "state_dir": str(resolved_state_dir),
            "auth_file": str(auth_file),
            "session_file": str(self.session_file(resolved_state_dir)),
            "cli_path": str(self.cli_path),
            "chrome_debug_host": "127.0.0.1",
            "active_chrome_debug_port": self._registered_debug_port(resolved_state_dir),
            "username": auth.get("username") if auth else None,
            "real_name": auth.get("realName") if auth else None,
            "obtained_at": auth.get("obtainedAt") if auth else None,
            "jwt_expires_at": auth.get("jwtExpiresAt") if auth else None,
            "updated": auth_file.stat().st_mtime if auth_file.exists() else None,
            "keepalive": self.keepalive_status(),
        }


sysu_anything_chat = SysuAnythingChatService()
