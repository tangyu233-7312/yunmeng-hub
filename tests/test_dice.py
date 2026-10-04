"""骰子插件：随机数由**后端**掷（模型只负责叙述），受控表达式求值，点数落库。

这一组的重点：
  1. 求值器是**受控**的：不执行代码、有界、坏输入只报错不抛异常
  2. 点数在**用户消息落库那一刻**掷定并写进 `messages.rolls_json` ——
     所以「查看提示词」预览与真实请求给出的是**同一个数字**（不重掷）
  3. 模型写的 `<roll>` 无论插件是否启用都会被剥掉（绝不让用户看到原始协议文本）
"""

from __future__ import annotations

import random
import uuid

import pytest
from fastapi.testclient import TestClient

from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import dice as dice_mod
from app.narrative import engine

PLUGINS = "/api/v1/plugins"
BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


# ==================================================================
#  一、纯函数：求值器（不碰数据库、不碰网络）
# ==================================================================
class _Seq:
    """按剧本出点数的假随机源 —— 爆炸骰/取高取低只有这样才能精确断言。"""

    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0)
        assert low <= value <= high, f"剧本给的 {value} 不在 {low}~{high} 内"
        return value


def test_notation_is_evaluated_the_trpg_way() -> None:
    rng = _Seq([4, 6])
    result = dice_mod.evaluate("2d6+3", rng=rng)
    assert result.ok and result.total == 13
    # 逐颗骰面必须留下来（论文里要能核对"这个 13 是怎么来的"）
    assert result.faces == [4, 6]
    assert result.terms[0]["subtotal"] == 10 and result.terms[-1]["value"] == 3


def test_keep_highest_and_lowest_drop_the_rest() -> None:
    high = dice_mod.evaluate("4d6kh3", rng=_Seq([1, 1, 5, 1]))
    assert high.total == 7, "4d6 取高 3：5+1+1"
    low = dice_mod.evaluate("4d6kl1", rng=_Seq([3, 5, 1, 5]))
    assert low.total == 1, "4d6 取低 1"


def test_exploding_dice_stop_at_the_budget() -> None:
    """★ 爆炸骰必须有上限：`1d2!` 理论上可以无限炸下去。"""
    result = dice_mod.evaluate("1d2!", rng=_Seq([2, 2, 1]))
    assert result.ok and result.total == 5
    assert result.terms[0]["exploded"] == 2
    # 剧本只给 3 个值：上限一旦失效，第 4 次 randint 会 IndexError
    capped = dice_mod.evaluate("1d2!", rng=_Seq([2] * (dice_mod.DEFAULT_LIMITS.max_explode + 1)))
    assert capped.ok
    assert capped.terms[0]["exploded"] == dice_mod.DEFAULT_LIMITS.max_explode


def test_success_check_is_kept_next_to_the_roll() -> None:
    won = dice_mod.evaluate("1d20+5<=15", rng=_Seq([10]))
    assert won.total == 15 and won.compare == "<=" and won.target == 15 and won.success is True
    lost = dice_mod.evaluate("1d20+5<=15", rng=_Seq([19]))
    assert lost.total == 24 and lost.success is False


def test_dice_are_reproducible_with_a_seed_but_not_without() -> None:
    first = dice_mod.evaluate("10d100", rng=random.Random(1234))
    second = dice_mod.evaluate("10d100", rng=random.Random(1234))
    assert first.faces == second.faces, "同一个种子必须给出同一组骰面（可复现）"
    others = {tuple(dice_mod.evaluate("10d100").faces) for _ in range(5)}
    assert len(others) > 1, "不给种子时不该每次一样（否则就不是随机了）"


