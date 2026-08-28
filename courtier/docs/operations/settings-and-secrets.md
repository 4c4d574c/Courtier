# 配置管理与密钥运维（DB-backed settings）

2026-08-28 配置中心化落地后的配置面说明。对应计划：
`docs/superpowers/plans/2026-08-27-db-backed-settings-implementation.md`。

## 配置分层

| 层 | 内容 | 配置位置 |
|----|------|----------|
| Tier 0（自举） | `MYSQL_URL`、`COURTIER_SETTINGS_KEY`、`DEPLOYMENT_ENVIRONMENT` | 容器 env / `.env`，仅此三项是启动必需 |
| Tier 1（应用配置） | LLM、ES、MinIO、插件连接、JWT、守卫预算、OTel 等全部应用设置 | 数据库 `settings` 表；前端「管理后台 → 系统设置」维护 |
| 基础设施 | MySQL/MinIO/ES/Langfuse/Grafana 容器自身的密码 | compose / 部署层（开发默认值已内置；生产用 docker secrets 或平台注入） |
| 插件 env | OCR 端点、ResNet 字体模型、插件受限 MinIO、插件 token | `plugins/plugin.env`（见 `plugin.env.example`） |

## 密钥安全

- **`COURTIER_SETTINGS_KEY` 是数据库内密文的加密密钥**（生成：`openssl rand -hex 32`）。
  **必须纳入部署备份：丢失后数据库内所有 secret（LLM key、ES/MinIO 凭据、JWT 密钥、插件
  token）不可恢复**，设置页会显示"不可读"并要求逐项重录。
- secret 字段在 API 中只写不读（`{"set": true, "tail": "abcd"}`），明文绝不回显；
  审计表 `settings_changes` 只记 sha256 哈希。
- 未配置加密密钥时，secret 字段保存直接 422 拒绝（fail-closed）。

## 生效模型（设置页逐项标注）

- **热生效**：下一次请求/下一次 agent 构建即用新值（LLM 参数、守卫阈值、run 限额等）。
- **保存后重建**：连接类（ES、MinIO、插件）。保存**前**先用新值探活（ES ping /
  MinIO list_buckets / 插件端点 TCP 拨号），失败不落库；成功后失效本地客户端单例、
  插件断开重连。
- **需重启**：CORS、OTel。CORS 有救援通道——环境变量 `CORS_ORIGINS` 一旦设置，
  **永远覆盖**数据库值（管理员把自己锁在外面时用它逃生）。

## 首启与种子导入

- 全新部署（无 admin）：前端自动进入 `/setup` 向导创建首个管理员；staging/production
  下向导仅接受内网来源或携带 `COURTIER_SETUP_KEY`（一次性）。创建后向导永久关闭，
  `/health` 报 `config.setup_required=false`。
- 老环境升级：首次启动且 `settings` 表为空时，`.env` 中的非默认值一次性导入数据库
  （含 secret，需已配置加密密钥；无密钥时 secret 跳过并在日志告警）。此后以数据库为准。

## 基础设施凭据匹配（易错点）

应用侧连接凭据（数据库存储）与 infra 容器自身密码是**两份独立配置**，必须一致：

| infra 容器 | 容器侧变量 | 应用侧（设置页） |
|------------|-----------|------------------|
| MySQL | `MYSQL_ROOT_PASSWORD`（compose） | `MYSQL_URL`（Tier 0 env，改密码需同步改 URL） |
| Elasticsearch | `ES_PASSWORD`（compose） | `ES_HOSTS/ES_USERNAME/ES_PASSWORD`（检索与存储组） |
| MinIO | `MINIO_ROOT_USER/PASSWORD`（compose） | `MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY`（检索与存储组） |

不匹配时设置页的"测试连接"按钮会直接暴露问题（探活在保存前执行）。

## 插件 token 双侧一致性

`COURTIER_PLUGIN_TOKEN` 是主进程与插件共享的通道密钥：主进程侧存数据库（设置页
"插件"组），插件侧在各自 env。**修改任一侧必须同步另一侧**，否则全部插件 401 阻断
（保存页有确认弹窗提示；保存后主进程自动断开重连）。
