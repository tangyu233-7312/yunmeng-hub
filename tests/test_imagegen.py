"""生图插件的规则层测试（不联网、不碰数据库）。

覆盖：
    · 模型写的 `<hne-image>…</hne-image>` 必须被剥掉，且能取出描述（插件停用时也一样）
    · 自动 / 模型请求两种触发方式
    · 提示词模板与画风拼接
    · 上游响应的多种形态（b64_json / url / data URI / chat content）
    · 接口地址拼接、图片文件头识别

★ 真正的 HTTP 与落盘在 `app/services/imagegen_service.py`，这里只测纯函数；
  服务层的密钥加密/脱敏在 `tests/test_plugins.py` 里用真实请求验证。
"""

from __future__ import annotations

import base64

import pytest

from app.core.exceptions import BadRequestError
from app.db.models import Plugin
from app.narrative import imagegen as rules
from app.services import imagegen_service, plugin_service


# ==================================================================
#  标签协议
# ==================================================================
def test_image_tags_are_stripped_and_collected() -> None:
    text = "她推开门。\n<hne-image>A woman in a red cloak opening a wooden door, moonlit</hne-image>\n风灌了进来。"
    clean, prompts = rules.split_requests(text)
    assert "<hne-image>" not in clean and "</hne-image>" not in clean
    assert "她推开门。" in clean and "风灌了进来。" in clean
    assert prompts == ["A woman in a red cloak opening a wooden door, moonlit"]


def test_image_tag_supports_prompt_attribute_and_self_closing() -> None:
    text = "aaa<hne-image prompt=\"misty harbor at dawn\" />bbb<hne-image prompt='second scene'/>"
    clean, prompts = rules.split_requests(text)
    assert clean == "aaabbb"
    assert prompts == ["misty harbor at dawn", "second scene"]


def test_image_tags_are_stripped_even_when_malformed_and_idempotent() -> None:
    text = "正文<hne-image>unclosed prompt"
    clean, prompts = rules.split_requests(text)
    assert "<hne-image" not in clean
    assert prompts == []
    once = rules.split_requests("a<hne-image>x</hne-image>b")[0]
    assert rules.split_requests(once)[0] == once


def test_image_tags_are_capped_and_deduped() -> None:
    text = "".join(f"<hne-image>same</hne-image><hne-image>p{i}</hne-image>" for i in range(6))
    _clean, prompts = rules.split_requests(text)
    assert len(prompts) == rules.MAX_IMAGES
    assert prompts[0] == "same"
    assert len(set(prompts)) == len(prompts)


# ==================================================================
#  触发与提示词
# ==================================================================
def test_auto_mode_runs_every_n_rounds() -> None:
    spec = rules.parse_config({"trigger": "auto", "interval": 3})
    assert rules.should_generate(spec, requests=[], round_no=3) is True
    assert rules.should_generate(spec, requests=[], round_no=4) is False
    assert rules.should_generate(spec, requests=[], round_no=6) is True


def test_model_mode_only_runs_when_model_asked() -> None:
    spec = rules.parse_config({"trigger": "model", "interval": 10})
    assert rules.should_generate(spec, requests=[], round_no=1) is False
    assert rules.should_generate(spec, requests=["a scene"], round_no=1) is True


def test_contract_only_in_model_mode() -> None:
    assert rules.render_contract(rules.parse_config({"trigger": "auto"})) == ""
    contract = rules.render_contract(rules.parse_config({"trigger": "model", "max_images": 2}))
    assert "<hne-image>" in contract and "最多 2 个" in contract


def test_build_prompts_uses_template_and_style() -> None:
    spec = rules.parse_config(
        {
            "trigger": "auto",
            "style": "watercolor",
            "prompt_template": "角色 {char}；玩家 {user}；画面 {scene}；风格 {style}",
            "max_images": 1,
        }
    )
    prompts = rules.build_prompts(
        spec, requests=[], scene="雨夜的城门", char="阿岚", user="旅人"
    )
    assert prompts == ["角色 阿岚；玩家 旅人；画面 雨夜的城门；风格 watercolor"]