def test_a_d6_actually_looks_fair() -> None:
    """分布体检：6000 次 1d6 的点数必须落在 1~6，且均值贴近 3.5。

    ★ 这不是"证明随机性"（伪随机数无法被证明），而是**排掉低级错误**：
      曾经写错过 `randint(1, sides+1)` / 漏掉爆炸深度上限，这条都能抓到。
    """
    rng = random.Random(20260926)
    faces = [dice_mod.evaluate("1d6", rng=rng).total for _ in range(6000)]
    assert set(faces) <= {1, 2, 3, 4, 5, 6}
    assert set(faces) == {1, 2, 3, 4, 5, 6}, "每一面都该出现过"
    mean = sum(faces) / len(faces)
    assert abs(mean - 3.5) < 0.15, f"均值 {mean:.3f} 偏离 3.5 太多"


def test_limits_are_reported_not_silently_bypassed() -> None:
    """★ 超限一律**拒绝并说明原因**，绝不"截断后照算"（截断会静默改变语义）。"""
    cases = {
        "200d6": "最多掷 100 颗",
        "1d2000": "最多 1000 个面",
        "1d0": "至少要有 2 个面",
        "(" * 12 + "1d6" + ")" * 12: "嵌套太深",
        "1d6/0": "除数不能是 0",
        "1d6+": "没写完",
    }
    for expression, keyword in cases.items():
        result = dice_mod.evaluate(expression)
        assert not result.ok, expression
        assert keyword in result.error, f"{expression} → {result.error}"
    assert "太长" in dice_mod.evaluate("1d6" * 200).error


def test_no_code_is_ever_executed() -> None:
    """★ 插件不放开 JS，解析器也不许被当成解释器：一切非骰子语法都只是"看不懂"。"""
    for expression in ("__import__('os').system('echo hi')", "1d6; import os", "1d6 or 1"):
        result = dice_mod.evaluate(expression)
        assert not result.ok
        assert result.total is None
        assert "看不懂的字符" in result.error or "多余内容" in result.error


def test_expression_and_label_are_split_the_same_way_everywhere() -> None:
    assert dice_mod.split_expression("1d20+5 力量检定") == ("1d20+5", "力量检定")
    assert dice_mod.split_expression("2d6 + 3 力量检定") == ("2d6 + 3", "力量检定")
    assert dice_mod.split_expression("1d20 + 5") == ("1d20 + 5", "")
    assert dice_mod.split_expression("1d100 # 聆听") == ("1d100", "聆听")


def test_triggers_only_match_at_the_start_of_a_line() -> None:
    spec = dice_mod.parse_config({})
    assert dice_mod.scan_commands("我该/r 1d6 吗", spec) == [], "行中间的不是指令"
    assert len(dice_mod.scan_commands("/r 1d6", spec)) == 1
    assert len(dice_mod.scan_commands("骰个 1d6", spec)) == 0, "触发词必须紧贴行首"


def test_longer_trigger_wins_over_the_shorter_one() -> None:
    """★ `/roll` 不能被更短的 `/r` 抢走（否则剩下的 `oll 1d20` 会被当成表达式）。"""
    spec = dice_mod.parse_config({})
    rolls = dice_mod.scan_commands("/roll 1d20", spec)
    assert len(rolls) == 1 and rolls[0].ok and rolls[0].expression == "1d20"


def test_bare_trigger_uses_the_default_expression() -> None:
    spec = dice_mod.parse_config({"default_expr": "2d6"})
    rolls = dice_mod.scan_commands("掷骰", spec)
    assert len(rolls) == 1 and rolls[0].expression == "2d6"


def test_too_many_commands_are_capped_with_an_explanation() -> None:
    spec = dice_mod.parse_config({})
    rolls = dice_mod.scan_commands("\n".join(["/r 1d6"] * 8), spec)
    assert len(rolls) == dice_mod.MAX_ROLLS_PER_TURN + 1
    assert rolls[-1].error and "最多掷" in rolls[-1].error


