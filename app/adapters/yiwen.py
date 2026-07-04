from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse
import json

import httpx


@dataclass
class YiwenSession:
    bearer_token: str
    cookies: dict[str, str] = field(default_factory=dict)
    username: str | None = None


@dataclass
class YiwenResult:
    answer: str
    scope: str = "校园资讯"
    raw: dict[str, Any] | None = None


@dataclass
class YiwenChatMeta:
    chat_id: str
    agent_id: str | None = None
    agent_name: str | None = None
    chat_title: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class YiwenMessagePage:
    chat_id: str
    items: list[dict[str, Any]]
    page: int
    size: int
    raw: dict[str, Any] | None = None


@dataclass
class YiwenAuthUrlResult:
    authorize_url: str
    third_type: str = "qwweb"
    state: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


@dataclass
class YiwenReplayResult:
    third_type: str
    token: str
    username: str | None = None
    real_name: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


class YiwenAuthError(ValueError):
    pass


class YiwenAdapter:
    def __init__(self, base_url: str = "https://chat.sysu.edu.cn", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _chat_referer_path(chat_id: str) -> str:
        return f"/znt/chat/{chat_id}"

    def _headers(
        self,
        session: YiwenSession,
        referer_path: str | None = None,
        *,
        accept_event_stream: bool = False,
    ) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream" if accept_event_stream else "application/json, text/plain, */*",
            "Authorization": f"Bearer {session.bearer_token}",
        }
        if referer_path:
            headers["Referer"] = f"{self.base_url}{referer_path}"
            headers["Origin"] = self.base_url
        return headers

    @staticmethod
    def _assert_api_ok(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        code = payload.get("code")
        msg = str(payload.get("msg") or payload.get("message") or "")
        if code in {401, 403, "401", "403"} or "认证失败" in msg or "无法访问系统资源" in msg:
            raise YiwenAuthError(msg or f"Yiwen API auth failed: {code}")
        if "code" in payload and str(code) not in {"0", "200"}:
            raise ValueError(msg or f"Yiwen API returned code={code}")

    @classmethod
    def _unwrap(cls, payload: Any) -> Any:
        cls._assert_api_ok(payload)
        if isinstance(payload, dict) and "data" in payload and payload["data"] is not None:
            return payload["data"]
        return payload

    @staticmethod
    def _pick(mapping: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def parse_callback_url(callback_url: str) -> tuple[str, str, str]:
        parsed = urlparse(callback_url)
        query = parse_qs(parsed.query)
        third_type = (query.get("thirdType") or [None])[0]
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [None])[0]
        if not third_type or not code or not state:
            raise ValueError("callback_url 缺少 thirdType/code/state 参数")
        return third_type, code, state

    async def get_wechat_auth_url(self) -> YiwenAuthUrlResult:
        url = f"{self.base_url}/znt/api/third/qwweb/render"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            response = await client.get(
                url,
                headers={
                    "Accept": "*/*",
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 MicroMessenger/8.0.45 wxwork/4.1.20",
                },
            )
            location = response.headers.get("location")
            history: list[dict[str, Any]] = [{"status": response.status_code, "url": url, "location": location}]
            for _ in range(8):
                if not location:
                    break
                if "open.weixin.qq.com" in location:
                    parsed = urlparse(location)
                    query = parse_qs(parsed.query)
                    return YiwenAuthUrlResult(
                        authorize_url=location,
                        third_type="qwweb",
                        state=(query.get("state") or [None])[0],
                        cookies={cookie.name: cookie.value for cookie in client.cookies.jar},
                        raw={"history": history},
                    )
                next_url = location if location.startswith("http") else str(httpx.URL(url).join(location))
                response = await client.get(
                    next_url,
                    headers={
                        "Accept": "*/*",
                        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 MicroMessenger/8.0.45 wxwork/4.1.20",
                    },
                )
                location = response.headers.get("location")
                history.append({"status": response.status_code, "url": next_url, "location": location})
        raise ValueError("Yiwen auth-url did not return a WeChat authorize URL")
    async def replay_callback(
        self,
        third_type: str,
        code: str,
        state: str,
        cookies: dict[str, str] | None = None,
    ) -> YiwenReplayResult:
        url = f"{self.base_url}/znt/api/third/{third_type}/callback"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, cookies=cookies or {}) as client:
            response = await client.get(
                url,
                params={"code": code, "state": state},
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"{self.base_url}/znt/callback?thirdType={third_type}&code={code}&state={state}",
                    "Origin": self.base_url,
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 MicroMessenger/8.0.45 wxwork/4.1.20",
                },
            )
            response.raise_for_status()
            raw = response.json()
            replay_cookies = {cookie.name: cookie.value for cookie in client.cookies.jar}

        data = self._unwrap(raw)
        if not isinstance(data, dict):
            data = {}

        token = self._pick(data, "token", "accessToken")
        if not token:
            raise ValueError("Yiwen callback response missing token")

        return YiwenReplayResult(
            third_type=third_type,
            token=token,
            username=self._pick(data, "username", "userName"),
            real_name=self._pick(data, "realName", "name"),
            cookies=replay_cookies,
            raw=raw if isinstance(raw, dict) else {"payload": raw},
        )

    @staticmethod
    def _resolve_chunk_content(event: dict[str, Any]) -> tuple[str, str]:
        content = event.get("content") if isinstance(event.get("content"), str) else ""
        reasoning = event.get("reasoningContent") if isinstance(event.get("reasoningContent"), str) else ""
        origin = event.get("origin")
        if isinstance(origin, str) and (not content or not reasoning):
            try:
                parsed = json.loads(origin)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                if not content:
                    for key in ("delta_content", "content"):
                        value = parsed.get(key)
                        if isinstance(value, str) and value:
                            content = value
                            break
                if not reasoning:
                    for key in ("reasoningContent", "reasoning_content"):
                        value = parsed.get(key)
                        if isinstance(value, str) and value:
                            reasoning = value
                            break
        return content, reasoning

    @staticmethod
    def _normalize_streaming_text(value: str) -> str:
        return value.replace("。", "").replace("！", "").replace("？", "").replace(".", "").replace("!", "").replace("?", "").strip()

    @classmethod
    def _merge_streaming_text(cls, current: str, incoming: str) -> str:
        if not incoming:
            return current
        if not current:
            return incoming
        current_cmp = cls._normalize_streaming_text(current)
        incoming_cmp = cls._normalize_streaming_text(incoming)
        if current_cmp and current_cmp == incoming_cmp and len(incoming) >= len(current):
            return incoming
        if incoming.startswith(current):
            return incoming
        if current.endswith(incoming):
            return current
        return current + incoming

    async def create_chat(self, session: YiwenSession, agent_id: str = "default") -> YiwenChatMeta:
        url = f"{self.base_url}/znt/api/ai/chat/new"
        payload = {"agentId": agent_id or "default"}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.post(
                url,
                headers=self._headers(session, referer_path="/znt/chat/empty"),
                cookies=session.cookies,
                json=payload,
            )
            response.raise_for_status()
            raw = response.json()

        data = self._unwrap(raw)
        if isinstance(data, str):
            chat_id = data
            data = {}
        elif isinstance(data, dict):
            chat_id = self._pick(data, "id", "chatId", "chat_id")
        else:
            chat_id = None
            data = {}
        if not chat_id:
            raise ValueError("Yiwen create_chat response missing chat id")

        return YiwenChatMeta(
            chat_id=chat_id,
            agent_id=self._pick(data, "agentId", "agent_id") or agent_id or "default",
            agent_name=self._pick(data, "agentName", "agent_name"),
            chat_title=self._pick(data, "chatTitle", "title", "name"),
            raw=raw if isinstance(raw, dict) else {"payload": raw},
        )

    async def get_chat_detail(self, session: YiwenSession, chat_id: str) -> YiwenChatMeta:
        url = f"{self.base_url}/znt/api/ai/chat/{chat_id}"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers=self._headers(session, referer_path=self._chat_referer_path(chat_id)),
                cookies=session.cookies,
            )
            response.raise_for_status()
            payload = response.json()

        data = self._unwrap(payload)
        if not isinstance(data, dict):
            data = {}

        return YiwenChatMeta(
            chat_id=chat_id,
            agent_id=self._pick(data, "agentId", "agent_id"),
            agent_name=self._pick(data, "agentName", "agent_name"),
            chat_title=self._pick(data, "chatTitle", "title", "name"),
            raw=payload if isinstance(payload, dict) else {"payload": payload},
        )

    async def get_agent_detail(
        self,
        session: YiwenSession,
        agent_id: str,
        referer_chat_id: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/znt/api/ai/agent/{agent_id}"
        referer_path = self._chat_referer_path(referer_chat_id) if referer_chat_id else "/znt/chat/empty"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers=self._headers(session, referer_path=referer_path),
                cookies=session.cookies,
            )
            response.raise_for_status()
            payload = response.json()
        return payload if isinstance(payload, dict) else {"payload": payload}

    def build_message_page_payload(self, page: int = 1, size: int = 50) -> dict[str, int]:
        return {"current": page, "size": size}

    async def get_message_page(
        self,
        session: YiwenSession,
        chat_id: str,
        page: int = 1,
        size: int = 50,
    ) -> YiwenMessagePage:
        url = f"{self.base_url}/znt/api/ai/chat/{chat_id}/message/page"
        payload = self.build_message_page_payload(page=page, size=size)
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.post(
                url,
                headers=self._headers(session, referer_path=self._chat_referer_path(chat_id)),
                cookies=session.cookies,
                json=payload,
            )
            response.raise_for_status()
            raw = response.json()

        data = self._unwrap(raw)
        if not isinstance(data, dict):
            data = {}

        items = data.get("records") or data.get("list") or data.get("items") or []
        if not isinstance(items, list):
            items = []

        return YiwenMessagePage(
            chat_id=chat_id,
            items=items,
            page=page,
            size=size,
            raw=raw if isinstance(raw, dict) else {"payload": raw},
        )

    async def build_completion_payload(
        self,
        session: YiwenSession,
        chat_id: str,
        question: str,
        agent_id: str | None = None,
        *,
        images: list[str] | None = None,
        model: str = "V3",
        search_source: str = "sysuKB",
    ) -> dict[str, Any]:
        resolved_agent_id = agent_id
        if not resolved_agent_id:
            meta = await self.get_chat_detail(session, chat_id)
            resolved_agent_id = meta.agent_id or "default"

        return {
            "agentId": resolved_agent_id,
            "chatId": chat_id,
            "images": images or [],
            "model": model,
            "searchSource": search_source,
            "content": question,
        }

    async def send_message(
        self,
        session: YiwenSession,
        chat_id: str,
        question: str,
        agent_id: str | None = None,
        *,
        images: list[str] | None = None,
        model: str = "V3",
        search_source: str = "sysuKB",
    ) -> YiwenResult:
        payload = await self.build_completion_payload(
            session,
            chat_id,
            question,
            agent_id,
            images=images,
            model=model,
            search_source=search_source,
        )
        url = f"{self.base_url}/znt/api/ai/chat/completions"

        answer_parts: list[str] = []
        reasoning_parts: list[str] = []
        raw_events: list[dict[str, Any]] = []
        message_id: str | None = None
        meta_message: dict[str, Any] | None = None
        reference: Any = None
        reference_type: str | None = None

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            async with client.stream(
                "POST",
                url,
                headers=self._headers(session, referer_path=self._chat_referer_path(chat_id), accept_event_stream=True),
                cookies=session.cookies,
                json=payload,
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "text/event-stream" not in content_type:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    try:
                        parsed_body = json.loads(body)
                    except json.JSONDecodeError:
                        parsed_body = {"payload": body}
                    self._assert_api_ok(parsed_body)
                    raise ValueError("Yiwen completions did not return an event stream")
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue

                    data_text = line[5:].strip()
                    if not data_text or data_text == "[DONE]":
                        continue

                    event = json.loads(data_text)
                    if not isinstance(event, dict):
                        continue

                    raw_events.append(event)
                    if isinstance(event.get("id"), str) and event["id"]:
                        message_id = event["id"]
                    if isinstance(event.get("metaMessage"), dict):
                        meta_message = event["metaMessage"]
                    if event.get("reference"):
                        reference_type = event.get("referenceType")
                        try:
                            reference = json.loads(event["reference"])
                        except (TypeError, json.JSONDecodeError):
                            reference = event["reference"]
                    content, reasoning = self._resolve_chunk_content(event)
                    if reasoning:
                        reasoning_parts[:] = [self._merge_streaming_text("".join(reasoning_parts), reasoning)]
                    if content:
                        answer_parts[:] = [self._merge_streaming_text("".join(answer_parts), content)]

        return YiwenResult(
            answer="".join(answer_parts),
            scope=search_source,
            raw={
                "question": question,
                "chat_id": chat_id,
                "payload": payload,
                "message_id": message_id,
                "reasoning_content": "".join(reasoning_parts),
                "meta_message": meta_message,
                "reference": reference,
                "reference_type": reference_type,
                "events": raw_events,
            },
        )
