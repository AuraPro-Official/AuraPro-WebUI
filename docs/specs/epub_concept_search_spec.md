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
- **独立 SQLite 数据库**：创建单独的 `epub_concept.db` 文件（位于 `DATA_DIR/epub_concept.db`），与主 `webui.db` 完全解耦，便于独立导入/导出/分享概念图谱数据。
- **实现模块**：[epub_concept_db.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/utils/epub_concept_db.py)
- **表结构**：
  | 表名 | 用途 | 主要字段 |
  |------|------|----------|
  | `books` | EPUB 图书注册表 | `book_id`, `book_title`, `file_hash`, `total_passages` |
  | `passages` | 100% 忠实原始段落 | `passage_id`, `book_id`, `toc_path_json`, `content`, `parent_context` |
  | `concepts` | 概念词条与别名 | `concept_id`, `canonical_name`, `aliases_json`, `definition` |
  | `concept_occurrences` | 概念 ↔ 段落多对多映射 | `concept_id`, `passage_id`, `book_title` |
  | `alias_index` | 别名快速反查索引 | `alias_lower` → `concept_id` |
- **特性**：WAL journal mode、线程安全单例连接管理、启动时自动从 SQLite 恢复内存索引
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

### Backend Components (`AuraPro-WebUI/backend/open_webui`)
1. [epub_parser.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/retrieval/loaders/epub_parser.py): EPUB NCX/NAV 结构化解析
2. [epub_concept_db.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/utils/epub_concept_db.py): **独立 SQLite 持久化层** (books/passages/concepts/alias_index/occurrences 五张表)
3. [batch_pipeline.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/utils/batch_pipeline.py): Batch API 批处理控制器
4. [concept_wiki.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/utils/concept_wiki.py): 概念图谱与 Wiki 管理器 (内存索引 + SQLite 持久化)
5. [epub_concept.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/routers/epub_concept.py): API 路由端点
6. [main.py](file:///Volumes/codes/workspace/aurapro_new/AuraPro-WebUI/backend/open_webui/main.py): 路由注册

### Desktop Client Components (`AuraPro-Desktop`)
1. [EpubConceptSearch.svelte](file:///Volumes/codes/workspace/aurapro_new/AuraPro-Desktop/src/renderer/src/components/EpubConceptSearch.svelte): 概念检索与解析 UI
2. [preload/index.ts](file:///Volumes/codes/workspace/aurapro_new/AuraPro-Desktop/src/preload/index.ts): Preload 桥接

---

## 三、 验证与测试计划 (Verification Plan)
1. EPUB 目录树解析单元测试。
2. Batch JSONL 异步提交与回收落库测试。
3. 本地轻量模型 NER 识别与延迟测试。
4. 100% 原文忠实度与 TOC 面包屑匹配测试。
