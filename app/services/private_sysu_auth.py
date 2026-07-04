from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from threading import RLock
from typing import Any


class PrivateSysuAuthError(RuntimeError):
    pass


class PrivateSysuAuthService:
    def __init__(self) -> None:
        self.project_root = Path(__file__).resolve().parents[2]
        self.base_state_dir = self.project_root / ".state" / "private-users"
        self.base_state_dir.mkdir(parents=True, exist_ok=True)
        self.cli_path = Path(os.getenv("SYSU_ANYTHING_CLI", r"C:\Users\86152\AppData\Local\Temp\package\bin\sysu-anything.js"))
        self.node_bin = os.getenv("SYSU_ANYTHING_NODE", "node")
        self.default_service_url = os.getenv("PRIVATE_SYSU_SERVICE_URL", "https://jwxt.sysu.edu.cn/jwxt/")
        self.single_user_fallback = os.getenv("PRIVATE_SYSU_SINGLE_USER_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}
        self._lock = RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._started_at: dict[str, float] = {}

    @staticmethod
    def _key(user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]

    def user_state_dir(self, user_id: str) -> Path:
        path = self.base_state_dir / self._key(user_id) / "sysu-anything"
        path.mkdir(parents=True, exist_ok=True)
        owner_file = path.parent / "owner.json"
        if not owner_file.exists():
            owner_file.write_text(json.dumps({"user_id": user_id, "created_at": time.time()}, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _process_status(self, user_id: str) -> dict[str, Any]:
        key = self._key(user_id)
        with self._lock:
            process = self._processes.get(key)
            started_at = self._started_at.get(key)
            if not process:
                return {"running": False, "started_at": started_at, "returncode": None}
            returncode = process.poll()
            if returncode is not None:
                self._processes.pop(key, None)
            return {"running": returncode is None, "started_at": started_at, "returncode": returncode, "pid": process.pid}

    def latest_qr_file(self, user_id: str) -> Path | None:
        state_dir = self.user_state_dir(user_id)
        candidates = [state_dir / "qr" / "workwechat-login.png"]
        qr_dir = state_dir / "qr"
        if qr_dir.exists():
            candidates.extend(sorted(qr_dir.glob("workwechat-login-*.png"), key=lambda p: p.stat().st_mtime, reverse=True))
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _session_file_has_cas(session_file: Path) -> bool:
        if not session_file.exists():
            return False
        try:
            payload = json.loads(session_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        text = json.dumps(payload, ensure_ascii=False)
        return "cas.sysu.edu.cn" in text or "esc-sso" in text or "cookies" in text

    @staticmethod
    def _read_owner_id(owner_file: Path) -> str | None:
        try:
            payload = json.loads(owner_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        user_id = payload.get("user_id")
        return str(user_id) if user_id else None

    def _latest_fallback_state_dir(self, requested_user_id: str) -> tuple[str, Path] | None:
        if not self.single_user_fallback:
            return None
        candidates: list[tuple[float, str, Path]] = []
        for session_file in self.base_state_dir.glob("*/sysu-anything/session.json"):
            state_dir = session_file.parent
            owner_id = self._read_owner_id(state_dir.parent / "owner.json")
            if not owner_id or owner_id == requested_user_id:
                continue
            if self._session_file_has_cas(session_file):
                try:
                    updated_at = session_file.stat().st_mtime
                except OSError:
                    updated_at = 0.0
                candidates.append((updated_at, owner_id, state_dir))
        if not candidates:
            return None
        _, owner_id, state_dir = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
        return owner_id, state_dir

    def effective_user_state_dir(self, user_id: str) -> tuple[str, Path, bool]:
        own_state_dir = self.user_state_dir(user_id)
        if self._session_file_has_cas(own_state_dir / "session.json"):
            return user_id, own_state_dir, False
        fallback = self._latest_fallback_state_dir(user_id)
        if fallback:
            fallback_user_id, fallback_state_dir = fallback
            return fallback_user_id, fallback_state_dir, True
        return user_id, own_state_dir, False

    def has_cas_session(self, user_id: str) -> bool:
        _, state_dir, _ = self.effective_user_state_dir(user_id)
        return self._session_file_has_cas(state_dir / "session.json")

    def status(self, user_id: str) -> dict[str, Any]:
        own_state_dir = self.user_state_dir(user_id)
        effective_user_id, state_dir, using_fallback = self.effective_user_state_dir(user_id)
        qr_file = self.latest_qr_file(user_id)
        process_status = self._process_status(user_id)
        files = {
            "cas_session": state_dir / "session.json",
            "libic_session": state_dir / "libic-session.json",
            "jwxt_session": state_dir / "jwxt-session.json",
            "usc_bpm_session": state_dir / "usc-bpm-session.json",
            "xgxt_session": state_dir / "xgxt-session.json",
        }
        has_files = {name: path.exists() for name, path in files.items()}
        file_updated_at = {}
        for name, path in files.items():
            try:
                file_updated_at[name] = path.stat().st_mtime if path.exists() else None
            except OSError:
                file_updated_at[name] = None
        has_cas_session = self._session_file_has_cas(files["cas_session"])
        summary = self._summary(has_files, process_status, bool(qr_file), has_cas_session)
        if using_fallback and has_cas_session:
            summary = "当前本地用户未直接绑定，但单机模式已复用最近一次有效个人企业微信/CAS 登录态。"
        return {
            "system": "sysu",
            "scope": "private",
            "user_id": user_id,
            "effective_user_id": effective_user_id,
            "using_single_user_fallback": using_fallback,
            "state_dir": str(state_dir),
            "own_state_dir": str(own_state_dir),
            "has_cas_session": has_cas_session,
            "has_libic_session": has_files["libic_session"],
            "has_jwxt_session": has_files["jwxt_session"],
            "has_jwxt_session_file": has_files["jwxt_session"],
            "jwxt_session_updated_at": file_updated_at["jwxt_session"],
            "cas_session_updated_at": file_updated_at["cas_session"],
            "has_usc_bpm_session": has_files["usc_bpm_session"],
            "has_xgxt_session": has_files["xgxt_session"],
            "qr_ready": bool(qr_file),
            "qr_url": "/auth/private/sysu/workwechat/qr" if qr_file else None,
            "qr_updated_at": qr_file.stat().st_mtime if qr_file else None,
            "login_process": process_status,
            "summary": summary,
        }

    @staticmethod
    def _summary(files: dict[str, bool], process_status: dict[str, Any], qr_ready: bool, has_cas_session: bool) -> str:
        if has_cas_session:
            return "已完成企业微信/CAS 登录，可继续刷新具体业务系统会话。"
        if process_status.get("running") and qr_ready:
            return "二维码已生成，等待用户用企业微信扫码确认。"
        if process_status.get("running"):
            return "正在生成企业微信二维码。"
        if process_status.get("returncode") not in {None, 0}:
            return "企业微信登录进程异常结束，请重新生成二维码。"
        return "尚未绑定个人企业微信登录。"

    def start_workwechat_login(self, user_id: str, *, service_url: str | None = None, timeout_seconds: int = 180) -> dict[str, Any]:
        if not self.cli_path.exists():
            raise PrivateSysuAuthError(f"sysu-anything CLI not found: {self.cli_path}")
        state_dir = self.user_state_dir(user_id)
        key = self._key(user_id)
        with self._lock:
            process = self._processes.get(key)
            if process and process.poll() is None:
                return self.status(user_id)
            stdout_file = state_dir / "auth-workwechat.out.log"
            stderr_file = state_dir / "auth-workwechat.err.log"
            stdout = stdout_file.open("ab")
            stderr = stderr_file.open("ab")
            cmd = [
                self.node_bin,
                str(self.cli_path),
                "auth",
                "workwechat",
                "--service-url",
                service_url or self.default_service_url,
                "--state-dir",
                str(state_dir),
                "--timeout",
                str(timeout_seconds),
                "--json",
            ]
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(cmd, cwd=str(self.project_root), stdout=stdout, stderr=stderr, creationflags=creationflags)
            self._processes[key] = process
            self._started_at[key] = time.time()
        return self.status(user_id)

    async def run_json_for_user(self, user_id: str, *args: str, timeout: float = 90.0) -> Any:
        if not self.cli_path.exists():
            raise PrivateSysuAuthError(f"sysu-anything CLI not found: {self.cli_path}")
        _, state_dir, _ = self.effective_user_state_dir(user_id)
        cmd = [self.node_bin, str(self.cli_path), *args, "--state-dir", str(state_dir), "--json"]
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
            raise PrivateSysuAuthError(f"sysu-anything command timed out: {' '.join(args)}") from exc
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise PrivateSysuAuthError(stderr or stdout or f"sysu-anything exited with {process.returncode}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise PrivateSysuAuthError(f"sysu-anything did not return JSON: {' '.join(args)}") from exc
        return payload

    async def run_text_for_user(self, user_id: str, *args: str, timeout: float = 90.0) -> str:
        if not self.cli_path.exists():
            raise PrivateSysuAuthError(f"sysu-anything CLI not found: {self.cli_path}")
        _, state_dir, _ = self.effective_user_state_dir(user_id)
        cmd = [self.node_bin, str(self.cli_path), *args, "--state-dir", str(state_dir)]
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
            raise PrivateSysuAuthError(f"sysu-anything command timed out: {' '.join(args)}") from exc
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise PrivateSysuAuthError(stderr or stdout or f"sysu-anything exited with {process.returncode}")
        return stdout


private_sysu_auth = PrivateSysuAuthService()









