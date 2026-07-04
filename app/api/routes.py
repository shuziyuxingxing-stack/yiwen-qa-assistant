import json
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from app.core.models import (
    AnswerSource,
    ChatAction,
    ChatRequest,
    ChatResponse,
    KbDocumentCreateRequest,
    KbDocumentResponse,
    KbSearchHitResponse,
    KbSearchResponse,
    LoginRequest,
    LoginResponse,
    PersonalQueryRequest,
    PersonalQueryResponse,
    UserProfile,
    YiwenCallbackReplayRequest,
)
from app.services.chat_service import ChatService
from app.services.session_store import SessionStore
from app.services.supplement_kb import SupplementKbService
from app.services.shared_yiwen import shared_yiwen_manager
from app.services.external_rag import external_rag_service
from app.services.freshman_materials import freshman_materials_service
from app.services.private_sysu_auth import PrivateSysuAuthError, private_sysu_auth
from app.services.sysu_anything_chat import SysuAnythingCliError, sysu_anything_chat


router = APIRouter()
session_store = SessionStore()
supplement_kb = SupplementKbService()
chat_service = ChatService(session_store=session_store, supplement_kb=supplement_kb)


DEFAULT_AGENT_ID = '619ae0c8ffb246d9b669017763359b81'



def resolve_user_id(user_id: str | None, authorization: str | None) -> str:
    if authorization:
        scheme, _, token = authorization.partition(' ')
        if scheme.lower() == 'bearer' and token:
            account = session_store.get_user_by_token(token.strip())
            if not account:
                raise HTTPException(status_code=401, detail='invalid access token')
            return account.user_id

    if user_id:
        return user_id

    raise HTTPException(status_code=401, detail='missing user_id or bearer token')



@router.get('/health')
async def health() -> dict[str, str]:
    return {'status': 'ok'}


def build_shared_yiwen_status_payload() -> dict[str, Any]:
    sysu_status = sysu_anything_chat.status()
    legacy_session = shared_yiwen_manager.to_payload()
    effective_session = dict(legacy_session)
    configured = bool(sysu_status.get('configured'))
    keepalive = sysu_status.get('keepalive') if isinstance(sysu_status.get('keepalive'), dict) else {}
    last_error = keepalive.get('last_error')
    last_success_at = keepalive.get('last_success_at')
    last_checked_at = keepalive.get('last_checked_at')
    effective_session['configured'] = configured
    effective_session['session_source'] = 'sysu_anything' if configured else 'sysu_anything_missing'
    effective_session['token_len'] = 0 if not configured else effective_session.get('token_len', 0)
    if not configured:
        alive_state = '未登录'
        next_action = '请先打开官方逸问登录窗口，完成登录后点击“从浏览器导入登录态”。'
    elif last_success_at:
        alive_state = '存活'
        next_action = '无需操作。后台会继续定时检测。'
    elif last_error:
        alive_state = '异常'
        next_action = '请确认专用 Chrome 窗口仍登录官方逸问，然后点击“从浏览器导入登录态”或“立即保活检测”。'
    else:
        alive_state = '已导入，等待检测'
        next_action = '可以点击“立即保活检测”确认是否存活。'
    return {
        'system': 'yiwen',
        'scope': 'shared',
        'summary': {
            'login_state': '已导入' if configured else '未导入',
            'alive_state': alive_state,
            'account': sysu_status.get('real_name') or sysu_status.get('username') or '未知',
            'keepalive': '运行中' if keepalive.get('running') else '未运行',
            'last_checked_at': last_checked_at,
            'last_success_at': last_success_at,
            'last_error': last_error,
            'last_auto_import_at': keepalive.get('last_auto_import_at'),
            'last_auto_import_error': keepalive.get('last_auto_import_error'),
            'next_action': next_action,
            'server_time': time.time(),
        },
        'session': effective_session,
        'sysu_anything': sysu_status,
        'legacy_session': legacy_session,
    }


@router.get('/admin/yiwen/shared/status')
async def shared_yiwen_status() -> dict[str, Any]:
    return build_shared_yiwen_status_payload()


@router.post('/admin/yiwen/shared/keepalive')
async def shared_yiwen_keepalive() -> dict[str, Any]:
    result = await sysu_anything_chat.keepalive_once()
    return {
        'system': 'yiwen',
        'scope': 'shared',
        'keepalive': result,
        'sysu_anything': sysu_anything_chat.status(),
    }

@router.post('/admin/yiwen/shared/check')
async def shared_yiwen_check() -> dict[str, Any]:
    if not sysu_anything_chat.has_auth():
        shared_yiwen_manager.mark_failure('SYSU-Anything chat-auth.json is missing')
        return {
            'system': 'yiwen',
            'scope': 'shared',
            'ok': False,
            'session': shared_yiwen_manager.to_payload(),
            'sysu_anything': sysu_anything_chat.status(),
        }
    try:
        result = await sysu_anything_chat.send(message='请用一句话回复：逸问连通性测试', model='V3', search_source='sysuKB')
        if result.chat_id:
            shared_yiwen_manager.set_runtime_chat_id(result.chat_id)
        shared_yiwen_manager.mark_success(result.chat_id)
        return {
            'system': 'yiwen',
            'scope': 'shared',
            'ok': True,
            'answer': result.answer,
            'chat_id': result.chat_id,
            'session': shared_yiwen_manager.to_payload(),
            'sysu_anything': sysu_anything_chat.status(),
        }
    except Exception as exc:
        shared_yiwen_manager.mark_failure(exc)
        return {
            'system': 'yiwen',
            'scope': 'shared',
            'ok': False,
            'error': str(exc),
            'session': shared_yiwen_manager.to_payload(),
            'sysu_anything': sysu_anything_chat.status(),
        }


