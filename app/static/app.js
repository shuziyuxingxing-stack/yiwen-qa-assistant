const TOKEN_KEY = "yiwenGatewayAccessToken";
const USER_ID_KEY = "yiwenGatewayUserId";
const CHAT_ID_PREFIX = "yiwenGatewayChatId:";
const CHANNEL_KEY = "yiwenGatewayChannel";

const CHANNELS = {
  sysu_kb: { title: "校内知识库", desc: "使用逸问官方“校内知识库”范围回答。", searchSource: "sysuKB", hint: "当前栏目：校内知识库。适合问校内制度、办事规则、校园卡流程、图书馆预约规则等公开信息。" },
  sysu_news: { title: "校内资讯", desc: "使用逸问官方“校内资讯”范围回答。", searchSource: "sysuSE", hint: "当前栏目：校内资讯。适合问通知、新闻、近期安排。" },
  web_search: { title: "联网搜索", desc: "使用逸问官方“联网搜索”范围回答。", searchSource: "internetSE", hint: "当前栏目：联网搜索。适合问需要外部网页或实时信息的问题。" },
  model: { title: "模型问答", desc: "使用逸问官方“模型问答”范围回答。", searchSource: "model", hint: "当前栏目：模型问答。适合通用推理、写作、解释类问题。" },
  private: { title: "私人事务", desc: "使用用户自己的中大账号会话查询本人事务。", searchSource: "private", hint: "当前栏目：私人事务。仅用于我的预约、我的请假进度、我的审批状态、我的成绩/课表等本人数据。" },
  freshman_materials: { title: "中大真题资料查询", desc: "检索塔社 GitHub 新生资料包和破壁计划 arxiv.jaison.ink 的真题/资料路径。", searchSource: "freshman_materials", hint: "当前栏目：中大真题资料查询。适合问高数真题、军理题库、课程资料等资源在哪。" },
};

let activeChannel = localStorage.getItem(CHANNEL_KEY) || "sysu_kb";
if (!CHANNELS[activeChannel]) activeChannel = "sysu_kb";
let privateStatusTimer = null;
let privateQrObjectUrl = null;

const userIdInput = document.getElementById("user-id");
const loginButton = document.getElementById("login-button");
const identityStatus = document.getElementById("identity-status");
const chatForm = document.getElementById("chat-form");
const messageInput = document.getElementById("message");
const messages = document.getElementById("messages");
const sendButton = document.getElementById("send-button");
const clearButton = document.getElementById("clear-button");
const publicStatus = document.getElementById("public-status");
const privateStatus = document.getElementById("private-status");
const privateLoginButton = document.getElementById("private-login-button");
const privateRefreshButton = document.getElementById("private-refresh-button");
const privateAuthPanel = document.getElementById("private-auth-panel");
const privateQr = document.getElementById("private-qr");
const privateAuthStatus = document.getElementById("private-auth-status");
const channelTitle = document.getElementById("channel-title");
const channelDesc = document.getElementById("channel-desc");
const composerHint = document.getElementById("composer-hint");
const channelPicker = document.getElementById("channel-picker");
const channelToggle = document.getElementById("channel-toggle");
const channelMenu = document.getElementById("channel-menu");
const channelCurrent = document.getElementById("channel-current");
const channelOptions = Array.from(document.querySelectorAll(".channel-option"));

channelOptions.forEach((option) => {
  if (!CHANNELS[option.dataset.channel]) {
    console.error("Unknown channel option", option.dataset.channel);
  }
});

function currentUserId() {
  return localStorage.getItem(USER_ID_KEY) || "";
}

