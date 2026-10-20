"""对话分支（平行世界 / 世界线）。

==================== 这个功能解决什么 ====================
叙事类对话里，用户常常会想"如果刚才选了另一条路会怎样"。
以前只有两个办法：
    · 撤回重来 —— 原来那条走向**永久消失**，回不去了；
    · 重新生成 —— 同上，而且新回复会替换旧回复，两条没法并存。
本模块让两条走向**同时存在**、随时切换，各自独立继续往下走。

==================== ★ 核心设计：一条世界线 = 一个会话行 ====================
这是整个功能唯一重要的决定，先说清楚为什么。

分叉时创建一个**新的 NarrativeSession 行**（复制主线到分叉点为止的消息），
两条线用 `branch_group_id` 归组。**不在同一个会话里加 branch_id 再到处过滤。**

理由是"隔离已经存在，不要重新发明它"。本项目的下面五处**本来就按
session_id 隔离**，一条线一个 session 就等于白拿了这五处隔离：

    1. 对话历史     engine.load_history()      —— 只按 session_id 取
    2. 长期记忆     memory.recall()            —— cross_session 默认 False
    3. 结构化状态   narrative_sessions.state_json        —— 挂在会话行上
    4. 剧情总结     rolling_summary / summary_*_round    —— 挂在会话行上
    5. 撤回/重生成  sessions.delete_messages_from()      —— 按 session_id 过滤

反过来，如果在**同一个会话**里加 branch_id 再逐处过滤，那么上面每一处
都要记得加上 `AND branch_id = ?`：漏改任何一处，就会出现用户最担心的那种
事故 —— **在第二条线说话，第一条线跟着变**（历史串台 / 记忆串台 /
状态串台 / 总结串台）。这种"漏一处就是一个隐藏 bug"的设计在这里不能接受。

代价只是多一行会话记录（外加复制到分叉点为止的消息），换来的是
**结构上不可能串台**。非常划算。

==================== 两条线的语义（用户拍板）====================
    · 只支持**两条**线：branch_index = 0（主线）/ 1（平行线）
    · 分叉是**显式**的：用户点「🌿 分支」按钮，模型另写一条不同走向
    · 分叉后两条线**各自独立推进**，互不影响；发送时只有当前线花 token

==================== 与"重新生成"的区别（别把两者混了）====================
    重新生成：**替换**当前回复，旧回复被删除 —— 两条走向不能共存。
    分支    ：**保留**当前回复，把它复制进新线当分叉点，新线另写一条走向。
              于是"原来的那条"和"另一条"同时存在，这正是本功能的价值。
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, NotFoundError
from app.db.models import Message, NarrativeSession

#: 只支持两条线（用户明确要求）。这是硬上限，不是"暂时"的。
MAX_BRANCHES = 2

#: 世界线默认名（用户没改过时用）。
DEFAULT_LABELS = {0: "主线", 1: "平行线"}


def label_of(session: Any) -> str:
    """这条世界线的显示名（用户改过就用他改的，否则用默认名）。"""
    custom = str(getattr(session, "branch_label", "") or "").strip()
    if custom:
        return custom
    index = int(getattr(session, "branch_index", 0) or 0)
    return DEFAULT_LABELS.get(index, f"世界线 {index + 1}")


def group_id_of(session: Any) -> int | None:
    """这条会话属于哪个世界线组。

    ★ 兼容没有 branch_group_id 的旧会话（迁移前建的，列是 NULL）：
      把它们当成"只有自己一条线的组" —— 组的身份就取它自己的 id。
      这样"是否在分支里"的判断在所有会话上都成立，不需要到处写 None 分支。
    """
    raw = getattr(session, "branch_group_id", None)
    if raw:
        return int(raw)
    return int(getattr(session, "id", 0) or 0) or None


def is_branching(session: Any) -> bool:
    """这个会话是否**开启了分支模式**（用户自己选的，默认关）。

    ★ 关闭时全站行为与加这个功能之前完全一致：没有分叉入口、没有切换器，
      发送消息也不会多生成任何东西。这是"默认不影响现有用户"的保证。
    """
    return bool(getattr(session, "branch_enabled", False))


def group_sessions(db: Session, session: NarrativeSession) -> list[NarrativeSession]:
    """取同一世界线组的全部会话（按 branch_index 升序）。

    ★ 为什么要按组取而不是"按 branch_group_id 查"：
      旧会话（branch_group_id 为 NULL）也要能出现在组里 —— 它们自己一组。
      这里用 group_id_of 算出的组身份去比对，两种会话走同一条路径。
    """
    gid = group_id_of(session)
    if gid is None:
        return [session]
    rows = list(
        db.scalars(
            select(NarrativeSession).where(
                NarrativeSession.user_id == session.user_id,
                NarrativeSession.id == gid,
            )
        ).all()
    )
    # 组内其它线：branch_group_id 指向 gid（不含 gid 自己）
    others = list(
        db.scalars(
            select(NarrativeSession).where(
                NarrativeSession.user_id == session.user_id,
                NarrativeSession.branch_group_id == gid,
            )
        ).all()
    )
    merged: dict[int, NarrativeSession] = {row.id: row for row in rows}
    for row in others:
        merged[row.id] = row
    return sorted(merged.values(), key=lambda r: (int(getattr(r, "branch_index", 0) or 0), r.id))



def serialize_group(db: Session, session: NarrativeSession) -> dict[str, Any]:
    """给界面用的世界线组信息（切换器靠它渲染）。

    形状：
        {"enabled": bool, "group_id": int, "max": 2,
         "current_id": int, "can_branch": bool, "has_sibling": bool,
         "lines": [{"id", "index", "label", "is_current", "message_count",
                    "round_count", "last_active_at", "status", "archived"}]}

    ★ `round_count`（一问一答算一轮）是**界面显示用**的那个数：用户明确要求
      别写"N 条消息"。口径与 `sessions.round_count_map` 完全一致（同一个函数）。
    """
    # 延迟导入：sessions 已经 import 了本模块，顶层互相 import 会成环。
    from app.narrative.sessions import round_count_map

    gid = group_id_of(session)
    lines: list[dict[str, Any]] = []
    rows = group_sessions(db, session)
    rounds = round_count_map(db, [row.id for row in rows])
    for row in rows:
        lines.append(
            {
                "id": row.id,
                "index": int(getattr(row, "branch_index", 0) or 0),
                "label": label_of(row),
                "is_current": row.id == session.id,
                "message_count": int(row.message_count or 0),
                "round_count": int(rounds.get(row.id, 0)),
                "last_active_at": row.last_active_at,
                "status": row.status,
                # 归档的线要能如实说明"它被归档了"（点过去会先自动恢复）
                "archived": str(getattr(row, "status", "active")) == "archived",
                # 这条线末尾是不是"一句还没被回复的用户消息"：
                #   是的话界面会给出「让 AI 写出这条走向」的重试入口。
                #   为什么需要它：分叉后那次生成如果失败/被中断，这条线会停在
                #   用户那句话上，而错误气泡会被随后的刷新抹掉 —— 用户看到的是
                #   "点了分支，AI 却没回复"，且**没有任何办法再触发一次**。
                "pending_user": _pending_user(db, row),
            }
        )
    # ★ 分支功能**始终可用**，不再需要先"开启一个模式"（用户 2026-10-13 拍板）：
    #   真正的"防误点"是点「🌿 分支」之后的**确认弹窗**（取消就什么都不做、0 token），
    #   而不是一个提前拨的开关 —— 开关开着的时候按钮照样能点，它防不了误点；
    #   反倒会制造"关着的时候入口看不见、用户以为功能没了"的困惑。
    #   所以 `enabled` 恒为 True（保留字段是为了兼容前端与旧数据），
    #   能不能分叉只看**组内是否已满两条线**。
    return {
        "enabled": True,
        "group_id": gid,
        "max": MAX_BRANCHES,
        # 只有"还没到两条线"时才给分叉入口（用户拍板：只两条）
        "can_branch": len(rows) < MAX_BRANCHES,
        "has_sibling": len(rows) > 1,
        "current_id": session.id,
        "lines": lines,
    }


def _pending_user(db: Session, session: NarrativeSession) -> bool:
    """这条线的最后一条消息是不是"还没被回复的用户消息"。

    判据只看**最后一条**：它是 user 就说明这一轮还没答完（正常生成中、
    或者生成失败/被中断了）。界面据此给出重试入口。
    """
    last_role = db.scalar(
        select(Message.role)
        .where(Message.session_id == session.id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    return str(last_role or "") == "user"


def enable(db: Session, user_id: int, session_id: int, enabled: bool = True) -> NarrativeSession:
    """设置这条会话的"用过世界线"标记。

    ★ 2026-10-13 起**这个开关不再影响功能可用性** —— 分支始终可用
      （用户拍板：防误点靠确认弹窗，不靠提前拨的开关；开关反而会制造
      "关着时入口消失、用户以为功能没了"）。接口保留只是为了兼容与将来的偏好设置。

    ★ 关闭**不会删除**已经产生的平行线 —— 那是用户花 token 生成的真实内容，
      删掉是不可逆的损失。
    """
    session = get_one(db, user_id, session_id)
    if enabled and group_id_of(session) is not None and get_group_size(db, session) >= MAX_BRANCHES:
        # 已有两条线时再开启没有意义，但也不算错 —— 如实放行（切回来看得到两条）
        logger.debug("开启分支模式时组内已有 {} 条线 | session_id={}", MAX_BRANCHES, session_id)
    session.branch_enabled = bool(enabled)
    if session.branch_group_id is None:
        # 把自己登记成组的起点，这样"组身份"在数据里也是显式的（不只靠 id 推断）
        session.branch_group_id = session.id
    db.commit()
    db.refresh(session)
    logger.info(
        "{}对话分支模式 | user_id={} session_id={} group_id={}",
        "开启" if enabled else "关闭",
        user_id,
        session_id,
        session.branch_group_id,
    )
    return session


def get_group_size(db: Session, session: NarrativeSession) -> int:
    """组内共有几条世界线。"""
    return len(group_sessions(db, session))


def get_one(db: Session, user_id: int, session_id: int) -> NarrativeSession:
    """取会话（校验归属），找不到就 404。

    ★ 复用 sessions.get_owned_session 的判据，但这里刻意不 import 它 ——
      sessions.py 已经 import 了本模块（为了在序列化里带上世界线信息），
      反向 import 会形成循环。两处判据都很短，重复的代价小于循环依赖。
    """
    row = db.scalar(
        select(NarrativeSession).where(
            NarrativeSession.user_id == user_id,
            NarrativeSession.id == session_id,
        )
    )
    if row is None:
        raise NotFoundError("会话不存在", detail={"session_id": session_id})
    return row


def _copy_message(row: Message, *, session_id: int) -> Message:
    """把一条消息**逐列**复制到新会话。

    ★ 为什么逐列抄而不是 `db.add(Message(**row.__dict__))`：
      __dict__ 里带着主键 id、SQLAlchemy 的内部状态（_sa_instance_state），
      以及 relationship 缓存 —— 直接拿来构造新对象会把主键一起带过去
      （要么撞主键、要么变成"更新原消息"），是这类复制最典型的坑。
      这里显式列出所有业务列，**新增列时这里必须跟着加**（有测试盯着）。
    """
    return Message(
        session_id=session_id,
        role=row.role,
        content=row.content,
        token_count=int(row.token_count or 0),
        model_name=row.model_name,
        latency_ms=row.latency_ms,
        # ---- 下面这些 JSON 列必须原样带走，否则新线会丢状态遥测/骰点/译文 ----
        state_meta_json=getattr(row, "state_meta_json", None),
        rolls_json=getattr(row, "rolls_json", None),
        translation_json=getattr(row, "translation_json", None),
        state_raw_json=getattr(row, "state_raw_json", None),
        usage_json=getattr(row, "usage_json", None),
    )


def fork(
    db: Session,
    user_id: int,
    session_id: int,
    *,
    after_message_id: int | None = None,
    label: str | None = None,
) -> tuple[NarrativeSession, str]:
    """从某条回复分叉出一条**新的世界线**，返回 (新线, 记忆迁移说明)。

    ★ 分叉语义（这里有个容易做错的地方，先说清楚）：
      用户在**某条角色回复**上点「分支」，要的是"这一轮回合换一个走向"。
      所以新线复制到**那条回复之前**（含它前面那条用户消息），
      **刻意不带那条回复本身** —— 带了的话新线里已经有同一轮的回复了，
      接下来就无从"另写一条走向"（会变成同一轮出现两条回复）。

      于是复制完的新线末尾是一条 user 消息，接下来用**现成的**
      「重新生成」通路（`/regenerate` 不传 message_id = 以最后一条用户消息为锚点）
      在它上面生成另一条走向：
        · 复用已有的流式/状态/骰点/翻译/记忆写入全套逻辑，不另写一条生成路径；
        · 两条线各自的生成都落在各自会话里，天然不串。

    复制的内容：到分叉点为止的全部消息（含各 JSON 元数据列）+ 会话级数据
        （状态、状态栏格式、滚动总结及覆盖区间、锚点、翻译/总结设置）。
    """
    source = get_one(db, user_id, session_id)

    # ★ 这里**不再**要求"先开启分支模式"（用户 2026-10-13 拍板去掉那个开关）：
    #   防误点靠的是前端点「🌿 分支」之后的确认弹窗，不是提前拨的开关。
    #   代码层面仍然只拦一件事：**已经两条线了就不能再分**。
    if get_group_size(db, source) >= MAX_BRANCHES:
        raise BadRequestError(
            f"最多只支持 {MAX_BRANCHES} 条世界线（已经用满）",
            detail={
                "session_id": session_id,
                "max": MAX_BRANCHES,
                "suggestion": "两条线已经都在了，直接点世界线切换器切过去看另一条",
            },
        )

    history = list(
        db.scalars(
            select(Message)
            .where(Message.session_id == source.id)
            .order_by(Message.id)
        ).all()
    )
    if not history:
        raise BadRequestError(
            "这个会话还没有任何消息，从开场白之后才能分叉",
            detail={"session_id": session_id},
        )

    if after_message_id is None:
        anchor = history[-1]
    else:
        anchor = next((m for m in history if m.id == int(after_message_id)), None)
        if anchor is None:
            raise BadRequestError(
                "分叉点不属于这条会话",
                detail={"session_id": session_id, "message_id": after_message_id},
            )

    # ★ 分叉点之后要生成另一条走向，所以新线末尾必须是**用户消息**：
    #     anchor 是角色回复 → 复制到它之前（不含它）
    #     anchor 是用户消息 → 连它一起复制（"这句话之后会怎样"）
    if str(anchor.role) == "user":
        prefix = [m for m in history if m.id <= anchor.id]
    else:
        prefix = [m for m in history if m.id < anchor.id]

    if not prefix or str(prefix[-1].role) != "user":
        raise BadRequestError(
            "这个位置分不出另一条走向：它前面没有你说的那句话",
            detail={
                "session_id": session_id,
                "message_id": anchor.id,
                "suggestion": "请在一条**角色回复**上点「分支」，或先发一句话",
            },
        )

    gid = group_id_of(source)
    # 新线取组内第一个空出来的序号（两条线的场景下就是 1）
    used = {int(getattr(r, "branch_index", 0) or 0) for r in group_sessions(db, source)}
    index = next((i for i in range(MAX_BRANCHES) if i not in used), MAX_BRANCHES - 1)

    twin = NarrativeSession(
        user_id=user_id,
        # ---- 与主线保持一致的设定（分支不改变"这是谁的故事"）----
        character_card_id=source.character_card_id,
        llm_provider_id=source.llm_provider_id,
        prompt_preset_id=source.prompt_preset_id,
        kind=source.kind,
        title=source.title,
        status="active",
        # ---- 会话级数据：状态 / 状态栏格式 / 总结 / 锚点 / 设置 ----
        state_json=source.state_json,
        state_schema_json=source.state_schema_json,
        rolling_summary=source.rolling_summary,
        summarized_until_message_id=source.summarized_until_message_id,
        summary_from_round=source.summary_from_round,
        summary_to_round=source.summary_to_round,
        summary_settings_json=source.summary_settings_json,
        summary_history_json=source.summary_history_json,
        memory_anchors_json=source.memory_anchors_json,
        translate_settings_json=source.translate_settings_json,
        # ---- 世界线元信息 ----
        branch_group_id=gid,
        branch_index=index,
        branch_label=(label or "").strip() or None,
        branch_enabled=True,
        branch_from_message_id=anchor.id,
        # ---- 统计：先置 0，复制完消息再重算 ----
        message_count=0,
        total_tokens=0,
        last_active_at=source.last_active_at,
    )
    db.add(twin)
    # 先 flush 拿到新线的主键，消息才能挂上去
    db.flush()

    for row in prefix:
        db.add(_copy_message(row, session_id=twin.id))

    # 主线自己也要登记进组（可能它是迁移前建的、branch_group_id 还是 NULL）
    if source.branch_group_id is None:
        source.branch_group_id = gid
    # `branch_enabled` 现在只是"这条故事用过世界线"的**标记**（不再是开关）：
    # 保留它是为了兼容旧数据与旧接口，功能可用性与它无关（见 serialize_group 的说明）。
    source.branch_enabled = True
    db.commit()
    db.refresh(twin)

    _recount(db, twin)

    # 记忆迁移的结果要能如实上报（失败也不影响分叉本身，见 memory 模块的降级约定）
    copied, memory_note = copy_memories(
        user_id=user_id, from_session_id=source.id, to_session_id=twin.id
    )
    logger.info(
        "分叉世界线 | user_id={} from={} to={} index={} 复制消息={} 复制记忆={}",
        user_id,
        source.id,
        twin.id,
        index,
        len(prefix),
        copied,
    )
    return twin, memory_note


def copy_memories(*, user_id: int, from_session_id: int, to_session_id: int) -> tuple[int, str]:
    """把主线的长期记忆复制给新线（按 session_id 改写元数据）。

    ★ 为什么这一步需要存在：
      向量记忆按 session_id 隔离（这正是"两条线不串台"的保证之一）。
      但新线如果**完全没有**分叉点之前的记忆，就会出现"刚分叉完它就把刚才
      说过的事忘了"——两条线的处境在分叉那一刻其实是一样的，记忆也该一样。
      所以分叉时把旧记忆复制一份挂到新线名下；此后两条线各记各的。

    ★ 为什么不用"让新线跨会话召回"来偷懒：
      那等于给新线开一个"能看见别的世界线"的口子 —— 之后主线的情节会漏进来，
      正是用户明确要求避免的。

    返回 (复制条数, 说明)。**任何失败都只返回说明，不抛异常**：
    分叉本身已经成功落库了，不该因为向量库的问题让用户以为分叉失败。
    """
    from app.db import chroma

    try:
        if not chroma.has_user_collection(user_id):
            return 0, "这个账号还没有长期记忆，新世界线从零开始记。"
        collection = chroma.get_user_collection(user_id, create=False)
        got = collection.get(
            where={"session_id": int(from_session_id)},
            include=["documents", "metadatas"],
        )
        # ★ 三个列表必须一起取、按下标对齐：chroma 的 get() 返回 ids /
        #   documents / metadatas 三个**等长**列表，缺一个就没法把"旧记忆 ID"
        #   与新记录对应上（第一版就漏了 ids，导致索引错位）。
        ids = list(got.get("ids") or [])
        documents = list(got.get("documents") or [])
        metadatas = list(got.get("metadatas") or [])
        if not documents:
            return 0, "分叉点之前的这段剧情还没有写入长期记忆，新世界线从零开始记。"

        records: list[chroma.MemoryRecord] = []
        for offset, (text, meta) in enumerate(zip(documents, metadatas)):
            meta = dict(meta or {})
            old_id = str(ids[offset]) if offset < len(ids) and ids[offset] else ""
            # 新记忆 ID 必须换一个：ChromaDB 的 upsert 按 ID 覆盖，
            # 沿用旧 ID 会把主线那条记忆**原地改成新线的**（等于偷走主线的记忆）。
            new_id = f"{old_id}-b{to_session_id}" if old_id else f"mem-b{to_session_id}-{offset}"
            extra = {
                k: v
                for k, v in meta.items()
                if isinstance(v, (str, int, float, bool)) and k not in ("session_id", "kind")
            }
            extra["session_id"] = int(to_session_id)
            records.append(
                chroma.MemoryRecord(
                    text=str(text or ""),
                    memory_id=new_id,
                    session_id=int(to_session_id),
                    kind=str(meta.get("kind") or "dialogue"),
                    extra=extra,
                )
            )
        chroma.add_memories(user_id, records)
        return len(records), (
            f"已把分叉点之前的 {len(records)} 条长期记忆复制到新世界线（此后的记忆两条线各记各的）。"
        )
    except Exception as exc:  # noqa: BLE001 - 记忆是锦上添花，绝不能连累分叉本身
        logger.warning("复制长期记忆失败（已降级，分叉仍然成功） | {}: {}", type(exc).__name__, exc)
        return 0, (
            f"长期记忆复制失败（{type(exc).__name__}），新世界线从零开始记 —— "
            "这不影响这段对话本身。"
        )


def _recount(db: Session, session: NarrativeSession) -> int:
    """重算消息条数与累计 token（复制完消息后调用）。

    ★ 口径与 sessions._recalculate_stats 保持一致：total_tokens 取
      「剩余消息的 token_count 之和」—— 新线并没有真的花过主线那些钱，
      但两条线在这一点上是同一起点，估成同一个值最不容易让人困惑。
    """
    rows = list(db.scalars(select(Message).where(Message.session_id == session.id)).all())
    session.message_count = len(rows)
    session.total_tokens = int(sum(int(m.token_count or 0) for m in rows))
    db.commit()
    db.refresh(session)
    return len(rows)


def rename_line(
    db: Session, user_id: int, session_id: int, label: str | None
) -> NarrativeSession:
    """给世界线改名（空 = 恢复默认名）。"""
    session = get_one(db, user_id, session_id)
    session.branch_label = (label or "").strip()[:64] or None
    db.commit()
    db.refresh(session)
    return session