def test_model_roll_tags_are_replaced_by_real_numbers() -> None:
    spec = dice_mod.parse_config({})
    text, rolls = dice_mod.resolve_model_rolls(
        "我举盾格挡。<roll>1d20+5</roll> 门后传来低吼。", spec, rng=_Seq([12])
    )
    assert "<roll>" not in text and "</roll>" not in text
    assert "（掷骰 1d20+5 = 17" in text
    assert len(rolls) == 1 and rolls[0].total == 17 and rolls[0].source == "model"


def test_model_roll_tags_are_stripped_even_without_the_plugin() -> None:
    """★ 插件没启用也要剥：绝不能让用户看到 `<roll>` 这种原始协议文本。"""
    text, rolls = dice_mod.resolve_model_rolls("挥剑。<roll>1d20</roll>", None)
    assert "<roll>" not in text and rolls == []
    assert "骰子插件未启用" in text


def test_a_broken_roll_tag_keeps_the_rest_of_the_reply() -> None:
    """没有闭合标签时只能剥标签本身 —— 掷骰常写在句子中间，整段截掉会吞掉正文。"""
    spec = dice_mod.parse_config({})
    text, rolls = dice_mod.resolve_model_rolls("先掷 <roll>1d20 然后我继续说话", spec)
    assert "然后我继续说话" in text
    assert "<roll>" not in text
    assert len(rolls) == 1 and rolls[0].error


def test_prompt_blocks_say_where_the_numbers_came_from() -> None:
    spec = dice_mod.parse_config({})
    contract = dice_mod.render_contract(spec)
    assert "没有**随机数能力" in contract or "没有" in contract
    assert "<roll>" in contract
    rows = [dice_mod.evaluate("1d100", rng=_Seq([7])).to_dict()]
    block = dice_mod.render_turn_block(rows)
    assert dice_mod.TURN_BLOCK_TITLE in block and "**7**" in block
    assert "剧情必须按上面的点数走" in block


def test_bad_payloads_never_break_the_reader() -> None:
    assert dice_mod.loads(None) == []
    assert dice_mod.loads("{不是 JSON") == []
    assert dice_mod.loads('{"a": 1}') == []
    assert dice_mod.loads('[{"total": 3}, 5]') == [{"total": 3}]
    assert dice_mod.dumps([]) is None


# ==================================================================
#  二、接口层：点数落库、「预览 == 实际」、开关
# ==================================================================
class FakeAdapter:
    script: dict = {}

    def __init__(self, row=None, **_):
        self.row = row
        self.default_params = GenerationParams(max_tokens=512)

    @property
    def budget(self):
        window = getattr(self.row, "context_window", None) or 8192
        max_out = getattr(self.row, "max_tokens", None) or 1024
        return compute_context_budget(window, max_out)

    def chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        return ChatResult(
            content=FakeAdapter.script.get("content", "（假回复）"),
            model="mock-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )

    def stream_chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        yield StreamChunk(delta=FakeAdapter.script.get("content", "（假回复）"))
        yield StreamChunk(finish_reason="stop", usage=TokenUsage(total_tokens=15))

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    FakeAdapter.script = {}
    monkeypatch.setattr(engine, "build_adapter", lambda row: FakeAdapter(row))
    monkeypatch.setattr(engine.memory_mod, "remember_turn", lambda **_: None)
    yield FakeAdapter
    FakeAdapter.script = {}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def user(client: TestClient):
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"dc_{token}",
        "email": f"dc_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    logged = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert logged.status_code == 200
    data = {
        "username": account["username"],
        "headers": {"Authorization": f"Bearer {logged.json()['data']['access_token']}"},
    }
    yield data
    from sqlalchemy import select

    from app.db.models import User
    from app.db.mysql import session_scope

    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == data["username"]))
        if row is not None:
            db.delete(row)


