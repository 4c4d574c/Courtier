# 资源库模块技术实现文档

> 说明：本文档面向后端/全栈开发者，梳理资源库模块的架构决策、代码改动位置及接口逻辑。

---

## 一、架构决策

资源库定位：**存储权威来源文档**（公安部文件、新华社通稿、人民日报评论等），不是做审核，是做知识结构化——让机器能读懂、能检索、能关联。

### 三层存储架构

| 层级 | 存储内容 | 组件 | 理由 |
|---|---|---|---|
| 原始文件 | PDF/DOCX 二进制 | MinIO | 解耦存储与计算，支持预签名 URL |
| 文本切片 | ~1000 字/块，带标签/日期 | Elasticsearch | 全文检索、聚合筛选、高亮 |
| 元信息 | 标题/作者/MD5/MinIO 路径等 | MySQL | 关系型查询，与现有系统统一 |

### 切片设计

- 上传时轻量提取标签，**不过度提取**
- 汇总时按需读取切片拼接送 LLM
- 切片大小约 **1000 字/块**，优先保持段落完整

---

## 二、基础设施配置

### 2.1 独立 Docker Compose

**文件**：`docker-compose.storage.yml`

与原 `docker-compose.yml` **分离**，避免污染原有服务：

- **MinIO**：`9002:9000`（API）、`9003:9001`（控制台）
  - 原 9000 端口被 `kraken.exe`（Docker Desktop 组件）占用
- **Elasticsearch**：`9200:9200`
  - JVM 内存限制 `-Xms1g -Xmx1g`
  - 磁盘水位线临时调至 `99%`（因宿主 D 盘仅剩 2.4% 空间）
  - 安全关闭：`xpack.security.enabled=false`

**数据挂载**：
- `./data/minio` → MinIO 数据
- `./data/es` → ES 数据

### 2.2 环境变量（`.env`）

新增以下配置项：

```env
# MinIO
MINIO_ENDPOINT=127.0.0.1:9002
MINIO_ACCESS_KEY=docaudit
MINIO_SECRET_KEY=docaudit123456
MINIO_SECURE=false
MINIO_BUCKET_RESOURCES=docaudit-resources
MINIO_BUCKET_DOCUMENTS=docaudit-documents

# Elasticsearch
ES_HOSTS=http://127.0.0.1:9200
ES_INDEX_CHUNKS=docaudit_chunks
```

对应 `src/api/config.py` 中 `Settings` 类新增字段：
- `minio_endpoint`, `minio_access_key`, `minio_secret_key`, `minio_secure`
- `minio_bucket_resources`, `minio_bucket_documents`
- `es_hosts`, `es_index_chunks`

---

## 三、新增模块

### 3.1 MinIO 客户端封装

**文件**：
- `src/miniop/__init__.py`
- `src/miniop/client.py`

**功能**：
- `get_minio_client()`：全局单例
- `ensure_bucket(bucket)`：确保 Bucket 存在
- `put_object(bucket, key, data, content_type)`：上传字节
- `get_object(bucket, key)`：下载字节（P3 导入查重库时用）
- `get_presigned_url(bucket, key, expires)`：生成临时访问 URL
- `remove_object(bucket, key)`：删除对象

**注意**：`presigned_get_object` 需要 `timedelta` 对象，不是秒数。

### 3.2 Elasticsearch 客户端封装

**文件**：
- `src/es/__init__.py`
- `src/es/client.py`

**功能**：
- `get_es_client()`：全局单例，超时 30 秒
- `init_index()`：初始化 `docaudit_chunks` 索引
- `index_chunk(doc_id, body)`：单条索引
- `bulk_index_chunks(actions)`：批量索引（`actions` 为 `{"index": {...}}, {...}` 交替列表）
- `search_chunks(query_body, skip, limit)`：执行搜索
- `delete_by_resource_id(resource_id)`：按 resource_id 删除全部切片

