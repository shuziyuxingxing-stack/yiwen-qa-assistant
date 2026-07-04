from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.services.private_sysu_auth import PrivateSysuAuthError, private_sysu_auth


@dataclass
class PrivateQueryResult:
    answer: str
    system: str
    needs_relogin: bool = False
    intent: str = "unknown"
    next_action: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PrivateSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], dict[str, Any]] = {}

    def get(self, user_id: str, system: str) -> dict[str, Any] | None:
        return self._sessions.get((user_id, system))

    def upsert(self, user_id: str, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._sessions[(user_id, system)] = payload
        return payload

    def status(self, user_id: str, system: str) -> dict[str, Any]:
        session = self.get(user_id, system)
        return {
            "user_id": user_id,
            "system": system,
            "has_session": bool(session),
            "session_keys": sorted(session.keys()) if session else [],
            "source": session.get("source") if session else None,
            "updated_at": session.get("updated_at") if session else None,
        }


class LibicConnector:
    system = "libic"
    base_url = "https://libic.sysu.edu.cn"

    RESERVATION_TOPIC_TOKENS = (
        "libic",
        "图书馆",
        "自习室",
        "研讨室",
        "学习空间",
        "空间预约",
        "study room",
        "seminar room",
        "reservation",
        "booking",
    )
    PERSONAL_SCOPE_TOKENS = (
        "我",
        "我的",
        "本人",
        "自己",
        "个人",
        "查我",
        "查询我",
        "预约记录",
        "我的预约",
        "进度",
        "状态",
        "结果",
        "记录",
    )

    MY_RESERVATION_CANDIDATES = (
        ("GET", "/api/reserve/my"),
        ("GET", "/api/reserve/list"),
        ("GET", "/api/reservation/my"),
        ("GET", "/api/reservation/list"),
        ("GET", "/api/appointment/my"),
        ("GET", "/api/appointment/list"),
        ("GET", "/api/user/reserve/list"),
        ("GET", "/api/user/reservation/list"),
        ("GET", "/api/space/reserve/my"),
        ("GET", "/api/space/order/list"),
        ("POST", "/api/reserve/list"),
        ("POST", "/api/reservation/list"),
        ("POST", "/api/appointment/list"),
    )

    TOKEN_KEYS = ("token", "access_token", "accessToken", "authorization", "Authorization", "bearer_token")
    COOKIE_KEYS = ("cookie", "cookies", "cookie_header", "Cookie")

    def __init__(self, sessions: PrivateSessionStore) -> None:
        self.sessions = sessions

    @staticmethod
    def matches(question: str) -> bool:
        lowered = question.lower()
        has_topic = any(token in question or token in lowered for token in LibicConnector.RESERVATION_TOPIC_TOKENS)
        has_personal_scope = any(token in question or token in lowered for token in LibicConnector.PERSONAL_SCOPE_TOKENS)
        return has_topic and has_personal_scope

    @staticmethod
    def default_state_path(user_id: str | None = None) -> Path:
        if user_id:
            return private_sysu_auth.user_state_dir(user_id) / "libic-session.json"
        return Path.home() / ".sysu-anything" / "libic-session.json"

    def import_sysu_anything_session(self, user_id: str) -> dict[str, Any] | None:
        path = self.default_state_path(user_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        payload = {
            "source": "sysu-anything",
            "source_path": str(path),
            "updated_at": time.time(),
            "raw": raw,
        }
        return self.sessions.upsert(user_id, self.system, payload)

    @classmethod
    def _find_first(cls, payload: Any, keys: tuple[str, ...]) -> Any:
        if isinstance(payload, dict):
            for key in keys:
                if payload.get(key):
                    return payload[key]
            for value in payload.values():
                found = cls._find_first(value, keys)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = cls._find_first(value, keys)
                if found:
                    return found
        return None

    @classmethod
    def _extract_auth(cls, session: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
        raw = session.get("raw", session)
        token = cls._find_first(raw, cls.TOKEN_KEYS)
        cookie_value = cls._find_first(raw, cls.COOKIE_KEYS)
        headers = {"Accept": "application/json, text/plain, */*", "Referer": cls.base_url + "/"}
        cookies: dict[str, str] = {}

        if isinstance(token, str) and token.strip():
            normalized = token.strip()
            if not normalized.lower().startswith("bearer "):
                normalized = "Bearer " + normalized
            headers["Authorization"] = normalized

        if isinstance(cookie_value, dict):
            cookies = {str(k): str(v) for k, v in cookie_value.items() if v is not None}
        elif isinstance(cookie_value, str) and cookie_value.strip():
            headers["Cookie"] = cookie_value.strip().removeprefix("Cookie:").strip()

        return headers, cookies

    @staticmethod
    def _looks_like_reservation_payload(payload: Any) -> bool:
        text = json.dumps(payload, ensure_ascii=False).lower()[:4000]
        return any(token in text for token in ("reserve", "reservation", "appointment", "booking", "room", "lab", "预约", "房间", "研讨室"))

    async def _probe_my_reservations(self, session: dict[str, Any]) -> dict[str, Any]:
        headers, cookies = self._extract_auth(session)
        attempts: list[dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=8.0, follow_redirects=True) as client:
            for method, path in self.MY_RESERVATION_CANDIDATES:
                try:
                    if method == "POST":
                        response = await client.post(path, headers=headers, cookies=cookies, json={"current": 1, "size": 20})
                    else:
                        response = await client.get(path, headers=headers, cookies=cookies, params={"current": 1, "size": 20})
                    content_type = response.headers.get("content-type", "")
                    item: dict[str, Any] = {"method": method, "path": path, "status_code": response.status_code, "content_type": content_type}
                    if response.status_code in {401, 403}:
                        item["auth_failed"] = True
                    if "json" in content_type:
                        payload = response.json()
                        item["json_preview"] = payload
                        if response.is_success and self._looks_like_reservation_payload(payload):
                            return {"matched": True, "request": {"method": method, "path": path}, "payload": payload, "attempts": attempts + [item]}
                    else:
                        item["text_preview"] = response.text[:300]
                    attempts.append(item)
                except Exception as exc:
                    attempts.append({"method": method, "path": path, "error": str(exc)})
        return {"matched": False, "attempts": attempts}

    async def query(self, user_id: str, question: str) -> PrivateQueryResult:
        session = self.sessions.get(user_id, self.system) or self.import_sysu_anything_session(user_id)
        if not session and private_sysu_auth.has_cas_session(user_id):
            try:
                await private_sysu_auth.run_json_for_user(user_id, "libic", "refresh", timeout=90.0)
                session = self.import_sysu_anything_session(user_id)
            except PrivateSysuAuthError:
                session = None
        if not session:
            return PrivateQueryResult(
                answer=(
                    "这是图书馆空间/研讨室预约类私人事务。公共逸问共享账号不能查询个人预约；"
                    "必须由该用户登录自己的中大账号后，后端复用 CAS -> libic /auth/address -> /authcenter -> /auth/token "
                    "建立个人 libic 会话，再调用网页中的个人预约接口。"
                ),
                system=self.system,
                needs_relogin=True,
                intent="libic_reservation_query",
                next_action="bind_libic_account",
                raw={
                    "entry_url": self.base_url + "/",
                    "session_file_probe": str(self.default_state_path(user_id)),
                    "login_chain": [
                        "SYSU CAS login",
                        "GET https://libic.sysu.edu.cn/auth/address",
                        "follow authcenter redirect",
                        "exchange /auth/token",
                        "call the discovered personal reservation-list request",
                    ],
                },
            )

        probe = await self._probe_my_reservations(session)
        if probe.get("matched"):
            return PrivateQueryResult(
                answer="已使用该用户的 libic 会话命中个人预约接口，下面是上游返回的预约数据。",
                system=self.system,
                needs_relogin=False,
                intent="libic_reservation_query",
                next_action="none",
                raw=probe,
            )

        auth_failed = any(item.get("auth_failed") for item in probe.get("attempts", []))
        if auth_failed:
            return PrivateQueryResult(
                answer="已找到本地 libic 会话，但上游返回未授权。需要用户重新登录中大账号并刷新 libic 会话。",
                system=self.system,
                needs_relogin=True,
                intent="libic_reservation_query",
                next_action="refresh_libic_session",
                raw=probe,
            )

        return PrivateQueryResult(
            answer=(
                "已找到该用户的 libic 会话，但当前候选接口没有命中“我的预约”数据。"
                "下一步需要在登录后的 https://libic.sysu.edu.cn/ 页面打开 Network，点击“我的预约/预约记录”，"
                "把真实请求路径加入 LibicConnector.MY_RESERVATION_CANDIDATES。"
            ),
            system=self.system,
            needs_relogin=False,
            intent="libic_reservation_query",
            next_action="discover_libic_my_reservations_api",
            raw=probe,
        )

class SysuAnythingPrivateConnector:
    system = "sysu-anything"
    JWXT_BASE_URL = "https://jwxt.sysu.edu.cn/jwxt"

    GYM_VENUE_HINTS = (
        ("健身房", "健身房"),
        ("羽毛球", "羽毛球"),
        ("网球", "网球"),
        ("篮球", "篮球"),
        ("乒乓", "乒乓球"),
        ("游泳", "游泳"),
        ("足球", "足球"),
        ("排球", "排球"),
    )

    def __init__(self) -> None:
        self._pending_leave_apply: dict[str, list[str]] = {}

    @staticmethod
    def _shanghai_today(offset_days: int = 0) -> str:
        tz = timezone(timedelta(hours=8))
        return (datetime.now(tz) + timedelta(days=offset_days)).date().isoformat()

    @classmethod
    def _extract_date(cls, question: str, default_offset_days: int = 0) -> str:
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", question)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        if "明天" in question:
            return cls._shanghai_today(1)
        if "后天" in question:
            return cls._shanghai_today(2)
        return cls._shanghai_today(default_offset_days)

    @classmethod
    def _extract_leave_dates(cls, question: str) -> tuple[str | None, str | None]:
        explicit = re.findall(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", question)
        if explicit:
            dates = [f"{int(y):04d}-{int(m):02d}-{int(d):02d}" for y, m, d in explicit]
            return dates[0], dates[1] if len(dates) > 1 else dates[0]
        month_days = re.findall(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号]?", question)
        if month_days:
            year = datetime.now(timezone(timedelta(hours=8))).year
            dates = [f"{year:04d}-{int(m):02d}-{int(d):02d}" for m, d in month_days]
            return dates[0], dates[1] if len(dates) > 1 else dates[0]
        if "明天" in question and "后天" in question:
            return cls._shanghai_today(1), cls._shanghai_today(2)
        if "今天" in question or "今日" in question:
            return cls._shanghai_today(0), cls._shanghai_today(0)
        if "明天" in question:
            return cls._shanghai_today(1), cls._shanghai_today(1)
        if "后天" in question:
            return cls._shanghai_today(2), cls._shanghai_today(2)
        return None, None

    @staticmethod
    def _normalize_leave_part(value: str | None) -> str | None:
        if not value:
            return None
        lowered = value.lower()
        if lowered in {"am", "上午", "早上", "上午半天"}:
            return "上午"
        if lowered in {"pm", "下午", "下午半天"}:
            return "下午"
        return None

    @classmethod
    def _extract_leave_parts(cls, question: str) -> tuple[str | None, str | None]:
        if any(token in question for token in ("全天", "整天", "一天")):
            return "上午", "下午"
        tokens = re.findall(r"上午|下午|am|pm", question, flags=re.IGNORECASE)
        parts = [cls._normalize_leave_part(token) for token in tokens]
        parts = [part for part in parts if part]
        if len(parts) >= 2:
            return parts[0], parts[1]
        if len(parts) == 1 and "半天" in question:
            return parts[0], parts[0]
        return None, None

    @staticmethod
    def _extract_leave_reason(question: str) -> str | None:
        for reason in ("病假", "事假", "丧假"):
            if reason in question:
                return reason
        if "因病" in question or "看病" in question or "发烧" in question or "生病" in question:
            return "病假"
        match = re.search(r"请假事由\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9_-]{1,12})", question)
        return match.group(1) if match else None

    @staticmethod
    def _extract_leave_explanation(question: str) -> str | None:
        match = re.search(r"(?:原因说明|请假说明|说明|因为|由于|理由是|原因是)\s*[:：]?\s*(.+?)(?:\s*(?:附件|文件|凭证|证明)\s*[:：]?|$)", question)
        if match:
            text = match.group(1).strip(" ，,；;。")
            if text and text not in {"事假", "病假", "丧假"}:
                return text
        return None

    @staticmethod
    def _extract_leave_attachment(question: str) -> tuple[str | None, str | None, str | None]:
        remote_path = re.search(r"(?:file-path|远端路径)\s*[:：]?\s*([^\s，,；;]+)", question, flags=re.IGNORECASE)
        remote_name = re.search(r"(?:file-name|远端文件名)\s*[:：]?\s*([^\s，,；;]+)", question, flags=re.IGNORECASE)
        if remote_path and remote_name:
            return None, remote_path.group(1), remote_name.group(1)
        quoted = re.search(r"(?:附件|文件|凭证|证明)\s*[:：]?\s*[\"']([^\"']+)[\"']", question)
        if quoted:
            return quoted.group(1), None, None
        local = re.search(r"(?:附件|文件|凭证|证明)\s*[:：]?\s*([A-Za-z]:\\[^\s，,；;]+)", question)
        if local:
            return local.group(1), None, None
        return None, None, None

    @staticmethod
    def _command_option(command: list[str], option: str) -> str | None:
        try:
            index = command.index(option)
        except ValueError:
            return None
        if index + 1 >= len(command):
            return None
        return command[index + 1]

    @classmethod
    def _summarize_leave_apply_command(cls, command: list[str], *, submitted: bool) -> str:
        lines = ["已按用户明确确认提交 JWXT 请假申请。" if submitted else "已生成 JWXT 请假申请官方预览，尚未提交。"]
        lines.append("")
        lines.append("申请字段：")
        for label, option in (
            ("请假原因", "--reason"),
            ("开始日期", "--start-date"),
            ("开始半天", "--start-part"),
            ("结束日期", "--end-date"),
            ("结束半天", "--end-part"),
            ("原因说明", "--explanation"),
        ):
            value = cls._command_option(command, option)
            if value:
                lines.append(f"- {label}：{value}")
        attachment = cls._command_option(command, "--attachment")
        remote_path = cls._command_option(command, "--file-path")
        remote_name = cls._command_option(command, "--file-name")
        if attachment:
            lines.append(f"- 附件：{attachment}")
        elif remote_path or remote_name:
            lines.append(f"- 已上传附件：{remote_name or ''} {remote_path or ''}".strip())
        if not submitted:
            lines.append("")
            lines.append("确认内容无误后回复“确认提交请假申请”；没有这句话，系统不会提交。")
        return "\n".join(lines)

    def _build_leave_apply_command(self, question: str) -> tuple[list[str] | None, list[str]]:
        missing: list[str] = []
        reason = self._extract_leave_reason(question)
        start_date, end_date = self._extract_leave_dates(question)
        start_part, end_part = self._extract_leave_parts(question)
        explanation = self._extract_leave_explanation(question)
        attachment, remote_path, remote_name = self._extract_leave_attachment(question)

        if not reason:
            missing.append("请假原因：事假/病假/丧假")
        if not start_date:
            missing.append("请假开始日期")
        if not end_date:
            missing.append("请假结束日期")
        if not start_part:
            missing.append("开始半天：上午/下午；全天可说“全天/一天”")
        if not end_part:
            missing.append("结束半天：上午/下午；全天可说“全天/一天”")
        if not explanation:
            missing.append("请假原因说明")
        if not attachment and not (remote_path and remote_name):
            missing.append("附件路径，或已上传附件的 file-path/file-name")
        if missing:
            return None, missing

        command = [
            "jwxt", "leave", "apply",
            "--reason", reason or "",
            "--start-date", start_date or "",
            "--start-part", start_part or "",
            "--end-date", end_date or "",
            "--end-part", end_part or "",
            "--explanation", explanation or "",
        ]
        if attachment:
            command.extend(["--attachment", attachment])
            name = Path(attachment).name
            if name and not name.isascii():
                command.extend(["--attachment-name", "leave-proof" + Path(name).suffix])
        else:
            command.extend(["--file-path", remote_path or "", "--file-name", remote_name or ""])
        return command, []

    @staticmethod
    def _is_leave_apply_intent(question: str) -> bool:
        return any(token in question for token in ("申请请假", "请假申请", "我要请假", "帮我请假", "提交请假", "预览请假", "发起请假"))

    @staticmethod
    def _is_leave_confirm_intent(question: str) -> bool:
        return any(token in question for token in ("确认提交请假", "确认提交", "确认申请", "正式提交请假"))

    @staticmethod
    def _extract_id(question: str) -> str | None:
        match = re.search(r"(?:id|ID|编号|单号|详情|报名|投递|预约)\s*[:：#]?\s*([A-Za-z0-9_-]{6,64})", question)
        if match:
            return match.group(1)
        match = re.search(r"\b([A-Za-z0-9_-]{8,64})\b", question)
        if match:
            return match.group(1)
        match = re.search(r"(\d{5,})", question)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_month(question: str) -> str | None:
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})\s*月?", question)
        if match:
            year, month = match.groups()
            return f"{int(year):04d}-{int(month):02d}"
        return None

    @staticmethod
    def _extract_time_range(question: str) -> tuple[str, str] | None:
        match = re.search(r"(\d{1,2})(?::|点|：)(\d{2})?\s*(?:-|到|至|~|—)\s*(\d{1,2})(?::|点|：)(\d{2})?", question)
        if match:
            start_hour, start_minute, end_hour, end_minute = match.groups()
            return f"{int(start_hour):02d}:{int(start_minute or 0):02d}", f"{int(end_hour):02d}:{int(end_minute or 0):02d}"
        return None

    @staticmethod
    def _extract_section_range(question: str) -> tuple[str, str] | None:
        match = re.search(r"(?:第)?\s*(\d{1,2})\s*(?:-|到|至|~)\s*(\d{1,2})\s*节", question)
        if match:
            return match.group(1), match.group(2)
        match = re.search(r"第\s*(\d{1,2})\s*节", question)
        if match:
            return match.group(1), match.group(1)
        return None

    @staticmethod
    def _infer_campus(question: str) -> str | None:
        aliases = (
            ("广州校区南校园", "广州校区南校园"),
            ("南校园", "广州校区南校园"),
            ("南校", "广州校区南校园"),
            ("广州校区北校园", "广州校区北校园"),
            ("北校园", "广州校区北校园"),
            ("北校", "广州校区北校园"),
            ("广州校区东校园", "广州校区东校园"),
            ("东校园", "广州校区东校园"),
            ("东校", "广州校区东校园"),
            ("珠海校区", "珠海校区"),
            ("珠海", "珠海校区"),
            ("深圳校区", "深圳校区"),
            ("深圳", "深圳校区"),
        )
        for alias, campus in aliases:
            if alias in question:
                return campus
        return None

    @staticmethod
    def _extract_limit(question: str, default: str = "10") -> str:
        match = re.search(r"(\d{1,2})\s*(?:条|个|场|项)", question)
        if not match:
            return default
        return str(min(max(int(match.group(1)), 1), 30))

    @staticmethod
    def _infer_bus_query(question: str) -> str:
        aliases = (
            ("南校园", "南校园"),
            ("南校", "南校园"),
            ("北校园", "北校园"),
            ("北校", "北校园"),
            ("东校园", "东校园"),
            ("东校", "东校园"),
            ("珠海校区", "珠海"),
            ("深圳校区", "深圳"),
            ("广州校区", "广州"),
            ("珠海", "珠海"),
            ("深圳", "深圳"),
        )
        tokens = []
        for alias, canonical in aliases:
            if alias in question and canonical not in tokens:
                tokens.append(canonical)
        return " ".join(tokens) if tokens else ""
    @staticmethod
    def _infer_career_keyword(question: str) -> str | None:
        match = re.search(r"(?:关于|有关|搜索|查询)\s*([^，。！？\s]{2,20})", question)
        if match:
            return match.group(1).strip()
        for token in ("腾讯", "阿里", "华为", "字节", "银行", "证券", "律师", "医院", "学校", "研究所"):
            if token in question:
                return token
        return None

    @staticmethod
    def _infer_libic_kind(question: str) -> str | None:
        for token in ("研讨室", "自习室", "学习空间", "珠海", "广州", "深圳"):
            if token in question:
                return token
        quoted = re.search(r"[\"'“”《》](.+?)[\"'“”《》]", question)
        if quoted:
            return quoted.group(1).strip()
        return None
    @staticmethod
    def _contains_any(question: str, tokens: tuple[str, ...] | list[str]) -> bool:
        lowered = question.lower()
        return any(token in question or token in lowered for token in tokens)

    @staticmethod
    def _extract_days(question: str, default: str = "14") -> str:
        if "今天" in question or "今日" in question:
            return "1"
        if "明天" in question:
            return "2"
        match = re.search(r"(\d{1,2})\s*天", question)
        if match:
            return str(min(max(int(match.group(1)), 1), 30))
        return default

    @classmethod
    def _infer_gym_venue_type(cls, question: str) -> str | None:
        for token, venue_type in cls.GYM_VENUE_HINTS:
            if token in question:
                return venue_type
        quoted = re.search(r"[\"'“”《》](.+?)[\"'“”《》]", question)
        if quoted:
            return quoted.group(1).strip()
        return None

    @staticmethod
    def _safe_json_text(value: Any, *, max_chars: int = 1200) -> str:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "\n..."

    @classmethod
    def _find_interesting_list(cls, payload: Any) -> list[Any] | None:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return None
        preferred_keys = (
            "records",
            "rows",
            "list",
            "items",
            "data",
            "result",
            "results",
            "courses",
            "bookings",
            "tasks",
            "sessions",
            "routes",
            "schoolBusShuttleMomentList",
        )
        for key in preferred_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = cls._find_interesting_list(value)
                if nested is not None:
                    return nested
        for value in payload.values():
            nested = cls._find_interesting_list(value)
            if nested is not None:
                return nested
        return None

    @classmethod
    def _brief_payload(cls, payload: Any, title: str) -> str:
        if isinstance(payload, dict):
            routes = payload.get("result", {}).get("routes") if isinstance(payload.get("result"), dict) else None
            if isinstance(routes, list):
                if not routes:
                    return f"{title}\n\n没有查到相关班车。"
                lines = [title, ""]
                for index, route in enumerate(routes[:6], start=1):
                    moments = route.get("schoolBusShuttleMomentList") if isinstance(route, dict) else None
                    times = "、".join(str(item.get("time")) for item in moments[:8] if isinstance(item, dict) and item.get("time")) if isinstance(moments, list) else ""
                    direction = route.get("drivingDirectionName") or f"{route.get('startStation', '')} -> {route.get('endStation', '')}"
                    line = f"{index}. {direction}"
                    if times:
                        line += f"：{times}"
                    if route.get("startStation") or route.get("endStation"):
                        line += f"；{route.get('startStation', '')} -> {route.get('endStation', '')}"
                    lines.append(line)
                if len(routes) > 6:
                    lines.append(f"其余 {len(routes) - 6} 条已省略。")
                return "\n".join(lines)

        interesting = cls._find_interesting_list(payload)
        if interesting is None:
            return f"{title}\n\n{cls._safe_json_text(payload)}"
        if not interesting:
            return f"{title}\n\n没有查到相关记录。"

        if all(isinstance(item, dict) for item in interesting):
            lines = [title, ""]
            for index, item in enumerate(interesting[:8], start=1):
                name = (
                    item.get("title")
                    or item.get("name")
                    or item.get("course_name")
                    or item.get("courseName")
                    or item.get("roomName")
                    or item.get("venueName")
                    or item.get("drivingDirectionName")
                    or item.get("id")
                    or item.get("cid")
                    or "未命名"
                )
                details = []
                for key in (
                    "timeText",
                    "timeRange",
                    "startTime",
                    "endTime",
                    "deadline",
                    "location",
                    "campus",
                    "campusName",
                    "venueName",
                    "roomName",
                    "classroom_name",
                    "companyName",
                    "salaryText",
                    "jobType",
                    "education",
                    "status",
                    "state",
                    "auditStatus",
                    "url",
                ):
                    value = item.get(key)
                    if value:
                        details.append(str(value))
                line = f"{index}. {name}"
                if details:
                    line += " | " + " | ".join(details[:5])
                lines.append(line)
            if len(interesting) > 8:
                lines.append(f"其余 {len(interesting) - 8} 条已省略。")
            return "\n".join(lines)

        preview = interesting[:5]
        return (
            f"{title}\n\n"
            f"共解析到 {len(interesting)} 条相关记录，先显示前 {len(preview)} 条：\n"
            f"{cls._safe_json_text(preview)}"
        )

    @staticmethod
    def _cookie_header_from_tough_cookie(session_file: Path, host: str, path: str) -> str:
        payload = json.loads(session_file.read_text(encoding="utf-8"))
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if not isinstance(cookies, list):
            return ""
        now = time.time()
        pairs: list[str] = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            key = str(cookie.get("key") or "").strip()
            value = cookie.get("value")
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            cookie_path = str(cookie.get("path") or "/")
            expires = cookie.get("expires")
            if not key or value is None:
                continue
            if domain and host.lower() != domain and not host.lower().endswith("." + domain):
                continue
            if cookie_path and not path.startswith(cookie_path.rstrip("/") or "/"):
                continue
            if isinstance(expires, str) and expires.lower() != "infinity":
                try:
                    if datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp() < now:
                        continue
                except ValueError:
                    pass
            pairs.append(f"{key}={value}")
        return "; ".join(pairs)

    async def _request_jwxt_json(
        self,
        client: httpx.AsyncClient,
        session_file: Path,
        pathname: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
        menu_id: str = "jwxsd_xscjcx",
        referer: str | None = None,
    ) -> dict[str, Any]:
        path = "/jwxt" + (pathname if pathname.startswith("/") else "/" + pathname)
        cookie_header = self._cookie_header_from_tough_cookie(session_file, "jwxt.sysu.edu.cn", path)
        if not cookie_header:
            raise PrivateSysuAuthError("JWXT session cookie jar is empty")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie_header,
            "Referer": referer or f"{self.JWXT_BASE_URL}/#/student",
            "X-Requested-With": "XMLHttpRequest",
            "menuId": menu_id,
            "lastAccessTime": str(int(time.time() * 1000)),
        }
        method_upper = method.upper()
        if method_upper == "POST":
            headers["Content-Type"] = "application/json;charset=UTF-8"
            response = await client.post(pathname, params=params or {}, json=json_body or {}, headers=headers)
        else:
            response = await client.get(pathname, params=params or {}, headers=headers)
        content_type = response.headers.get("content-type", "")
        item: dict[str, Any] = {
            "method": method_upper,
            "path": pathname,
            "params": params or {},
            "status_code": response.status_code,
            "content_type": content_type,
        }
        if json_body is not None:
            item["json_body"] = json_body
        if response.status_code in {401, 403}:
            item["auth_failed"] = True
        if "json" not in content_type:
            item["text_preview"] = response.text[:300]
            return item
        try:
            payload = response.json()
        except ValueError:
            item["text_preview"] = response.text[:300]
            return item
        item["payload"] = payload
        return item

    @classmethod
    def _payload_success(cls, item: dict[str, Any] | None) -> bool:
        if not item or not (200 <= int(item.get("status_code") or 0) < 300):
            return False
        payload = item.get("payload")
        if payload is None:
            return False
        if isinstance(payload, dict):
            code = payload.get("code")
            if code is None:
                return True
            return str(code) in {"0", "1", "200", "success", "SUCCESS"}
        return True

    @staticmethod
    def _payload_data(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    @classmethod
    def _normalize_pull_options(cls, payload: Any, key: str) -> list[dict[str, Any]]:
        data = cls._payload_data(payload)
        if isinstance(data, dict):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _grade_option_label(item: dict[str, Any]) -> str:
        return str(
            item.get("dataName")
            or item.get("name")
            or item.get("label")
            or item.get("termName")
            or item.get("dataNumber")
            or item.get("termNumber")
            or ""
        )

    @staticmethod
    def _grade_option_value(item: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = item.get(key)
            if value not in {None, ""}:
                return str(value)
        return ""

    @classmethod
    def _select_grade_filters(cls, question: str, pull_payload: Any, add_score_flag: Any) -> dict[str, Any]:
        train_options = cls._normalize_pull_options(pull_payload, "selectTrainType")
        year_options = cls._normalize_pull_options(pull_payload, "selectYearPull")
        term_options = cls._normalize_pull_options(pull_payload, "selectTermPull")

        train_type = "01"
        train_label = "主修"
        if train_options:
            train_type = cls._grade_option_value(train_options[0], "dataNumber", "value", "code") or train_type
            train_label = cls._grade_option_label(train_options[0]) or train_label
            for item in train_options:
                label = cls._grade_option_label(item)
                value = cls._grade_option_value(item, "dataNumber", "value", "code")
                if (label and label in question) or (value and value in question):
                    train_type = value or train_type
                    train_label = label or train_label
                    break
        elif "辅修" in question:
            train_label = "辅修"

        school_year = ""
        year_match = re.search(r"(20\d{2})\s*[-/—~至到]\s*(20\d{2})", question)
        if year_match:
            school_year = f"{year_match.group(1)}-{year_match.group(2)}"
        else:
            single_year = re.search(r"(20\d{2})\s*学年", question)
            if single_year:
                start_year = int(single_year.group(1))
                school_year = f"{start_year}-{start_year + 1}"
        if not school_year and year_options:
            school_year = cls._grade_option_value(year_options[0], "dataNumber", "value", "schoolYear", "name")

        school_semester = ""
        if any(token in question for token in ("第一学期", "上学期", "秋季", "秋学期")) or re.search(r"(?<!\d)1\s*学期", question):
            school_semester = "1"
        elif any(token in question for token in ("第二学期", "下学期", "春季", "春学期")) or re.search(r"(?<!\d)2\s*学期", question):
            school_semester = "2"
        elif any(token in question for token in ("第三学期", "小学期", "夏季", "夏学期")) or re.search(r"(?<!\d)3\s*学期", question):
            school_semester = "3"
        if not school_semester and term_options:
            school_semester = cls._grade_option_value(term_options[0], "termNumber", "dataNumber", "value") or "1"
        if not school_semester:
            school_semester = "1"
        semester_labels = {"1": "第一学期", "2": "第二学期", "3": "第三学期"}
        term_label = next(
            (cls._grade_option_label(item) for item in term_options if cls._grade_option_value(item, "termNumber", "dataNumber", "value") == school_semester),
            semester_labels.get(school_semester, school_semester),
        )
        if term_label in semester_labels:
            term_label = semester_labels[term_label]

        return {
            "params": {
                "scoSchoolYear": school_year,
                "trainTypeCode": train_type,
                "addScoreFlag": add_score_flag if add_score_flag not in {None, ""} else "",
                "scoSemester": school_semester,
            },
            "selected": {
                "training_category": train_label,
                "trainTypeCode": train_type,
                "school_year": school_year,
                "semester": term_label,
                "scoSemester": school_semester,
            },
            "available": {
                "training_categories": train_options,
                "school_years": year_options,
                "semesters": term_options,
            },
        }

    @classmethod
    def _extract_grade_records(cls, payload: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if any(value.get(key) for key in ("scoCourseName", "scoFinalScore", "originalScore", "scoCredit", "scoPoint")):
                    key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                    if key not in seen:
                        seen.add(key)
                        records.append(value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return records

    @classmethod
    def _summarize_grade_payload(
        cls,
        score_item: dict[str, Any] | None,
        rank_item: dict[str, Any] | None,
        credit_item: dict[str, Any] | None,
        pie_item: dict[str, Any] | None,
        selected: dict[str, Any],
    ) -> str:
        lines = ["已通过该用户自己的 JWXT 登录态调用官方“我的成绩”只读接口。"]
        filter_text = "；".join(
            str(value)
            for value in (
                selected.get("training_category"),
                selected.get("school_year"),
                selected.get("semester"),
            )
            if value not in {None, ""}
        )
        if filter_text:
            lines.append(f"查询条件：{filter_text}")
        if score_item and cls._payload_success(score_item):
            payload = score_item.get("payload")
            records = cls._extract_grade_records(payload)
            if records:
                lines.append("")
                lines.append(f"成绩记录：共解析到 {len(records)} 条，先显示前 {min(len(records), 8)} 条。")
                for index, item in enumerate(records[:8], start=1):
                    name = item.get("scoCourseName") or item.get("courseName") or item.get("course_name") or item.get("kcmc") or "未命名课程"
                    year = item.get("scoSchoolYear")
                    semester = item.get("scoSemester")
                    credit = item.get("scoCredit")
                    final_score = item.get("scoFinalScore")
                    original_score = item.get("originalScore")
                    point = item.get("scoPoint")
                    reason = item.get("specialReason")
                    rank = item.get("teachClassRank")
                    detail = " | ".join(
                        str(value)
                        for value in (
                            f"{year} 第{semester}学期" if year and semester else None,
                            f"学分 {credit}" if credit not in {None, ""} else None,
                            f"成绩 {final_score or original_score}" if (final_score or original_score) not in {None, ""} else None,
                            f"绩点 {point}" if point not in {None, ""} else None,
                            f"排名 {rank}" if rank not in {None, ""} else None,
                            reason,
                        )
                        if value not in {None, ""}
                    )
                    lines.append(f"{index}. {name}" + (f" | {detail}" if detail else ""))
                if len(records) > 8:
                    lines.append(f"其余 {len(records) - 8} 条已省略。")
            else:
                lines.append("")
                lines.append("成绩列表接口已返回成功，但在当前查询条件下没有解析到个人成绩记录。")
        else:
            lines.append("")
            lines.append("成绩列表接口未成功返回可解析数据；没有生成成绩。")
        if rank_item and cls._payload_success(rank_item):
            lines.append("")
            lines.append("官方学年排名接口已返回成功。")
        if credit_item and cls._payload_success(credit_item):
            lines.append("学分/绩点汇总接口已返回成功。")
        if pie_item and cls._payload_success(pie_item):
            lines.append("成绩分布接口已返回成功。")
        return "\n".join(lines)


    @classmethod
    def _extract_exam_records(cls, payload: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if any(value.get(key) for key in ("examSubjectName", "examDate", "classroomNumber", "durationTime")):
                    key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                    if key not in seen:
                        seen.add(key)
                        records.append(value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return records

    @classmethod
    def _summarize_exam_payload(cls, exam_items: list[dict[str, Any]], selected_weeks: list[dict[str, Any]]) -> str:
        lines = ["已通过该用户自己的 JWXT 登录态调用官方考试信息接口。"]
        all_records: list[dict[str, Any]] = []
        for item in exam_items:
            if cls._payload_success(item):
                all_records.extend(cls._extract_exam_records(item.get("payload")))
        if not all_records:
            week_names = [str(item.get("examWeekName") or item.get("name") or item.get("label")) for item in selected_weeks if isinstance(item, dict)]
            suffix = "，" + "、".join(week_names) if week_names else ""
            lines.append(f"JWXT 官方考试信息接口已响应{suffix}，但当前条件下没有返回个人考试安排。")
            return "\n".join(lines)
        lines.append("")
        lines.append(f"考试安排：共解析到 {len(all_records)} 条，先显示前 {min(len(all_records), 10)} 条。")
        for index, item in enumerate(all_records[:10], start=1):
            subject = item.get("examSubjectName") or item.get("courseName") or item.get("course") or item.get("name") or "未命名考试"
            date = item.get("examDate") or item.get("date")
            time_text = item.get("durationTime") or item.get("examTime") or item.get("startTime")
            room = item.get("classroomNumber") or item.get("classNo") or item.get("classroom")
            stage = item.get("examStage")
            mode = item.get("examMode")
            detail = " | ".join(str(value) for value in (date, time_text, room, stage, mode) if value not in {None, ""})
            lines.append(f"{index}. {subject}" + (f" | {detail}" if detail else ""))
        if len(all_records) > 10:
            lines.append(f"其余 {len(all_records) - 10} 条已省略。")
        return "\n".join(lines)

    @classmethod
    def _extract_exam_date(cls, question: str) -> str | None:
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", question)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号]", question)
        if match:
            month, day = match.groups()
            return f"{datetime.now(timezone(timedelta(hours=8))).year:04d}-{int(month):02d}-{int(day):02d}"
        if "今天" in question or "今日" in question:
            return cls._shanghai_today(0)
        if "明天" in question:
            return cls._shanghai_today(1)
        if "后天" in question:
            return cls._shanghai_today(2)
        return None

    @staticmethod
    def _exam_week_name(item: dict[str, Any]) -> str:
        return str(item.get("examWeekName") or item.get("name") or item.get("label") or "")

    @classmethod
    def _select_exam_weeks(cls, question: str, weeks: list[Any]) -> list[dict[str, Any]]:
        week_dicts = [item for item in weeks if isinstance(item, dict)]
        if not week_dicts:
            return []

        def name(item: dict[str, Any]) -> str:
            return cls._exam_week_name(item)

        makeup_tokens = ("缓补", "补考", "缓考", "自行添加", "重修")
        asks_makeup = any(token in question for token in makeup_tokens)
        wants_all = any(token in question for token in ("全部", "所有", "完整"))
        normal_weeks = [item for item in week_dicts if not any(token in name(item) for token in makeup_tokens)]
        makeup_weeks = [item for item in week_dicts if any(token in name(item) for token in makeup_tokens)]

        preferred: list[str] = []
        if "期中" in question:
            preferred.extend(["期中", "中考"])
        if any(token in question for token in ("期末", "末考")):
            preferred.extend(["期末", "末"])
        if asks_makeup:
            preferred.extend(list(makeup_tokens))

        for token in preferred:
            matched = [item for item in week_dicts if token in name(item)]
            if matched:
                return matched[:8]

        if wants_all:
            return week_dicts[:12]
        if asks_makeup:
            return makeup_weeks[:8] or week_dicts[:8]
        # 官网页面必须选考试周；用户未指定时，遍历候选周并把缓补考/自行添加放到最后，避免误选第一个空周。
        ordered = normal_weeks + makeup_weeks
        return ordered[:12]

    async def _query_jwxt_exam(self, user_id: str, question: str) -> PrivateQueryResult:
        command = ["jwxt", "exam-direct"]
        if not private_sysu_auth.has_cas_session(user_id):
            return self._login_required("jwxt", "jwxt_exam", command)
        _, state_dir, using_fallback = private_sysu_auth.effective_user_state_dir(user_id)
        session_file = state_dir / "jwxt-session.json"
        if not session_file.exists():
            return PrivateQueryResult(
                answer="已完成企业微信/CAS 登录，但尚未生成 JWXT 业务会话，不能查询考试信息。请先刷新教务业务会话。",
                system="jwxt",
                needs_relogin=True,
                intent="jwxt_exam",
                next_action="refresh_jwxt_session",
                raw={"session_file_exists": False, "using_single_user_fallback": using_fallback},
            )

        attempts: list[dict[str, Any]] = []
        exam_items: list[dict[str, Any]] = []
        selected_weeks: list[dict[str, Any]] = []
        exam_referer = f"{self.JWXT_BASE_URL}/mk/#/stuExamInfo?code=jwxsd_ksxxck&resourceName=%E8%80%83%E8%AF%95%E4%BF%A1%E6%81%AF%E6%9F%A5%E7%9C%8B"
        async with httpx.AsyncClient(base_url=self.JWXT_BASE_URL, timeout=15.0, follow_redirects=True) as client:
            current = await self._request_jwxt_json(client, session_file, "/base-info/acadyearterm/showNewAcadlist", menu_id="jwxsd_ksxxck", referer=exam_referer)
            attempts.append(current)
            current_data = current.get("payload", {}).get("data") if isinstance(current.get("payload"), dict) else None
            acad_year = None
            if isinstance(current_data, dict):
                acad_year = current_data.get("acadYearSemester") or current_data.get("academicYearSemester") or current_data.get("academicYear")
            if not acad_year:
                acad_year = datetime.now().strftime("%Y-%Y")

            weeks_item = await self._request_jwxt_json(client, session_file, "/schedule/agg/commonScheduleExamTime/queryExamWeekName", {"yearTerm": acad_year}, menu_id="jwxsd_ksxxck", referer=exam_referer)
            attempts.append(weeks_item)
            weeks = self._find_interesting_list(weeks_item.get("payload")) or []
            selected_weeks = self._select_exam_weeks(question, weeks)

            for week in selected_weeks:
                week_id = week.get("examWeekId") or week.get("id") or week.get("value")
                week_name = week.get("examWeekName") or week.get("name") or week.get("label")
                if not week_id:
                    continue
                body = {"acadYear": acad_year, "examWeekId": week_id, "examWeekName": week_name or ""}
                exam_date = self._extract_exam_date(question)
                if exam_date:
                    body["examDate"] = exam_date
                item = await self._request_jwxt_json(client, session_file, "/examination-manage/classroomResource/queryStuEaxmInfo", {"code": "jwxsd_ksxxck"}, method="POST", json_body=body, menu_id="jwxsd_ksxxck", referer=exam_referer)
                attempts.append(item)
                exam_items.append(item)

        if any(item.get("auth_failed") for item in attempts):
            return PrivateQueryResult(
                answer="JWXT 考试信息接口返回 401/403，说明该用户的教务业务会话未建立或已失效；没有生成任何考试安排。请刷新教务业务会话或重新扫码绑定。",
                system="jwxt",
                needs_relogin=True,
                intent="jwxt_exam",
                next_action="refresh_business_session",
                raw={
                    "command": command,
                    "official_source_verified": False,
                    "official_page": exam_referer,
                    "official_endpoints": [
                        "GET /base-info/acadyearterm/showNewAcadlist",
                        "GET /schedule/agg/commonScheduleExamTime/queryExamWeekName",
                        "POST /examination-manage/classroomResource/queryStuEaxmInfo",
                    ],
                    "using_single_user_fallback": using_fallback,
                    "selected_weeks": selected_weeks,
                    "exam_date": self._extract_exam_date(question),
                    "attempts": attempts,
                },
            )

        success = any(self._payload_success(item) for item in exam_items)
        if not selected_weeks:
            answer = "已识别为 JWXT 考试信息查询，但官方考试周接口没有返回可用考试周，未生成任何考试安排。"
        elif not success:
            answer = "已识别为 JWXT 考试信息查询，但尚未取得官方考试信息接口的成功响应，未生成任何考试安排。"
        else:
            answer = self._summarize_exam_payload(exam_items, selected_weeks)
        return PrivateQueryResult(
            answer=answer,
            system="jwxt",
            needs_relogin=False,
            intent="jwxt_exam",
            next_action="none" if success else "discover_jwxt_exam_params",
            raw={
                "command": command,
                "official_source_verified": bool(success),
                "official_page": exam_referer,
                "official_endpoints": [
                    "GET /base-info/acadyearterm/showNewAcadlist",
                    "GET /schedule/agg/commonScheduleExamTime/queryExamWeekName",
                    "POST /examination-manage/classroomResource/queryStuEaxmInfo",
                ],
                "using_single_user_fallback": using_fallback,
                "selected_weeks": selected_weeks,
                "exam_date": self._extract_exam_date(question),
                "attempts": attempts,
            },
        )

    async def _query_jwxt_grades(self, user_id: str, question: str) -> PrivateQueryResult:
        command = ["jwxt", "grade-direct"]
        if not private_sysu_auth.has_cas_session(user_id):
            return self._login_required("jwxt", "jwxt_grade", command)
        _, state_dir, using_fallback = private_sysu_auth.effective_user_state_dir(user_id)
        session_file = state_dir / "jwxt-session.json"
        if not session_file.exists():
            return PrivateQueryResult(
                answer="已完成企业微信/CAS 登录，但尚未生成 JWXT 业务会话，不能查询成绩。请先刷新教务业务会话。",
                system="jwxt",
                needs_relogin=True,
                intent="jwxt_grade",
                next_action="refresh_jwxt_session",
                raw={"session_file_exists": False, "using_single_user_fallback": using_fallback},
            )

        attempts: list[dict[str, Any]] = []
        score_item: dict[str, Any] | None = None
        rank_item: dict[str, Any] | None = None
        credit_item: dict[str, Any] | None = None
        pie_item: dict[str, Any] | None = None
        filter_info: dict[str, Any] = {"params": {}, "selected": {}, "available": {}}
        grade_referer = f"{self.JWXT_BASE_URL}/mk/studentWeb/#/stuAchievementView?code=jwxsd_wdcj&resourceName=%E6%88%91%E7%9A%84%E6%88%90%E7%BB%A9"
        async with httpx.AsyncClient(base_url=self.JWXT_BASE_URL, timeout=15.0, follow_redirects=True) as client:
            status_item = await self._request_jwxt_json(
                client,
                session_file,
                "/achievement-manage/score-check/checkStuStatus",
                menu_id="jwxsd_wdcj",
                referer=grade_referer,
            )
            attempts.append(status_item)
            status_payload = status_item.get("payload") if isinstance(status_item.get("payload"), dict) else {}
            status_data = status_payload.get("data") if isinstance(status_payload, dict) else {}
            add_score_flag = status_data.get("addScoreFlag") if isinstance(status_data, dict) else ""

            pull_item = await self._request_jwxt_json(
                client,
                session_file,
                "/achievement-manage/score-check/getPull",
                menu_id="jwxsd_wdcj",
                referer=grade_referer,
            )
            attempts.append(pull_item)
            filter_info = self._select_grade_filters(question, pull_item.get("payload"), add_score_flag)
            params = filter_info["params"]

            score_item = await self._request_jwxt_json(
                client,
                session_file,
                "/achievement-manage/score-check/list",
                params,
                menu_id="jwxsd_wdcj",
                referer=grade_referer,
            )
            attempts.append(score_item)

            rank_item = await self._request_jwxt_json(
                client,
                session_file,
                "/achievement-manage/score-check/getSortByYear",
                params,
                menu_id="jwxsd_wdcj",
                referer=grade_referer,
            )
            attempts.append(rank_item)

            credit_item = await self._request_jwxt_json(
                client,
                session_file,
                "/achievement-manage/score-check/stuCreditSitlist",
                menu_id="jwxsd_wdcj",
                referer=grade_referer,
            )
            attempts.append(credit_item)

            pie_item = await self._request_jwxt_json(
                client,
                session_file,
                "/achievement-manage/score-check/getPicPie",
                menu_id="jwxsd_wdcj",
                referer=grade_referer,
            )
            attempts.append(pie_item)

        if any(item.get("auth_failed") for item in attempts):
            return PrivateQueryResult(
                answer="JWXT 成绩接口返回 401/403，说明该用户的教务业务会话未建立或已失效；没有生成任何成绩。请刷新教务业务会话或重新扫码绑定。",
                system="jwxt",
                needs_relogin=True,
                intent="jwxt_grade",
                next_action="refresh_business_session",
                raw={
                    "command": command,
                    "official_source_verified": False,
                    "official_page": grade_referer,
                    "official_endpoints": [
                        "GET /achievement-manage/score-check/checkStuStatus",
                        "GET /achievement-manage/score-check/getPull",
                        "GET /achievement-manage/score-check/list",
                        "GET /achievement-manage/score-check/getSortByYear",
                        "GET /achievement-manage/score-check/stuCreditSitlist",
                        "GET /achievement-manage/score-check/getPicPie",
                    ],
                    "selected_filters": filter_info.get("selected", {}),
                    "using_single_user_fallback": using_fallback,
                    "attempts": attempts,
                },
            )

        success = self._payload_success(score_item)
        answer = self._summarize_grade_payload(
            score_item,
            rank_item,
            credit_item,
            pie_item,
            filter_info.get("selected", {}),
        )
        return PrivateQueryResult(
            answer=answer,
            system="jwxt",
            needs_relogin=False,
            intent="jwxt_grade",
            next_action="none" if success else "discover_jwxt_grade_params",
            raw={
                "command": command,
                "official_source_verified": bool(success),
                "official_page": grade_referer,
                "official_endpoints": [
                    "GET /achievement-manage/score-check/checkStuStatus",
                    "GET /achievement-manage/score-check/getPull",
                    "GET /achievement-manage/score-check/list",
                    "GET /achievement-manage/score-check/getSortByYear",
                    "GET /achievement-manage/score-check/stuCreditSitlist",
                    "GET /achievement-manage/score-check/getPicPie",
                ],
                "selected_filters": filter_info.get("selected", {}),
                "available_filter_counts": {
                    key: len(value) if isinstance(value, list) else 0
                    for key, value in filter_info.get("available", {}).items()
                },
                "using_single_user_fallback": using_fallback,
                "attempts": attempts,
            },
        )
    @staticmethod
    def _login_required(system: str, intent: str, command: list[str], error: str | None = None, *, has_cas_session: bool = False) -> PrivateQueryResult:
        detail = f" 上游返回：{error}" if error else ""
        if system == "ykt":
            answer = (
                "这个问题需要使用用户自己的雨课堂网页登录态。雨课堂不复用中大 CAS；"
                "需要先完成雨课堂微信扫码登录后才能查询课程、作业或签到。"
                f"{detail}"
            )
            next_action = "bind_ykt_account"
        else:
            if has_cas_session:
                answer = (
                    "已检测到该用户有个人企业微信/CAS 登录态，但目标业务系统没有接受该会话，"
                    "需要重新刷新对应业务系统会话或重新扫码绑定。这个错误不是共享逸问账号问题，也不是普通用户需要手工找 token/cookie。"
                    f"{detail}"
                )
                next_action = "refresh_business_session"
            else:
                answer = (
                    "这个问题需要使用用户自己的中大账号登录态。请先在私人事务栏点击“企业微信扫码绑定”，"
                    "扫码成功后再重试。后端会把该用户的登录态保存在独立 state-dir 中，不会和共享逸问账号混用。"
                    f"{detail}"
                )
                next_action = "bind_sysu_account"
        return PrivateQueryResult(
            answer=answer,
            system=system,
            needs_relogin=True,
            intent=intent,
            next_action=next_action,
            raw={"command": command, "error": error},
        )

    async def _query_jwxt_leave_apply(self, user_id: str, question: str) -> PrivateQueryResult:
        command: list[str] | None = None
        is_confirm = self._is_leave_confirm_intent(question)
        if is_confirm:
            pending = self._pending_leave_apply.get(user_id)
            if not pending:
                return PrivateQueryResult(
                    answer="当前没有待确认的请假申请预览。请先说清请假开始/结束日期、上午/下午、事由、说明和附件路径，系统会先生成预览，不会直接提交。",
                    system="jwxt",
                    needs_relogin=False,
                    intent="jwxt_leave_apply_confirm_need_preview",
                    next_action="need_leave_apply_preview",
                    raw={"command": []},
                )
            command = [*pending, "--confirm"]
        else:
            command, missing = self._build_leave_apply_command(question)
            if missing:
                return PrivateQueryResult(
                    answer="请假申请需要补充以下信息后才能生成官方预览：\n" + "\n".join(f"- {item}" for item in missing),
                    system="jwxt",
                    needs_relogin=False,
                    intent="jwxt_leave_apply_need_detail",
                    next_action="need_more_detail",
                    raw={"missing": missing},
                )

        has_cas_session = private_sysu_auth.has_cas_session(user_id)
        if not has_cas_session:
            return self._login_required("jwxt", "jwxt_leave_apply", command or [])
        try:
            payload = await private_sysu_auth.run_json_for_user(user_id, *(command or []), timeout=120.0)
        except PrivateSysuAuthError as exc:
            text = str(exc)
            relogin_tokens = ("认证", "登录", "unauthorized", "unauthenticated", "401", "403", "CAS", "cookie", "token")
            if any(token.lower() in text.lower() for token in relogin_tokens):
                return self._login_required("jwxt", "jwxt_leave_apply", command or [], text, has_cas_session=has_cas_session)
            return PrivateQueryResult(
                answer=f"已调用 JWXT 请假申请官方流程，但上游返回错误：{text}",
                system="jwxt",
                needs_relogin=False,
                intent="jwxt_leave_apply_error",
                next_action="retry_or_fix_leave_apply_fields",
                raw={"command": command or [], "error": text},
            )

        if is_confirm:
            self._pending_leave_apply.pop(user_id, None)
            answer = self._summarize_leave_apply_command(command or [], submitted=True)
            next_action = "none"
            intent = "jwxt_leave_apply_submit"
        else:
            self._pending_leave_apply[user_id] = command or []
            answer = self._summarize_leave_apply_command(command or [], submitted=False)
            next_action = "confirm_leave_apply"
            intent = "jwxt_leave_apply_preview"
        return PrivateQueryResult(
            answer=answer,
            system="jwxt",
            needs_relogin=False,
            intent=intent,
            next_action=next_action,
            raw={
                "command": command or [],
                "payload": payload,
                "official_source_verified": True,
                "official_page": f"{self.JWXT_BASE_URL}/mk/#/studentAskLeaveInfo?code=jwxsd_qjsq",
                "official_basis": "sysu-anything jwxt leave apply; preview by default, --confirm only after explicit user confirmation",
                "submitted": is_confirm,
            },
        )

    async def _run(self, user_id: str, system: str, intent: str, title: str, command: list[str], timeout: float = 120.0, auth_required: bool = True) -> PrivateQueryResult:
        has_cas_session = private_sysu_auth.has_cas_session(user_id) if auth_required else False
        if auth_required and not has_cas_session:
            return self._login_required(system, intent, command)
        try:
            payload = await private_sysu_auth.run_json_for_user(user_id, *command, timeout=timeout)
        except PrivateSysuAuthError as exc:
            text = str(exc)
            relogin_tokens = ("认证", "登录", "unauthorized", "unauthenticated", "401", "403", "CAS", "cookie", "token")
            if any(token.lower() in text.lower() for token in relogin_tokens):
                return self._login_required(system, intent, command, text, has_cas_session=has_cas_session)
            return PrivateQueryResult(
                answer=f"已调用 {system} 官方功能命令，但上游返回错误：{text}",
                system=system,
                needs_relogin=False,
                intent=intent,
                next_action="retry_or_discover_api",
                raw={"command": command, "error": text},
            )
        return PrivateQueryResult(
            answer=self._brief_payload(payload, title),
            system=system,
            needs_relogin=False,
            intent=intent,
            next_action="none",
            raw={"command": command, "payload": payload},
        )

    def match(self, question: str) -> tuple[str, str, list[str], str] | None:
        if self._contains_any(question, ("校巴", "班车", "校车", "校园巴士", "穿梭巴士")):
            command = ["__NO_AUTH__", "bus"]
            bus_query = self._infer_bus_query(question)
            if bus_query:
                command.extend(["--query", bus_query])
            if self._contains_any(question, ("未来", "接下来", "之后", "还有", "未发车")):
                command.append("--upcoming")
            if self._contains_any(question, ("工作日",)):
                command.extend(["--bus", "1"])
            if self._contains_any(question, ("节假日", "周末", "假日")):
                command.extend(["--bus", "0"])
            return ("bus", "bus_schedule", command, "已查询校区班车时刻。")

        if self._contains_any(question, ("岐关", "岐关车", "珠海到广州", "广州到珠海", "珠海广州")):
            command = ["__NO_AUTH__", "qg", "list", "--available"]
            if "明天" in question:
                command.append("--tomorrow")
            elif "今天" in question or "今日" in question:
                command.append("--today")
            return ("qg", "qg_schedule", command, "已查询岐关车可选班次。")

        if self._contains_any(question, ("宣讲会", "宣讲", "校招宣讲")) and self._contains_any(question, ("详情", "查看", "具体")):
            item_id = self._extract_id(question)
            if not item_id:
                return ("career", "career_teachin_detail_need_id", [], "请补充宣讲会 ID，例如“查看宣讲会 174790 详情”。")
            return ("career", "career_teachin_detail", ["__NO_AUTH__", "career", "teachin", "detail", "--id", item_id], "已查询就业系统宣讲会详情。")

        if self._contains_any(question, ("招聘会", "双选会")) and self._contains_any(question, ("详情", "查看", "具体")):
            item_id = self._extract_id(question)
            if not item_id:
                return ("career", "career_jobfair_detail_need_id", [], "请补充招聘会 ID，例如“查看招聘会 49326 详情”。")
            return ("career", "career_jobfair_detail", ["__NO_AUTH__", "career", "jobfair", "detail", "--id", item_id], "已查询就业系统招聘会详情。")

        if self._contains_any(question, ("就业岗位", "实习岗位", "招聘岗位", "岗位详情", "职位详情")) and self._contains_any(question, ("详情", "查看", "具体")):
            item_id = self._extract_id(question)
            if not item_id:
                return ("career", "career_job_detail_need_id", [], "请补充岗位 ID，例如“查看岗位 2370124 详情”。")
            return ("career", "career_job_detail", ["__NO_AUTH__", "career", "job", "detail", "--id", item_id], "已查询就业系统岗位详情。")

        if self._contains_any(question, ("宣讲会", "宣讲", "校招宣讲")) and self._contains_any(question, ("报名", "参加", "加入")):
            item_id = self._extract_id(question)
            if not item_id:
                return ("career", "career_teachin_signup_need_id", [], "请补充宣讲会 ID。系统只会生成报名预览，不会自动提交。")
            return ("career", "career_teachin_signup_preview", ["career", "teachin", "signup", "--id", item_id], "已生成宣讲会报名预览；未加 confirm，不会自动提交。")

        if self._contains_any(question, ("招聘会", "双选会")) and self._contains_any(question, ("报名", "参加", "加入")):
            item_id = self._extract_id(question)
            if not item_id:
                return ("career", "career_jobfair_signup_need_id", [], "请补充招聘会 ID。系统只会生成报名预览，不会自动提交。")
            return ("career", "career_jobfair_signup_preview", ["career", "jobfair", "signup", "--id", item_id], "已生成招聘会报名预览；未加 confirm，不会自动提交。")

        if self._contains_any(question, ("就业岗位", "实习岗位", "招聘岗位", "职位")) and self._contains_any(question, ("投递", "申请", "报名")):
            item_id = self._extract_id(question)
            if not item_id:
                return ("career", "career_job_apply_need_id", [], "请补充岗位 ID。系统只会生成投递预览，不会自动提交。")
            return ("career", "career_job_apply_preview", ["career", "job", "apply", "--id", item_id], "已生成岗位投递预览；未加 confirm，不会自动提交。")

        if self._contains_any(question, ("宣讲会", "宣讲", "校招宣讲")):
            command = ["__NO_AUTH__", "career", "teachin", "list", "--limit", self._extract_limit(question)]
            keyword = self._infer_career_keyword(question)
            if keyword:
                command.extend(["--title", keyword])
            if self._contains_any(question, ("线上", "在线")):
                command.extend(["--type", "online"])
            if self._contains_any(question, ("线下", "现场")):
                command.extend(["--type", "offline"])
            return ("career", "career_teachin_list", command, "已查询就业系统宣讲会列表。")

        if self._contains_any(question, ("招聘会", "双选会")):
            command = ["__NO_AUTH__", "career", "jobfair", "list", "--limit", self._extract_limit(question)]
            return ("career", "career_jobfair_list", command, "已查询就业系统招聘会列表。")

        if self._contains_any(question, ("就业岗位", "实习岗位", "招聘岗位", "岗位列表", "找实习", "找工作")):
            command = ["__NO_AUTH__", "career", "job", "list", "--limit", self._extract_limit(question)]
            return ("career", "career_job_list", command, "已查询就业系统岗位列表。")

        if self._contains_any(question, ("交叉探索", "学术讲座", "讲座", "组会", "seminar")):
            item_id = self._extract_id(question)
            if self._contains_any(question, ("预约", "报名", "参加")):
                if not item_id:
                    return ("explore", "explore_seminar_reserve_need_id", [], "请补充讲座/组会 ID。系统只会生成预约预览，不会自动提交。")
                return ("explore", "explore_seminar_reserve_preview", ["explore", "seminar", "reserve", "--id", item_id], "已生成交叉探索讲座预约预览；未加 confirm，不会自动提交。")
            if self._contains_any(question, ("详情", "查看", "具体")):
                if not item_id:
                    return ("explore", "explore_seminar_detail_need_id", [], "请补充讲座/组会 ID。")
                return ("explore", "explore_seminar_detail", ["explore", "seminar", "detail", "--id", item_id], "已查询交叉探索讲座/组会详情。")
            if self._contains_any(question, ("日历", "哪天", "当天", "日期")):
                return ("explore", "explore_seminar_calendar_date", ["explore", "seminar", "calendar", "--date", self._extract_date(question)], "已查询交叉探索讲座日历。")
            month = self._extract_month(question)
            if month:
                return ("explore", "explore_seminar_calendar_month", ["explore", "seminar", "calendar", "--month", month], "已查询交叉探索讲座月历。")
            kind = "todayHot" if self._contains_any(question, ("今天", "今日", "热门")) else "latest"
            command = ["explore", "seminar", "list", "--kind", kind]
            keyword = self._infer_career_keyword(question)
            if keyword:
                command.extend(["--keyword", keyword])
            return ("explore", "explore_seminar_list", command, "已查询交叉探索讲座/组会列表。")

        if self._contains_any(question, ("科研项目", "科研课题", "科研训练", "科研配对", "research")):
            item_id = self._extract_id(question)
            if self._contains_any(question, ("申请", "报名", "加入")):
                if not item_id:
                    return ("explore", "explore_research_apply_need_id", [], "请补充科研项目 ID。系统只会生成申请预览，不会自动提交。")
                return ("explore", "explore_research_apply_preview", ["explore", "research", "apply", "--id", item_id], "已生成科研项目申请预览；未加 confirm，不会自动提交。")
            if self._contains_any(question, ("详情", "查看", "具体")):
                if not item_id:
                    return ("explore", "explore_research_detail_need_id", [], "请补充科研项目 ID。")
                return ("explore", "explore_research_detail", ["explore", "research", "detail", "--id", item_id], "已查询交叉探索科研项目详情。")
            if self._contains_any(question, ("筛选", "条件", "类型")):
                return ("explore", "explore_research_filters", ["explore", "research", "filters"], "已查询交叉探索科研项目筛选条件。")
            kind = "pairing" if self._contains_any(question, ("配对", "招募")) else "latest"
            command = ["explore", "research", "list", "--kind", kind, "--page-size", self._extract_limit(question)]
            keyword = self._infer_career_keyword(question)
            if keyword:
                command.extend(["--keyword", keyword])
            return ("explore", "explore_research_list", command, "已查询交叉探索科研项目列表。")

        if self._contains_any(question, ("图书馆房型", "空间房型", "研讨室类型", "自习室类型", "可预约空间")):
            command = ["libic", "room-types"]
            kind = self._infer_libic_kind(question)
            if kind:
                command.extend(["--query", kind])
            return ("libic", "libic_room_types", command, "已查询图书馆空间预约系统房型。")

        if self._contains_any(question, ("自习室空位", "研讨室空位", "学习空间空位", "图书馆空位", "可约研讨室", "可预约研讨室", "可预约自习室")):
            kind = self._infer_libic_kind(question)
            if not kind:
                return ("libic", "libic_available_need_kind", [], "请补充空间类型，例如“查询明天珠海研讨室空位”或“查询今天自习室空位”。")
            return (
                "libic",
                "libic_available",
                ["libic", "available", "--kind", kind, "--date", self._extract_date(question)],
                f"已查询图书馆空间预约系统“{kind}”的可预约空档。",
            )

        if self._contains_any(question, ("场地类型", "场馆类型", "有哪些场地", "有哪些场馆", "可预约场地")):
            command = ["gym", "venue-types"]
            venue_type = self._infer_gym_venue_type(question)
            if venue_type:
                command.extend(["--query", venue_type])
            return ("gym", "gym_venue_types", command, "已查询体育场馆预约系统场地类型。")

        if self._contains_any(question, ("场馆空位", "场地空位", "可约场地", "可预约场地", "羽毛球空位", "健身房空位")):
            venue_type = self._infer_gym_venue_type(question)
            if not venue_type:
                return ("gym", "gym_available_need_venue_type", [], "请补充场地类型，例如“查询明天羽毛球空位”或“查询今天健身房空位”。")
            return (
                "gym",
                "gym_available",
                ["gym", "available", "--venue-type", venue_type, "--date", self._extract_date(question), "--days", self._extract_days(question, default="1")],
                f"已查询体育场馆预约系统“{venue_type}”的可预约时段。",
            )

        if self._contains_any(question, ("体育账号", "体育账户", "场馆账户", "体育余额", "场馆余额", "违约分", "信誉分")):
            return ("gym", "gym_profile", ["gym", "profile"], "已查询体育场馆预约系统个人资料。")

        if self._contains_any(question, ("体育校区", "场馆校区", "有哪些体育校区")):
            return ("gym", "gym_campuses", ["gym", "campuses"], "已查询体育场馆预约系统校区列表。")

        if self._contains_any(question, ("预约", "预订")) and self._contains_any(question, ("健身房", "羽毛球", "网球", "篮球", "乒乓", "游泳", "足球", "排球")) and not self._contains_any(question, ("我的", "本人", "记录", "状态")):
            venue_type = self._infer_gym_venue_type(question)
            time_range = self._extract_time_range(question)
            if not venue_type or not time_range:
                return ("gym", "gym_book_need_detail", [], "请补充场地类型和时间段，例如“预览预约明天羽毛球 19:00-21:00”。系统只会预览，不会自动提交。")
            start, end = time_range
            return (
                "gym",
                "gym_book_preview",
                ["gym", "book", "--venue-type", venue_type, "--date", self._extract_date(question), "--start", start, "--end", end],
                f"已生成“{venue_type}”预约预览；未加 confirm，不会自动提交。",
            )

        if self._contains_any(question, ("成绩", "绩点", "GPA", "gpa", "学分")):
            return ("jwxt", "jwxt_grade", ["__DIRECT_JWXT_GRADE__"], "已查询 JWXT 成绩/绩点/学分。")

        if self._contains_any(question, ("考试", "期末", "期中", "考场", "考试安排", "考试课表", "考试信息")):
            return ("jwxt", "jwxt_exam", ["__DIRECT_JWXT_EXAM__"], "已查询 JWXT 考试信息。")

        if self._contains_any(question, ("请假", "假条", "销假")):
            if self._is_leave_confirm_intent(question) or self._is_leave_apply_intent(question):
                return ("jwxt", "jwxt_leave_apply", ["__DIRECT_JWXT_LEAVE_APPLY__"], "已处理 JWXT 请假申请预览/提交。")
            if self._contains_any(question, ("原因", "类型")):
                return ("jwxt", "jwxt_leave_reasons", ["jwxt", "leave", "reasons"], "已查询 JWXT 请假原因/类型。")
            if self._contains_any(question, ("汇总", "统计", "摘要")):
                return ("jwxt", "jwxt_leave_summary", ["jwxt", "leave", "summary"], "已查询 JWXT 请假汇总。")
            if self._contains_any(question, ("审核", "审批详情", "流程")):
                item_id = self._extract_id(question)
                if not item_id:
                    return ("jwxt", "jwxt_leave_audit_need_id", [], "请补充请假记录 ID，才能查询审核流。")
                return ("jwxt", "jwxt_leave_audit", ["jwxt", "leave", "audit", "--id", item_id], "已查询 JWXT 请假审核流。")
            return ("jwxt", "jwxt_leave_list", ["jwxt", "leave", "list", "--page", "1", "--size", "20"], "已查询 JWXT 请假记录。")

        if self._contains_any(question, ("上课时间", "节次时间", "第几节", "作息时间")):
            return ("jwxt", "jwxt_section_times", ["jwxt", "section-times"], "已查询 JWXT 节次时间。")

        if self._contains_any(question, ("今天课", "今日课", "课表", "课程表", "上课")):
            if "今天" in question or "今日" in question:
                return ("jwxt", "jwxt_today", ["today"], "已查询今天的 JWXT 课程。")
            return ("jwxt", "jwxt_timetable", ["jwxt", "timetable"], "已查询 JWXT 当前周课表。")

        if self._contains_any(question, ("场馆", "体育馆", "健身房", "羽毛球", "网球", "篮球", "乒乓", "游泳", "足球", "排球")) and self._contains_any(question, ("预约", "记录", "状态", "我的", "本人")):
            venue_type = self._infer_gym_venue_type(question)
            if not venue_type:
                return ("gym", "gym_booking_need_venue_type", [], "请补充场地类型，例如“查询我的健身房预约”或“查询我的羽毛球预约”。")
            return (
                "gym",
                "gym_bookings_mine",
                ["gym", "bookings", "--venue-type", venue_type, "--mine", "--days", self._extract_days(question)],
                f"已查询 gym 系统中“{venue_type}”的本人预约记录。",
            )

        if self._contains_any(question, ("雨课堂", "ykt", "课堂派")):
            if self._contains_any(question, ("状态", "是否登录", "登录状态")):
                return ("ykt", "ykt_status", ["__NO_AUTH__", "ykt", "status"], "已检查雨课堂网页登录状态。")
            if self._contains_any(question, ("课程", "课堂列表", "我的课堂")):
                return ("ykt", "ykt_courses", ["__NO_AUTH__", "ykt", "courses"], "已查询雨课堂课程列表。")
            if self._contains_any(question, ("签到", "checkin")):
                return ("ykt", "ykt_checkin_list", ["__NO_AUTH__", "ykt", "checkin", "list"], "已查询雨课堂签到活动列表。")
            if self._contains_any(question, ("作业", "homework")):
                return ("ykt", "ykt_homework_list", ["__NO_AUTH__", "ykt", "homework", "list"], "已查询雨课堂作业列表。")

        if self._contains_any(question, ("勤工", "助学岗位", "岗位申请", "勤工助学")):
            if self._contains_any(question, ("简历", "个人简历")):
                return ("xgxt", "xgxt_workstudy_resume", ["xgxt", "workstudy", "resume"], "已查询学工系统勤工助学简历状态。")
            if self._contains_any(question, ("有哪些", "列表", "可申请", "岗位")) and not self._contains_any(question, ("我的", "本人", "记录", "状态", "进度")):
                return ("xgxt", "xgxt_workstudy_list", ["xgxt", "workstudy", "list", "--page", "1", "--size", self._extract_limit(question)], "已查询学工系统勤工助学岗位列表。")
            return ("xgxt", "xgxt_workstudy_records", ["xgxt", "workstudy", "records", "--page", "1", "--size", "20"], "已查询学工系统勤工助学申请记录。")

        if self._contains_any(question, ("学工当前用户", "学工账号", "xgxt 当前用户")):
            return ("xgxt", "xgxt_current_user", ["xgxt", "current-user"], "已查询学工系统当前用户。")

        if self._contains_any(question, ("离返校选项", "假期登记选项", "假期登记条件")):
            return ("xgxt", "xgxt_holiday_filters", ["xgxt", "holiday", "filters"], "已查询学工系统离返校登记选项。")

        if self._contains_any(question, ("离校", "返校", "离返校", "寒假", "暑假", "假期登记")) and self._contains_any(question, ("任务", "记录", "状态", "我的", "本人")):
            return ("xgxt", "xgxt_holiday_list", ["xgxt", "holiday", "list"], "已查询学工系统离返校登记任务。")

        if self._contains_any(question, ("审批", "待办", "已办", "申请进度", "会议室", "课室", "教室", "学生活动中心", "活动室")):
            if self._contains_any(question, ("应用", "可申请", "有哪些流程")):
                return ("usc", "usc_apps", ["usc", "apps"], "已查询 USC/BPM 可用应用。")
            if self._contains_any(question, ("课室校区", "教室校区")):
                return ("usc", "usc_classroom_campuses", ["usc", "classroom", "campuses"], "已查询课室申请校区选项。")
            if self._contains_any(question, ("课室节次", "教室节次", "节次选项")):
                return ("usc", "usc_classroom_sections", ["usc", "classroom", "sections"], "已查询课室申请节次选项。")
            if self._contains_any(question, ("可用课室", "空课室", "课室空位", "教室空位")):
                section_range = self._extract_section_range(question)
                if not section_range:
                    return ("usc", "usc_classroom_rooms_need_section", [], "请补充节次，例如“查询明天珠海校区第1-2节可用课室”。")
                section_start, section_end = section_range
                command = ["usc", "classroom", "rooms", "--date", self._extract_date(question), "--section-start", section_start, "--section-end", section_end]
                campus = self._infer_campus(question)
                if campus:
                    command.extend(["--campus", campus])
                return ("usc", "usc_classroom_rooms", command, "已查询 USC/BPM 可用课室。")
            if self._contains_any(question, ("会议室校区", "会议校区")):
                return ("usc", "usc_meeting_campuses", ["usc", "meeting", "campuses"], "已查询会议室预约校区选项。")
            if self._contains_any(question, ("会议室列表", "会议室有哪些", "可选会议室")):
                command = ["usc", "meeting", "venues"]
                campus = self._infer_campus(question)
                if campus:
                    command.extend(["--campus", campus])
                return ("usc", "usc_meeting_venues", command, "已查询会议室列表。")
            if self._contains_any(question, ("会议室空位", "会议室可用", "会议室是否可约")):
                time_range = self._extract_time_range(question)
                item_id = self._extract_id(question)
                if not time_range or not item_id:
                    return ("usc", "usc_meeting_availability_need_detail", [], "请补充会议室名称/ID 和时间段，例如“查询明天 C507 08:00-09:00 是否可约”。")
                start_time, end_time = time_range
                date = self._extract_date(question)
                command = ["usc", "meeting", "availability", "--venue", item_id, "--start", f"{date} {start_time}", "--end", f"{date} {end_time}"]
                campus = self._infer_campus(question)
                if campus:
                    command.extend(["--campus", campus])
                return ("usc", "usc_meeting_availability", command, "已查询会议室可用性。")
            if self._contains_any(question, ("活动室", "学生活动中心")) and self._contains_any(question, ("房间", "场地", "可用", "列表")):
                command = ["usc", "activity", "rooms"]
                time_range = self._extract_time_range(question)
                if time_range:
                    start_time, end_time = time_range
                    date = self._extract_date(question)
                    command.extend(["--apply-date", date, "--start", f"{date} {start_time}:00", "--end", f"{date} {end_time}:00"])
                return ("usc", "usc_activity_rooms", command, "已查询学生活动中心场地选项。")
            if self._contains_any(question, ("社团", "组织")) and self._contains_any(question, ("列表", "查询", "选项")):
                return ("usc", "usc_activity_clubs", ["usc", "activity", "clubs"], "已查询学生活动中心社团/组织选项。")
            if self._contains_any(question, ("待办", "任务")):
                return ("usc", "usc_tasks", ["usc", "tasks", "--page", "1", "--size", "20"], "已查询 USC/BPM 待办任务。")
            return ("usc", "usc_sessions", ["usc", "sessions", "--page", "1", "--size", "20"], "已查询 USC/BPM 申请会话/审批记录。")

        return None

    async def query(self, user_id: str, question: str) -> PrivateQueryResult | None:
        matched = self.match(question)
        if not matched:
            return None
        system, intent, command, title = matched
        auth_required = True
        if command and command[0] == "__NO_AUTH__":
            auth_required = False
            command = command[1:]
        if not command:
            return PrivateQueryResult(
                answer=title,
                system=system,
                needs_relogin=False,
                intent=intent,
                next_action="need_more_detail",
                raw={"command": command},
            )
        if command == ["__DIRECT_JWXT_GRADE__"]:
            return await self._query_jwxt_grades(user_id, question)
        if command == ["__DIRECT_JWXT_EXAM__"]:
            return await self._query_jwxt_exam(user_id, question)
        if command == ["__DIRECT_JWXT_LEAVE_APPLY__"]:
            return await self._query_jwxt_leave_apply(user_id, question)
        return await self._run(user_id, system, intent, title, command, auth_required=auth_required)

class SysuAnythingAdapter:
    def __init__(self, sessions: PrivateSessionStore | None = None) -> None:
        self.sessions = sessions or PrivateSessionStore()
        self.libic = LibicConnector(self.sessions)
        self.sysu_private = SysuAnythingPrivateConnector()

    async def query_public_function(self, user_id: str, question: str) -> PrivateQueryResult | None:
        matched = self.sysu_private.match(question)
        if not matched:
            return None
        _, _, command, _ = matched
        return await self.sysu_private.query(user_id, question)
    async def query_personal(self, user_id: str, question: str) -> PrivateQueryResult:
        if self.libic.matches(question):
            return await self.libic.query(user_id, question)

        sysu_result = await self.sysu_private.query(user_id, question)
        if sysu_result:
            return sysu_result

        if private_sysu_auth.has_cas_session(user_id):
            return PrivateQueryResult(
                answer=(
                    "已检测到该本地会话绑定了个人企业微信/CAS 登录态，但当前问题没有匹配到可执行的私人事务。"
                    "请直接说要查询的具体事项，例如：查询今天课表、查询我的请假记录、查询我的羽毛球预约、"
                    "查询明天珠海校区第1-2节可用课室、查询我的勤工助学申请记录。"
                    "如果你问的是公开规则、流程或校车班车，应切换到公共栏目或直接说明“查询校车/班车”。"
                ),
                system="sysu-anything",
                needs_relogin=False,
                intent="private_dispatch_need_detail",
                next_action="need_more_detail",
            )

        return PrivateQueryResult(
            answer=(
                "私人事务只处理需要本人身份才能看到的数据，例如：我的自习室预约、我的请假进度、"
                "我的审批状态、我的家庭经济困难认定进度、我的场馆预约记录、我的成绩或课表。"
                "公开规则和办事流程，例如图书馆预约规则、校园卡办理流程、校车/班车时刻，应切换到官方公共栏目查询。"
                "当前需要先绑定该用户自己的企业微信/CAS 登录态。"
            ),
            system="sysu-anything",
            needs_relogin=True,
            intent="private_dispatch",
            next_action="bind_sysu_account",
        )




























