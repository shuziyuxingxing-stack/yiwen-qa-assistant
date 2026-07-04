from app.core.models import RouteType


PRIVATE_KEYWORDS = {
    "课表",
    "成绩",
    "考试",
    "选课",
    "请假",
    "审批",
    "预约",
    "学费",
    "校园卡",
    "宿舍",
    "奖学金",
    "助学金",
    "我的事务",
}

PRIVATE_OWNERSHIP_KEYWORDS = {
    "我",
    "我的",
    "本人",
    "个人",
    "自己",
    "查我",
    "帮我查",
}

PRIVATE_STATUS_KEYWORDS = {
    "状态",
    "进度",
    "记录",
    "结果",
    "余额",
    "待办",
    "已办",
    "有没有",
}

PUBLIC_KEYWORDS = {
    "新生",
    "报到",
    "通知",
    "规定",
    "规则",
    "要求",
    "流程",
    "指南",
    "说明",
    "地点",
    "时间",
    "新闻",
    "资讯",
    "校内",
    "学校",
    "学院",
    "介绍",
}


def _contains_any(message: str, lowered: str, tokens: set[str]) -> bool:
    return any(token in message or token in lowered for token in tokens)


def classify_message(message: str) -> RouteType:
    lowered = message.lower()
    has_private_topic = _contains_any(message, lowered, PRIVATE_KEYWORDS)
    has_private_owner = _contains_any(message, lowered, PRIVATE_OWNERSHIP_KEYWORDS)
    has_private_status = _contains_any(message, lowered, PRIVATE_STATUS_KEYWORDS)
    has_public = _contains_any(message, lowered, PUBLIC_KEYWORDS)

    if has_private_topic and (has_private_owner or has_private_status):
        return "private"
    if has_private_topic and has_public:
        return "public"
    if has_private_topic:
        return "private"
    return "public"
