"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest


from app.core.exceptions import BadRequestError


from app.services import plugin_service


RAW_URL = "https://raw.githubusercontent.com/example/hne-plugins/main/hello.json"

BLOB_URL = "https://github.com/example/hne-plugins/blob/main/hello.json"


def test_css_sanitizer_blocks_escape_and_remote_import() -> None:
    dirty = (
        "</style><script>alert(1)</script>"
        "@import url(https://evil.example/x.css);"
        ".a{width:expression(alert(1));background:url(javascript:alert(2));behavior:url(#x)}"
        ".b{color:red}"
    )
    clean = plugin_service.sanitize_css(dirty)
    assert "</style>" not in clean, "必须挡掉 style 标签逃逸"
    assert "@import" not in clean
    assert "expression(" not in clean
    assert "javascript:" not in clean
    assert "behavior:" not in clean
    assert ".b{color:red}" in clean, "正常样式不能被误伤"


IMAGE_CONFIG = {
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


@pytest.mark.parametrize(
    "url,expected_part",
    [
        ("http://github.com/u/r/blob/main/a.json", "https"),
        ("https://gitlab.com/u/r/blob/main/a.json", "只允许从 GitHub"),
        (
            "https://raw.githubusercontent.com.evil.com/u/r/main/a.json",
            "只允许从 GitHub",
        ),
        ("https://github.com/u/r", "blob"),
        ("", "请填写"),
    ],
)
def test_install_url_whitelist(url: str, expected_part: str) -> None:
    with pytest.raises(Exception) as exc:
        plugin_service.normalize_github_url(url)
    assert expected_part in str(exc.value)


def test_blob_url_is_converted_to_raw() -> None:
    assert plugin_service.normalize_github_url(BLOB_URL) == RAW_URL
    # raw 地址原样通过（顺手去掉 query，GitHub 的 ?plain=1 之类不该带过去）
    assert plugin_service.normalize_github_url(RAW_URL + "?plain=1") == RAW_URL


GIST_RAW = "https://gist.githubusercontent.com/someone/abc123/raw"


def test_gist_urls_are_supported() -> None:
    """gist 也是 GitHub：最轻的分享方式，写一个插件不必建仓库。"""
    # 单文件 gist：页面地址自动补 /raw
    assert (
        plugin_service.normalize_github_url("https://gist.github.com/someone/abc123")
        == GIST_RAW
    )
    # 已经是 gist 的 raw 地址
    assert plugin_service.normalize_github_url(GIST_RAW) == GIST_RAW
    assert (
        plugin_service.normalize_github_url(
            "https://gist.github.com/someone/abc123/raw"
        )
        == GIST_RAW
    )
    # 多文件：?file= 明确指定（原样使用）
    assert (
        plugin_service.normalize_github_url(
            "https://gist.github.com/someone/abc123?file=demo.json"
        )
        == f"{GIST_RAW}/demo.json"
    )
    # 多文件：页面锚点 #file-demo-json 也能还原（GitHub 的锚点规则有损，只在能安全还原时猜）
    assert (
        plugin_service.normalize_github_url(
            "https://gist.github.com/someone/abc123#file-demo-json"
        )
        == f"{GIST_RAW}/demo.json"
    )
    # 还原不了的锚点不猜（gist 的 /raw 默认给第一个文件）
    assert (
        plugin_service.normalize_github_url(
            "https://gist.github.com/someone/abc123#file-某个中文名"
        )
        == GIST_RAW
    )


def test_gist_url_rejects_path_traversal_and_short_path() -> None:
    with pytest.raises(Exception) as exc:
        plugin_service.normalize_github_url(
            "https://gist.github.com/someone/abc123?file=../x.json"
        )
    assert "只能是文件名" in str(exc.value)
    with pytest.raises(Exception) as exc2:
        plugin_service.normalize_github_url("https://gist.github.com/someone")
    assert "gist 地址" in str(exc2.value)


def test_imagegen_manifest_is_rejected_from_github_install() -> None:
    """生图是内置插件：远程清单不能决定"故事正文发到哪个图像服务器"。"""
    with pytest.raises(BadRequestError) as exc:
        plugin_service.manifest_to_fields(
            {
                "spec": "hne_plugin_v1",
                "name": "远程生图",
                "kind": "imagegen",
                "config": {**IMAGE_CONFIG, "base_url": "https://evil.example/v1"},
            }
        )
    assert "内置插件" in str(exc.value)


pytestmark = pytest.mark.unit
