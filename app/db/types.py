"""跨数据库的列类型（MySQL 与 SQLite 都认）。

==================== 为什么需要这个文件 ====================
本项目原来只支持 MySQL，于是模型里直接用了 **MySQL 方言类型**：
`sqlalchemy.dialects.mysql.MEDIUMTEXT` 与 `...mysql.JSON`。

这在只跑 MySQL 时没问题，但一旦想让用户"不装数据库也能用"（SQLite 单文件），
建表就会当场炸：

    CompileError: (in table 'plugins', column 'raw_manifest'):
    Compiler <SQLiteTypeCompiler> can't render element of type MEDIUMTEXT

也就是说 —— **方言类型把数据层钉死在某一个数据库上**。这个文件把那个钉子拔掉：
类型在这里定义一次，模型只 import 这里的名字，两边都能建表。

==================== 各类型的取舍（都是实测过的）====================

`LongText`
    MySQL 侧渲染成 **MEDIUMTEXT**（16 MB）：本项目单条消息、角色卡开场白、
    剧情总结都可能上万字，MySQL 的 TEXT 只有 64 KB，偏紧 —— 原来选 MEDIUMTEXT
    是有理由的，这里**保持 MySQL 侧行为不变**。
    SQLite 侧退化成 TEXT：SQLite 的类型系统是"亲和性"（affinity）而非强制长度，
    TEXT 没有长度上限，所以那边**不需要**也不存在 MEDIUMTEXT 这个概念。

JSON 列为什么**不**在这里定义类型
    （这一段是本轮实验的结论，下次别再用方言类型去碰它）
    我一开始想给"只存不查的 JSON"做一个"MySQL 用原生 JSON、SQLite 用 TEXT"的变体类型
    （叫 `JsonText`）。实测发现两件事，于是放弃：
      ① 它必须能和 `MutableDict.as_mutable()` 配合（插件 config、预设 config、
         世界书 entries、角色卡 extra_data 都是"原地改也要被追踪"的列），
         而 `MutableDict` 的 coerce 只认通用类型；塞变体进去会报
         `Attribute 'config' does not accept objects of type 'dict'`；
      ② SQLite 侧若只给它 TEXT，SQLAlchemy **不会**把 dict 序列化成 JSON，
         插入时直接 `type 'dict' is not supported`。
    所以 JSON 语义的列一律用 **`sqlalchemy.JSON`（通用类型）**：
    MySQL 上渲染成 `JSON`，SQLite 上也是 `JSON`（内部按 TEXT 存，
    但 dict ↔ JSON 字符串的转换由 SQLAlchemy 负责），两边行为一致。
    本项目**没有任何按 JSON 路径查询的代码**（搜索 `JSON_EXTRACT` / `json_extract`
    均为 0 处），所有 JSON 都是"整块读出来在 Python 里解析"，所以这个选择没有代价。

`PkInt`
    自增主键。MySQL 侧 BIGINT；SQLite 侧必须是 INTEGER ——
    SQLite 里只有 `INTEGER PRIMARY KEY` 才是 rowid 的别名、才能自增，
    BIGINT 主键插进去会直接撞 `NOT NULL constraint failed: users.id`（实测确认过）。
    用 `with_variant` 明确告诉 SQLAlchemy 该用哪个，不依赖它的隐式改写。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Integer, Text
from sqlalchemy.dialects import mysql

__all__ = ["LongText", "PkInt"]

#: 长文本：MySQL 用 MEDIUMTEXT（16 MB），其他数据库用普通 TEXT（SQLite 无长度限制）
LongText = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")

#: 自增主键：MySQL 用 BIGINT，SQLite 必须用 INTEGER（只有它才是 rowid 别名）
PkInt = BigInteger().with_variant(Integer(), "sqlite")
