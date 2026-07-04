from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime
from dataclasses import dataclass
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


class FreshmanMaterialsService:
    owner = "thinktraveller"
    repo = "SYSU_freshman_materials"
    branch = "main"
    github_repo_url = f"https://github.com/{owner}/{repo}"
    api_tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"

    STOPWORDS = (
        "塔社", "新生", "资料包", "资料", "文件", "文档", "材料", "路径", "目录", "仓库",
        "在哪", "哪里", "位置", "有没有", "有吗", "找", "查询", "请问", "告诉我", "给我", "下载",
        "的", "一下", "相关", "具体", "github", "GitHub",
    )

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
            response = await client.get(self.api_tree_url, headers={"Accept": "application/vnd.github+json", "User-Agent": "yiwen-freshman-materials-indexer"})
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

    async def search(self, query: str, limit: int = 8) -> list[FreshmanMaterialHit]:
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
            ))
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]

    async def answer(self, query: str) -> tuple[str, list[FreshmanMaterialHit], dict[str, Any]]:
        hits = await self.search(query, limit=8)
        status = self.status()
        if not hits:
            answer = (
                "没有在塔社新生资料包索引中匹配到明确路径。\n"
                f"仓库：{self.github_repo_url}\n"
                "可以换一个更具体的文件名、课程名、关键词或资料类别再试。"
            )
            return answer, hits, status
        lines = ["在塔社新生资料包中找到以下可能路径：", ""]
        for index, hit in enumerate(hits, start=1):
            kind = "文件" if hit.type == "blob" else "目录"
            size = f"，{hit.size} bytes" if hit.size is not None else ""
            lines.append(f"{index}. [{kind}] {hit.path}{size}")
            lines.append(f"   GitHub：{hit.html_url}")
        lines.append("")
        lines.append(f"索引来源：{self.github_repo_url}")
        if status.get("updated_at"):
            updated_text = datetime.fromtimestamp(float(status["updated_at"])).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"索引更新时间：{updated_text}")
        return "\n".join(lines), hits, status


freshman_materials_service = FreshmanMaterialsService()



