# 历史会话文件预览修复计划

> 2026-09-02 · 根因分析与实现计划

## 1. 问题与根因

历史会话恢复后，上传文件卡片显示「历史文件，暂不支持预览」。根因（已核实代码）：

1. `FileCard.vue:15` 在 `file.url` 为空时渲染该提示。
2. `file.url` 由 `chatMessages.ts` `buildFileItem` 用 `turn.message.fileId` 在前端内存数组 `fileRecords`（即 `HomeView.vue` 的 `uploadedFiles`）中查得；查不到则 `url: ""`。
3. `uploadedFiles` 全项目唯一写入点是当次上传流程，存入的是 `URL.createObjectURL(file)` 的 **blob 临时链接**（仅当前页面会话有效），且 `handleHistorySelect` 恢复会话时主动 `revokeUploadedFiles()` 清空。
4. 后端只有 `POST /files` 上传路由，**没有按 fileId 取回文件的下载端点**——前端即使持有 fileId 也无法重建 URL。

数据侧条件齐全：`session_store` 每 turn 持久化 `fileId`/`fileName` 并随快照返回；`FileStore` 把 fileId→磁盘路径持久化在 `file_index.json`（重启可解析）；`FileInfo.owner` 支持归属校验。**结论：这是「预览 URL 只设计了上传当场一条通路」的架构缺口，非数据丢失。**

## 2. 方案总览

- **后端**：新增 `GET /api/files/{file_id}` 下载/预览端点（归属校验 + 内联响应）。
- **前端**：预览 URL 统一改为服务端 URL `/api/files/{fileId}`（上传与恢复走同一条路），恢复会话时从快照 turn 元数据回填 `fileRecords`；删除 blob URL 生命周期代码。

认证已具备条件（无需新机制）：登录时已种 `access_token` httpOnly cookie（`routes/auth.py`），`get_current_user` 接受 header/query/cookie 三种方式（`middleware/auth.py` `_resolve_token`），`<iframe :src>` 与 `<a download>` 可直接凭 cookie 过认证。

## 3. 后端改动

### 3.1 归属校验收敛为共享助手（顺带去重）

`sessions.py` 的 `_resolve_audit_file_owned_or_404` 语义（admin 放行；`owner` 为空的遗留文件对任意登录用户放行；否则 403）保持不变，但将其下沉为可复用实现——在 `FileStore` 上新增方法：

```python
async def authorized_path(self, file_id, upload_dir, user, is_admin) -> Path
# 404（不存在/路径逃逸/文件已删）、403（非 owner 且非 admin 且 owner 非空）语义同现 helper
```

`sessions.py` 两个 `_resolve_audit_file_*` helper 改为薄封装调用它（对外行为逐字节不变，回归点：现有 sessions 测试全绿）。

### 3.2 新路由 `GET /api/files/{file_id}`（`routes/files.py`）

- 依赖 `Depends(get_current_user)`；限流 `60/minute`（预览/下载均为用户主动点击，宽限额防滥用即可）。
- `FileResponse(path, media_type=<按扩展名>, filename=original_name, content_disposition_type="inline")`：浏览器可预览的类型（pdf/图片）内联展示，未知类型退化为下载。media_type 取 `file_service.ALLOWED_MIME_TYPES[ext]` 首个，缺省 `application/octet-stream`。
- 原文件名含中文：Starlette `FileResponse` 的 `filename=` 自动输出 RFC 5987 `filename*`，无需额外处理。

## 4. 前端改动

### 4.1 预览 URL 统一为服务端 URL

- `HomeView.handleSubmit`：上传成功后 `uploadedFiles` 存 `{ fileId, name, url: \`/api/files/${fileId}\` }`；**删除** `URL.createObjectURL` / `revokeObjectURL` / `revokeUploadedFiles` 的 revoke 逻辑（数组仅置空）。blob 链接的「刷新即死、卸载即 revoke」问题整类消除。
- 预览面板（`FilePreviewPanel.vue`）本就以普通 URL 消费（iframe/download 链接），零改动。

### 4.2 恢复会话时回填文件记录

- `utils/chatMessages.ts` 新增 `fileRecordsFromTurns(turns): ChatFileRecord[]`：遍历 `turns`，收集 `message.fileId` 非空的 `{fileId, name: fileName, url: /api/files/{fileId}}`，按 fileId 去重（edit-resend 会复用同一 fileId），保序。
- `handleHistorySelect`：`revokeUploadedFiles()` 改为 `uploadedFiles.value = fileRecordsFromTurns(loaded)`。
- 现有行为保留：`buildFileItem` 的 `record?.url ?? ""` 兜底不动——极老会话若 turn 只有 `fileName` 无 `fileId`，仍显示「历史文件」提示（此时确实无预览能力）。

## 5. 测试

- **后端**（`tests/agent/api/test_files_download.py` 新增）：
  - 上传→下载 200，内容字节一致，`Content-Disposition` 内联且文件名正确；
  - 未知 fileId 404；非 owner 用户 403；admin 访问他人文件 200；遗留空 owner 文件任意登录用户可读；
  - 仅 cookie（无 Authorization header）可下载——覆盖 iframe 场景。
- **前端**（`webui/scripts/` 现有 Node 断言脚本模式）：
  - `fileRecordsFromTurns` 去重/保序/fileId 缺省跳过；
  - `buildChatMessages`：恢复态 turn 在 `uploadedFiles` 回填后生成可预览 file item（url 非空）。
- 回归：`uv run pytest -m "not integration"` + `cd webui && npm test`。

## 6. 提交切分

1. `feat(api): file download endpoint with ownership check`（3.1 + 3.2 + 后端测试）
2. `feat(webui): restore historical file previews via server URLs`（4.1 + 4.2 + 前端测试）

## 7. 风险与边界

- **权限口径**：下载端点与 `_resolve_audit_file_owned_or_404` 完全同口径，不引入新的越权面；下载 URL 不可猜测性依赖 fileId（32 hex），且仍有登录 + 归属双重校验。
- **MinIO/迁移**：不涉及——上传文件仅存本地 upload 目录，与插件 transfer bucket 无关。
- **旧会话兼容**：无 fileId 的历史 turn 维持现状兜底文案。
