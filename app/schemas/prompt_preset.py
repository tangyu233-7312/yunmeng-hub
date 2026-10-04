"""提示词预设的请求 / 响应模型。

界面需要看到的不只是"有哪些预设"，更重要的是：
**这套预设到底会怎么拼、哪些块没生效、哪些参数发了也没用**。
所以下面的响应模型里，`blocks` / `skipped` / `notes` 这些都是必须的，
不是可选装饰 —— 没有它们，用户没法判断"破甲到底生效没有"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.narrative import presets

# ==================================================================
#  公共视图
# ==================================================================
class PresetBlockOut(BaseModel):
    """预设里的一个块（界面按这个渲染可勾选的列表）。

    ★ `content` 是**完整正文**，不是摘要。
      一开始写成了"前 120 字预览"，结果发现"编辑单块正文"和
      "整体替换块列表"这两条路都需要全文 —— 传摘要会让用户的破甲文本被悄悄截短，
      这属于最不能接受的那类 bug。列表页要短，可以让前端自己截。
    """

    identifier: str
    name: str
    kind: Literal["rule", "marker"]
    content: str = ""
    role: str = "system"
    system_prompt: bool = True
    injection_position: int = Field(default=0, description="0 进系统提示词 / 1 按深度插进历史")
    injection_depth: int = Field(default=0, description="深度注入插在倒数第几条之前")
    enabled: bool = True
    supported: bool = Field(default=True, description="本系统是否真的能渲染它")
    order_index: int = 0


class PresetSamplingOut(BaseModel):
    """预设携带的采样参数。

    为什么不直接塞进 JSON 列给前端？
      因为要**分类**告诉用户哪些参数在云端会被忽略 —— 这一点直接决定
      "我调了 top_k 为什么没反应"。所以这里显式列出，并带 note。
    """

    temperature: float | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    max_tokens: int | None = None
    context_window: int | None = Field(
        default=None,
        description="★ 酒馆的 openai_max_context。本系统的上下文窗口属于**模型配置**，"
        "这里只记录预设想要的值，不会覆盖模型配置（避免预算与实际不一致）",
    )

    # 本地推理引擎参数：云端 OpenAI 兼容接口会静默忽略
    top_k: float | None = None
    top_a: float | None = None
    min_p: float | None = None
    repetition_penalty: float | None = None

    note: str = Field(default="", description="参数生效性的说明（哪些发了也没用）")
    ignored_by_cloud: list[str] = Field(
        default_factory=list, description="这些参数对云端接口无效（会被静默忽略）"
    )


class PresetBrief(BaseModel):
    """预设列表项。"""

    id: int
    name: str
    description: str | None = None
    source_format: str = "hne"
    source_filename: str | None = None
    is_active: bool = False
    is_builtin: bool = Field(
        default=False,
        description=(
            "是否是系统内置的「守卫规则」预设。它永远追加在你绑定的预设之后，"
            "同样可以编辑/删除，删除后可用「还原内置规则」恢复。"
        ),
    )
    block_count: int = 0
    enabled_block_count: int = 0
    depth_block_count: int = Field(default=0, description="走深度注入的块数（破甲通常靠它）")
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PresetDetail(PresetBrief):
    """预设详情（含全部块与采样参数）。"""

    blocks: list[PresetBlockOut] = Field(default_factory=list)
    sampling: PresetSamplingOut = Field(default_factory=PresetSamplingOut)
    import_notes: list[str] = Field(default_factory=list, description="导入时的提醒与降级说明")


# ==================================================================
#  写入
# ==================================================================
class PresetImportIn(BaseModel):
    """导入一份预设。

    两种来源二选一：
      · `raw`    —— 直接贴酒馆的 completion preset JSON
      · `preset` —— 本系统导出格式（{blocks, sampling}）
    """

    name: str | None = Field(default=None, max_length=120, description="不传则取 JSON 里的 name")
    description: str | None = Field(default=None, max_length=500)
    source_filename: str | None = Field(default=None, max_length=255)
    raw: dict[str, Any] | None = Field(default=None, description="酒馆预设 JSON 本体")
    preset: dict[str, Any] | None = Field(default=None, description="本系统导出格式的配置本体")
    make_active: bool = Field(default=False, description="导入后设为全局默认预设")

    @model_validator(mode="after")
    def _need_body(self) -> "PresetImportIn":
        if not self.raw and not self.preset:
            raise ValueError("raw 与 preset 至少要给一个")
        return self


class PresetUpdateIn(BaseModel):
    """改预设（PATCH 语义）。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = Field(default=None, description="设为该用户的全局默认预设")
    blocks: list[PresetBlockOut] | None = Field(
        default=None, description="整体替换块（改顺序 / 启停 / 正文都走这里）"
    )
    sampling: PresetSamplingOut | None = Field(default=None, description="整体替换采样参数")


