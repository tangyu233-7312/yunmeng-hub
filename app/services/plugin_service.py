"""插件的增删改查、配置校验、以及"从 GitHub 安装"。

==================== 安全边界（读代码前先看这段）====================
1. **只允许 GitHub**：安装时只接受 `github.com` / `raw.githubusercontent.com` /
   `gist.github.com` / `gist.githubusercontent.com` 的 https 地址
   （网页地址与 gist 页面地址会自动转成 raw 地址）。其它域名一律拒绝，
   并且在**跟随重定向之后**再校验一次最终域名（且只认 raw 类域名）——
   否则一个 302 就能把"只允许 GitHub"绕过去。
2. **只下载数据，不执行代码**：插件清单是 JSON，能力只有三种（正则/注入/CSS），
   没有 JS 执行入口，因此插件拿不到 API Key、发不出请求、读不到会话。
3. **体积与条数都有上限**：清单 ≤ 256KB、正则规则 ≤ 50 条、单条正则 ≤ 300 字、
   CSS ≤ 20KB。超限直接拒绝，不"截断后照用"（截断会静默改变语义）。
4. **失败隔离**：下载/解析/校验任何一步出错都只影响这次安装，
   已装好的插件、正在进行的对话都不受影响。
5. CSS 会过一遍白名单清洗（挡 `</style>` 逃逸、`@import` 拉远程表、
   `expression()`、`javascript:`、`behavior:`）。
"""

from __future__ import annotations

import json
import random
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, NotFoundError
from app.db.models import Plugin, User
from app.db.models.plugin import PLUGIN_KINDS

# ==================================================================
#  常量
# ==================================================================
#: 允许用户**粘贴**的域名（只允许 GitHub，含 gist —— gist 也是 GitHub 的东西）
ALLOWED_HOSTS = (
    "github.com",
    "raw.githubusercontent.com",
    "gist.github.com",
    "gist.githubusercontent.com",
)
#: 真正会去**下载**的域名：只有 raw 类地址（页面地址会先被规范化成 raw）
FETCH_HOSTS = ("raw.githubusercontent.com", "gist.githubusercontent.com")
#: 清单体积上限
MAX_MANIFEST_BYTES = 256 * 1024
#: 下载超时（秒）
FETCH_TIMEOUT = 10.0
#: 授权协议标识：清单里写了就校验，没写也接受（尽力而为，不强制）
MANIFEST_SPEC = "hne_plugin_v1"

MAX_RULES = 50
MAX_PATTERN_CHARS = 300
MAX_REPLACEMENT_CHARS = 2000
MAX_CSS_CHARS = 20 * 1024
MAX_PROMPT_CHARS = 8 * 1024
#: 骰子插件：触发词个数 / 单个触发词长度 / 默认表达式长度
MAX_TRIGGERS = 8
MAX_TRIGGER_CHARS = 8
MAX_EXPR_CHARS = 60

_SECURITY_NOTE = (
    "插件只支持四种声明式能力（正则替换 / 提示词注入 / CSS 主题 / 跑团骰点），"
    "**不执行任何第三方 JS**，因此拿不到你的 API Key、也读不到你的会话内容。"
    "骰点由服务端受控求值（自己写的解析器，不用 eval、不放开 JS），"
    "点数与骰面会记进消息里，可复核。"
    "安装来源只允许 GitHub（仓库与 gist 都行，gist 是最轻的分享方式）；"
    "CSS 里的 url() 仍可能让作者看到你的 IP（与角色卡美化同一取舍）。"
)

#: 默认插件：两个空壳，**不预置任何规则**（可停用/可删除，删了不会自动重建）
DEFAULT_PLUGIN_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "正则替换（示例）",
        "kind": "regex",
        "description": "把发给模型的提示词里某些词换掉。默认没有任何规则，自己加一条试试。",
        "config": {"rules": []},
        "is_builtin": True,
        "priority": 100,
    },
    {
        "name": "提示词注入（示例）",
        "kind": "prompt",
        "description": "往系统提示词里插一段固定文本（文风约束、称呼统一等）。默认内容为空。",
        "config": {"position": "end", "content": ""},
        "is_builtin": True,
        "priority": 110,
    },
)

# ==================================================================
#  内置示例目录（对照 SillyTavern 的内置扩展挑出来的"能声明式表达"的那些）
# ==================================================================
#  云梦枢主题的背景：星点 + 星云（用代码生成，理由见 _starfield_css）
# ==================================================================
#: 三档星点：(x, y, 直径px, 透明度)。坐标与对应 tile 尺寸配套。
#: ★ 分三档是为了层次：小暗点铺底（多）、中等点（中）、亮点带光晕（少）。
#:   只有一档等大的点，看着像噪点而不是星空（这是用户"背景还是一片黑"之后的第二版）。
_STARS_SMALL = [
    (22, 34, 1.8, 0.34), (148, 96, 1.7, 0.30), (84, 150, 1.9, 0.36), (212, 44, 1.8, 0.32),
    (38, 108, 1.7, 0.28), (176, 22, 1.9, 0.34), (120, 180, 1.8, 0.30), (66, 76, 1.7, 0.28),
    (232, 128, 1.9, 0.32), (14, 176, 1.8, 0.30), (96, 8, 1.7, 0.28), (256, 88, 1.8, 0.32),
    (40, 196, 1.7, 0.28), (168, 140, 1.9, 0.34), (204, 178, 1.8, 0.30), (8, 60, 1.7, 0.28),
    (132, 108, 1.8, 0.30), (188, 66, 1.7, 0.28), (60, 28, 1.9, 0.32), (236, 8, 1.8, 0.30),
    (104, 214, 1.7, 0.28), (280, 132, 1.8, 0.30), (156, 196, 1.9, 0.32), (24, 130, 1.7, 0.28),
]
_STARS_MEDIUM = [
    (52, 74, 2.4, 0.52), (198, 152, 2.6, 0.56), (118, 34, 2.3, 0.48), (286, 214, 2.5, 0.54),
    (86, 242, 2.4, 0.50), (240, 106, 2.6, 0.56), (30, 178, 2.3, 0.48), (162, 288, 2.5, 0.52),
    (322, 60, 2.4, 0.50), (68, 318, 2.3, 0.46),
]
_STARS_BRIGHT = [
    (140, 118, 3.4, 0.82), (44, 268, 3.0, 0.70), (268, 40, 3.2, 0.74),
    (216, 232, 3.6, 0.86), (96, 190, 3.0, 0.68), (330, 168, 3.2, 0.72),
]
_STAR_TILE_SMALL = 190
_STAR_TILE_MEDIUM = 300
_STAR_TILE_BRIGHT = 460


