# WangPanBot

一个基于 `aiogram` 的 Telegram 文件收录与搜索机器人，支持多数据库后端。

## 功能

- 发送文件自动收录到数据库
- 关键词搜索文件名（支持分页）
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

先复制 `.env.example`，按需填写变量。

通用变量：

- `BOT_TOKEN` 必填，Telegram Bot Token
- `ADMIN_ID` 可选，设置后仅管理员可在私聊上传/删除
- `SEARCH_LIMIT` 可选，单页搜索条数，默认 `5`
- `SEARCH_SESSION_TTL_SECONDS` 可选，分页会话过期时间（秒），默认 `1800`
- `DB_PROVIDER` 必填，数据库类型：`sqlite | supabase | mongodb | turso | neon`

数据库变量（只需要填你选择的那一组）：

- `sqlite`
: `DB_PATH`（默认 `data.db`）
- `supabase`
: `SUPABASE_DATABASE_URL`（推荐）或 `SUPABASE_DB_URL`，也可用 `DATABASE_URL` 兜底
- `neon`
: `NEON_DATABASE_URL`，也可用 `DATABASE_URL` 兜底
- `mongodb`
: `MONGODB_URI`（必填），可选 `MONGODB_DB_NAME`、`MONGODB_COLLECTION_NAME`
- `turso`
: `TURSO_DATABASE_URL`（必填），`TURSO_AUTH_TOKEN`（云库通常必填），可选 `TURSO_LOCAL_PATH`（embedded replica 本地文件）

Postgres 连接池（`supabase`/`neon`）：

- `POSTGRES_POOL_SIZE` 可选，默认 `5`

Webhook 变量（仅 `app.py` 模式）：

- `WEBHOOK_PATH` 默认 `/webhook`
- `WEBHOOK_URL` 完整地址，优先级最高
- 或 `WEBHOOK_BASE_URL` + `WEBHOOK_PATH`
- Render 可不填域名，自动使用 `RENDER_EXTERNAL_URL`

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
