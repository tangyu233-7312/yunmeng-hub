"""跨数据库后端的 SQL 表达式（本项目同时支持 SQLite 与 MySQL）。

==================== 为什么需要这个模块 ====================
本项目的定位是**单机桌面应用**：默认用 SQLite（装完即用、不用装数据库服务），
同时保留 MySQL 作为可选后端（多人共用/已有 MySQL 的用户）。

只要支持两个后端，就一定会遇到"某个函数只有其中一边有"的情况。
本项目全部代码里只有**两处**这样的地方（都在下面），所以刻意不引入
任何"数据库抽象层"，而是把这两处集中在同一个文件里，并写清楚
"两边分别编译成什么、为什么必须不同、语义靠哪个测试守着"。

★ 判断标准（以后新增查询时照这个来）：
    · 只用 SQLAlchemy 的通用表达式（`==` / `.in_()` / `.like()` / `func.count()` …）
      → 不需要动这个文件；
    · 用到某个数据库**特有的函数或语义**
      → 在这里加一个构造，并在两个方言下各写一个 `@compiles`；
        绝不在业务代码里写 `if settings.is_sqlite:` 去分叉 SQL ——
        那样两套 SQL 会各自演化，最后没人知道哪套是对的。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import String, bindparam, literal
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ColumnElement

#: LIKE 的转义字符。`%` 与 `_` 在 LIKE 里是通配符，用户搜索 "50%" 时必须转义，
#: 否则 `LIKE '%50%%'` 会匹配到所有含 "50" 的记录（本项目真实踩过的坑）。
#:
#: ★ 为什么**不用反斜杠**（这是本轮返工一次才定下来的）：
#:   反斜杠看着最"自然"（MySQL 的默认转义符就是它），但它会在 MySQL 上编译出
#:   `... LIKE ? ESCAPE '\\'`，而 MySQL 的语法分析器直接报
#:   `1064 ... near '%3520ce82%' ESCAPE '\\'`（实测：6 个用例因此挂在 MySQL 后端上）。
#:   根因是反斜杠在 SQL 字符串字面量与 MySQL 的 C 风格转义里**双重特殊**，
#:   要写对得先想清楚"服务器看到的到底是几个反斜杠"，非常容易出错。
#:   ★ 用一个**两边都不特殊**的普通字符（`!`）就完全没有这层歧义：
#:     它不影响 MySQL 的默认转义行为，也让 SQL 文本一眼能读。
#:     `!` 是 SQL 里常用的自定义转义符（SQL Server 文档里的例子就是它）。
LIKE_ESCAPE = "!"


def escape_like(term: str) -> str:
    """转义 LIKE 通配符（用 `LIKE_ESCAPE` 作为转义符）。

    ★ 转义符本身必须**最先**处理，否则它会被后续步骤二次转义（`!` → `!!` → `!!!!`），
      匹配结果就悄悄错了。
    """
    return (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )


class LikeEscaped(ColumnElement[bool]):
    r"""带**显式** ESCAPE 子句的 LIKE：`列 LIKE :模式 ESCAPE '!'`。

    ==================== ★ 为什么非要有这个类 ====================
    SQLAlchemy 的 `column.like(pattern)` 编译出来是 `列 LIKE :模式`，**没有 ESCAPE**。
    两个后端对此的默认行为不同：

        · MySQL   ：反斜杠**本来就是** LIKE 的默认转义符（除非开了 `NO_BACKSLASH_ESCAPES`），
                    所以 `\%` 能被当作字面量。
        · SQLite  ：**没有默认转义符**！`LIKE '%50\%%'` 里的 `\` 只是一个普通字符，
                    于是"搜 50% 应该命中 1 条"变成"命中 0 条"——
                    实测就是这么挂的（`test_list_search_escapes_like_wildcards`
                    与 `test_list_shows_world_book_name` 两个用例）。

    显式写出 `ESCAPE '!'` 之后两边的语义**完全对齐**，而且不再依赖
    MySQL 的 `sql_mode` 默认值 —— 这比"靠默认行为碰巧一致"稳固得多。

    ★ 为什么两个方言都发同一条 SQL、而不是只给 SQLite 加：
      目的是让"两个后端生成的 SQL 完全一致"。任何差异都应该来自
      "某个函数只有一边有"（如 JSON_CONTAINS），而不是来自我们的措辞。
    """

    inherit_cache = True

    def __init__(self, column: Any, pattern: str) -> None:
        super().__init__()
        self.column = column
        #: 已经过 `escape_like` 处理的模式串（含 `%` 包裹）
        self.pattern = pattern


@compiles(LikeEscaped)
def _compile_like_escaped(element: LikeEscaped, compiler, **kw) -> str:  # noqa: ANN001
    column = compiler.process(element.column, **kw)
    # ★ 显式给出 String 类型：不指定的话，MySQL 驱动（PyMySQL）在参数化时
    #   拿不到类型信息，会抛 `ProgrammingError`（实测：6 个用例就是因此挂的）。
    #   SQLite 那边宽松，不报错 —— 所以这个坑只在 MySQL 后端上暴露。
    pattern = compiler.process(bindparam(None, element.pattern, type_=String), **kw)
    return f"{column} LIKE {pattern} ESCAPE '{LIKE_ESCAPE}'"


def like_contains(column, term: str) -> LikeEscaped:  # noqa: ANN001
    """「列里包含 term」的条件（term 里的通配符按字面量处理）。

    用法：`like_contains(CharacterCard.name, user_input)`
    """
    return LikeEscaped(column, f"%{escape_like(term)}%")


class JsonArrayContains(ColumnElement[bool]):
    r"""「JSON 数组里包含某个字符串元素」的**跨后端**写法。

    ==================== ★ 为什么不能直接用 func.json_contains ====================
    标签存在 JSON 列里，普通等值比较没用，得判"数组里有没有这一项"。
    最直接的写法是 MySQL 的 `JSON_CONTAINS(列, '"标签"')` —— 这也是本函数
    最初唯一要做的事。但 SQLite **没有这个函数**，切到 SQLite 后标签筛选直接报
    `no such function: json_contains`（实测：一次全量测试里挂了 3 个用例）。

    ==================== 两个后端为什么写法不同 ====================
    * MySQL 8：原生 `JSON_CONTAINS(列, 候选值)`，语义最准。
    * SQLite：JSON1 扩展**没有** JSON_CONTAINS。最贴近的等价物是表值函数
      `json_each(列)`，但它会把行**展开**（一行变多行），用在 WHERE 里还要配
      DISTINCT 去重 —— 而本项目的筛选条件同时喂给「列表查询」和「计数查询」
      （见 `character_card_service._build_filters` 的注释），展开行会让计数直接算错。

    ★ 所以 SQLite 侧退化为"文本匹配"：
      本项目写入 JSON 列时用的是 `json.dumps` 的**默认**行为（`ensure_ascii=True`），
      中文因此被存成 `\uXXXX` 转义形式（这串反斜杠在源码里必须写成 raw string，
      否则 Python 会把文档字符串里的 `\u` 当成 Unicode 转义而报错 —— 本轮真踩过）：

          标签 ["独特标签abc", "其它"]  →  列里的文本
          ["\u72ec\u7279\u6807\u7b7eabc", "\u5176\u5b83"]

      这是**实测发现**的：第一版直接用原样中文去匹配，两边永远匹配不上，
      表现为"筛选恒为 0 条"，而且不报任何错。

      于是"某个标签恰好是数组里的一项"可以精确表达为
      "文本里出现了带引号的 `\uXXXX` 片段"—— 两端的引号正是排除
      "奇幻" 命中 "奇幻世界" 这类子串误判的关键，
      由 `test_list_filter_by_tag_is_exact_not_substring` 守着。

      ★ 为什么用 `instr` 而不是 `LIKE`：LIKE 里 `%` 和 `_` 是通配符，
        而标签值是**绑定参数**，用户传 `tag=%` 会匹配到所有卡（"筛选失灵"且不报错）。
        `instr` 是纯子串查找，没有元字符问题 —— 少一处"要记得转义"就少一个坑。

      代价（写清楚，不藏）：若标签**值本身**含双引号，匹配会变宽。
      但标签是短分类词，且两个后端跑的是同一批断言，行为差异会被测试抓到。
    """

    inherit_cache = True

    def __init__(self, column: Any, probe: str) -> None:
        super().__init__()
        self.column = column
        #: 带引号的 JSON 字符串字面量（中文已转义成 \uXXXX），就是要找的片段
        self.probe = probe


@compiles(JsonArrayContains, "mysql")
def _compile_json_array_mysql(element: JsonArrayContains, compiler, **kw) -> str:  # noqa: ANN001
    """MySQL：原生 JSON_CONTAINS。"""
    column = compiler.process(element.column, **kw)
    probe = compiler.process(literal(element.probe), **kw)
    return f"JSON_CONTAINS({column}, {probe})"


@compiles(JsonArrayContains, "sqlite")
def _compile_json_array_sqlite(element: JsonArrayContains, compiler, **kw) -> str:  # noqa: ANN001
    """SQLite：等价于 `instr(列, '"\\uXXXX…"') > 0`。"""
    column = compiler.process(element.column, **kw)
    probe = compiler.process(literal(element.probe), **kw)
    return f"instr({column}, {probe}) > 0"


def json_array_contains(column, value: str) -> JsonArrayContains:  # noqa: ANN001
    """「JSON 数组列里恰好包含字符串 value」的条件。

    ★ 探针必须是 **`ensure_ascii=True` 的 JSON 字面量**，也就是
      `json.dumps("奇幻")` → `'"\\u5947\\u5e7b"'`（带引号、非 ASCII 转义）。
      原因见 `JsonArrayContains` 的文档：列里的文本就是这种形态，
      用原样中文去匹配会**永远匹配不上**（第一版就是这么错的）。
      MySQL 侧则因为 JSON_CONTAINS 会先解析候选值，带引号的字面量正好是它要的形式。
    """
    return JsonArrayContains(column, json.dumps(value))