def _starfield_css() -> str:
    """生成 `body` 的背景声明：深空底 + 星点（三档） + 两团星云。

    ★ 为什么用**代码生成**而不是手写几十行：每一层星点在 `background-image`、
      `background-size`、`background-repeat` 三个列表里都要各占一个位置。
      手写极容易让三个列表长度不一致 —— 那会让浏览器整条声明解析失败或错位平铺
      （本项目真的踩过"用简写重置了 background-size"这个坑，见 §30.2c）。
      这里三个列表由**同一份数据**生成，长度天然一致。
    ★ 星云放在最前两层（`no-repeat` + 超大 tile，永远不会看到重复），
      星点在后（各自 `repeat`，用不同 tile 尺寸错开，看不出规律）。
    ★ 尺寸/透明度是**量出来的**，不是感觉：见 docs/handoff.md §30.2e 的三版对照表。
    """
    layers: list[str] = [
        "radial-gradient(1250px 780px at 8% -12%,rgba(167,139,250,.085),transparent 60%)",
        "radial-gradient(1000px 700px at 108% 112%,rgba(0,229,255,.075),transparent 58%)",
    ]
    sizes = ["2400px 1500px", "2400px 1500px"]
    repeats = ["no-repeat", "no-repeat"]

    def add(dots: list[tuple[int, int, float, float]], tile: int, colour: str, halo: bool) -> None:
        for x, y, d, a in dots:
            stop = f"rgba({colour},{a})"
            # 亮点多给一层"光晕"，看着才像星星而不是色块
            middle = f",rgba({colour},{round(a * 0.22, 3)}) 52%" if halo else ""
            layers.append(
                f"radial-gradient({d}px {d}px at {x}px {y}px,{stop}{middle},transparent 68%)"
            )
            sizes.append(f"{tile}px {tile}px")
            repeats.append("repeat")

    add(_STARS_SMALL, _STAR_TILE_SMALL, "255,255,255", False)
    add(_STARS_MEDIUM, _STAR_TILE_MEDIUM, "214,206,255", False)
    add(_STARS_BRIGHT, _STAR_TILE_BRIGHT, "255,255,255", True)

    return (
        "background-color:var(--bg);"
        f"background-image:{','.join(layers)};"
        f"background-size:{','.join(sizes)};"
        f"background-repeat:{','.join(repeats)};"
        "background-attachment:fixed;"
    )


