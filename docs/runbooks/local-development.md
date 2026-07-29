# 本地开发

1. 准备 `.env`，至少设置 `POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`DATABASE_URL`、`CELERY_BROKER_URL`、`AI_SERVICE_TOKEN`。真实模型模式还需模型路由和供应商密钥；密钥不得写入 Compose。
2. 执行 `docker compose -f infra/docker/docker-compose.yml up --build`。
3. 依次检查 API `/v1/health/live`、`/v1/health/ready`，Pi `/internal/v1/health/live`、`/internal/v1/health/ready`，Web `http://127.0.0.1:3000`。
4. 用同一 `trace_id` 检查 API、Task、Worker、Pi 事件。日志不得出现授权头、Cookie、Prompt、模型正文或用户完整简历。
5. 停止时先停止入口，再给 Worker 至少 45 秒完成或释放租约：`docker compose -f infra/docker/docker-compose.yml down`。

本地 fixture 通过不代表真实模型、COS、真机或云网络通过。
