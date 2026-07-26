from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.api.routes import sysu_anything_chat
from app.main import app
from app.services.sysu_anything_chat import SysuAnythingChatService


class LocalBridgeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_uses_registered_localhost_port(self) -> None:
        service = SysuAnythingChatService()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir).resolve()
            service._remember_debug_port(state_dir, 43123)
            runner = AsyncMock(return_value={"authState": {"token": "must-not-leave-backend"}})
            with patch.object(service, "_run_json", runner):
                await service.import_chrome_debug(state_dir=state_dir)

            args = runner.await_args.args
            self.assertEqual(args[:4], ("chat", "import-chrome-debug", "--host", "127.0.0.1"))
            self.assertEqual(args[4:6], ("--port", "43123"))

    def test_launch_is_bound_to_dynamic_localhost_port(self) -> None:
        service = SysuAnythingChatService()
        fake_process = Mock(pid=12345)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            with (
                patch.object(service, "_find_chrome", return_value=Path("chrome.exe")),
                patch.object(service, "_allocate_local_port", return_value=43124),
                patch("app.services.sysu_anything_chat.subprocess.Popen", return_value=fake_process) as popen,
            ):
                result = service.launch_chrome_debug(state_dir=state_dir)

            command = popen.call_args.args[0]
            self.assertIn("--remote-debugging-address=127.0.0.1", command)
            self.assertIn("--remote-debugging-port=43124", command)
            self.assertEqual(result["port"], 43124)


class LocalBridgeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        login = self.client.post("/auth/local")
        self.assertEqual(login.status_code, 200)
        self.headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    def test_bridge_response_never_returns_token(self) -> None:
        imported = {"authState": {"token": "must-not-leave-backend"}}
        status = {
            "configured": True,
            "state_dir": "local-state",
            "auth_file": "local-auth",
            "session_file": "local-session",
            "cli_path": "local-cli",
        }
        with (
            patch.object(sysu_anything_chat, "import_chrome_debug", AsyncMock(return_value=imported)),
            patch.object(sysu_anything_chat, "status", return_value=status),
        ):
            response = self.client.post("/auth/yiwen/chrome/import", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("result", response.json())
        self.assertNotIn("must-not-leave-backend", response.text)

    def test_legacy_bridge_response_is_also_redacted(self) -> None:
        imported = {
            "authState": {
                "token": "must-not-leave-backend",
                "username": "local-test",
            }
        }
        status = {
            "configured": True,
            "state_dir": "local-state",
            "auth_file": "local-auth",
            "session_file": "local-session",
            "cli_path": "local-cli",
        }
        with (
            patch.object(sysu_anything_chat, "import_chrome_debug", AsyncMock(return_value=imported)),
            patch.object(sysu_anything_chat, "status", return_value=status),
        ):
            response = self.client.post("/admin/yiwen/shared/chrome/import")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("result", response.json())
        self.assertNotIn("must-not-leave-backend", response.text)

    def test_manual_token_import_endpoint_is_removed(self) -> None:
        response = self.client.post(
            "/auth/yiwen/browser/import",
            headers=self.headers,
            json={"token": "manual-token-is-disabled"},
        )
        self.assertEqual(response.status_code, 404)
