# SQL Pulse 账号注册与登录设计方案（V1）

- 状态：Draft
- 日期：2026-09-17
- 范围：注册、登录、退出、会话 Cookie、页面与 API 保护、密码存储
- 关联实现：`app/main.py`、`app/db.py`、`app/config.py`、`app/routers/*`
- 关联文档：`sqlpulse/docs/feature_gap_analysis.md`

## 1. 背景与目标

SQL Pulse 当前没有账号体系和鉴权，任何能访问 8080 端口的人都可以提交压测 SQL、造数任务和 Shell 脚本。业务功能已具备页面历史、任务状态和 API 能力，因此需要以不重构现有技术栈为前提，引入轻量级注册与登录。

V1 采用“无状态签名 Cookie 会话”而非内存 token，原因：

- 不用引入 Redis。
- `uvicorn --workers > 1` 时不会出现内存会话只在某个 worker 生效的问题。
- 服务重启后，只要 `APP_SECRET_KEY` 不变，已登录用户无需重新登录。
- 不依赖额外会话框架，FastAPI 依赖注入即可完成页面和 API 保护。

## 2. 范围

### 2.1 V1 实现

- 用户注册：用户名 + 密码。
- 登录与退出。
- 全局鉴权开关：`AUTH_ENABLED=true` 时启用登录保护，`false` 时完全放行，不出现登录拦截。
- 初始测试账号：`root / root`，用于本地开发和测试环境快速登录。
- 密码使用 Argon2 哈希存储。
- 登录状态使用签名的服务端 Cookie 会话。
- 所有业务页面和 API 默认需要登录。
- 侧边栏展示当前用户和退出入口。
- 单元测试与集成测试覆盖认证闭环。

### 2.2 明确不实现

- JWT。
- 内存会话表。
- Redis 会话。
- FastAPI Users / Authlib / OAuth。
- RBAC、细粒度权限、用户角色。
- 邮箱验证、密码找回、手机验证、2FA。
- 管理端禁用用户、强制下线、单点注销。

这些能力可放入 V2；V1 只解决“谁能进入系统”这一层。

## 3. 功能需求

### FR-1 注册

- `GET /register` 展示注册页。
- `POST /register` 接收 `username`、`password`、`password_confirm`。
- 用户名为 3 到 32 位字符，去除首尾空格。
- 用户名不区分大小写，重复注册返回错误。
- 密码长度 8 到 128 位；前后端都做校验。
- 注册成功后自动登录并跳转到 `/runs/new`。

### FR-2 登录

- `GET /login` 展示登录页。
- `POST /login` 校验用户名和密码。
- 成功后写会话 Cookie，跳转 `/runs/new`。
- 失败时留在登录页并展示错误，不泄露用户是否存在。

### FR-3 退出

- `POST /logout` 清除会话，跳转 `/login`。

### FR-4 页面保护

- 未登录访问业务页面时跳转到 `/login`。
- 已登录用户访问 `/login` 或 `/register` 时跳转到 `/runs/new`。
- `/healthz`、静态资源保持公开。

### FR-5 API 保护

- 现有 `/api/*` 业务接口默认需要登录。
- 未登录的 API 请求返回 `401 Unauthorized`。
- SSE 流接口同样走同一个会话 Cookie，同源请求不受影响。

### FR-6 当前用户

- `base.html` 侧边栏显示当前用户名。
- 未登录用户不可见业务导航项。

### FR-7 全局鉴权开关

- 提供 `AUTH_ENABLED` 配置，默认 `true`。
- `AUTH_ENABLED=true`：按 V1 规则要求登录；未登录页面跳转 `/login`，未登录 API 返回 401。
- `AUTH_ENABLED=false`：所有业务页面和 API 不校验登录状态，未登录用户可直接进入业务页面。
- 关闭鉴权时，认证入口在导航中隐藏，避免用户误以为必须登录。
- 开关只影响请求鉴权，不改变 `users` 表、密码哈希和会话代码；随时可重新启用。

### FR-8 初始测试账号

- 应用启动时若 `users` 表中不存在 `root`，自动创建初始账号 `root / root`。
- 已存在 `root` 用户时不覆盖已有账号和密码，也不影响其他用户。
- 初始账号通过种子逻辑写入，不经过注册接口，因此不受注册密码最小 8 位限制；直接登录校验即可。
- 初始账号仅用于本地开发与测试；共享或生产环境应改为关闭种子账号或修改密码。