def _make_session(client: TestClient, user: dict) -> int:
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"dc_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-dice-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    card = client.post(
        CARDS,
        # ★ 卡名里不能带"骰子"两个字：卡名会进系统提示词，会让下面
        #   "没启用插件时提示词里不该出现骰子"的断言永远为假（踩过一次）
        json={"name": f"检定卡_{uuid.uuid4().hex[:6]}", "greeting": "……你来了。"},
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
    session = client.post(
        BASE,
        json={
            "character_card_id": card.json()["data"]["id"],
            "llm_provider_id": provider.json()["data"]["id"],
        },
        headers=user["headers"],
    )
    assert session.status_code == 201, session.text
    return session.json()["data"]["id"]


def _add_dice_plugin(client: TestClient, user: dict, **overrides) -> dict:
    config = {
        "triggers": ["/r", "/roll", "掷骰"],
        "default_expr": "1d100",
        "allow_model_roll": True,
        "show_detail": True,
        "explain": True,
        "max_dice": 100,
        "max_sides": 1000,
    }
    config.update(overrides)
    created = client.post(
        PLUGINS,
        json={
            "name": f"跑团骰点_{uuid.uuid4().hex[:6]}",
            "kind": "dice",
            "description": "测试用",
            "config": config,
            "enabled": True,
        },
        headers=user["headers"],
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]


def test_catalog_offers_the_dice_plugin(client: TestClient, user: dict) -> None:
    listed = client.get(PLUGINS, headers=user["headers"])
    assert listed.status_code == 200, listed.text
    keys = [item["key"] for item in listed.json()["data"]["catalog"]]
    assert "trpg_dice" in keys, "骰子插件必须能从内置目录一键添加"


def test_a_broken_default_expression_is_rejected_at_save_time(
    client: TestClient, user: dict
) -> None:
    """★ 写错的默认表达式不该等到用户掷骰时才发现。"""
    bad = client.post(
        PLUGINS,
        json={"name": "坏的骰子", "kind": "dice", "config": {"default_expr": "1d6+"}},
        headers=user["headers"],
    )
    assert bad.status_code == 400, bad.text
    assert "默认表达式写错了" in bad.text