class PresetBlockContentIn(BaseModel):
    """单独改一个块（界面上"编辑这一块的正文"用，比整体替换安全）。"""

    content: str | None = Field(default=None, max_length=100_000)
    enabled: bool | None = None
    role: Literal["system", "user", "assistant"] | None = None
    injection_position: Literal[0, 1] | None = None
    injection_depth: int | None = Field(default=None, ge=0, le=64)

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value if value is None else value.strip()


# ==================================================================
#  预览（"这套预设会把提示词拼成什么样"）
# ==================================================================
class PreviewMessageOut(BaseModel):
    """装配出来的一条消息。"""

    position: str = Field(..., description="system_prompt（系统提示词）/ depth（插进历史）")
    depth: int | None = Field(default=None, description="深度注入时插在倒数第几条之前")
    role: str = "system"
    content: str = ""
    from_blocks: list[str] = Field(
        default_factory=list, description="这条消息由哪些块贡献（按顺序）"
    )


class PresetPreviewOut(BaseModel):
    """一次装配预览的完整结果。"""

    session_id: int | None = None
    preset_id: int | None = None
    preset_name: str | None = None
    messages: list[PreviewMessageOut] = Field(default_factory=list)
    used_blocks: list[str] = Field(default_factory=list)
    skipped_blocks: list[str] = Field(default_factory=list, description="被跳过的块及原因")
    disabled_blocks: list[str] = Field(default_factory=list)
    unknown_macros: list[str] = Field(
        default_factory=list, description="预设里用到、但本系统不认识的宏（会原样保留）"
    )
    warnings: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0
    history_messages: int = Field(default=0, description="本次带上了几条历史消息")
    notes: list[str] = Field(default_factory=list, description="装配层面的说明（例如历史固定接在后面）")


def sampling_to_out(sampling: dict[str, Any]) -> PresetSamplingOut:
    """把配置里的采样参数转成响应模型，并算出"哪些参数云端无效"。"""
    fields = set(PresetSamplingOut.model_fields) - {"note", "ignored_by_cloud"}
    payload = {k: v for k, v in sampling.items() if k in fields}
    ignored = sorted(
        k for k in sampling if presets.PARAM_SUPPORT.get(k) == "passthrough"
    )
    out = PresetSamplingOut(**payload)
    out.ignored_by_cloud = ignored
    if ignored:
        out.note = (
            "标注为「本地推理引擎参数」的项发给云端 OpenAI 兼容接口会被**静默忽略**"
            "（实测同类现象见 docs/pitfalls.md 第 4 条）。本系统照常保存，但不假装它们生效。"
        )
    return out


def block_to_out(block: presets.PromptBlock) -> PresetBlockOut:
    return PresetBlockOut(
        identifier=block.identifier,
        name=block.name or block.identifier,
        kind=block.kind,
        content=block.content,
        role=block.role,
        system_prompt=block.system_prompt,
        injection_position=block.injection_position,
        injection_depth=block.injection_depth,
        enabled=block.enabled,
        supported=block.supported,
        order_index=block.order_index,
    )