## 4. 非功能需求

- NFR-1 密码不以明文保存，不写入日志、错误信息和请求体回显。
- NFR-2 会话内容必须签名，防止篡改。
- NFR-3 会话在服务重启后保持有效，不需要内存存储。
- NFR-4 会话默认 7 天过期，可配置。
- NFR-5 业务接口通过统一依赖保护，避免逐个路由漏加。
- NFR-6 认证失败不暴露 “用户不存在 / 密码错误” 的精确原因。
- NFR-7 V1 在受信内网环境使用；对外发布前必须补充 CSRF 防护。
- NFR-8 `AUTH_ENABLED=false` 时，现有业务功能保持原有行为，不需要客户端 Cookie 或登录状态。

## 5. 技术选型

| 能力 | 选择 | 理由 |
|---|---|---|
| 密码哈希 | `pwdlib[argon2]` | 现代、维护活跃；替代已不活跃的 passlib |
| HTML 表单解析 | `python-multipart` | FastAPI `Form` 依赖 |
| 会话 | Starlette `SessionMiddleware` | FastAPI 自带生态，无状态签名 Cookie |
| 签名依赖 | `itsdangerous` | Starlette 会话中间件的底层实现 |
| 权限控制 | FastAPI `Depends` | 不需要独立认证框架或中间件协议 |

新增依赖：

```text
pwdlib[argon2]
python-multipart
itsdangerous
```

## 6. 总体设计

### 6.1 登录流程

```text
Browser
  -> POST /register 或 POST /login
  -> app/routers/auth.py 校验表单
  -> app/auth.py 使用 pwdlib 校验/生成密码哈希
  -> app/db.py 读写 users 表
  -> 把 user_id 写入 request.session
  -> SessionMiddleware 签名后写入浏览器 Cookie
  -> 后续请求携带 Cookie 自动恢复当前用户
```

### 6.2 请求保护流程

```text
请求进入
  -> SessionMiddleware 解签 Cookie
  -> AUTH_ENABLED=false：直接放行，进入现有业务路由
  -> AUTH_ENABLED=true：继续查询当前用户
  -> get_current_user 根据 user_id 查询 users 表
  -> require_user 校验当前用户
  -> 页面请求：未登录跳转 /login
  -> API 请求：未登录返回 401
  -> 已登录：进入现有业务路由
```

### 6.3 模块职责

| 模块 | 职责 |
|---|---|
| `app/auth.py` | 密码哈希、`get_current_user`、`require_user` |
| `app/routers/auth.py` | 注册、登录、退出页面与表单处理 |
| `app/db.py` | `users` 表、用户查询与创建 |
| `app/config.py` | `APP_SECRET_KEY`、会话过期时间、Cookie 配置 |
| `app/main.py` | 挂载 SessionMiddleware、注册 auth 路由、统一保护业务路由 |
| `app/templates/login.html` | 登录页 |
| `app/templates/register.html` | 注册页 |
| `app/templates/base.html` | 当前用户、退出入口、未登录导航 |

## 7. 数据模型

在现有 SQLite `SCHEMA` 中新增 `users` 表：

```sql
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
```

字段说明：

- `id`：服务端生成的 UUID。
- `username`：唯一登录名，不区分大小写。
- `password_hash`：Argon2 哈希串。
- `created_at`：创建时间。

种子初始化逻辑：

```text
lifespan 执行 init_schema()
  -> 若 AUTH_SEED_ROOT=true 且不存在 username=root
  -> 使用 Argon2 哈希 root 密码
  -> INSERT 初始账号 root / root
```

不通过 `INSERT OR REPLACE` 或每次启动重写密码，避免用户在运行期修改密码后被覆盖。

V1 业务数据不按用户隔离，`runs` 和 `datagen_jobs` 暂不增加 `owner_user_id`。如果后续要求“每人只能看自己的任务”，再增加该列并按用户过滤。

## 8. 页面与 API 设计

### 8.1 认证入口