# ==================================================================
#  ★ 挑选原则：只收录**本项目四种声明式能力能真做到**的东西。
#    SillyTavern 的内置扩展里，TTS / 图片生成 / 图片描述 / 快捷回复 /
#    自动摘要 / 聊天向量化 / 令牌计数这些要么必须执行 JS、要么必须接外部服务，
#    本项目的插件**不执行任何第三方代码**，所以一律不做 —— 但不装看不见：
#    下面 UNSUPPORTED_ST_EXTENSIONS 会在界面上如实列出来（并说明"本项目已有原生替代"）。
#    「掷骰」在 ST 里是 JS 扩展，这里改成了声明式骰子插件（服务端受控求值）。
#  ★ 这些条目只是"目录"，**不会自动生效**：用户点了「添加」才会变成自己的插件。
PLUGIN_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "key": "trpg_dice",
        "name": "跑团骰点（Dice）",
        "kind": "dice",
        "source": "SillyTavern 内置扩展 · Dice（本项目改为声明式实现）",
        "description": (
            "掷骰**由后端受控求值**：玩家用 `/r 1d20+5`（或 `掷骰 2d6`）自己掷，"
            "模型也能写 `<roll>1d20</roll>` 让系统掷 —— 大模型没有真随机，"
            "点数必须由系统给。支持 kh/kl 取高取低、爆炸骰、括号与成功判定。"
        ),
        "config": {
            "triggers": ["/r", "/roll", "掷骰", "投掷"],
            "default_expr": "1d100",
            "allow_model_roll": True,
            "show_detail": True,
            "explain": True,
            "max_dice": 100,
            "max_sides": 1000,
        },
    },
    {
        "key": "authors_note",
        "name": "作者注（Author's Note）",
        "kind": "prompt",
        "source": "SillyTavern 内置功能 · Author's Note",
        "description": "把一段「基调/风格」要求钉在提示词最末尾（权重最高）。内容按需改。",
        "config": {
            "position": "end",
            "content": "【作者注】保持叙事连贯与克制：一次只推进一小段，不替玩家决定他的行动。",
        },
    },
    {
        "key": "objective",
        "name": "当前目标（Objective）",
        "kind": "prompt",
        "source": "SillyTavern 可安装扩展 · Objective",
        "description": "给模型一个本场景要达成的目标，它会朝这个方向推进剧情。",
        "config": {
            "position": "end",
            "content": "【当前目标】让玩家在本场景内做出一个会带来后果的选择，并让后果立刻可见。",
        },
    },
    {
        "key": "zh_reply",
        "name": "始终用中文回复",
        "kind": "prompt",
        "source": "SillyTavern 内置扩展 · Chat Translation（思路）",
        "description": "不接翻译 API，只用一条提示词约束输出语言：正文一律简体中文。",
        "config": {
            "position": "end",
            "content": "【输出语言】无论上下文里出现什么语言，正文一律用简体中文书写。",
        },
    },
    {
        "key": "strip_markdown",
        "name": "清理 Markdown 强调符号",
        "kind": "regex",
        "source": "SillyTavern 内置扩展 · Regex（常用规则）",
        "description": "把历史里模型爱写的 **加粗** / *斜体* 记号去掉，免得后续回合越写越多。",
        "config": {
            "rules": [
                {"pattern": r"\*\*(.+?)\*\*", "replacement": r"\1", "flags": "s"},
                {"pattern": r"(?<!\*)\*([^*\n]+)\*(?!\*)", "replacement": r"\1", "flags": ""},
            ]
        },
    },
    {
        "key": "dark_theme",
        "name": "深色主题（Dark Lite 风格）",
        "kind": "css",
        "source": "SillyTavern 内置主题（配色思路）",
        "description": "把控制台换成深色：底色、面板、边框、文字一起换，只动样式不动数据。",
        "config": {
            "css": (
                ":root{"
                "--bg:#12161c;--panel:#1a2029;--border:#2a323d;--border-strong:#3a4553;"
                "--text:#e6eaf0;--text-dim:#a3adbb;--text-faint:#7b8695;"
                "--brand:#7c9cff;--brand-dark:#6b86e0;--brand-soft:#1e2740;"
                "--ok:#4ec38a;--ok-soft:#16301f;--warn:#e0b357;--warn-soft:#332a15;"
                "--danger:#ef6d5a;--danger-soft:#3a1f1c;"
                # ★ 第十六轮：表面变量与提示文字色也要换 —— 以前这里只换了上面那些，
                #   而组件里写死的 `#fff` / `#fafbfc` 让深色下冒出成片白块
                #   （用户反馈"你注意那行白色的"）。现在组件全部走变量，这里必须给全。
                "--surface:#1b2230;--surface-2:#161c26;--surface-3:#161c26;"
                "--info-text:#a9bcff;--warn-text:#e6c579;--danger-text:#f0a396;--ok-text:#7fd8ab;"
                "--shadow:0 1px 3px rgba(0,0,0,.5);--shadow-lg:0 12px 32px rgba(0,0,0,.6);}"
                "body{background:var(--bg);color:var(--text);}"
                ".topbar{background:var(--panel);}"
            )
        },
    },
    {
        "key": "yunmeng_nebula",
        "name": "云梦枢 · 星云暗涌",
        "kind": "css",
        "source": "本项目新增（云梦枢默认视觉方案）",
        "description": (
            "沉浸式深空主题：深空底 + 两团极弱星云（左上紫、右下青）、面板毛玻璃、"
            "量子紫主色 + 极光青交互色；文字用柔和灰白、不做大面积色块，长时间读故事不刺眼。"
            "只改样式，不动数据。"
        ),
        "config": {
            # ★ 背景那一块由 `_starfield_css()` 生成（几十层星点，三个列表必须等长），
            #   所以整段 CSS 是"字符串 + 生成结果 + 字符串"拼起来的。
            "css": (
                """/* ================= 云梦枢 · 星云暗涌 =================
   设计原则（"沉浸，但不打扰阅读"）：
   · 深空底 + 两团极弱星云（左上紫 / 右下青，固定不动）。装饰只在外围，正文区永远最高对比度。
   · 面板走毛玻璃：半透明 + backdrop-filter，让星云透出来一点点，而不是死板的纯色块。
   · 文字一律柔和灰白（#e8eaf2），不用纯白 —— 白字压在深底上久看会累。
   · 配色分工：**量子紫 #A78BFA = 状态**（选中、"我"的声音），
     **极光青 #00E5FF = 交互**（悬停、聚焦、AI 侧微光）。颜色只出现在细线/描边/微光上，
     不做大面积色块（大色块会抢走对文字的注意力）。
   · 不做动效：这一版刻意不加过渡与脉冲，避免"晃眼"。
   ★ 完整变量：本主题把 --surface / --*-border / --catalog-accent 都给全了，
     所以引擎的"补缺"逻辑不会再多加任何东西（用户仍可自己覆盖任意一项）。 */
:root{
--bg:#0b0f19;
--panel:rgba(32,34,54,.42);
--surface:rgba(32,34,54,.42);
--surface-2:rgba(20,22,38,.55);
--surface-3:rgba(42,45,70,.38);
--border:rgba(255,255,255,.09);
--border-strong:rgba(255,255,255,.17);
--text:#e8eaf2;--text-dim:#a6abc0;--text-faint:#7c8298;
--brand:#a78bfa;--brand-dark:#8b6cf0;--brand-soft:rgba(167,139,250,.16);
--catalog-accent:#c4b5fd;
--ok:#5fd0a0;--ok-soft:rgba(78,195,138,.13);--ok-text:#8fe0bb;--ok-border:rgba(95,208,160,.32);
--warn:#e0b357;--warn-soft:rgba(224,179,87,.12);--warn-text:#eccb8c;--warn-border:rgba(224,179,87,.30);
--danger:#ef6d5a;--danger-soft:rgba(239,109,90,.12);--danger-text:#f3a99c;--danger-border:rgba(239,109,90,.30);
--info-text:#c4b5fd;--info-border:rgba(167,139,250,.30);
--shadow:0 1px 2px rgba(0,0,0,.5);
--shadow-lg:0 18px 48px rgba(0,0,0,.55);
/* ★ 极光青：只在"交互态"出现（悬停 / 聚焦 / 高亮边缘）。
   于是配色有了分工：**量子紫 = 状态（选中、我是谁），极光青 = 交互（我正在碰它）**。
   ★ 只在本主题内使用，不进 styles.css 的 :root —— 否则其它主题都得跟着定义它。 */
--aurora:#00e5ff;
}
/* 背景：深空底 + **三档星点** + 两团极弱星云（左上紫 / 右下青），全部在最底层。
   ★ 星点用"平铺小径向渐变"实现（纯 CSS、零请求、不落 DOM、不拦截鼠标）：
     三档各自不同 tile 尺寸（190 / 300 / 460px），叠加后看不出重复的规律感。
   ★ 分三档是为了**层次**：小暗点铺底（24 个）、中等点（10 个）、亮点带光晕（6 个）。
     只有一档等大的点，看着像噪点而不是星空 —— 用户两轮反馈"背景看不出变化"之后，
     我量了像素才发现：不是没画，是**又小又稀**（145×260 的区域里只有 4~5 个灰点）。
   ★ 尺寸/透明度是**量出来**的（见 docs/handoff.md §30.2e 三版对照）：
     全部星点**暗于正文文字**（230），最亮的带光晕点渲染峰值约 150。
   ★ 这片背景由 `_starfield_css()` 生成：三个列表（image/size/repeat）由同一份数据来，
     长度天然一致（手写几十层极易让三个列表错位，浏览器会直接丢弃整条声明）。 */
"""
            + "body{" + _starfield_css() + "color:var(--text);}\n"
            + """/* 顶栏：半透明 + 底部一条极细的"紫→青"光带（全页唯一"发光"的地方） */
.topbar{background:rgba(11,14,24,.66);border-bottom:1px solid var(--border);
backdrop-filter:blur(16px) saturate(1.1);-webkit-backdrop-filter:blur(16px) saturate(1.1);}
.topbar::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;
background:linear-gradient(90deg,transparent,rgba(167,139,250,.60),rgba(0,229,255,.45),transparent);}
/* 「云梦枢」三个字与图标的"星辉"：极淡的紫光，不刺眼 */
.brand-text{color:#f2efff;text-shadow:0 0 12px rgba(167,139,250,.55),0 0 26px rgba(0,229,255,.22);}
.brand-logo{filter:drop-shadow(0 0 10px rgba(167,139,250,.55)) drop-shadow(0 2px 8px rgba(0,229,255,.22))
drop-shadow(0 1px 2px rgba(0,0,0,.5));}
/* 导航：选中 = 淡紫底 + **由中间向两边渐隐的极光青细线**（比实心下划线更"星辉"）；
   悬停 = 极光青（交互色） */
.nav-item{color:#9aa0b6;}
.nav-item:hover{background:rgba(0,229,255,.07);color:#d8fbff;}
.nav-item.active{
background-color:rgba(167,139,250,.14);color:#e6dcff;
background-image:linear-gradient(90deg,transparent,rgba(0,229,255,.95),transparent);
background-repeat:no-repeat;background-position:bottom center;background-size:92% 2px;
box-shadow:0 6px 18px -10px rgba(167,139,250,.7);}
/* 毛玻璃面板：卡片 / 弹窗 / 会话项 / 输入区 */
.panel,.cc,.modal,.session-item,.chat-main,.composer,.alt-box,.blocks-table{
backdrop-filter:blur(14px) saturate(1.05);-webkit-backdrop-filter:blur(14px) saturate(1.05);}
.cc{background:linear-gradient(180deg,rgba(42,45,70,.5),rgba(24,26,44,.42));
border-top-color:rgba(167,139,250,.50);}
/* "魔法卡牌"悬停：微微上浮 3px + 边框泛出紫色光晕（幅度刻意很小：
   浮得太多会像廉价动效，1px 边框的"泛光"才是那种"牌在灯光下"的感觉） */
.cc:hover{transform:translateY(-3px);
border-color:rgba(167,139,250,.45);border-top-color:rgba(196,181,253,.75);
box-shadow:0 0 0 1px rgba(167,139,250,.28),0 22px 46px -26px rgba(167,139,250,.85);}
/* 卡片头像：方形→**圆形** + 深空渐变（与左侧会话头像同一套语言） */
.cc-avatar{border-radius:50%;
background:radial-gradient(120% 120% at 30% 20%,rgba(167,139,250,.42),rgba(12,16,28,.95));
color:#e2d8ff;
box-shadow:inset 0 0 0 3px rgba(11,15,25,.85),0 0 14px -6px rgba(167,139,250,.55);}
.cc-greeting{background:linear-gradient(180deg,rgba(20,22,38,.55),rgba(16,18,32,.45));
color:var(--text-dim);border-left-color:rgba(167,139,250,.60);}
/* 会话列表：未选中保持透明；选中 = 半透明紫底 + 最左一条 3px 紫色实线；
   头像改成**圆形**、深空渐变底（原来的方块/亮色与星空紫调不搭） */
.session-item{background:transparent;border-color:transparent;}
.session-item:hover{background:rgba(0,229,255,.05);border-color:rgba(0,229,255,.14);}
.session-item.active{background:rgba(167,139,250,.15);
border-color:rgba(167,139,250,.30);box-shadow:inset 3px 0 0 #a78bfa;}
.si-avatar{border-radius:50%;
background:radial-gradient(120% 120% at 30% 20%,rgba(167,139,250,.40),rgba(12,16,28,.92));
color:#e9e2ff;border:1px solid rgba(167,139,250,.28);}
.session-item.active .si-avatar{background:radial-gradient(120% 120% at 30% 20%,#c4b5fd,#7c5cf0);
color:#0b0f19;border-color:rgba(196,181,253,.7);}
/* 对话区：两种气泡给出**颜色上的**区分（不只是左右位置）——
   玩家侧：紫色边缘（那是"我"的声音，与品牌色一致）；
   AI 侧：极微弱的**青色发光**边缘（那是"对面"的声音，冷一点、退后一点）。 */
.chat-head{background:rgba(11,14,24,.34);}
.chat-main{background:rgba(11,14,24,.28);}
.messages{background:transparent;}
.msg.user .msg-body{background:linear-gradient(180deg,rgba(167,139,250,.16),rgba(120,92,220,.12));
color:#efeaff;border:1px solid rgba(167,139,250,.38);
box-shadow:0 10px 26px -22px rgba(167,139,250,.5);}
.msg.assistant .msg-body{background:rgba(22,24,40,.62);border:1px solid rgba(0,229,255,.18);
box-shadow:0 0 0 1px rgba(0,229,255,.06),0 10px 26px -22px rgba(0,0,0,.9);}
.composer{background:rgba(11,14,24,.6);}
#composer-input{background:rgba(9,12,20,.7);border-color:var(--border);color:var(--text);}
#composer-input:focus{border-color:rgba(0,229,255,.55);box-shadow:0 0 0 3px rgba(0,229,255,.12);}
/* 表单 / 表格 / 弹窗 */
input,select,textarea{background:rgba(12,15,26,.6);border-color:var(--border-strong);color:var(--text);}
input:focus,select:focus,textarea:focus{border-color:rgba(0,229,255,.5);
box-shadow:0 0 0 3px rgba(0,229,255,.10);}
table.list th{background:rgba(255,255,255,.035);color:var(--text-dim);}
table.list tr:hover td{background:rgba(0,229,255,.04);}
.modal-mask{background:rgba(5,7,13,.62);}
.modal{background:rgba(17,19,32,.86);border:1px solid var(--border);}
.modal-head,.modal-foot{border-color:var(--border);}
.modal-foot{background:rgba(11,14,24,.5);}
.segmented button.active{background:rgba(167,139,250,.20);color:#e9e2ff;
box-shadow:inset 0 -2px 0 var(--brand);}
.btn.sec{background:rgba(255,255,255,.05);border-color:var(--border-strong);color:var(--text);}
.btn.sec:hover{background:rgba(0,229,255,.10);border-color:rgba(0,229,255,.42);color:#d8fbff;}
.btn{background:linear-gradient(180deg,rgba(167,139,250,.95),rgba(139,108,240,.95));color:#0b0f19;}
.btn:hover{filter:brightness(1.07);box-shadow:0 6px 20px -10px rgba(0,229,255,.6);}
/* 状态栏 / 骰子 / 提示条：低亮度低饱和，它们只是"信息"，不该抢戏。
   ★ 例外：状态栏那条"还没有状态…"的提示给一圈**极弱青色外发光** ——
     它是这个项目的创新点（状态探针），值得让用户第一眼就注意到它的存在与变化；
     但只用 10px / 15% 的辉光，不做闪烁（闪烁会把人从正文里拽出来）。 */
.state-bar{background:rgba(17,19,32,.4);box-shadow:0 0 10px rgba(0,229,255,.15);}
.state-bar.empty{background:rgba(17,19,32,.32);box-shadow:0 0 12px rgba(0,229,255,.18);}
.roll-chip{background:rgba(167,139,250,.12);border-color:rgba(167,139,250,.30);color:#e2d8ff;}
.alert{background:rgba(255,255,255,.03);}
/* 滚动条：细、暗，不抢注意力；悬停用极光青 */
*::-webkit-scrollbar{width:10px;height:10px;}
*::-webkit-scrollbar-track{background:transparent;}
*::-webkit-scrollbar-thumb{background:rgba(255,255,255,.10);border-radius:8px;
border:3px solid transparent;background-clip:content-box;}
*::-webkit-scrollbar-thumb:hover{background:rgba(0,229,255,.30);background-clip:content-box;}
/* 登录页同一套星云，"第一眼"就统一 */
.auth-wrap{background:radial-gradient(1100px 700px at 20% -10%,rgba(167,139,250,.16),transparent 60%),
radial-gradient(900px 600px at 110% 110%,rgba(0,229,255,.12),transparent 58%),#080b14;}
.auth-box{background:rgba(17,19,32,.82);border:1px solid var(--border);}
.auth-logo{filter:drop-shadow(0 4px 14px rgba(167,139,250,.45));}
"""
            ),
        },
    },
    {
        "key": "big_text",
        "name": "大字号 · 宽松行距（护眼）",
        "kind": "css",
        "source": "本项目新增（无障碍）",
        "description": "正文调大一号、行距放宽，长时间读故事更舒服。",
        "config": {
            "css": (
                "body{font-size:15.5px;}"
                ".msg-body{line-height:1.95;font-size:15px;}"
                ".session-item{line-height:1.6;}"
            )
        },
    },
)