@router.post('/admin/yiwen/shared/chrome/start')
async def shared_yiwen_chrome_start() -> dict[str, Any]:
    try:
        return {'system': 'yiwen', 'scope': 'shared', **sysu_anything_chat.launch_chrome_debug()}
    except SysuAnythingCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post('/admin/yiwen/shared/chrome/import')
async def shared_yiwen_chrome_import() -> dict[str, Any]:
    try:
        result = await sysu_anything_chat.import_chrome_debug()
    except SysuAnythingCliError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    auth_state = result.get('authState') if isinstance(result.get('authState'), dict) else {}
    shared_yiwen_manager.set_runtime_session(
        bearer_token=str(auth_state.get('token') or 'imported-by-sysu-anything'),
        username=auth_state.get('username') or auth_state.get('realName'),
        agent_id=DEFAULT_AGENT_ID,
    )
    shared_yiwen_manager.status.session_source = 'sysu_anything_chrome_debug'
    return {
        'system': 'yiwen',
        'scope': 'shared',
        'status': 'imported-from-chrome-debug',
        'result': result,
        'session': shared_yiwen_manager.to_payload(),
        'sysu_anything': sysu_anything_chat.status(),
    }


@router.get('/admin/yiwen/shared/auth-url')
async def shared_yiwen_auth_url() -> dict[str, Any]:
    try:
        result = await sysu_anything_chat.auth_url()
    except SysuAnythingCliError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        'system': 'yiwen',
        'scope': 'shared',
        'mode': 'sysu-anything-auth-url',
        **result,
    }


@router.post('/admin/yiwen/shared/replay-callback')
async def shared_yiwen_replay_callback(payload: YiwenCallbackReplayRequest) -> dict[str, Any]:
    if not payload.callback_url:
        raise HTTPException(status_code=400, detail='callback_url is required')
    try:
        result = await sysu_anything_chat.replay_callback(payload.callback_url)
    except SysuAnythingCliError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    auth_state = result.get('authState') if isinstance(result.get('authState'), dict) else {}
    shared_yiwen_manager.set_runtime_session(
        bearer_token=str(auth_state.get('token') or 'imported-by-sysu-anything'),
        username=auth_state.get('username') or auth_state.get('realName'),
        agent_id=DEFAULT_AGENT_ID,
    )
    shared_yiwen_manager.status.session_source = 'sysu_anything_callback'
    return {
        'system': 'yiwen',
        'scope': 'shared',
        'status': 'saved-from-sysu-anything-callback',
        'result': result,
        'session': shared_yiwen_manager.to_payload(),
        'sysu_anything': sysu_anything_chat.status(),
    }


