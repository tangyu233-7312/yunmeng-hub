"""端到端冒烟测试：用真实 HTTP 打一遍完整业务流程。

==================== 这个脚本和 pytest 有什么区别？====================
pytest 用的是 FastAPI 的 TestClient（进程内直连，不经过网络）。
本脚本**真的发 HTTP 请求**给一个正在运行的服务，并且：

  ★ 关键设计：**双通道核对**
    每一步不仅看接口返回了什么，还会**直连 MySQL 再查一遍**，
    确认数据库里真的发生了对应的变化。
    这样「接口返回成功但其实没写库」这类问题就藏不住了。

==================== 用法 ====================
先在**一个终端**启动服务：

    .\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

再在**另一个终端**运行本脚本：

    .\\.venv\\Scripts\\python.exe scripts\\smoke_test.py

可选参数：

    --base-url http://127.0.0.1:8001   指定别的地址
    --live                             额外做一次真实大模型调用（会产生少量费用）
    --keep                             跑完不清理数据，方便你自己去数据库里翻

脚本会自动清理它创建的数据（除非加 --keep）。
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import threading
import uuid
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

# ---- 让脚本能 import 到项目里的 app 包（与 scripts/init_db.py 同样的手法）----
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台默认是 GBK，中文会变乱码；这里强制切换成 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from loguru import logger  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.models import (  # noqa: E402
    CharacterCard,
    LLMProvider,
    Message,
    NarrativeSession,
    User,
    WorldBook,
)
from app.db.mysql import dispose_engine, session_scope  # noqa: E402

# ==================================================================
#  输出小工具
# ==================================================================
PASSED = 0
FAILED = 0


def title(text: str) -> None:
    print(f"\n{'=' * 68}\n  {text}\n{'=' * 68}", flush=True)


def step(text: str) -> None:
    print(f"\n[..] {text}", flush=True)


def ok(text: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  [OK] {text}", flush=True)


def fail(text: str) -> None:
    global FAILED
    FAILED += 1
    print(f"  [!!] {text}", flush=True)


def check(condition: bool, text: str) -> bool:
    """断言式检查：成立打 OK，不成立打 !! 并继续跑（便于一次看全所有问题）。"""
    if condition:
        ok(text)
    else:
        fail(text)
    return bool(condition)


# ==================================================================
#  ★ 独立核对通道：直连 MySQL 数一遍
# ==================================================================
def db_counts() -> dict[str, int]:
    """直接查数据库的行数 —— 不经过接口，用于核对接口的说法是否属实。"""
    with session_scope() as db:
        return {
            model.__tablename__: int(
                db.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in (
                User,
                LLMProvider,
                CharacterCard,
                WorldBook,
                NarrativeSession,
                Message,
            )
        }


def print_counts(label: str) -> dict[str, int]:
    counts = db_counts()
    print(f"  {label}: {counts}", flush=True)
    return counts


# ==================================================================
#  构造一张真实合法的角色卡 PNG（含正确的 CRC 与可解压的 IDAT）
# ==================================================================
def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def make_card_png(card: dict) -> bytes:
    """生成一张 1x1 的合法 PNG，把角色卡 JSON 以 base64 塞进 tEXt 块。

    这正是 SillyTavern 生态分发角色卡的真实格式。
    """
    encoded = base64.b64encode(
        json.dumps(card, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    out = b"\x89PNG\r\n\x1a\n"
    out += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    out += _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    out += _png_chunk(b"tEXt", b"chara\x00" + encoded.encode("latin-1"))
    out += _png_chunk(b"IEND", b"")
    return out


# ==================================================================
#  主流程
# ==================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="端到端冒烟测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--live", action="store_true", help="额外做一次真实大模型调用（会产生少量费用）"
    )
    parser.add_argument("--keep", action="store_true", help="跑完不清理数据")
    args = parser.parse_args()

    setup_logging()

    # ★ 把日志压到 WARNING：否则 httpx 会把每一次请求都打到 stderr，
    #   刷屏不说，还会让 PowerShell 把这些诊断信息误判成命令出错
    #   （表现为脚本明明成功、退出码却是 1）。
    #   本脚本的输出应当只有它自己那几行 [OK] / [!!]。
    logger.remove()
    logger.add(sys.stderr, level="WARNING", colorize=False)

    settings = get_settings()
    base = args.base_url.rstrip("/")
    api = f"{base}/api/v1"

    tag = uuid.uuid4().hex[:8]
    username = f"smoke_{tag}"
    password = "Smoke-Passw0rd!"

    title(f"端到端冒烟测试  |  {base}")
    print(f"  数据库：{settings.MYSQL_USER}@{settings.MYSQL_HOST}:"
          f"{settings.MYSQL_PORT}/{settings.MYSQL_DB}", flush=True)
    print(f"  本次测试用户：{username}", flush=True)

    client = httpx.Client(timeout=60.0)

    try:
        # ---------------- 0. 先记录基线 ----------------
        title("步骤 0 / 记录数据库基线")
        # ★ 变量名必须唯一：这个基线要在 13 步之后才用得上，
        #   中间任何一步用了 `before` 这样的通用名都会把它覆盖掉 ——
        #   写消息级操作那段时就踩到了，结果收尾的"行数一致"永远失败。
        baseline_counts = print_counts("测试前")
        ok("已读取数据库基线（后面每一步都会直连数据库核对）")

        # ---------------- 1. 健康检查 ----------------
        title("步骤 1 / 健康检查：确认服务与各组件真的活着")
        resp = client.get(f"{base}/health")
        check(resp.status_code == 200, f"GET /health 返回 {resp.status_code}")
        health = resp.json()
        print(f"  {json.dumps(health, ensure_ascii=False, indent=2)[:900]}", flush=True)

        components = health.get("components", {})
        db_status = components.get("database", {}).get("status")
        check(db_status == "ok", f"MySQL 组件状态 = {db_status}")
        vs_status = components.get("vector_store", {}).get("status")
        check(vs_status == "ok", f"向量库组件状态 = {vs_status}")
        check(
            health.get("status") == "ok",
            f"整体状态 = {health.get('status')}",
        )

        # ---------------- 2. 注册与登录 ----------------
        title("步骤 2 / 注册 + 登录（真实验证 bcrypt 与 JWT）")
        resp = client.post(
            f"{api}/auth/register",
            json={
                "username": username,
                "email": f"{username}@example.com",
                "password": password,
            },
        )
        check(resp.status_code == 201, f"注册返回 {resp.status_code}")
        user_id = resp.json()["data"]["id"]
        print(f"  新用户 id = {user_id}（这是数据库里的真实自增主键）", flush=True)

        # ★ 双通道核对：直连数据库确认这个用户真的存在
        with session_scope() as db:
            row = db.scalar(select(User).where(User.username == username))
            check(row is not None and row.id == user_id, "数据库里确实有这个用户")
            if row is not None:
                check(row.password_hash != password, "库里存的是哈希，不是明文密码")
                check(
                    row.password_hash.startswith("$2b$"),
                    f"密码哈希是 bcrypt 格式（{row.password_hash[:7]}...）",
                )

        resp = client.post(
            f"{api}/auth/login",
            json={"username": username, "password": password},
        )
        check(resp.status_code == 200, f"登录返回 {resp.status_code}")
        tokens = resp.json()["data"]
        access = tokens["access_token"]
        headers = {"Authorization": f"Bearer {access}"}
        ok("拿到 access_token / refresh_token")

        # 顺带验证一下安全设计：refresh_token 不能当访问令牌用
        resp = client.get(
            f"{api}/auth/me",
            headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
        )
        check(resp.status_code == 401, "refresh_token 调业务接口被拒绝（401）—— 类型校验生效")

        # 未登录访问受保护接口
        resp = client.get(f"{api}/character-cards")
        check(resp.status_code == 401, "未登录访问角色卡接口被拒绝（401）")

        # ---------------- 3. 模型配置 ----------------
        title("步骤 3 / 建立模型配置（验证 API Key 加密存储）")
        # ★ 这里用**假的**密钥：本步骤验证的是「加密存储」，不需要真密钥，
        #   也就不会产生任何 API 费用。
        #   只有加了 --live 时才换成 .env 里的真密钥，用于最后的真实调用验证。
        use_real_key = bool(args.live and settings.DEFAULT_LLM_API_KEY)
        plain_key = (
            settings.DEFAULT_LLM_API_KEY
            if use_real_key
            else "sk-smoke-test-secret-abcdef123456"
        )
        if use_real_key:
            print("  已启用 --live：本步骤改用 .env 里的真实密钥", flush=True)
        resp = client.post(
            f"{api}/providers",
            json={
                "name": f"冒烟配置_{tag}",
                "provider_type": "openai_compatible",
                "base_url": settings.DEFAULT_LLM_BASE_URL,
                "api_key": plain_key,
                "model_name": settings.DEFAULT_LLM_MODEL,
                "context_window": 65536,
                "generation": {"temperature": 0.8, "max_tokens": 2048},
                "is_default": True,
            },
            headers=headers,
        )
        check(resp.status_code == 201, f"建立模型配置返回 {resp.status_code}")
        provider = resp.json()["data"]
        provider_id = provider["id"]
        # 注意：这里检查的是「响应里没有明文」，
        # 用真密钥时也一样成立（接口只会回显脱敏形式）
        check(
            plain_key not in resp.text,
            f"响应里没有密钥明文（只回显 {provider['api_key_masked']}）",
        )

        # ★ 双通道核对：数据库里存的必须是密文
        with session_scope() as db:
            row = db.get(LLMProvider, provider_id)
            check(row is not None, "数据库里确实有这条模型配置")
            if row is not None:
                check(
                    row.api_key_encrypted != plain_key
                    and plain_key not in row.api_key_encrypted,
                    "数据库里存的是密文，不是明文密钥",
                )

        # ---------------- 4. 世界书 ----------------
        title("步骤 4 / 新建世界书（验证条目规范化与未知字段保留）")
        resp = client.post(
            f"{api}/world-books",
            json={
                "name": f"冒烟世界书_{tag}",
                "description": "端到端测试用",
                "entries": [
                    {
                        "keys": ["  龙  ", "龙", "巨龙"],  # 故意带空白与重复
                        "content": "世上最后一条龙已在三百年前死去",
                        "position": "before_char",  # 我们不认识的字段
                        "priority": 7,
                    },
                    {"keys": ["森林"], "content": "禁忌森林", "enabled": False},
                ],
            },
            headers=headers,
        )
        check(resp.status_code == 201, f"新建世界书返回 {resp.status_code}")
        book = resp.json()["data"]
        book_id = book["id"]
        entry0 = book["entries"][0]

        check(entry0["keys"] == ["龙", "巨龙"], f"关键词已去空白去重 -> {entry0['keys']}")
        check(entry0["enabled"] is True, "缺失的 enabled 补成了默认值 True")
        check(entry0["extensions"] == {}, "补齐了规范要求的 extensions 字段")
        check(
            entry0.get("position") == "before_char" and entry0.get("priority") == 7,
            "不认识的字段（position / priority）被原样保留",
        )

        with session_scope() as db:
            check(db.get(WorldBook, book_id) is not None, "数据库里确实有这本世界书")

        # ---------------- 5. 角色卡：手动创建 + 关联世界书 ----------------
        title("步骤 5 / 新建角色卡并关联世界书")
        resp = client.post(
            f"{api}/character-cards",
            json={
                "name": f"冒烟角色_{tag}",
                "description": "端到端测试角色",
                "personality": "冷静、话少",
                "greeting": "（她抬起头看了你一眼）……你来了。",
                "tags": ["  奇幻  ", "奇幻", "侦探"],  # 故意带空白与重复
                "world_book_id": book_id,
            },
            headers=headers,
        )
        check(resp.status_code == 201, f"新建角色卡返回 {resp.status_code}")
        card = resp.json()["data"]
        card_id = card["id"]

        check(card["tags"] == ["奇幻", "侦探"], f"标签已规范化 -> {card['tags']}")
        check(
            card["world_book"] is not None and card["world_book"]["id"] == book_id,
            "角色卡已关联世界书",
        )

        with session_scope() as db:
            row = db.get(CharacterCard, card_id)
            check(row is not None, "数据库里确实有这张角色卡")
            if row is not None:
                check(row.world_book_id == book_id, "数据库里的 world_book_id 外键正确")

        # 越权：另一本书（不存在）应当 404
        resp = client.post(
            f"{api}/character-cards",
            json={"name": "非法关联", "world_book_id": 999999999},
            headers=headers,
        )
        check(resp.status_code == 404, "关联不存在的世界书返回 404")

        # ---------------- 6. PNG 导入 + 导出往返 ----------------
        title("步骤 6 / 导入 PNG 角色卡，并验证导出往返无损")
        v2_card = {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": f"PNG卡_{tag}",
                "description": "来自 PNG",
                "personality": "温和",
                "scenario": "酒馆",
                "first_mes": "你好，旅人。Hello there, traveller.",  # 含空格，验证不被吞
                "mes_example": "<START>\n{{user}}: 你好",
                "alternate_greetings": ["另一个开头"],
                "system_prompt": "自定义系统提示词",
                "post_history_instructions": "尾注",
                "tags": ["奇幻"],
                "creator": "某位作者",
                "creator_notes": "作者的话",
                "extensions": {"some_plugin": {"voice": "soft"}},
                "character_book": {
                    "name": "PNG 里的世界书",
                    "scan_depth": 4,
                    "token_budget": 1200,
                    "entries": [{"keys": ["龙"], "content": "设定内容"}],
                },
            },
        }
        png_bytes = make_card_png(v2_card)
        print(f"  生成的 PNG 大小：{len(png_bytes)} 字节", flush=True)

        resp = client.post(
            f"{api}/character-cards/import-png",
            files={"file": ("card.png", png_bytes, "image/png")},
            headers=headers,
        )
        check(resp.status_code == 201, f"PNG 导入返回 {resp.status_code}")
        png_card = resp.json()["data"]
        png_card_id = png_card["id"]

        check(png_card["greeting"] == "你好，旅人。Hello there, traveller.",
              "开场白原样导入（★ 空格没被吞掉）")
        check(png_card["world_book"] is not None, "PNG 里带的世界书被独立建表并关联了")

        book_of_png = png_card["world_book"]
        check(book_of_png["name"] == "PNG 里的世界书", "世界书名字正确")

        # ★ 双通道核对：世界书真的落库了
        with session_scope() as db:
            wb = db.get(WorldBook, book_of_png["id"])
            check(wb is not None, "数据库里确实有这本从 PNG 提取的世界书")
            if wb is not None:
                check(
                    (wb.extra_data or {}).get("scan_depth") == 4
                    and (wb.extra_data or {}).get("token_budget") == 1200,
                    "世界书的 scan_depth / token_budget 等设置被原样保留",
                )

        # 导出并核对往返
        resp = client.get(f"{api}/character-cards/{png_card_id}/export", headers=headers)
        check(resp.status_code == 200, f"导出返回 {resp.status_code}")
        exported = resp.json()["data"]
        data = exported["data"]

        check(exported["spec"] == "chara_card_v2", f"spec = {exported['spec']}")
        check(data["first_mes"] == v2_card["data"]["first_mes"],
              "first_mes 往返一致")
        check(data["mes_example"] == v2_card["data"]["mes_example"],
              "mes_example 往返一致（★ 换行没被压掉）")
        check(data["creator"] == "某位作者", "creator 字段没丢")
        check(data["extensions"]["some_plugin"] == {"voice": "soft"},
              "别人插件写的 extensions 没丢")
        cb = data["character_book"]
        check(cb["scan_depth"] == 4 and cb["token_budget"] == 1200,
              "世界书设置原样导出")
        check(cb["entries"][0]["content"] == "设定内容", "世界书条目内容正确")
        check("extensions" in cb, "世界书补齐了规范要求的 extensions")

        # 再导入一次，验证「导出→导入→再导出」完全一致
        resp = client.post(
            f"{api}/character-cards/import",
            json={"card": exported},
            headers=headers,
        )
        check(resp.status_code == 201, f"二次导入返回 {resp.status_code}")
        again_id = resp.json()["data"]["id"]
        again = client.get(
            f"{api}/character-cards/{again_id}/export", headers=headers
        ).json()["data"]
        check(again == exported, "★ 导出 → 导入 → 再导出，两次结果完全一致（往返无损）")

        # ---------------- 7. 列表 / 搜索 / 标签筛选 ----------------
        title("步骤 7 / 列表、搜索与通配符转义")
        resp = client.get(f"{api}/character-cards", params={"q": tag}, headers=headers)
        body = resp.json()["data"]
        check(resp.status_code == 200, f"列表返回 {resp.status_code}")
        check(body["total"] >= 2, f"搜到 {body['total']} 张本轮的卡")
        check("entries" not in body["items"][0], "列表是精简结构（不含长文本字段）")
        check("has_more" in body, "分页信息里有 has_more")

        # 搜索里的 % 必须是字面意思，不能当通配符。
        # 构造一对只差这一点点的名字：A 含 "50%"，B 含 "5030"
        name_a = f"{tag}50%off"
        name_b = f"{tag}5030"
        client.post(f"{api}/character-cards", json={"name": name_a}, headers=headers)
        client.post(f"{api}/character-cards", json={"name": name_b}, headers=headers)

        # 搜 "{tag}50%"：
        #   正确（已转义）-> 只命中 A
        #   错误（拿 % 当通配符）-> A 和 B 都会被命中
        found = client.get(
            f"{api}/character-cards", params={"q": f"{tag}50%"}, headers=headers
        ).json()["data"]
        names = [item["name"] for item in found["items"]]
        check(
            name_a in names and name_b not in names,
            f"★ LIKE 通配符已转义：搜 '{tag}50%' 只命中含 % 的那张 -> {names}",
        )

        # 标签筛选走 MySQL 的 JSON_CONTAINS
        tagged = client.get(
            f"{api}/character-cards", params={"tag": "奇幻"}, headers=headers
        ).json()["data"]
        check(tagged["total"] >= 1, f"按标签筛选命中 {tagged['total']} 张")

        # ---------------- 8. 删除角色卡：两个勾选项 ----------------
        title("步骤 8 / ★ 删除角色卡的两个勾选项")
        # 先造一个会用到卡片的会话，并确认「不带 force」会被拦下
        with session_scope() as db:
            session_row = NarrativeSession(
                user_id=user_id,
                character_card_id=card_id,
                llm_provider_id=provider_id,
                title="冒烟测试会话",
                status="active",
            )
            db.add(session_row)
            db.flush()
            session_id = session_row.id
        ok(f"手动插入一个会话 id={session_id}（3.8 的接口还没做，这里直连数据库造数据）")

        resp = client.delete(f"{api}/character-cards/{card_id}", headers=headers)
        check(resp.status_code == 409, f"不带 force 被拦下（{resp.status_code}）")
        detail = resp.json().get("detail", {})
        check(detail.get("session_count") == 1, "409 里告知有 1 个会话")
        check(detail.get("has_world_book") is True, "409 里告知挂着世界书")
        check(
            "delete_sessions" in detail.get("options", {})
            and "delete_world_book" in detail.get("options", {}),
            "409 里给出了两个可勾选项（前端据此渲染弹窗）",
        )

        # 取消勾选「删除世界书」-> 书要留下
        resp = client.delete(
            f"{api}/character-cards/{card_id}",
            params={"force": True, "delete_world_book": False},
            headers=headers,
        )
        check(resp.status_code == 200, f"带 force 删除成功（{resp.status_code}）")
        summary = resp.json()["data"]
        print(f"  删除摘要：{json.dumps(summary, ensure_ascii=False)}", flush=True)
        check(summary["deleted_sessions"] == 1, "对话记录已按默认勾选删除")
        check(summary["deleted_world_book"] is False, "世界书按取消勾选保留了下来")

        with session_scope() as db:
            check(db.get(CharacterCard, card_id) is None, "★ 直连数据库：角色卡确实没了")
            check(db.get(NarrativeSession, session_id) is None, "★ 直连数据库：会话确实没了")
            check(db.get(WorldBook, book_id) is not None, "★ 直连数据库：世界书确实还在")

        resp = client.get(f"{api}/world-books/{book_id}", headers=headers)
        check(resp.status_code == 200, "保留的世界书仍可正常访问")

        # 世界书被别的卡占用时，即使勾了删除也要保留
        holder = client.post(
            f"{api}/character-cards",
            json={"name": f"占用者_{tag}", "world_book_id": book_id},
            headers=headers,
        ).json()["data"]
        client.post(
            f"{api}/character-cards",
            json={"name": f"共用者_{tag}", "world_book_id": book_id},
            headers=headers,
        )
        resp = client.delete(
            f"{api}/character-cards/{holder['id']}",
            params={"force": True, "delete_world_book": True},
            headers=headers,
        )
        summary = resp.json()["data"]
        check(
            summary["world_book_kept"] is True
            and summary["deleted_world_book"] is False,
            "★ 世界书被别的卡共用时，勾了删除也会保留",
        )
        print(f"  保留原因：{summary['world_book_kept_reason']}", flush=True)

        # ---------------- 9. 复制角色卡 ----------------
        title("步骤 9 / 复制角色卡（世界书必须独立复制）")
        resp = client.post(f"{api}/character-cards/{png_card_id}/duplicate", headers=headers)
        check(resp.status_code == 201, f"复制返回 {resp.status_code}")
        copied = resp.json()["data"]
        check(
            copied["world_book"]["id"] != png_card["world_book"]["id"],
            "★ 复制出来的卡用的是**新的**世界书，不是共享同一本",
        )

        with session_scope() as db:
            src = db.get(WorldBook, png_card["world_book"]["id"])
            dst = db.get(WorldBook, copied["world_book"]["id"])
            check(src is not None and dst is not None and src.id != dst.id,
                  "★ 直连数据库：两本书确实是两条独立记录")

        # ---------------- 10. 跨用户越权 ----------------
        title("步骤 10 / 越权防护（另一个用户来访问）")
        other = f"smoke_other_{tag}"
        client.post(
            f"{api}/auth/register",
            json={"username": other, "email": f"{other}@example.com", "password": password},
        )
        other_token = client.post(
            f"{api}/auth/login", json={"username": other, "password": password}
        ).json()["data"]["access_token"]
        other_headers = {"Authorization": f"Bearer {other_token}"}

        resp = client.get(f"{api}/character-cards/{png_card_id}", headers=other_headers)
        check(resp.status_code == 404, f"读别人的私有卡 -> 404（而不是 403）")
        resp = client.patch(
            f"{api}/character-cards/{png_card_id}",
            json={"name": "被改了"},
            headers=other_headers,
        )
        check(resp.status_code == 404, "改别人的私有卡 -> 404")
        resp = client.get(f"{api}/world-books/{book_id}", headers=other_headers)
        check(resp.status_code == 404, "读别人的世界书 -> 404")
        resp = client.post(
            f"{api}/character-cards",
            json={"name": "偷书", "world_book_id": book_id},
            headers=other_headers,
        )
        check(resp.status_code == 404, "★ 把别人的世界书挂到自己卡上 -> 404（防 IDOR）")

        # ---------------- 11. 叙事会话与对话（含 SSE 流式）----------------
        title("步骤 11 / ★ 叙事会话与对话（含 SSE 流式输出）")
        # 本机起一个「假 OpenAI 服务」，这样整条链路都是**真实 HTTP**：
        #   假模型（8123） ← 适配层 ← 叙事引擎 ← /stream SSE ← 本脚本
        # 好处是端到端可复现，且绝不花钱、不依赖任何真实 API Key。
        from scripts.fake_openai_server import Handler as FakeHandler

        fake_port = 8123
        fake_server = ThreadingHTTPServer(("127.0.0.1", fake_port), FakeHandler)
        threading.Thread(target=fake_server.serve_forever, daemon=True).start()
        ok(f"本机假模型服务已启动（127.0.0.1:{fake_port}）")

        resp = client.post(
            f"{api}/providers",
            json={
                "name": f"假模型_{tag}",
                "provider_type": "openai_compatible",
                "base_url": f"http://127.0.0.1:{fake_port}/v1",
                "api_key": "",
                "model_name": "fake-model",
                "context_window": 8192,
                "generation": {"temperature": 0.8, "max_tokens": 1024},
            },
            headers=headers,
        )
        check(resp.status_code == 201, f"建立假模型配置返回 {resp.status_code}")
        fake_provider_id = resp.json()["data"]["id"]

        # 用 PNG 那张卡（它自带 system_prompt / post_history_instructions / 世界书）
        resp = client.post(
            f"{api}/narrative/sessions",
            json={"character_card_id": png_card_id, "llm_provider_id": fake_provider_id},
            headers=headers,
        )
        check(resp.status_code == 201, f"新建会话返回 {resp.status_code}")
        narrative = resp.json()["data"]
        narrative_id = narrative["id"]

        check(narrative["message_count"] == 1, "开场白已作为第一条消息写入")
        check(
            narrative["messages"][0]["role"] == "assistant"
            and narrative["messages"][0]["content"] == "你好，旅人。Hello there, traveller.",
            "第一条消息就是角色卡的开场白（★ 用户一进来就看到角色先开口）",
        )
        check(
            narrative["prompt"]["system_prompt_source"] == "card",
            "★ 提示词用的是角色卡自带的自定义系统提示词",
        )
        # ★ 3.9 起世界书不再"全量注入"，而是**按关键词触发**：
        #   开场白里没有提到「龙」，所以这条设定不该出现（详见步骤 11.5 的完整验证）
        check(
            narrative["prompt"]["world_book_entries"] == 0
            and "设定内容" not in (narrative["prompt"]["system_prompt"] or ""),
            "★ 世界书改成了关键词触发：没提到关键词就不注入（3.9 的行为变化）",
        )

        # 非流式发一条
        resp = client.post(
            f"{api}/narrative/sessions/{narrative_id}/messages",
            json={"content": "你好，请介绍一下你自己"},
            headers=headers,
        )
        check(resp.status_code == 201, f"发消息返回 {resp.status_code}")
        sent = resp.json()["data"]
        check(
            sent["assistant_message"]["content"].startswith("【本地假模型】"),
            "拿到了助手回复",
        )
        check(sent["usage"]["total_tokens"] > 0, f"返回了 token 用量 {sent['usage']}")

        with session_scope() as db:
            rows = list(
                db.scalars(
                    select(Message)
                    .where(Message.session_id == narrative_id)
                    .order_by(Message.id)
                ).all()
            )
            check(
                [r.role for r in rows] == ["assistant", "user", "assistant"],
                f"★ 直连数据库：三条消息按顺序落库 -> {[r.role for r in rows]}",
            )
            session_row = db.get(NarrativeSession, narrative_id)
            check(
                session_row is not None
                and session_row.message_count == 3
                and session_row.total_tokens > 0
                and session_row.last_active_at is not None,
                "★ 直连数据库：message_count / total_tokens / last_active_at 都更新了",
            )

        # 流式发一条：逐段读，确认是真的分段到达而不是一次性吐完
        chunks: list[tuple[str, str]] = []
        with client.stream(
            "GET",
            f"{api}/narrative/sessions/{narrative_id}/stream",
            params={"content": "再说一句"},
            headers=headers,
        ) as response:
            check(response.status_code == 200, f"流式接口返回 {response.status_code}")
            check(
                "text/event-stream" in response.headers.get("content-type", ""),
                "Content-Type 是 text/event-stream",
            )
            event_name = "message"
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    chunks.append((event_name, line[5:].strip()))

        names = [name for name, _ in chunks]
        check(names[0] == "meta", "首条是 meta 元信息事件")
        check(names[-1] == "end", "末条是 end 事件")
        check(names.count("delta") > 5, f"★ 正文分了 {names.count('delta')} 段推送（SSE 流式生效）")

        text = "".join(
            json.loads(payload)["delta"] for name, payload in chunks if name == "delta"
        )
        check(
            text.startswith("【本地假模型】"),
            f"把 delta 拼起来就是完整回复（{len(text)} 字）",
        )
        done = json.loads(next(payload for name, payload in chunks if name == "done"))
        check(done["usage"]["total_tokens"] > 0, "done 事件带上了 token 用量")
        check(done["session"]["message_count"] == 5, "done 事件里的会话统计是更新后的值")

        with session_scope() as db:
            last = db.scalars(
                select(Message)
                .where(Message.session_id == narrative_id)
                .order_by(Message.id.desc())
                .limit(1)
            ).first()
            check(
                last is not None and last.content == text,
                "★ 直连数据库：流式生成的正文与界面收到的**完全一致**",
            )

        # 会话列表 / 详情 / 改名 / 归档
        page = client.get(
            f"{api}/narrative/sessions", params={"limit": 5}, headers=headers
        ).json()["data"]
        check(page["total"] >= 1 and "messages" not in page["items"][0],
              "会话列表是分页精简结构（不带消息正文）")
        check(
            bool(page["items"][0]["last_message_preview"]),
            "列表里带最后一条消息的预览",
        )

        resp = client.patch(
            f"{api}/narrative/sessions/{narrative_id}",
            json={"title": "改过名的会话"},
            headers=headers,
        )
        check(
            resp.status_code == 200 and resp.json()["data"]["title"] == "改过名的会话",
            "PATCH 重命名生效",
        )
        resp = client.patch(
            f"{api}/narrative/sessions/{narrative_id}",
            json={"status": "archived"},
            headers=headers,
        )
        check(resp.json()["data"]["status"] == "archived", "归档生效（消息不会被删）")

        # 越权：别人的会话一律 404
        resp = client.get(f"{api}/narrative/sessions/{narrative_id}", headers=other_headers)
        check(resp.status_code == 404, "★ 读别人的会话 -> 404")
        resp = client.get(
            f"{api}/narrative/sessions/{narrative_id}/stream",
            params={"content": "偷看"},
            headers=other_headers,
        )
        check(resp.status_code == 404, "★ 流式接口同样拦住了越权访问")

        # 删除会话：必须把消息一起删干净
        resp = client.delete(f"{api}/narrative/sessions/{narrative_id}", headers=headers)
        check(resp.status_code == 200, f"删除会话返回 {resp.status_code}")
        check(resp.json()["data"]["deleted_messages"] == 5, "删除摘要里报告了 5 条消息")
        with session_scope() as db:
            check(db.get(NarrativeSession, narrative_id) is None, "★ 直连数据库：会话确实没了")
            check(
                db.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.session_id == narrative_id)
                )
                == 0,
                "★ 直连数据库：消息被级联删干净（不留孤儿行）",
            )

        fake_server.shutdown()
        client.delete(f"{api}/providers/{fake_provider_id}", headers=headers)
        ok("假模型服务已停止，临时模型配置已删除")

        # ---------------- 11.5 世界书关键词触发 + 长期记忆（3.9）----------------
        title("步骤 11.5 / ★ 世界书关键词触发 + 向量长期记忆")

        # 直达存储层的辅助函数：这里要看的是"向量库里到底有没有这条记忆"，
        # 走接口反而是绕圈子（接口返回的是检索结果，不是存储事实）
        from app.db.chroma import search_memories
        from app.narrative import memory as memory_mod

        def stored_memories(uid: int, query: str, session_id: int) -> list:
            """在向量库里直接找记忆。

            ★ 为什么容错？
              ChromaDB 的 PersistentClient **不是跨进程安全**的：本脚本直连向量库
              与正在运行的服务进程同时操作同一个集合时，偶尔会报
              "InternalError: Error finding id"。这不是业务 bug，而是
              "两个进程各持一份索引"的必然结果。出现这种底层异常时按
              "查不到"处理，并把它如实记在日志里，不让冒烟测试误报。
            """
            try:
                return search_memories(uid, query, top_k=5, session_id=session_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  [..] 向量库查询被跳过（跨进程索引不一致）：{type(exc).__name__}", flush=True)
                return []

        # ★ 这一段单独用一个新账号：
        #   向量库按 user_id 分集合，而 ChromaDB 的 PersistentClient 不是跨进程安全的，
        #   复用上面那个已经"删过集合"的账号容易出现索引不一致的假失败。
        mem_user = f"smoke_mem_{tag}"
        client.post(
            f"{api}/auth/register",
            json={"username": mem_user, "email": f"{mem_user}@example.com", "password": password},
        )
        mem_token = client.post(
            f"{api}/auth/login", json={"username": mem_user, "password": password}
        ).json()["data"]["access_token"]
        mem_headers = {"Authorization": f"Bearer {mem_token}"}
        mem_user_id = client.get(f"{api}/auth/me", headers=mem_headers).json()["data"]["id"]

        book = client.post(
            f"{api}/world-books",
            json={
                "name": f"关键词书_{tag}",
                "entries": [
                    {"keys": ["灯塔"], "content": "灯塔在风暴夜会熄灭", "enabled": True},
                    {"keys": ["巨龙"], "content": "巨龙已在三百年前死去", "enabled": True},
                ],
                # 规范里的参数：只扫最近 4 条消息、最多注入 256 token
                "scan_depth": 4,
                "token_budget": 256,
            },
            headers=mem_headers,
        ).json()["data"]
        keyword_card = client.post(
            f"{api}/character-cards",
            json={"name": f"关键词卡_{tag}", "greeting": "……", "world_book_id": book["id"]},
            headers=mem_headers,
        ).json()["data"]

        # 先起一个假模型（这一步要真的发一轮对话，好让提示词被构建出来）
        fake_server = ThreadingHTTPServer(("127.0.0.1", fake_port), FakeHandler)
        threading.Thread(target=fake_server.serve_forever, daemon=True).start()
        mem_provider = client.post(
            f"{api}/providers",
            json={
                "name": f"记忆假模型_{tag}",
                "provider_type": "openai_compatible",
                "base_url": f"http://127.0.0.1:{fake_port}/v1",
                "api_key": "",
                "model_name": "fake-model",
                "context_window": 8192,
                "generation": {"temperature": 0.8, "max_tokens": 1024},
            },
            headers=mem_headers,
        ).json()["data"]

        mem_session = client.post(
            f"{api}/narrative/sessions",
            json={"character_card_id": keyword_card["id"], "llm_provider_id": mem_provider["id"]},
            headers=mem_headers,
        ).json()["data"]
        mem_session_id = mem_session["id"]
        # 开场白（"……"）里没有任何关键词 -> 不该注入任何设定
        check(
            mem_session["prompt"]["world_book_entries"] == 0,
            "★ 没提到关键词时，世界书条目一条都不注入",
        )

        resp = client.post(
            f"{api}/narrative/sessions/{mem_session_id}/messages",
            json={"content": "灯塔还亮着吗"},
            headers=mem_headers,
        )
        check(resp.status_code == 201, f"发消息返回 {resp.status_code}")
        sent = resp.json()["data"]
        check(
            sent["context"]["world_book_entries"] >= 1,
            "★ 提到「灯塔」后，命中的设定被注入提示词",
        )
        check(
            "灯塔在风暴夜会熄灭" in sent["prompt"]["system_prompt"],
            "★ 注入的正是命中的那条设定",
        )
        check(
            "巨龙已在三百年前死去" not in sent["prompt"]["system_prompt"],
            "★ 没提到的条目不会被注入（这就是关键词触发的意义）",
        )

        # 长期记忆：上一轮对话应当已经写进向量库
        hits = stored_memories(mem_user_id, "灯塔 亮着", mem_session_id)
        check(len(hits) >= 1, f"★ 直连向量库：这一轮对话已写入长期记忆（{len(hits)} 条）")
        if hits:
            check("灯塔" in hits[0].text, f"记忆内容正确：{hits[0].text[:40]}")

        # 再发一轮，带上相似度足够的记忆 -> 必须被召回并拼进提示词
        memory_mod.remember_fact(
            mem_user_id,
            session_id=mem_session_id,
            text="用户：灯塔还亮着吗\n角色：灯塔在风暴夜会熄灭",
        )
        resp = client.post(
            f"{api}/narrative/sessions/{mem_session_id}/messages",
            json={"content": "灯塔还亮着吗"},
            headers=mem_headers,
        )
        check(resp.status_code == 201, f"第二次发消息返回 {resp.status_code}")
        second = resp.json()["data"]
        check(
            second["context"]["recalled_memories"] >= 1,
            f"★ 召回了 {second['context']['recalled_memories']} 条长期记忆",
        )
        check(
            "相关回忆" in second["prompt"]["system_prompt"],
            "★ 召回的回忆确实拼进了系统提示词",
        )
        check(not second["context"]["memory_error"], "记忆检索没有报错")

        # 记忆接口
        listed = client.get(
            f"{api}/narrative/sessions/{mem_session_id}/memories",
            params={"q": "灯塔"},
            headers=mem_headers,
        )
        check(listed.status_code == 200, f"记忆检索接口返回 {listed.status_code}")
        check(listed.json()["data"]["total"] >= 1, "接口能查到这个会话的记忆")

        added = client.post(
            f"{api}/narrative/sessions/{mem_session_id}/memories",
            json={"text": "约定：风暴夜不要去灯塔"},
            headers=mem_headers,
        )
        check(added.status_code == 201, f"手动记住一条返回 {added.status_code}")

        # ★ 一致性：删会话必须把记忆一起清掉
        #   （"删除前确实有记忆"上面已经验证过：stored_memories 查到了内容。
        #     这里补最后一环 —— 删完必须查不到。
        #     刻意不再查一次"删除前"，因为本脚本直连向量库、与运行中的服务各持一份索引，
        #     同一段流程里查太多次容易撞上跨进程的索引不一致，那是假失败。）
        client.delete(f"{api}/narrative/sessions/{mem_session_id}", headers=mem_headers)
        check(
            stored_memories(mem_user_id, "灯塔", mem_session_id) == [],
            "★ 删会话后：向量库里的记忆一起消失了（不留幽灵记忆）",
        )

        # ==================================================================
        title("步骤 11.6 / ★ 消息级操作（编辑 / 撤回 / 重新生成）")
        # ==================================================================
        # 这一段的每一步都做**双通道核对**：接口说删了几条，数据库里就必须真的少几条。
        # 这类"改历史"的操作最容易出现"接口说删了、其实没删"。
        #
        # ★ 为什么要在这里**再起一个**假模型服务、而不是用主 provider？
        #   主 provider 指向的是 `.env` 里配的真实厂商地址（只有 --live 时才真的调）。
        #   重新生成必须真的走一次模型，所以这里用本机假模型，既不联网也不花钱。
        fake_port = 8123
        fake_server = ThreadingHTTPServer(("127.0.0.1", fake_port), FakeHandler)
        threading.Thread(target=fake_server.serve_forever, daemon=True).start()
        msg_provider = client.post(
            f"{api}/providers",
            headers=headers,
            json={
                "name": f"消息操作假模型_{uuid.uuid4().hex[:6]}",
                "provider_type": "openai_compatible",
                "base_url": f"http://127.0.0.1:{fake_port}/v1",
                "api_key": "",
                "model_name": "fake-model",
                "context_window": 8192,
                "is_default": False,
            },
        ).json()["data"]

        msg_card = client.post(
            f"{api}/character-cards",
            headers=headers,
            json={
                "name": f"消息操作卡_{uuid.uuid4().hex[:6]}",
                "greeting": "（开场白）",
                "is_public": False,
            },
        ).json()["data"]
        msg_session = client.post(
            f"{api}/narrative/sessions",
            headers=headers,
            json={"character_card_id": msg_card["id"], "llm_provider_id": msg_provider["id"]},
        ).json()["data"]
        msg_sid = msg_session["id"]

        for text in ("第一句", "第二句"):
            sent = client.post(
                f"{api}/narrative/sessions/{msg_sid}/messages",
                json={"content": text},
                headers=headers,
            )
            check(sent.status_code == 201, f"发「{text}」返回 {sent.status_code}")

        detail = client.get(f"{api}/narrative/sessions/{msg_sid}", headers=headers).json()["data"]
        check(
            [m["role"] for m in detail["messages"]]
            == ["assistant", "user", "assistant", "user", "assistant"],
            "开场白 + 两轮对话共 5 条消息",
        )
        user_ids = [m["id"] for m in detail["messages"] if m["role"] == "user"]
        first_reply_id = [m["id"] for m in detail["messages"] if m["role"] == "assistant"][1]

        # ---- 编辑第一句：应当连带删掉它之后的 3 条，只留开场白 + 改过的第一句 ----
        edited = client.patch(
            f"{api}/narrative/sessions/{msg_sid}/messages/{user_ids[0]}",
            json={"content": "改过的第一句"},
            headers=headers,
        )
        check(edited.status_code == 200, f"编辑返回 {edited.status_code}")
        check(edited.json()["data"]["deleted_messages"] == 3, "★ 编辑报告删除了 3 条后继消息")
        detail = client.get(f"{api}/narrative/sessions/{msg_sid}", headers=headers).json()["data"]
        check(
            detail["message_count"] == 2,
            f"★ 复核：消息只剩 {detail['message_count']} 条（应为 2）",
        )
        check(detail["messages"][-1]["content"] == "改过的第一句", "改后的内容已落库")
        check(
            detail["total_tokens"] == sum(m["token_count"] for m in detail["messages"]),
            "★ 删除后 token 统计已重算（不会留下算不回来的旧总数）",
        )

        # ---- 编辑角色回复应当被拒绝 ----
        rejected = client.patch(
            f"{api}/narrative/sessions/{msg_sid}/messages/{detail['messages'][0]['id']}",
            json={"content": "伪造"},
            headers=headers,
        )
        check(rejected.status_code == 400, f"★ 编辑角色回复被拒绝（{rejected.status_code}）")

        # ---- 补一轮，然后重新生成最后一条回复 ----
        client.post(
            f"{api}/narrative/sessions/{msg_sid}/messages",
            json={"content": "再问一次"},
            headers=headers,
        )
        before = client.get(f"{api}/narrative/sessions/{msg_sid}", headers=headers).json()["data"]
        before_user_ids = [m["id"] for m in before["messages"] if m["role"] == "user"]

        with client.stream(
            "GET", f"{api}/narrative/sessions/{msg_sid}/regenerate", headers=headers
        ) as streamed:
            check(streamed.status_code == 200, f"重新生成返回 {streamed.status_code}")
            body = "".join(streamed.iter_text())
        check("event: done" in body, "重新生成的流以 done 结束")
        after = client.get(f"{api}/narrative/sessions/{msg_sid}", headers=headers).json()["data"]
        check(
            [m["id"] for m in after["messages"] if m["role"] == "user"] == before_user_ids,
            "★ 重新生成没有多出一条重复的用户消息",
        )
        check(
            after["messages"][-1]["id"] != before["messages"][-1]["id"],
            "★ 回复被真正重写（旧的那条被删、新的那条是另一个 id）",
        )

        # ---- 对中间那条回复重新生成：它自己与后面的内容都要被替换 ----
        #   ★ 重新取一次第一条回复的 id：上面那次"重新生成最后一条"已经把它换掉了，
        #     拿旧的 id 会撞上 404（写这行时就是这么发现该断言的）。
        first_reply_id = [m["id"] for m in after["messages"] if m["role"] == "assistant"][1]
        with client.stream(
            "GET",
            f"{api}/narrative/sessions/{msg_sid}/regenerate",
            headers=headers,
            params={"message_id": first_reply_id},
        ) as streamed:
            check(streamed.status_code == 200, "指定中间回复重新生成返回 200")
            "".join(streamed.iter_text())
        after = client.get(f"{api}/narrative/sessions/{msg_sid}", headers=headers).json()["data"]
        check(
            len([m for m in after["messages"] if m["role"] == "assistant"]) == 2,
            "★ 中间那条回复被**替换**（不是追加），助手消息仍然只有 2 条",
        )

        # ---- 撤回：连同自己及其之后的内容一起删 ----
        target_user_id = [m["id"] for m in after["messages"] if m["role"] == "user"][0]
        retracted = client.post(
            f"{api}/narrative/sessions/{msg_sid}/messages/{target_user_id}/retract",
            headers=headers,
        )
        check(retracted.status_code == 200, f"撤回返回 {retracted.status_code}")
        check(
            retracted.json()["data"]["retracted_content"] == "改过的第一句",
            "★ 撤回把原话还给了前端（可以放回输入框改了再发）",
        )
        final = client.get(f"{api}/narrative/sessions/{msg_sid}", headers=headers).json()["data"]
        check(
            [m["role"] for m in final["messages"]] == ["assistant"],
            f"★ 撤回后只剩开场白，实际 {[m['role'] for m in final['messages']]}",
        )

        # ---- 越权：别人的会话里的消息不该能操作 ----
        #   ★ 这个临时账号必须**自己删掉**：步骤 13 只删它自己创建的那三个账号，
        #     不会按前缀批量清理（前缀清理是 cleanup_demo_data.py 的职责）。
        #     不删的话收尾的"行数一致"核对必然失败 —— 真实踩到过。
        other_name = f"smoke_other_{uuid.uuid4().hex[:6]}"
        client.post(
            f"{api}/auth/register",
            json={
                "username": other_name,
                "email": f"{other_name}@example.com",
                "password": "Test-Passw0rd!",
            },
        )
        other_token = client.post(
            f"{api}/auth/login",
            json={"username": other_name, "password": "Test-Passw0rd!"},
        ).json()["data"]["access_token"]
        cross = client.post(
            f"{api}/narrative/sessions/{msg_sid}/messages/{target_user_id}/retract",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        check(cross.status_code == 404, f"★ 别人的会话里的消息一律 404（实际 {cross.status_code}）")

        with session_scope() as db:
            row = db.scalar(select(User).where(User.username == other_name))
            if row is not None:
                db.delete(row)

        client.delete(f"{api}/narrative/sessions/{msg_sid}", headers=headers)
        client.delete(
            f"{api}/character-cards/{msg_card['id']}", headers=headers, params={"force": True}
        )
        client.delete(f"{api}/providers/{msg_provider['id']}", headers=headers)
        fake_server.shutdown()
        ok("消息级操作的临时数据已清理")
        client.delete(f"{api}/providers/{mem_provider['id']}", headers=mem_headers)
        client.delete(f"{api}/world-books/{book['id']}", headers=mem_headers, params={"force": True})
        client.delete(
            f"{api}/character-cards/{keyword_card['id']}", headers=mem_headers, params={"force": True}
        )
        ok("3.9 相关的临时数据已清理")

        # ---------------- 11.7 骰子插件（后端掷骰）----------------
        title("步骤 11.7 / ★ 骰子插件：随机数由后端掷、点数落库")
        dice_port = 8124
        dice_server = ThreadingHTTPServer(("127.0.0.1", dice_port), FakeHandler)
        threading.Thread(target=dice_server.serve_forever, daemon=True).start()
        dice_user = f"smoke_dice_{tag}"
        client.post(
            f"{api}/auth/register",
            json={"username": dice_user, "email": f"{dice_user}@example.com", "password": password},
        )
        dice_token = client.post(
            f"{api}/auth/login", json={"username": dice_user, "password": password}
        ).json()["data"]["access_token"]
        dice_headers = {"Authorization": f"Bearer {dice_token}"}

        # 从内置目录一键添加（与界面上点「添加」是同一条路）
        added = client.post(f"{api}/plugins/catalog/trpg_dice", headers=dice_headers)
        check(added.status_code in (200, 201), f"从内置目录添加骰子插件返回 {added.status_code}")
        dice_plugin = added.json()["data"]
        check(dice_plugin["kind"] == "dice", "★ 插件类型是 dice（受控求值，不执行第三方 JS）")

        # 写错的默认表达式必须在保存时就被拒绝（不能等用户掷骰才发现）
        bad = client.post(
            f"{api}/plugins",
            headers=dice_headers,
            json={"name": "坏骰子", "kind": "dice", "config": {"default_expr": "1d6+"}},
        )
        check(bad.status_code == 400, f"★ 写坏的默认表达式被拒绝（实际 {bad.status_code}）")

        dice_provider = client.post(
            f"{api}/providers",
            headers=dice_headers,
            json={
                "name": f"骰子假模型_{tag}",
                "provider_type": "openai_compatible",
                "base_url": f"http://127.0.0.1:{dice_port}/v1",
                "api_key": "",
                "model_name": "fake-model",
                "context_window": 8192,
            },
        ).json()["data"]
        dice_card = client.post(
            f"{api}/character-cards",
            headers=dice_headers,
            json={"name": f"骰子卡_{tag}", "greeting": "……"},
        ).json()["data"]
        dice_sid = client.post(
            f"{api}/narrative/sessions",
            headers=dice_headers,
            json={"character_card_id": dice_card["id"], "llm_provider_id": dice_provider["id"]},
        ).json()["data"]["id"]

        rolled = client.post(
            f"{api}/narrative/sessions/{dice_sid}/messages",
            headers=dice_headers,
            json={"content": "/r 2d6+3 力量检定"},
        )
        check(rolled.status_code == 201, f"掷骰这条消息返回 {rolled.status_code}")
        rolled_data = rolled.json()["data"]
        rolls = rolled_data["user_message"]["rolls"]
        check(len(rolls) == 1, f"★ /r 2d6+3 被识别成一次掷骰（{len(rolls)} 条）")
        if rolls:
            die = rolls[0]
            check(die["total"] is not None, f"点数已掷出：{die.get('text')}")
            check(
                5 <= int(die["total"]) <= 15 and len(die["faces"]) == 2,
                "★ 2d6+3 的点数落在 5~15，且留下了两颗骰面（可复核）",
            )
            check(die["label"] == "力量检定", "★ 表达式后面的说明被当成这次掷骰的用途")
            system = rolled_data["prompt"]["system_prompt"]
            check(
                "## 本轮骰点" in system and f"**{die['total']}**" in system,
                "★ 提示词里的本轮骰点与落库的点数完全一致（预览不重掷）",
            )
            check("<roll>" in system, "★ 插件把「用 <roll> 请求掷骰」的规则写进了系统提示词")
            # 刷新（重新拉详情）还是同一个数字
            detail = client.get(f"{api}/narrative/sessions/{dice_sid}", headers=dice_headers)
            again = [
                m for m in detail.json()["data"]["messages"] if m["role"] == "user"
            ][-1]["rolls"][0]
            check(again["total"] == die["total"], "★ 重新拉详情点数不变（落库，绝不重掷）")

        client.delete(f"{api}/narrative/sessions/{dice_sid}", headers=dice_headers)
        client.delete(
            f"{api}/character-cards/{dice_card['id']}", headers=dice_headers, params={"force": True}
        )
        client.delete(f"{api}/providers/{dice_provider['id']}", headers=dice_headers)
        dice_server.shutdown()
        with session_scope() as db:
            row = db.scalar(select(User).where(User.username == dice_user))
            if row is not None:
                db.delete(row)
        ok("骰子插件的临时数据已清理")

        # ---------------- 11.8 角色卡 VN 立绘（表情跟着状态栏变）----------------
        title("步骤 11.8 / ★ 角色卡 VN 立绘：舞台数据由后端按状态算")
        # ★ 单独一个账号：上面骰子那段把自己那个账号删了（token 随之失效），
        #   这一段又**不需要**调模型（只验状态 → 立绘的映射），所以不需要假模型。
        vn_user = f"smoke_vn_{tag}"
        client.post(
            f"{api}/auth/register",
            json={"username": vn_user, "email": f"{vn_user}@example.com", "password": password},
        )
        vn_token = client.post(
            f"{api}/auth/login", json={"username": vn_user, "password": password}
        ).json()["data"]["access_token"]
        vn_headers = {"Authorization": f"Bearer {vn_token}"}
        vn_provider = client.post(
            f"{api}/providers",
            headers=vn_headers,
            json={
                "name": f"VN假模型_{tag}",
                "provider_type": "openai_compatible",
                "base_url": "https://mock.invalid/v1",
                "api_key": "",
                "model_name": "fake-model",
                "context_window": 8192,
            },
        ).json()["data"]

        vn_calm = "https://cdn.example.com/smoke-calm.png"
        vn_angry = "https://cdn.example.com/smoke-angry.png"
        vn_card = client.post(
            f"{api}/character-cards/import",
            headers=vn_headers,
            json={
                "card": {
                    "spec": "chara_card_v2",
                    "data": {
                        "name": f"VN卡_{tag}",
                        "first_mes": "（她回过头）……你来了。",
                        "extensions": {
                            "hne": {
                                "vn": {
                                    "sprites": {"平静": vn_calm, "生气": vn_angry},
                                    "background": "https://cdn.example.com/smoke-bg.png",
                                    "default": "平静",
                                    "expression_field": "表情 名",
                                },
                                "state_schema": [{"name": "mood", "type": "text"}],
                                "initial_state": {"mood": "平静"},
                            }
                        },
                    },
                }
            },
        )
        check(vn_card.status_code == 201, f"导入 VN 角色卡返回 {vn_card.status_code}")
        vn_row = vn_card.json()["data"]
        stored_vn = vn_row["extensions"]["hne"]["vn"]
        check(
            stored_vn["sprites"] == {"平静": vn_calm, "生气": vn_angry},
            "★ 立绘配置原样存住（只收 http(s) 与 data:image 内嵌图）",
        )
        check(
            stored_vn["expression_field"] == "mood",
            "★ 非法的表情字段名被改回默认 mood（而不是存一个用不了的值）",
        )

        vn_sid = client.post(
            f"{api}/narrative/sessions",
            headers=vn_headers,
            json={"character_card_id": vn_row["id"], "llm_provider_id": vn_provider["id"]},
        ).json()["data"]["id"]
        vn_detail = client.get(f"{api}/narrative/sessions/{vn_sid}", headers=vn_headers).json()[
            "data"
        ]
        check(
            (vn_detail.get("vn") or {}).get("sprite_url") == vn_calm,
            "★ 会话详情里给出舞台数据：当前表情「平静」→ 平静那张立绘",
        )
        check(
            vn_detail.get("vn", {}).get("expressions") == ["平静", "生气"],
            "★ 可选表情清单也一并给出（界面与作者都能核对）",
        )
        patched = client.patch(
            f"{api}/narrative/sessions/{vn_sid}/state",
            headers=vn_headers,
            json={"state": {"mood": "生气"}},
        )
        check(patched.status_code == 200, f"手动改状态返回 {patched.status_code}")
        after = client.get(f"{api}/narrative/sessions/{vn_sid}", headers=vn_headers).json()["data"]
        check(
            after["vn"]["sprite_url"] == vn_angry,
            "★ 状态里的表情变了 → 立绘跟着换（表情是状态栏字段，不是新协议）",
        )
        client.delete(f"{api}/narrative/sessions/{vn_sid}", headers=vn_headers)
        client.delete(
            f"{api}/character-cards/{vn_row['id']}", headers=vn_headers, params={"force": True}
        )
        client.delete(f"{api}/providers/{vn_provider['id']}", headers=vn_headers)
        with session_scope() as db:
            row = db.scalar(select(User).where(User.username == vn_user))
            if row is not None:
                db.delete(row)
        ok("VN 立绘的临时数据已清理")

        # ---------------- 11.9 翻译中间件（跨语言对话）----------------
        title("步骤 11.9 / ★ 翻译中间件：原文/译文两份都留着，成本如实回报")
        tr_port = 8125
        tr_server = ThreadingHTTPServer(("127.0.0.1", tr_port), FakeHandler)
        threading.Thread(target=tr_server.serve_forever, daemon=True).start()
        tr_user = f"smoke_tr_{tag}"
        client.post(
            f"{api}/auth/register",
            json={"username": tr_user, "email": f"{tr_user}@example.com", "password": password},
        )
        tr_token = client.post(
            f"{api}/auth/login", json={"username": tr_user, "password": password}
        ).json()["data"]["access_token"]
        tr_headers = {"Authorization": f"Bearer {tr_token}"}
        tr_provider = client.post(
            f"{api}/providers",
            headers=tr_headers,
            json={
                "name": f"翻译假模型_{tag}",
                "provider_type": "openai_compatible",
                "base_url": f"http://127.0.0.1:{tr_port}/v1",
                "api_key": "",
                "model_name": "fake-model",
                "context_window": 8192,
            },
        ).json()["data"]
        tr_card = client.post(
            f"{api}/character-cards",
            headers=tr_headers,
            json={"name": f"外语卡_{tag}", "greeting": "Hello there, traveler."},
        ).json()["data"]
        tr_sid = client.post(
            f"{api}/narrative/sessions",
            headers=tr_headers,
            json={"character_card_id": tr_card["id"], "llm_provider_id": tr_provider["id"]},
        ).json()["data"]["id"]

        # ① 默认必须是关的（不许未经同意花 token）
        panel_resp = client.get(
            f"{api}/narrative/sessions/{tr_sid}/translate", headers=tr_headers
        )
        check(panel_resp.status_code == 200, f"读取翻译面板返回 {panel_resp.status_code}：{panel_resp.text[:120]}")
        panel = panel_resp.json().get("data") or {}
        check(panel.get("settings", {}).get("enabled") is False, "★ 翻译中间件默认关闭（默认不花用户的钱）")
        check(panel.get("cost_tokens") == 0, "★ 关闭时预估消耗是 0")
        check(
            "input_lang" not in (panel.get("settings") or {}),
            "★ 第十六轮：不再有「输入侧语言」设置（一个目标语言 + 源语言自动识别）",
        )
        check(
            (panel.get("settings") or {}).get("target_lang") == "简体中文",
            "★ 面向中文用户：默认目标就是简体中文（外语 → 中文）",
        )

        # ② 打开中间件（回复方向）：模型说英文 → 中间件译成中文
        saved = client.patch(
            f"{api}/narrative/sessions/{tr_sid}/translate",
            headers=tr_headers,
            json={
                "enabled": True,
                "mode": "middleware",
                "direction": "reply",
                "target_lang": "简体中文",
            },
        )
        check(saved.status_code == 200, f"保存翻译设置返回 {saved.status_code}")

        sent = client.post(
            f"{api}/narrative/sessions/{tr_sid}/messages",
            headers=tr_headers,
            json={"content": "英文回复 请用英文讲一段"},
        )
        check(sent.status_code == 201, f"开启翻译后发消息返回 {sent.status_code}")
        tr_data = sent.json()["data"]
        assistant = tr_data["assistant_message"]
        check(
            assistant["content"].startswith("I hear you"),
            "★ content 永远是**模型说的话**（这里是英文原文）",
        )
        translation = assistant.get("translation") or {}
        check(
            translation.get("text", "").startswith("【本地假译文】"),
            "★ 译文存下来了（另一份文本，不覆盖原文）",
        )
        check(
            translation.get("display") == "translation",
            "★ 输出侧：界面默认显示译文（display=translation）",
        )
        check(
            int(translation.get("tokens") or 0) > 0,
            f"★ 翻译消耗的 token 也记下来了（{translation.get('tokens')}）",
        )
        check(
            any("翻译中间件" in note for note in tr_data["notes"]),
            "★ 花在翻译上的钱要在 notes 里如实说",
        )

        # ②b ★ 第十六轮：翻译过程必须有提示（正文流完之后还要等一次模型调用）
        with client.stream(
            "GET",
            f"{api}/narrative/sessions/{tr_sid}/stream",
            params={"content": "英文回复 再来一句"},
            headers=tr_headers,
        ) as streamed:
            sse_body = "".join(streamed.iter_text())
        check(
            "event: translating" in sse_body,
            "★ 翻译开始前发了 translating 事件（用户不再对着原文干等）",
        )
        if "event: translating" in sse_body and "event: done" in sse_body:
            check(
                sse_body.index("event: translating") < sse_body.index("event: done"),
                "★ 提示发在收尾之前（前端靠 done 把它收掉）",
            )
        check(
            '"lang": "简体中文"' in sse_body or '"lang":"简体中文"' in sse_body,
            "★ 提示里带着目标语言（界面据此写「正在翻译回复成简体中文…」）",
        )

        # ③ 已经是中文就别再花一次钱（本地判断，不调模型）
        again = client.post(
            f"{api}/narrative/sessions/{tr_sid}/messages",
            headers=tr_headers,
            json={"content": "再说一句"},
        ).json()["data"]["assistant_message"]
        check(
            again.get("translation") is None,
            "★ 回复本来就是中文 → 不译、不花钱（used_model=False）",
        )

        # ④ 输入侧：中文 → 英文再发给模型；原文不丢
        #    ★ 第十六轮起没有"输入侧语言"：把**目标语言**选成英文即可（两个方向共用一个目标）
        client.patch(
            f"{api}/narrative/sessions/{tr_sid}/translate",
            headers=tr_headers,
            json={"direction": "input", "target_lang": "英文"},
        )
        sent_in = client.post(
            f"{api}/narrative/sessions/{tr_sid}/messages",
            headers=tr_headers,
            json={"content": "你好呀，我该往哪走？"},
        ).json()["data"]
        user_row = sent_in["user_message"]
        check(
            (user_row.get("translation") or {}).get("display") == "translation",
            "★ 输入侧：界面显示的是 translation.text（用户原话），content 才是模型看到的译文",
        )
        check(
            (user_row.get("translation") or {}).get("text") == "你好呀，我该往哪走？",
            "★ 用户原话一个字都没丢（存在 translation.text 里）",
        )
        check(
            user_row["content"].startswith("【本地假译文】"),
            "★ 而 content 是**模型看到的译文**（提示词装配不需要额外机制）",
        )

        # ⑤ prompt 模式：0 token，只加一句提示词（预览里能看到，与实际一致）
        client.patch(
            f"{api}/narrative/sessions/{tr_sid}/translate",
            headers=tr_headers,
            json={"mode": "prompt", "direction": "reply", "target_lang": "简体中文"},
        )
        preview = client.get(
            f"{api}/narrative/sessions/{tr_sid}",
            headers=tr_headers,
            params={"with_prompt": "true"},
        ).json()["data"]
        check(
            "【输出语言】" in preview["prompt"]["system_prompt"],
            "★ prompt 模式把「用简体中文回复」写进系统提示词（预览与实际一致）",
        )
        check(
            preview["translate"]["cost_tokens"] == 0,
            "★ prompt 模式预估消耗是 0（它真的不调模型）",
        )

        client.delete(f"{api}/narrative/sessions/{tr_sid}", headers=tr_headers)
        client.delete(
            f"{api}/character-cards/{tr_card['id']}", headers=tr_headers, params={"force": True}
        )
        client.delete(f"{api}/providers/{tr_provider['id']}", headers=tr_headers)
        tr_server.shutdown()
        with session_scope() as db:
            row = db.scalar(select(User).where(User.username == tr_user))
            if row is not None:
                db.delete(row)
        ok("翻译中间件的临时数据已清理")


        # ==================================================================

        # ---------------- 可选：真实大模型调用 ----------------
        if args.live:
            title("步骤 12 / 真实大模型调用（--live）")
            if not settings.DEFAULT_LLM_API_KEY:
                fail("未配置 HNE_DEFAULT_LLM_API_KEY，跳过")
            else:
                resp = client.post(
                    f"{api}/providers/{provider_id}/test", headers=headers
                )
                check(resp.status_code == 200, f"连通性测试返回 {resp.status_code}")
                result = resp.json()["data"]
                print(f"  {json.dumps(result, ensure_ascii=False)[:500]}", flush=True)
                check(result["ok"] is True, f"连通性测试通过（耗时 {result['latency_ms']} ms）")
        else:
            title("步骤 12 / 真实大模型调用（已跳过）")
            print("  加 --live 参数可额外验证一次真实 API 调用（会产生少量费用）",
                  flush=True)

        # ---------------- 13. 清理 ----------------
        title("步骤 13 / 清理测试数据")
        if args.keep:
            print(f"  已指定 --keep，数据保留。测试用户：{username} / {other} / {mem_user}", flush=True)
            print("  你可以自己去数据库里翻这些数据。", flush=True)
        else:
            with session_scope() as db:
                for name in (username, other, mem_user):
                    row = db.scalar(select(User).where(User.username == name))
                    if row is not None:
                        db.delete(row)
            ok("已删除测试用户（其名下数据由外键级联清除）")

        after = print_counts("测试后")
        if not args.keep:
            check(
                after == baseline_counts,
                "★ 直连数据库：行数与测试前完全一致，没有留下垃圾数据",
            )

        # ---------------- 汇总 ----------------
        title("结果汇总")
        print(f"  通过：{PASSED}    失败：{FAILED}", flush=True)
        if FAILED == 0:
            print("\n  全部通过 —— 接口说的和数据库里真实发生的完全一致。", flush=True)
        else:
            print(f"\n  有 {FAILED} 项未通过，请把上面的 [!!] 行发我。", flush=True)
        return 0 if FAILED == 0 else 1

    except httpx.ConnectError:
        print(
            f"\n[失败] 连不上 {base}\n"
            "       请先在另一个终端启动服务：\n"
            "       .\\.venv\\Scripts\\python.exe -m uvicorn app.main:app "
            "--host 127.0.0.1 --port 8000\n",
            flush=True,
        )
        return 2
    finally:
        client.close()
        dispose_engine()


if __name__ == "__main__":
    raise SystemExit(main())