| 方法 | 路径 | 说明 | 认证要求 |
|---|---|---|---|
| `GET` | `/register` | 注册页 | 公开 |
| `POST` | `/register` | 执行注册 | 公开 |
| `GET` | `/login` | 登录页 | 公开 |
| `POST` | `/login` | 执行登录 | 公开 |
| `POST` | `/logout` | 退出登录 | 可选 |

表单参数：

```text
username
password
password_confirm（仅注册）
```

认证路由只使用表单请求，不扩展 JSON API；后续如果接入移动端或外部自动化客户端，再单独设计 token API。

### 8.2 业务路由保护

公开路由：

```text
/healthz
/static/*
/login
/register
```

受保护页面：

```text
/
/runs/new
/runs
/runs/{run_id}/monitor
/runs/{run_id}/report
/datagen
```

受保护 API：

```text
/api/db/test
/api/runs
/api/runs/{run_id}*
/api/datagen/jobs*
```

`app/main.py` 中通过 `include_router(..., dependencies=[Depends(require_user)])` 统一挂载业务路由，不在每个路由函数里重复判断。

当 `AUTH_ENABLED=false` 时，上述页面与 API 保护全部关闭；`require_user` 依赖直接放行，`/login`、`/register` 即使被访问也不作为业务入口。

## 9. 会话设计

```python
from starlette.middleware.sessions import SessionMiddleware

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    session_cookie="sqlpulse_session",
    max_age=settings.auth_session_ttl_seconds,
    same_site="lax",
    https_only=settings.auth_cookie_secure,
)
```

约定：

- 会话中只保存 `user_id`，不保存密码和敏感信息。
- 会话过期由 Cookie `Max-Age` 控制。
- 退出时执行 `request.session.clear()`。
- 服务重启不丢会话，因为签名验证只依赖 `APP_SECRET_KEY`。
- 修改或轮换 `APP_SECRET_KEY` 会使所有老会话失效。

V1 的取舍：

- 不支持服务端主动踢人、封禁单用户或指定设备下线。
- 如需这些能力，V2 增加 `sessions` 表，服务端保存 `session_id -> user_id`，登录和权限检查均查表。

## 10. 配置项

新增到 `Settings` 和 `.env.example`：

```text
AUTH_ENABLED=true
APP_SECRET_KEY=
AUTH_SESSION_TTL_SECONDS=604800
AUTH_SESSION_COOKIE=sqlpulse_session
AUTH_COOKIE_SECURE=false
AUTH_ALLOW_REGISTER=true
AUTH_SEED_ROOT=true
```

说明：

- `AUTH_ENABLED=false` 适合本地开发、演示或暂时不需要登录的环境；生产或共享环境应保持 `true`。
- `APP_SECRET_KEY` 必填，建议 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成。
- `AUTH_COOKIE_SECURE` 在 HTTPS 后置为 `true`。
- `AUTH_ALLOW_REGISTER` 若只想内定账号，可关闭注册入口。
- `AUTH_SEED_ROOT=true` 时自动创建测试账号 `root / root`；生产环境应设为 `false` 或部署后立即修改密码。

## 11. 与现有代码集成的改动点

| 文件 | 改动 |
|---|---|
| `requirements.txt` | 新增 3 个认证依赖 |
| `app/db.py` | `SCHEMA` 增加 `users` 表；增加用户查询/创建函数与种子账号初始化 |
| `app/config.py` | 增加 `APP_SECRET_KEY` 与会话配置 |
| `app/auth.py`（新增） | 密码哈希封装与 `get_current_user` / `require_user` |
| `app/routers/auth.py`（新增） | 注册、登录、退出 |
| `app/main.py` | 增加 SessionMiddleware、auth 路由、业务路由统一鉴权依赖 |
| `app/templates/login.html`（新增） | 登录页 |
| `app/templates/register.html`（新增） | 注册页 |
| `app/templates/base.html` | 当前用户、退出按钮、未登录导航 |
| `.env.example` | 新增 `AUTH_ENABLED` 与认证配置示例 |
| `docker-compose.yml` | 传输 `APP_SECRET_KEY`、`AUTH_ENABLED` |
| `tests/integration/test_e2e.py` | 增加先登录再调用业务 API 的测试夹具 |

现有 `data/sqlpulse.db` 无需手工迁移：lifespan 中 `db.init_schema()` 会以 `CREATE TABLE IF NOT EXISTS` 自动新增 `users` 表。

## 12. 安全设计