**索引 Mapping**（当前使用默认 `standard` 分析器）：

```python
INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "resource_id": {"type": "integer"},
            "chunk_no": {"type": "integer"},
            "chunk_text": {"type": "text"},
            "title": {"type": "text"},
            "author": {"type": "keyword"},
            "source_id": {"type": "integer"},
            "tags": {"type": "keyword"},
            "publish_date": {"type": "date"},
            "char_count": {"type": "integer"},
            "created_at": {"type": "date"},
        }
    },
}
```

> **IK 分词器已弃用**：ES 8.19.6 与 IK 8.19.6 的 entitlement 机制冲突，安装后写入请求卡死。当前使用 `standard` 分析器，中文按字切分，搜索精度较低，待后续优化。

### 3.3 资源库数据库模型

**文件**：`src/dbop/tables/resource.py`

**表名**：`resources`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int PK | 自增主键 |
| `title` | varchar(512) | 标题 |
| `author` | varchar(256) | 作者 |
| `source` | varchar(256) | 来源 |
| `tags` | varchar(1024) | 标签，逗号分隔 |
| `publish_date` | date | 发布日期 |
| `file_type` | varchar(32) | 文件类型 |
| `file_size` | int | 文件大小（字节） |
| `minio_path` | varchar(512) | MinIO 对象路径 |
| `md5` | varchar(64) | 文件 MD5 |
| `chunk_count` | int | 切片数量 |
| `char_count` | int | 总字符数 |
| `status` | varchar(32) | ready / processing / error |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

已在 `src/dbop/tables/__init__.py` 导出。

---

## 四、核心接口逻辑

### 4.1 文件上传 `POST /api/v1/resources/upload`

**文件**：`src/api/routers/resources.py`

**流程**：
1. `save_upload_file(file, "resources", upload_dir)` 保存本地 → 计算 MD5
2. `put_object(bucket, object_key, file_bytes, content_type)` 上传 MinIO
   - `object_key = f"{md5_hex}/{file.filename}"`
3. `_extract_text(file_path)` 提取纯文本
   - PDF：用 `fitz`（PyMuPDF）逐页 `get_text("text")`
   - DOCX：用 `python-docx` 遍历段落 + 表格
4. `_split_chunks(text, chunk_size=1000)` 按段落聚合切分
   - 优先保持段落完整，单段不超过 1000 字
5. `CRUDRepository(ResourceTable).create(...)` 写入 MySQL
6. `bulk_index_chunks(es_actions)` 批量入 ES
   - action 格式：`{"index": {"_index": "...", "_id": "{resource_id}_{chunk_no}"}}, {doc_body}`
7. 清理本地临时文件

**表单字段**：`file`, `title`, `author`, `source`, `tags`, `publish_date`

### 4.2 列表查询 `POST /api/v1/resources/list`

- 支持按 `title` / `author` / `source` / `tags` 模糊搜索（`ilike`）
- 支持 `skip` / `limit` 分页
- 默认按 `created_at` 降序

### 4.3 单条查询 `POST /api/v1/resources/get`

- 接收 `resource_id`
- 返回完整资源信息

### 4.4 全文搜索 `POST /api/v1/resources/search`

**文件**：`src/api/routers/resources.py` → `search_resources`

**ES Query 构建**：
- `must`：`multi_match` 搜索 `chunk_text^3` + `title^2`
- `filter`：
  - `terms`：标签精确匹配
  - `term`：作者精确匹配
  - `term`：来源 ID 精确匹配
  - `range`：发布日期范围
- `highlight`：`chunk_text`（150 字片段 × 3）+ `title`（100 字片段 × 1）
  - 高亮标签：`<em>` / `</em>`

**返回**：`total` + `hits[]`（含 `highlight` 字段）

### 4.5 删除 `POST /api/v1/resources/delete`

