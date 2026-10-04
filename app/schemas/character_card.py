"""角色卡的请求 / 响应模型。

==================== 这个模块里最值得看的三件事 ====================
1. 「列表」和「详情」返回**两种不同结构**（Brief / Out），原因见 CharacterCardBrief。
2. PATCH 用 `model_fields_set` 区分「没传」和「传了 null」，
   这样才能支持「把某个字段清空」这种操作（详见 CharacterCardUpdate）。
3. 标签与备选开场白会做规范化（去空白、去重、限长），
   避免出现 ['奇幻', ' 奇幻 ', ''] 这种脏数据。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------- 规范化时的上限（改动这里即可全局生效）----------------
MAX_TAG_COUNT = 20
MAX_TAG_LENGTH = 50
#: 备选开场白的条数上限。
#  ★ 为什么从 20 提到 100：这个上限原本是配合前端"一行一条"的文本域定的，
#    而那个前端把**一条多行开场白**拆成了几十条（1 条 3107 字 / 66 个换行
#    → 67 条），于是正常保存必然 422。前端已改成"一条一页、不按换行拆"，
#    上限跟着放宽到 100：够用，又不会被恶意请求塞爆。
MAX_GREETING_COUNT = 100
MAX_GREETING_LENGTH = 8000


def _clean_text(value: str | None) -> str | None:
    """文本字段统一处理：去掉首尾空白；空字符串视为「没填」(None)。

    ★ 为什么要区分 "" 和 None？
      数据库里存 "" 和 NULL 都查不出内容，但会让判断逻辑变麻烦
      （比如 `if card.greeting:` 和 `if card.greeting is not None:` 结果不同）。
      统一收敛成 None，后面构建提示词时只需要判断一种情况。
    """
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_str_list(
    value: list[str] | None,
    *,
    max_count: int,
    max_length: int,
    field_label: str,
) -> list[str] | None:
    """字符串数组的通用规范化：去空白 → 丢弃空项 → 去重（保留原顺序）→ 检查上限。

    ★ 这里选择「超出上限就报错」而不是「悄悄截断」，
      因为静默截断会让用户以为内容存进去了，其实丢了 —— 这种 bug 极难发现。
    """
    if value is None:
        return None

    result: list[str] = []
    seen: set[str] = set()

    for raw in value:
        if not isinstance(raw, str):
            raise ValueError(f"{field_label}必须是字符串数组")
        item = raw.strip()
        if not item:
            continue  # 丢弃空字符串，不报错（界面上的空输入框很常见）
        if len(item) > max_length:
            raise ValueError(
                f"{field_label}中单项长度不能超过 {max_length} 个字符：{item[:20]}..."
            )
        # 去重：以去空白后的原文为准，避免 ['奇幻', ' 奇幻 '] 这种重复
        if item in seen:
            continue
        seen.add(item)
        result.append(item)

    if len(result) > max_count:
        raise ValueError(f"{field_label}最多 {max_count} 个，当前 {len(result)} 个")

    return result


class CharacterCardCreate(BaseModel):
    """新建角色卡。

    除 name 外全部可选 —— 鼓励用户先把卡建出来再慢慢完善，
    而不是被一堆必填项挡在门外。
    """

    name: str = Field(..., min_length=1, max_length=100, description="角色名")

    # ---------------- 展示信息 ----------------
    avatar_url: str | None = Field(default=None, max_length=512, description="头像地址")
    description: str | None = Field(default=None, description="一句话简介")

    # ---------------- 人设（会被拼进系统提示词）----------------
    personality: str | None = Field(default=None, description="性格特征")
    background: str | None = Field(default=None, description="背景故事 / 身世设定")
    speaking_style: str | None = Field(default=None, description="说话风格与语气")
    scenario: str | None = Field(default=None, description="初始场景，即故事从哪里开始")
    example_dialogue: str | None = Field(
        default=None, description="对话示例（few-shot 范例，用于稳定输出风格）"
    )

    # ---------------- 开场白 ----------------
    greeting: str | None = Field(
        default=None,
        description="开场白。建会话时会作为角色的第一条消息，故事从这里开始",
    )
    alternate_greetings: list[str] = Field(
        default_factory=list, description=f"备选开场白，最多 {MAX_GREETING_COUNT} 条"
    )

    # ---------------- 提示词覆盖（高级）----------------
    system_prompt: str | None = Field(
        default=None, description="自定义系统提示词（留空则用引擎自动拼装的那份）"
    )
    post_history_instructions: str | None = Field(
        default=None, description="尾注指令，追加在对话历史之后"
    )

    # ---------------- 其它 ----------------
    tags: list[str] = Field(default_factory=list, description=f"标签，最多 {MAX_TAG_COUNT} 个")
    is_public: bool = Field(default=False, description="是否公开（公开后其他用户可查看并选用）")
    world_book_id: int | None = Field(
        default=None,
        description="关联的世界书ID。一本世界书可被多张卡共用，详见 /world-books 接口",
    )
    extensions: dict[str, Any] | None = Field(
        default=None,
        description="★ 扩展命名空间（Character Card V2 规范里的 data.extensions）。"
        "本项目自有字段放在 extensions.hne 下，其中与状态栏有关的是："
        "state_schema（字段清单，最权威）与 initial_state（初始状态，字段可从它推断）。"
        "详见 app/narrative/state_schema.py",
    )

    # ---------------- 校验器 ----------------
    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("角色名不能为空")
        return value

    @field_validator(
        "avatar_url",
        "description",
        "personality",
        "background",
        "speaking_style",
        "scenario",
        "example_dialogue",
        "greeting",
        "system_prompt",
        "post_history_instructions",
    )
    @classmethod
    def _clean_text_fields(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str]) -> list[str]:
        return _clean_str_list(
            value, max_count=MAX_TAG_COUNT, max_length=MAX_TAG_LENGTH, field_label="标签"
        ) or []

    @field_validator("alternate_greetings")
    @classmethod
    def _validate_greetings(cls, value: list[str]) -> list[str]:
        return _clean_str_list(
            value,
            max_count=MAX_GREETING_COUNT,
            max_length=MAX_GREETING_LENGTH,
            field_label="备选开场白",
        ) or []


class CharacterCardUpdate(BaseModel):
    """更新角色卡（PATCH 语义：只提交需要改的字段）。

    ==================== 怎么把一个字段「清空」？====================
    这是 PATCH 接口的经典难题。假设前端想让 greeting 变空：

        {"greeting": null}

    但 `greeting: str | None = None` 的写法里，null 和「字段没出现」长得一模一样，
    服务层没法区分「用户要清空」还是「用户没提到这个字段」。

    ★ 解决办法：pydantic v2 提供了 `model_fields_set`，
      它记录本次请求**实际出现了哪些字段名**。于是：

        字段不在 model_fields_set 里  ->  没提到，保持原值不动
        字段在 model_fields_set 里     ->  按提交的值处理（哪怕是 None，就是清空）

      服务层用这个集合来决定要不要赋值，见 services/character_card_service.py。
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    avatar_url: str | None = Field(default=None, max_length=512)
    description: str | None = None
    personality: str | None = None
    background: str | None = None
    speaking_style: str | None = None
    scenario: str | None = None
    example_dialogue: str | None = None
    greeting: str | None = None
    alternate_greetings: list[str] | None = None
    system_prompt: str | None = None
    post_history_instructions: str | None = None
    tags: list[str] | None = None
    is_public: bool | None = None
    #: 传整数 = 关联到这本世界书；传 null = 解除关联；不传 = 保持原样
    world_book_id: int | None = None
    #: 扩展命名空间；传 null = 清空（此时卡就不再声明状态栏格式）
    extensions: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("角色名不能为空")
        return value

    @field_validator(
        "avatar_url",
        "description",
        "personality",
        "background",
        "speaking_style",
        "scenario",
        "example_dialogue",
        "greeting",
        "system_prompt",
        "post_history_instructions",
    )
    @classmethod
    def _clean_text_fields(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str] | None) -> list[str] | None:
        return _clean_str_list(
            value, max_count=MAX_TAG_COUNT, max_length=MAX_TAG_LENGTH, field_label="标签"
        )

    @field_validator("alternate_greetings")
    @classmethod
    def _validate_greetings(cls, value: list[str] | None) -> list[str] | None:
        return _clean_str_list(
            value,
            max_count=MAX_GREETING_COUNT,
            max_length=MAX_GREETING_LENGTH,
            field_label="备选开场白",
        )


