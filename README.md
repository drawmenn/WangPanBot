# WangPanBot

一个基于 `aiogram` 的 Telegram 文件收录与搜索机器人，支持多数据库后端。

## 功能

- 发送文件自动收录到数据库
- 关键词搜索文件名（支持分页）
- 分页结果显示：当前页/总页数、总文件数、总容量
- 文件类型筛选（支持常用文档/视频/音频/图片/压缩包）
- 点击按钮回传文件
- 管理员删除文件（按钮删除或 `/delete 文件ID`）
- 数据库后端支持：`sqlite / supabase / mongodb / turso / neon`（部署时单选其一）
- 支持 `polling` 和 `webhook` 两种运行方式

## 环境要求

- Python 3.10+

## 安装

```bash
pip install -r requirements.txt
```

## 配置

可以按下面 4 步来配，照着填就行。

1. 先复制配置文件  
把 `.env.example` 复制为 `.env`，后续都改 `.env`。

2. 先填两个“必须项”  
- `BOT_TOKEN`：你的 Telegram 机器人 Token  
- `DB_PROVIDER`：你要用哪种数据库  
可选值：`sqlite | supabase | mongodb | turso | neon`

3. 数据库只填一组  
你选了哪个 `DB_PROVIDER`，就只填那一组变量，其他可以留空。

- 选 `sqlite`：填 `DB_PATH`（默认 `data.db`）  
- 选 `supabase`：填 `SUPABASE_DATABASE_URL`（或 `SUPABASE_DB_URL` / `DATABASE_URL`）  
- 选 `neon`：填 `NEON_DATABASE_URL`（或 `DATABASE_URL`）  
- 选 `mongodb`：填 `MONGODB_URI`（可再填 `MONGODB_DB_NAME`、`MONGODB_COLLECTION_NAME`）  
- 选 `turso`：填 `TURSO_DATABASE_URL`、`TURSO_AUTH_TOKEN`（可选 `TURSO_LOCAL_PATH`）

4. 按运行方式补充  
- 用 `bot.py`（轮询模式）：不用填 webhook 变量  
- 用 `app.py`（webhook 模式）：填下面任意一组  
  `WEBHOOK_URL`  
  或 `WEBHOOK_BASE_URL` + `WEBHOOK_PATH`  
  在 Render 上可以不填域名，平台会自动给 `RENDER_EXTERNAL_URL`

常用可选项：

- `ADMIN_ID`：填你的 Telegram 数字 ID，开启管理员权限（私聊上传/删除限制）  
- `SEARCH_LIMIT`：每页显示多少条，默认 `5`  
- `SEARCH_SESSION_TTL_SECONDS`：分页会话多久过期（秒），默认 `1800`  
- `POSTGRES_POOL_SIZE`：Postgres 连接池大小，默认 `5`（仅 Supabase/Neon 用到）

`ADMIN_ID` 速查模板：

```env
# 开启管理员限制（推荐）
ADMIN_ID=123456789

# 不限制管理员（留空）
ADMIN_ID=
```

说明：

- `ADMIN_ID` 必须是“纯数字 Telegram 用户 ID”，不是用户名（例如不是 `@abc`）  
- 可在 Telegram 里找 `@userinfobot`（或 `@getmyid_bot`）发消息获取数字 ID

## 配置模板

下面是可直接复制的 `.env` 模板。你只需要选一个数据库模板使用。

通用最小模板（所有方案都要有）：

```env
BOT_TOKEN=123456:your_telegram_bot_token
ADMIN_ID=
SEARCH_LIMIT=5
SEARCH_SESSION_TTL_SECONDS=1800
```

模板 A：SQLite（最简单，单机/测试）：

```env
DB_PROVIDER=sqlite
DB_PATH=data.db
```

模板 B：Supabase（Postgres）：

```env
DB_PROVIDER=supabase
SUPABASE_DATABASE_URL=postgresql://username:password@host:5432/postgres
POSTGRES_POOL_SIZE=5
```

模板 C：Neon（Postgres）：

```env
DB_PROVIDER=neon
NEON_DATABASE_URL=postgresql://username:password@host:5432/dbname?sslmode=require
POSTGRES_POOL_SIZE=5
```

模板 D：MongoDB Atlas：

```env
DB_PROVIDER=mongodb
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB_NAME=wangpanbot
MONGODB_COLLECTION_NAME=files
```

模板 E：Turso（libSQL）：

```env
DB_PROVIDER=turso
TURSO_DATABASE_URL=libsql://your-db-your-org.turso.io
TURSO_AUTH_TOKEN=your_turso_auth_token
TURSO_LOCAL_PATH=
```

轮询模式（`bot.py`）不需要 webhook 变量。

Webhook 模式（`app.py`）加上其中一组：

```env
WEBHOOK_URL=https://your-domain.com/webhook
WEBHOOK_PATH=/webhook
```

或

```env
WEBHOOK_BASE_URL=https://your-domain.com
WEBHOOK_PATH=/webhook
```

## 本地运行（Polling）

```bash
python bot.py
```

## Webhook 运行（FastAPI）

设置下面任意一组：

1. `WEBHOOK_URL`（完整地址，例如 `https://example.com/webhook`）
2. `WEBHOOK_BASE_URL`（域名）+ `WEBHOOK_PATH`（路径）

然后启动：

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

健康检查：

```text
GET /healthz
```

## Render 免费部署

仓库已提供 `render.yaml`，可直接用 Blueprint 创建服务。

步骤：

1. 在 Render 选择 `New +` -> `Blueprint`
2. 连接本仓库并导入 `render.yaml`
3. 在环境变量中填写 `BOT_TOKEN`
4. 首次部署完成后，Telegram webhook 会自动设置为 `https://<your-service>.onrender.com/webhook`

注意：

- Render 免费 Web Service 会在 15 分钟无流量后休眠
- 如果 `DB_PROVIDER=sqlite`，免费实例文件系统是临时的，`data.db` 在重启/重新部署后会丢失
- 生产场景建议改用 Postgres 持久化数据

## Kubernetes 部署

项目已提供 `Dockerfile` 和 `k8s/` 清单。

### 1) 构建并推送镜像

```bash
docker build -t <your-registry>/wangpanbot:latest .
docker push <your-registry>/wangpanbot:latest
```

### 2) 修改 Kubernetes 配置

- `k8s/deployment.yaml` 中的镜像地址改为你的镜像
- `k8s/secret.yaml` 中填入真实 `BOT_TOKEN`（可选 `ADMIN_ID`）
- `k8s/configmap.yaml` 中把 `WEBHOOK_BASE_URL` 改为你的 HTTPS 域名（如 `https://bot.example.com`）
- `k8s/ingress.yaml` 中把 `bot.example.com` 和 `tls secretName` 改为你的配置

### 3) 部署

```bash
kubectl apply -k k8s
```

### 4) 验证

```bash
kubectl -n wangpanbot get pods,svc,ingress
kubectl -n wangpanbot logs deploy/wangpanbot -f
```

注意：

- Telegram webhook 需要公网可访问的 HTTPS 地址
- 当前用 SQLite + PVC，建议 `replicas: 1`（多副本会有 SQLite 并发/锁冲突风险）
- 生产环境建议迁移到 Postgres，再考虑横向扩容

## 项目结构

- `core.py` 机器人核心逻辑（配置、数据库、handler）
- `bot.py` polling 入口
- `app.py` webhook 入口
- `k8s/` Kubernetes 部署清单
