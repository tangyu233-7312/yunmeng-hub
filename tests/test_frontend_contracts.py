"""Web 前端源码的静态契约测试（纯读文件，不需要浏览器、不需要后端）。

==================== 为什么把这类断言单独放一个文件 ====================
前端的 bug 有两类：
  · 逻辑错（要靠探针/浏览器才能发现）；
  · **接线错**（改了 A 忘了改 B、写了个永远不会生效的分支）——
    这一类用"读源码 + 断言关键片段"就能抓住，而且跑得飞快。

本项目已经吃过好几次"接线只接一半"的亏（`chooseBackend` 的 explicit、
`switchMode` 的作用域、`backend_entry` 忽略 `HNE_ENV_FILE`），
所以这里把 web/ 侧的关键接线也钉住。
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUTH_JS = REPO / "web" / "js" / "views" / "auth.js"
CHAT_JS = REPO / "web" / "js" / "views" / "chat.js"
UI_JS = REPO / "web" / "js" / "ui.js"
STYLES = REPO / "web" / "css" / "styles.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_login_page_tells_which_storage_is_in_use() -> None:
    """★ 登录页必须显示"当前连的是哪个库"。

    ==================== 这条守着一个真实的误判 ====================
    用户切换存储方式后拿原账号登录，界面只回"用户名或密码错误" ——
    他的第一反应是"我的账号被人改了吗？"
    而真相是：两种存储是**两个互相独立的库**，连账号都不互通，
    切过去就得在新库里重新注册。这件事必须写在**用户看得到的地方**。
    """
    src = _read(AUTH_JS)
    assert "auth-storage-note" in src, "登录页要有那块提示的容器"
    # 必须真的去问后端"现在用的是哪个后端"，而不是写死一句文案
    assert "/health" in src, "要读 /health 才知道当前存储方式"
    assert "components" in src and "database" in src and "backend" in src, (
        "要从 /health 的 components.database.detail.backend 取后端类型"
    )
    # 两种存储都要有可读的名字，并且**明确写出"不互通"**
    assert "MySQL 数据库" in src and "本机文件（SQLite）" in src
    assert "不互通" in src, "必须明确说清两边的数据与账号都不互通"
    assert "切换存储方式" in src, "要告诉用户怎么切回去"


def test_login_page_storage_hint_never_blocks_login() -> None:
    """★ 提示是"锦上添花"，探不到就静默隐藏 —— 绝不能影响登录本身。

    判据：那段逻辑必须有 try/catch，且 catch 里不弹错误。
    ★ 锚点要选**代码**里的那段（`fetch('/health'`），而不是模板里的容器 id ——
      后者在 HTML 里先出现一次，从那里切片会把逻辑段切掉一半（第一版就这么错了）。
    """
    src = _read(AUTH_JS)
    # ★ 从 IIFE 开头切到下一个函数边界（`tabs.forEach`）—— 锚点要包含 `try {`。
    #   第一版从 `fetch('/health'` 开始切，而 `try {` 在它**之前**，于是假失败。
    start = src.index("(async () => {\n    const box = $('#auth-storage-note', root);")
    end = src.index("tabs.forEach", start)
    block = src[start:end]
    assert "try {" in block and "catch" in block, "取 /health 必须包在 try/catch 里"
    # catch 里不许 toastErr（那会在登录页弹一个与登录无关的红条）
    catch_part = block[block.index("catch"):]
    assert "toastErr" not in catch_part, "探活失败不该弹错误提示去打扰用户"


def test_storage_hint_has_styles() -> None:
    """提示块要有样式，否则看起来像一段漏出来的裸文本。"""
    css = _read(STYLES)
    assert ".auth-storage-note" in css, "提示容器要有样式"
    assert ".auth-storage-hint" in css, "提示正文要有样式（小一号、压暗）"


def test_no_leftover_debug_artifacts_in_frontend() -> None:
    """前端源码里不该留下 `console.log` 调试残留（本项目零构建，日志会直接进用户控制台）。"""
    offenders: list[str] = []
    for path in (REPO / "web" / "js").rglob("*.js"):
        text = _read(path)
        for idx, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            # 允许注释里出现这个词（说明性文字），只挑真正的调用
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if "console.log(" in stripped:
                offenders.append(f"{path.relative_to(REPO)}:{idx}")
    assert not offenders, f"前端有 console.log 残留：{offenders}"


# ==================================================================
#  ★★ 诊断信息（「关于 → 复制诊断信息」）的隐私红线
# ==================================================================
DIAGNOSTICS_JS = REPO / "web" / "js" / "diagnostics.js"
ABOUT_JS = REPO / "web" / "js" / "about.js"


def test_diagnostics_text_never_carries_bodies() -> None:
    """★★ 诊断文本是**设计成可以贴给别人**的，所以绝不能带请求/响应体。

    ==================== 这条守着一个真实的隐私事故 ====================
    「请求日志」面板故意摊开**原始请求/响应体**（含对话原文、角色卡内容），
    那是给本机用户自己排查用的。而「复制诊断信息」的用途恰恰相反 ——
    用户会把它贴到群里、issue 里。两者一旦混用，用户一句"帮我看看"
    就会把整段私密对话发出去。

    所以这里做**静态断言**：诊断模块只能从请求日志条目里取
    「方法 / 路径 / 状态码 / request_id / 后端那一句话」这几个白名单字段，
    不许出现 `requestBody` / `responseBody`。

    （浏览器侧还有一条动态断言，见 `scripts/ui_probe.py` 的「13.关于」一节。）
    """
    src = _read(DIAGNOSTICS_JS)
    # ★ 允许**唯一**一处对响应体的读取：`.message`（后端写死的短句），且必须截断。
    #   除此之外不许碰请求体或响应体的其它部分。
    assert "requestBody" not in src, "诊断模块不该读请求体（含对话原文）"
    body_reads = [
        line.strip() for line in src.splitlines()
        if "responseBody" in line
    ]
    assert body_reads, "诊断模块应当从响应体里取 message（否则错误摘要没内容）"
    for line in body_reads:
        assert "responseBody?.message" in line, (
            f"只允许读 responseBody.message（后端保证是写死短句），实际：{line}"
        )
        assert ".slice(" in line, f"从响应体取的内容必须截断：{line}"
    for required in ("entry.method", "entry.url", "entry.status", "entry.requestId"):
        assert required in src, f"诊断模块应当取 {required}（白名单字段）"


def test_diagnostics_collector_excludes_normal_business_failures() -> None:
    """★ 404/409 这类"正常业务失败"不该进诊断文本，否则看的人会以为系统到处在坏。"""
    src = _read(DIAGNOSTICS_JS)
    assert "isReportableFailure" in src, "应当有一个明确的取舍函数"
    assert "[400, 401, 403, 422, 429]" in src, "应当显式列出值得报的状态码"


def test_about_dialog_honors_the_agreed_scope() -> None:
    """「关于」弹窗的内容是用户逐条定过的 —— 这里钉住"放什么、不放什么"。

    放：版本 / 技术栈 / 当前存储 / 数据目录+日志目录 / GitHub 地址 / 复制诊断
    不放：向量库信息、已知限制（"未签名"那条改放在**首次设置向导页**）
    """
    src = _read(ABOUT_JS)
    for required in ("版本", "技术栈", "当前存储", "数据目录", "日志目录", "开源地址"):
        assert required in src, f"「关于」里应当有「{required}」"
    assert "github.com" in src, "应当有开源地址"
    # ★ 判据要锚定在**渲染出来的那一行**上，而不是"文件里出现过这几个字" ——
    #   因为注释里会解释"为什么不要它"，那种出现是正常的（第一版就误报了）。
    assert "row('已知限制'" not in src, "「已知限制」是用户划掉的（放关于页没用）"
    assert "row('向量库" not in src, "「向量库信息」是用户划掉的（普通用户看不懂）"
    # 未签名说明按用户决定放在向导页（用户看到 SmartScreen 警告的当下才需要它）
    setup = _read(REPO / "desktop" / "src" / "setup.html")
    assert "代码签名" in setup, "「未签名」说明应当放在首次设置向导页"


# ==================================================================
#  ★★ 侧边栏「显示选项」的接线（2026-10-13 用户反馈："按钮失效了，点不了"）
# ==================================================================
def _handler_block(src: str, anchor: str) -> str:
    """从 `anchor` 处的监听器切片到它的 `{ signal },` 收尾（含函数体）。"""
    start = src.index(anchor)
    end = src.index("{ signal },", start)
    return src[start:end]


def test_sidebar_stage_switch_actually_takes_effect() -> None:
    """★★ 侧边栏「🎭 立绘舞台」开关必须真的改得动画面。

    ==================== 这条守着一个真实故障 ====================
    用户反馈"图中按钮失效了，点不了"。真实浏览器 + 真实鼠标事件的复现结论：
    侧边栏这个开关写的是**全局默认**，而生效值优先读**按会话**存的键 ——
    只要那个会话被顶栏「🎭 舞台」点过一次（`hne_vn_stage:<id>` 存在），
    再点侧边栏开关就只改了一个**不生效**的值：开关立刻弹回原位、舞台纹丝不动。
    （旧提示文案还写着"再用顶栏点一下即可跟回默认"，等于让用户自己绕路。）

    ⇒ 所以硬性要求：这个处理函数必须**清掉本会话的单独设置**，
       否则那一次点击的结果一定会被下一次渲染覆盖掉。
    """
    src = _read(CHAT_JS)
    block = _handler_block(src, "$('#vn-stage-default', root)?.addEventListener(")
    assert "setVnStageDefault(" in block, "侧边栏开关要写全局默认值"
    assert "clearVnStage(" in block, (
        "侧边栏开关必须同时清掉本会话的单独设置 —— 只写默认值会出现"
        "「点了立刻弹回、舞台不动」的假死开关（真实故障）"
    )
    assert "renderChatPanel(" in block, "改完要重画面板，否则要刷新才看得出变化"

    # 清函数本身要存在，且删的是**按会话**那个键（不是全局默认那个键）
    helper = src[src.index("function clearVnStage(") :]
    helper = helper[: helper.index("\n}\n")]
    assert "removeItem(" in helper, "跟回默认 = 删掉按会话存的键"
    assert "${VN_STAGE_KEY}:${detail.id}" in helper, "删的必须是这个会话的那一项"
    assert "VN_STAGE_DEFAULT_KEY" not in helper, (
        "不能顺手把全局默认也删掉 —— 用户刚刚明确设过它"
    )


def test_stage_override_and_default_stay_two_directions() -> None:
    """★ 两个入口的语义不许混：顶栏 = 本会话临时覆盖；侧边栏 = 全局默认。

    顶栏按钮若改成也写全局默认，就变成"点一次临时开关，把所有会话的默认都改了" ——
    这正是本项目 2026-10-13 收敛分支开关时否掉的那类"多出来的隐藏状态"。
    """
    src = _read(CHAT_JS)
    block = _handler_block(src, "if (act === 'vn-stage') {")
    assert "setVnStage(" in block, "顶栏 🎭 舞台写的是本会话的覆盖值"
    assert "setVnStageDefault(" not in block and "clearVnStage(" not in block, (
        "顶栏按钮不该改全局默认，也不该清掉覆盖本身（它就是来设覆盖的）"
    )


# ==================================================================
#  预设美化（酒馆正则的显示层）：接线契约
# ==================================================================
def test_regex_view_button_only_shown_when_backend_says_so() -> None:
    """★ 「✨ 美化」按钮必须**由后端字段**决定显不显示。

    ==================== 这条守着什么 ====================
    这个按钮点了会去后端跑预设自带的显示层正则。如果无条件显示，
    遇到"预设里没有显示层正则"的会话就会**点了没反应**（后端如实回
    「没有可用的显示层正则」）—— 用户会以为功能坏了。

    所以判据只能来自后端算好的 `regex_view.display_count`（它解析了
    extensions 才知道有几条显示层脚本），前端**不许自己猜**。
    """
    src = _read(CHAT_JS)
    # ★ 不能借用 `_handler_block`：那个 helper 是为"事件监听器块"写的
    #   （它切到 `{ signal },` 收尾），而这段在 `messageActions` 函数里。
    #   这里按函数体切：从函数签名到下一个顶层函数。
    start = src.index("function messageActions(")
    block = src[start : src.index("\nfunction ", start + 10)]
    assert "data-act=\"regex-view\"" in block, "按钮要带 data-act 让分发器能接住"
    assert "display_count" in block, "条件必须读后端给的 display_count"
    # 不许用"有没有预设"之类的本地推测来替代后端判据
    assert "prompt_preset" not in block, (
        "不能拿「有没有绑定预设」当判据 —— 预设有正则与没正则是两回事"
    )


def test_regex_view_renders_in_sandbox_and_can_go_back() -> None:
    """★ 美化结果必须走 `mountRich`（无 allow-scripts 的 iframe 沙箱），且能切回原文。

    两条硬要求：
      · 渲染走 mountRich —— 预设产出的 HTML 里**可能带 `<script>`**
        （实测那份预设就有），只有沙箱能保证它不执行（红线：绝不执行第三方 JS）；
      · 能切回原文 —— 否则用户看到美化版就再也拿不回模型原话。

    ★ 另外必须开 `keepProseLineBreaks`：预设美化出来的正文是**裸换行**分段
      （没有 `<p>`/`<br>`），iframe 默认会把换行折叠成空格 ——
      不开这项，正文会挤成一坨（真实反馈："文字排版变得十分难看，没有分段"）。
    """
    src = _read(CHAT_JS)
    start = src.index("function paintRegexView(")
    block = src[start : src.index("\nfunction ", start + 10)]
    assert "mountRich(" in block, "必须走沙箱渲染，不能直接 innerHTML 插预设给的 HTML"
    assert "keepProseLineBreaks: true" in block, (
        "必须开 keepProseLineBreaks，否则正文的裸换行会被 iframe 折叠、挤成一坨"
    )
    assert "regexRaw" in block, "要记下原文，供切回原文"
    assert "dataset.regexView" in block, "要有个状态位支持『切回原文』"
    # 切回原文那条路径也要存在
    assert "function unpaintRegexView(" in src, "要能还原成纯文本原文"


def test_prose_line_breaks_are_preserved_in_every_rich_render() -> None:
    """★ 每一处 `mountRich` 都要开 `keepProseLineBreaks`。

    ==================== 这条守着一个真实的排版故障 ====================
    酒馆正则美化出来的正文、以及部分角色卡开场白，是**HTML 外壳 + 裸换行正文**
    （实测本机 8 张卡里有 1 张如此），没有 `<p>`/`<br>` 分段。
    iframe 默认 `white-space: normal`，会把换行折叠成空格 ——
    正文挤成一坨。用户的原话："文字排版变得十分难看，没有分段"。

    教训：**同一个根因出现在多条渲染路径上时，只修其中一条等于没修**。
    所以这里遍历所有 `mountRich(` 调用点，逐个要求带上这个参数。
    """
    src = _read(CHAT_JS)
    import re as _re

    calls = _re.findall(r"mountRich\(([\s\S]{0,320}?)\);", src)
    assert calls, "找不到 mountRich 调用点（锚点失效了？）"
    for i, call in enumerate(calls):
        assert "keepProseLineBreaks: true" in call, (
            f"第 {i + 1} 处 mountRich 没开 keepProseLineBreaks，"
            "该路径上的裸换行正文会被折叠成一坨：\n" + call[:200]
        )


def test_sanitize_html_converts_prose_newlines_after_rebuilding() -> None:
    """★ `proseLineBreaks` 的转换必须发生在 `clean()` **之后**。

    `clean` 是重建 DOM（文本节点都重新 append 过）。若先转成 `<br>` 再清洗，
    那些 `<br>` 会被当成文本转义掉 —— 等于白做。
    """
    src = _read(UI_JS)
    start = src.index("export function sanitizeHtml(")
    block = src[start : src.index("\nexport ", start + 10)]
    assert "proseNewlinesToBreaks(box)" in block, "要有这一步"
    idx_clean = block.index("clean(doc.body, box)")
    idx_conv = block.index("proseNewlinesToBreaks(box)")
    assert idx_conv > idx_clean, "转换必须在 clean() 之后，否则 <br> 会被当文本转义"
    # 纯缩进节点不能碰（否则会把标签缩进也顶出来，排版更乱）
    helper = src[src.index("function proseNewlinesToBreaks(") :]
    helper = helper[: helper.index("\n}\n")]
    assert "value.trim()" in helper, "要跳过纯缩进/空白的文本节点"
    assert "'style'" in helper and "'script'" in helper, "<style>/<script> 里的换行是语义的一部分"


def test_regex_view_is_beautified_by_default() -> None:
    """★ 美化必须**默认生效**，不能要求用户每条手点。

    用户原话："如果有美化，应该默认美化，而不是让用户每次都要去点美化按钮
    （加了美化不就为了看吗）"。

    判据：
      · 存在批量拉取 + 自动上色的入口（一次请求，不是逐条点）；
      · 会话打开 / 新回复到账 / silentReload 三条路径都要调它 ——
        漏掉任何一条，"默认美化"就只对部分消息生效，同一条会话里新旧消息
        排版会不一致（这比"全都不美化"更让人困惑）。
    """
    src = _read(CHAT_JS)
    assert "async function autoBeautify(" in src, "要有自动美化入口"
    assert "function loadRegexView(" in src, "要批量拉（一次请求），而不是逐条点"
    # 一次批量请求整条会话，而不是 N 个单条请求
    assert "/regex-view`" in src and "/messages/${id}/regex-view" in src

    # ★ 必须**逐个函数**检查，不能只数调用次数。
    #   第一版写的是 `src.count("autoBeautify(root)") >= 3` ——
    #   变异测试证明它是**假绿**：把 openSession 里那处删掉，
    #   另外两处照样凑够 3 次，断言依旧通过，而"打开会话即美化"已经失效。
    def _func_block(name: str) -> str:
        start = src.index(f"function {name}(")
        # 往前吞掉紧邻的 JSDoc/注释，不影响判断
        body = src[start + 10 :]
        nxt = body.find("\nasync function ")
        nxt2 = body.find("\nfunction ")
        candidates = [n for n in (nxt, nxt2) if n != -1]
        return src[start : start + 10 + (min(candidates) if candidates else len(body))]

    for fn in ("openSession", "silentReload"):
        assert "autoBeautify(root)" in _func_block(fn), (
            f"{fn} 里没有调 autoBeautify —— 默认美化在这条路径上失效"
            "（打开会话 / 新回复到账都会看不到美化）"
        )
    # 新回复到账那条轮询路径（scheduleGeneratingPoll 内的回调）
    assert src.count("autoBeautify(root)") >= 3, "新回复到账的路径也要调"


def test_regex_view_respects_explicit_raw_choice() -> None:
    """★ 用户点过「原文」的那条，下一轮刷新不许被自动美化顶回去。

    `silentReload` 每轮都会重建面板并重新自动美化。若不记住用户的选择，
    他点的「原文」会在下一轮回复到达时自己变回美化 —— 看起来就是"开关按不住"。
    """
    src = _read(CHAT_JS)
    assert "regexViewRawChoice" in src, "要记住用户明确选过看原文的消息"
    start = src.index("async function autoBeautify(")
    block = src[start : src.index("\nfunction ", start + 10)]
    assert "regexViewRawChoice.has(" in block, "自动美化要跳过用户选过原文的那条"


def test_regex_view_cache_is_cleared_when_switching_session() -> None:
    """★ 切会话必须清空美化缓存与选择记录。

    不清的话：缓存会随会话无上限地涨，而且用户的"看原文"选择会串到别的会话上。
    """
    src = _read(CHAT_JS)
    start = src.index("async function openSession(")
    block = src[start : src.index("\nasync function ", start + 10)]
    assert "regexViewCache = new Map()" in block, "切会话要清空美化缓存"
    assert "regexViewRawChoice = new Set()" in block, "切会话要清空『看原文』的选择"


def test_regex_view_handler_is_wired_into_dispatcher() -> None:
    """★ 接线检查：按钮的 data-act 必须在事件分发器里有对应分支。

    "写了按钮但没接分发"是本项目吃过好几次的亏（改了 A 忘了改 B），
    静态读源码就能抓住。
    """
    src = _read(CHAT_JS)
    assert "act === 'regex-view'" in src, "分发器里缺少 regex-view 分支"
    assert "showRegexView(" in src, "要真的调到处理函数"


def test_regex_view_endpoint_path_matches_backend() -> None:
    """前后端路径必须逐字对得上（前端拼的 URL 与后端注册的路由）。"""
    src = _read(CHAT_JS)
    assert "/regex-view`" in src, "前端请求的路径要以 /regex-view 结尾"
    backend = (REPO / "app" / "api" / "v1" / "narrative.py").read_text(encoding="utf-8")
    assert '"/sessions/{session_id}/messages/{message_id}/regex-view"' in backend, (
        "后端要注册同名路由，否则前端 404"
    )


def test_each_message_action_function_is_defined_once_with_its_own_body() -> None:
    """★ 每个消息级处理函数只能定义一次，且**注释不能被留下当孤儿**。

    ==================== 这条守住一次真实的编辑事故 ====================
    我插入 `showRegexView` 时，锚点选了 `async function translateOneMessage(`
    这一行，于是新函数被塞进了 `translateOneMessage` 的 **JSDoc 与函数体之间**：
      · 那段 JSDoc 变成了孤儿（挂在 `showRegexView` 上面，说的却是翻译的事）；
      · `translateOneMessage` 的定义与函数体被拆开重组成两个函数（后面那个
        没有 try/catch 的调用 `showTranslating`、前一个没有定义）。
    语法检查**发现不了**（JS 是宽松的：没有声明的调用要到运行时才报），
    只有"函数定义出现两次"这种静态特征能抓住。
    """
    src = _read(CHAT_JS)
    import re as _re

    for name in ("showRegexView", "translateOneMessage", "copyMessage", "toggleTranslation"):
        # ★ 用整行匹配 `[async] function NAME(`：不能简单地数两次子串
        #   —— `"async function X("` 里本身就含有 `"function X("`，会数成 2
        #   （第一版就是这么假失败的）。
        hits = len(
            _re.findall(rf"^(?:async\s+)?function\s+{_re.escape(name)}\s*\(", src, _re.M)
        )
        assert hits == 1, f"{name} 应当只定义一次，实际 {hits} 次（可能被插入的代码拆开了）"
    # 翻译那条路径的独有调用必须留在 translateOneMessage 里
    start = src.index("async function translateOneMessage(")
    rest = src[start + 10 :]
    nxt = rest.find("\nasync function ")
    end = start + 10 + nxt if nxt != -1 else len(src)
    block = src[start:end]
    assert "showTranslating(" in block, "translateOneMessage 的函数体被搬走了"
    assert "/translate`" in block, "translateOneMessage 里必须调它自己的接口"
    assert "regex-view" not in block, "美化那段不该混进 translateOneMessage"