**级联清理**：
1. `delete_by_resource_id(resource_id)` → 删 ES 切片
2. `remove_object(bucket, minio_path)` → 删 MinIO 对象
3. `CRUDRepository(ResourceTable).delete(session, id)` → 删 MySQL 记录

### 4.6 导入查重库 `POST /api/v1/resources/import-to-library`

**文件**：`src/api/routers/resources.py` → `import_to_library`

**流程**：
1. 查 MySQL 获取资源信息（MD5、标题、MinIO 路径）
2. `get_object(bucket, minio_path)` 从 MinIO 下载到临时路径
3. 按段落提取文本（与查重库 `library.py` 的 `_extract_paragraphs` 逻辑一致）
4. 批量写入 `LibraryTable`
   - `doc_id = resource.md5`
   - `doc_name = resource.title`
   - `page_no` / `block_no` / `block_text`
5. 清理临时文件

### 4.7 提取规则 `POST /api/v1/resources/extract-rules`

**文件**：`src/api/routers/resources.py` → `extract_rules_from_resource`

**当前实现（占位）**：
- 以文档标题作为单条规则
- `name = pattern = resource.title`
- `domain_id` 由调用方传入（默认 `GENERAL`）
- `severity = "warning"`
- `is_manual = False`

**预留 LLM 接口注释**：

```python
# TODO: LLM 规则提取算法（待设计）
# 未来流程：
# 1. 从 ES 读取该 resource_id 的全部切片
# 2. 将切片文本（或拼接后的全文）送入 LLM
# 3. Prompt 模板："请从以下公文中提取审核规则，返回 JSON 数组..."
# 4. 解析 LLM 返回的 JSON，批量创建 Rule 记录
```

---

## 五、依赖变更

**文件**：`pyproject.toml`

新增依赖：
```toml
"elasticsearch==8.17.0",
"elastic-transport==8.17.0",
"minio>=7.2.15",
```

> 注意：`elasticsearch` 必须锁定 `8.17.0`，`9.x` 与 ES 8.19.6 服务端不兼容，会导致写入超时。

---

## 六、踩坑记录

| 问题 | 原因 | 解决 |
|---|---|---|
| 9000 端口占用 | `kraken.exe`（Docker Desktop）监听 | MinIO API 改为 9002，控制台改为 9003 |
| `elasticsearch-py` 9.x 导入报错 | `elastic-transport` 版本检测失败 | 降级 `elasticsearch==8.17.0` + `elastic-transport==8.17.0` |
| 循环导入 `ImportError` | `app.py` 顶层导入 `src.miniop` | lifespan 内**延迟导入** |
| ES 写入超时 30s | **D 盘仅剩 2.4%**，触发 `flood stage` 只读 | docker-compose 中调低 `disk.watermark` 至 `99%` |
| ES 写入仍超时 | IK 分词器与 ES 8.18+ entitlement 机制冲突 | **弃用 IK**，回退 `standard` 分析器 |
| `get_object` 未导出 | `src/miniop/__init__.py` 漏导 | 补导出 |

---

## 七、测试脚本

| 脚本 | 测试范围 |
|---|---|
| `tests/scripts/test_storage_connection.py` | MinIO + ES 连接 |
| `tests/scripts/test_resources_api.py` | 上传 / 列表 / 查询 / 删除 |
| `tests/scripts/test_resources_search.py` | 关键词搜索 / 筛选 / 高亮 / 无匹配 |
| `tests/scripts/test_resources_import.py` | 导入查重库 + 验证 library 表 |
| `tests/scripts/test_resources_extract_rules.py` | 提取规则 + 验证 rules 表 |

---

## 八、待办事项

1. **中文分词优化**：当前 `standard` 分析器按字切分，搜索精度低。待解决 ES 8.19.6 + IK 兼容问题，或降级 ES 到 8.17.x。
2. **LLM 规则提取**：`extract-rules` 接口已预留注释，待设计 Prompt 和解析逻辑。
3. **P5 审核案例入库**：由其他同事负责。
