# 数据流与信任边界

```text
Web / 微信小程序 -> HTTPS FastAPI -> PostgreSQL
                               -> Redis / Outbox / Celery
                               -> 对象存储
                               -> 内网 Pi -> 已审批模型供应商
```

FastAPI 是唯一公网业务入口和 owner/授权/配额事实拥有者。客户端不直连 Pi、数据库、Redis、COS SDK 或模型供应商。Pi 不访问业务数据库或用户文件，只接收完成工作流所需的最小结构化输入。

敏感数据在传输和存储时加密；服务密钥只由运行环境注入。日志和 OTel 删除授权头、Cookie、Prompt、reasoning、供应商正文、邮箱和简历正文，仅保留 `trace_id`、`task_id`、版本、状态、耗时、token/cost 数值。数据库与 Redis 只在私有网络，对象下载使用绑定 owner/action/key/expiry 的短期签名。
