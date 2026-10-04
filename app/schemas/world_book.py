"""世界书的请求 / 响应模型。

==================== 关于「条目」的校验策略 ====================
世界书的条目来自用户手写，也可能来自网上下载的角色卡，质量参差不齐。
这里的策略是：**规范化 + 明确报错**，而不是悄悄丢弃。

  · 缺 `keys`      -> 补成空数组（关键词空的条目只能靠 constant 之类的方式触发，合法）
  · `keys` 给字符串 -> 宽容地当成只含一个关键词（有些工具会这么写）
  · 缺 `enabled`   -> 默认 True
  · 缺 `content`   -> **报错**。一条没有内容的设定毫无意义，
                       而且它多半说明数据有问题，静默丢掉等于骗用户说导入成功了。
  · 其它未知字段   -> 原样保留（规范要求不得丢弃无法识别的字段）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------- 上限（改动这里即可全局生效）----------------
MAX_ENTRIES = 500
MAX_KEYS_PER_ENTRY = 50
MAX_KEY_LENGTH = 200
MAX_CONTENT_LENGTH = 20_000


def _clean_text(value: str | None) -> str | None:
    """去首尾空白；空字符串视为「没填」。与角色卡的规则保持一致。"""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_keys(raw: object) -> list[str]:
    """规范化关键词数组：容忍字符串、去空白、去重、限长。"""
    if isinstance(raw, str):
        # 宽容处理：`"keys": "龙"` 当成 `["龙"]`
        raw = [raw]
    if not isinstance(raw, list):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        key = item.strip()
        if not key or key in seen:
            continue
        if len(key) > MAX_KEY_LENGTH:
            raise ValueError(f"关键词过长（上限 {MAX_KEY_LENGTH} 字）：{key[:20]}...")
        seen.add(key)
        result.append(key)

    if len(result) > MAX_KEYS_PER_ENTRY:
        raise ValueError(f"单条条目的关键词最多 {MAX_KEYS_PER_ENTRY} 个")
    return result


def normalize_entry(raw: object, index: int) -> dict:
    """把一条原始条目规范化成标准结构（供 schema 校验与导入共用）。"""
    label = f"第 {index + 1} 条条目"
    if not isinstance(raw, dict):
        raise ValueError(f"{label}必须是一个对象")

    content = raw.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{label}缺少 content（设定正文）")

    if len(content) > MAX_CONTENT_LENGTH:
        raise ValueError(
            f"{label}的 content 过长（{len(content)} 字，上限 {MAX_CONTENT_LENGTH}）"
        )

    # ★ 先整体复制，把未知字段都带上，再覆盖我们认识的字段。
    #   这样规范里那些我们暂未实现的条目字段（position / selective /
    #   secondary_keys / priority ...）也能原样存下来。
    entry = dict(raw)
    entry["keys"] = _normalize_keys(raw.get("keys"))
    entry["content"] = content.strip()
    entry["enabled"] = bool(raw.get("enabled", True))

    # insertion_order 用于决定多个条目同时命中时的插入顺序
    order = raw.get("insertion_order", 0)
    try:
        entry["insertion_order"] = int(order)
    except (TypeError, ValueError):
        entry["insertion_order"] = 0

    # 规范要求每个条目都必须有 extensions 字段（哪怕是空对象）
    if not isinstance(entry.get("extensions"), dict):
        entry["extensions"] = {}

    return entry


def normalize_entries(raw: object) -> list[dict]:
    """规范化整个条目数组。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("entries 必须是数组")

    if len(raw) > MAX_ENTRIES:
        raise ValueError(f"条目数最多 {MAX_ENTRIES} 条，当前 {len(raw)} 条")

    return [normalize_entry(item, index) for index, item in enumerate(raw)]


