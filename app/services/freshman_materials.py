from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


@dataclass
class FreshmanMaterialHit:
    path: str
    type: str
    size: int | None
    html_url: str
    raw_url: str | None
    score: float
    source: str = "github_freshman_materials"
    source_title: str = "塔社新生资料包"
    title: str | None = None
    detail: str | None = None
    kind: str | None = None


class FreshmanMaterialsService:
    owner = "thinktraveller"
    repo = "SYSU_freshman_materials"
    branch = "main"
    github_repo_url = f"https://github.com/{owner}/{repo}"
    api_tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
    arxiv_base_url = "https://arxiv.jaison.ink"
    arxiv_materials_url = f"{arxiv_base_url}/api/materials"
    arxiv_packages_url = f"{arxiv_base_url}/api/packages"

    STOPWORDS = (
        "塔社", "新生", "资料包", "中大", "真题", "资料", "查询", "文件", "文档", "材料", "路径", "目录", "仓库",
        "在哪", "哪里", "位置", "有没有", "有吗", "找", "查询", "请问", "告诉我", "给我", "下载",
        "的", "一下", "相关", "具体", "github", "GitHub",
    )

    ARXIV_CATEGORY_LABELS = {
        "past_exam": "历年真题",
        "study_material": "学习资料",
        "experience": "经验攻略",
        "package": "课程包",
    }

    def __init__(self, cache_path: Path | None = None) -> None:
        self.cache_path = cache_path or Path(".state") / "freshman_materials_tree.json"
        self._tree: list[dict[str, Any]] | None = None
        self._loaded_at: float | None = None
        self._last_error: str | None = None

    @staticmethod
    def _normalize_text(value: str) -> str:
        value = value.lower()
        value = re.sub(r"[\\/_.\-+()（）【】\[\]《》<>:：,，。;；!！?？'\"“”\s]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _clean_text(value: Any) -> str:
        text = "" if value is None else str(value)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text).replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _query_terms(cls, query: str) -> list[str]:
        cleaned = query
        for word in cls.STOPWORDS:
            cleaned = cleaned.replace(word, " ")
        normalized = cls._normalize_text(cleaned)
        terms: set[str] = {token for token in normalized.split() if len(token) >= 2}
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]+", cleaned))
        if len(chinese) >= 2:
            for size in range(2, min(7, len(chinese) + 1)):
                for index in range(0, len(chinese) - size + 1):
                    terms.add(chinese[index:index + size])
        ascii_terms = re.findall(r"[a-zA-Z0-9]{2,}", cleaned)
        terms.update(term.lower() for term in ascii_terms)
        return sorted(terms, key=len, reverse=True)

    @classmethod
    def _file_url(cls, path: str) -> str:
        return f"{cls.github_repo_url}/blob/{cls.branch}/{quote(path)}"

    @classmethod
    def _tree_url(cls, path: str) -> str:
        return f"{cls.github_repo_url}/tree/{cls.branch}/{quote(path)}"

    @classmethod
    def _raw_url(cls, path: str) -> str:
        return f"https://raw.githubusercontent.com/{cls.owner}/{cls.repo}/{cls.branch}/{quote(path)}"

    def _load_cache(self) -> list[dict[str, Any]]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            self._last_error = str(exc)
            return []
        tree = payload.get("tree") if isinstance(payload, dict) else None
        if not isinstance(tree, list):
            return []
        return [item for item in tree if isinstance(item, dict) and item.get("path")]

    def _save_cache(self, payload: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    async def refresh(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(
                self.api_tree_url,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "yiwen-sysu-materials-indexer"},
            )
        response.raise_for_status()
        payload = response.json()
        tree = payload.get("tree") if isinstance(payload, dict) else None
        if not isinstance(tree, list):
            raise RuntimeError("GitHub tree payload missing tree list")
        cache_payload = {
            "repo": f"{self.owner}/{self.repo}",
            "repo_url": self.github_repo_url,
            "api_url": self.api_tree_url,
            "sha": payload.get("sha"),
            "truncated": payload.get("truncated"),
            "updated_at": time.time(),
            "tree": tree,
        }
        self._save_cache(cache_payload)
        self._tree = [item for item in tree if isinstance(item, dict) and item.get("path")]
        self._loaded_at = time.time()
        self._last_error = None
        return self.status()

    async def refresh_all(self) -> dict[str, Any]:
        status = await self.refresh()
        try:
            await self._search_arxiv_api("高数", limit=2)
            status["arxiv_live_api"] = {"ok": True, "last_error": None}
        except Exception as exc:
            self._last_error = str(exc)
            status["arxiv_live_api"] = {"ok": False, "last_error": str(exc)}
        return status

    async def _ensure_tree(self) -> list[dict[str, Any]]:
        if self._tree is None:
            self._tree = self._load_cache()
            self._loaded_at = time.time() if self._tree else None
        if not self._tree:
            try:
                await self.refresh()
            except Exception as exc:
                self._last_error = str(exc)
        return self._tree or []

    def status(self) -> dict[str, Any]:
        tree = self._tree if self._tree is not None else self._load_cache()
        files = [item for item in tree if item.get("type") == "blob"]
        dirs = [item for item in tree if item.get("type") == "tree"]
        updated_at = None
        if self.cache_path.exists():
            try:
                payload = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
                updated_at = payload.get("updated_at") if isinstance(payload, dict) else None
            except (OSError, json.JSONDecodeError):
                updated_at = None
        return {
            "repo": f"{self.owner}/{self.repo}",
            "repo_url": self.github_repo_url,
            "api_url": self.api_tree_url,
            "arxiv": {
                "site": self.arxiv_base_url,
                "materials_api": self.arxiv_materials_url,
                "packages_api": self.arxiv_packages_url,
                "mode": "live_search",
            },
            "sources": [
                {"key": "github_freshman_materials", "title": "塔社新生资料包", "mode": "cached_github_tree"},
                {"key": "arxiv_jaison", "title": "破壁计划 arxiv.jaison.ink", "mode": "live_api_search"},
            ],
            "cache_path": str(self.cache_path),
            "cached": bool(tree),
            "items": len(tree),
            "files": len(files),
            "directories": len(dirs),
            "updated_at": updated_at,
            "loaded_at": self._loaded_at,
            "last_error": self._last_error,
        }

    @classmethod
    def _score_item(cls, item: dict[str, Any], query: str, terms: list[str]) -> float:
        path = str(item.get("path") or "")
        if not path:
            return 0.0
        normalized_path = cls._normalize_text(path)
        filename = path.rsplit("/", 1)[-1]
        normalized_filename = cls._normalize_text(filename)
        cleaned_query = cls._normalize_text(query)
        score = 0.0
        if cleaned_query and cleaned_query in normalized_path:
            score += 80.0
        if cleaned_query and cleaned_query in normalized_filename:
            score += 120.0
        path_parts = [cls._normalize_text(part) for part in path.split("/") if part]
        for term in terms:
            if term in normalized_filename:
                score += 16.0 + min(len(term), 12)
            if term in normalized_path:
                score += 6.0 + min(len(term), 10)
            if any(term == part or term in part for part in path_parts[:-1]):
                score += 3.0
        if item.get("type") == "blob":
            score += 1.5
        depth = path.count("/")
        score -= math.log(depth + 1, 2) * 0.3
        return score

    async def search_github(self, query: str, limit: int = 8) -> list[FreshmanMaterialHit]:
        tree = await self._ensure_tree()
        terms = self._query_terms(query)
        if not terms and query.strip():
            terms = [self._normalize_text(query.strip())]
        hits: list[FreshmanMaterialHit] = []
        for item in tree:
            score = self._score_item(item, query, terms)
            if score <= 0:
                continue
            path = str(item.get("path"))
            item_type = str(item.get("type") or "")
            html_url = self._file_url(path) if item_type == "blob" else self._tree_url(path)
            hits.append(FreshmanMaterialHit(
                path=path,
                type=item_type,
                size=item.get("size") if isinstance(item.get("size"), int) else None,
                html_url=html_url,
                raw_url=self._raw_url(path) if item_type == "blob" else None,
                score=round(score, 3),
                source="github_freshman_materials",
                source_title="塔社新生资料包",
                title=path.rsplit("/", 1)[-1],
                detail="GitHub 仓库文件树",
                kind="文件" if item_type == "blob" else "目录",
            ))
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]

    @classmethod
    def _score_arxiv_item(cls, item: dict[str, Any], query: str, terms: list[str], rank: int, item_type: str) -> float:
        fields = [
            cls._clean_text(item.get("title")),
            cls._clean_text(item.get("file_name")),
            cls._clean_text(item.get("course_name")),
            cls._clean_text(item.get("description")),
            cls._clean_text(item.get("department")),
            cls._clean_text(item.get("instructor")),
            cls._clean_text(item.get("year")),
            cls._clean_text(item.get("category")),
        ]
        searchable = cls._normalize_text(" ".join(part for part in fields if part))
        cleaned_query = cls._normalize_text(query)
        score = max(0.0, 40.0 - rank * 2.0)
        if cleaned_query and cleaned_query in searchable:
            score += 80.0
        for term in terms:
            if term and term in searchable:
                score += 12.0 + min(len(term), 10)
        if item_type == "package":
            score += 2.0
        return score

    @classmethod
    def _arxiv_hit_from_item(cls, item: dict[str, Any], item_type: str, query: str, terms: list[str], rank: int) -> FreshmanMaterialHit | None:
        item_id = item.get("id")
        if item_id is None:
            return None
        title = cls._clean_text(item.get("title")) or cls._clean_text(item.get("course_name")) or f"{item_type} {item_id}"
        file_name = cls._clean_text(item.get("file_name"))
        file_path = cls._clean_text(item.get("file_path"))
        course_name = cls._clean_text(item.get("course_name"))
        description = cls._clean_text(item.get("description"))
        category = cls._clean_text(item.get("category"))
        category_label = cls.ARXIV_CATEGORY_LABELS.get(category, "课程包" if item_type == "package" else category)
        url_kind = "package" if item_type == "package" else "material"
        html_url = f"{cls.arxiv_base_url}/{url_kind}/{item_id}"
        if file_path and not file_path.startswith("/") and "home/ubuntu" not in file_path:
            path = file_path
        else:
            path = file_name or title or html_url
        detail_parts = [part for part in [category_label, course_name, file_name, description] if part]
        score = cls._score_arxiv_item(item, query, terms, rank, item_type)
        if score <= 0:
            return None
        return FreshmanMaterialHit(
            path=path,
            type=item_type,
            size=item.get("file_size") if isinstance(item.get("file_size"), int) else None,
            html_url=html_url,
            raw_url=None,
            score=round(score, 3),
            source="arxiv_jaison",
            source_title="破壁计划 arxiv.jaison.ink",
            title=title,
            detail=" | ".join(detail_parts),
            kind="课程包" if item_type == "package" else category_label or "资料",
        )

    async def _search_arxiv_api(self, query: str, limit: int = 8) -> list[FreshmanMaterialHit]:
        terms = self._query_terms(query)
        params = {"search": query.strip(), "page_size": max(limit, 5)}
        headers = {"Accept": "application/json", "User-Agent": "yiwen-sysu-materials-indexer"}
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
            material_response = await client.get(self.arxiv_materials_url, params=params)
            package_response = await client.get(self.arxiv_packages_url, params=params)
        material_response.raise_for_status()
        package_response.raise_for_status()
        payloads = [
            ("material", material_response.json()),
            ("package", package_response.json()),
        ]
        hits: list[FreshmanMaterialHit] = []
        for item_type, payload in payloads:
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                continue
            for rank, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                hit = self._arxiv_hit_from_item(item, item_type, query, terms, rank)
                if hit:
                    hits.append(hit)
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]

    @staticmethod
    def _merge_balanced_hits(github_hits: list[FreshmanMaterialHit], arxiv_hits: list[FreshmanMaterialHit], limit: int) -> list[FreshmanMaterialHit]:
        if limit <= 0:
            return []
        if not github_hits:
            return arxiv_hits[:limit]
        if not arxiv_hits:
            return github_hits[:limit]

        arxiv_quota = min(len(arxiv_hits), max(1, limit // 2))
        github_quota = min(len(github_hits), limit - arxiv_quota)
        remaining = limit - arxiv_quota - github_quota
        if remaining > 0:
            extra_arxiv = min(len(arxiv_hits) - arxiv_quota, remaining)
            arxiv_quota += extra_arxiv
            remaining -= extra_arxiv
        if remaining > 0:
            github_quota += min(len(github_hits) - github_quota, remaining)

        merged: list[FreshmanMaterialHit] = []
        for index in range(max(github_quota, arxiv_quota)):
            if index < arxiv_quota:
                merged.append(arxiv_hits[index])
            if index < github_quota:
                merged.append(github_hits[index])
        return merged[:limit]

    async def search(self, query: str, limit: int = 8) -> list[FreshmanMaterialHit]:
        search_limit = max(1, min(limit, 60))
        github_hits = await self.search_github(query, limit=max(search_limit, 8))
        try:
            arxiv_hits = await self._search_arxiv_api(query, limit=max(search_limit, 8))
        except Exception as exc:
            self._last_error = f"arxiv search failed: {exc}"
            arxiv_hits = []
        return self._merge_balanced_hits(github_hits, arxiv_hits, search_limit)

    def _format_hit_lines(self, hits: list[FreshmanMaterialHit], start_index: int = 1) -> list[str]:
        lines: list[str] = []
        for offset, hit in enumerate(hits):
            index = start_index + offset
            kind = hit.kind or ("文件" if hit.type == "blob" else "目录")
            size = f"，{hit.size} bytes" if hit.size is not None else ""
            title = f"{hit.title} - " if hit.title and hit.title != hit.path else ""
            lines.append(f"{index}. [{hit.source_title} | {kind}] {title}{hit.path}{size}")
            if hit.detail:
                lines.append(f"   说明：{hit.detail}")
            lines.append(f"   链接：{hit.html_url}")
        return lines

    def _build_answer_pages(self, hits: list[FreshmanMaterialHit], status: dict[str, Any], page_size: int = 6) -> list[str]:
        if not hits:
            return []
        total_pages = math.ceil(len(hits) / page_size)
        pages: list[str] = []
        for page_index in range(total_pages):
            start = page_index * page_size
            page_hits = hits[start:start + page_size]
            lines = [f"中大真题资料查询结果（第 {page_index + 1}/{total_pages} 页，共 {len(hits)} 条）：", ""]
            lines.extend(self._format_hit_lines(page_hits, start_index=start + 1))
            if page_index == total_pages - 1:
                lines.append("")
                lines.append(f"索引来源：{self.github_repo_url}；{self.arxiv_base_url}")
                if status.get("updated_at"):
                    updated_text = datetime.fromtimestamp(float(status["updated_at"])).strftime("%Y-%m-%d %H:%M:%S")
                    lines.append(f"GitHub 缓存更新时间：{updated_text}")
                lines.append("说明：该栏目只做资料路径/入口发现，不下载资料，也不总结资料内容。")
            pages.append("\n".join(lines))
        return pages

    async def answer(self, query: str) -> tuple[str, list[FreshmanMaterialHit], dict[str, Any]]:
        hits = await self.search(query, limit=24)
        status = self.status()
        if not hits:
            answer = (
                "没有在中大真题资料查询中匹配到明确路径。\n"
                f"已检索来源：{self.github_repo_url}；{self.arxiv_base_url}\n"
                "可以换一个更具体的课程名、文件名、老师名、年份或资料类别再试。"
            )
            status["answer_pages"] = [answer]
            return answer, hits, status
        pages = self._build_answer_pages(hits, status, page_size=6)
        status["answer_pages"] = pages
        return "\n\n".join(pages), hits, status


freshman_materials_service = FreshmanMaterialsService()



