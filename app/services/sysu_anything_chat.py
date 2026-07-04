from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


class SysuAnythingChatService:
    def __init__(self) -> None:
        self.project_root = Path(__file__).resolve().parents[2]
        self.state_dir = self.project_root / ".state" / "sysu-anything-chat"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.chrome_profile_dir = self.state_dir / "chrome-profile"
        self.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
        self.cli_path = Path(os.getenv("SYSU_ANYTHING_CLI", r"C:\Users\86152\AppData\Local\Temp\package\bin\sysu-anything.js"))
        self.node_bin = os.getenv("SYSU_ANYTHING_NODE", "node")
        self.keepalive_interval_seconds = int(os.getenv("YIWEN_KEEPALIVE_SECONDS", "300"))
        self.auto_import_from_chrome = os.getenv("YIWEN_AUTO_IMPORT_CHROME", "1").strip().lower() not in {"0", "false", "no"}
        self.chrome_debug_port = int(os.getenv("YIWEN_CHROME_DEBUG_PORT", "9222"))
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

    def auth_file(self) -> Path:
        return self.state_dir / "chat-auth.json"

    def session_file(self) -> Path:
        return self.state_dir / "chat-session.json"

    def has_auth(self) -> bool:
        try:
            payload = json.loads(self.auth_file().read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(str(payload.get("token") or "").strip())

    def load_auth_state(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.auth_file().read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not payload.get("token"):
            return None
        return payload

    async def _run_json(self, *args: str, timeout: float = 90.0) -> dict[str, Any]:
        if not self.cli_path.exists():
            raise SysuAnythingCliError(f"sysu-anything CLI not found: {self.cli_path}")
        cmd = [self.node_bin, str(self.cli_path), *args, "--state-dir", str(self.state_dir), "--json"]
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

    async def auth_url(self) -> dict[str, Any]:
        return await self._run_json("chat", "auth-url", timeout=45.0)

    async def replay_callback(self, callback_url: str) -> dict[str, Any]:
        return await self._run_json("chat", "replay-callback", "--url", callback_url, timeout=90.0)

    async def import_chrome_debug(self, *, host: str = "127.0.0.1", port: int | None = None, skip_validate: bool = False) -> dict[str, Any]:
        args = ["chat", "import-chrome-debug", "--host", host, "--port", str(port or self.chrome_debug_port)]
        if skip_validate:
            args.append("--skip-validate")
        return await self._run_json(*args, timeout=90.0)

    async def validate_auth(self, *, agent_id: str = "default") -> dict[str, Any]:
        return await self._run_json("chat", "agent", "--id", agent_id, timeout=45.0)

    async def send(
        self,
        *,
        message: str,
        chat_id: str | None = None,
        agent_id: str | None = None,
        model: str = "V3",
        search_source: str = "sysuKB",
    ) -> SysuAnythingChatResult:
        args = ["chat", "send", "--message", message, "--model", model]
        if chat_id:
            args.extend(["--chat-id", chat_id])
        if agent_id:
            args.extend(["--agent", agent_id])
        if search_source:
            args.extend(["--search-source", search_source])
        payload = await self._run_json(*args, timeout=180.0)
        return self._parse_send_payload(payload)

    async def send_with_recovery(
        self,
        *,
        message: str,
        chat_id: str | None = None,
        agent_id: str | None = None,
        model: str = "V3",
        search_source: str = "sysuKB",
    ) -> SysuAnythingChatResult:
        try:
            return await self.send(
                message=message,
                chat_id=chat_id,
                agent_id=agent_id,
                model=model,
                search_source=search_source,
            )
        except SysuAnythingCliError as first_error:
            await self.recover_from_chrome(reason=str(first_error))
            return await self.send(
                message=message,
                chat_id=chat_id,
                agent_id=agent_id,
                model=model,
                search_source=search_source,
            )

    def _parse_send_payload(self, payload: dict[str, Any]) -> SysuAnythingChatResult:
        completion = payload.get("completion") if isinstance(payload.get("completion"), dict) else {}
        answer = str(completion.get("outputContent") or completion.get("content") or "").strip()
        return SysuAnythingChatResult(
            chat_id=str(payload.get("chatId") or completion.get("chatId") or "") or None,
            answer=answer,
            raw=payload,
        )

    async def recover_from_chrome(self, *, reason: str | None = None) -> dict[str, Any]:
        now = time.time()
        self._last_keepalive["last_error_at"] = now
        if reason:
            self._last_keepalive["last_error"] = reason
        result = await self.import_chrome_debug(skip_validate=False)
        self._last_keepalive["last_auto_import_at"] = time.time()
        self._last_keepalive["last_auto_import_error"] = None
        self._last_keepalive["last_success_at"] = time.time()
        self._last_keepalive["last_error"] = None
        return result

    def launch_chrome_debug(self, *, port: int | None = None) -> dict[str, Any]:
        resolved_port = port or self.chrome_debug_port
        chrome_path = self._find_chrome()
        if not chrome_path:
            raise SysuAnythingCliError("未找到 Chrome 或 Edge，无法启动 SYSU-Anything Chrome 调试导入窗口。")
        args = [
            str(chrome_path),
            f"--remote-debugging-port={resolved_port}",
            f"--user-data-dir={self.chrome_profile_dir}",
            "--no-first-run",
            "--new-window",
            "https://chat.sysu.edu.cn/znt/chat/empty",
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(args, cwd=str(self.project_root), creationflags=creationflags)
        return {
            "started": True,
            "pid": proc.pid,
            "port": resolved_port,
            "profile_dir": str(self.chrome_profile_dir),
            "url": "https://chat.sysu.edu.cn/znt/chat/empty",
            "message": "请在打开的浏览器窗口完成官方逸问登录，然后回到本页点击“从浏览器导入登录态”。后续只要该浏览器登录态还在，后端可自动重新导入。",
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

    def status(self) -> dict[str, Any]:
        auth = self.load_auth_state()
        return {
            "configured": bool(auth),
            "state_dir": str(self.state_dir),
            "auth_file": str(self.auth_file()),
            "session_file": str(self.session_file()),
            "cli_path": str(self.cli_path),
            "username": auth.get("username") if auth else None,
            "real_name": auth.get("realName") if auth else None,
            "obtained_at": auth.get("obtainedAt") if auth else None,
            "jwt_expires_at": auth.get("jwtExpiresAt") if auth else None,
            "updated": self.auth_file().stat().st_mtime if self.auth_file().exists() else None,
            "keepalive": self.keepalive_status(),
        }


sysu_anything_chat = SysuAnythingChatService()