function chatIdKey(userId = currentUserId(), channel = activeChannel) {
  return `${CHAT_ID_PREFIX}${userId}:${channel}`;
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function getChatId() {
  if (activeChannel === "private") return "";
  return localStorage.getItem(chatIdKey()) || "";
}

function setChatId(chatId) {
  if (chatId && activeChannel !== "private") localStorage.setItem(chatIdKey(), chatId);
}

function clearChatId() {
  localStorage.removeItem(chatIdKey());
}

function setIdentity(userId, token) {
  localStorage.setItem(USER_ID_KEY, userId);
  localStorage.setItem(TOKEN_KEY, token);
  userIdInput.value = userId;
}

function clearIdentityToken() {
  localStorage.removeItem(TOKEN_KEY);
}

function authHeaders(extra = {}) {
  const token = getToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

function formatTimestamp(value) {
  if (!value) return "";
  const millis = typeof value === "number" ? value * 1000 : Date.parse(value);
  if (!Number.isFinite(millis)) return "";
  return new Date(millis).toLocaleString("zh-CN", { hour12: false });
}

function applyChannel(channel) {
  activeChannel = CHANNELS[channel] ? channel : "sysu_kb";
  localStorage.setItem(CHANNEL_KEY, activeChannel);
  const config = CHANNELS[activeChannel];
  channelTitle.textContent = config.title;
  channelDesc.textContent = config.desc;
  composerHint.textContent = config.hint;
  channelCurrent.textContent = config.title;
  channelOptions.forEach((option) => option.classList.toggle("active", option.dataset.channel === activeChannel));
  messageInput.placeholder = activeChannel === "private"
    ? "输入私人事务，例如：查询我预约的自习室、查看我的请假进度"
    : (activeChannel === "freshman_materials"
      ? "输入资料关键词，例如：高数真题、军理题库在哪"
      : `在“${config.title}”中提问`);
}

function addMessage(role, content, meta = "") {
  const item = document.createElement("article");
  item.className = `message ${role}`;

  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = content;
  item.appendChild(body);

  if (meta) {
    const foot = document.createElement("div");
    foot.className = "message-meta";
    foot.textContent = meta;
    item.appendChild(foot);
  }

  messages.appendChild(item);
  messages.scrollTop = messages.scrollHeight;
}

async function ensureLogin(force = false) {
  const existingToken = getToken();
  const existingUser = currentUserId();
  if (!force && existingToken && existingUser) {
    userIdInput.value = existingUser;
    identityStatus.textContent = `本机会话：${existingUser}`;
    return existingToken;
  }

  identityStatus.textContent = "正在创建本机会话...";
  const response = await fetch("/auth/local", { method: "POST" });
  const data = await response.json();
  if (!response.ok || !data.access_token) {
    throw new Error(data.detail || "本机会话创建失败");
  }
  setIdentity(data.user_id, data.access_token);
  identityStatus.textContent = `本机会话：${data.user_id}`;
  return data.access_token;
}

async function fetchPrivateSysuStatus() {
  const token = getToken();
  if (!token) return null;
  const response = await fetch("/auth/private/sysu/workwechat/status", { headers: authHeaders() });
  if (response.status === 401) {
    clearIdentityToken();
    await ensureLogin(true);
    return fetchPrivateSysuStatus();
  }
  if (!response.ok) throw new Error("私人登录状态检测失败");
  return response.json();
}


async function loadPrivateQr() {
  const response = await fetch(`/auth/private/sysu/workwechat/qr?t=${Date.now()}`, { headers: authHeaders() });
  if (!response.ok) return;
  const blob = await response.blob();
  if (privateQrObjectUrl) URL.revokeObjectURL(privateQrObjectUrl);
  privateQrObjectUrl = URL.createObjectURL(blob);
  privateQr.src = privateQrObjectUrl;
}
function renderPrivateSysuStatus(status) {
  if (!status) return;
  const hasJwxtSession = Boolean(status.has_jwxt_session_file || status.has_jwxt_session);
  if (status.has_cas_session && hasJwxtSession) {
    privateStatus.textContent = "教务会话已生成";
  } else if (status.has_cas_session) {
    privateStatus.textContent = "已绑定，待刷新教务";
  } else {
    privateStatus.textContent = "需要个人登录";
  }

  const details = [];
  if (status.has_cas_session) details.push("企业微信/CAS 已绑定");
  if (hasJwxtSession) {
    const updated = formatTimestamp(status.jwxt_session_updated_at);
    details.push(updated ? `JWXT 教务会话已生成，更新时间：${updated}` : "JWXT 教务会话已生成");
  }
  privateAuthStatus.textContent = details.length ? `${details.join("；")}。` : (status.summary || "等待个人企业微信登录。");

  if (status.qr_ready) {
    privateAuthPanel.hidden = false;
    void loadPrivateQr();
  }
  if (status.has_cas_session && privateStatusTimer) {
    clearInterval(privateStatusTimer);
    privateStatusTimer = null;
  }
}

async function refreshStatus() {
  try {
    const yiwen = await fetch("/admin/yiwen/shared/status").then((r) => r.json());
    const summary = yiwen.summary || {};
    publicStatus.textContent = summary.alive_state === "存活" ? "已连接逸问" : (summary.login_state || "未配置");
  } catch (err) {
    publicStatus.textContent = "检测失败";
  }

  try {
    const status = await fetchPrivateSysuStatus();
    renderPrivateSysuStatus(status);
  } catch (err) {
    privateStatus.textContent = "需要个人登录";
  }
}

function renderChatResponse(data) {
  if (data.chat_id) setChatId(data.chat_id);
  const sourceText = (data.sources || [])
    .map((source) => source.title || source.system || source.type)
    .filter(Boolean)
    .join("、");
  const mode = activeChannel === "private" ? "私人事务" : CHANNELS[activeChannel].title;
  const meta = `${mode}${sourceText ? ` | ${sourceText}` : ""}`;
  const pages = Array.isArray(data.answer_pages) && data.answer_pages.length
    ? data.answer_pages
    : [data.answer || "没有返回内容。"];
  pages.forEach((page, index) => {
    const pageMeta = pages.length > 1 ? `${meta} · 第 ${index + 1}/${pages.length} 页` : meta;
    addMessage("assistant", page, pageMeta);
  });

  const needsAction = (data.actions || []).find((action) => action.needed);
  if (needsAction) {
    let detail = needsAction.message || "需要补充授权。";
    if (needsAction.system === "jwxt" && ["refresh_business_session", "refresh_jwxt_session"].includes(needsAction.type)) {
      detail = `${detail} 请点击左侧“刷新教务会话”；如出现二维码，请用企业微信扫码确认。`;
    } else if (needsAction.system === "libic" || needsAction.type === "bind_sysu_account") {
      detail = `${detail} 请先点击左侧“企业微信扫码绑定”。`;
    }
    addMessage("notice", detail, needsAction.system || "系统提示");
  }
  refreshStatus();
}

async function postChat(message) {
  await ensureLogin();
  const config = CHANNELS[activeChannel];
  const body = () => JSON.stringify({
    message,
    channel: activeChannel,
    chat_id: getChatId() || undefined,
    model: "V3",
    search_source: config.searchSource,
  });

  let response = await fetch("/chat", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json; charset=utf-8" }),
    body: body(),
  });
  let data = await response.json();
  if (response.status === 401 && data.detail === "invalid access token") {
    clearIdentityToken();
    await ensureLogin(true);
    response = await fetch("/chat", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json; charset=utf-8" }),
      body: body(),
    });
    data = await response.json();
  }
  if (!response.ok) {
    throw new Error(data.detail || "请求失败");
  }
  if (data.chat_id) setChatId(data.chat_id);
  return data;
}