class WorldBookCreate(BaseModel):
    """新建世界书。"""

    name: str = Field(..., min_length=1, max_length=200, description="世界书名称")
    description: str | None = Field(default=None, description="简介")
    entries: list[dict[str, Any]] = Field(
        default_factory=list, description=f"条目数组，最多 {MAX_ENTRIES} 条"
    )

    # ---------------- 关键词触发检索的参数（3.9）----------------
    # ★ 这两个参数在 Character Card V2 规范里属于 character_book，
    #   本项目把它们存进 world_books.extra_data（保证导入导出往返无损）。
    #   在 3.9 之前它们只能"从卡里带进来"，界面完全改不了 ——
    #   于是关键词触发检索做出来也没法调。所以这里把它们提升为正式入参。
    scan_depth: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description="扫描最近多少条消息来匹配关键词（对应规范的 scan_depth，默认 8）",
    )
    token_budget: int | None = Field(
        default=None,
        ge=0,
        le=100_000,
        description="最多允许注入多少 token 的设定正文（对应规范的 token_budget，默认 1024）",
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("世界书名称不能为空")
        return value

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @field_validator("entries")
    @classmethod
    def _validate_entries(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return normalize_entries(value)

    book_settings: dict[str, int] = Field(
        default_factory=dict,
        exclude=True,
        description="本次显式提交的扫描参数（服务层写进 extra_data 用；不出现在请求/响应里）",
    )

    @model_validator(mode="after")
    def _collect_scan_settings(self) -> "WorldBookCreate":
        """只收集**显式提交**的扫描参数，整理成 book_settings。

        ★ 为什么要单独收集，而不是让服务层去读 self.scan_depth？
          因为 PATCH 语义需要区分「没提交」与「提交了」。
          服务层拿到 book_settings 这个干净字典后，
          "更新 extra_data" 就是一次 dict.update，不必再判断哪些字段出现过。

        ★ 为什么要声明成 Field 而不是直接 self.book_settings = {...}？
          pydantic v2 禁止给未声明的字段赋值（会抛 "object has no field"）。
          声明 + exclude=True 既满足校验，又不会污染请求/响应结构。
        """
        settings: dict[str, int] = {}
        for field in ("scan_depth", "token_budget"):
            if field in self.model_fields_set:
                value = getattr(self, field)
                if value is not None:
                    settings[field] = int(value)
        self.book_settings = settings
        return self


class WorldBookUpdate(BaseModel):
    """更新世界书（PATCH 语义）。

    规则与角色卡一致：字段**不出现**在请求里 = 保持原值；
    显式传 `null` = 清空（name 除外，它是必填的，传 null 会被拒绝）。

    ★ entries 是**整体替换**而不是合并：传了就整份换掉。
      合并数组语义含糊（按什么键合并？顺序怎么办？），
      整体替换对前端反而更好实现（编辑器里本来就有完整的一份）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    entries: list[dict[str, Any]] | None = None
    scan_depth: int | None = Field(default=None, ge=1, le=200)
    token_budget: int | None = Field(default=None, ge=0, le=100_000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("世界书名称不能为空")
        return value

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @field_validator("entries")
    @classmethod
    def _validate_entries(
        cls, value: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        if value is None:
            return None
        return normalize_entries(value)

    book_settings: dict[str, int] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def _collect_scan_settings(self) -> "WorldBookUpdate":
        """与 WorldBookCreate 同样的处理：只收集显式提交的扫描参数。"""
        settings: dict[str, int] = {}
        for field in ("scan_depth", "token_budget"):
            if field in self.model_fields_set and getattr(self, field) is not None:
                settings[field] = int(getattr(self, field))
        self.book_settings = settings
        return self


class WorldBookRef(BaseModel):
    """世界书的精简引用，嵌在角色卡详情里返回。

    只给「够用」的信息：前端要显示「已关联世界书：《克苏鲁世界》（42 条）」，
    但这些信息在列表页不该把整本书都拖出来。
    """

    id: int
    name: str | None = None
    display_name: str = ""
    entry_count: int = 0
    enabled_entry_count: int = 0


class WorldBookBrief(BaseModel):
    """世界书（列表项）。

    与角色卡一样，列表不返回 entries 全文 ——
    一本世界书可能有几百个条目，列表全带上会很笨重。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    #: 数据库里真实存储的名字，可能为 null（规范允许世界书不写名字）
    name: str | None = None
    #: ★ 界面显示用的名字：name 为空时自动兜底成「未命名世界书（N 条）」。
    #:   与 name 分开是为了不把兜底文案写进数据库 —— 那会破坏导出的一致性。
    display_name: str = ""
    description: str | None = None

    entry_count: int = 0
    enabled_entry_count: int = 0
    #: 有多少张角色卡在用这本书（删除前要提醒用户）
    card_count: int = 0

    created_at: datetime
    updated_at: datetime


class WorldBookOut(BaseModel):
    """世界书（详情，完整版）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str | None = None
    display_name: str = ""
    description: str | None = None
    entries: list[dict[str, Any]] = Field(default_factory=list)

    entry_count: int = 0
    enabled_entry_count: int = 0
    #: 关键词触发检索的生效参数（3.9；界面用于展示与修改）
    scan_depth: int = 8
    token_budget: int = 1024
    #: 正在使用这本书的角色卡（给出 id 与名字，便于界面提示「正在被这些卡使用」）
    used_by_cards: list[dict[str, Any]] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime
