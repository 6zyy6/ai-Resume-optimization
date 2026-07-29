# 本地开发

宿主机联调：

1. 准备 `.env`。至少设置 `APP_ENV=development`、`DATABASE_URL`、`CELERY_BROKER_URL`、`AUTH_REDIS_URL`、`AI_REDIS_URL`、`AI_SERVICE_TOKEN`、`LOCAL_AUTH_SECRET` 和 `LOCAL_EMAIL_OTP`。`DATABASE_URL` 必须使用 `postgresql+asyncpg://`；连接远程库时使用本地 SSH 隧道地址，不要直接暴露 PostgreSQL 公网端口。
2. 真实模型模式设置 `AI_RUNTIME_MODE=production`、模型路由和供应商密钥。密钥不得提交。
3. 执行 `pnpm dev`。启动前会构建 Pi 并执行 `alembic upgrade head`，随后同时启动 Web、API、Pi、Outbox Dispatcher 和本地单进程 Celery Worker。
4. 打开 `http://127.0.0.1:3000/login`。验证码使用 `.env` 中的 `LOCAL_EMAIL_OTP`；此固定验证码只在 `APP_ENV=development` 的本地入口生效。
5. 检查 API `http://127.0.0.1:8000/v1/health/ready`、Pi `http://127.0.0.1:3101/internal/v1/health/ready`。按 `Ctrl-C` 会停止整套进程。

微信小程序需单独执行 `pnpm dev:miniprogram`。

Docker Compose 联调仍可执行 `docker compose -f infra/docker/docker-compose.yml up --build`。停止时先停止入口，再给 Worker 至少 45 秒完成或释放租约。

本地 fixture 通过不代表真实模型、COS、真机或云网络通过。
