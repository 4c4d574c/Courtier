# 简化后端启动入口设计

## 背景

当前启动 Courtier 后端需要手动设置 `PYTHONPATH` 并调用 uvicorn：

```bash
PYTHONPATH=courtier:domains/docaudit:libs/shared:libs/docaudit \
  uv run python -m uvicorn courtier.agent.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

命令冗长且容易出错。目标是在项目根目录提供一个 `main.py`，让开发者可以：

```bash
uv run main.py
```

## 目标

- 通过 `uv run main.py` 一键启动后端
- 支持 `--host`、`--port`、`--reload` 参数
- 启动前自动执行 `alembic upgrade head`
- 更新项目文档中的启动命令

## 方案

采用根目录独立 `main.py` 方案。

### 新增文件：`courtier/main.py`

职责：
1. 自动将 `courtier`、`domains/docaudit`、`libs/shared`、`libs/docaudit` 加入 `sys.path`
2. 用 `argparse` 解析命令行参数
3. 调用 `alembic.command.upgrade()` 自动跑迁移
4. 调用 `uvicorn.run()` 启动 FastAPI 服务

参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8000` | 监听端口 |
| `--reload` | `False` | 是否启用热重载 |

### 更新文档

修改 `courtier/CLAUDE.md` 和 `courtier/README.md`，将启动命令简化为：

```bash
uv run main.py
```

并保留可选参数示例：

```bash
uv run main.py --host 127.0.0.1 --port 8080 --reload
```

## 验证标准

- `uv run main.py` 能正常启动服务器
- 8 个插件全部注册成功
- `--reload` 参数生效
- 启动前自动完成数据库迁移

## 后续可扩展

以后若需要 `courtier serve` 子命令，可将 `main.py` 中的逻辑提取到 `courtier/cli/main.py`，`main.py` 仅作为薄包装保留。