- 密码使用 Argon2，默认参数由 `pwdlib` 推荐值决定。
- SQLite 查询使用参数绑定，用户名不做字符串拼接。
- 表单长度、用户名格式、密码长度均在后端再次校验。
- 登录失败统一提示“用户名或密码错误”。
- 密码明文只在表单提交的瞬时内存中出现，不落库、不打日志。
- `root / root` 是弱口令，仅用于本地和测试环境；`AUTH_SEED_ROOT` 默认只应在开发配置中打开。
- 会话 Cookie 设置 `HttpOnly` 和 `SameSite=Lax`；HTTPS 时启用 `Secure`。
- V1 不在内网之外的场景承诺防 CSRF；对外部署前必须增加会话 CSRF token。
- 登录只是访问门禁，不是授权边界：SQL Pulse 能执行任意 SQL 和 Shell，开放注册仅限受信团队。
- `AUTH_ENABLED=false` 会使系统退回“端口即门禁”状态，仅建议在隔离的本地或演示环境使用。

## 13. 测试方案

### 单元测试

- 用户名创建成功。
- 用户名重复且大小写不敏感时失败。
- 密码哈希可验证，且库里不出现明文密码。
- 非法用户名和短密码被拒绝。
- `require_user` 对无会话请求返回 401。
- `AUTH_ENABLED=false` 时 `require_user` 直接放行，页面和 API 均可匿名访问。
- 空库启动后自动生成 `root / root`，且重复启动不会覆盖密码。

### 集成测试

- `/healthz` 无需登录。
- 未登录访问 `/runs/new` 被重定向到 `/login`。
- 注册后用同一客户端访问 `/runs/new` 成功。
- 退出后再次访问业务 API 返回 401。
- 已登录客户端可完成原有 `POST /api/runs` 流程。
- 使用 `root / root` 可直接登录。
- 重启应用后会话 Cookie 仍然有效。

### 改造影响

现有 `tests/integration/test_e2e.py` 中所有业务 API 调用目前都不带 Cookie；接入鉴权后，需要在测试夹具中先注册一个测试用户并用同一 `httpx.Client` 登录，再执行原有断言。

为保留原有测试路径，可让现有 e2e 环境使用 `AUTH_ENABLED=false` 运行回归；认证相关集成测试单独使用 `AUTH_ENABLED=true` 验证完整登录流程。

## 14. 实施步骤

### 步骤一：依赖与基础

- 更新 `requirements.txt`。
- `app/db.py` 增加 `users` 表与用户函数。
- `app/db.py` 增加 `root / root` 种子账号初始化逻辑。
- `app/config.py` 增加认证配置。
- `.env.example` 与 `docker-compose.yml` 补充 `APP_SECRET_KEY`。

### 步骤二：认证核心

- 新增 `app/auth.py`。
- 新增 `app/routers/auth.py`。
- `app/main.py` 挂载 SessionMiddleware 和 auth 路由。

### 步骤三：业务保护

- 为业务 router 增加 `Depends(require_user)`。
- 页面路由执行未登录跳转。
- `base.html` 增加用户信息与退出入口。

### 步骤四：鉴权开关

- 在 `require_user` 最前面判断 `AUTH_ENABLED`，关闭时直接返回当前用户为 `None` 或放行。
- 导航根据开关和登录状态隐藏或显示登录/注册/退出入口。
- 为关闭状态补充回归测试。

### 步骤五：测试与文档

- 补单元测试与集成测试。
- 更新 README 的启动与使用说明。
- 更新 `sqlpulse/docs/feature_gap_analysis.md` 的鉴权状态。

## 15. 验收标准

- 用户可注册、登录、退出。
- 注册后密码只以 Argon2 哈希保存在 SQLite。
- 未登录无法访问业务页面和业务 API。
- `/healthz`、认证页和静态资源保持公开。
- 会话默认 7 天有效，服务重启后仍有效。
- 服务端不维护内存 token 表，单进程或多 worker 行为一致。
- 首次启动后可用 `root / root` 登录，后续启动不覆盖该账号及其修改后的密码。
- 原有压测、造数、监控、报告流程在登录后不受影响。
- 设置 `AUTH_ENABLED=false` 后，未登录用户可直接访问业务页面和 API，不出现登录拦截。
