import re

from app.adapters.sysu_anything import SysuAnythingAdapter
from app.adapters.yiwen import YiwenAdapter, YiwenResult
from app.core.models import AnswerSource, ChatAction, ChatRequest, ChatResponse
from app.core.router import classify_message
from app.services.external_rag import ExternalRagHit, external_rag_service
from app.services.freshman_materials import freshman_materials_service
from app.services.session_store import SessionStore
from app.services.supplement_kb import KbSearchHit, SupplementKbService
from app.services.shared_yiwen import get_shared_default_agent_id, shared_yiwen_manager
from app.services.sysu_anything_chat import SysuAnythingCliError, sysu_anything_chat


CHANNEL_SEARCH_SOURCES = {
    "sysu_kb": "sysuKB",
    "sysu_news": "sysuSE",
    "web_search": "internetSE",
    "model": "model",
}

CHANNEL_TITLES = {
    "sysu_kb": "校内知识库",
    "sysu_news": "校内资讯",
    "web_search": "联网搜索",
    "model": "模型问答",
    "private": "私人事务",
    "auto": "自动路由",
}

EXTERNAL_RAG_TRIGGER_KEYWORDS = (
    "外部知识库",
    "本地知识库",
    "辅助知识库",
    "上传",
    "文档",
    "文件",
    "pdf",
    "PDF",
    "年报",
    "半年报",
    "季报",
    "财报",
    "报告",
    "富特科技",
    "公司",
    "营收",
    "收入",
    "利润",
    "资产",
    "负债",
    "现金流",
    "客户",
    "供应商",
    "业务",
    "财务",
    "证券",
    "股票",
)

EXTERNAL_RAG_BLOCK_KEYWORDS = (
    "你是谁",
    "你是什么模型",
    "模型名称",
    "知识截止",
    "deepseek",
    "DeepSeek",
    "chatgpt",
    "ChatGPT",
    "逸问是谁",
)


