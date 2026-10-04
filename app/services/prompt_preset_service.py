"""提示词预设业务逻辑：导入、转换、启用、预览。

==================== 这一层负责什么？====================
`app/narrative/presets.py` 负责"文本 → 结构"的纯解析，
本模块负责**把它接到数据库与权限上**：谁能看、谁能改、
导入时名字怎么定、全局默认怎么互斥、预览要用哪条会话的真实上下文。

==================== 三条刻意的设计 ====================
1. **导入是"保真 + 告知"，不是"自动优化"。**
   酒馆预设里有我们渲染不了的块（personaDescription）和云端无效的参数
   （top_k 等）。处理方式是**原样存下来 + 明确告诉用户**，
   而不是偷偷删掉或假装生效 —— 用户拿这份预设还能导回酒馆继续用。

2. **全局默认预设互斥。** 同一个用户同时只能有一个 `is_active=True`。
   否则"新建会话时用哪套"就变成随机行为，用户会以为系统坏了。

3. **预览必须走真实上下文。** 预览用哪张卡、哪本书、哪段历史，
   必须和真正发消息时**完全一致**，否则用户会拿着预览去排查一个不存在的问题
   （这个坑在 `engine.build_session_prompt` 的注释里记过一次）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, NotFoundError
from app.db.models import PromptPreset, User
from app.narrative import presets
from app.schemas.prompt_preset import (
    PresetBlockOut,
    PresetBrief,
    PresetDetail,
    PresetImportIn,
    PresetPreviewOut,
    PresetUpdateIn,
    block_to_out,
    sampling_to_out,
)

#: 预览时最多带多少条历史（和真实对话的裁剪逻辑保持同一口径即可，这里只做展示）
PREVIEW_HISTORY_LIMIT = 40


# ==================================================================
#  查询
# ==================================================================
def _get_owned(db: Session, user_id: int, preset_id: int) -> PromptPreset:
    row = db.scalar(
        select(PromptPreset).where(
            PromptPreset.id == preset_id, PromptPreset.user_id == user_id
        )
    )
    if row is None:
        # 别人的预设一律 404（不泄露"这个 ID 存在"）
        raise NotFoundError("提示词预设不存在")
    return row


def _config_of(row: PromptPreset) -> presets.PromptPresetConfig:
    return presets.from_config(row.config)


def _notes_of(row: PromptPreset) -> list[str]:
    return [line for line in (row.import_notes or "").splitlines() if line.strip()]


def to_brief(row: PromptPreset) -> PresetBrief:
    config = _config_of(row)
    return PresetBrief(
        id=row.id,
        name=row.name,
        description=row.description,
        source_format=row.source_format,
        source_filename=row.source_filename,
        is_active=row.is_active,
        is_builtin=bool(row.is_builtin),
        block_count=len(config.blocks),
        enabled_block_count=sum(1 for b in config.blocks if b.enabled),
        depth_block_count=sum(1 for b in config.blocks if b.enabled and b.depth_injected),
        warnings=[],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_detail(row: PromptPreset) -> PresetDetail:
    config = _config_of(row)
    brief = to_brief(row)
    return PresetDetail(
        **brief.model_dump(),
        blocks=[block_to_out(b) for b in sorted(config.blocks, key=lambda b: b.order_index)],
        sampling=sampling_to_out(config.sampling),
        import_notes=_notes_of(row),
    )


def list_presets(db: Session, user_id: int) -> list[PromptPreset]:
    # ★ 顺手确保内置守卫预设存在：用户第一次打开「提示词预设」页就能看到它、
    #   并且可以立刻改或删（用户明确要求"这些默认预设也能在预设管理里改"）。
    ensure_builtin(db, user_id)
    return list(
        db.scalars(
            select(PromptPreset)
            .where(PromptPreset.user_id == user_id)
            .order_by(
                # ★ 内置守卫预设排在**最后**：它是系统行，不是用户自己的作品；
                #   排最前会把"你的预设"挤下去（也违反"全局默认排最前"的既有约定）。
                PromptPreset.is_builtin.asc(),
                PromptPreset.is_active.desc(),
                PromptPreset.updated_at.desc(),
            )
        )
    )


def get_preset(db: Session, user_id: int, preset_id: int) -> PromptPreset:
    return _get_owned(db, user_id, preset_id)


def get_active(db: Session, user_id: int) -> PromptPreset | None:
    """取该用户的全局默认预设（没有就返回 None = 走内置装配）。"""
    return db.scalar(
        select(PromptPreset).where(
            PromptPreset.user_id == user_id, PromptPreset.is_active.is_(True)
        )
    )


def resolve_for_session(db: Session, session_row: Any) -> PromptPreset | None:
    """决定一条会话当前用哪套预设。

    规则（写死并测试）：**会话自己绑定的 > 用户的全局默认 > 不用预设（内置装配）**。
    ★ 会话绑定一个"已被删除"的预设时，外键是 SET NULL，所以这里拿到的是 None
      → 自动回落到全局默认。用户的感受是"预设没了，但故事照常能聊"。
    ★ 这里**不掺内置守卫预设**：守卫是"追加"而不是"替代"，
      由 `engine.resolve_preset` 用 `presets.merge_configs` 接在后面。
    """
    if session_row is None:
        return None
    preset_id = getattr(session_row, "prompt_preset_id", None)
    if preset_id:
        row = db.scalar(
            select(PromptPreset).where(
                PromptPreset.id == preset_id,
                PromptPreset.user_id == session_row.user_id,
            )
        )
        if row is not None:
            return row
    return get_active(db, session_row.user_id)


# ==================================================================
#  内置守卫预设：按需创建 / 删除 / 还原
# ==================================================================
def get_builtin(db: Session, user_id: int) -> PromptPreset | None:
    return db.scalar(
        select(PromptPreset).where(
            PromptPreset.user_id == user_id, PromptPreset.is_builtin.is_(True)
        )
    )


def _make_builtin(db: Session, user_id: int) -> PromptPreset:
    config = presets.build_guard_config()
    row = PromptPreset(
        user_id=user_id,
        name="内置守卫规则（身份认知 / 不跑偏 / 输出长度）",
        description=(
            "系统自带的沉浸感硬规则，**永远追加在你绑定的预设之后**。"
            "可以像普通预设一样改块、禁用块、改正文；删掉后不再生效，"
            "随时可以在这里点「还原内置规则」恢复。"
        ),
        config=config.to_dict(),
        source_format="hne",
        import_notes=(
            "这份预设由系统生成，用于保证角色不承认自己是 AI、剧情不跑偏、回复不过短。\n"
            "它是**可编辑**的：改正文、禁用某一块、调整顺序都会立即生效。\n"
            "删掉它 = 关掉这些硬规则；「还原内置规则」会把它恢复成出厂内容。"
        ),
        is_active=False,
        is_builtin=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def ensure_builtin(db: Session, user_id: int) -> PromptPreset | None:
    """确保内置守卫预设存在（第一次用到时自动建一份）。

    ★ 用户主动删过（`builtin_preset_dismissed`）就**不再重建** ——
      否则"删除"按钮就是假的。想恢复得自己点「还原」。
    """
    row = get_builtin(db, user_id)
    if row is not None:
        return row
    user = db.get(User, user_id)
    if user is None or getattr(user, "builtin_preset_dismissed", False):
        return None
    return _make_builtin(db, user_id)


def restore_builtin(db: Session, user_id: int) -> PromptPreset:
    """把内置守卫预设恢复成出厂内容（删过就重建，没删过就重置）。"""
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("用户不存在", detail={"user_id": user_id})
    if getattr(user, "builtin_preset_dismissed", False):
        user.builtin_preset_dismissed = False
        db.commit()
    row = get_builtin(db, user_id)
    if row is None:
        return _make_builtin(db, user_id)
    # 已存在 → 只重置内容，保留用户起的名字之外的一切（名字也一起还原，语义是"还原"）
    config = presets.build_guard_config()
    row.name = "内置守卫规则（身份认知 / 不跑偏 / 输出长度）"
    row.config = config.to_dict()
    row.is_builtin = True
    db.commit()
    db.refresh(row)
    return row


# ==================================================================
#  写入
# ==================================================================
def _set_active(db: Session, user_id: int, preset_id: int) -> None:
    """把某个预设设为全局默认，并把其它预设的默认标记清掉（互斥）。"""
    db.execute(
        update(PromptPreset)
        .where(PromptPreset.user_id == user_id, PromptPreset.id != preset_id)
        .values(is_active=False)
    )
    db.execute(
        update(PromptPreset)
        .where(PromptPreset.user_id == user_id, PromptPreset.id == preset_id)
        .values(is_active=True)
    )


def import_preset(
    db: Session, user_id: int, payload: PresetImportIn, *, max_presets: int = 200
) -> PromptPreset:
    """导入一份预设。

    ★ 即使 JSON 看起来像酒馆预设，也要先过 `presets.from_config`：
      它不是"原样存"，而是**规范化 + 登记问题**（不支持的块、无效参数）。
      这样界面才有的可说，而不是把一堆看不懂的字段丢给用户。
    """
    count = db.scalar(
        select(func.count()).select_from(PromptPreset).where(PromptPreset.user_id == user_id)
    )
    if (count or 0) >= max_presets:
        raise BadRequestError(
            f"预设数量已达上限（{max_presets}），请先删掉一些不再使用的再导入",
            detail={"limit": max_presets},
        )

    if payload.preset is not None:
        config = presets.from_config(payload.preset)
        source_format = "hne"
    else:
        config = presets.from_sillytavern(payload.raw or {})
        source_format = "sillytavern"

    if not config.blocks:
        raise BadRequestError(
            "这份预设里没有解析出任何提示词块",
            detail={
                "suggestion": "请确认导入的是 SillyTavern 的「补全预设 / completion preset」JSON",
            },
        )

    name = (payload.name or "").strip()
    if not name:
        # 没给名字就用来源文件名（去掉扩展名），再不行才用兜底名
        stem = (payload.source_filename or "").rsplit(".", 1)[0].strip()
        name = stem or "未命名预设"
    name = name[:120]

    notes = list(config.warnings)
    if not notes:
        notes = ["导入完成，未发现需要提醒的问题。"]

    row = PromptPreset(
        user_id=user_id,
        name=name,
        description=payload.description,
        config=config.to_dict(),
        source_filename=payload.source_filename,
        source_format=source_format,
        import_notes="\n".join(notes),
        is_active=False,
    )
    db.add(row)
    # 先 flush 拿到自增主键，才能在同一次事务里把它设为全局默认（互斥更新需要 id）
    db.flush()

    if payload.make_active:
        _set_active(db, user_id, row.id)

    # ★ 必须显式 commit：接口层的 get_db 是"不自动提交"的（事务由业务代码控制），
    #   只 flush 不 commit 的话，下一个请求就查不到这条数据了 —— 而且不报任何错。
    db.commit()
    db.refresh(row)
    return row


def create_from_default(
    db: Session, user_id: int, *, name: str, description: str | None = None
) -> PromptPreset:
    """用"等价于内置装配"的结构新建一份预设。

    为什么要有它：让用户有一个**可编辑的起点** —— 新建之后直接就是
    一套和当前行为完全一致的块清单，照着改顺序 / 加破甲块即可，
    不用面对空白页去猜块该叫什么名字。
    """
    config = presets.build_default_config()
    row = PromptPreset(
        user_id=user_id,
        name=name[:120],
        description=description or "从系统内置装配复制而来，可直接修改",
        config=config.to_dict(),
        source_format="hne",
        import_notes="这是按系统内置装配生成的初始结构：改顺序、加块、禁用块都可以。",
        is_active=False,
    )
    db.add(row)
    db.commit()
    return row


def update_preset(
    db: Session, user_id: int, preset_id: int, payload: PresetUpdateIn
) -> PromptPreset:
    """改预设：名字 / 描述 / 块 / 采样参数 / 是否默认。"""
    row = _get_owned(db, user_id, preset_id)
    config = _config_of(row)

    if payload.blocks is not None:
        # 整体替换块：按传入顺序重排 order_index。
        # ★ 已有块**保留原正文**（界面上的开关/排序操作不该顺手改正文），
        #   新出现的块才用传入的 content。改正文请走 update_block 那个接口，
        #   这样"误传一个截断的 blocks 列表"不会把用户的破甲文本悄悄截短。
        by_id = {b.identifier: b for b in config.blocks}
        rebuilt: list[presets.PromptBlock] = []
        for index, item in enumerate(payload.blocks):
            existing = by_id.get(item.identifier)
            rebuilt.append(
                presets.PromptBlock(
                    identifier=item.identifier,
                    name=item.name or item.identifier,
                    content=existing.content if existing is not None else item.content,
                    role=item.role,
                    system_prompt=item.system_prompt,
                    injection_position=item.injection_position,
                    injection_depth=item.injection_depth,
                    forbid_overrides=existing.forbid_overrides if existing else False,
                    enabled=item.enabled,
                    order_index=index,
                    kind=item.kind,
                    supported=item.supported,
                )
            )
        config.blocks = rebuilt

    if payload.sampling is not None:
        incoming = payload.sampling.model_dump(exclude={"note", "ignored_by_cloud"})
        merged = {k: v for k, v in incoming.items() if v is not None}
        # 原有的"本地推理引擎参数"要保住（用户在界面上看不到它们，但导出时不能丢）
        for key, value in config.sampling.items():
            if presets.PARAM_SUPPORT.get(key) == "passthrough":
                merged.setdefault(key, value)
        config.sampling = merged

    row.config = config.to_dict()
    if payload.name is not None:
        row.name = payload.name
    if payload.description is not None:
        row.description = payload.description
    if payload.is_active is not None:
        if payload.is_active:
            _set_active(db, user_id, row.id)
        else:
            row.is_active = False
    db.commit()
    db.refresh(row)
    return row


def update_block(
    db: Session, user_id: int, preset_id: int, identifier: str, payload
) -> PromptPreset:
    """只改一个块（改正文 / 启停 / 角色 / 注入深度）。"""
    row = _get_owned(db, user_id, preset_id)
    config = _config_of(row)
    block = config.block_map().get(identifier)
    if block is None:
        raise NotFoundError(f"预设里没有这个块：{identifier}")

    if payload.content is not None:
        block.content = payload.content
    if payload.enabled is not None:
        block.enabled = payload.enabled
    if payload.role is not None:
        block.role = payload.role
    if payload.injection_position is not None:
        block.injection_position = payload.injection_position
    if payload.injection_depth is not None:
        block.injection_depth = payload.injection_depth

    row.config = config.to_dict()
    db.commit()
    db.refresh(row)
    return row


def add_block(
    db: Session, user_id: int, preset_id: int, block: PresetBlockOut
) -> PromptPreset:
    """追加一个自定义块（这是"加破甲块"的入口）。"""
    row = _get_owned(db, user_id, preset_id)
    config = _config_of(row)
    if block.identifier in config.block_map():
        raise BadRequestError(f"块标识已存在：{block.identifier}")
    config.blocks.append(
        presets.PromptBlock(
            identifier=block.identifier,
            name=block.name or block.identifier,
            content=block.content,
            role=block.role,
            system_prompt=block.system_prompt,
            injection_position=block.injection_position,
            injection_depth=block.injection_depth,
            enabled=block.enabled,
            order_index=max((b.order_index for b in config.blocks), default=-1) + 1,
            kind=block.kind,
            supported=True,
        )
    )
    row.config = config.to_dict()
    db.commit()
    db.refresh(row)
    return row


def delete_block(db: Session, user_id: int, preset_id: int, identifier: str) -> PromptPreset:
    """删掉一个块。

    ★ 内部块（main / chatHistory 这些）**不允许删**：它们代表系统能力，
      删了之后用户会以为"这段内容不存在"，其实只是没人往那个位置放东西。
      要停用请用 enabled=false（语义清楚且可逆）。
    """
    row = _get_owned(db, user_id, preset_id)
    config = _config_of(row)
    block = config.block_map().get(identifier)
    if block is None:
        raise NotFoundError(f"预设里没有这个块：{identifier}")
    if not block.is_marker and identifier in presets.BLOCK_LABELS:
        raise BadRequestError(
            f"内置块「{block.name}」不能删除；如果要停用它，请改成禁用（enabled=false）",
            detail={"identifier": identifier, "suggestion": "把 enabled 设为 false"},
        )
    config.blocks = [b for b in config.blocks if b.identifier != identifier]
    row.config = config.to_dict()
    db.commit()
    db.refresh(row)
    return row


def delete_preset(db: Session, user_id: int, preset_id: int) -> None:
    """删除预设。

    绑定过它的会话不会被删：外键是 ON DELETE SET NULL，
    会话会自动回落到"全局默认预设 / 内置装配"。

    ★ 删的是**内置守卫预设**时，额外留下墓碑位（`users.builtin_preset_dismissed`）：
      否则按需创建的逻辑下一次就会把它重建出来，"删除"等于没删。
    """
    row = _get_owned(db, user_id, preset_id)
    was_builtin = bool(row.is_builtin)
    db.delete(row)
    if was_builtin:
        user = db.get(User, user_id)
        if user is not None:
            user.builtin_preset_dismissed = True
    db.commit()


def export_preset(db: Session, user_id: int, preset_id: int) -> dict[str, Any]:
    """导出成酒馆能读的 completion preset（尽量还原）。

    ★ 为什么导出成酒馆格式而不是我们自己的格式？
      因为用户很可能想把它拿回酒馆继续改。能来回搬才是真的"不锁定"。
      导出时把我们没有的字段留给酒馆去补默认值，不做假设。
    """
    row = _get_owned(db, user_id, preset_id)
    config = _config_of(row)

    reverse = {v: k for k, v in presets.SAMPLING_KEYS.items()}
    payload: dict[str, Any] = {"chat_completion_source": "custom"}
    for key, value in config.sampling.items():
        payload[reverse.get(key, key)] = value

    payload["prompts"] = [
        {
            "identifier": b.identifier,
            "name": b.name,
            "system_prompt": b.system_prompt,
            "role": b.role if b.role in ("system", "user", "assistant") else None,
            "content": b.content,
            "injection_position": b.injection_position,
            "injection_depth": b.injection_depth,
            "forbid_overrides": b.forbid_overrides,
        }
        for b in config.blocks
    ]
    payload["prompt_order"] = [
        {
            "character_id": 100000,
            "order": [
                {"identifier": b.identifier, "enabled": b.enabled}
                for b in sorted(config.blocks, key=lambda b: b.order_index)
            ],
        }
    ]
    return payload


# ==================================================================
#  预览：这套预设到底会把提示词拼成什么
# ==================================================================
def preview(
    db: Session, user_id: int, *, session_id: int | None = None, preset_id: int | None = None
) -> "PresetPreviewOut":
    """按真实上下文装配一次，并**逐块**说明每段内容的来源。

    ★ 为什么预览必须走真实上下文（会话的卡、世界书、历史、记忆）？
      因为"预览和真正发出去的东西不一致"是这个项目踩过的坑
      （见 engine.build_session_prompt 的注释）。预览的价值就在于**可信**。
    """
    from app.db.models import Message as MessageModel
    from app.db.models import NarrativeSession
    from app.narrative import presets as presets_mod
    from app.narrative.engine import load_card_and_book
    from app.narrative.prompt_builder import build_prompt, resolve_system_prompt
    from app.schemas.prompt_preset import PresetPreviewOut, PreviewMessageOut

    session_row: NarrativeSession | None = None
    if session_id is not None:
        session_row = db.scalar(
            select(NarrativeSession).where(
                NarrativeSession.id == session_id, NarrativeSession.user_id == user_id
            )
        )
        if session_row is None:
            raise NotFoundError("会话不存在")

    # ---- 决定用哪套预设：显式指定的 > 会话绑定的 > 全局默认 ----
    if preset_id is not None:
        row = _get_owned(db, user_id, preset_id)
    elif session_row is not None:
        row = resolve_for_session(db, session_row)
    else:
        row = get_active(db, user_id)

    config = _config_of(row) if row is not None else None

    if session_row is None:
        # 没有指定会话时做"结构预览"：不绑定真实卡片与历史，
        # 只把预设的块顺序摊开。界面用它让用户先看懂结构再选会话。
        result = (
            presets_mod.render_blocks(config, presets_mod.RenderRequest())
            if config is not None
            else presets_mod.RenderResult()
        )
        return PresetPreviewOut(
            preset_id=getattr(row, "id", None),
            preset_name=getattr(row, "name", None),
            messages=[
                PreviewMessageOut(
                    position="system_prompt" if item.depth is None else "depth",
                    depth=item.depth,
                    role=item.message.role,
                    content=item.message.content,
                    from_blocks=[item.identifier],
                )
                for item in result.messages
            ],
            used_blocks=list(result.used),
            skipped_blocks=list(result.skipped),
            disabled_blocks=[b.identifier for b in (config.blocks if config else []) if not b.enabled],
            unknown_macros=list(result.unknown_macros),
            notes=[
                "这是**结构预览**（未指定会话）：只展示块顺序与角色，"
                "不包含真实角色卡 / 世界书 / 历史。指定会话可以看到完整效果。"
            ],
        )

    # ---- 真实上下文 ----
    card, book = load_card_and_book(db, session_row)
    history = list(
        db.scalars(
            select(MessageModel)
            .where(MessageModel.session_id == session_row.id)
            .order_by(MessageModel.id.desc())
            .limit(PREVIEW_HISTORY_LIMIT)
        )
    )[::-1]

    # 世界书：按真实的**混合检索口径**跑一遍（不调模型、纯本地计算）
    # ★ 为什么走 retrieval 而不是 world_book_scanner.scan：
    #   真实请求里关键词与语义两路共享同一个 token 预算、并且要经过去重与重排。
    #   预览若还用"世界书自己的预算"单跑，就会显示"注入了 3 条"而实际只进 1 条 ——
    #   又是一次"预览与实际不一致"（本项目对这条零容忍）。
    #   这里**不含语义通道**（向量检索是外部依赖、会花钱/耗时），
    #   所以下面会显式提醒用户"回忆未参与，实际入选可能更少"。
    from app.narrative import retrieval as retrieval_mod
    from app.narrative import world_book_scanner

    scan_depth, token_budget = world_book_scanner.resolve_settings(book)
    retrieval = retrieval_mod.retrieve(
        book=book,
        history=history,
        query="",
        user_id=None,  # ← 不触发向量检索（见上）
        session_id=session_row.id,
        scan_depth=scan_depth,
        budget=token_budget if token_budget > 0 else retrieval_mod.DEFAULT_BUDGET,
    )
    world_entries = retrieval.world_entries

    user_row = db.get(User, user_id)
    user_name = str(getattr(user_row, "username", "") or "") if user_row else ""

    builtin_main, _source = resolve_system_prompt(
        card, world_book=book, world_book_entries=world_entries
    )

    plan = build_prompt(
        card=card,
        history=history,
        world_book=book,
        world_book_entries=world_entries,
        recalled_memories=None,  # 预览不触发向量检索（它是外部依赖，且会花钱/耗时）
        preset=row,
        preset_config=config,
        user_name=user_name,
    )
    plan.retrieval = retrieval.to_dict()
    plan.retrieval_summary = retrieval_mod.describe(retrieval) + "（本次预览未含语义通道）"

    messages = [
        PreviewMessageOut(
            position="system_prompt",
            role="system",
            content=plan.system_prompt,
            from_blocks=list(plan.preset_blocks_used) or ["（内置装配）"],
        )
    ]
    for depth, message in plan.depth_messages:
        messages.append(
            PreviewMessageOut(
                position="depth",
                depth=depth,
                role=message.role,
                content=message.content,
                from_blocks=["（深度注入的预设块）"],
            )
        )
    messages.append(
        PreviewMessageOut(
            position="history",
            role="（多条）",
            content=f"本次带上 {len(history)} 条历史消息（这里不逐条展开）",
            from_blocks=["chatHistory"],
        )
    )

    notes = [
        "对话历史固定接在系统提示词之后：预设里的 chatHistory 标记只决定"
        "「历史要不要带上」，不改变它的位置。",
        "长期记忆召回没有在预览里触发（它要访问向量库、有耗时）；"
        "真实发消息时会额外追加在系统提示词末尾。",
        # ★ 预算现在由关键词与语义两路共享：预览只算了关键词那一路，
        #   所以真实请求里世界书条目**可能更少**（被回忆挤掉）。这句必须说，
        #   否则又成了"预览与实际不一致"。
        "本次预览只跑了关键词通道：真实请求里两路共享 token 预算，"
        "世界书条目可能被回忆挤出（届时会话界面会给出提醒）。",
    ]
    if book is not None and retrieval.lexical_total == 0:
        notes.append("本次没有命中任何世界书关键词（或没有历史消息可扫）。")

    return PresetPreviewOut(
        session_id=session_row.id,
        preset_id=getattr(row, "id", None),
        preset_name=getattr(row, "name", None),
        messages=messages,
        used_blocks=list(plan.preset_blocks_used),
        skipped_blocks=list(plan.preset_blocks_skipped),
        disabled_blocks=list(plan.preset_blocks_disabled),
        unknown_macros=list(plan.preset_unknown_macros),
        warnings=list(plan.warnings),
        history_messages=len(history),
        notes=notes,
    )