#: SillyTavern 里**本项目不做**的内置扩展（需要执行代码或接外部服务），界面上如实列出。
#: 结构：(名称, 为什么不做 / 本项目有什么替代)
UNSUPPORTED_ST_EXTENSIONS: tuple[tuple[str, str], ...] = (
    ("Text To Speech（语音朗读）", "需要调用 TTS 服务并播放音频，得执行代码"),
    ("Image Generation / Image Captioning（生图与看图）", "需要接外部图像 API"),
    ("Expression Images（表情立绘）", "需要在对话旁叠加图片资源"),
    ("Quick Reply（快捷回复/脚本）", "本质是脚本执行器 —— 本项目插件不执行第三方代码"),
    ("Summarize（自动摘要）", "已有原生实现：上下文裁剪时自动写滚动摘要"),
    ("Chat Vectorization（聊天向量化）", "已有原生实现：长期记忆按语义召回"),
    ("Token Counter（令牌计数）", "已有原生实现：每条消息与每轮都有 token 统计"),
)



# ==================================================================
#  校验
# ==================================================================
def _as_text(value: Any, *, limit: int, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BadRequestError(f"{field} 必须是字符串")
    if len(value) > limit:
        raise BadRequestError(f"{field} 太长（上限 {limit} 字，当前 {len(value)} 字）")
    return value


def sanitize_css(css: str) -> str:
    """清洗 CSS（与角色卡富文本那套规则保持一致，另外挡掉 style 标签逃逸）。

    ★ 为什么必须挡 `</`：这段 CSS 会被塞进页面的 <style> 标签里，
   `</style><script>…` 是最直接的逃逸方式。CSS 里出现 `</` 没有正当用途，
   统一转义成 `<\\/`（在 CSS 里是合法的转义写法，语义不变）。
    """
    text = str(css or "")
    text = re.sub(r"</", "<\\/", text)
    text = re.sub(r"@import[^;]*;?", "", text, flags=re.I)
    text = re.sub(r"expression\s*\(", "", text, flags=re.I)
    text = re.sub(r"javascript\s*:", "", text, flags=re.I)
    text = re.sub(r"behavior\s*:", "", text, flags=re.I)
    return text.strip()


def validate_config(kind: str, config: Any) -> dict[str, Any]:
    """把插件配置校验成"可信的配置"，返回规范化后的副本。

    ★ 手工新建与从 URL 安装走的是**同一个**函数：两套校验迟早会打架。
    """
    if kind not in PLUGIN_KINDS:
        raise BadRequestError(f"插件类型必须是 {' / '.join(PLUGIN_KINDS)} 之一")
    if not isinstance(config, dict):
        raise BadRequestError("插件配置必须是一个 JSON 对象")

    if kind == "regex":
        rules = config.get("rules") or []
        if not isinstance(rules, list):
            raise BadRequestError("regex 插件的 rules 必须是数组")
        if len(rules) > MAX_RULES:
            raise BadRequestError(f"替换规则最多 {MAX_RULES} 条（当前 {len(rules)} 条）")
        cleaned: list[dict[str, str]] = []
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise BadRequestError(f"第 {index + 1} 条规则必须是对象")
            pattern = _as_text(
                rule.get("pattern"), limit=MAX_PATTERN_CHARS, field=f"第 {index + 1} 条 pattern"
            )
            if not pattern:
                raise BadRequestError(f"第 {index + 1} 条规则没有填 pattern")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise BadRequestError(
                    f"第 {index + 1} 条正则写错了：{exc}", detail={"pattern": pattern}
                ) from exc
            flags = _as_text(rule.get("flags"), limit=8, field=f"第 {index + 1} 条 flags")
            if set(flags) - set("imsx"):
                raise BadRequestError(
                    f"第 {index + 1} 条 flags 只支持 i / m / s / x（收到 {flags!r}）"
                )
            cleaned.append(
                {
                    "pattern": pattern,
                    "replacement": _as_text(
                        rule.get("replacement"),
                        limit=MAX_REPLACEMENT_CHARS,
                        field=f"第 {index + 1} 条 replacement",
                    ),
                    "flags": flags,
                }
            )
        return {"rules": cleaned}

    if kind == "prompt":
        position = str(config.get("position") or "end")
        if position not in ("start", "before_guard", "end"):
            raise BadRequestError("提示词注入的位置只能是 start / before_guard / end")
        content = _as_text(config.get("content"), limit=MAX_PROMPT_CHARS, field="content")
        return {"position": position, "content": content}

    if kind == "dice":
        # ★ 就地导入（而不是模块级 import）：骰子求值器 `app.narrative.dice` 是**纯函数模块**
        #   （不认识数据库、不认识服务层），但服务层在模块级依赖叙事层会把分层弄拧。
        #   就地导入一次即可，调用频率是"用户改插件配置"，不在热路径上。
        from app.narrative import dice as dice_mod

        raw_triggers = config.get("triggers")
        if raw_triggers is None:
            raw_triggers = list(dice_mod.DEFAULT_CONFIG["triggers"])
        if not isinstance(raw_triggers, list):
            raise BadRequestError("触发词（triggers）必须是数组")
        triggers: list[str] = []
        for item in raw_triggers:
            token = _as_text(item, limit=MAX_TRIGGER_CHARS, field="触发词").strip()
            if not token:
                continue
            if any(char.isspace() for char in token):
                raise BadRequestError(f"触发词里不能有空格（收到 {token!r}）")
            if token not in triggers:
                triggers.append(token)
        if not triggers:
            raise BadRequestError("至少要留一个触发词（例如 /r）")
        if len(triggers) > MAX_TRIGGERS:
            raise BadRequestError(f"触发词最多 {MAX_TRIGGERS} 个（当前 {len(triggers)} 个）")
        default_expr = _as_text(
            config.get("default_expr") or dice_mod.DEFAULT_CONFIG["default_expr"],
            limit=MAX_EXPR_CHARS,
            field="默认表达式",
        ).strip()
        # ★ 默认表达式**当场掷一次**做语法检查：写错的配置不该等到用户掷骰时才发现
        probe = dice_mod.evaluate(default_expr, rng=random.Random(0))
        if not probe.ok:
            raise BadRequestError(
                f"默认表达式写错了：{probe.error}", detail={"expr": default_expr}
            )
        return {
            "triggers": triggers,
            "default_expr": default_expr,
            "allow_model_roll": bool(config.get("allow_model_roll", True)),
            "show_detail": bool(config.get("show_detail", True)),
            "explain": bool(config.get("explain", True)),
            "max_dice": _as_int(
                config.get("max_dice"), 1, 1000, dice_mod.DEFAULT_CONFIG["max_dice"]
            ),
            "max_sides": _as_int(
                config.get("max_sides"), 2, 100000, dice_mod.DEFAULT_CONFIG["max_sides"]
            ),
        }

    # kind == "css"（唯一剩下的分支，所以不写 if）
    raw_css = config.get("css")
    if raw_css is None:
        raw_css = ""
    if not isinstance(raw_css, str):
        raise BadRequestError("css 插件的 css 必须是字符串")
    if len(raw_css) > MAX_CSS_CHARS:
        raise BadRequestError(f"CSS 太长（上限 {MAX_CSS_CHARS} 字符，当前 {len(raw_css)}）")
    return {"css": sanitize_css(raw_css)}


def _as_int(value: Any, low: int, high: int, fallback: int) -> int:
    """整数字段校验（缺省用 fallback，超范围直接报错而不是悄悄夹住）。"""
    if value is None or value == "":
        return fallback
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequestError(f"这里需要一个整数，收到 {value!r}") from exc
    if number < low or number > high:
        raise BadRequestError(f"数值必须在 {low} ~ {high} 之间（收到 {number}）")
    return number


def summary_of(kind: str, config: dict[str, Any]) -> str:
    """一句话摘要，给界面显示（省得前端各自解析 config）。"""
    if kind == "regex":
        rules = config.get("rules") or []
        return f"{len(rules)} 条替换规则"
    if kind == "prompt":
        position = {"start": "提示词开头", "before_guard": "守卫之前", "end": "提示词末尾"}.get(
            str(config.get("position")), "提示词末尾"
        )
        size = len(str(config.get("content") or ""))
        return f"注入到{position}（{size} 字）" if size else f"注入到{position}（内容为空，暂不生效）"
    if kind == "dice":
        triggers = " / ".join(str(item) for item in config.get("triggers") or []) or "（无）"
        mode = "模型也能请求掷骰" if config.get("allow_model_roll", True) else "只认玩家指令"
        return f"触发词 {triggers}，默认 {config.get('default_expr')}，{mode}"
    size = len(str(config.get("css") or ""))
    return f"主题样式 {size} 字符" if size else "主题样式为空（暂不生效）"


# ==================================================================
#  CRUD
# ==================================================================
def ensure_defaults(db: Session, user_id: int) -> None:
    """保证账号里存在两个默认插件（空壳），**只发一次**。

    ★ 为什么可以做在"列表"里：默认插件是**空壳**，装上也不会改变任何行为；
      而"注册时就建"会让注册流程依赖插件模块，"从别处导入账号"也会漏。
    ★ 用 `users.plugin_defaults_seeded` 当墓碑位：用户把默认插件删光之后，
      下一次访问不能又冒出来 —— 那就不是"可删除"了
      （内置守卫预设用的是同一招，见 users.builtin_preset_dismissed）。
    """
    user = db.get(User, user_id)
    if user is None or bool(getattr(user, "plugin_defaults_seeded", False)):
        return
    for spec in DEFAULT_PLUGIN_SPECS:
        db.add(
            Plugin(
                user_id=user_id,
                name=spec["name"],
                kind=spec["kind"],
                description=spec["description"],
                config=validate_config(spec["kind"], spec["config"]),
                is_builtin=bool(spec["is_builtin"]),
                priority=int(spec["priority"]),
                enabled=True,
            )
        )
    user.plugin_defaults_seeded = True
    db.commit()
    logger.info("初始化默认插件 | user_id={} count={}", user_id, len(DEFAULT_PLUGIN_SPECS))


def list_plugins(db: Session, user_id: int) -> list[Plugin]:
    ensure_defaults(db, user_id)
    return list(
        db.scalars(
            select(Plugin)
            .where(Plugin.user_id == user_id)
            .order_by(Plugin.priority.asc(), Plugin.id.asc())
        )
    )


def load_enabled(db: Session, user_id: int) -> list[Plugin]:
    """给提示词装配用：只取启用中的插件（**不建默认插件**，避免读路径写库）。"""
    return list(
        db.scalars(
            select(Plugin)
            .where(Plugin.user_id == user_id, Plugin.enabled.is_(True))
            .order_by(Plugin.priority.asc(), Plugin.id.asc())
        )
    )


def get_owned(db: Session, user_id: int, plugin_id: int) -> Plugin:
    row = db.scalar(
        select(Plugin).where(Plugin.id == plugin_id, Plugin.user_id == user_id)
    )
    if row is None:
        # 别人的插件一律 404（连"存在"都不透露），与世界书/会话同一套规矩
        raise NotFoundError(f"插件不存在：#{plugin_id}")
    return row


def create_plugin(db: Session, user_id: int, payload: Any) -> Plugin:
    row = Plugin(
        user_id=user_id,
        name=payload.name.strip(),
        kind=payload.kind,
        description=(payload.description or None),
        version=(payload.version or None),
        config=validate_config(payload.kind, payload.config),
        priority=int(payload.priority),
        enabled=bool(payload.enabled),
        is_builtin=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_plugin(db: Session, user_id: int, plugin_id: int, payload: Any) -> Plugin:
    row = get_owned(db, user_id, plugin_id)
    touched = payload.model_fields_set
    if "name" in touched and payload.name:
        row.name = payload.name.strip()
    if "description" in touched:
        row.description = payload.description or None
    if "version" in touched:
        row.version = payload.version or None
    if "enabled" in touched and payload.enabled is not None:
        row.enabled = bool(payload.enabled)
    if "priority" in touched and payload.priority is not None:
        row.priority = int(payload.priority)
    if "config" in touched and payload.config is not None:
        row.config = validate_config(row.kind, payload.config)
    db.commit()
    db.refresh(row)
    return row


def delete_plugin(db: Session, user_id: int, plugin_id: int) -> None:
    row = get_owned(db, user_id, plugin_id)
    db.delete(row)
    db.commit()


def serialize(row: Plugin) -> dict[str, Any]:
    config = row.config if isinstance(row.config, dict) else {}
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "description": row.description,
        "version": row.version,
        "author": row.author,
        "source_url": row.source_url,
        "is_builtin": bool(row.is_builtin),
        "enabled": bool(row.enabled),
        "priority": row.priority,
        "config": config,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "summary": summary_of(row.kind, config),
    }


def theme_css(db: Session, user_id: int) -> str:
    """把所有启用中的 CSS 插件拼成一段样式（界面直接塞进 <style>）。"""
    chunks: list[str] = []
    for row in load_enabled(db, user_id):
        if row.kind != "css":
            continue
        config = row.config if isinstance(row.config, dict) else {}
        css = sanitize_css(str(config.get("css") or ""))
        if css:
            chunks.append(f"/* 插件：{row.name} */\n{css}")
    body = "\n\n".join(chunks)
    shim = _theme_shim(body)
    if shim:
        chunks.append(shim)
    return "\n\n".join(chunks)


def _theme_shim(css: str) -> str:
    """给"老主题"补上后来才加的语义变量。

    ★ 为什么需要：这些变量都是**后加**的（`--surface`/`--surface-2`/`--surface-3`
      以及提示框边框 `--warn-border` 等），而 CSS 插件的内容是用户点「添加」时
      **拷进数据库**的，插件目录里那份升级了，用户库里那份不会自动跟着变 ——
      缺的那一项就停留在**浅色默认值**上（表现为"深色主题下弹窗/卡片又是一块白"
      或"横幅上一条亮线"）。引擎在这里兜住，用户不必"删了重加"。

    ★ 映射规则（只补缺的，用户自己写过的变量一律不动）：
      · 只有 `--panel`（老变量）→ 补 `--surface:var(--panel)`
      · 有 `--surface` 没 `--panel` → 补 `--panel:var(--surface)`
      · 缺 `--surface-2/3` → 顺着 `--surface` 链下来
      · 缺 `--{warn,danger,info,ok}-border` → 用该主题自己的
        "文字色 × 柔和底"的**中间调**补上（`color-mix`），这样它跟着主题一起变深，
        不会是一条亮线；不支持 `color-mix` 的浏览器退回该主题的文字色。
      · 缺 `--catalog-accent` → 跟着该主题的 `--brand`（目录里 ✦ 头像的字色）

    一项都不缺（或主题跟这些变量毫无关系）时不做任何事。
    """
    fixes: list[str] = []
    if "--panel" in css or "--surface" in css:
        if "--surface" not in css:
            fixes.append("--surface:var(--panel)")
        if "--panel" not in css:
            fixes.append("--panel:var(--surface)")
        if "--surface-2" not in css:
            fixes.append("--surface-2:var(--surface)")
        if "--surface-3" not in css:
            fixes.append("--surface-3:var(--surface-2)")

    for name in ("warn", "danger", "info", "ok"):
        var = f"--{name}-border"
        if var in css:
            continue
        text, soft = f"--{name}-text", f"--{name}-soft"
        # 主题连这套语义色都没有 → 不碰（它跟提示框无关）
        if text not in css and soft not in css:
            continue
        base = f"var({text})" if text in css else f"var({soft})"
        mix = f"color-mix(in srgb, {base} 40%, var({soft}))" if soft in css else base
        # 先写退路（老浏览器不认 color-mix 时整条声明会被丢掉，留下这一条）
        fixes.append(f"{var}:{base};{var}:{mix}")

    # 目录里那个 ✦ 头像的字色：主题没写就跟着品牌色走
    # （原来它写死在 JS 内联样式里，深色主题下会压在自己的深色底上、看不清）
    if "--catalog-accent" not in css and "--brand" in css:
        fixes.append("--catalog-accent:var(--brand)")

    if not fixes:
        return ""
    return "/* 引擎补全：老主题缺少的语义变量（用户可以随时在自己插件里覆盖） */\n" + (
        ":root{" + ";".join(fixes) + "}"
    )


def security_note() -> str:
    return _SECURITY_NOTE


def catalog_for(db: Session, user_id: int) -> list[dict[str, Any]]:
    """内置示例目录（带上"这个用户是不是已经添加过 / 是否需要更新"的标记）。

    ★ 为什么需要 `update_available`：插件内容在**添加时就被拷进数据库**了，
      而内置目录（尤其那几条主题）会随版本升级。于是"我改了主题，用户却看不出变化"
      —— 用户看到的还是他当初添加的那份。以前只能让他删掉重加（还得自己发现这件事）。
      现在目录接口会如实告诉他"这条有更新"，界面给一个「更新」按钮。
    """
    rows = {row.name: row for row in list_plugins(db, user_id)}
    items: list[dict[str, Any]] = []
    for spec in PLUGIN_CATALOG:
        config = validate_config(spec["kind"], spec["config"])
        row = rows.get(spec["name"])
        same_kind = row is not None and row.kind == spec["kind"]
        up_to_date = same_kind and (row.config or {}) == config
        items.append(
            {
                "key": spec["key"],
                "name": spec["name"],
                "kind": spec["kind"],
                "source": spec["source"],
                "description": spec["description"],
                "summary": summary_of(spec["kind"], config),
                "installed": row is not None,
                # 同名但**类型不同**的插件是用户自己建的，不当成"内置插件的旧版"
                "update_available": same_kind and not up_to_date,
            }
        )
    return items


def add_from_catalog(db: Session, user_id: int, key: str) -> tuple[Plugin, bool]:
    """把目录里的一条示例加进用户的插件列表；**已经添加过**的则更新到最新内容。

    ★ 目录条目本身**不会自动生效**：必须用户点「添加 / 更新」才会动他的插件。
      这样"内置示例"就不会变成"偷偷改了你的提示词"。
    ★ 返回值第二项 `updated`：True = 这次是把旧版覆盖成最新版（界面据此换文案）。

    三种情况说清楚，不糊在一起：
      · 没添加过 → 新建（201）
      · 添加过、内容与目录不同 → **覆盖成目录里的最新内容**（200）
      · 添加过、已经是最新 → 报「已经是最新版」（400）—— 不假装"又成功了"
    """
    spec = next((item for item in PLUGIN_CATALOG if item["key"] == key), None)
    if spec is None:
        raise NotFoundError(
            f"内置示例里没有 {key!r}",
            detail={"available": [item["key"] for item in PLUGIN_CATALOG]},
        )
    config = validate_config(spec["kind"], spec["config"])
    exists = db.scalar(
        select(Plugin).where(Plugin.user_id == user_id, Plugin.name == spec["name"])
    )
    if exists is not None:
        if exists.kind != spec["kind"]:
            raise BadRequestError(
                f"你已有一个同名的「{exists.kind}」插件，内置示例是「{spec['kind']}」类型，"
                "不会覆盖它 —— 请先改名或删掉你自己的那个"
            )
        if (exists.config or {}) == config:
            raise BadRequestError(f"「{spec['name']}」已经是最新版")
        exists.config = config
        exists.description = spec["description"]
        db.commit()
        db.refresh(exists)
        logger.info(
            "从内置目录更新插件 | user_id={} key={} plugin_id={}", user_id, key, exists.id
        )
        return exists, True

    row = Plugin(
        user_id=user_id,
        name=spec["name"],
        kind=spec["kind"],
        description=spec["description"],
        version=None,
        author=None,
        source_url=None,
        is_builtin=False,
        enabled=True,
        priority=100,
        config=config,
        raw_manifest=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("从内置目录添加插件 | user_id={} key={} plugin_id={}", user_id, key, row.id)
    return row, False


def unsupported_extensions() -> list[dict[str, str]]:
    """本项目**做不到**的 SillyTavern 内置扩展（如实列出，不装作支持）。"""
    return [{"name": name, "reason": reason} for name, reason in UNSUPPORTED_ST_EXTENSIONS]


# ==================================================================
#  安装：GitHub → 清单 → 插件
# ==================================================================
def normalize_github_url(url: str) -> str:
    """把用户粘的地址规范化成 raw 地址，并做域名白名单校验。

    支持这些写法（都是 GitHub 的）：
        https://github.com/<owner>/<repo>/blob/<ref>/<path>   （仓库网页地址）
        https://github.com/<owner>/<repo>/raw/<ref>/<path>
        https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>
        https://gist.github.com/<user>/<id>                   （单文件 gist，自动加 /raw）
        https://gist.github.com/<user>/<id>?file=demo.json    （多文件 gist 指定文件）
        https://gist.github.com/<user>/<id>#file-demo-json    （锚点也认，见下）
        https://gist.githubusercontent.com/<user>/<id>/raw[/<file>]

    ★ gist 为什么值得支持：它是最轻的分享方式 —— 写一个插件不必建仓库。
      gist 同样是 GitHub 的域名，所以"只允许 GitHub"这条边界没有被放宽；
      而且我们**只下载数据、不执行代码**，gist 与仓库的风险级别是一样的。
    """
    text = str(url or "").strip()
    if not text:
        raise BadRequestError("请填写插件地址")
    parts = urlsplit(text)
    if parts.scheme != "https":
        raise BadRequestError("只允许 https 地址（http 明文可能被中间人篡改）")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise BadRequestError(
            f"只允许从 GitHub 安装（{' / '.join(ALLOWED_HOSTS)}），收到的是 {host or '空域名'}",
            detail={"allowed_hosts": list(ALLOWED_HOSTS)},
        )

    if host == "github.com":
        segments = [s for s in parts.path.split("/") if s]
        # /<owner>/<repo>/blob|raw/<ref>/<path...>
        if len(segments) < 5 or segments[2] not in ("blob", "raw"):
            raise BadRequestError(
                "GitHub 网页地址要形如 https://github.com/用户/仓库/blob/分支/路径.json"
                "（也可以直接用 raw.githubusercontent.com 的地址）"
            )
        owner, repo, _, ref = segments[0], segments[1], segments[2], segments[3]
        rest = "/".join(segments[4:])
        return urlunsplit(
            ("https", "raw.githubusercontent.com", f"/{owner}/{repo}/{ref}/{rest}", "", "")
        )

    if host == "gist.github.com":
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) < 2:
            raise BadRequestError(
                "gist 地址要形如 https://gist.github.com/用户名/gist编号"
                "（也可以直接用 gist.githubusercontent.com 的 raw 地址）"
            )
        user, gist_id = segments[0], segments[1]
        tail = segments[3:] if len(segments) > 2 and segments[2] == "raw" else []
        # 多文件 gist：文件名从 ?file= 或 #file-xxx-yyy 锚点里取
        name = _gist_file_hint(parts)
        if name and not tail:
            tail = [name]
        path = f"/{user}/{gist_id}/raw" + (f"/{'/'.join(tail)}" if tail else "")
        return urlunsplit(("https", "gist.githubusercontent.com", path, "", ""))

    # 已经是 raw 地址：原样通过（顺手去掉 ?plain=1 之类的查询串）
    if not parts.path.strip("/"):
        raise BadRequestError("地址里没有文件路径")
    return urlunsplit(("https", host, parts.path, "", ""))


def _gist_file_hint(parts) -> str:
    """从 gist 地址里猜出"要哪个文件"。

    两种来源：
      · `?file=demo.json` —— 明确、推荐，**原样使用**；
      · `#file-demo-json` —— 页面锚点，GitHub 的规则是"文件名小写、非字母数字换成 -"，
        这是**有损**的，所以只在能安全还原时才用（末尾是 -json / -txt / -md 这类扩展名）。
        还原不了就不猜：gist 的 /raw 默认给第一个文件，多文件时让用户用 ?file= 指明。
    """
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        if key.lower() == "file" and value.strip():
            name = value.strip().lstrip("/")
            # 只允许普通文件名，避免 ../ 之类的路径穿越
            if "/" not in name and "\\" not in name and name not in (".", ".."):
                return name
            raise BadRequestError("?file= 里只能是文件名，不能带路径")
    anchor = (parts.fragment or "").strip()
    if anchor.lower().startswith("file-"):
        guess = anchor[5:]
        for ext in ("json", "txt", "md", "yaml", "yml"):
            if guess.lower().endswith(f"-{ext}"):
                return f"{guess[: -len(ext) - 1]}.{ext}"
    return ""


def fetch_manifest(url: str, *, client: httpx.Client | None = None) -> tuple[dict, int]:
    """下载并解析插件清单，返回 `(清单, 字节数)`。

    ★ 失败一律转成 BadRequestError 并带上人话，不把 httpx 的原始异常暴露给用户。
    """
    own_client = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True)
    try:
        response = client.get(
            url,
            headers={"Accept": "application/json", "User-Agent": "hne-plugin-installer/1.0"},
        )
    except httpx.HTTPError as exc:
        raise BadRequestError(f"下载插件失败：{exc}") from exc
    finally:
        if own_client:
            client.close()

    # ★ 跟随重定向之后**再查一次域名**：只查用户填的那个地址是可以被 302 绕过的
    final_host = (urlsplit(str(response.url)).hostname or "").lower()
    if final_host not in FETCH_HOSTS:
        raise BadRequestError(
            f"地址被重定向到了 {final_host or '未知域名'}，不在允许列表里，已拒绝安装"
        )

    if response.status_code != 200:
        raise BadRequestError(
            f"下载插件失败：HTTP {response.status_code}"
            "（GitHub 上的文件是否存在？仓库是否公开？私有仓库需要带 token，本功能不支持）"
        )
    body = response.content or b""
    if len(body) > MAX_MANIFEST_BYTES:
        raise BadRequestError(
            f"插件清单太大（{len(body)} 字节，上限 {MAX_MANIFEST_BYTES} 字节）"
        )
    try:
        manifest = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadRequestError(f"插件清单不是合法 JSON：{exc}") from exc
    if not isinstance(manifest, dict):
        raise BadRequestError("插件清单必须是一个 JSON 对象")
    return manifest, len(body)


