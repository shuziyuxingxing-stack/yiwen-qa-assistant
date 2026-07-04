# 中大逸问问答助手

中大逸问问答助手是一个面向普通用户的中山大学问答网关。它把官方逸问公共问答、个人校园事务查询、辅助知识库和中大真题资料路径检索放在同一个网页入口里，同时通过 FastAPI 暴露后端接口，方便嵌入资料站、门户页、机器人或其他校园服务。

项目的核心目标不是替代官方系统，而是降低使用门槛：普通用户不需要找 token、复制 cookie、安装脚本或理解抓包流程；管理员完成一次共享逸问账号登录后，普通公共问答即可统一复用；涉及本人数据的查询则必须由用户自己扫码绑定个人中大账号会话。

## 项目目的

本项目主要解决四类需求：

1. 让普通用户以网页聊天的方式使用官方逸问的公共问答能力。
2. 把“校内知识库、校内资讯、联网搜索、模型问答”等官方逸问栏目显式拆开，避免所有问题都混到同一个模式里。
3. 为私人事务查询保留独立入口，让用户登录自己的企业微信/CAS 后查询本人数据，例如课表、成绩、请假、预约、审批等。
4. 把外部资料目录检索做成一个可嵌入的后端服务，例如“中大真题资料查询”栏目可同时检索 GitHub 新生资料包和 arxiv.jaison.ink 的资料入口。

## 登录和登录态保存

本项目区分三种登录概念：本地会话、公共逸问共享登录、个人校园系统登录。

### 本地会话

前端首次打开时会自动调用：

```http
POST /auth/local
```

后端生成一个本地 `access_token`，前端保存在浏览器 `localStorage` 里。这个 token 只用于区分当前浏览器会话、保存聊天上下文和绑定私人登录态，不是中大账号，也不是逸问官方 token。

用户点击“重建会话”会创建新的本地会话。不同本地会话原则上隔离聊天和私人绑定状态。

### 公共逸问共享登录

公共问答使用管理员维护的共享逸问账号。管理员打开：

```text
http://127.0.0.1:8013/admin/yiwen/shared/login
```

推荐流程：

1. 点击“打开官方逸问登录窗口”。
2. 在弹出的官方逸问页面完成管理员共享账号登录。
3. 回到管理页，点击“从浏览器导入登录态”。
4. 点击“发送真实测试问题”，确认公共问答链路可用。

后端通过 SYSU-Anything 的 `chat import-chrome-debug` 导入官方逸问登录态，并把状态保存到：

```text
.state/sysu-anything-chat/chat-auth.json
.state/sysu-anything-chat/chat-session.json
```

服务启动后会自动运行保活检测。公共四个官方栏目只需要管理员共享登录态可用，普通用户不需要登录自己的中大账号。

### 个人校园系统登录

私人事务必须使用用户自己的中大账号会话。用户在前端点击“企业微信扫码绑定”，后端会调用 SYSU-Anything 的企业微信/CAS 登录流程，生成二维码并等待用户扫码。

个人登录态按本地用户隔离保存，大致路径为：

```text
.state/private-users/<user-hash>/sysu-anything/session.json
.state/private-users/<user-hash>/sysu-anything/jwxt-session.json
.state/private-users/<user-hash>/sysu-anything/libic-session.json
.state/private-users/<user-hash>/sysu-anything/usc-bpm-session.json
.state/private-users/<user-hash>/sysu-anything/xgxt-session.json
```

什么时候需要登录：

- 不需要个人登录：校内知识库、校内资讯、联网搜索、模型问答、中大真题资料查询。
- 需要管理员共享逸问登录：校内知识库、校内资讯、联网搜索、模型问答。
- 需要用户个人企业微信/CAS 登录：私人事务。
- 不需要逸问登录：中大真题资料查询，它直接查公开资料目录和公开站点 API。

## 六个前端栏目

前端聊天页左下角有栏目切换。当前六个栏目含义如下。

### 校内知识库

调用官方逸问的“校内知识库”范围，适合公开校内制度、办事流程、校园卡、图书馆规则等问题。后端通过 SYSU-Anything 调用逸问，并使用 `search_source=sysuKB`。

### 校内资讯