class CharacterCardBrief(BaseModel):
    """角色卡（列表项，精简版）。

    ==================== 为什么要单独定义一个精简结构？====================
    角色卡里有 4 个 MEDIUMTEXT 字段（开场白、对话示例、系统提示词、尾注），
    单张卡就可能上百 KB。如果列表接口把 20 张卡的全文都返回，
    响应体轻易上兆，页面打开会明显卡顿。

    所以列表只给「扫一眼就能决定点不点」的信息：
    头像、名字、简介、标签、是否自己的、开场白摘要。
    用户点进某张卡时再调详情接口拿全文 —— 这也是主流 App 的通用做法。

    ★ description 虽然也是 TEXT，但它按语义就是「一句话简介」，
      属于列表必须展示的内容，因此保留。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    avatar_url: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)

    is_public: bool
    #: 当前请求者是否是这张卡的作者。前端据此决定要不要显示「编辑 / 删除」按钮
    is_owner: bool = False
    #: 有多少个叙事会话正在用这张卡（删除前必须让用户知道会牵连多少故事）
    session_count: int = 0

    #: 是否有开场白（列表上显示一个小标记）
    has_greeting: bool = False
    #: 开场白摘要（截断到 120 字）
    greeting_preview: str | None = None

    #: 关联的世界书名字（列表上显示「已关联：《克苏鲁世界》」）
    world_book_name: str | None = None

    created_at: datetime
    updated_at: datetime


class CharacterCardOut(BaseModel):
    """角色卡（详情，完整版）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int

    name: str
    avatar_url: str | None = None
    description: str | None = None

    personality: str | None = None
    background: str | None = None
    speaking_style: str | None = None
    scenario: str | None = None
    example_dialogue: str | None = None

    greeting: str | None = None
    alternate_greetings: list[str] = Field(default_factory=list)

    system_prompt: str | None = None
    post_history_instructions: str | None = None

    tags: list[str] = Field(default_factory=list)
    is_public: bool

    #: ★ 扩展命名空间（extensions.hne.state_schema / initial_state 在这里）。
    #:   卡片编辑界面要用它回填"状态栏格式"，所以详情必须吐出来。
    extensions: dict[str, Any] | None = Field(
        default=None,
        description="扩展命名空间（Character Card V2 的 data.extensions）；"
        "extensions.hne 下是本项目自有字段",
    )

    is_owner: bool = False
    session_count: int = 0

    #: 关联的世界书（精简引用，不含条目正文）。为 null 表示未关联
    world_book: dict[str, Any] | None = Field(
        default=None, description="关联的世界书摘要：{id, name, entry_count, enabled_entry_count}"
    )

    created_at: datetime
    updated_at: datetime