def test_build_prompts_model_mode_appends_style_without_template() -> None:
    spec = rules.parse_config({"trigger": "model", "style": "anime"})
    prompts = rules.build_prompts(
        spec, requests=["a castle on a cliff"], scene="", char="", user=""
    )
    assert "a castle on a cliff" in prompts[0]
    assert "Style: anime" in prompts[0]


def test_auto_scene_truncates_and_keeps_user_question() -> None:
    scene = rules.build_auto_scene("我要开门", "长" * 2000, 300)
    assert scene.startswith("玩家：我要开门")
    assert len(scene) <= 330


# ==================================================================
#  接口地址与响应
# ==================================================================
@pytest.mark.parametrize(
    "base,path,expected",
    [
        ("https://api.openai.com/v1", "images/generations", "https://api.openai.com/v1/images/generations"),
        ("https://api.openai.com/v1/", "images/generations", "https://api.openai.com/v1/images/generations"),
        ("https://x.test/v1/images/generations", "images/generations", "https://x.test/v1/images/generations"),
        ("", "images/generations", ""),
    ],
)
def test_join_endpoint(base: str, path: str, expected: str) -> None:
    assert rules.join_endpoint(base, path) == expected


def test_extract_b64_json_even_when_short() -> None:
    refs = rules.extract_images_response({"data": [{"b64_json": "aGVsbG8="}]}, "openai-images")
    assert refs == [{"kind": "b64", "data": "aGVsbG8="}]


def test_extract_url_and_data_uri() -> None:
    assert rules.extract_images_response(
        {"data": [{"url": "https://cdn.test/a.png"}]}, "openai-images"
    )[0]["url"].endswith("a.png")
    data_uri = "data:image/png;base64,iVBORw0KGgo="
    assert rules.extract_images_response(
        {"data": [{"url": data_uri}]}, "openai-images"
    )[0]["kind"] == "b64"


def test_extract_chat_image_from_content_and_images() -> None:
    one = rules.extract_chat_image(
        {"choices": [{"message": {"content": "![x](https://cdn.test/b.png)"}}]}
    )
    assert one and one["url"].endswith("b.png")
    two = rules.extract_chat_image(
        {"choices": [{"message": {"images": [{"image_url": {"url": "https://cdn.test/c.png"}}]}}]}
    )
    assert two and two["url"].endswith("c.png")