function setChannelMenuOpen(open) {
  channelMenu.classList.toggle("open", open);
  channelToggle.classList.toggle("open", open);
  channelToggle.setAttribute("aria-expanded", open ? "true" : "false");
}

async function startPrivateLogin() {
  await ensureLogin();
  privateAuthPanel.hidden = false;
  privateAuthStatus.textContent = "正在生成企业微信二维码...";
  const response = await fetch("/auth/private/sysu/workwechat/start", { method: "POST", headers: authHeaders() });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "企业微信登录启动失败");
  renderPrivateSysuStatus(data);
  if (!privateStatusTimer) {
    privateStatusTimer = setInterval(async () => {
      try {
        renderPrivateSysuStatus(await fetchPrivateSysuStatus());
      } catch (err) {
        privateAuthStatus.textContent = err.message || String(err);
      }
    }, 2000);
  }
}

async function refreshPrivateJwxt() {
  await ensureLogin();
  privateAuthPanel.hidden = false;
  privateAuthStatus.textContent = "正在刷新 JWXT 教务会话...";
  const response = await fetch("/auth/private/sysu/jwxt/refresh", { method: "POST", headers: authHeaders() });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "刷新教务会话失败");
  renderPrivateSysuStatus(data);
  if (data.refreshed) {
    privateAuthStatus.textContent = data.message || "JWXT 教务会话已刷新，可重试私人事务问题。";
  } else if (data.pending_login) {
    privateAuthStatus.textContent = data.message || "请使用企业微信扫码确认。";
  }
  await refreshStatus();
}

channelToggle.addEventListener("click", () => {
  setChannelMenuOpen(!channelMenu.classList.contains("open"));
});

channelOptions.forEach((option) => {
  option.addEventListener("click", () => {
    applyChannel(option.dataset.channel);
    setChannelMenuOpen(false);
  });
});

document.addEventListener("click", (event) => {
  if (!channelPicker.contains(event.target)) setChannelMenuOpen(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setChannelMenuOpen(false);
});

loginButton.addEventListener("click", async () => {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_ID_KEY);
    messages.innerHTML = "";
    await ensureLogin(true);
    await refreshStatus();
  } catch (err) {
    identityStatus.textContent = err.message;
  }
});

privateLoginButton.addEventListener("click", async () => {
  privateLoginButton.disabled = true;
  try {
    await startPrivateLogin();
  } catch (err) {
    privateAuthPanel.hidden = false;
    privateAuthStatus.textContent = err.message || String(err);
  } finally {
    privateLoginButton.disabled = false;
  }
});

privateRefreshButton.addEventListener("click", async () => {
  privateRefreshButton.disabled = true;
  try {
    await refreshPrivateJwxt();
  } catch (err) {
    privateAuthPanel.hidden = false;
    privateAuthStatus.textContent = err.message || String(err);
  } finally {
    privateRefreshButton.disabled = false;
  }
});

clearButton.addEventListener("click", () => {
  messages.innerHTML = "";
  clearChatId();
  messageInput.focus();
});

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = messageInput.value.trim();
  if (!message) return;

  addMessage("user", message, CHANNELS[activeChannel].title);
  messageInput.value = "";
  sendButton.disabled = true;
  sendButton.textContent = "发送中";

  try {
    const data = await postChat(message);
    renderChatResponse(data);
  } catch (err) {
    addMessage("notice", err.message || String(err), "请求失败");
  } finally {
    sendButton.disabled = false;
    sendButton.textContent = "发送";
    messageInput.focus();
  }
});

messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    chatForm.requestSubmit();
  }
});

(function init() {
  const savedUser = localStorage.getItem(USER_ID_KEY);
  if (savedUser) userIdInput.value = savedUser;
  applyChannel(activeChannel);
  ensureLogin().then(refreshStatus).catch((err) => {
    identityStatus.textContent = err.message;
  });
  addMessage("assistant", "公共问题请使用官方四个栏目；私人事务只用于查询本人数据，需要先在左侧完成企业微信扫码绑定。", "系统");
})();







