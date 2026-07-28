# 中大逸问问答助手

这是一个本地运行的中山大学问答入口，把以下能力放在同一个网页中：

- 官方逸问：校内知识库、校内资讯、联网搜索、模型问答。
- 私人事务：课表、成绩、考试、请假及其他个人校园系统查询。
- 中大真题资料查询：检索公开资料目录和下载入口。
- 逸问历史对话：登录同一个逸问账号后读取该账号的历史会话。

项目不是中山大学官方服务。官方页面、账号和上游接口的可用性以学校系统为准。

## 本地部署

### 环境要求

- Windows 10/11
- Python 3.10 或更高版本
- Node.js 18 或更高版本（建议安装当前 LTS）
- Chrome 或 Edge
- PowerShell

SYSU-Anything 会作为项目内的 npm 依赖自动安装，不需要全局安装。

### 安装

```powershell
git clone https://github.com/shuziyuxingxing-stack/yiwen-qa-assistant.git
cd yiwen-qa-assistant
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

安装脚本会：

1. 创建 `.venv` Python 虚拟环境。
2. 安装 `requirements.txt`。
3. 在 `node_modules` 中安装固定版本的 `sysu-anything`。
4. 首次运行时由 `.env.example` 创建本地 `.env`。

### 启动

```powershell
.\start.ps1
```

默认地址：

```text
http://127.0.0.1:8013
```

指定端口或不自动打开浏览器：

```powershell
.\start.ps1 -Port 8014
.\start.ps1 -NoBrowser
```

服务仅监听 `127.0.0.1`。登录态保存在本机 `.state` 目录。

## 登录说明

项目内部有三类状态：

1. **助手本地会话**：区分本机浏览器用户、保存聊天上下文。
2. **官方逸问授权**：用于四个官方公共问答栏目和逸问历史记录。
3. **企业微信/CAS 会话**：用于成绩、课表等私人校园事务。

### 官方逸问

本地部署会为当前用户启动独立的 Chrome/Edge 配置目录和动态 localhost 调试端口。用户只需在官方逸问页面完成授权；前端自动轮询本地后端，后端通过 CDP 和 SYSU-Anything 读取 token 并写入当前用户的 `.state`。token 不返回给前端，也不需要用户复制、粘贴或运行控制台脚本。

逸问使用独立的 `qwweb` 授权链。仅拥有 CAS 或教务系统会话不保证可以生成逸问 token。若跳转到 `appgw.sysu.edu.cn` 后出现 `Access Forbidden`，请在校园网或学校 VPN 环境下重试；这可能是网关对公网出口的访问限制。

同一个逸问账号授权后，历史记录接口会读取该账号在上游保存的会话：

```http
GET /me/yiwen/chats?limit=12
GET /me/yiwen/chats/{chat_id}/messages?limit=80
```

### 私人事务

在网页中点击企业微信扫码绑定。状态按本地用户隔离保存在：

```text
.state/private-users/<user-hash>/sysu-anything/
```

其中可能包含：

```text
session.json
jwxt-session.json
libic-session.json
usc-bpm-session.json
xgxt-session.json
chat-auth.json
chat-session.json
```


## 功能栏目

### 校内知识库

调用官方逸问的 `sysuKB` 检索范围，适合制度、办事流程和校内公共信息。

### 校内资讯

调用官方逸问的 `sysuSE` 检索范围，适合通知、新闻和近期安排。

### 联网搜索

调用官方逸问的 `internetSE` 检索范围。

### 模型问答

调用官方逸问的 `model` 范围。

### 中大真题资料查询

检索以下公开来源：

- [SYSU freshman materials](https://github.com/thinktraveller/SYSU_freshman_materials)
- [arxiv.jaison.ink](https://arxiv.jaison.ink)

该栏目只提供资料路径和入口，不下载或总结资料内容。搜索会优先匹配课程名、真题、试卷、考试、答案等关键词，降低无关仓库文件的排序。

### 私人事务

根据问题路由到当前用户自己的校园系统会话，例如：

- 成绩、课表、考试安排。
- 请假记录和申请预览。
- 图书馆空间、场馆或课室预约。
- 审批、勤工助学和离返校记录。

## 配置

本地配置位于 `.env`。通常无需修改 SYSU-Anything 路径，程序会自动使用：

```text
node_modules/sysu-anything/bin/sysu-anything.js
```

主要配置：

```dotenv
SYSU_ANYTHING_NODE=node
PRIVATE_SYSU_SERVICE_URL=https://jwxt.sysu.edu.cn/jwxt/
PRIVATE_SYSU_SINGLE_USER_FALLBACK=0
YIWEN_CHROME_DEBUG_PORT=9222
YIWEN_AUTO_IMPORT_CHROME=1
YIWEN_KEEPALIVE_SECONDS=300
FRESHMAN_MATERIALS_CACHE_TTL_SECONDS=86400
EXTERNAL_RAG_ENABLED=0
```

若需要使用自定义 SYSU-Anything，可设置：

```dotenv
SYSU_ANYTHING_CLI=C:\path\to\sysu-anything.js
```

## 手动启动

完成 `setup.ps1` 后也可以直接运行：

```powershell
$env:SYSU_ANYTHING_CLI = (Resolve-Path ".\node_modules\sysu-anything\bin\sysu-anything.js")
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8013
```

## 主要接口

```http
GET  /health
POST /auth/local
GET  /me
GET  /channels
POST /chat

GET  /me/yiwen/status
GET  /me/yiwen/chats
GET  /me/yiwen/chats/{chat_id}/messages

POST /auth/private/sysu/workwechat/start
GET  /auth/private/sysu/workwechat/status
GET  /auth/private/sysu/workwechat/qr
POST /auth/private/sysu/jwxt/refresh
POST /personal/query

GET  /materials/sysu/search
GET  /materials/sysu/status
POST /materials/sysu/refresh
```

统一问答示例：

```http
POST /chat
Authorization: Bearer <local_access_token>
Content-Type: application/json

{
  "message": "程序设计真题",
  "channel": "freshman_materials",
  "model": "V3",
  "search_source": "freshman_materials"
}
```


## SYSU-Anything 的关系

本项目使用 SYSU-Anything 作为校园系统驱动层，负责 CAS/企业微信登录、会话持久化、教务连接以及逸问发送和历史记录操作。

- [SYSU-Anything](https://github.com/qybaihe/SYSU-Anything)

##其余公益项目
如果您在浏览本项目时，希望查找中山大学校内其他公益项目，欢迎加入微信群：鸭大公益项目