def test_user_command_rolls_once_and_the_prompt_carries_that_same_number(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 本组最关键的一条：点数落库 → 提示词里的数字与接口返回的数字**必须相同**。

    如果改成"构建提示词时现掷"，这里就会挂 —— 而用户点一次「查看提示词」
    就会看到另一个点数（"预览与实际不一致"）。
    """
    session_id = _make_session(client, user)
    _add_dice_plugin(client, user)
    fake_llm.script["content"] = "（他把骰子推回来）"

    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "/r 1d100 聆听"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    body = sent.json()["data"]
    rolls = body["user_message"]["rolls"]
    assert len(rolls) == 1 and rolls[0]["source"] == "user"
    assert rolls[0]["label"] == "聆听" and rolls[0]["total"] is not None

    system = fake_llm.script["requests"][-1].messages[0].content
    assert dice_mod.TURN_BLOCK_TITLE in system, "本轮点数必须以客观事实的形式进提示词"
    assert f"**{rolls[0]['total']}**" in system, "提示词里的点数与落库的点数必须一致"
    assert "剧情必须按上面的点数走" in system

    # 预览走的是同一条装配路径：数字也必须一样
    detail = client.get(
        f"{BASE}/{session_id}", params={"with_prompt": True}, headers=user["headers"]
    )
    assert detail.status_code == 200, detail.text
    preview = detail.json()["data"]["prompt"]["system_prompt"]
    assert f"**{rolls[0]['total']}**" in preview, "「查看提示词」不能给出另一个点数"


def test_dice_rules_are_only_injected_when_the_plugin_is_enabled(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _make_session(client, user)
    fake_llm.script["content"] = "嗯。"
    client.post(
        f"{BASE}/{session_id}/messages", json={"content": "/r 1d100"}, headers=user["headers"]
    )
    system = fake_llm.script["requests"][-1].messages[0].content
    assert dice_mod.TURN_BLOCK_TITLE not in system, "没启用插件时不该注入本轮骰点"
    assert "<roll>" not in system, "没启用插件时不该告诉模型 <roll> 这种写法"

    created = _add_dice_plugin(client, user)
    client.post(
        f"{BASE}/{session_id}/messages", json={"content": "你好"}, headers=user["headers"]
    )
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "<roll>" in system and "随机数" in system, "启用后必须告诉模型怎么请求掷骰"

    # 停用之后立刻不再生效（与其它插件同一套语义）
    assert (
        client.patch(
            f"{PLUGINS}/{created['id']}", json={"enabled": False}, headers=user["headers"]
        ).status_code
        == 200
    )
    client.post(
        f"{BASE}/{session_id}/messages", json={"content": "/r 1d100"}, headers=user["headers"]
    )
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "<roll>" not in system and dice_mod.TURN_BLOCK_TITLE not in system


def test_model_requested_roll_is_resolved_and_stored(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _make_session(client, user)
    _add_dice_plugin(client, user)
    fake_llm.script["content"] = "他抬手格挡。<roll>1d20+5</roll>（然后等你）"

    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我攻击他"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    assistant = sent.json()["data"]["assistant_message"]
    assert "<roll>" not in assistant["content"] and "</roll>" not in assistant["content"]
    assert "（掷骰 1d20+5 = " in assistant["content"]
    rolls = assistant["rolls"]
    assert len(rolls) == 1 and rolls[0]["source"] == "model"
    assert rolls[0]["expression"] == "1d20+5"
    assert f"= {rolls[0]['total']}" in assistant["content"], "正文里的数字要与落库的一致"

    # 下一轮模型在历史里看到的是**真实点数**（否则它只能自己编）
    client.post(f"{BASE}/{session_id}/messages", json={"content": "继续"}, headers=user["headers"])
    request = fake_llm.script["requests"][-1]
    history = " ".join(str(getattr(item, "content", "")) for item in request.messages)
    assert f"（掷骰 1d20+5 = {rolls[0]['total']}" in history


def test_rolls_survive_a_reload(client: TestClient, user: dict, fake_llm) -> None:
    """点数读的是落库那一份：重新打开会话（重新拉详情）必须还是同一个数字。"""
    session_id = _make_session(client, user)
    _add_dice_plugin(client, user)
    fake_llm.script["content"] = "（沉默）"
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "/r 3d6 属性"}, headers=user["headers"]
    )
    total = sent.json()["data"]["user_message"]["rolls"][0]["total"]

    for _ in range(2):
        detail = client.get(f"{BASE}/{session_id}", headers=user["headers"])
        messages = detail.json()["data"]["messages"]
        user_message = [m for m in messages if m["role"] == "user"][-1]
        assert user_message["rolls"][0]["total"] == total


def test_a_bad_user_expression_becomes_a_readable_error(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _make_session(client, user)
    _add_dice_plugin(client, user)
    fake_llm.script["content"] = "（不懂）"
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "/r 1d6+"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    rolls = sent.json()["data"]["user_message"]["rolls"]
    assert len(rolls) == 1 and rolls[0]["error"]
    assert "×" not in rolls[0]["error"] and rolls[0]["total"] is None
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "不合法" in system and "别替他编一个点数" in system


def test_a_plugin_written_by_hand_can_be_created_from_scratch(
    client: TestClient, user: dict, fake_llm
) -> None:
    """手工新建（不走目录）也要能用 —— 校验只有一套。"""
    session_id = _make_session(client, user)
    created = _add_dice_plugin(client, user, triggers=["#投"], default_expr="1d6")
    assert created["kind"] == "dice"
    assert created["summary"].startswith("触发词")

    fake_llm.script["content"] = "（点头）"
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "#投 3d6"}, headers=user["headers"]
    )
    rolls = sent.json()["data"]["user_message"]["rolls"]
    assert len(rolls) == 1 and rolls[0]["expression"] == "3d6"
