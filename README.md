# 逸问问答助手

本项目是一个本地/内网可部署的逸问问答后端和网页助手。

当前主线已经改为直接复用 `sysu-anything` 的 Chat 能力：

- 公共问答：后端调用 `sysu-anything chat send --json`，使用管理员共享逸问账号。
- 共享登录：管理员通过 `/admin/yiwen/shared/login` 启动官方逸问登录窗口，再用 `sysu-anything chat import-chrome-debug` 导入登录态。
- 普通用户：只在助手网页里输入自己的本地用户标识并提问，不需要 token、cookie、控制台脚本、Tampermonkey 或手工抓包。
- 后端嵌入：其他服务可调用 `/auth/login` 获取本地访问令牌，再调用 `/chat`。
- 私人事务：保留独立连接器方向，用户自己的中大账号会话用于查询预约、审批、课表等个人数据；当前 libic 连接器仍处于接入/摸索阶段。

## 当前真实链路

### 公共问答

公共问答不再走自写 cookie/token 桥接逻辑，而是后端包装 SYSU-Anything CLI：

```text
node .../sysu-anything.js chat send --message <问题> --state-dir .state/sysu-anything-chat --json
```

登录态存放在：

```text
.state/sysu-anything-chat/chat-auth.json
.state/sysu-anything-chat/chat-session.json
```

### 管理员共享账号登录

打开：

```text
http://127.0.0.1:8013/admin/yiwen/shared/login
```

推荐流程：

1. 点击“打开官方逸问登录窗口”。
2. 在弹出的官方逸问页面完成管理员共享账号登录。
3. 回到管理页，点击“从浏览器导入登录态”。
4. 点击“发送真实测试问题”，确认公共问答链路可用。

这个流程调用的是 SYSU-Anything 自带的：

```text
sysu-anything chat import-chrome-debug --json
```

页面也保留了 SYSU-Anything 的原生 callback 回放包装：

```text
sysu-anything chat auth-url --json
sysu-anything chat replay-callback --url <callback-url> --json
```

但如果现网要求原始企业微信/浏览器上下文，单独 callback URL 可能无法完成回放，此时应使用 Chrome 调试导入。

## 常用接口

健康检查：

```http
GET /health
```

共享逸问状态：

```http
GET /admin/yiwen/shared/status
```

公共问答真实测试：

```http
POST /admin/yiwen/shared/send-test
```

本地用户进入：

```http
POST /auth/login
Content-Type: application/json

{
  "user_id": "demo-user",
  "display_name": "张同学"
}
```

统一问答：

```http
POST /chat
Authorization: Bearer <本地 access_token>
Content-Type: application/json

{
  "message": "请问门诊什么时候开门",
  "model": "V3",
  "search_source": "sysuKB"
}
```

## 私人事务方向

私人事务不能使用管理员共享逸问账号，因为预约、审批、课表等数据属于用户本人。后续应为每个系统建立独立连接器：

- 用户用自己的中大账号完成官方登录。
- 后端保存该用户对应系统的会话状态。
- 自然语言请求先分类到对应连接器，例如“查询我预约的自习室”进入 libic/预约连接器。
- 连接器调用真实校园系统接口获取结构化数据，再由助手整理成自然语言答案。

当前已有的私人事务入口：

```http
GET /auth/private/libic/start
POST /auth/private/libic/import-sysu-anything
GET /me/private/libic/status
POST /personal/query
```

## 本地运行

```powershell
cd D:\Downloads\逸问问答助手
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8013
```

网页入口：

```text
http://127.0.0.1:8013/
```
