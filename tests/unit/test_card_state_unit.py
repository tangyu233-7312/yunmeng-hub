"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest


from app.narrative import card_state as cs


@pytest.mark.parametrize(
    ("text", "source"),
    [
        ('他笑了。\n<state>{"hp": 42}</state>', cs.SRC_STATE_TAG),
        ('他笑了。\n```json\n{"hp": 42}\n```', cs.SRC_FENCE),
        ('他笑了。\n```\n{"hp": 42}\n```', cs.SRC_FENCE),
        ('他笑了。\n<UpdateVariable>\n{"hp": 42}\n</UpdateVariable>', cs.SRC_MVU),
        ('他笑了。<script type="application/json">{"hp": 42}</script>', cs.SRC_SCRIPT),
        ('他笑了。\n<status>{"hp": 42}</status>', cs.SRC_INLINE_TAG),
    ],
)
def test_every_known_card_state_format_is_recognized(text: str, source: str) -> None:
    """用户要求"尽量都认"：五种真实存在的卡状态写法都要认，并**如实记下是哪种**。

    ★ 记来源（source）不是装饰：用户看到状态被剥掉时，得能查到"系统按哪条规则处理的"——
      否则一旦误判，谁都没法定位是哪条正则太宽。
    """
    cleaned, hit = cs.extract(text)
    assert hit is not None, f"{source} 没被认出来：{text!r}"
    assert hit["source"] == source
    assert hit["value"] == {"hp": 42}
    assert "<state>" not in cleaned and '"hp"' not in cleaned
    assert "他笑了" in cleaned, "正文必须留下"


def test_state_block_must_be_at_the_end() -> None:
    """★ 保守的第一条：**正文中段的 JSON 不是状态块**（误判的代价是正文断片）。

    作者的状态块几乎总在回复末尾；而正文里随手贴的配置片段、台词里的 JSON
    如果被当状态块藏起来，用户看到的就是"说到一半没了"——比漏认严重得多。
    """
    cleaned, hit = cs.extract('前面 {"hp": 1}\n后面还有一大段正经剧情，不能被吞掉。')
    assert hit is None
    assert cleaned == '前面 {"hp": 1}\n后面还有一大段正经剧情，不能被吞掉。'


def test_non_json_and_non_state_langs_are_left_alone() -> None:
    """保守的第二条：不是 JSON 的、或明显是别的语言的围栏，一律不碰。"""
    for text in (
        "正文\n```python\n{'hp': 42}\n```",  # python 围栏：里面是 dict 字面量，不是 JSON
        "正文\n```yaml\nhp: 42\n```",
        "<status>HP 42 / 100</status>",  # 纯文本标签是**给人看的正文**
        "正文\n```html\n<div>{}</div>\n```",
    ):
        cleaned, hit = cs.extract(text)
        assert hit is None, f"不该被认成卡状态：{text!r}"
        assert cleaned == text


def test_placeholder_tag_is_a_hint_not_a_block() -> None:
    """`<StatusPlaceHolderImpl/>` 是"这张卡自带状态栏"的信号，**不是**数据。

    ★ 所以只做提示，绝不改正文：它是作者留的锚点，剥掉等于改作者的卡。
    """
    assert cs.placeholder_hint("正文\n<StatusPlaceHolderImpl/>") is True
    cleaned, hit = cs.extract("正文\n<StatusPlaceHolderImpl/>")
    assert hit is None
    assert cleaned == "正文\n<StatusPlaceHolderImpl/>"


def test_state_tag_belongs_to_the_card_only_when_card_declares_no_schema() -> None:
    """★ `<state>` 归谁，取决于"这张卡有没有声明 state_schema" —— 这是最易错的一处。

    声明了 → 那是给**本系统**的（每轮都输出），若被当卡状态剥走存进第二存储，
             系统的状态永远是空的，状态栏会一直说"模型还没输出过"，正文里那个块又没了。
    没声明 → 那是**作者自己的**，系统以前只是把它剥掉丢弃；现在改成收进第二存储。
    """
    text = '正文\n<state>{"hp": 1}</state>'
    # 卡声明了 schema：不动正文（交给 state.py），也不当卡状态
    assert cs.extract_for_session(text, has_system_schema=True)[1] is None
    # 卡没声明：认，并剥掉
    cleaned, hit = cs.extract_for_session(text, has_system_schema=False)
    assert hit is not None and hit["source"] == cs.SRC_STATE_TAG
    assert "<state>" not in cleaned


def test_drift_reports_only_missing_and_retyped() -> None:
    """★ 漂移检查只做能确证的两件事：字段消失 / 类型变了。

    **不报数值增减**：卡状态没有 schema，就没有"该往哪个方向算偏移"的定义，
    硬报必然误报（模型写 42 下一轮写 43 到底算不算异常？没人能定义）。
    """
    diff = cs.diff_fields(
        {"hp": 42, "mp": 10, "bag": ["钥匙"]},
        {"hp": "42", "ex": 3},  # hp 类型变了；mp/bag 没了；多了一个 ex
    )
    assert diff["missing"] == ["mp", "bag"]
    assert diff["retyped"] == [{"key": "hp", "was": "number", "now": "text"}]
    assert diff["new"] == ["ex"]
    notes = cs.drift_notes(diff)
    assert any("少了上一轮有的字段" in n for n in notes)
    assert any("类型变了" in n for n in notes)
    # 数值变了（42 -> 43）不算漂移
    assert cs.diff_fields({"hp": 42}, {"hp": 43}) == {
        "missing": [],
        "retyped": [],
        "new": [],
    }
    # null 不算类型变了（"这轮还没填"是正常状态）
    assert cs.diff_fields({"hp": 42}, {"hp": None})["retyped"] == []


pytestmark = pytest.mark.unit