def manifest_to_fields(manifest: dict) -> dict[str, Any]:
    """把清单校验成插件字段（手工新建与 URL 安装共用）。"""
    spec = manifest.get("spec")
    if spec is not None and str(spec) != MANIFEST_SPEC:
        raise BadRequestError(
            f"不认识的清单格式：spec={spec!r}（本版本只认 {MANIFEST_SPEC}）"
        )
    kind = str(manifest.get("kind") or "").strip()
    if kind not in PLUGIN_KINDS:
        raise BadRequestError(
            f"清单里的 kind 必须是 {' / '.join(PLUGIN_KINDS)} 之一（收到 {kind!r}）"
        )
    name = str(manifest.get("name") or "").strip()
    if not name:
        raise BadRequestError("清单里没有 name")
    if len(name) > 120:
        raise BadRequestError("清单里的 name 太长（上限 120 字）")
    return {
        "name": name,
        "kind": kind,
        "description": (str(manifest.get("description") or "").strip() or None),
        "version": (str(manifest.get("version") or "").strip() or None),
        "author": (str(manifest.get("author") or "").strip() or None),
        "config": validate_config(kind, manifest.get("config")),
    }


def install_from_url(
    db: Session, user_id: int, url: str, *, client: httpx.Client | None = None
) -> tuple[Plugin, str, int, list[str]]:
    """从 GitHub 安装/更新插件。

    返回 `(插件行, 规范化后的地址, 字节数, 清单顶层字段名)`。

    ★ 同名同来源的插件重复安装时**就地更新**（而不是堆一堆同名副本）：
      插件作者改了规则后，用户重装一次即可升级。
    """
    raw_url = normalize_github_url(url)
    manifest, size = fetch_manifest(raw_url, client=client)
    fields = manifest_to_fields(manifest)

    existing = db.scalar(
        select(Plugin).where(
            Plugin.user_id == user_id,
            Plugin.source_url == raw_url,
        )
    )
    if existing is None:
        # 同一来源也可能改了名字：按 source_url 认，不按名字认
        existing = Plugin(user_id=user_id, source_url=raw_url, priority=100, enabled=True)
        db.add(existing)

    existing.name = fields["name"]
    existing.kind = fields["kind"]
    existing.description = fields["description"]
    existing.version = fields["version"]
    existing.author = fields["author"]
    existing.config = fields["config"]
    existing.raw_manifest = json.dumps(manifest, ensure_ascii=False)[:MAX_MANIFEST_BYTES]
    existing.is_builtin = False
    db.commit()
    db.refresh(existing)
    logger.info(
        "安装插件 | user_id={} plugin_id={} kind={} url={}",
        user_id,
        existing.id,
        existing.kind,
        raw_url,
    )
    return existing, raw_url, size, list(manifest.keys())