class CharacterCardImport(BaseModel):
    """导入角色卡。

    支持两种业界格式，服务层会自动识别（看有没有 spec 字段）：
      · Character Card V2：{"spec": "chara_card_v2", "spec_version": "2.0", "data": {...}}
      · Character Card V1：扁平结构 {"name": ..., "first_mes": ...}

    ★ 如果卡里带了 character_book（世界书），会被**独立建成一本世界书**并自动关联，
      而不是塞进卡片的 extra_data 里 —— 这样它才能被多张卡复用，
      删卡时也能选择保留。详见 app/db/models/world_book.py。

    ★ PNG 形式的角色卡请走另一个接口：POST /character-cards/import-png
    """

    card: dict[str, Any] = Field(
        ...,
        description="角色卡 JSON 原文（V2 规范或 V1 扁平格式均可）",
    )
    is_public: bool = Field(
        default=False, description="导入后是否直接设为公开"
    )
    name_override: str | None = Field(
        default=None,
        max_length=100,
        description="覆盖卡片自带的名字（用于导入同名卡时区分）",
    )


class CharacterCardDeleteResult(BaseModel):
    """删除角色卡的执行摘要。

    ★ 为什么要返回一个摘要，而不是简单地回 null？
      删除这张卡会牵连两样东西（会话、世界书），而用户可能只选择删其中一样；
      更特殊的是：**世界书有可能想删却删不掉**（还有别的卡在用它）。
      这些情况都必须如实回报，否则用户会以为「我勾了删除，应该删干净了」，
      实际世界书还在 —— 又是一种静默误导。
    """

    deleted_sessions: int = Field(
        default=0, description="一并删除的叙事会话数量（含其全部对话记录）"
    )
    deleted_world_book: bool = Field(
        default=False, description="关联的世界书是否已被删除"
    )
    world_book_kept: bool = Field(
        default=False, description="是否存在「想删但因故保留」的世界书"
    )
    world_book_kept_reason: str | None = Field(
        default=None, description="世界书被保留的原因（供界面直接显示给用户）"
    )


class CharacterCardExport(BaseModel):
    """导出角色卡（Character Card V2 规范结构）。

    ★ system_prompt 等字段一律导出为字符串而不是 null：
      规范定义它们是 string 类型，如果给出 null，
      有些严格的导入器会直接报错。所以缺省值填 ""，符合规范要求。
    """

    spec: str = "chara_card_v2"
    spec_version: str = "2.0"
    data: dict[str, Any]
