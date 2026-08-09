# EPUB 概念词条图谱与 100% 原文检索系统规范 (EPUB Concept Wiki & Grounded Search Spec)

本文档归档于项目 repository 中，作为 EPUB 概念图谱构建与原文检索模块的权威设计规范与状态文档。

---

## 架构总体设计 (System Architecture)

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                         AuraPro Desktop 客户端 (Electron)                      │
│   ┌───────────────────────────┐                ┌───────────────────────────┐   │
│   │ EPUB 导入 & 图谱构建 UI   │                │ 概念查询 & 原文回显 UI    │   │
│   └─────────────┬─────────────┘                └─────────────▲─────────────┘   │
└─────────────────┼─────────────────────────────────────────────┼────────────────┘
                  │                                             │
                  │  (IPC / Remote HTTP API)                    │  (Passages + TOC)
                  ▼                                             │
┌───────────────────────────────────────────────────────────────┴────────────────┐
│                   AuraPro Backend 服务层 (FastAPI Backend)                      │
│                                                                                │
│  【离线 Batch 批处理建库 Pipeline】                 【在线检索 Pipeline】      │
│   1. EPUB 结构化解析 (EpubParser)                   1. 查询理解与概念定位      │
│      └─ 提取章节/TOC目录/自然段                           ├─ Tier 1: Trie 字典全匹配(0ms)│
│   2. 采样验证 & Batch JSONL 构建                     └─ Tier 2: 本地轻量LLM回退│
│      ├─ 10万字小样本验证 & Prompt调优                  (Qwen2.5-1.5B/3B 本地推理)│
│      └─ 全量文本导出为 OpenAI/Anthropic Batch JSONL 2. 混合召回通道            │
│   3. Batch 异步提交与结果回收落地                     ├─ 通道 A: 图谱精确召回  │
│      ├─ 24小时异步 Batch API (50% 成本)             └─ 通道 B: 向量语义召回  │
│      └─ 解析 JSON 写入数据库 (Passages/Concepts)        3. Cross-Encoder 精排序     │
│                                                       ├─ 相关度打分与截断    │
│                                                       └─ MMR 多样性去重      │
└───────────────────────────────┬────────────────────────────────────────────────┘
                                │
                                ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│                         统一数据存储层 (PostgreSQL / SQLite)                    │
