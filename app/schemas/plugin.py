"""插件的请求 / 响应模型。

==================== 四种插件的配置形状 ====================
    regex  : {"rules": [{"pattern": "…", "replacement": "…", "flags": "i"}, …]}
             —— 对**发给模型的提示词**做替换（不改数据库、不改界面显示）
    prompt : {"position": "start|before_guard|end", "content": "…"}
             —— 往系统提示词里插一段固定文本
    css    : {"css": "…"}
             —— 注入控制台样式（纯样式）
    dice   : {"triggers": ["/r", "掷骰"], "default_expr": "1d100",
              "allow_model_roll": true, "show_detail": true, "explain": true,
              "max_dice": 100, "max_sides": 1000}
             —— 跑团骰点：用户用指令掷、模型用 `<roll>` 标签请求系统掷，
                **由后端受控求值**（不执行任何代码），点数是既成事实

★ 配置的具体校验在 `app/services/plugin_service.py`：
  手工新建与"从 GitHub 安装"两条路必须走**同一套**校验，
  否则就会出现"手写的能存、下载的说非法"这种两套标准。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PluginKind = Literal["regex", "prompt", "css", "dice"]
#: 提示词注入的位置：开头 / 身份守卫之前 / 整条提示词末尾（越靠后权重越高）
PromptPosition = Literal["start", "before_guard", "end"]


class PluginCreate(BaseModel):
    """手工新建插件。"""

    name: str = Field(..., min_length=1, max_length=120, description="插件名")
    kind: PluginKind = Field(..., description="regex / prompt / css")
    description: str | None = Field(default=None, max_length=500)
    version: str | None = Field(default=None, max_length=32)
    priority: int = Field(default=100, ge=0, le=10000, description="应用顺序（小的先）")
    enabled: bool = True
    config: dict[str, Any] = Field(..., description="配置本体，结构随 kind 而定")


class PluginUpdate(BaseModel):
    """修改插件（只传要改的字段）。

    ★ 用 `model_fields_set` 区分"没传"和"显式传 null"，
      与本项目其它 PATCH 接口保持同一套语义。
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    version: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    config: dict[str, Any] | None = None


class PluginInstall(BaseModel):
    """从 GitHub 安装插件。"""

    url: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="GitHub 上插件 JSON 的地址（网页地址或 raw 地址都可以，会自动转换）",
    )


class PluginOut(BaseModel):
    """插件详情。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: PluginKind
    description: str | None = None
    version: str | None = None
    author: str | None = None
    source_url: str | None = None
    is_builtin: bool = False
    enabled: bool = True
    priority: int = 100
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: 给界面看的一句话摘要（例如"3 条替换规则""位置：末尾"），省得前端自己解析 config
    summary: str = ""


class PluginCatalogItem(BaseModel):
    """内置示例目录里的一条（灵感来自 SillyTavern 的内置扩展，但只包含声明式能做的）。"""

    key: str
    name: str
    kind: PluginKind
    source: str = ""
    description: str = ""
    summary: str = ""
    installed: bool = False
    #: 已添加，但库里那份内容与**当前目录**不同 = 有更新可用（插件内容是添加时拷贝的）
    update_available: bool = False


class UnsupportedExtension(BaseModel):
    """SillyTavern 有、但本项目**刻意不做**的内置扩展（如实列出）。"""

    name: str
    reason: str


class PluginListOut(BaseModel):
    """列表响应：插件 + 内置示例 + 一句安全边界说明（界面直接显示）。"""

    model_config = ConfigDict(from_attributes=True)

    items: list[PluginOut] = Field(default_factory=list)
    total: int = 0
    #: 内置示例目录（点了「添加」才会变成自己的插件）
    catalog: list[PluginCatalogItem] = Field(default_factory=list)
    #: SillyTavern 里需要执行代码 / 接外部服务、本项目不做的内置扩展
    unsupported: list[UnsupportedExtension] = Field(default_factory=list)
    #: 允许安装的来源（只允许 GitHub，界面据此给出提示）
    allowed_hosts: list[str] = Field(default_factory=list)
    security_note: str = ""


class PluginInstallResult(BaseModel):
    """安装结果：既回插件本身，也回"从哪个地址下下来的、清单里有什么"。"""

    plugin: PluginOut
    source_url: str
    fetched_bytes: int = 0
    manifest_keys: list[str] = Field(default_factory=list)
