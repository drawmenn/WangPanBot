# WangPanBot

一个基于 `aiogram + sqlite` 的 Telegram 文件收录与搜索机器人。

## 功能

- 发送文件自动收录到数据库
- 关键词搜索文件名
- 点击按钮回传文件
- 支持 `polling` 和 `webhook` 两种运行方式

## 环境要求

- Python 3.10+

## 安装

```bash
pip install -r requirements.txt
```

## 配置

参考 `.env.example` 设置环境变量：

- `BOT_TOKEN` 必填
- `ADMIN_ID` 可选，设置后仅允许该用户在私聊上传
- `DB_PATH` 可选，默认 `data.db`
- `SEARCH_LIMIT` 可选，默认 `5`
- `WEBHOOK_*` 仅 webhook 模式需要（在 Render 上可不填，自动使用 `RENDER_EXTERNAL_URL`）

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
- 免费实例文件系统是临时的，`data.db`（SQLite）在重启/重新部署后会丢失
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