class ChatService:
    def __init__(
        self,
        session_store: SessionStore | None = None,
        supplement_kb: SupplementKbService | None = None,
    ) -> None:
        self.yiwen = YiwenAdapter()
        self.private = SysuAnythingAdapter()
        self.sessions = session_store or SessionStore()
        self.supplement_kb = supplement_kb or SupplementKbService()
        self.external_rag = external_rag_service

    async def _query_yiwen(self, payload: ChatRequest) -> tuple[str, list[ChatAction], YiwenResult | None, str | None]:
        upstream_chat_id = payload.chat_id
        resolved_agent_id = payload.agent_id or get_shared_default_agent_id() or "default"

        if not sysu_anything_chat.has_auth():
            return (
                "公共逸问共享账号尚未完成 SYSU-Anything 登录导入。管理员需要打开 /admin/yiwen/shared/login，按页面启动官方逸问登录窗口并导入登录态。",
                [
                    ChatAction(
                        type="auth",
                        system="yiwen",
                        needed=True,
                        message="缺少 SYSU-Anything chat-auth.json，公共问答暂不可用。普通用户不需要处理 token/cookie。",
                    )
                ],
                None,
                upstream_chat_id or payload.chat_id,
            )

        try:
            result = await sysu_anything_chat.send_with_recovery(
                message=payload.message,
                chat_id=upstream_chat_id,
                agent_id=resolved_agent_id,
                model=payload.model,
                search_source=payload.search_source,
            )
            effective_chat_id = result.chat_id or upstream_chat_id
            if effective_chat_id:
                shared_yiwen_manager.set_runtime_chat_id(effective_chat_id)
            shared_yiwen_manager.mark_success(effective_chat_id)
            answer = result.answer or "逸问返回成功，但当前响应内容为空。"
            return answer, [], None, effective_chat_id
        except SysuAnythingCliError as exc:
            shared_yiwen_manager.mark_failure(exc)
            return (
                f"SYSU-Anything 调用逸问失败：{exc}",
                [
                    ChatAction(
                        type="refresh_shared_yiwen",
                        system="yiwen",
                        needed=True,
                        message="共享逸问登录态不可用或已失效。请管理员通过 /admin/yiwen/shared/login 重新导入官方登录态。",
                    )
                ],
                None,
                upstream_chat_id,
            )
        except Exception as exc:
            shared_yiwen_manager.mark_failure(exc)
            return (
                f"公共逸问上游连接失败：{exc}",
                [
                    ChatAction(
                        type="retry",
                        system="yiwen",
                        needed=True,
                        message="后端调用 SYSU-Anything 失败，请检查本地服务日志。",
                    )
                ],
                None,
                upstream_chat_id,
            )

    @staticmethod
    def _private_action(private_result) -> ChatAction:
        action_type = private_result.next_action or ("login_required" if private_result.needs_relogin else "private_query")
        if private_result.needs_relogin:
            if private_result.system == "jwxt" and private_result.next_action in {"refresh_business_session", "refresh_jwxt_session"}:
                message = "已检测到个人企业微信/CAS 登录态，但 JWXT 教务业务会话未建立或已失效；请点击左侧“刷新教务会话”，如出现二维码则用企业微信扫码确认。"
            elif private_result.next_action == "refresh_business_session":
                message = "已检测到个人企业微信/CAS 登录态，但目标业务系统会话未建立或已失效；请刷新对应业务系统会话或重新扫码绑定。"
            elif private_result.system == "libic":
                message = "需要绑定该用户自己的中大账号，并建立 libic 图书馆空间会话后，才能查询个人预约。"
            else:
                message = "需要绑定该用户自己的中大账号会话后，才能查询个人事务。"
        else:
            if private_result.system in {"bus", "career", "qg"}:
                message = "官方功能查询已执行。"
            else:
                message = "私人事务连接器已识别请求，正在使用该用户自己的校园系统会话处理。"
        return ChatAction(
            type=action_type,
            system=private_result.system,
            needed=private_result.needs_relogin,
            message=message,
        )

    @staticmethod
    def _kb_context(hits: list[KbSearchHit]) -> str:
        if not hits:
            return ""
        lines = ["[内置辅助知识库命中]"]
        for index, hit in enumerate(hits, start=1):
            lines.append(f"{index}. {hit.document.title}: {hit.snippet}")
        return "\n".join(lines)

    @staticmethod
    def _kb_sources(hits: list[KbSearchHit]) -> list[AnswerSource]:
        return [
            AnswerSource(
                type="supplement_kb",
                title=hit.document.title,
                system="supplement_kb",
                detail=f"score={hit.score}; source={hit.document.source or 'local'}",
            )
            for hit in hits
        ]

    @staticmethod
    def _external_rag_context(hits: list[ExternalRagHit]) -> str:
        if not hits:
            return ""
        lines = ["[外部辅助知识库命中]"]
        for index, hit in enumerate(hits, start=1):
            page_text = f" p.{hit.page}" if hit.page is not None else ""
            lines.append(f"{index}. {hit.title}{page_text}: {hit.snippet}")
        return "\n".join(lines)

    @staticmethod
    def _external_rag_sources(hits: list[ExternalRagHit]) -> list[AnswerSource]:
        return [
            AnswerSource(
                type="external_rag",
                title=hit.title,
                system="external_rag",
                detail=f"score={hit.score}; doc_id={hit.doc_id or 'unknown'}; page={hit.page if hit.page is not None else 'unknown'}",
            )
            for hit in hits
        ]

    @staticmethod
    def _yiwen_source(payload: ChatRequest, result: YiwenResult | None) -> AnswerSource:
        detail_parts = [
            f"channel={CHANNEL_TITLES.get(payload.channel, payload.channel)}",
            f"searchSource={payload.search_source}",
            f"model={payload.model}",
        ]
        if result and result.raw:
            reference = result.raw.get("reference")
            if isinstance(reference, list):
                detail_parts.append(f"references={len(reference)}")
        return AnswerSource(
            type="yiwen",
            title=CHANNEL_TITLES.get(payload.channel, "逸问公共知识问答"),
            system="yiwen",
            detail="; ".join(detail_parts),
        )

    @staticmethod
    def _should_query_external_rag(payload: ChatRequest, route: str) -> bool:
        if route not in {"public", "hybrid"}:
            return False
        message = payload.message.strip()
        if any(token in message for token in EXTERNAL_RAG_BLOCK_KEYWORDS):
            return False
        if payload.channel == "model" and not any(token in message for token in EXTERNAL_RAG_TRIGGER_KEYWORDS):
            return False
        return any(token in message for token in EXTERNAL_RAG_TRIGGER_KEYWORDS)

    @staticmethod
    def _filter_external_hits(message: str, hits: list[ExternalRagHit]) -> list[ExternalRagHit]:
        if not hits:
            return []
        if any(token in message for token in EXTERNAL_RAG_TRIGGER_KEYWORDS):
            return hits
        message_tokens = {token for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", message.lower()) if len(token) >= 2}
        filtered = []
        for hit in hits:
            title_tokens = {token for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", hit.title.lower()) if len(token) >= 2}
            if message_tokens & title_tokens:
                filtered.append(hit)
        return filtered

    async def _collect_auxiliary_context(self, payload: ChatRequest, route: str) -> tuple[list[KbSearchHit], list[ExternalRagHit], str]:
        kb_hits = self.supplement_kb.search(payload.message, user_id=payload.user_id, limit=3)
        kb_context = self._kb_context(kb_hits)
        external_hits: list[ExternalRagHit] = []
        external_context = ""
        if self._should_query_external_rag(payload, route):
            try:
                raw_external_hits = await self.external_rag.search(payload.message, topk=3)
                external_hits = self._filter_external_hits(payload.message, raw_external_hits)
                external_context = self._external_rag_context(external_hits)
            except Exception:
                external_hits = []
                external_context = ""
        combined_context = "\n".join(part for part in [kb_context, external_context] if part)
        return kb_hits, external_hits, combined_context

    @staticmethod
    def _private_source_detail(result) -> str:
        raw = getattr(result, "raw", {}) or {}
        endpoints = raw.get("official_endpoints") if isinstance(raw, dict) else None
        verified = raw.get("official_source_verified") if isinstance(raw, dict) else None
        parts: list[str] = []
        if verified is True:
            parts.append("official_source_verified=true")
        elif verified is False:
            parts.append("official_source_verified=false")
        if isinstance(endpoints, list) and endpoints:
            parts.append("endpoints=" + " | ".join(str(item) for item in endpoints[:6]))
        if not parts and raw.get("command"):
            parts.append("command=" + " ".join(str(item) for item in raw.get("command", [])))
        return "; ".join(parts) if parts else "official_source_verified=unknown"
    async def handle(self, payload: ChatRequest) -> ChatResponse:
        if payload.channel == "private":
            route = "private"
        elif payload.channel == "auto":
            route = classify_message(payload.message)
        else:
            route = "public"

        fallback_chat_id = payload.chat_id or "local-session"
        if payload.channel == "freshman_materials":
            answer, hits, status = await freshman_materials_service.answer(payload.message)
            detail = f"repo={status.get('repo')}; cached={status.get('cached')}; hits={len(hits)}; updated_at={status.get('updated_at')}"
            return ChatResponse(
                chat_id=fallback_chat_id,
                route="public",
                answer=answer,
                sources=[AnswerSource(type="system", title="塔社新生资料包", system="freshman_materials", detail=detail)],
                actions=[],
            )

        kb_hits, external_hits, combined_context = await self._collect_auxiliary_context(payload, route)
        yiwen_payload = payload
        if payload.channel in CHANNEL_SEARCH_SOURCES:
            yiwen_payload = yiwen_payload.model_copy(update={"search_source": CHANNEL_SEARCH_SOURCES[payload.channel]})
        if combined_context and route in {"public", "hybrid"}:
            yiwen_payload = yiwen_payload.model_copy(update={
                "message": (
                    f"{payload.message}\n\n"
                    "以下是本助手检索到的外部/本地知识库材料，与当前栏目上游知识同等作为参考。"
                    "仅在材料和问题直接相关时使用；不要因为提供了材料就强行引用或追加原文。\n"
                    f"{combined_context}"
                )
            })

        if route == "public":
            official_result = await self.private.query_public_function(payload.user_id, payload.message)
            if official_result:
                return ChatResponse(
                    chat_id=fallback_chat_id,
                    route=route,
                    answer=official_result.answer,
                    sources=[
                        AnswerSource(
                            type="system",
                            title="官方功能查询",
                            system=official_result.system,
                        )
                    ],
                    actions=[self._private_action(official_result)],
                )

            answer, actions, yiwen_result, effective_chat_id = await self._query_yiwen(yiwen_payload)
            return ChatResponse(
                chat_id=effective_chat_id or fallback_chat_id,
                route=route,
                answer=answer,
                sources=[self._yiwen_source(yiwen_payload, yiwen_result)] + self._kb_sources(kb_hits) + self._external_rag_sources(external_hits),
                actions=actions,
            )

        if route == "private":
            private_result = await self.private.query_personal(payload.user_id, payload.message)
            return ChatResponse(
                chat_id=fallback_chat_id,
                route=route,
                answer=private_result.answer,
                sources=[
                    AnswerSource(
                        type="private_connector",
                        title="私人事务查询",
                        system=private_result.system,
                        detail=self._private_source_detail(private_result),
                    )
                ],
                actions=[self._private_action(private_result)],
            )

        public_answer, public_actions, yiwen_result, effective_chat_id = await self._query_yiwen(yiwen_payload)
        private_result = await self.private.query_personal(payload.user_id, payload.message)
        return ChatResponse(
            chat_id=effective_chat_id or fallback_chat_id,
            route=route,
            answer=(
                "公共知识与私人事务拆分回答：\n\n"
                f"[逸问公共知识]\n{public_answer}\n\n"
                f"[私人事务连接器]\n{private_result.answer}"
            ),
            sources=[
                self._yiwen_source(yiwen_payload, yiwen_result),
                *self._kb_sources(kb_hits),
                *self._external_rag_sources(external_hits),
                AnswerSource(
                    type="private_connector",
                    title="私人事务查询",
                    system=private_result.system,
                    detail=self._private_source_detail(private_result),
                ),
            ],
            actions=public_actions + [self._private_action(private_result)],
        )









