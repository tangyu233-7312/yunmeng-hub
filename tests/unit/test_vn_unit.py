"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest


from app.narrative import vn as vn_mod

PNG = "data:image/png;base64,iVBORw0KGgo="

CALM = "https://cdn.example.com/calm.png"

ANGRY = "https://cdn.example.com/angry.png"

BG = "https://cdn.example.com/classroom.png"


def test_image_urls_are_restricted_to_safe_schemes() -> None:
    """★ 这份配置会进 `<img src>`：unsafe 一律拒绝，并说清为什么。"""
    config, notes = vn_mod.normalize(
        {
            "sprites": {
                "平静": CALM,
                "生气": "javascript:alert(1)",
                "坏": "file:///C:/secret.png",
                "svg": "data:image/svg+xml;base64,PHN2Zz4=",
                "内嵌": PNG,
            },
            "background": "data:image/svg+xml;base64,PHN2Zz4=",
        }
    )
    assert set(config["sprites"]) == {"平静", "内嵌"}, config["sprites"]
    assert config["background"] == ""
    joined = " ".join(notes)
    assert (
        "只支持 http(s)" in joined
        and "不支持 SVG" in joined
        and "背景图没生效" in joined
    )


def test_url_length_is_capped() -> None:
    config, notes = vn_mod.normalize(
        {"sprites": {"巨图": "data:image/png;base64," + "A" * 400_000}}
    )
    assert config["sprites"] == {}
    assert "地址太长" in " ".join(notes)


def test_sprite_count_is_capped() -> None:
    many = {f"表情{i}": CALM for i in range(vn_mod.MAX_SPRITES + 5)}
    config, notes = vn_mod.normalize({"sprites": many})
    assert len(config["sprites"]) == vn_mod.MAX_SPRITES
    assert f"最多 {vn_mod.MAX_SPRITES} 张" in " ".join(notes)


def test_sprites_accept_both_written_forms() -> None:
    """对象与数组两种写法都收（网上下载的卡两种都见过）。"""
    as_object, _ = vn_mod.normalize({"sprites": {"平静": CALM}})
    as_list, _ = vn_mod.normalize({"sprites": [{"name": "平静", "url": CALM}]})
    as_pairs, _ = vn_mod.normalize({"sprites": [["平静", CALM]]})
    assert (
        as_object["sprites"]
        == as_list["sprites"]
        == as_pairs["sprites"]
        == {"平静": CALM}
    )


def test_expression_field_name_is_validated() -> None:
    config, notes = vn_mod.normalize(
        {"sprites": {"平静": CALM}, "expression_field": "表情 名"}
    )
    assert config["expression_field"] == vn_mod.DEFAULT_FIELD
    assert "不合法" in " ".join(notes)


def test_stage_explains_when_the_expression_has_no_sprite() -> None:
    card = type(
        "C",
        (),
        {
            "name": "薇拉",
            "extra_data": {
                "extensions": {
                    "hne": {
                        "vn": {
                            "sprites": {"平静": CALM, "生气": ANGRY},
                            "background": BG,
                            "default": "平静",
                        }
                    }
                }
            },
        },
    )()
    stage = vn_mod.stage(card, {"mood": "害羞"})
    assert stage is not None
    assert stage["sprite_url"] == CALM, "退回默认立绘"
    assert stage["expression"] == "害羞"
    assert any("没有对应立绘" in note for note in stage["warnings"])
    assert stage["background"] == BG and stage["name"] == "薇拉"
    assert stage["expressions"] == ["平静", "生气"]


def test_no_stage_when_the_card_does_not_declare_vn() -> None:
    plain = type("C", (), {"name": "路人", "extra_data": {}})()
    assert vn_mod.stage(plain, {"mood": "生气"}) is None


def test_expression_field_is_added_to_the_schema_with_the_choices() -> None:
    """★ 没这个字段立绘永远不会变 —— 自动补一个，并把可选值告诉模型。"""
    config, _ = vn_mod.normalize({"sprites": {"平静": CALM, "生气": ANGRY}})
    schema = {
        "spec": "hne_state_v1",
        "fields": [{"name": "hp", "type": "meter", "max": 10}],
    }
    notes = vn_mod.ensure_expression_field(schema, config)
    assert [f["name"] for f in schema["fields"]] == ["hp", "mood"]
    mood = schema["fields"][-1]
    assert mood["type"] == "text"
    assert "平静 / 生气" in mood["description"], (
        "可选值必须写进字段描述，模型才知道填什么"
    )
    assert notes and "已往状态栏补一个" in notes[0]
    assert vn_mod.ensure_expression_field(schema, config) == [], "重复调用不再加第二个"


def test_author_defined_expression_field_is_respected() -> None:
    """作者自己定义过就一个字都不动（标签/描述按他的来）。"""
    config, _ = vn_mod.normalize(
        {"sprites": {"平静": CALM}, "expression_field": "心情"}
    )
    schema = {"fields": [{"name": "心情", "label": "她的心情", "type": "text"}]}
    assert vn_mod.ensure_expression_field(schema, config) == []
    assert schema["fields"] == [{"name": "心情", "label": "她的心情", "type": "text"}]


def test_merge_keeps_the_other_namespaces() -> None:
    config, _ = vn_mod.normalize({"sprites": {"平静": CALM}})
    merged = vn_mod.merge_into_extensions(
        {"hne": {"state_schema": [{"name": "hp"}]}, "other": {"x": 1}}, config
    )
    assert merged["other"] == {"x": 1}
    assert merged["hne"]["state_schema"] == [{"name": "hp"}]
    assert merged["hne"]["vn"]["spec"] == vn_mod.SPEC
    assert merged["hne"]["vn"]["sprites"] == {"平静": CALM}


pytestmark = pytest.mark.unit
