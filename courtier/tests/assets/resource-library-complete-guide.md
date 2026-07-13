# 资源库模块完整开发部署手册

> 本文档为资源库模块的一站式参考，涵盖架构设计、后端实现、前端对接、部署步骤及踩坑记录。

---

## 目录

1. [架构概述](#一架构概述)
2. [基础设施](#二基础设施)
3. [后端实现](#三后端实现)
4. [接口清单与前端对接](#四接口清单与前端对接)
5. [部署步骤](#五部署步骤)
6. [测试验证](#六测试验证)
7. [已知问题与优化方向](#七已知问题与优化方向)
8. [踩坑记录](#八踩坑记录)

---

## 一、架构概述

### 1.1 定位

资源库存储**权威来源文档**（公安部文件、新华社通稿、人民日报评论等），核心目标是**知识结构化**：让机器能检索、能关联、能参与审核。

### 1.2 三层存储架构

| 层级 | 存储内容 | 组件 | 理由 |
|---|---|---|---|
| 原始文件 | PDF/DOCX 二进制 | MinIO | 解耦存储与计算，支持预签名 URL |
| 文本切片 | ~1000 字/块，带标签/日期 | Elasticsearch | 全文检索、聚合筛选、高亮 |
| 元信息 | 标题/作者/MD5/MinIO 路径等 | MySQL | 关系型查询，与现有系统统一 |

### 1.3 功能模块

| 模块 | 功能 | 阶段 |
|---|---|---|
| 文档上传 | 上传 PDF/DOCX，填写元信息 | P1 |
| 文档列表 | 分页展示，支持模糊搜索 | P1 |
| 文档详情 | 查看完整信息，提供操作入口 | P1 |
| 全文检索 | 关键词搜索，支持筛选与高亮 | P2 |
| 导入查重库 | 一键将文档段落写入查重库 | P3 |
| 提取规则 | 从文档提取审核规则（占位，待接 LLM） | P4 |
| 审核案例入库 | 审核结果转案例存入资源库 | P5（其他同事负责） |

---

## 二、基础设施

### 2.1 Docker Compose（独立存储服务）

**文件**：`docker-compose.storage.yml`

与原 `docker-compose.yml` 分离，避免污染：

- **MinIO**：`9002:9000`（API）、`9003:9001`（控制台）
  - 账号/密码：`docaudit` / `docaudit123456`
- **Elasticsearch**：`9200:9200`
  - JVM 内存：`-Xms1g -Xmx1g`
  - 安全关闭：`xpack.security.enabled=false`
  - 磁盘水位线临时调至 `99%`（因宿主 D 盘空间紧张）

**数据挂载**：
- `./data/minio` → MinIO 数据
- `./data/es` → ES 数据

### 2.2 拉取镜像命令

```bash
# 官方源
docker pull quay.io/minio/minio:RELEASE.2025-07-23T15-54-02Z
docker pull docker.elastic.co/elasticsearch/elasticsearch:8.19.6

# 阿里云加速
docker pull registry.cn-hangzhou.aliyuncs.com/minio/minio:RELEASE.2025-07-23T15-54-02Z
docker pull registry.cn-hangzhou.aliyuncs.com/elasticsearch/elasticsearch:8.19.6
```

### 2.3 环境变量（`.env`）

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

### 2.4 Python 依赖

**文件**：`pyproject.toml`

```toml
"elasticsearch==8.17.0",
"elastic-transport==8.17.0",
"minio>=7.2.15",
```

> **必须锁定 `8.17.0`**，`9.x` 与 ES 8.19.6 服务端不兼容，会导致写入超时。

---

## 三、后端实现

### 3.1 新增/修改文件清单

#### 新增文件

```
docaudit/
├── docker-compose.storage.yml
├── src/
│   ├── miniop/
│   │   ├── __init__.py
│   │   └── client.py
│   ├── es/
│   │   ├── __init__.py
│   │   └── client.py
│   ├── dbop/tables/
│   │   └── resource.py
│   ├── api/schemas/
│   │   └── resource.py
│   └── api/routers/
│       └── resources.py
└── tests/scripts/
    ├── test_storage_connection.py
    ├── test_resources_api.py
    ├── test_resources_search.py
    ├── test_resources_import.py
    └── test_resources_extract_rules.py
```

#### 修改文件

| 文件 | 修改内容 |
|---|---|
| `.env` | 新增 `MINIO_*`、`ES_HOSTS`、`ES_INDEX_CHUNKS` |
| `pyproject.toml` | 新增 `elasticsearch`、`elastic-transport`、`minio` 依赖 |
| `src/api/config.py` | `Settings` 新增 `minio_*`、`es_*` 字段 |
| `src/api/app.py` | lifespan 延迟导入初始化；注册 `/api/v1/resources` 路由 |
| `src/dbop/tables/__init__.py` | 导出 `ResourceTable` |

### 3.2 核心模块说明

#### MinIO 客户端（`src/miniop/client.py`）

- `get_minio_client()`：全局单例
- `ensure_bucket(bucket)`：确保 Bucket 存在
- `put_object(bucket, key, data, content_type)`：上传字节
- `get_object(bucket, key)`：下载字节
- `get_presigned_url(bucket, key, expires)`：生成临时访问 URL
- `remove_object(bucket, key)`：删除对象

#### ES 客户端（`src/es/client.py`）

- `get_es_client()`：全局单例，超时 30 秒
- `init_index()`：初始化 `docaudit_chunks` 索引
- `index_chunk(doc_id, body)`：单条索引
- `bulk_index_chunks(actions)`：批量索引
- `search_chunks(query_body, skip, limit)`：执行搜索
- `delete_by_resource_id(resource_id)`：按 resource_id 删除全部切片

**索引 Mapping**（使用 `standard` 分析器）：

```json
{
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
      "created_at": {"type": "date"}
    }
  }
}
```

#### 数据库模型（`src/dbop/tables/resource.py`）

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

### 3.3 核心接口逻辑

#### 上传 `POST /api/v1/resources/upload`

1. 保存本地临时文件 → 计算 MD5
2. `put_object` 上传 MinIO（`object_key = {md5}/{filename}`）
3. 提取纯文本（PDF 用 `fitz`，DOCX 用 `python-docx`）
4. 按段落聚合切片（~1000 字/块，优先保持段落完整）
5. 写入 MySQL `resources` 表
6. `bulk_index_chunks` 批量入 ES
7. 清理临时文件

#### 全文搜索 `POST /api/v1/resources/search`

- `must`：`multi_match` 搜索 `chunk_text^3` + `title^2`
- `filter`：`terms` 标签 / `term` 作者 / `term` 来源 / `range` 日期
- `highlight`：`chunk_text`（150 字片段 ×3）+ `title`（100 字片段 ×1）
- 返回：`total` + `hits[]`（含 `highlight` 字段）

#### 删除 `POST /api/v1/resources/delete`

级联清理：
1. 删 ES 切片
2. 删 MinIO 对象
3. 删 MySQL 记录

#### 导入查重库 `POST /api/v1/resources/import-to-library`

1. 查 MySQL 获取资源信息
2. `get_object` 从 MinIO 下载到临时文件
3. 按段落提取文本
4. 批量写入 `LibraryTable`（`doc_id = resource.md5`）
5. 清理临时文件

#### 提取规则 `POST /api/v1/resources/extract-rules`

**当前占位实现**：
- 以文档标题作为单条规则入库
- `name = pattern = resource.title`
- `domain_id` 由调用方传入（默认 `GENERAL`）

**预留 LLM 链路**（`src/api/routers/resources.py` 中有 TODO 注释）：
```python
# TODO: LLM 规则提取算法
# 1. 从 ES 读取该 resource_id 的全部切片
# 2. 将切片文本送入 LLM
# 3. Prompt: "请从以下公文中提取审核规则，返回 JSON 数组..."
# 4. 解析 JSON，批量创建 Rule 记录
```

---

## 四、接口清单与前端对接

**所有接口前缀**：`/api/v1/resources`

### 4.1 上传

```http
POST /api/v1/resources/upload
Content-Type: multipart/form-data
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | File | 是 | PDF 或 DOCX |
| `title` | string | 是 | 文档标题 |
| `author` | string | 否 | 作者 |
| `source` | string | 否 | 来源 |
| `tags` | string | 否 | 逗号分隔标签 |
| `publish_date` | string | 否 | `YYYY-MM-DD` |

**响应**：
```json
{
  "id": 1,
  "title": "关于安全生产的重要讲话",
  "file_type": "docx",
  "file_size": 38389,
  "chunk_count": 1,
  "char_count": 983,
  "md5": "d53dd4b550517ee766e43b4a995c734b",
  "created_at": "2026-04-22T21:19:42"
}
```

### 4.2 列表

```http
POST /api/v1/resources/list
Content-Type: application/json
```

**请求体**：
```json
{
  "title": "安全",
  "author": null,
  "source": null,
  "tags": null,
  "skip": 0,
  "limit": 20
}
```

### 4.3 单条查询

```http
POST /api/v1/resources/get
Content-Type: application/json
```

```json
{"resource_id": 1}
```

### 4.4 全文搜索（核心）

```http
POST /api/v1/resources/search
Content-Type: application/json
```

**请求体**：
```json
{
  "query": "安全生产",
  "tags": ["安全生产"],
  "author": "测试作者",
  "source_id": null,
  "date_from": "2024-01-01",
  "date_to": "2024-12-31",
  "skip": 0,
  "limit": 20
}
```

**响应**：
```json
{
  "total": 4,
  "hits": [
    {
      "resource_id": 1,
      "chunk_no": 0,
      "chunk_text": "安全生产是关系人民群众生命财产安全的大事...",
      "title": "关于安全生产的重要讲话",
      "author": "测试作者",
      "tags": ["安全生产", "测试"],
      "publish_date": "2024-01-01",
      "highlight": {
        "chunk_text": [
          "<em>安</em><em>全</em>生产是关系人民群众生命财产<em>安</em><em>全</em>的大事..."
        ],
        "title": ["关于<em>安</em><em>全</em>生产的重要讲话"]
      }
    }
  ]
}
```

**前端注意**：
- `<em>` 标签需要渲染为高亮样式（如黄色背景）
- 搜索结果展示：标题 + 高亮片段 + 标签/作者/日期

### 4.5 删除

```http
POST /api/v1/resources/delete
Content-Type: application/json
```

```json
{"resource_id": 1}
```

**注意**：级联删除 MySQL、ES、MinIO 数据，需二次确认。

### 4.6 导入查重库

```http
POST /api/v1/resources/import-to-library
Content-Type: application/json
```

```json
{"resource_id": 1}
```

**响应**：
```json
{
  "resource_id": 1,
  "doc_id": "d53dd4b550517ee766e43b4a995c734b",
  "doc_name": "关于安全生产的重要讲话",
  "paragraph_count": 23
}
```

### 4.7 提取规则

```http
POST /api/v1/resources/extract-rules
Content-Type: application/json
```

```json
{
  "resource_id": 1,
  "domain_id": "GOV"
}
```

**领域枚举**：`GENERAL`（通用）、`GOV`（政务）、`AUD`（审计）、`ECON`（经济）、`FIN`（财政）、`INDU`（产业）...

**响应**：
```json
{
  "resource_id": 1,
  "rules_created": 1,
  "rule_ids": [445]
}
```

### 4.8 页面结构建议

#### 资源库列表页 `/resources`

```
+---------------------------------------------------+
| 资源库                              [+ 上传文档]   |
+---------------------------------------------------+
| 搜索: [________]  标签: [____]  作者: [____] [搜索] |
+---------------------------------------------------+
| 标题 | 作者 | 来源 | 标签 | 日期 | 操作 |
| 关于安全... | 张三 | 新华社 | 安全,讲话 | 2024-01 | [详情][删除][导入查重][提取规则] |
+---------------------------------------------------+
| < 1 2 3 ... 10 >                                  |
+---------------------------------------------------+
```

#### 资源库搜索页 `/resources/search`

```
+---------------------------------------------------+
| 关键词: [________]                                |
| 标签:   [□ 安全生产] [□ 讲话] [□ 规范] ...         |
| 作者:   [________]  日期: [____] ~ [____] [搜索]   |
+---------------------------------------------------+
| 找到 4 条结果                                     |
+---------------------------------------------------+
| [关于安全生产的重要讲话]                          |
| ...<em>安</em><em>全</em>生产是关系人民群众...    |
| 作者: 张三 | 标签: 安全生产,讲话 | 2024-01-01      |
+---------------------------------------------------+
```

---

## 五、部署步骤

### 5.1 启动存储服务

```bash
cd /path/to/docaudit
docker compose -f docker-compose.storage.yml up -d
```

验证：
- MinIO 控制台：`http://localhost:9003`
- ES 状态：`curl http://localhost:9200`

### 5.2 安装依赖

```bash
uv sync
# 或：uv pip install "elasticsearch==8.17.0" "elastic-transport==8.17.0" "minio>=7.2.15"
```

### 5.3 启动后端

```bash
uv run python main.py
```

验证：`http://localhost:8000/docs` 能看到 `/api/v1/resources` 接口。

---

## 六、测试验证

```bash
# P0：MinIO + ES 连接
uv run python tests/scripts/test_storage_connection.py

# P1：上传 / 列表 / 查询 / 删除
uv run python tests/scripts/test_resources_api.py

# P2：搜索 + 筛选 + 高亮
uv run python tests/scripts/test_resources_search.py

# P3：导入查重库
uv run python tests/scripts/test_resources_import.py

# P4：提取规则
uv run python tests/scripts/test_resources_extract_rules.py
```

全部通过即部署成功。

---

## 七、已知问题与优化方向

### 7.1 中文分词精度低

当前 ES 使用 `standard` 分析器，中文按**字切分**。搜索"安全生产"会匹配包含"安""全""生""产"的文档。

**原因**：ES 8.19.6 与 IK 8.19.6 的 entitlement 机制冲突，安装后写入卡死。

**优化方向**：降级 ES 到 8.17.x，安装 IK 分词器。

### 7.2 磁盘水位线

ES 默认磁盘水位线为 95%，`docker-compose.storage.yml` 中临时调至 99%。建议：
- 清理磁盘空间至 10% 以上
- 或把 `./data/es` 挂载到其他分区

### 7.3 LLM 规则提取

`extract-rules` 接口当前为占位实现，仅提取标题。后续需设计 Prompt 和解析逻辑，从文档中自动提取多条结构化规则。

### 7.4 文件预览/下载

当前后端未提供单独的下载接口。建议后续补充：
```
GET /api/v1/resources/{id}/download → 返回 MinIO 预签名 URL
```

---

## 八、踩坑记录

| 问题 | 原因 | 解决 |
|---|---|---|
| 9000 端口占用 | `kraken.exe`（Docker Desktop）监听 | MinIO API 改为 9002，控制台改为 9003 |
| `elasticsearch-py` 9.x 导入报错 | `elastic-transport` 版本检测失败 | 降级 `elasticsearch==8.17.0` + `elastic-transport==8.17.0` |
| 循环导入 `ImportError` | `app.py` 顶层导入 `src.miniop` | lifespan 内**延迟导入** |
| ES 写入超时 30s | **D 盘仅剩 2.4%**，触发 `flood stage` 只读 | docker-compose 中调低 `disk.watermark` 至 `99%` |
| ES 写入仍超时 | IK 分词器与 ES 8.18+ entitlement 机制冲突 | **弃用 IK**，回退 `standard` 分析器 |
| `get_object` 未导出 | `src/miniop/__init__.py` 漏导 | 补导出 |

---

## 附录：项目结构速览

```
docaudit/
├── docker-compose.storage.yml      # 存储服务（MinIO + ES）
├── main.py                          # 后端入口
├── pyproject.toml                   # 依赖配置
├── .env                             # 环境变量
├── src/
│   ├── api/
│   │   ├── app.py                   # FastAPI 应用
│   │   ├── config.py                # 配置
│   │   ├── dependencies.py          # 数据库会话依赖
│   │   ├── routers/
│   │   │   ├── resources.py         # 资源库 7 个接口
│   │   │   └── ...
│   │   └── schemas/
│   │       └── resource.py          # 请求/响应模型
│   ├── dbop/
│   │   ├── db_manager.py            # CRUD 封装
│   │   └── tables/
│   │       ├── resource.py          # resources 表
│   │       └── __init__.py
│   ├── es/
│   │   ├── __init__.py
│   │   └── client.py                # ES 客户端封装
│   └── miniop/
│       ├── __init__.py
│       └── client.py                # MinIO 客户端封装
├── tests/scripts/                   # 测试脚本
└── docs/                            # 技术文档
    ├── resource-library-complete-guide.md   # 本文件
    ├── resource-library-implementation.md   # 后端技术文档
    ├── resource-library-frontend-guide.md   # 前端开发指南
    └── resource-library-setup-guide.md      # 部署使用说明
```

---

> 文档生成时间：2026-04-22
> 适用版本：资源库 P0-P4 完成后版本
