"""嵌入（Embedding）抽象层。

==================== 为什么需要「可插拔嵌入」？====================
向量检索的效果完全取决于嵌入模型 —— 它负责把一段文本映射成高维向量，
让「语义相近」的文本在向量空间中彼此靠近。

本项目提供两种后端，可通过 .env 的 EMBEDDING_BACKEND 切换：

  1. onnx_default —— 本地 ONNX MiniLM-L6-v2
     · 优点：完全本地推理，零成本、断网可用；首次下载约 80MB 后永久离线
     · 缺点：MiniLM 主要面向英文语料，中文语义效果一般

  2. api —— 调用用户自配 API 的 /embeddings 接口
     · 优点：可选用中文效果更好的国产嵌入模型（bge-m3、text-embedding-v3 等）
     · 缺点：产生 API 费用、依赖网络
     · ★ 这正是本项目「异构」设计从对话模型延伸到嵌入模型的体现：不绑定任何厂商

==================== ★ 一个必须重视的陷阱 ====================
**不同嵌入模型产生的向量绝对不能混用！**

把 384 维的 MiniLM 向量和 1024 维的 bge-m3 向量放进同一个集合去比较，
算出来的"距离"毫无意义，检索结果会完全错乱 —— 而且不会报任何错。
这是典型的「静默错误」，比崩溃更难排查。

因此本项目给每个向量集合记录一个「嵌入指纹」（后端名 + 模型名 + 维度），
每次打开集合时都会校验；一旦发现与当前配置不一致，立刻抛出明确错误，
而不是让错误的数据继续污染检索结果。
"""

from app.embeddings.base import BaseEmbedding, EmbeddingFingerprint
from app.embeddings.factory import create_embedding

__all__ = ["BaseEmbedding", "EmbeddingFingerprint", "create_embedding"]
