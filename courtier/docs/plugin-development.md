# Courtier 插件开发指南

## 概述

插件是**独立 TCP 服务器**，与宿主之间用换行分隔的 JSON-RPC 2.0 通信。每个插件是独立进程、独立 venv、独立部署单元——本地开发经 `scripts/dev-plugins.py` 拉起，生产上是 docker-compose 服务或 systemd 单元，可以和宿主不同机。

```
┌──────────────┐    JSON-RPC 2.0 over TCP     ┌──────────────┐
│   Courtier   │ ◄──────────────────────────► │    插件      │
│ 宿主（主动拨号）│   （+ MinIO 传输桶文件搬运）    │  （服务器）   │
└──────────────┘                              └──────────────┘
```

宿主按 `COURTIER_PLUGIN_ENDPOINTS`（DB 设置，`name=host:port` 逗号分隔，格式错误启动即报错）逐个**拨号**。连接建立后插件立刻发送 `plugin.register`（能力清单 + 共享 token），宿主校验 token 后回答 `plugin.auth` 证明自己，随后 `plugin.host_services` 与 `plugin.runtime_context` 两条通知完成握手，插件工具才可调用。认证通过前，除 `plugin.register`/`plugin.auth` 外的一切方法都被拒绝。

## 快速开始

1. 建插件目录——跨域工具放 `plugins/shared/<name>/`，域专属工具放 `plugins/<domain>/<name>/`（目录归属同时决定插件属于哪个域，是域门控的依据）：

```
plugins/shared/my_tool/
├── plugin.yaml     # 清单（策略层：默认端口、超时、host 服务依赖）
├── entry.py        # 入口：PluginRuntime 子类
├── tools.py        # 工具实现
└── pyproject.toml  # 依赖（uv 管理；依赖 courtier-plugin-sdk + 所需 libs）
```

2. 写 `plugin.yaml`：

```yaml
name: my_tool            # 必须与目录名一致
version: "1.0.0"
api: "2.0"               # 独立部署协议（token 双向握手）
description: "这个插件做什么"
timeout_ms: 120000       # 宿主侧默认调用等待预算（毫秒）；长任务插件应调大

runtime:
  port: 9201             # 默认监听端口（--listen / COURTIER_PLUGIN_LISTEN 可覆盖）
  env:                   # 可选：字面量环境默认值（os.environ.setdefault）
    MY_TUNING_KNOB: "42"
```

清单是 `extra: forbid` 的严格模型——不要添加未定义字段。**不要写 capabilities 块**：工具契约在运行时由 `entry.py` 的 `register_tool()` 注册，模型侧的使用指引由 `register_capabilities()` 的 `system_prompt` 携带（清单里的提示词文本是会悄悄漂移的死配置）。

3. 写 `tools.py` 和 `entry.py`：

```python
# tools.py
from courtier_plugin_sdk import ToolResult

class MyTool:
    name = "do_something"
    display_name = "做点什么"
    description = "执行一个自定义操作"
    parameters = {
        "type": "object",
        "properties": {"input_data": {"type": "string"}},
        "required": ["input_data"],
    }

    async def execute(self, **kwargs):
        return ToolResult(success=True, data={"result": kwargs["input_data"]})
```

```python
# entry.py
from courtier_plugin_sdk import PluginRuntime
from tools import MyTool

class MyPlugin(PluginRuntime):
    def register_capabilities(self):
        # 工具也可以在这里以 dict 形式声明；推荐 register_tool()（自动提取契约）。
        # system_prompt 会随 plugin.register 发给宿主，作为模型侧的使用指引。
        return {"capabilities": [], "system_prompt": "# 我的工具\n……"}

    def _setup_handlers(self):
        self.register_tool(MyTool())

if __name__ == "__main__":
    import asyncio
    asyncio.run(MyPlugin().run())
```

4. 启动并接线宿主：

```bash
# 插件侧（scripts/dev-plugins.py 会自动发现并拉起，无需手动跑）
cd plugins/shared/my_tool && uv sync
COURTIER_PLUGIN_TOKEN=dev-token .venv/bin/python entry.py   # 监听 runtime.port

# 宿主侧（DB 设置「插件端点映射」；.env 只在首启播种时生效）
COURTIER_PLUGIN_TOKEN=dev-token
COURTIER_PLUGIN_ENDPOINTS=...,my_tool=127.0.0.1:9201
```

