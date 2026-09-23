# 静态知识 RAG 架构

## 组件职责

- **LlamaIndex** 管理静态方法知识卡的 `TextNode`、本地持久化向量索引、dense 检索、BM25 检索、模块 metadata filter 和 Reciprocal Rank Fusion（RRF）。Embedding 通过 `BaseEmbedding` 适配器复用项目当前配置的模型。
- **SQLite** 保存知识卡原文、稳定知识卡 ID、来源及其他 metadata，也继续保存历史作答、批改报告、训练记录及其 FTS5 / sqlite-vec 索引。用户历史检索链不迁移到 LlamaIndex。
- **LangGraph** 负责 Agent 状态、工具路由和执行流程。静态知识与其他检索结果统一转成现有 Evidence 结构，因此证据目录、`read_source` 和上层 Agent 接口保持原样。

## 持久化与增量更新

LlamaIndex 使用轻量的本地 `SimpleVectorStore`。持久化文件位于用户数据目录的 `llamaindex/knowledge/` 下，并按索引 schema、embedding 模型和维度分目录。进程启动后优先从该目录加载，随后按稳定知识卡 ID 对照 SQLite 中的知识卡。

新增卡片只插入对应 Node；删除卡片只删除对应 Node；文本或参与 embedding 的语义 metadata 修改时只重算该卡片的 embedding。仅来源信息等不参与 embedding 的 metadata 修改会更新 Node 和持久化文件，并复用原向量。BM25 词项索引从持久化 Node 文本按需重建，不触发 embedding 计算。

## 检索流程

1. 检索路由根据 `GONGKAO_KNOWLEDGE_RETRIEVER` 选择静态知识检索实现；默认值 `hybrid` 保持现有路径。
2. 选择 LlamaIndex 时，当前模块作为 metadata filter，同时过滤 dense Retriever 和 BM25 候选集合。BM25 使用项目已有中文分词器产生的词项。
3. LlamaIndex 的 `QueryFusionRetriever` 用 RRF 合并 dense 与 BM25 排名，输出静态知识候选；动态历史等其余来源仍由现有 SQLite / FTS5 / sqlite-vec 链路处理。
4. 候选通过稳定知识卡 ID 映射回 SQLite 中的原始行，并转换成项目 Evidence 字段。知识卡 Evidence ID 保持稳定，`read_source` 仍按该 ID 读取原知识卡。
5. LangGraph 接收统一 Evidence，继续执行原有证据筛选、Agent 推理和工具流程。