调用官方逸问的“校内资讯”范围，适合通知、新闻、近期安排、校内动态等问题。后端使用 `search_source=sysuSE`。

### 联网搜索

调用官方逸问的“联网搜索”范围，适合需要外部网页或相对实时信息的问题。后端使用 `search_source=internetSE`。

### 模型问答

调用官方逸问的“模型问答”范围，适合通用写作、解释、推理、改写等不强依赖检索的问题。后端使用 `search_source=model`。

### 中大真题资料查询

用于资料路径和资料入口发现，不下载资料，也不总结资料内容。当前同时检索：

- GitHub `thinktraveller/SYSU_freshman_materials` 仓库文件树。
- `arxiv.jaison.ink` 的公开 `/api/materials` 和 `/api/packages` 接口。

该栏目会返回多页结果，尽量让两个来源都有展示。例如用户问“高数真题”“习概”“军理题库”，系统会给出可能的文件路径、资料标题和跳转链接。

### 私人事务

用于本人数据和本人操作，例如：

- 我的课表、成绩、考试安排。
- 我的请假记录和请假申请预览。
- 我的自习室、图书馆空间、场馆或课室预约状态。
- 我的审批、勤工助学、离返校登记等个人事务。

私人事务不能使用管理员共享逸问账号。它必须使用当前用户自己的企业微信/CAS 登录态，并由后端连接具体校园系统或 SYSU-Anything 连接器获取真实数据。

## 页面和接口地址

本地默认服务地址：

```text
http://127.0.0.1:8013
```


### 前端页面

```text
GET /
GET /static/index.html
```

默认聊天入口：

```text
http://127.0.0.1:8013/
```

### 管理员页面

共享逸问登录和保活检测页面：

```text
http://127.0.0.1:8013/admin/yiwen/shared/login
```

管理员状态接口：

```http
GET /admin/yiwen/shared/status
POST /admin/yiwen/shared/chrome/start
POST /admin/yiwen/shared/chrome/import
POST /admin/yiwen/shared/keepalive
POST /admin/yiwen/shared/send-test
GET /admin/yiwen/shared/login.json
```

### 后端开放接口

健康检查：

```http
GET /health
```

本地登录和用户信息：

```http
POST /auth/local
POST /auth/login
GET /me
```

统一问答接口：

```http
POST /chat
Authorization: Bearer <local_access_token>
Content-Type: application/json

{
  "message": "高数真题",
  "channel": "freshman_materials",
  "model": "V3",
  "search_source": "freshman_materials"
}
```

栏目列表：

```http
GET /channels
```

中大真题资料查询：

```http
GET /materials/sysu/search?q=高数真题&limit=24
GET /materials/sysu/status
POST /materials/sysu/refresh
```

旧路径仍保留兼容：

```http
GET /materials/freshman/search
GET /materials/freshman/status
POST /materials/freshman/refresh
```

内置和外部辅助知识库：

```http
POST /kb/documents
GET /kb/documents
GET /kb/search
GET /kb/external/status
```

私人事务登录和状态：

```http
POST /auth/private/sysu/workwechat/start
GET /auth/private/sysu/workwechat/status
GET /auth/private/sysu/workwechat/qr
POST /auth/private/sysu/jwxt/refresh
GET /me/private/{system}/status
POST /personal/query
```

已对齐或正在探索的官方功能目录：

```http
GET /official/interfaces
POST /admin/official/probe
```

## 本地运行

安装依赖后启动：

```powershell
cd D:\Downloads\逸问问答助手
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8013
```

常用环境变量：

```text
SYSU_ANYTHING_CLI=<sysu-anything.js 路径>
SYSU_ANYTHING_NODE=node
PRIVATE_SYSU_SERVICE_URL=https://jwxt.sysu.edu.cn/jwxt/
PRIVATE_SYSU_SINGLE_USER_FALLBACK=1
```


## 致谢

本项目的登录和校园系统接入思路参考了 SYSU-Anything，资料查询栏目使用了 SYSU freshman materials 和 arxiv.jaison.ink 的公开资料来源。

- SYSU-Anything: https://github.com/qybaihe/SYSU-Anything
- SYSU freshman materials: https://github.com/thinktraveller/SYSU_freshman_materials
- arxiv.jaison.ink: https://arxiv.jaison.ink