`pyproject.toml` 只依赖 `courtier-plugin-sdk`（路径依赖，editable）和用到的 `libs/` 库，**永远不要依赖 `courtier` 应用包**。参照 `plugins/shared/annotate/`。

## 工具契约

`register_tool(tool_instance)` 从实例属性自动提取契约，随 `plugin.register` 上报：

| 属性 | 作用 |
|------|------|
| `name` / `display_name` / `description` / `parameters` | 工具名、展示名、描述、JSON Schema 入参（模型的可见面） |
| `output_artifact_type` | 结果登记为该类型的产物 |
| `output_schema` | 结果的输出 schema（供投影/绑定） |
| `file_params` | 文件型参数名清单；契约里对应属性标记 `format: file-ref`，宿主派发前改写为 `minio://` 引用 |
| `input_fields` | `InputField(name, artifact_type, materialize_as)` 列表：声明某参数从会话产物自动绑定 |
| `skip_persist` | 结果跳过宿主的自动持久化（不生成 `$ref`） |
| `skip_ref_resolution` | 派发前不对入参做 `$ref` 展开 |
| `internal` | 宿主专用工具（如上传期服务），不对代理会话暴露 |
| `call_timeout_seconds` | 该工具的宿主侧调用等待预算（覆盖 `timeout_ms` 派生默认；长任务如转码用） |
| `runtime_policy` | `RuntimePolicy(max_calls, max_consecutive)` 调用频次上限 |

**宿主注入参数（`x-host-injected`）**：schema 属性标记 `"x-host-injected": "<injector>"` 表示该参数由宿主在派发边界填充、模型不填（模型给的值会被丢弃，参数对模型不可见）。注入器注册在宿主 ToolRegistry 上，在 `$ref` 展开之后、工具执行之前运行；注入 `None` 时参数保持未设置，工具按缺少该可选输入降级。现成例子：`search` 插件的 `query_embedding`（宿主注入查询向量）与 `memory` 插件的调用者身份。这是「插件不持有 LLM 凭据」原则的机制化：模型端只给文本 `query`，向量由宿主嵌入后注入。

## 文件传输（`file_params` + `resolve_file`）

跨机部署与宿主不共享文件系统。工具入参要传文件时，声明 `file_params` 并在执行时解析：

```python
from courtier_plugin_sdk import resolve_file

class ConvertTool:
    name = "convert"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}}}
    file_params = ["file_path"]   # 契约中注册为 format: file-ref

    async def execute(self, file_path: str = ""):
        local = await resolve_file(file_path)   # minio:// → 下载到本次请求工作目录
        ...
```

派发时宿主强制 upload-dir 沙箱（fail-closed），把文件 PUT 进 `courtier-plugin-io` 传输桶（内容寻址键，去重），再给插件发 `minio://bucket/key` 引用。`resolve_file` 用插件自己的受限 MinIO 账号把引用下载进每请求临时目录（调用结束自动清理）；普通本地路径原样通过（同机开发 / 测试）。

## 产出文件（`put_file` + `storage.presign_get`）

```python
from courtier_plugin_sdk import HostStorage, put_file
from courtier_plugin_sdk.files import parse_minio_ref

ref = await put_file(Path("/tmp/out.docx"))        # 直传传输桶，返回 minio:// 引用
bucket, key = parse_minio_ref(ref)
receipt = await HostStorage(self.host_service_client).presign_get(bucket, key)
url = receipt["download_url"]                       # 宿主签发的用户下载链接
```

`put_file` 上传到 `out/<插件名>/<uuid>/<文件名>` 键；`presign_get` 由宿主校验传输桶白名单后签发。需要清单声明 `dependencies.host_services: [storage]` 和 `permissions: [read:storage, write:storage]`（旧式 base64 整文件上传 `storage.put` 同样走 `storage` 服务）。

## 环境所有权

插件读自己的环境，宿主不注入任何变量。插件消费的每个变量都记录在 `plugins/plugin.env.example`（本地开发复制为 `plugins/plugin.env`，由 `scripts/dev-plugins.py` 加载；生产由 compose/systemd 注入）。必需项：

