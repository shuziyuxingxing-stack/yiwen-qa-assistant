from typing import Literal

from pydantic import BaseModel, Field


RouteType = Literal["public", "private", "hybrid"]
SourceType = Literal["yiwen", "private_connector", "supplement_kb", "external_rag", "system"]
ChatChannel = Literal["sysu_kb", "sysu_news", "web_search", "model", "private", "freshman_materials", "auto"]


class ChatRequest(BaseModel):
    user_id: str | None = Field(default=None, min_length=1)
    message: str = Field(..., min_length=1)
    channel: ChatChannel = "sysu_kb"
    chat_id: str | None = None
    agent_id: str | None = None
    model: str = Field(default="V3", min_length=1)
    search_source: str = Field(default="sysuKB", min_length=1)


class LoginRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    display_name: str | None = None


class LoginResponse(BaseModel):
    user_id: str
    display_name: str | None = None
    access_token: str
    token_type: str = "bearer"


class UserProfile(BaseModel):
    user_id: str
    display_name: str | None = None
    created_at: float
    last_seen_at: float

class YiwenCallbackReplayRequest(BaseModel):
    callback_url: str | None = None
class AnswerSource(BaseModel):
    type: SourceType
    title: str
    system: str | None = None
    detail: str | None = None


class ChatAction(BaseModel):
    type: str
    system: str | None = None
    needed: bool
    message: str | None = None


class ChatResponse(BaseModel):
    chat_id: str
    route: RouteType
    answer: str
    sources: list[AnswerSource]
    actions: list[ChatAction] = Field(default_factory=list)
    answer_pages: list[str] = Field(default_factory=list)


class PersonalQueryRequest(BaseModel):
    user_id: str | None = Field(default=None, min_length=1)
    message: str = Field(..., min_length=1)


class PersonalQueryResponse(BaseModel):
    user_id: str
    answer: str
    system: str
    needs_relogin: bool = False
    sources: list[AnswerSource] = Field(default_factory=list)
    actions: list[ChatAction] = Field(default_factory=list)


class KbDocumentCreateRequest(BaseModel):
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    visibility: Literal["public", "private"] = "public"


class KbDocumentResponse(BaseModel):
    doc_id: str
    title: str
    content: str
    owner_user_id: str | None = None
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    visibility: str
    created_at: float
    updated_at: float


class KbSearchHitResponse(KbDocumentResponse):
    score: float
    snippet: str


class KbSearchResponse(BaseModel):
    query: str
    hits: list[KbSearchHitResponse]