def test_detect_image_type_only_accepts_safe_bitmaps() -> None:
    assert rules.detect_image_type(b"\x89PNG\r\n\x1a\nxxx")[0] == "png"
    assert rules.detect_image_type(b"\xff\xd8\xffxxx")[0] == "jpg"
    assert rules.detect_image_type(b"GIF89a...")[0] == "gif"
    assert rules.detect_image_type(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x")[0] == "webp"
    assert rules.detect_image_type(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None


# ==================================================================
#  执行层（不发真实 HTTP）
# ==================================================================
def test_generate_one_decodes_b64_and_saves_file(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = rules.parse_config(
        {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-image-1",
            "protocol": "openai-images",
            "timeout_seconds": 30,
        }
    )
    png = b"\x89PNG\r\n\x1a\n" + b"test-image"
    payload = {"data": [{"b64_json": base64.b64encode(png).decode("ascii")}]}
    monkeypatch.setattr(imagegen_service, "_post_json", lambda *a, **k: payload)

    record = imagegen_service._generate_one(spec, "", "a misty harbor")
    assert record["url"].startswith("/media/imagegen/")
    name = record["url"].rsplit("/", 1)[-1]
    path = imagegen_service.media_dir() / name
    try:
        assert path.read_bytes() == png
    finally:
        path.unlink(missing_ok=True)


def test_generate_one_chat_protocol_uses_chat_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = rules.parse_config(
        {
            "base_url": "https://gateway.test/v1",
            "model": "image-via-chat",
            "protocol": "openai-chat",
            "timeout_seconds": 30,
        }
    )
    seen: dict = {}

    def fake_post(endpoint, body, api_key, timeout, protocol):
        seen.update(endpoint=endpoint, body=body, protocol=protocol)
        return {"choices": [{"message": {"content": "![x](https://cdn.test/a.png)"}}]}

    monkeypatch.setattr(imagegen_service, "_post_json", fake_post)
    monkeypatch.setattr(
        imagegen_service,
        "_materialize",
        lambda ref, timeout: b"\xff\xd8\xff" + b"jpeg",
    )
    record = imagegen_service._generate_one(spec, "", "a castle")
    assert seen["endpoint"].endswith("/chat/completions")
    assert seen["body"]["messages"][0]["content"] == "a castle"
    assert record["protocol"] == "openai-chat"
    name = record["url"].rsplit("/", 1)[-1]
    path = imagegen_service.media_dir() / name
    try:
        assert path.read_bytes().startswith(b"\xff\xd8\xff")
    finally:
        path.unlink(missing_ok=True)


def test_generate_one_rejects_response_without_image(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = rules.parse_config(
        {"base_url": "https://api.openai.com/v1", "model": "gpt-image-1"}
    )
    monkeypatch.setattr(imagegen_service, "_post_json", lambda *a, **k: {"data": []})
    with pytest.raises(imagegen_service.ImageGenError):
        imagegen_service._generate_one(spec, "", "nothing")


# ==================================================================
#  配置校验 / 密钥
# ==================================================================
def _valid_imagegen_config(**overrides):
    config = {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-image-1",
        "protocol": "openai-images",
        "trigger": "auto",
        "interval": 1,
        "size": "1024x1024",
        "style": "watercolor",
        "max_images": 1,
        "timeout_seconds": 120,
        "max_chars": 1200,
        "extra_body": {"watermark": False},
    }
    config.update(overrides)
    return config


def test_imagegen_config_is_normalized_and_key_is_encrypted() -> None:
    plain = "sk-test-1234567890"
    config = plugin_service.validate_config(
        "imagegen", _valid_imagegen_config(api_key=plain), allow_secrets=True
    )
    assert config["base_url"] == "https://api.openai.com/v1"
    assert config["api_key_enc"] and plain not in config["api_key_enc"]

    row = Plugin(
        id=1,
        user_id=1,
        name="生图",
        kind="imagegen",
        config=config,
        priority=100,
        enabled=True,
        is_builtin=False,
    )
    out = plugin_service.serialize(row)
    assert out["has_api_key"] is True
    assert "****" in out["api_key_masked"]
    assert "api_key_enc" not in out["config"]
    assert plain not in str(out)


def test_imagegen_update_without_key_keeps_old_ciphertext() -> None:
    config = plugin_service.validate_config(
        "imagegen", _valid_imagegen_config(api_key="sk-keep-me-please"), allow_secrets=True
    )
    updated = plugin_service.validate_config(
        "imagegen",
        _valid_imagegen_config(style="oil painting"),
        allow_secrets=True,
        existing=config,
    )
    assert updated["api_key_enc"] == config["api_key_enc"]
    assert updated["style"] == "oil painting"


def test_imagegen_manifest_cannot_smuggle_a_key() -> None:
    with pytest.raises(BadRequestError):
        plugin_service.validate_config(
            "imagegen", _valid_imagegen_config(api_key="sk-remote"), allow_secrets=False
        )


def test_imagegen_rejects_bad_config_shapes() -> None:
    with pytest.raises(BadRequestError):
        plugin_service.validate_config("imagegen", _valid_imagegen_config(base_url="ftp://x"))
    with pytest.raises(BadRequestError):
        plugin_service.validate_config("imagegen", _valid_imagegen_config(protocol="nope"))
    with pytest.raises(BadRequestError):
        plugin_service.validate_config("imagegen", _valid_imagegen_config(size="big"))
    with pytest.raises(BadRequestError):
        plugin_service.validate_config(
            "imagegen", _valid_imagegen_config(extra_body={"api_key": "x"})
        )


def test_imagegen_summary_mentions_key_state() -> None:
    config = plugin_service.validate_config("imagegen", _valid_imagegen_config())
    assert "Key 未配置" in plugin_service.summary_of("imagegen", config)
    with_key = plugin_service.validate_config(
        "imagegen", _valid_imagegen_config(api_key="sk-summary-test"), allow_secrets=True
    )
    assert "Key 已配置" in plugin_service.summary_of("imagegen", with_key)