- `COURTIER_PLUGIN_TOKEN`——与宿主一致，未设置时插件拒绝启动；
- 收发文件的插件还需要受限 MinIO 账号（`MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/SECURE`，由 `scripts/minio_plugin_io.py` 拨备、仅限传输桶），可选 `MINIO_BUCKET_PLUGIN_IO`（默认 `courtier-plugin-io`）与 `COURTIER_PLUGIN_WORKDIR`（下载根目录）。

manifest `runtime.env` 里的字面量由 SDK 在启动时 `os.environ.setdefault` 兜底；`${ENV:...}` 占位符已被忽略（插件在使用点直接读自己的环境）。插件**不持有任何 LLM 端点或密钥**，也**不持有 DB 凭据**（数据一律走宿主服务反向 RPC）。

## 宿主服务

在 `plugin.yaml` 的 `dependencies.host_services` + `permissions` 里声明，宿主在**每次**反向调用时强制校验。已知服务名：`cache`、`artifact_store`、`storage`、`template_store`、`memory_store`；已知权限域：`read:documents`、`write:artifacts`、`read:cache`、`write:cache`、`read:storage`、`write:storage`、`read:templates`、`network:outbound`。

方法面：`cache.persist/load/resolve/micro_compact`，`artifact_store.put/get/list`，`storage.put`（旧式 base64）、`storage.presign_get`，`template_store.get`，`memory_store.call`。会话级方法校验 `session_id`——插件只能触碰它当前正在服务的会话（连接的在途请求表是事实源）。SDK 侧经 `HostStorage`、`HostTemplateStore` 或裸 `HostServiceClient.call(method, params)` 使用；`host_service_client` 属性在握手完成（`plugin.host_services` 通知到达）后可用，注意工具构造时它还不存在，要用惰性 getter。

## 协议细节

- **内建方法**（SDK 自动处理）：`plugin.auth`、`plugin.health`、`plugin.shutdown`、`tool.list`、`checker.list`；`tool.execute` 有内建派发器——按名调到 `register_tool` 注册的实例，且**每次调用包在每请求工作目录里**（下载随调用结束清理）。
- **自定义方法**：`@runtime.on("method")` 注册请求处理器（同步 handler 自动进线程池执行，不阻塞事件循环）；`@runtime.on_notification("method")` 注册通知处理器（后台任务运行）。
- **并发上限**：单连接在途请求上限 `PLUGIN_MAX_CONCURRENT`（默认 8），超限直接回 `plugin busy` 错误，防御宿主失控把插件打爆。
- **宿主可取消**：宿主对长时间请求发 `request.cancel` 通知，SDK 取消对应的在途任务。
- **日志脱敏**：SDK 对 JSON-RPC 日志行里的 `token`/`api_key`/`password`/`secret`/`authorization` 键做打码。

## 生命周期与运维

- 宿主每 30s 健康检查一次（`plugin.health`，5s 超时）；连续 3 次失败断开连接，连接循环以指数退避重连（1s 起，封顶 30s，永久重试；握手成功后重置退避）。
- 管理操作作用于**连接**而非进程：`POST /api/admin/plugins/{name}/action` 的 start = 拨号、stop = 断开、restart = 重拨。插件日志归插件侧（`docker compose logs <服务名>` / 进程 stdout），管理端 logs 接口固定返回 410。
- 插件进程的存活由部署层负责（compose `restart: unless-stopped` / systemd）。优雅停机用 SIGTERM——SDK 已注册 SIGTERM/SIGINT 处理，停接受、关活动连接、退出。
- `plugin.shutdown` 请求只关闭发起它的那条连接，服务器继续运行；停进程才靠信号。

## 本地开发与测试

- `scripts/dev-plugins.py`：读 `plugins/plugin.env`，把 `plugins/` 下所有有 `.venv` 和 `plugin.yaml` 的插件按各自 manifest 端口拉起，日志带插件名前缀；Ctrl+C 全停。首次使用先在每个插件目录 `uv sync`。
- 单元测试：直接 `await tool.execute(...)`。
- 协议级测试：向 `runtime._reader` / `runtime._writer` 注入 `asyncio.Queue` + 写端替身再 `run()`——该路径完全绕开网络与鉴权门（单进程内没有信任边界）。
- 回环 TCP 全链路样例见 `tests/plugin/test_serve_mode.py` 与 `tests/plugin/test_integration.py`。