│   ┌────────────────────────┐┌────────────────────────┐┌─────────────────────┐ │
│   │ passages (段落+TOC目录)││ concepts (概念图谱/别名)││ embeddings (向量)   │ │
│   └────────────────────────┘└────────────────────────┘└─────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────┘
```

---

## 一、 详细模块规范 (Detailed Component Specifications)

### 1. 离线解析与段落切片模块 (`epub_parser`)

- **输入**：`.epub` 电子书文件。
- **解析策略**：
  - 深度遍历 EPUB 标准目录结构（`NCX` / `NAV` 表）。
  - 构建多级 TOC 路径树（如：`计算机网络 > 第3章 传输层 > 3.4 TCP 拥塞控制`）。
  - 按 HTML `<p>` 元素或自然段拆分为 Child Paragraph，关联 Parent Section 目录。
- **存储数据结构 (`passages` 表)**：
  - `passage_id` (PK): `book_id + chapter_index + passage_index`
  - `book_title`: 书名 (如 `"深度学习导论"`)
  - `toc_path`: 完整的层级目录数组 (如 `["第5章 卷积网络", "5.2 残差模块"]`)
  - `content`: 100% 原始自然段字符串
  - `parent_context`: 包含前后段落或小节标题的父上下文

### 2. Batch 批处理概念抽取与 Wiki 融合引擎 (`batch_concept_pipeline`)

为了实现海量书籍（数千万字）的高效离线建库，并最大程度降低 LLM Token 成本（享受 50% 折扣与超高并发额度），离线建库采用 **Batch API 异步流水线**：

- **阶段一：样本采样与 Prompt 调试 (Sample Prompt Tuning)**
  - 抽取约 10 万字段落作为测试样本。
  - 调用线上 API (OpenAI/Anthropic) 校验概念抽取格式、JSON Schema 约束与抽取粒度，直到满意。
- **阶段二：Batch JSONL 文件构建 (JSONL Construction)**
  - 将海量图书段落按照批处理规范生成 `.jsonl` 请求文件（符合 OpenAI Batch `/v1/batches` 或 Anthropic Message Batches 格式）。
  - 每条记录包含 `custom_id` (`passage_id`) 和 Structured Outputs JSON 提示词。
- **阶段三： Batch API 异步提交与轮询 (Async Submission & Polling)**
  - Python 脚本提交 `.jsonl` 文件至 Batch 接口。
  - 自动后台轮询状态（允许 24 小时内异步吐出分析好的 JSON 结果）。
- **阶段四：结果解析与全局 Wiki 归一化 (Batch Result Processing)**
  - 批量解析吐出的 JSONL 结果文件。
  - **种子词典匹配 + Wiki 融合消歧**：结合外部种子词表（如 `glossary_*.json`），将新抽取的概念、别名与 `Wiki_Concept_Index` 动态归一化合并，建立 `concept_id` ↔ `[passage_id]` 映射网。

- **存储数据结构 (`concepts` 表)**：
  - `concept_id` (PK): `CONCEPT_xxxx`
  - `canonical_name`: 主名称 (如 `"残差网络"`)
  - `aliases`: 别名JSON数组 (如 `["ResNet", "Residual Network", "深度残差网络"]`)
  - `definition`: LLM 总结的标准定义说明
  - `occurrences`: 出现的段落集合与关联权重 `[{"passage_id": "...", "book_title": "..."}]`

### 3. 存储层设计 (`storage_layer`) — 已实现

- **独立 SQLite 数据库**：创建单独的 `epub_concept_v1.db` 文件（默认位于 `DATA_DIR/epub_concept_v1.db`，可由 `EPUB_CONCEPT_DB_PATH` 覆盖），与主 `webui.db` 完全解耦，便于独立导入/导出/分享概念图谱数据。
- **实现模块**：[store.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/retrieval/epub/store.py)（`SQLiteEpubStore`）
- **表结构**：
  | 表名 | 用途 | 主要字段 |
  |------|------|----------|
  | `books` | EPUB 图书注册表 | `book_id`, `title`, `current_version_id` |
  | `book_versions` | 图书版本与导入状态机 | `version_id`, `book_id`, `content_hash`, `status`, `parser_version` |
  | `epub_blobs` | 原始 EPUB 字节留存 | `version_id`, `content`, `byte_count` |
  | `toc_nodes` | 目录树节点（自引用层级） | `toc_node_id`, `version_id`, `parent_toc_node_id`, `title`, `spine_index`, `ordinal` |
  | `passages` | 100% 忠实原始段落 | `passage_id`, `version_id`, `toc_node_id`, `content`, `content_kind`, `ordinal` |
  | `retrieval_units` | 段落切分后的检索/向量单元 | `retrieval_unit_id`, `passage_id`, `start_codepoint`, `end_codepoint`, `embedding_profile`, `vector_state` |
  | `concepts` | 概念词条 | `concept_id`, `canonical_name`, `normalized_name`, `definition`, `status` |
  | `concept_aliases` | 别名快速反查索引 | `alias_id`, `concept_id`, `alias`, `normalized_alias`, `source` |
  | `concept_mentions` | 概念 ↔ 段落带偏移的证据映射 | `mention_id`, `concept_id`, `passage_id`, `start_codepoint`, `end_codepoint`, `evidence` |
  | `concept_relations` | 概念间关系三元组 | `relation_id`, `subject_concept_id`, `predicate`, `object_concept_id` |
  | `concept_relation_assertions` | 关系在某一图书版本上的断言与状态 | `assertion_id`, `relation_id`, `version_id`, `status`, `source` |
  | `concept_relation_evidence` | 关系断言的原文证据 | `relation_evidence_id`, `assertion_id`, `passage_id`, `start_codepoint`, `end_codepoint`, `evidence` |
  | `batch_jobs` | Batch 作业记录 | `batch_job_id`, `version_id`, `provider`, `provider_job_id`, `profile_name`, `status` |
  | `batch_items` | Batch 单条请求与回收结果 | `batch_item_id`, `batch_job_id`, `passage_id`, `custom_id`, `status`, `request_json`, `response_json`, `failure_diagnostics_json`, `skipped_self_relations`, `skipped_short_evidence`, `skipped_ambiguous_concepts` |
- **特性**：WAL journal mode、线程安全连接管理、带版本号的迁移执行器（`schema_migrations` 表 + `PRAGMA user_version`，当前 `SCHEMA_VERSION = 10`），启动时自动建库与增量迁移
- `failure_diagnostics_json` 只记录失败条目的**无内容**量化指标（失败类别 slug、码点长度、出现次数、布尔标志），用于调优 Prompt；绝不写入原文、证据串、锚点、模型输出或原始供应商错误。
- `skipped_self_relations` 记录本条目入库时因两端已被管理员合并为同一概念而跳过的关系条数（SDD 4.2.2 第 6 点之 a）。该列由 schema 约束为整数或 NULL，因此结构上不可能承载概念名或原文；NULL 表示“未度量”（`CONCEPT_MENTIONS` 条目、未成功条目、或迁移前的旧行）。
- `skipped_short_evidence` 记录本条目 grounding 阶段因低于该 Prompt Profile **实际执行**的证据下限而被丢弃的 span 条数（SDD 4.2.2 第 6 点之 b）；概念 mention 与关系 evidence 共用一个计数器，因为二者是同一缺陷、同一修法。丢弃的 span 按定义不在 `response_json` 中，所以该列是它们存在过的唯一记录。同样由 schema 约束为整数或 NULL；NULL 表示“未度量”（未经 grounding 的条目，或迁移前的旧行）。注意“实际执行”的下限低于 Prompt 指令中**要求**的字数：要求值促使模型给出有区分度的引文，执行值只拒绝真正不可用的片段，二者在 Profile 上分列并各自钉死。
- `skipped_ambiguous_concepts` 记录本条目入库时因某个概念的名称与别名同时命中**多个**既有概念、无法在不擅自断定二者相同的前提下挂接而被跳过的概念条数（SDD 4.2.2 第 6 点之 c）。该概念的 mention 随之一并跳过，凡以它为端点的关系也一并丢弃且**不另行计数**——计数器记录的是成因，级联结果可从 `response_json` 推得，这与 `skipped_short_evidence` 只数被丢弃的 span、不数因此失去证据的关系是同一取舍。**与 `skipped_short_evidence` 的关键不对称**：过短 span 是在只读的 grounding 阶段被剔除的，因此按定义**不在** `response_json` 中；而概念歧义只有到**写入**阶段（`_resolve_or_create_concept` 是一次写操作）才能发现，所以被跳过的概念连同其名称、别名与 mention 仍**原样保留**在 `response_json` 中，与被跳过的自环关系一致。该列记录的是这次写入**做了什么**，而非 `response_json` **缺了什么**——这是刻意为之：`response_json` 必须在重放时逐字节一致，入库幂等性正建立在此之上，若写入阶段回头改写载荷以反映自身跳过，该保证即告失效。同样由 schema 约束为整数或 NULL；但与上面两列不同，本列在两种作业类型上**都会度量**（任何成功条目都要解析概念），因此 `0` 恒为真实的零，NULL 只表示“该行写于本列存在之前”。
- **远程/私有部署**：后续可替换为 PostgreSQL + `pgvector`，当前先以 SQLite 为主。
- 保留 100% 原文，不存储任何篡改或加工后的段落内容。

### 4. 在线混合查询与重排流水线 (`query_pipeline`)

- **Step 1: 极速概念定位 (Tiered Concept Lookup)**
  - **Tier 1 (0ms 判定)**：在内存 Trie/Automaton 匹配用户提问。若精准包含词条或别名，直接定位目标 `concept_id`。
  - **Tier 2 (本地轻量 LLM Fallback)**：未能在字典中精准命中时，调用**本地轻量开源模型**（如 `Qwen2.5-1.5B` / `Qwen2.5-3B` / `Llama-3.2-1B`，通过 AuraPro Desktop 现有的 `llama.cpp` 或 `Ollama` 本地推理引擎运行）。秒级（1s~2s）延迟完全可接受，保证客户端查询**完全免费、离线可用且零隐私泄露**。
- **Step 2: 混合多路召回 (Hybrid Multi-Recall)**
  - **图谱精准路 (Channel A)**：通过命中的 `concept_id`，从 SQLite `concept_occurrences` 表中直接获取所有出现过的 `passage_id` 集合（绝对无漏召）。
  - **向量语义路 (Channel B)**：计算提问 Embedding，在向量库检索 Top-K 语义关联段落（防错召与补隐式相关段）。
  - 取两路 Candidate 集交/并集（约 20~50 段）。
- **Step 3: Cross-Encoder 精排序与 MMR 去重 (Reranking & Diversity)**
  - 使用 `BGE-Reranker-v2` 对 `(User Question, Passage Content)` 交叉打分。
  - 引入 MMR（最大边际相关性）或同章节邻近段落合并逻辑，防止相邻 duplicate 段落刷屏。
  - 依据分值差值实现**动态截断**，确定返回 1 到 N 段。
- **Step 4: 结果结构化渲染 (Standard Payload)**
  - 输出格式包含：
    1. 100% 忠实原文内容 (`content`)
    2. 来源书籍 (`book_title`)
    3. 层级目录面包屑 (`toc_path`)
    4. 命中关联概念 (`matched_concept`)

---

## 二、 代码模块组织 (Code Architecture Plan)

> 本节从属于 [epub_concept_sdd.md](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/docs/specs/epub_concept_sdd.md)；如有冲突，以 SDD 为准。

### 解析层 (`backend/open_webui/retrieval/parsers/epub/`)

纯解析、无持久化，按格式版本演进（`PARSER_FORMAT_VERSION`）。

| 模块             | 职责                                                                        |
| ---------------- | --------------------------------------------------------------------------- |
| `archive.py`     | ZIP 归档安全校验（zip-bomb / 路径穿越 / 体积上限）                          |
| `package.py`     | `container.xml`、OPF 清单与 spine 解析，`href` 归一                         |
| `toc.py`         | NAV (EPUB3) 与 NCX (EPUB2) 目录解析                                         |
| `toc_mapping.py` | 段落 → 目录节点（面包屑）映射                                               |
| `xhtml.py`       | XHTML 可见正文抽取与锚点收集                                                |
| `model.py`       | `EpubParseResult` / `ParsedPassage` / `TocEntry` / `ParserWarning` 等值对象 |
| `parser.py`      | `EPUBParser.parse_book()` 与模块级 `parse_epub()` 入口                      |

### 领域层 (`backend/open_webui/retrieval/epub/`)

| 模块                                                          | 职责                                                      |
| ------------------------------------------------------------- | --------------------------------------------------------- |
| `store.py`                                                    | 独立 SQLite 持久化层 `SQLiteEpubStore` 与版本化迁移执行器 |
| `batch.py`                                                    | Batch 作业编排：JSONL 生成、提交、轮询与结果回收落库      |
| `search.py`                                                   | 混合多路召回与重排的在线查询流水线                        |
| `section_graph.py`                                            | 章节/目录图谱与邻近段落合并                               |
| `retrieval_units.py`                                          | 段落切分为检索单元                                        |
| `vector_index.py` / `sqlite_vec.py` / `sqlite_vec_backend.py` | 向量索引抽象与 `sqlite-vec` 后端实现                      |
| `inference.py` / `prompt_profiles.py` / `calibration.py`      | Tier-2 本地推理调用、提示词配置与校准                     |
| `desktop_runtime.py`                                          | 读取 Desktop 下发的本地运行时描述符                       |

### 服务层 (`backend/open_webui/services/`)

1. [epub_concept.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/services/epub_concept.py): EPUB 概念检索领域服务（导入、索引、查询用例编排）
2. [epub_runtime.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/services/epub_runtime.py): 生命周期装配（建库、启停、RAG 推理策略配置）

### HTTP 层 (`backend/open_webui/routers/`)

1. [epub.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/routers/epub.py): 唯一的 EPUB 路由（前缀 `/api/v1/epub`）。**所有路由均鉴权**：只读接口要求 `get_verified_user`，导入、销毁、词表、索引与 Batch 等写操作要求 `get_admin_user`。
2. [main.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/main.py): 路由注册与 `epub_runtime` 生命周期挂载

### Frontend Components (`AuraPro-WebUI`)

1. [src/lib/apis/epub/index.ts](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/src/lib/apis/epub/index.ts): `/api/v1/epub` 前端 API 客户端
2. [src/routes/(app)/epub/+page.svelte](<file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/src/routes/(app)/epub/+page.svelte>): 用户侧概念检索与阅读界面
3. [src/routes/(app)/admin/epub/+page.svelte](<file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/src/routes/(app)/admin/epub/+page.svelte>): 管理员侧导入、索引与 Batch 管理界面

### Desktop Client Components (`AuraPro-Desktop`)

Desktop **不是独立的功能客户端**，不承载任何 EPUB 功能 UI，也不接收 EPUB 专用 IPC、凭据或写权限。其唯一职责是**本地运行时供给**：

1. `src/main/utils/llamacpp.ts` / `src/main/utils/index.ts`: llama.cpp 运行时与模型的下载、校验与生命周期管理
2. 运行时描述符落盘（`AURAPRO_DESKTOP_LLM_RUNTIME_FILE`），供 WebUI 后端的 `retrieval/epub/desktop_runtime.py` 读取以启用 Tier-2 本地推理

---

## 三、 验证与测试计划 (Verification Plan)

1. EPUB 目录树解析单元测试。
2. Batch JSONL 异步提交与回收落库测试。
3. 本地轻量模型 NER 识别与延迟测试。
4. 100% 原文忠实度与 TOC 面包屑匹配测试。
