from dataclasses import dataclass, field
from threading import RLock
from typing import Any
import re
import secrets
import time


@dataclass
class KbDocument:
    doc_id: str
    title: str
    content: str
    owner_user_id: str | None = None
    source: str | None = None
    tags: list[str] = field(default_factory=list)
    visibility: str = "public"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_payload(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "content": self.content,
            "owner_user_id": self.owner_user_id,
            "source": self.source,
            "tags": list(self.tags),
            "visibility": self.visibility,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class KbSearchHit:
    document: KbDocument
    score: float
    snippet: str

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.document.to_payload(),
            "score": self.score,
            "snippet": self.snippet,
        }


class SupplementKbService:
    def __init__(self) -> None:
        self._lock = RLock()
        self._documents: dict[str, KbDocument] = {}

    @staticmethod
    def _tokens(text: str) -> list[str]:
        lowered = text.lower()
        ascii_tokens = re.findall(r"[a-z0-9_\-]{2,}", lowered)
        cjk_tokens = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
        short_cjk: list[str] = []
        for token in cjk_tokens:
            short_cjk.extend(token[i : i + 2] for i in range(max(0, len(token) - 1)))
        return list(dict.fromkeys(ascii_tokens + cjk_tokens + short_cjk))

    @staticmethod
    def _visible_to(document: KbDocument, user_id: str | None) -> bool:
        if document.visibility == "public":
            return True
        return bool(user_id and document.owner_user_id == user_id)

    @staticmethod
    def _snippet(content: str, query: str, width: int = 180) -> str:
        if len(content) <= width:
            return content
        query = query.strip()
        index = content.find(query) if query else -1
        if index < 0:
            index = 0
        start = max(0, index - 40)
        end = min(len(content), start + width)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(content) else ""
        return prefix + content[start:end] + suffix

    def add_document(
        self,
        *,
        title: str,
        content: str,
        owner_user_id: str | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        visibility: str = "public",
    ) -> KbDocument:
        clean_title = title.strip()
        clean_content = content.strip()
        if not clean_title:
            raise ValueError("title cannot be empty")
        if not clean_content:
            raise ValueError("content cannot be empty")
        if visibility not in {"public", "private"}:
            raise ValueError("visibility must be public or private")

        now = time.time()
        doc = KbDocument(
            doc_id=secrets.token_urlsafe(10),
            title=clean_title,
            content=clean_content,
            owner_user_id=owner_user_id if visibility == "private" else None,
            source=source,
            tags=tags or [],
            visibility=visibility,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._documents[doc.doc_id] = doc
        return doc

    def list_documents(self, *, user_id: str | None = None) -> list[KbDocument]:
        with self._lock:
            docs = [doc for doc in self._documents.values() if self._visible_to(doc, user_id)]
        return sorted(docs, key=lambda item: item.created_at, reverse=True)

    def search(self, query: str, *, user_id: str | None = None, limit: int = 5) -> list[KbSearchHit]:
        tokens = self._tokens(query)
        raw_query = query.strip().lower()
        hits: list[KbSearchHit] = []
        with self._lock:
            candidates = [doc for doc in self._documents.values() if self._visible_to(doc, user_id)]

        for doc in candidates:
            haystack = " ".join([doc.title, doc.content, " ".join(doc.tags), doc.source or ""]).lower()
            score = 0.0
            if raw_query and raw_query in haystack:
                score += 8.0
            for token in tokens:
                if token in haystack:
                    score += 1.0
                    if token in doc.title.lower():
                        score += 1.5
            if score > 0:
                hits.append(KbSearchHit(document=doc, score=score, snippet=self._snippet(doc.content, query)))

        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]
