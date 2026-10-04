"""上传前纯净度自检（`scripts/prepublish_check.py`）的测试。

★ 为什么要给一个"检查脚本"写测试：它是发布流程的**闸门**。
  闸门有两个失败方向，而且都会造成真实损害：
    · 漏报 → 密钥/个人数据被推到公开仓库（不可逆）；
    · 误报 → 检查器永远红着，人就开始无视它（等于没有闸门）。
  所以下面每条都同时钉住"该报的必须报"和"不该报的别乱报"。
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_checker() -> ModuleType:
    """按路径加载 scripts/prepublish_check.py（scripts 不是包）。"""
    spec = importlib.util.spec_from_file_location(
        "prepublish_check", ROOT / "scripts" / "prepublish_check.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _scan(tmp_path: Path, files: dict[str, str], names: list[str] | None = None) -> dict:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    args = argparse.Namespace(root=str(tmp_path), name=names or [])
    return checker.scan(args)


def test_real_looking_key_outside_tests_is_a_blocker(tmp_path: Path) -> None:
    """源码里出现高熵密钥 → BLOCKER（这条是闸门的核心价值）。"""
    result = _scan(
        tmp_path,
        {"app/core/config.py": 'API_KEY = "sk-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"\n'},
    )
    assert result["blockers"], "真实形状的密钥必须拦住"
    assert any("config.py" in b for b in result["blockers"])


def test_fake_key_in_tests_is_not_a_blocker(tmp_path: Path) -> None:
    """测试里的**故意假密钥**只提示、不拦（否则闸门永远红着）。

    分两档，都要钉住：
      · 明显占位（值里带 invalid/test/fake…）→ 进 notes，不需要人看；
      · 高熵、看不出是假的（有人真把密钥粘进测试了）→ 进 WARN，必须让人看见。
    """
    result = _scan(
        tmp_path,
        {"tests/test_llm.py": 'api_key="sk-definitely-invalid-key-for-testing"\n'},
    )
    assert not result["blockers"], "测试夹具不该拦住发布"
    assert any("test_llm.py" in n for n in result["notes"]), "占位值也要如实列出来"

    result2 = _scan(
        tmp_path / "second",
        {"tests/test_other.py": 'api_key="sk-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"\n'},
    )
    assert not result2["blockers"], "测试范围内的可疑值不直接拦"
    assert any("test_other.py" in w for w in result2["warns"]), "但必须提醒人工确认"


def test_personal_path_is_warned(tmp_path: Path) -> None:
    """本机路径属于个人标识 → WARN（要人工确认，不直接拦）。"""
    result = _scan(
        tmp_path,
        {"README.md": "示例：C:\\Users\\someone\\Downloads\\preset.json\n"},
    )
    assert not result["blockers"]
    assert any("README.md" in w for w in result["warns"])


def test_requested_name_is_reported(tmp_path: Path) -> None:
    """`--name` 指定的真实用户名要被找出来（发布前最后一次自查用）。

    ★ 这条断言在第十八轮**从 WARN 改成了 BLOCKER**，理由来自一次真实事故：
      `docs/next-electron-and-github.md` 里写着贡献者的真账号名，而它当时只被报成 WARN，
      于是"闸门通过"了 —— 但 `--name` 是用户亲口声明"这是我的真名"的输入，
      它出现就说明有使用痕迹，必须拦住，不能只是"提示一下"。
    """
    result = _scan(tmp_path, {"docs/x.md": "账号 张三 的数据\n"}, names=["张三"])
    assert any("docs/x.md" in b for b in result["blockers"]), result


def test_requested_name_blocker_does_not_echo_the_name(tmp_path: Path) -> None:
    """BLOCKER 的**措辞本身**不能带上那个真名。

    ★ 为什么较真：这份报告会被贴进 issue / 聊天记录里求助。
      如果报告里复述了真名，那就在"为了排掉使用痕迹"的过程中又把它写了一遍。
    """
    result = _scan(tmp_path, {"docs/x.md": "账号 张三 的数据\n"}, names=["张三"])
    for item in result["blockers"]:
        assert "张三" not in item, item


def test_multiple_requested_names_all_count(tmp_path: Path) -> None:
    """`--name` 可以给多次（真名 + 常用账号名），每一个都要拦。"""
    result = _scan(
        tmp_path,
        {"a.md": "作者 alice 与 bob\n"},
        names=["alice", "bob"],
    )
    assert any("a.md" in b for b in result["blockers"])
    # 两个名字都在同一个文件里，报两条（每条一处证据）
    matching = [b for b in result["blockers"] if "a.md" in b]
    assert len(matching) == 2, matching


def test_name_check_covers_every_text_file(tmp_path: Path) -> None:
    """真名可能藏在任何文本文件里（md / py / json / ps1 …），不能只扫文档。"""
    result = _scan(
        tmp_path,
        {
            "desktop/scripts/make-ico.ps1": "# 由 张三 整理\n",
            "desktop/package.json": '{"author": "张三"}\n',
        },
        names=["张三"],
    )
    files = {b.split("：", 1)[-1].split(":")[0] for b in result["blockers"] if "真名" in b}
    assert "desktop/scripts/make-ico.ps1" in files, result["blockers"]
    assert "desktop/package.json" in files, result["blockers"]


def test_runtime_data_directory_is_a_blocker(tmp_path: Path) -> None:
    """`data/` 下除白名单外都是运行期数据 → BLOCKER。"""
    result = _scan(tmp_path, {"data/logs/app.log": "真实提示词与会话内容\n"})
    assert any("data/logs" in b for b in result["blockers"])


def test_clean_tree_passes(tmp_path: Path) -> None:
    """干净目录必须通过（正向对照，防止检查器变成"永远红"）。"""
    result = _scan(
        tmp_path,
        {
            "README.md": "# 云梦枢\n",
            "app/main.py": "print('hello')\n",
            "data/test-cards/card.json": '{"name": "测试卡"}\n',
        },
    )
    assert not result["blockers"], result["blockers"]
