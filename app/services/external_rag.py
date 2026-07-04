from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class ExternalRagHit:
    title: str
    snippet: str
    score: float
    doc_id: str | None = None
    page: int | None = None
    pdf_url: str | None = None
    viewer_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class ExternalRagService:
    def __init__(self) -> None:
        self.base_url = os.getenv("EXTERNAL_RAG_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        self.enabled = os.getenv("EXTERNAL_RAG_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
        self.timeout_seconds = float(os.getenv("EXTERNAL_RAG_TIMEOUT_SECONDS", "8"))
        self.default_topk = int(os.getenv("EXTERNAL_RAG_TOPK", "5"))

    def status_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "search_endpoint": f"{self.base_url}/api/external/search",
            "ask_endpoint": f"{self.base_url}/api/external/ask",
            "kb_endpoint": f"{self.base_url}/api/external/kb",
        }

    async def check(self) -> dict[str, Any]:
        payload = self.status_payload()
        if not self.enabled:
            return {**payload, "ok": False, "message": "external RAG disabled"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(f"{self.base_url}/api/external/kb")
            payload["status_code"] = response.status_code
            payload["ok"] = response.is_success
            if response.is_success:
                data = response.json()
                if isinstance(data, dict):
                    docs = data.get("docs") or data.get("documents") or data.get("items") or data.get("kb")
                    if isinstance(docs, list):
                        payload["document_count"] = len(docs)
            else:
                payload["message"] = response.text[:300]
        except Exception as exc:
            payload["ok"] = False
            payload["message"] = str(exc)
        return payload

    async def search(self, query: str, *, topk: int | None = None) -> list[ExternalRagHit]:
        if not self.enabled:
            return []
        body = {
            "q": query,
            "topk": topk or self.default_topk,
            "include_vector": False,
            "max_text_chars": 1200,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.base_url}/api/external/search", json=body)
        response.raise_for_status()
        data = response.json()
        raw_hits = None
        if isinstance(data, dict):
            raw_hits = data.get("hits") or data.get("items") or data.get("results")
        else:
            raw_hits = data
        if not isinstance(raw_hits, list):
            return []
        hits: list[ExternalRagHit] = []
        for item in raw_hits:
            if not isinstance(item, dict):
                continue
            text = str(item.get("snippet") or item.get("text") or "").strip()
            if not text:
                continue
            doc_id = str(item.get("doc_id") or item.get("docId") or "").strip() or None
            title = str(item.get("title") or item.get("report_title") or item.get("filename") or doc_id or "外部知识库文档")
            page_value = item.get("page")
            try:
                page = int(page_value) if page_value is not None else None
            except (TypeError, ValueError):
                page = None
            score_value = item.get("score") or item.get("distance") or 0
            try:
                score = float(score_value)
            except (TypeError, ValueError):
                score = 0.0
            hits.append(ExternalRagHit(
                title=title,
                snippet=text,
                score=score,
                doc_id=doc_id,
                page=page,
                pdf_url=item.get("pdf_url"),
                viewer_url=item.get("viewer_url"),
                raw=item,
            ))
        return hits


external_rag_service = ExternalRagService()