@router.post('/admin/yiwen/shared/send-test')
async def shared_yiwen_send_test() -> dict[str, Any]:
    try:
        result = await sysu_anything_chat.send(message='请问门诊什么时候开门', model='V3', search_source='sysuKB')
    except SysuAnythingCliError as exc:
        shared_yiwen_manager.mark_failure(exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result.chat_id:
        shared_yiwen_manager.set_runtime_chat_id(result.chat_id)
    shared_yiwen_manager.mark_success(result.chat_id)
    return {
        'system': 'yiwen',
        'scope': 'shared',
        'chat_id': result.chat_id,
        'answer': result.answer,
        'raw': result.raw,
        'session': shared_yiwen_manager.to_payload(),
    }


@router.get('/admin/yiwen/shared/login.json')
async def shared_yiwen_login_json() -> dict[str, str]:
    return {
        'system': 'yiwen',
        'scope': 'shared',
        'login_page': '/admin/yiwen/shared/login',
        'chrome_start_endpoint': '/admin/yiwen/shared/chrome/start',
        'chrome_import_endpoint': '/admin/yiwen/shared/chrome/import',
        'auth_url_endpoint': '/admin/yiwen/shared/auth-url',
        'callback_replay_endpoint': '/admin/yiwen/shared/replay-callback',
        'send_test_endpoint': '/admin/yiwen/shared/send-test',
        'message': '后端直接包装 SYSU-Anything chat 命令；普通用户不接触 token/cookie/控制台脚本。',
    }




@router.get('/admin/yiwen/shared/login', response_class=HTMLResponse)
async def shared_yiwen_login_page() -> str:
    return '''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>共享逸问登录</title>
  <style>
    :root { color-scheme: light; font-family: "Microsoft YaHei", Arial, sans-serif; }
    body { margin: 0; background: #f6f7f9; color: #1f2937; }
    main { max-width: 980px; margin: 0 auto; padding: 32px 20px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    h2 { margin: 0 0 10px; font-size: 19px; }
    p { line-height: 1.7; }
    .panel { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 20px; margin-top: 16px; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
    button { border: 0; border-radius: 6px; padding: 10px 14px; font-size: 15px; cursor: pointer; }
    button.primary { background: #0f766e; color: #fff; }
    button.secondary { background: #e5e7eb; color: #111827; }
    textarea, code, pre, .status-grid { box-sizing: border-box; width: 100%; white-space: pre-wrap; word-break: break-all; border-radius: 6px; }
    textarea { min-height: 92px; border: 1px solid #d1d5db; padding: 12px; font-family: Consolas, monospace; }
    code, pre { display: block; background: #111827; color: #e5e7eb; padding: 12px; overflow: auto; }
    .status-grid { display: grid; grid-template-columns: 150px 1fr; gap: 8px 12px; background: #f9fafb; padding: 12px; margin-top: 12px; }
    .status-grid b { color: #374151; }
    .ok-text { color: #047857; font-weight: 700; }
    .bad-text { color: #b91c1c; font-weight: 700; }
    .muted { color: #6b7280; }
    .status { background: #f9fafb; border-radius: 6px; padding: 12px; margin-top: 12px; white-space: pre-wrap; }
  </style>
</head>
<body>
  <main>
    <h1>共享逸问登录</h1>
    <p>这个页面只给管理员使用。普通用户只在助手里提问，不需要处理 token、cookie、控制台脚本或浏览器插件。</p>

    <section class="panel">
      <h2>方式一：SYSU-Anything 浏览器态导入</h2>
      <p>后端会启动一个独立 Chrome 调试窗口。你在该窗口完成官方逸问登录后，本服务调用 SYSU-Anything 的 <code>chat import-chrome-debug</code> 导入登录态。服务启动后会每隔数分钟自动校验登录态；如果调试浏览器仍保持登录，会尝试自动重新导入。</p>
      <div class="actions">
        <button class="primary" type="button" id="start-chrome">打开官方逸问登录窗口</button>
        <button class="primary" type="button" id="import-chrome">从浏览器导入登录态</button>
        <button class="secondary" type="button" id="send-test">发送真实测试问题</button>
        <button class="secondary" type="button" id="keepalive-now">立即保活检测</button>
      </div>
      <div class="status" id="chrome-status">尚未操作。</div>
    </section>


    <section class="panel">
      <h2>当前状态</h2>
      <div class="actions">
        <button class="secondary" type="button" id="check-status">刷新状态</button>
      </div>
      <div id="status" class="status-grid">检测中...</div>
    </section>
  </main>
  <script>
    const statusEl = document.getElementById('status');
    const chromeStatusEl = document.getElementById('chrome-status');

    async function parseJson(response) {
      const text = await response.text();
      try { return JSON.parse(text); } catch (err) { return { detail: text }; }
    }
    function formatTime(value) {
      if (!value) return '无';
      const millis = typeof value === 'number' ? value * 1000 : Date.parse(value);
      if (!Number.isFinite(millis)) return String(value);
      return new Date(millis).toLocaleString('zh-CN', { hour12: false });
    }
    function cleanError(value) {
      if (!value) return '无';
      const text = String(value).replace(/^sysu-anything failed:\s*/i, '').trim();
      if (text.includes('认证失败')) return '官方逸问认证失败，需要重新导入登录态。';
      if (text.includes('fetch failed')) return '无法连接官方逸问或 Chrome 调试窗口未保持登录。';
      return text;
    }
    function renderStatus(payload) {
      const s = payload.summary || {};
      const aliveClass = s.alive_state === '存活' ? 'ok-text' : (s.alive_state === '异常' || s.alive_state === '未登录' ? 'bad-text' : 'muted');
      statusEl.innerHTML = `
        <b>登录态</b><span>${s.login_state || '未知'}</span>
        <b>存活状态</b><span class="${aliveClass}">${s.alive_state || '未知'}</span>
        <b>账号</b><span>${s.account || '未知'}</span>
        <b>保活任务</b><span>${s.keepalive || '未知'}</span>
        <b>最近检测</b><span>${formatTime(s.last_checked_at)}</span>
        <b>最近成功</b><span>${formatTime(s.last_success_at)}</span>
        <b>最近错误</b><span>${cleanError(s.last_error)}</span>
        <b>自动重导入</b><span>${s.last_auto_import_at ? '最近成功：' + formatTime(s.last_auto_import_at) : cleanError(s.last_auto_import_error)}</span>
        <b>下一步</b><span>${s.next_action || '无'}</span>
      `;
    }
    function showOperation(target, payload) {
      const summary = payload.status_summary || (payload.sysu_anything && payload.sysu_anything.keepalive ? payload.sysu_anything.keepalive : null);
      if (payload.answer) {
        target.textContent = '测试成功。逸问回答：\\n' + payload.answer;
      } else if (payload.started) {
        target.textContent = '官方逸问登录窗口已打开。请在该窗口完成登录，然后点击“从浏览器导入登录态”。';
      } else if (payload.status === 'imported-from-chrome-debug') {
        target.textContent = '导入成功。已保存共享逸问登录态。';
      } else if (payload.keepalive) {
        target.textContent = payload.keepalive.last_error ? '保活异常：' + cleanError(payload.keepalive.last_error) : '保活成功，当前登录态可用。';
      } else if (payload.detail) {
        target.textContent = '操作失败：' + cleanError(payload.detail);
      } else if (summary && summary.last_error) {
        target.textContent = '操作完成，但状态异常：' + cleanError(summary.last_error);
      } else {
        target.textContent = '操作完成。';
      }
    }
    async function refreshStatus() {
      const response = await fetch('/admin/yiwen/shared/status');
      renderStatus(await parseJson(response));
    }
    document.getElementById('start-chrome').addEventListener('click', async () => {
      chromeStatusEl.textContent = '正在启动浏览器...';
      const response = await fetch('/admin/yiwen/shared/chrome/start', { method: 'POST' });
      showOperation(chromeStatusEl, await parseJson(response));
      await refreshStatus();
    });
    document.getElementById('import-chrome').addEventListener('click', async () => {
      chromeStatusEl.textContent = '正在调用 SYSU-Anything 导入浏览器态...';
      const response = await fetch('/admin/yiwen/shared/chrome/import', { method: 'POST' });
      showOperation(chromeStatusEl, await parseJson(response));
      await refreshStatus();
    });
    document.getElementById('send-test').addEventListener('click', async () => {
      chromeStatusEl.textContent = '正在通过 SYSU-Anything 发送真实测试问题...';
      const response = await fetch('/admin/yiwen/shared/send-test', { method: 'POST' });
      showOperation(chromeStatusEl, await parseJson(response));
      await refreshStatus();
    });
    document.getElementById('keepalive-now').addEventListener('click', async () => {
      chromeStatusEl.textContent = '正在执行保活检测...';
      const response = await fetch('/admin/yiwen/shared/keepalive', { method: 'POST' });
      showOperation(chromeStatusEl, await parseJson(response));
      await refreshStatus();
    });
    document.getElementById('check-status').addEventListener('click', refreshStatus);
    void refreshStatus();
  </script>
</body>
</html>'''


@router.post('/auth/login', response_model=LoginResponse)
async def login(payload: LoginRequest) -> LoginResponse:
    try:
        account = session_store.login_user(payload.user_id, payload.display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return LoginResponse(
        user_id=account.user_id,
        display_name=account.display_name,
        access_token=account.access_token,
    )

@router.post('/auth/local', response_model=LoginResponse)
async def local_login() -> LoginResponse:
    account = session_store.create_local_user(display_name='本机会话')
    return LoginResponse(
        user_id=account.user_id,
        display_name=account.display_name,
        access_token=account.access_token,
    )

@router.get('/me', response_model=UserProfile)
async def me(authorization: str | None = Header(default=None)) -> UserProfile:
    user_id = resolve_user_id(None, authorization)
    account = session_store.get_user(user_id)
    if not account:
        raise HTTPException(status_code=404, detail='user not found')
    return UserProfile(
        user_id=account.user_id,
        display_name=account.display_name,
        created_at=account.created_at,
        last_seen_at=account.last_seen_at,
    )


@router.post('/chat', response_model=ChatResponse)
async def chat(payload: ChatRequest, authorization: str | None = Header(default=None)) -> ChatResponse:
    resolved_user_id = resolve_user_id(None, authorization)
    return await chat_service.handle(payload.model_copy(update={'user_id': resolved_user_id}))




@router.post('/kb/documents', response_model=KbDocumentResponse)
async def create_kb_document(
    payload: KbDocumentCreateRequest,
    authorization: str | None = Header(default=None),
) -> KbDocumentResponse:
    owner_user_id = None
    if payload.visibility == 'private':
        owner_user_id = resolve_user_id(None, authorization)
    elif authorization:
        owner_user_id = resolve_user_id(None, authorization)

    try:
        doc = supplement_kb.add_document(
            title=payload.title,
            content=payload.content,
            owner_user_id=owner_user_id,
            source=payload.source,
            tags=payload.tags,
            visibility=payload.visibility,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KbDocumentResponse(**doc.to_payload())


@router.get('/kb/documents', response_model=list[KbDocumentResponse])
async def list_kb_documents(authorization: str | None = Header(default=None)) -> list[KbDocumentResponse]:
    user_id = None
    if authorization:
        user_id = resolve_user_id(None, authorization)
    return [KbDocumentResponse(**doc.to_payload()) for doc in supplement_kb.list_documents(user_id=user_id)]


@router.get('/kb/search', response_model=KbSearchResponse)
async def search_kb(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=5, ge=1, le=20),
    authorization: str | None = Header(default=None),
) -> KbSearchResponse:
    user_id = None
    if authorization:
        user_id = resolve_user_id(None, authorization)
    hits = supplement_kb.search(q, user_id=user_id, limit=limit)
    return KbSearchResponse(
        query=q,
        hits=[KbSearchHitResponse(**hit.to_payload()) for hit in hits],
    )


@router.post('/personal/query', response_model=PersonalQueryResponse)
async def personal_query(
    payload: PersonalQueryRequest,
    authorization: str | None = Header(default=None),
) -> PersonalQueryResponse:
    resolved_user_id = resolve_user_id(None, authorization)
    result = await chat_service.private.query_personal(resolved_user_id, payload.message)
    return PersonalQueryResponse(
        user_id=resolved_user_id,
        answer=result.answer,
        system=result.system,
        needs_relogin=result.needs_relogin,
        sources=[
            AnswerSource(
                type='private_connector',
                title='\u79c1\u4eba\u4e8b\u52a1\u67e5\u8be2',
                system=result.system,
            )
        ],
        actions=[
            ChatAction(
                type='login_required',
                system=result.system,
                needed=result.needs_relogin,
                message='\u9700\u8981\u7ed1\u5b9a\u771f\u5b9e\u6821\u56ed\u7cfb\u7edf\u767b\u5f55\u6001\u540e\u624d\u80fd\u67e5\u8be2\u4e2a\u4eba\u9884\u7ea6\u3001\u5ba1\u6279\u3001\u8bfe\u8868\u7b49\u4e8b\u52a1\u3002',
            )
        ],
    )

@router.get('/channels')
async def channels() -> dict[str, Any]:
    return {
        'channels': [
            {'channel': 'sysu_kb', 'title': '校内知识库', 'route': 'public', 'upstream': 'yiwen', 'search_source': 'sysuKB', 'cli_arg': '--search-source sysuKB'},
            {'channel': 'sysu_news', 'title': '校内资讯', 'route': 'public', 'upstream': 'yiwen', 'search_source': 'sysuSE', 'cli_arg': '--search-source sysuSE'},
            {'channel': 'web_search', 'title': '联网搜索', 'route': 'public', 'upstream': 'yiwen', 'search_source': 'internetSE', 'cli_arg': '--search-source internetSE'},
            {'channel': 'model', 'title': '模型问答', 'route': 'public', 'upstream': 'yiwen', 'search_source': 'model', 'cli_arg': '--search-source model'},
            {'channel': 'freshman_materials', 'title': '塔社新生资料包', 'route': 'public', 'upstream': 'github_tree_index', 'search_source': None, 'cli_arg': None},
            {'channel': 'private', 'title': '私人事务', 'route': 'private', 'upstream': 'private_connectors', 'search_source': None, 'cli_arg': None},
        ]
    }


@router.get('/kb/external/status')
async def external_kb_status() -> dict[str, Any]:
    return await external_rag_service.check()



@router.get('/materials/freshman/search')
async def freshman_materials_search(q: str = Query(..., min_length=1), limit: int = Query(default=8, ge=1, le=20)) -> dict[str, Any]:
    hits = await freshman_materials_service.search(q, limit=limit)
    return {
        'query': q,
        'repo': freshman_materials_service.github_repo_url,
        'hits': [hit.__dict__ for hit in hits],
    }
@router.get('/materials/freshman/status')
async def freshman_materials_status() -> dict[str, Any]:
    return freshman_materials_service.status()


@router.post('/materials/freshman/refresh')
async def freshman_materials_refresh() -> dict[str, Any]:
    try:
        return await freshman_materials_service.refresh()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
@router.get('/sources')
async def sources() -> dict[str, list[dict[str, str]]]:
    return {
        'sources': [
            {'name': 'yiwen', 'type': 'public', 'description': '逸问公共问答上游'},
            {'name': 'jwxt', 'type': 'private', 'description': '教务请假/课表等私人事务连接器'},
            {'name': 'supplement_kb', 'type': 'public', 'description': '本地补充知识占位'},
        ]
    }




@router.get('/private/capabilities')
async def private_capabilities() -> dict[str, Any]:
    return {
        'login': {
            'status': 'implemented',
            'method': 'SYSU-Anything auth workwechat per local session',
            'state_dir': '.state/private-users/<user-hash>/sysu-anything',
            'endpoints': [
                'POST /auth/private/sysu/workwechat/start',
                'GET /auth/private/sysu/workwechat/status',
                'GET /auth/private/sysu/workwechat/qr',
            ],
        },
        'capabilities': [
            {
                'name': '我的图书馆/自习室/研讨室预约',
                'system': 'libic',
                'project_status': 'partial',
                'school_connection': 'CAS -> libic refresh 已接入；个人预约列表接口仍在候选探测阶段',
                'implemented_endpoints': ['POST /auth/private/libic/refresh', 'POST /chat channel=private'],
                'sysu_anything_basis': ['libic refresh', 'libic whoami', 'libic room-types', 'libic available', 'libic reserve'],
            },
            {
                'name': '我的请假记录/请假审核进度',
                'system': 'jwxt',
                'project_status': 'wired_read_only',
                'school_connection': '自然语言已接入 SYSU-Anything jwxt leave list，只读查询当前用户请假记录/进度',
                'sysu_anything_basis': ['jwxt status', 'jwxt leave list', 'jwxt leave audit', 'jwxt leave apply preview/--confirm'],
            },
            {
                'name': '我的课表/教务状态',
                'system': 'jwxt',
                'project_status': 'wired_read_only',
                'school_connection': '自然语言已接入 SYSU-Anything today / jwxt timetable，只读查询今天课程或当前周课表',
                'sysu_anything_basis': ['jwxt status', 'today'],
            },
            {
                'name': '我的场馆预约记录',
                'system': 'gym',
                'project_status': 'wired_read_only_with_required_venue_type',
                'school_connection': '自然语言已接入 SYSU-Anything gym bookings --mine；需从问题中识别场地类型，如健身房/羽毛球',
                'sysu_anything_basis': ['gym bookings --mine'],
            },
            {
                'name': 'USC/BPM 预约/审批类事务',
                'system': 'usc/bpm',
                'project_status': 'wired_read_only',
                'school_connection': '自然语言已接入 SYSU-Anything usc tasks / usc sessions，只读查询待办、申请会话和审批记录；具体表单提交仍需二次确认',
                'sysu_anything_basis': ['usc whoami', 'usc sessions', 'usc tasks', 'usc examine-data'],
            },
            {
                'name': '勤工助学/学工事务记录',
                'system': 'xgxt',
                'project_status': 'wired_read_only',
                'school_connection': '自然语言已接入 SYSU-Anything xgxt workstudy records/resume，只读查询勤工助学申请记录和简历状态',
                'sysu_anything_basis': ['xgxt workstudy records', 'xgxt workstudy resume'],
            },
            {
                'name': '我的长假离返校登记任务',
                'system': 'xgxt',
                'project_status': 'wired_read_only',
                'school_connection': '自然语言已接入 SYSU-Anything xgxt holiday list，只读查询离返校登记任务',
                'sysu_anything_basis': ['xgxt holiday list'],
            },
            {
                'name': '雨课堂课程/作业/签到',
                'system': 'ykt',
                'project_status': 'wired_read_only_requires_ykt_login',
                'school_connection': '自然语言已接入 SYSU-Anything ykt status/courses/homework/checkin；雨课堂使用独立微信登录态，不复用 CAS',
                'sysu_anything_basis': ['ykt status', 'ykt courses', 'ykt homework list', 'ykt checkin list'],
            },
            {
                'name': '交叉探索讲座/科研项目',
                'system': 'explore',
                'project_status': 'wired_read_only_and_preview_only_actions',
                'school_connection': '自然语言已接入 SYSU-Anything explore seminar/research 列表、日历、详情；预约/申请只生成预览，不自动提交',
                'sysu_anything_basis': ['explore seminar list', 'explore seminar calendar', 'explore seminar detail', 'explore seminar reserve preview', 'explore research list', 'explore research detail', 'explore research apply preview'],
            },
            {
                'name': '就业宣讲会/招聘会/岗位',
                'system': 'career',
                'project_status': 'wired_public_read_and_private_preview_actions',
                'school_connection': '自然语言已接入 SYSU-Anything career teachin/jobfair/job 列表和详情；报名/投递只生成预览，不自动提交',
                'sysu_anything_basis': ['career teachin list/detail/signup preview', 'career jobfair list/detail/signup preview', 'career job list/detail/apply preview'],
            },
            {
                'name': '我的成绩查询',
                'system': 'jwxt',
                'project_status': 'wired_read_only_official_studentWeb',
                'school_connection': '已对齐 JWXT 官方 studentWeb stuAchievementView 页面，只读调用 checkStuStatus/getPull/list/getSortByYear/stuCreditSitlist/getPicPie；按用户自己的 JWXT 会话查询本人数据',
                'sysu_anything_basis': ['CAS/JWXT session from SYSU-Anything state-dir'],
            },
            {
                'name': '家庭经济困难认定进度',
                'system': 'xgxt/unknown',
                'project_status': 'not_confirmed',
                'school_connection': '尚未确认具体学校系统和接口，不能宣称已打通',
                'sysu_anything_basis': [],
            },
        ],
    }

@router.get('/me/private/{system}/status')
async def private_system_status(
    system: str,
    user_id: str | None = Query(default=None, min_length=1),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    resolved_user_id = resolve_user_id(None, authorization)
    if system == 'sysu':
        return private_sysu_auth.status(resolved_user_id)
    status = chat_service.private.sessions.status(resolved_user_id, system)
    if system in {'libic', 'jwxt'}:
        private_status = private_sysu_auth.status(resolved_user_id)
        status['has_cas_session'] = private_status.get('has_cas_session')
        status['has_libic_session_file'] = private_status.get('has_libic_session')
        status['has_jwxt_session_file'] = private_status.get('has_jwxt_session')
        status['effective_user_id'] = private_status.get('effective_user_id')
        status['using_single_user_fallback'] = private_status.get('using_single_user_fallback')
        status['private_login_summary'] = private_status.get('summary')
    return status


@router.get('/auth/private/{system}/start')
async def private_auth_start(
    system: str,
    user_id: str | None = Query(default=None, min_length=1),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    resolved_user_id = resolve_user_id(None, authorization)
    if system == 'sysu':
        return private_sysu_auth.status(resolved_user_id)
    if system == 'libic':
        return {
            'system': 'libic',
            'user_id': resolved_user_id,
            'status': 'login-required',
            'entry_url': 'https://libic.sysu.edu.cn/',
            'import_endpoint': '/auth/private/libic/import-sysu-anything',
            'message': '需要用户登录自己的中大账号。后端会复用 CAS -> libic /auth/address -> /authcenter -> /auth/token 建立个人 libic 会话。',
            'discovery_steps': [
                '打开 SYSU CAS 登录',
                '进入 https://libic.sysu.edu.cn/',
                '跟随 /auth/address 和 authcenter 跳转',
                '通过 /auth/token 获取 libic 站点会话',
                '在 Network 中确认我的预约/预约记录请求路径',
            ],
        }
    return {
        'system': system,
        'user_id': resolved_user_id,
        'status': 'unsupported',
        'message': '暂不支持该私人系统。',
    }

@router.post('/auth/private/libic/import-sysu-anything')
async def private_libic_import_sysu_anything(
    user_id: str | None = Query(default=None, min_length=1),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    resolved_user_id = resolve_user_id(None, authorization)
    session = chat_service.private.libic.import_sysu_anything_session(resolved_user_id)
    if not session:
        return {
            'system': 'libic',
            'user_id': resolved_user_id,
            'imported': False,
            'message': '未找到 SYSU-Anything 的 libic-session.json。需要先完成个人中大账号登录并刷新 libic 会话。',
            'expected_path': str(chat_service.private.libic.default_state_path()),
        }
    return {
        'system': 'libic',
        'user_id': resolved_user_id,
        'imported': True,
        'session': chat_service.private.sessions.status(resolved_user_id, 'libic'),
    }

@router.post('/auth/private/sysu/workwechat/start')
async def private_workwechat_start(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    resolved_user_id = resolve_user_id(None, authorization)
    try:
        return private_sysu_auth.start_workwechat_login(resolved_user_id)
    except PrivateSysuAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get('/auth/private/sysu/workwechat/status')
async def private_workwechat_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    resolved_user_id = resolve_user_id(None, authorization)
    return private_sysu_auth.status(resolved_user_id)


@router.get('/auth/private/sysu/workwechat/qr')
async def private_workwechat_qr(authorization: str | None = Header(default=None)) -> FileResponse:
    resolved_user_id = resolve_user_id(None, authorization)
    qr_file = private_sysu_auth.latest_qr_file(resolved_user_id)
    if not qr_file:
        raise HTTPException(status_code=404, detail='workwechat QR is not ready')
    return FileResponse(qr_file, media_type='image/png')


@router.post('/auth/private/sysu/jwxt/refresh')
async def private_jwxt_refresh(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    resolved_user_id = resolve_user_id(None, authorization)
    if not private_sysu_auth.has_cas_session(resolved_user_id):
        try:
            login_status = private_sysu_auth.start_workwechat_login(resolved_user_id, service_url='https://jwxt.sysu.edu.cn/jwxt/')
        except PrivateSysuAuthError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            **login_status,
            'system': 'jwxt',
            'refreshed': False,
            'pending_login': True,
            'message': '尚未绑定个人企业微信/CAS 登录态，已生成或复用企业微信二维码；扫码后会建立教务业务会话。',
        }
    try:
        output = await private_sysu_auth.run_text_for_user(resolved_user_id, 'jwxt', 'status', timeout=90.0)
    except PrivateSysuAuthError as exc:
        text = str(exc)
        if any(token in text.lower() for token in ('认证', '登录', 'unauthorized', 'unauthenticated', '401', '403', 'cas', 'cookie', 'token')):
            try:
                login_status = private_sysu_auth.start_workwechat_login(resolved_user_id, service_url='https://jwxt.sysu.edu.cn/jwxt/')
            except PrivateSysuAuthError as start_exc:
                raise HTTPException(status_code=502, detail=f'{text}; retry start failed: {start_exc}') from start_exc
            return {
                **login_status,
                'system': 'jwxt',
                'refreshed': False,
                'pending_login': True,
                'message': '教务业务会话刷新失败，已重新生成企业微信二维码；请扫码后重试。',
                'error': text,
            }
        raise HTTPException(status_code=502, detail=text) from exc
    status = private_sysu_auth.status(resolved_user_id)
    return {
        **status,
        'system': 'jwxt',
        'refreshed': True,
        'pending_login': False,
        'message': '教务业务会话已刷新；JWXT status 已通过当前用户自己的 CAS 登录态完成二段登录。',
        'jwxt_status_preview': output[:800],
    }


@router.post('/auth/private/libic/refresh')
async def private_libic_refresh(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    resolved_user_id = resolve_user_id(None, authorization)
    if not private_sysu_auth.has_cas_session(resolved_user_id):
        raise HTTPException(status_code=401, detail='missing private SYSU CAS session; scan enterprise WeChat first')
    try:
        refresh = await private_sysu_auth.run_json_for_user(resolved_user_id, 'libic', 'refresh', timeout=90.0)
    except PrivateSysuAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    chat_service.private.libic.import_sysu_anything_session(resolved_user_id)
    return {
        'system': 'libic',
        'user_id': resolved_user_id,
        'refreshed': True,
        'refresh': refresh,
        'session': chat_service.private.sessions.status(resolved_user_id, 'libic'),
    }

@router.post('/auth/{system}/start')
async def auth_start(system: str) -> dict[str, Any]:
    if system != 'yiwen':
        return {
            'system': system,
            'status': 'stub',
            'message': '当前只有 yiwen 已接入真实会话登记入口。',
        }

    return {
        'system': 'yiwen',
        'status': 'callback-auth',
        'message': '后端直接调用 SYSU-Anything chat 登录与发送命令。推荐使用 /admin/yiwen/shared/login 的 Chrome 调试导入。',
        'auth_url_endpoint': '/admin/yiwen/shared/auth-url',
        'callback_replay_endpoint': '/admin/yiwen/shared/replay-callback',
        'login_page': '/admin/yiwen/shared/login',
    }



@router.get('/me/{system}/status')
async def me_status(
    system: str,
    user_id: str | None = Query(default=None, min_length=1),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    resolved_user_id = resolve_user_id(user_id, authorization)
    if system != 'yiwen':
        return {
            'system': system,
            'status': 'unknown',
            'message': '当前还没有真实会话存储与检查逻辑。',
        }

    status = session_store.get_yiwen_status(resolved_user_id)
    status['system'] = 'yiwen'
    return status

@router.get('/official/interfaces')
async def official_interfaces() -> dict[str, Any]:
    interfaces = [
        {'key': 'bus_schedule', 'system': 'bus', 'title': '校区班车/校车时刻', 'auth_required': False, 'channel': 'sysu_kb', 'sample_prompt': '查询校车', 'upstream_basis': 'sysu-anything bus --json'},
        {'key': 'career_teachin_list', 'system': 'career', 'title': '就业宣讲会列表', 'auth_required': False, 'channel': 'sysu_kb', 'sample_prompt': '查一下最近3场宣讲会', 'upstream_basis': 'sysu-anything career teachin list --limit 3 --json'},
        {'key': 'career_job_list', 'system': 'career', 'title': '就业岗位/实习岗位列表', 'auth_required': False, 'channel': 'sysu_kb', 'sample_prompt': '查一下实习岗位5条', 'upstream_basis': 'sysu-anything career job list --limit 5 --json'},
        {'key': 'freshman_materials', 'system': 'freshman_materials', 'title': '塔社新生资料包路径检索', 'auth_required': False, 'channel': 'freshman_materials', 'sample_prompt': '军理题库在哪', 'upstream_basis': 'GitHub tree index: thinktraveller/SYSU_freshman_materials'},
        {'key': 'jwxt_timetable', 'system': 'jwxt', 'title': '本人课表/今日课程', 'auth_required': True, 'channel': 'private', 'sample_prompt': '查询今天课表', 'upstream_basis': 'sysu-anything today --state-dir <user-state> --json'},
        {'key': 'jwxt_leave', 'system': 'jwxt', 'title': '本人请假记录/审核流', 'auth_required': True, 'channel': 'private', 'sample_prompt': '查询我的请假记录', 'upstream_basis': 'sysu-anything jwxt leave list --state-dir <user-state> --json'},
        {'key': 'jwxt_leave_apply', 'system': 'jwxt', 'title': '本人请假申请预览/确认提交', 'auth_required': True, 'channel': 'private', 'sample_prompt': '申请请假：病假，2026-07-08 全天，说明发烧去校医院，附件 C:\\tmp\\proof.png', 'upstream_basis': 'sysu-anything jwxt leave apply 默认预览；只有用户明确“确认提交请假申请”才追加 --confirm'},
        {'key': 'jwxt_grade', 'system': 'jwxt', 'title': '本人成绩/绩点/学分', 'auth_required': True, 'channel': 'private', 'sample_prompt': '查询我2025-2026学年第二学期主修成绩', 'upstream_basis': '本项目补充：复用 SYSU-Anything CAS/JWXT 会话，对齐官方 studentWeb stuAchievementView；调用 GET /achievement-manage/score-check/checkStuStatus、/getPull、/list、/getSortByYear、/stuCreditSitlist、/getPicPie'},
        {'key': 'jwxt_exam', 'system': 'jwxt', 'title': '本人考试信息/期末考试安排', 'auth_required': True, 'channel': 'private', 'sample_prompt': '查询我的期末考试课表', 'upstream_basis': '本项目补充：复用 SYSU-Anything CAS/JWXT 会话，直接调用 GET /schedule/agg/commonScheduleExamTime/queryExamWeekName 与 POST /examination-manage/classroomResource/queryStuEaxmInfo；只有官方接口成功响应才返回个人考试数据'},
        {'key': 'gym_available', 'system': 'gym', 'title': '体育场馆空位/预约预览', 'auth_required': True, 'channel': 'private', 'sample_prompt': '查询明天羽毛球空位', 'upstream_basis': 'sysu-anything gym available --venue-type 羽毛球 --json'},
        {'key': 'usc_classroom_rooms', 'system': 'usc', 'title': 'USC/BPM 可用课室', 'auth_required': True, 'channel': 'private', 'sample_prompt': '查询明天珠海校区第1-2节可用课室', 'upstream_basis': 'sysu-anything usc classroom rooms --date <date> --section-start 1 --section-end 2 --campus 珠海校区 --json'},
        {'key': 'xgxt_workstudy_records', 'system': 'xgxt', 'title': '勤工助学岗位/本人申请记录', 'auth_required': True, 'channel': 'private', 'sample_prompt': '查询我的勤工助学申请记录', 'upstream_basis': 'sysu-anything xgxt workstudy records --state-dir <user-state> --json'},
        {'key': 'ykt_homework', 'system': 'ykt', 'title': '雨课堂作业/签到/课程', 'auth_required': True, 'channel': 'private', 'sample_prompt': '查询雨课堂作业', 'upstream_basis': 'sysu-anything ykt homework list --state-dir <user-state> --json', 'login_note': '雨课堂使用独立微信登录态，不复用中大 CAS。'},
        {'key': 'explore_seminar', 'system': 'explore', 'title': '交叉探索讲座/科研项目', 'auth_required': True, 'channel': 'private', 'sample_prompt': '查询讲座列表', 'upstream_basis': 'sysu-anything explore seminar list --kind latest --json'},
    ]
    return {
        'summary': '当前项目已经对齐的官网功能接口目录。auth_required=false 可直接测试；auth_required=true 必须使用用户自己的登录态。',
        'skills': [
            {'name': 'microsoft/playwright-cli@playwright-cli', 'purpose': '浏览器自动化与官网网络请求观察', 'install_status': '已搜索到；安装时 GitHub clone 被重置，未完成安装'},
            {'name': 'currents-dev/playwright-best-practices-skill@playwright-best-practices', 'purpose': 'Playwright 稳定测试写法参考', 'install_status': '已搜索到；未安装'},
            {'name': 'github/awesome-copilot@playwright-explore-website', 'purpose': '探索网站流程并生成可回放步骤', 'install_status': '已搜索到；未安装'},
        ],
        'interfaces': interfaces,
    }


@router.post('/admin/official/probe')
async def official_probe(
    include_private: bool = Query(default=False),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    catalog = (await official_interfaces())['interfaces']
    if authorization:
        user_id = resolve_user_id(None, authorization)
    else:
        user_id = 'official-probe-user'
    selected = [item for item in catalog if include_private or not item.get('auth_required')]
    results = []
    for item in selected:
        started_at = time.time()
        try:
            response = await chat_service.handle(ChatRequest(
                user_id=user_id,
                message=item['sample_prompt'],
                channel=item['channel'],
                chat_id='official-probe',
            ))
            source_systems = [source.system for source in response.sources]
            action = response.actions[0] if response.actions else None
            needs_login = bool(action.needed) if action else False
            if needs_login:
                status = 'login_required'
            elif item['system'] in source_systems:
                status = 'ok'
            else:
                status = 'system_mismatch'
            results.append({
                'key': item['key'],
                'title': item['title'],
                'system': item['system'],
                'status': status,
                'auth_required': item['auth_required'],
                'sample_prompt': item['sample_prompt'],
                'source_systems': source_systems,
                'action': action.model_dump() if action else None,
                'answer_preview': response.answer[:500],
                'elapsed_ms': int((time.time() - started_at) * 1000),
            })
        except Exception as exc:
            results.append({
                'key': item['key'],
                'title': item['title'],
                'system': item['system'],
                'status': 'error',
                'auth_required': item['auth_required'],
                'sample_prompt': item['sample_prompt'],
                'error': str(exc),
                'elapsed_ms': int((time.time() - started_at) * 1000),
            })
    return {
        'include_private': include_private,
        'user_id': user_id,
        'total': len(results),
        'ok': sum(1 for item in results if item['status'] == 'ok'),
        'login_required': sum(1 for item in results if item['status'] == 'login_required'),
        'errors': sum(1 for item in results if item['status'] == 'error'),
        'results': results,
    }












