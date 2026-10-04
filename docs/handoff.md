# 项目交接文档（handoff）

> **这份文档的用途**：让一个**全新的、没有任何历史对话记忆的 AI 会话**在几分钟内接手本项目。
>
> **为什么需要它**：把整个开发过程塞进同一个会话，会导致每一轮都要重发全部历史，
> Token 成本随会话长度**超线性增长**。正确做法是**一个步骤一个新会话**，
> 靠本文档交接（读它只需几千 token，而不是几百万）。
>
> **新会话的第一件事**：完整读一遍本文档，再动手。
>
> 最后更新：第十五轮（★ 自动翻译中间件：跨语言对话，见 §28）
> + 第十四轮（★ 角色卡 VN 模式：立绘/表情，见 §27）
> + 第十三轮（★ 骰子插件 + 五套总结提示词 + 删除群聊，见 §26）
> + 第十二轮（★ 状态漂移曲线 + 状态遥测，见 §25）
> + 第十一轮（★ 总结按钮反馈 + 防重复总结，见 §24）
> + 第十轮（★ 记忆锚点，见 §23）
> + 第九轮（★ 记忆总结改成用户可控的「记忆管理面板」，见 §22）
> + 第八轮（★ 检索优先级修正 + 剧情总结分层合并，见 §21）
> + 第七轮（★ 混合检索融合 + `scripts/benchmark.py`，见 §20）
> + 第六轮（★ 状态栏字段改由角色卡 / 世界书定义，见 §19）
> + 第五轮（状态栏手动纠正 / 主模型 failover / 流式开关 / 状态栏"每轮都要输出"）
> + 用户实测反馈的前端 bug（列表页按钮失效、导入 JSON 只能粘贴、整页卡「加载中」、纠正按钮不弹窗）
> + **⑤ 插件市场极简版**（声明式**四类**：正则替换 / 提示词注入 / CSS 主题 / **跑团骰点**；仅 GitHub 安装，仓库与 gist 都支持）
> + **纯聊天会话（无角色）· 兼 API 体检台**（见 §17）。
> 当前状态：**`pytest` 842 passed · `smoke` 179/0 · 浏览器探针 128/0**。

---

## 0. ★ 新对话从这里开始（只读这一节也能接手）

> **只想快速接手？先读 [`docs/handoff-quick.md`](handoff-quick.md)**（一页速览：现状 / 命令 /
> 红线 / 文件地图 / 已知坑 / 可直接复制的开场白）。本文档是**详细版**，
> 需要细节（某一轮为什么这么改、踩过什么坑）时再往下读。

> ★★ **第一步（两步都做完再动手）**：
> ① 读上面那份速览（或本文档的 0.1~0.9 节）；
> ② 读 **`docs/ai-session-rules.md`** —— **AI 协作纪律（省 Token 7 条 + 交付流程）**。
>
> ⚠️ 那个文件**不在仓库里**（已写进 `.gitignore`：项目仓库讲项目本身，协作纪律属于
> 本地工作方式）。它在**本机**存在；如果你在**别的机器或新 clone** 上干活，
> 它不存在是正常的 —— 此时请**先问用户要这份文件**，不要凭猜测代替它。
> 另外 `README.md` 与 `handoff-quick.md` 里都**只有指引、没有规则原文**，
> 不要在那里找。

### 0.1 这是什么项目

**异构大模型交互式叙事引擎**：用户自配任意 LLM API（URL / Key / 模型名），
做角色扮演式的交互叙事。Python 3.13 + FastAPI + MySQL 8 + ChromaDB，
前端是**零依赖、零构建**的原生 HTML/CSS/ES Module 控制台（`/console`）。
工作目录：`E:\VSCode\hetero-narrative-engine`。

### 0.2 前 5 分钟怎么做

```powershell
# 1) 确认端口空闲（上一次的服务应当已停）
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue

# 2) 起服务（改 app/** 必须重启；改 web/** 只要刷新浏览器）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3) 界面：http://127.0.0.1:8000/console   接口文档：http://127.0.0.1:8000/docs
```

三层验证（数字对不上就是真的出问题了，别放过）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q          # 期望 877 passed + 2 skipped
.\.venv\Scripts\python.exe scripts\smoke_test.py # 期望 184 / 0（需要服务在跑）
.\.venv\Scripts\python.exe scripts\ui_probe.py   # 期望 147 项检查，0 失败（自己起假模型+清场）
.\.venv\Scripts\python.exe scripts\benchmark.py  # 检索消融表 + 状态一致率（离线；--from-db 为只读回放）
```

> ⚠️ **不要同时跑 `pytest` 和 `smoke_test.py`**：smoke 首尾各查一次全库行数，
> 并发的测试账号会让它误报"留下垃圾数据"（已误报过一次，串行即 142/0）。

### 0.3 红线（踩了会造成不可逆损失）

| 绝对不要 | 原因 |
|---|---|
| `init_db.py --drop` | 会**清空用户真实数据** |
| 删任何测试 | 用户明确要求；测试是唯一的安全网 |
| 在测试里调真实 LLM API | 会花钱；一律用 `scripts/fake_openai_server.py` |
| 只清理"测试前缀"以外的账号 | 清理只走 `scripts/cleanup_demo_data.py`（只删 `ui_`/`tmp_`/`msg_`/`dbg_` 等前缀） |
| 擅自改用户的真实数据 | 数据库里 `真实用户`(#887) 名下的东西一律不动 |

### 0.4 用户真实数据（**不要碰**）

- 用户 `真实用户`(id=887)；模型配置 2 条（`Deepseek` + 一条 `ChatGPT`）。
- 角色卡 2 张（id 2973 开场白是 11060 字 HTML；id 3696 是 3431 字 + 1 条 3107 字备选开场白）。
- 世界书 2 本（id 1207 共 36 条、id 1525 共 13 条）。
- 会话 2 条（用户自己建的）；提示词预设 1 份（用户导入的「赤狐DeepSeek(4)」，`is_active=1`）。
- ⚠️ 有一条**需要用户自己改**的数据：`ChatGPT` 那条的 API 地址里多了一个英文双引号
  （`https://ai.zyyun.xyz"/v1`），豆包那条 `…/api/v3/responses` 要改成 `…/api/v3`。
  **不要代劳**，界面上编辑一次即可。

### 0.5 当前能力（后端 3.1~3.10 全做完）

模型配置异构适配层 / 角色卡（含 PNG 导入导出）/ 世界书（关键词触发）/
长期记忆（ChromaDB 语义召回）/ 叙事会话与 SSE 流式对话 / 消息级操作 /
**提示词预设**（酒馆 completion preset 导入导出 + 深度注入）/ **内置守卫规则** /
可视化控制台（含 HTML 开场白的 iframe 沙箱渲染、对话窗口可拖动改大小、Token 统计）。

### 0.6 还没做的（用户已经定过的方向）

| 项 | 状态 |
|---|---|
| **Electron 打包** | 用户选了**便携免安装版**（portable/dir）；**等他把界面验完再做** |
| 预设的 `forbid_overrides`（覆盖机制） | 未实现，字段原样保真存取 |
| 世界书 `recursive_scanning` | 未实现，字段保留；界面**故意没有**开关（不做假按钮） |
| 是否要 `scripts/start_server.ps1` / `stop_server.ps1` | 我提过，用户未表态 |

### 0.7 容易踩的环境坑（省下你半小时）

- PowerShell **5.1**：没有 `??`、`&&`、三元；`$PID` 只读。
- `node --check` **对 ES Module 不可靠**（默认按 CJS 解析 → 假阳性+假阴性，
  曾经因此漏掉一个语法错误导致**全站白屏**）。正确做法：复制成 `.mjs` 再 `node --check`。
- 前端模块间**只能用裸名** `hne/xxx`（importmap），**禁用相对导入**；
  改了 `web/**` 只要刷新（入口页的 `?v=__WEB_VERSION__` 由服务端按文件指纹替换）。
- 前端缓存击穿的自愈逻辑在 `web/js/boot.js` + `NoCacheStaticFiles`，别删。
- loguru 会往 stderr 写日志，导致 PowerShell 报 `exit 1` 的**假警报**；看内容别只看退出码。
- 浏览器探针用 Edge：`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`。

### 0.8 第四轮刚做完什么（细节见第 14 节）

| 用户报的问题 | 真根因 | 关键位置 |
|---|---|---|
| HTML 开场白不渲染 | `<style>`/`<img>` 被整块丢掉 + 外层 ``` 围栏没剥 | `ui.js::sanitizeHtml` / `mountRich`（iframe 沙箱） |
| AI 暴露身份、不按剧情、回复太短 | 之前没有任何硬规则 | **内置守卫预设**，永远追加在用户预设之后 |
| 备选开场白"一行一条" | `join('\n')/split('\n')` 把 1 条拆成 67 条 → 保存 422 | 改成一条一页的翻页编辑器 |
| 重新生成 / 编辑后确认没反应 | 一个 id 同时当"要删的回复"和"要问的话" | `narrative.py::_prepare` 返回两个 id |

### 0.9 开新对话时，把下面这段直接粘过去

```text
项目在 E:\VSCode\hetero-narrative-engine（异构大模型交互式叙事引擎）。

第一步：只读 docs/handoff.md 的「## 0. ★ 新对话从这里开始」这一节（约 60 行），
不要全项目扫描、不要整目录读取，我会贴报错/截图给你定位。

第二步：读本机的 `docs/ai-session-rules.md`（**该文件不在公开仓库里**，是本机私有规则），
严格按它那 7 条做事：先 grep 定位 → 只读命中行前后 50 行 → 每次输出 ≤100 行 →
不确定就先列查询计划等我确认。

红线：不许 init_db.py --drop；不许删测试；测试里不许调真实 LLM；
清理数据只走 scripts/cleanup_demo_data.py；真实用户（本机 DB 里的 id 已隐去） 的真实数据一律不动。

我的验收标准（改完必须自己先跑一遍再找我）：
  pytest -q                       → 877 passed + 2 skipped
  scripts\smoke_test.py           → 184 / 0（需先启服务）
  scripts\ui_probe.py             → 147 项检查 0 失败
  scripts\benchmark.py            → 退出码 0（--from-db 只读回放也要能跑）
  desktop\ 的 npm test            → 115 / 0（改了 desktop/ 才需要）
注意：pytest 和 smoke_test 不要同时跑（会互相误报）。

现在我要做的事是：<在这里写你的新需求 / 贴上问题与截图>
```

> 为什么这么写：把"读哪一节、守什么规矩、怎么算验完"一次性交代清楚，
> 新会话就不需要你把整个历史重发一遍 —— 这是本项目省 Token 的核心做法。

---


## 0. 三十秒速览

| 项 | 值 |
|---|---|
| 项目 | 异构大模型交互式叙事引擎（毕业设计）—— 后端 + 可视化控制台 |
| 技术栈 | Python 3.13 + FastAPI + MySQL 8 + ChromaDB + 原生 HTML/CSS/JS |
| 项目根目录 | `E:\VSCode\hetero-narrative-engine` |
| 测试现状 | **660 项全绿**（pytest）；冒烟 **142 项全过**；浏览器探针 **93 项全过** |
| 已完成 | 3.1 ~ 3.10（含**消息级操作**：复制 / 编辑 / 撤回 / 重新生成 + Token 统计）<br>+ 第五轮（状态栏 / 主模型 failover / 流式开关 / **插件市场极简版** / **纯聊天会话**，见 §15~§17） |
| 下一步 | 用户在真机上验收；剩下的都是可选项：Electron 便携版打包（见 §0 表格下方说明） |
| 论文核心 | `app/llm/` —— 异构大模型适配层（两个协议证明抽象的是**协议差异**） |
| 论文素材 | `docs/paper-materials.md`（对比表 / 公式 / 实测数据 / 踩坑表，每条都标了代码出处） |

> 计划书（README「开发进度」）里的 3.1 ~ 3.10 全部完成：3.10 收尾中的
> 文档与论文素材已完成，**只剩可选的 Electron 便携版打包**。

---

## 1. 环境事实（可直接复制执行）

```powershell
# 项目根目录
cd E:\VSCode\hetero-narrative-engine

# Python（务必用这个解释器，不要用 3.13t 自由线程版或 Anaconda）
E:\Python\Envirment\python.exe
.\.venv\Scripts\python.exe                        # 项目虚拟环境

# 常用命令
.\.venv\Scripts\python.exe -m pytest -q           # 全量测试（约 3 分钟，586 项）
.\.venv\Scripts\python.exe -m pytest tests/test_narrative.py -q   # 只跑某模块
.\.venv\Scripts\python.exe scripts/init_db.py     # 查看/创建表结构
.\.venv\Scripts\python.exe scripts/smoke_test.py --keep   # 端到端冒烟（需先启服务）
.\.venv\Scripts\python.exe scripts/cleanup_demo_data.py   # 清理测试遗留数据

# 启动服务（前端控制台在 /console）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# ★ 假 OpenAI 兼容服务（不花钱、不联网就能演示"流式打字机"）
.\.venv\Scripts\python.exe scripts/fake_openai_server.py --port 8123
#   然后建一个模型配置：base_url=http://127.0.0.1:8123/v1，模型名 fake-model
```

| 资源 | 值 |
|---|---|
| MySQL | 8.0.28，服务名 `MySQL80`，数据库 `narrative_engine`，账号 `narrative_app` |
| 配置 | 全部环境变量前缀 **`HNE_`**，写在 `.env`（已 gitignore） |
| 表 | 6 张：`users` `llm_providers` `character_cards` `world_books` `narrative_sessions` `messages` |
| 接口文档 | http://127.0.0.1:8000/docs |
| 可视化控制台 | http://127.0.0.1:8000/console |
| 健康检查 | http://127.0.0.1:8000/health |

### 控制台环境的坑

- PowerShell 是 **5.1**（不支持 `??` / `&&`）
- 中文乱码：命令前加 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`
- `npm.ps1` 被执行策略挡住 → 用 `npm.cmd`（但本项目前端**不需要 npm**）
- 读文件一律用 `read` 工具，不要用 `cat`

---

## 2. 当前进度

### 已完成

| 步骤 | 内容 |
|---|---|
| 3.1 | FastAPI 骨架、配置、日志、全局异常、健康检查 |
| 3.2 | MySQL 连接池 + 6 张表的 ORM 模型 + 建表脚本 |
| 3.3 | ChromaDB + 可插拔嵌入后端（onnx 本地 / 远程 API）+ 语义检索 |
| 3.4a | 统一 LLM 调用层：数据结构、适配器抽象、OpenAI 兼容协议、错误码归一化、重试 |
| 3.4b | 生成参数层与上下文预算（温度 / 最大输出 / 思考强度 / 参数生效性探测） |
| 3.4c | Anthropic Messages 协议适配器（第二个协议） |
| 3.5 | 用户注册 / 登录 / JWT / 刷新令牌 |
| 3.6 | 角色卡 + 世界书（公共卡库、删除勾选项、Character Card V2 的 JSON/PNG 导入导出） |
| 3.7 | 模型配置 CRUD（连通性测试、思考强度探测、拉取模型列表） |
| 3.8 | 叙事会话与对话：提示词构建、上下文裁剪与滚动摘要、SSE 流式、前端对话界面 |
| 3.9 | 世界书关键词触发检索（scan_depth / token_budget）+ 向量长期记忆（写入 / 语义召回 / 预算预留 / 删除一致性）|
| 3.10 | 收尾：**论文素材** `docs/paper-materials.md`、**README 收口**（长期记忆/关键词触发一节、前端能力表、测试数值）、**Swagger 中文描述**核对、**无用代码清理**（见下） |
| — | 可视化控制台（`web/`，可点击原型） |

### 3.10 的代码清理（新增，写在这里防止后人"以为被误删"）

用「AST Load 引用分析 + 全仓 grep 交叉验证」做了一轮死代码审计，删掉了**已被证明无引用**的东西：

| 位置 | 删掉的东西 | 判定依据 |
|---|---|---|
| `app/services/character_card_service.py` | `SCOPES`、`SORT_OPTIONS` 两个常量 | 全仓只有定义处 1 处匹配；scope 由接口层 `Literal[...]` 校验，排序在前端硬编码 |
| `app/schemas/narrative.py` | `SessionPage = Page[SessionBrief]` 别名 | 接口层用的是 `ApiResponse[Page[SessionBrief]]`，别名 0 引用 |
| `app/core/config.py` | `reset_settings_cache()` | 全仓（含 tests）0 引用 |
| 6 个文件的未使用 import | `character_card_service`、`func`、`text`、`LLMBadRequestError`、`ChatMessage`、`StreamChunk`、`Page` | 每个名字在文件内只出现在自己的 import 行上 |
| `web/js/ui.js` | `hs()`、`formValuesOrNull()`、`alerts()` 三个导出 | 导出/导入矩阵里 importer 数 = 0，文件内也无调用 |
| `web/js/views/auth.js` | `lastUsername()` 导出 | 0 引用 |
| 4 个视图的未使用 import | `api`/`esc`（auth）、`$$`/`checkboxField`/`confirmDialog`/`fmtDate`/`textareaField`/`toastWarn`（books）、`confirmDialog`（cards）、`emptyState`/`textareaField`（chat） | 逐文件统计正文出现次数 = 0 |

**刻意保留**（审计标记为"看起来没用但删不得"）：

| 保留项 | 原因 |
|---|---|
| `app/schemas/common.py` 的 `ErrorResponse` | 它只用于在 Swagger 里**说明错误的统一结构**，删掉等于删文档 |
| `app/schemas/narrative.py` 的 `SseError`、`WorldBookScanInfo`、`WorldBookRef` | Pydantic 契约类：SSE 错误负载的字段定义、以及"将来要暴露给界面"的统计结构。删了会让 wire 格式无从查证 |
| `app/db/chroma.py` 的 `Base` 导入 | 显式带 `# noqa: F401`，保持数据层 import 形态一致 |
| 全部 15 个"零名字引用"的路由处理函数 | 它们是 `@router.*` 注册的**公开 API**，靠 URL 可达，不是死代码 |
| `app/llm/params.py` 与 `app/schemas/provider.py` 里重复的 `_check_output_fits_window` | **刻意的双重防御**：接口层拦下来才会返回 422 而不是 500，别合并 |

**已知但本轮没动的重复实现**（审计发现 7 组，都是"能跑、可读、动它有风险"）：
`_escape_like`×2、`_now`×3、`_safe_json`/`_loads`×2、`_masked_key`/`masked_api_key`×2、`_clean_text`×2、
`alertsHTML` vs 已删除的 `alerts`。**要合并的话请单独一步做，并跑全量测试。**

### 未完成

| 步骤 | 内容 | 备注 |
|---|---|---|
| 3.10 剩余 | **Electron 打包**（便携免安装版） | 用户已确认要做，**排在用户实际验证通过之后**；`.env` 绝不能进安装包 |

### 3.9 之后还没用起来的字段（3.10 / 后续可选）

- `world_books.extra_data` 里的 `recursive_scanning`（递归扫描）
  —— **仍未实现**，3.9 已如实说明"暂不支持"，字段照样原样存取与导出。
  要做的话：命中条目后再用它的正文扫一遍其它条目的关键词（SillyTavern 的行为）
- `narrative_sessions.rolling_summary` 目前是**本地截断式压缩**
  （`context_manager.summarize_dropped`，不额外调模型）。想换成模型生成的真摘要时，
  函数签名不用改；但注意那会给每轮多一次模型调用
- `world_books.extra_data.token_budget` 的上限校验（现在是 0~100000）
- `narrative_sessions.message_count` / `total_tokens` / `last_active_at` —— 已维护

---

## 3. 架构与分层约定（必须遵守）

```
api/v1/     收请求、参数校验、调用 service、包装统一响应。不写业务逻辑
services/   业务逻辑。不碰 HTTP，不知道状态码（抛 AppException 子类）
db/models/  ORM。表结构的唯一事实来源
schemas/    Pydantic 请求/响应模型
llm/        ★ 异构适配层，与业务完全解耦
narrative/  ★ 叙事引擎：提示词构建 / 上下文裁剪 / 对话编排（不出现任何厂商字段名）
embeddings/ 可插拔嵌入后端
utils/      纯函数工具（如 png_card.py）
web/        前端控制台（零依赖，原生 ES Module）

scripts/    init_db / cleanup_demo_data / smoke_test / fake_openai_server
tests/      pytest（集成测试，真实连 MySQL 与 ChromaDB）
docs/       generation-params.md / pitfalls.md / handoff.md（本文）
```

### 硬性规则

1. **同步 SQLAlchemy Session**：读写数据库的接口一律用 `def`（不是 `async def`），
   FastAPI 会自动丢进线程池。**`async def` 里禁止直接查数据库**，要用
   `from starlette.concurrency import run_in_threadpool`。
   > 3.8 的 SSE 端点是本项目第一个 `async def` 业务接口，写法见
   > `app/api/v1/narrative.py` 顶部说明：**校验在线程池里做在前，流用独立 session**。
   > 另外：**async 生成器里不要直接用请求注入的那个 `db`** ——
   > 响应开始后它的生命周期不再可靠，要用 `session_scope()` 自己开一个。

2. **跨字段校验必须放服务层**：PATCH 是部分字段更新，接口层看不到最终组合。
   否则用户会收到 500 而不是 422。

3. **越权规则**（各模块**故意不同**，别统一）：
   | 资源 | 别人的私有 | 别人的公开 |
   |---|---|---|
   | 模型配置 | 404 | ——（没有公开） |
   | 角色卡 | 404 | **403**（能看不等于能改） |
   | 世界书 | 404 | ——（没有公开） |

4. **PATCH 用 `model_fields_set`** 区分「没提交」与「提交了 null」。
   必填字段（如世界书 `name`）传 null 视为「不修改」。

5. **列表接口返回精简结构**，不返回长文本字段（角色卡有 4 个 MEDIUMTEXT）。
   分页用 `schemas/common.py` 的 `Page`。

6. **注释一律用中文**，写「为什么这么做」而不是「这行在做什么」。
   用户会亲自审查代码。

---

## 4. ★ 不可破坏的设计决策

| 决策 | 原因（改动前务必读懂） |
|---|---|
| **`max_tokens` 包含思考过程的 token** | 用户明确要求的规则（与酒馆一致）。界面上必须标注，不能只写「最大输出」 |
| **`reasoning_effort=auto` 不发送任何字段** | 最安全。有些模型「接受但忽略」该参数，实测 `deepseek-flash` 就是 |
| **适配层改动用户参数时必须回报** | 通过 `ChatResult.notes` / `StreamChunk.notes`。静默降级是误导 |
| **拒绝静默降级，宁可报错** | 例如 Anthropic 放不下思考预算时直接报错，不偷偷关掉思考 |
| **Anthropic 开启思考时必须移除 temperature / top_p** | 协议硬约束 |
| **ChromaDB 集合一律 `embedding_function=None` + 显式传向量 + 指纹校验** | 否则重新打开集合时它悄悄注入默认嵌入函数，检索结果全错但不报错 |
| **API Key 用 Fernet 加密入库，响应只回显脱敏** | 安全红线 |
| **思考强度探测结论只保留「当前」一份** | 用户否决了「记录多个模型的历史」，嫌杂乱。改模型名靠 `probed_model` 自动判定过期 |
| **删角色卡默认拒绝，必须带 `force=true`** | 会牵连会话与世界书。两个勾选项都默认 true |
| **复制角色卡时世界书要**复制成新的一本**** | 共享引用会导致改副本影响原卡 |
| **`Page` 只用在角色卡/世界书，没用在 `/providers`** | 模型配置量级小，分页纯属多余 |
| **`[hidden] { display: none !important; }` 必须在样式表里** | 作者样式的 `display` 会覆盖浏览器的 `[hidden]`，删掉它所有 hidden 全部失效 |
| **前端视图的 `root.addEventListener` 必须带 `{ signal }`** | 否则监听器泄漏到其它页面（详见第 5 节） |
| **委托监听器分发的按钮必须写 `data-act`** | 少了它点击**毫无反应且不报错**，还会被 `closest()` 误匹配到别的按钮（真实踩过，见 pitfalls 第 14 条） |
| **流式失败要保留半截回复并标注「回复中断」** | 用户已经看到那些字了，刷新后消失会像"幻觉" |
| **用户消息先落库、再调模型** | 模型失败不该把用户打的字一起吞掉 |
| **世界书条目在 3.8 是"全部启用条目"注入** | 关键词触发检索（scan_depth / token_budget）留给 3.9，别在 3.8 里偷偷加 |

---

## 5. 已知陷阱

**完整清单见 [`pitfalls.md`](pitfalls.md)（12 类，每条含现象→根因→修复→为什么难发现）。**
必读的几条：

1. **非重入锁的自杀式死锁** —— 同一线程二次 `acquire()` 是**永久静默阻塞**。
   本项目出现过两次（`db/mysql.py`、`embeddings/api_backend.py`）。
   规则：**持有锁时，绝不调用会加同一把锁的函数**，需要结果就先在锁外取出来。
2. **ORM 默认把子表外键置 NULL** —— 与外键动作（SET NULL / CASCADE）及是否可空强相关。
   改外键时**必须两处一起改**（DDL + relationship 的 `passive_deletes`）。
   改完**一定要真删一次验证**，不能推理。
3. **推理模型吃光输出配额 → 静默空回复** —— `finish_reason=length` + HTTP 200。
   已有 `_raise_if_no_content` 守卫，别删。
4. **为兼容一种格式做的文本处理可能误伤另一种** —— PNG 解析里「去掉所有空白」
   只能用于 base64，用在 JSON 原文上会把 `"Hello there"` 变成 `"Hellothere"`。
5. **前端监听器泄漏到其它页面**（见下）。

### 5.1 前端监听器泄漏（上一轮刚修，最容易重犯）

`#view` 是**常驻元素**，切页只替换内部内容。挂在它上面的委托监听器**不会自动消失**。

```js
// ✗ 错：监听器永久残留，访问过的每个页面都会留下一个
root.addEventListener('click', handler);

// ✓ 对：带上 signal，app.js 切页时 abort() 一次性摘掉
root.addEventListener('click', handler, { signal });
```

症状极具迷惑性（真实出现过）：世界书页点卡片却弹出「角色卡不存在」、
一个按钮弹 N 个窗要关 N 次、按钮文字变成空白甚至 `undefined`。

**新增视图时必须同样处理**，并用 `tests/test_console.py` 里的
`test_view_delegated_listeners_are_abortable` 守住。

---

## 6. 验证方式与期望数值

### 三层验证

| 层级 | 命令 | 期望 |
|---|---|---|
| 单元/集成 | `.\.venv\Scripts\python.exe -m pytest -q` | **660 passed** |
| 端到端（真实 HTTP） | 先启服务，再 `scripts\smoke_test.py` | **142 项全过，退出码 0** |
| 前端行为（真实浏览器） | 先启服务，再 `scripts\ui_probe.py` | **93 项全过**，文字逐字增长、无意外失败请求 |

### 各测试文件用例数（用来核对有没有被误删）

```
test_anthropic.py        54      test_console.py        37
test_auth.py             27      test_db.py              9
test_character_cards.py  91      test_llm.py            76
test_chroma.py           21      test_main.py            8
test_params.py           59      test_providers.py      36
test_memory.py           16      test_narrative.py      68
test_world_books.py      39
                         ---
                         539
```

### ★ 浏览器探针（验证前端 bug 的手段，必须掌握）

纯后端测试**测不出** CSS / JS 的 bug。上一轮 4 个前端 bug 都是靠这个方法定位和验证的：

```powershell
# 1) 启服务
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2) 写一个临时探针页放在 web/ 下（验证完删掉）
#    它把 token 塞进 localStorage，用 iframe 加载 /console/，
#    然后模拟点击、读取 getComputedStyle / DOM 状态，把结果写进 <pre> 里

# 3) 无头 Edge 加载它并导出 DOM
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
Start-Process $edge -ArgumentList @(
  "--headless","--disable-gpu","--no-sandbox","--no-first-run",
  "--user-data-dir=$env:TEMP\hne_edge","--virtual-time-budget=25000",
  "--dump-dom", "http://127.0.0.1:8000/console/_probe.html?t=<token>&u=<urlencoded user>"
) -NoNewWindow -Wait -RedirectStandardOutput out.html
# 用 UTF8 读取 out.html，正则抓 <pre> 里的结果
```

**玩法 B：无头 Edge + DevTools 协议（CDP）—— ★ 验证"逐字出现"只能用它**

`--virtual-time-budget` 会把"等待网络"的**虚拟时钟跑得比真实时间快**，
拿它判断「文字是不是逐字出现」会得到错误结论（3.8 实测：文字确实在流式增长，
但虚拟时间早就耗尽，探针只能看到空消息体，白白排查半天）。

CDP 的做法（3.8 用这个抓到并验证了「发送按钮缺 data-act」这个 bug）：

```python
# scripts/_probe_browser_cdp.py 就是这种临时脚本（用完删掉即可），思路：
#  1) 启动 Edge：--headless=new --remote-debugging-port=9333 http://127.0.0.1:8000/console/
#  2) GET http://127.0.0.1:9333/json/list 拿 page 的 webSocketDebuggerUrl
#  3) websockets.connect(...) 后发 Runtime.evaluate（returnByValue=True, awaitPromise=True）
#  4) 每 ~120ms 采样 textContent.length，得到真实的文字增长曲线
#     （3.8 实测：7.39 秒内出现 54 次增长，最终 132 字 —— 这就是"逐字出现"的证据）
```
依赖：`websockets`（虚拟环境里已有）、`httpx`。

关键技术点（都踩过）：
- **★ `localStorage` 是按「源」隔离的**：在 `about:blank` 或别的源上写入 token，
  导航到 `http://127.0.0.1:8000` 之后**读不到**（表现为：顶栏不出现、请求全部 401）。
  正确顺序是 **先导航到目标源 → 再写 localStorage → 再重载页面**。（3.10 实测踩到，白查一轮）
- **不要用固定 `sleep` 等首屏**：改用轮询 `document.querySelector('#view h1')`，
  慢启动时固定 sleep 会把"还没渲染"误判成 bug
- **假模型是逐字吐的**（默认每字 0.12s，一条 120 字的回复要 ~15 秒）：
  等流结束要判「气泡不再是 `.pending`」，不要用固定时长
- **必须用 `Start-Process -RedirectStandardOutput` 抓输出**，直接 `>` 重定向会得到 0 字节
- 探针页要**放在 `web/` 下**，它由 `/console/` 挂载提供（根路径访问不到）
- 读文件时必须 `[System.IO.File]::ReadAllText($f, [System.Text.Encoding]::UTF8)`，否则中文乱码
- `--headless`（旧模式）比 `--headless=new` 在本机更可靠（CDP 则要用 `--headless=new`）
- ★ **iframe 的内容不会出现在 `--dump-dom` 的输出里**：探针必须把
  `iframe.contentDocument` 的状态**序列化进自己的 `<pre>`**，否则什么都看不到
- 探针脚本里 `print` 中文会撞上 GBK 控制台（`UnicodeEncodeError`）→ **写文件**再读
- 截图用 `--screenshot=<绝对路径>.png`，之后可以直接看
- 需要真实模型时用 `scripts/fake_openai_server.py`（本地假 OpenAI 服务，逐字吐字）

---

## 7. ★★ 上一轮 AI 犯过的错误 —— 新对话必须避免

> 这一节是本文件**最有价值的部分**。每条都真实发生过，并浪费了 Token 或降低了交付质量。

### 7.1 编辑文件时用「半截锚点」导致误删代码（犯了 4 次）

```python
# ✗ 错：old_string 只写到下一行的一半，结果把那一行也删了
old_string = 'class Foo(BaseModel):\n    """文档首行'
new_string = 'class Foo(BaseModel):'
# → 文档串首行被删除，文件语法错误
```

**正确做法**：`old_string` 要包含**足够完整、唯一**的上下文；
或者先 `read` 出完整段落，再整体替换。改完**立刻做语法检查**：

```powershell
.\.venv\Scripts\python.exe -c "import ast,pathlib; ast.parse(pathlib.Path('app/xxx.py').read_text(encoding='utf-8')); print('OK')"
```

### 7.2 凭记忆/假设写代码，没先核实 API 的真实形状

曾假设登录响应里有 `user` 字段，实际**只有 token**（要另调 `/auth/me`）。
写测试前**先跑一次接口看真实响应**，别猜。

### 7.3 改了 ORM 模型却忘记重建数据库

把 `world_books.name` 改成可空后没跑 `init_db.py`，测试直接报
`Column 'name' cannot be null`，白查一轮。

**规则：改任何 `mapped_column` 之后，立刻重建表并核对字段。**
（注意第 10 节的红线：**不能随便 `--drop`**，见下）

### 7.4 自己写的检查脚本本身有 bug，制造大量假报警

第一版 import/export 一致性检查因为 `Path.resolve()` 与字典里的相对路径不匹配，
把 **74 处正常代码全部报成错误**。

**规则：检查脚本报错时，先怀疑检查脚本自己**（用一两个已知正确的样本校准）。

### 7.5 PowerShell here-string 吃掉引号

用 `@"..."@` 包 Python 代码时，里面的 `"` 会被吞掉，导致 `SyntaxError`。

**规则：需要写多行文件内容时用 `write` 工具**，不要塞进 here-string。
必须传参时走 URL 参数或临时 JSON 文件。

### 7.6 把 `2>$null` 误写成 `` 2>`$null ``

反引号转义让 PowerShell 把 stderr 重定向到一个**名为 `$null` 的文件**，
并在工作区留下垃圾文件。

### 7.7 追查不存在的问题

`[exit code: 1]` 反复出现，实际是 **PowerShell 把原生命令写到 stderr 的内容
当成错误**（本项目 loguru 会往 stderr 打日志）。脚本本身退出码是 0。

**规则**：看到非零退出码，先看**脚本自己打印的结果**是否完整；
用 `Write-Host "exit=$LASTEXITCODE"` 显式确认，不要盲目排查。

### 7.8 一次改动塞太多东西

一个测试失败时，无法判断是哪个改动引起的。

**规则：一次只改一类东西，改完立刻验证。**

### 7.9 汇报过长

每轮写 1500+ 字总结，是纯粹的 Token 浪费。

**规则：汇报 ≤ 300 字**，除非用户明确要求详细。

### 7.10 前端加载态传空文案

`buttonLoading(btn, '')` 让按钮只剩转圈、文字全无，看起来像坏了。
后来修的时候又留下 `btn.innerHTML = undefined` 的缺陷（会显示字面量 `undefined`）。

**规则：加载态必须保留原文案**，且恢复函数要能抵御**重复调用**
（`buttonLoading` 现在把原始内容记在 `element.dataset` 上，别改回去）。

### 7.11 写完前端没在浏览器里点一遍就交付

导致 4 个「只要点一下就能发现」的 bug 漏到用户那边，用户无法继续测试。

**规则：前端改动必须用第 6 节的浏览器探针验证过再交付。**

### 7.12 清理脚本的前缀列表过期 + SQL LIKE 未转义

- 前缀列表缺 `cc_ / wb_ / ui_ / smoke_`，导致测试中断时（夹具在 `yield` 前抛错）
  留下的用户清理不掉；
- **LIKE 里的 `_` 是「任意单个字符」通配符**，`'ui_%'` 会匹配 `uin_foo` 这类
  **真实用户名** —— 差点误删。现在用 `_like_pattern()` 转义。

**规则：这个脚本会删数据，改它要格外小心；新增测试前缀记得回来补。**

### 7.13 误以为「测试全绿」等于「没问题」

- 测试全绿，但登录页面上多了一条顶栏（CSS `display` 覆盖了 `[hidden]`）
- 测试全绿，但一个按钮点击后弹出 4 个窗（监听器泄漏）

**规则：`pytest` 只管后端逻辑。前端必须有真实浏览器验证。**

### 7.14 「测试能通过」不等于「测试不留下垃圾」

夹具在 `setup` 阶段抛异常时，`teardown` **不会执行**，
于是那批失败用例留下的用户永久残留在库里。

**规则：测试跑完后核对数据库行数**（`scripts/cleanup_demo_data.py`）。

---

## 8. ★ Token 使用纪律

用户对成本敏感（上一会话累积消耗巨大）。请遵守：

1. **一个步骤一个会话**，不要跨步骤累积。
2. **汇报 ≤ 300 字**。多用表格和短句，不要长篇解释（除非用户要求）。
3. **攒批修改**：不要为了一行改动单独调用工具，把同一文件的多个改动合并。
4. **局部读文件**：用 `offset` / `limit` 只读需要的段，不要整文件读；
   搜索用 `grep` 而不是通读。
5. **平时只跑相关测试模块**，只在里程碑跑全量 427 项（全量约 3 分钟）。
6. **不要重复排查已记录的假象**（见 7.7、7.5、7.6）。
7. **不要重复确认已验证的结论**。本文档里写「已实测/已验证」的，直接采信。
8. **不要凭记忆写代码或文档**，先采集事实（一条命令能拿到就别猜）。
9. **图片很贵**，只在真的需要看视觉结果时才让用户发截图。
10. **优先用自动化验证代替来回确认**：`pytest` + `smoke_test.py` + 浏览器探针，
    一次跑完比来回问用户快得多也便宜得多。

---

## 9. 已完成：3.8 叙事对话 + 3.9 关键词触发与向量长期记忆 —— 下一步是 3.10

### 3.8 已交付什么

**后端**（`app/narrative/` 这个包原来全是空的，现在有三个模块）：

| 文件 | 职责 |
|---|---|
| `app/narrative/prompt_builder.py` | 拼提示词：角色卡 `system_prompt` 优先，否则按人设字段自动拼装；世界书条目作为「世界设定」注入；`post_history_instructions` 作为**尾注**追加在历史之后（并标明"这不是用户说的话"，防止模型把它当成用户要求） |
| `app/narrative/context_manager.py` | token 估算（CJK 一字≈1 token，其它四字符≈1）与上下文裁剪：超预算丢最早的对话、保留最近的、把丢掉的压进 `rolling_summary`；系统提示词本身超预算时按比例截断并**如实标记** |
| `app/narrative/sessions.py` | 会话 CRUD 与序列化（列表是精简结构 + 最后一条消息预览） |
| `app/narrative/engine.py` | 对话编排：**用户消息先落库** → 拼提示词 → 裁剪 → 调模型（流式/非流式）→ 回复落库 + 更新统计 |
| `app/api/v1/narrative.py` | 7 个接口，含本项目唯一的 `async def` 业务接口（SSE 流） |
| `app/schemas/narrative.py` | 请求/响应模型（`MessageCreate.content` 先 strip 再校验长度，避免 `"   "` 走到 400 而不是 422） |

**接口**（越权规则：别人的会话一律 **404**，会话没有"公开"这一说）：

| 接口 | 说明 |
|---|---|
| `POST /narrative/sessions` | 建会话；角色卡开场白写成**第一条 assistant 消息**；不传模型则用默认模型，一个都没有也能建（只是暂时不能聊） |
| `GET /narrative/sessions` | 列表，`status=active/archived/all`，按 `last_active_at` 倒序 |
| `GET /narrative/sessions/{id}` | 详情（默认最近 200 条消息 + 提示词预览） |
| `PATCH /narrative/sessions/{id}` | 改名 / 归档 / 换卡 / 换模型（换卡只影响之后的对话） |
| `DELETE /narrative/sessions/{id}` | 删除（消息级联删除，返回删了几条） |
| `POST /narrative/sessions/{id}/messages` | 非流式发消息 |
| `GET /narrative/sessions/{id}/stream?content=…` | **SSE 流式**（`meta` / `notes` / `reason` / `delta` / `done` / `error` / `end`） |

**前端**：`web/js/views/chat.js`（会话列表 + 对话窗 + 打字机效果）、顶栏新增「叙事会话」。

### 3.8 的几条关键决定（改之前先读懂）

1. **流式接口用 GET**：浏览器原生 `EventSource` 不支持自定义请求头，只能把 JWT 塞进 URL
   （会进服务器日志/浏览器历史）。所以前端改用 **fetch + ReadableStream 手动解析 SSE**，
   但接口保留 GET 形态，方便 `curl` 直接调试。
2. **async 生成器里用 `session_scope()` 自己开 session**，不要用请求注入的 `db`：
   响应开始后请求级依赖的生命周期不再可靠。
   校验与用户消息落库放在**流开始之前**（失败直接返回 JSON 404/400，前端好处理）；
   流里再出错只能发 `error` 事件 —— 但**必须发**，不能静默断开。
3. **同步生成器用 `run_in_threadpool` 逐步驱动**（`_next_or_none`），
   否则"等模型吐字"的这段时间会占住事件循环。
4. **中途失败保留半截回复**并标注「回复中断」：用户已经看到了那些字，刷新后不该消失。
5. **空回复在流式路径改成 error 事件**（非流式路径仍由 `_raise_if_no_content` 抛异常），
   把「推理模型把配额花在思考上」这个原因讲清楚。
6. **世界书目前是"全部启用条目"注入系统提示词**，关键词触发检索留给 3.9。

### 3.9 交付了什么（已完成）

| 文件 | 职责 |
|---|---|
| `app/narrative/world_book_scanner.py` | 关键词触发：只扫最近 `scan_depth` 条消息，命中条目按 `insertion_order` 排序、按 `token_budget` 截断；预算是 0 或不足时**至少保留第一条**（否则用户看不出为什么一条都没注入） |
| `app/narrative/memory.py` | 长期记忆的业务层：一轮对话写成一条 `dialogue` 记忆（**确定性 ID** ⇒ 幂等）、按语义召回（默认只在会话内、相似度门槛 0.35）、拼「相关回忆」小节、按会话/按用户清理。**所有函数都不抛异常** —— 记忆坏了只降级 |
| `engine.build_session_prompt()` | ★ 把「关键词扫描 + 记忆召回 + 提示词拼装」抽成**一个**函数，对话与"查看提示词"预览共用，保证**预览 == 真实请求** |
| `api/v1/narrative.py` | 新增 `GET/POST /sessions/{id}/memories`、`DELETE /memories`（这里的失败必须报错，因为这是用户主动要的结果，不能像对话那样降级） |
| `schemas/world_book.py` | ★ 新增 `scan_depth` / `token_budget` 入参 —— 之前这两个值只能从角色卡带进来，界面根本改不了，关键词触发做出来也没法调 |
| `web/js/views/chat.js` | 「记忆」面板（检索 / 手动记一条 / 清空）+ 头部显示「世界书命中 N 条 · 回忆 N 条」；`books.js` 增加两个扫描参数输入框 |

行为变化（**注意，这是破坏性的语义变更**）：

| 项目 | 3.8 | 3.9 |
|---|---|---|
| 世界书注入 | 全部启用条目 | **只注入最近 scan_depth 条消息里提到关键词的条目** |
| 长期记忆 | 无 | 每轮对话写入向量库；发消息前按语义召回 top_k 条拼进系统提示词 |

### 3.9 的验收结果（全部满足）

- [x] `pytest -q` 全绿，**505 passed**（原 479 + 新增 26：world book 扫描 6 + 记忆 16 + 参数 3 + 配套 1）
- [x] `tests/test_memory.py`（16 项）真实连 ChromaDB：语义召回、相似度门槛、幂等、会话隔离、
      **向量库故障降级不报错**、删会话连带清记忆、清空记忆不碰对话记录
- [x] `tests/test_narrative.py` 增加纯单元的关键词扫描用例（命中/未命中/禁用/scan_depth/预算/大小写）
- [x] 正反两条断言：命中时提示词里有该设定、未命中时**没有**；有回忆时有「相关回忆」小节、没有时不出现
- [x] `scripts/smoke_test.py` 新增「世界书关键词触发 + 向量长期记忆」步骤，**122 项全过**、跑完无残留
- [x] 真实浏览器（CDP）验证：界面上显示「世界书命中 1 条 · 回忆 1 条」、
      提示词弹窗只含命中的条目、「记忆」面板能检索到 2 条并能新增到 3 条、控制台无报错
- [x] 更新 `README.md` / `docs/handoff.md` / `docs/pitfalls.md`

### 3.10 收尾 —— 已完成的部分

| 事项 | 做了什么 | 验收 |
|---|---|---|
| **README 收口** | 补「长期记忆与关键词触发」一节（行为变化、参数含义、怎么自查）、前端能力表补上叙事会话与消息级操作、`tests/ 531 项`、新增 `docs/paper-materials.md` 入口 | 文档里的数字与 `pytest` / `smoke_test` 实际输出一致 |
| **论文素材** | 新建 `docs/paper-materials.md`：①异构适配层字段级差异表 ②上下文预算公式 + **可复算命令** ③双通道记忆对比表 ④6 条「问题→根因→修复」表 ⑤可直接引用的 10 条设计原则 ⑥建议的 8 张图与数据来源 | 每个数字都能指回**文件行号**或**复现命令**，未实测的一律标注 |
| **接口文档** | 核对 `/docs`：全部 summary 与参数描述均为中文；清理了 `router.py` 里"后续启用 memory"这条**过期注释**（记忆接口其实已在 `narrative` 下上线） | Swagger 逐页扫过 |
| **前端小修** | ①默认落地页从「角色卡」改成**「叙事会话」**（与顶栏顺序一致）②`index.html` 加内联 SVG favicon，消掉控制台的 `favicon.ico 404` ③`sendMessage` 结束后按输入框内容决定发送按钮是否可用（原来无条件启用） | 浏览器探针全过、控制台无报错 |
| **数据清理** | 探针账号 `ui_probe_browser`（前缀 `ui_`）由 `scripts/cleanup_demo_data.py` 清理干净；向量库里 16 个**拥有者账号已不存在**的孤儿集合已清掉（清理脚本现在会自动报告孤儿集合，但**不自动删**） | 见第 13 节 |
| **Electron 打包** | **未做**（用户已确认要「便携免安装版」，排在用户实际验证之后） | 打包后能登录、能发起一轮流式对话；`.env` 不被打进安装包 |

#### 3.10 + 用户反馈修复的验收结果（本轮实测）

- [x] `pytest -q` → **531 passed**（清理死代码前后各跑一次都全绿；新增 24 项：
      消息级操作 13 + base_url 校验 13 中的部分 + 配套）
- [x] `scripts/smoke_test.py` → **142 项全过，exit=0**，跑完行数与测试前一致
- [x] `scripts/ui_probe.py`（真实浏览器 CDP）→ **65 项全过**，
      除"故意删会话"那一次 404 外**没有任何失败请求**
- [x] 探针实测：流式**逐字增长 125 次 / 16.24s**，最终 127 字
- [x] 记忆对照组：首轮预览**不含**「相关回忆」，历史里有对话后预览**含**「相关回忆」与「回忆 N 条」
- [x] 回归项：打开并关闭「设置」弹窗后，**发送按钮仍可点、消息真的发得出去**（委托监听器没被误 abort）
- [x] 用户反馈三件事：会话消失不再报错误码、消息级操作可用、Base URL 填错能被前端削掉 + 后端拒绝 + 弹窗给出排查顺序

#### 3.8 验收结果（全部满足）


- [x] `pytest -q` 全绿，**479 passed**（原 427 + 新增 52，没有删任何用例）
- [x] `tests/test_narrative.py`（48 项）覆盖：会话 CRUD、越权 404、发消息落库、
      上下文裁剪与滚动摘要、SSE 事件顺序与格式、error 事件传递、角色卡被删后的兜底
- [x] 流式路径全部用**假适配器**测（不联网、不打真实 API）
- [x] 真实浏览器验证：7.39 秒里文字增长 54 次（★ 确实是逐字出现），控制台无报错，
      刷新后对话仍在，`data-act` 缺失的 bug 就是这一步抓到的
- [x] `scripts/smoke_test.py` 增加「叙事会话与对话」步骤（含本机假模型 + 真 SSE），**106 项全过**
- [x] 数据库跑完无残留（直连 MySQL 核对行数与测试前一致）
- [x] 更新 `README.md` / `docs/pitfalls.md`（新增第 13、14 条）

---

## 9.5 ★ 用户实测反馈的七件事（全部处理完，重要）

> 这一节是**用户真的打开界面点过之后**提出来的，比任何自测都值钱。
> ① 在 `web/js/views/chat.js`；② 在 `app/narrative/sessions.py` + `chat.js`；
> ③ 在 `app/llm/base.py`；④ 在 `app/services/world_book_service.py`；
> ⑤ 在 `web/js/views/providers.js` 等四个视图；⑥ 是环境问题（已加检查命令）；
> ⑦ 在 `app/main.py` + `web/index.html` + `web/js/boot.js`。

### ① 删掉角色卡后，对话界面顶着一个「会话不存在」的红条

**现象**：删角色卡时勾了「连同对话记录一起删除」，回到会话页右侧显示
`会话不存在 request_id: …`，而左边列表是空的 —— 两边信息互相矛盾，
用户完全不知道是哪个会话、发生了什么。

**根因**：前端把 `state.sessionId` 记在内存里，会话被删后仍然拿这个 id 去请求详情，
404 被当成"错误"直接摊在界面上。

**修复**（`chat.js::showMissingSession`）：404 时**清掉记住的会话 id**，
右侧换成一句解释＋下一步该做什么（"这个会话已经不在了……点左上新建一个"），
不再显示错误码。同时 `refreshSessions` 会把左侧列表刷新成空状态。

**顺带修掉的同类问题**：直接点「重新生成」时若会话已删，模板串会拼出
`…/sessions/null/regenerate`，后端返回 422，用户看到的是"请求失败"。
现在会先校验会话 id 是否存在。
> ★ 这个 bug 是**探针的"没有意外失败请求"这条断言抓出来的** ——
> 该断言值得保留（见 `scripts/ui_probe.py`）。

### ② 消息级操作：复制 / 编辑 / 撤回 / 重新生成 + Token 统计

**后端**（三个新接口，语义各不相同，别混）：

| 接口 | 作用 | 删掉什么 |
|---|---|---|
| `PATCH /narrative/sessions/{id}/messages/{mid}` | 编辑一条 user 消息 | 它**之后**的全部消息 |
| `POST /narrative/sessions/{id}/messages/{mid}/retract` | 撤回一条 user 消息 | 它**自己**及其之后的全部消息，并把原话返回给前端 |
| `GET /narrative/sessions/{id}/regenerate?message_id=` | 重新生成一条 assistant 回复 | 它**自己**及其之后的全部消息（**不会多出一条用户发言**） |

三条硬约定（改之前先读懂）：

1. **只能编辑/撤回 user 消息**，assistant 是模型产物，只能"重新生成"。
   改它等于伪造历史，模型会以为那是自己说过的话。
2. **删消息后必须重算统计**（`sessions._recalculate_stats`）。
   不重算就会出现"只有 3 条消息却显示累计 12000 token"这种算不回来的数字。
   代价是 `total_tokens` 从"厂商真实用量"变成"剩余消息估算值"——已在代码里如实标注。
3. **重新生成必须删掉被替换的那条回复自己**（`delete_messages_from(inclusive=True)`）。
   不删的话新回复会追加在后面，历史里出现"两条针对同一句话的回复"。
   > ★ 这个 bug 是**写 pytest 时断言抓到的**：只检查 `> id` 让中间那条留下了。
   > 而"不传 message_id"的路径锚点是 user 消息，必须 `inclusive=False`，
   > 否则会把用户那句话一起删掉（同一批断言也抓到了）。

**前端**：每条消息悬停出现操作按钮（复制人人有；编辑/撤回只在 user 消息；重新生成只在 assistant 消息）。
按钮**默认 `opacity: 0` 而不是 `display: none`**，鼠标移上去不会把下面的内容顶下去。
「编辑」保存后会自动走一次「重新生成」，这样历史里不会留下两条用户发言。

**Token 统计**：对话头部显示「本轮 输入 N · 输出 N · 其中思考 N · 合计 N ｜ 累计 N」。
思考 token 单独列出来，是因为推理模型可能把 88% 的输出配额花在思考上（有实测数据），
不单独显示就完全看不出钱花在哪。

### ③ 接不通第三方模型（ChatGPT / 豆包）—— 根因是 Base URL 填错了

**用户库里的真实配置**（查数据库看到的，不是猜的）：

```
url='https://ai.zyyun.xyz"/v1'                                  ← 多了一个引号
url='https://ark.cn-beijing.volces.com/api/v3/responses'        ← 多了一段 /responses
```

**根因**：本项目的请求地址 = `base_url + /chat/completions`。
豆包那条会打到 `…/api/v3/responses/chat/completions` ——
而 `/api/v3/responses` 是火山方舟的**另一套协议**（Responses API），
该路径不存在，于是对方返回
「指定的模型不存在，或当前 API Key 无权访问该模型」：
**上游把"地址拼错了"伪装成"模型名错了"**，用户怎么改模型名都没用。

**修复（三道防线）**：

1. **前端提交前自动削掉**已知的接口结尾（`/chat/completions`、`/responses`、`/messages`…）
   并 toast 提示改成了什么（`providers.js::normalizeBaseUrl`）。
2. **后端构造适配器时直接拒绝**这种 base_url，并给出"应该填成什么"
   （`base.py::_reject_endpoint_in_base_url`，只在 `OpenAICompatibleProvider` 里调用 ——
   Anthropic 适配器自己会判断用户是否已把 `/v1/messages` 填全，基类加了会**误拦合法用法**，
   这一点有专门的回归测试守着：`test_anthropic_base_url_may_include_full_path`）。
3. **连通性测试失败时弹窗**列出「实际请求地址 / 上游 HTTP 状态 / 上游原话 / 原始响应体」，
   再按状态码给出排查顺序（404 → 先拉模型列表，拉不到就是 URL 错）。
   另外表单里加了常见厂商的 Base URL **一键预设**（含正确写法与模型名怎么填）。

> ★ **一条真实数据待用户自己改**：`ChatGPT` 这条配置的 base_url 里有一个多余的 `"`
> （`https://ai.zyyun.xyz"/v1`）。这是用户的真实数据，**没有擅自修改**，
> 需要他自己在界面上删掉那个引号。
> 好消息是上面的第 1 道防线会在下次保存时把这类问题暴露出来。

**排查这类问题的通用顺序**（也写进了界面提示）：先点「模型」拉列表 →
拉不到 = Base URL 错；拉得到但名字对不上 = 模型名错；
都对 = 账号没开通该模型。

### ④ 世界书页整页报 `Out of sort memory`（1038）—— 用户第二轮反馈

**现象**：导入一本大世界书之后，「世界书」页变成一片红：
`(1038, 'Out of sort memory, consider increasing server sort buffer size')`。

**根因**：列表查询用 `select(WorldBook)` 取整行，而 `entries` 是一列几百 KB 的 JSON，
**它被塞进 MySQL 的排序缓冲**（本项目用默认的 256KB）→ 溢出。
实测：约 150KB 的数据不报错，**约 880KB 才稳定复现**（所以小数据测试发现不了）。

**修复**（`world_book_service.list_book_briefs`）：让排序那一步**绝不带 entries** ——
① 只查标量列 + ORDER BY + 分页；② 用 id 单独取 entries 一列，在 Python 里数条目数。
详见 `docs/pitfalls.md` 第 19 条。

### ⑤ 点一次按钮弹出好几个一样的窗 —— 用户第二轮反馈

**现象**：在「模型配置」页点「模型」，同时弹出 3 个一模一样的弹窗。

**根因**：视图会**调用自己**（测试/删除/保存后刷新列表），而刷新会把挂在常驻
`#view` 上的委托监听器**重新绑一遍** → 监听器 1→2→4 个 → 一次点击被分发 N 次。
**第一次进页面完全正常，必须先操作一次让视图自我刷新，第二次点击才会弹 2 个** ——
这就是它躲过自测的原因。

**修复**：把"绑监听器"与"刷新列表"彻底拆开（`attachProviderListeners` vs
`renderProviderList`），并给刷新加并发闸门（`refreshing`）；books/cards 用
`eventsBound` 守卫做同样的事。探针新增「10.6.弹窗叠加」三项盯着它。
详见 `docs/pitfalls.md` 第 20 条。

### ⑥ 端口被占用 / MySQL 连接被重置 —— 这两条**不是 bug**

- `[Errno 10048] error while attempting to bind on address ('127.0.0.1', 8000)`：
  说明 8000 端口上**已经有一个服务在跑**（旧进程没关，浏览器里那个页面还连着它）。
  先确认再启动，别重复起：见第 13.1 节的检查命令。
- `MySQL server has gone away (ConnectionResetError 10054)`：
  是我在**临时复现脚本**里往一张表插入约 8MB 的 JSON 触发了
  MySQL 的 `max_allowed_packet`（4MB）限制，**与本系统无关**，脚本已删除。

### ⑦ 打开控制台「一片空白」—— 浏览器缓存导致 ES Module 版本混用（已根治）

**现象**：服务日志一切正常（全是 200/304），但浏览器里整页空白，控制台只有一行红字：

```
Uncaught SyntaxError: The requested module '../ui.js' does not
provide an export named 'freshViewSignal' (at chat.js:29:3)
```

**根因**：前端是原生 ES Module，浏览器**按 URL 缓存**每个 `.js`。
只改了部分文件时，用户浏览器会拿「新的 `views/*.js` + 旧的 `ui.js`」混用，
而 `import { freshViewSignal }`（本轮新加的）在旧 `ui.js` 里不存在 ——
**ES Module 遇到"导入不存在的导出"会整包加载失败**：白屏 + 一行不显眼的红字。
服务器上的 `ui.js` 明明是新的（实测 `freshViewSignal` 就在第 266 行），问题全在浏览器那一侧。

**⚠️ 踩过的弯路（重要，别再重复）**：
第一版修复只是给 `/console` 加 `Cache-Control: no-cache, must-revalidate`，
以为"下次刷新就会回源校验"。**结果用户按了 `Ctrl+F5` 依然白屏**，
服务日志里照样全是 `304` —— 浏览器宁可复用旧副本。
**结论：对 ES Module 而言，"指望浏览器自己重新校验"是不可靠的方案。**

**真正的修复（两层，缺一不可）**：

1. **模块 URL 带版本号（importmap）** —— `web/index.html` 里由后端动态渲染
   `<script type="importmap">`，把裸名 `hne/ui` 映射到 `/console/js/ui.js?v=<指纹>`。
   `app/main.py::_frontend_version()` 用**所有前端文件的 mtime+size** 算 12 位指纹
   （故意不用内容哈希：不用把文件读一遍，一次 `os.scandir` 就够）。
   版本一变 → 所有模块 URL 一起变 → 浏览器**没有旧副本可复用**，必须重新下载；
   版本没变（只改后端）→ URL 不变，照旧吃缓存，不牺牲速度。
   配合把模块间导入从相对路径（`'../ui.js'`）全部改成裸名（`'hne/ui'`）。

2. **boot.js 自愈** —— `index.html` 自己也可能被缓存住（带着旧版本号）。
   `web/js/boot.js` 用 `fetch('/console/', {cache:'no-store'})` 问一次真实版本，
   对不上就 `location.replace('/console/?v=<新版本>')` 自动重载；重载也没救回来时
   才显示一条红色提示条（`.boot-stale`），而不是让用户对着白屏发呆。

**防回归测试**（`tests/test_console.py`，新增 8 项）：
入口页占位符必须被替换且 `no-store`、各模块 URL 必须带**同一个**版本号、
`web/js/**` 新增模块必须登记进 importmap、模块间**禁止**相对导入、
改文件后版本号必须变、`boot.js` 的关键行为不能被删。

**顺带修掉的两个东西**：

1. **探针的假通过**：第 6 节「编辑后的内容出现在历史里」原本等的是"最后一条气泡不在
   pending"，但编辑流程里有一个**危险窗口**——那时最后一条消息还是编辑前的旧回复
   （既不 pending 也有内容），断言可能抢在重渲染前取样。改成等"编辑后的文字真的出现
   在历史里"（等一个必然发生的事件），连跑三次全过。
2. **危险窗口对真实用户的影响**：那 100ms 里旧消息仍可点，用户点「撤回 / 重新生成」
   会按旧 id 删数据、把刚编辑的内容抹掉。现在整个窗口用 `state.streaming` 罩住
   （`chat.js::openEditDialog`）。

**验证**（本轮全部实跑过）：`pytest -q` → **586 passed**；
`smoke_test.py` → **142 项全过**；`ui_probe.py` → **65 项全过 ×3 次连跑**。

> ★ 给用户的自救（现在基本用不上了）：`Ctrl + F5`；
> 彻底一点：F12 → Application → Clear site data。
> ★ **改了 `app/` 下的 Python 必须重启服务**；只改 `web/` 则刷新页面即可
>   （有了版本号之后，前端改动**不需要**再让用户清缓存）。
> 详见 `docs/pitfalls.md` 第 21 条。

---

## 9.6 ★ 用户第二轮反馈（三条：1 个真 bug + 1 组界面改进 + 1 个后续需求）
### ① 角色卡页：切过筛选之后，页面内所有按钮都点不动了（真 bug，已修）

**用户原话**：

> 点击角色卡里的「公共卡库」或「全部可见」之后（可能还包括其他按钮，
> 点了「导入 PNG」之后也发现动不了其他键了），这个页面的按钮就失效了（点击无反应）。

**根因**（详见 `docs/pitfalls.md` 第 22 条）：
`cards.js` 里"监听器只绑一次"的守卫，**判据用的是调用方传进来的 signal，
绑定的对象也是它**；而筛选那条路径**根本没传 signal**（`undefined`），
"自己重渲染自己"时又可能传进**已经 abort 的旧信号**。
`addEventListener(..., { signal: 已abort })` 等于**不注册** ——
于是委托监听器一诞生就是死的，而 `eventsBound` 已置 true，再也不会重绑。

**为什么用户描述的分界线那么关键**：
"页面内按钮全废、顶栏还能点" 正好说明坏的是挂在 `#view` 上的**那一层事件委托**，
而不是渲染或接口。这条线索直接省掉了大量排查。

**修复**：一律绑 `activeSignal.signal`（当前批次），守卫也用同一个值判断死活；
信号已死就**不绑**；拿不到信号时 `console.error` 明确报出来，绝不静默绑死监听器。

**同时补上探针的盲区**（否则这条 bug 永远测不到）：
原来探针只有一个账号，而 `scope=public` 查的是"**别人**公开的卡"，
所以公共卡库永远是空态 —— 现在探针会多建一个账号 `ui_probe_other` 放一张公开卡；
并且新增第 10.7 节：切三种筛选、**每次都点一次卡片「详情」**，断言弹窗真打开。

> ★ 另一个同类坑：探针起无头浏览器时没指定窗口尺寸，默认 800px 宽，
> 正好落进 CSS 的 `max-width: 900px` 移动端断点 ——
> 桌面专属功能（两栏布局、拖动调节大小）**根本测不到**。现在固定 1360×1000。

### ② 界面改进：卡片美化 + 对话区可拖动调节大小（已做）

用户原话：

> 部分角色卡可能需要前端美化，这应该也实现。且我发现对话窗口大小不是很好看，
> 考虑放大或者考虑像窗口一样的让用户自己决定大小（拖动变化）。

- **角色卡美化**（`web/css/styles.css`）：品牌色顶边、圆角方形头像、
  名称/简介的信息层级、开场白改成带品牌色左边线的引用块、卡片悬停微抬。
  做的是"信息层级"而不是堆阴影 —— 答辩时它是第一眼看到的东西。
- **对话区可拖动**（`web/js/views/chat.js::initChatResize` + CSS）：
  右下角小三角手柄，**左右拖改会话列表宽度、上下拖改对话区高度**，
  双击恢复默认，尺寸存 `localStorage`。
  用 pointer 事件 + `setPointerCapture`（拖出手柄也不断），
  窄屏（上下堆叠）自动隐藏手柄。
  ★ 为什么不用纯 CSS 的 `resize`：它做不到"一个手柄同时改宽和高"。

### ③ 后续需求：酒馆式「预设」（用户已给样例文件，**尚未开始**）

用户原话：

> 这不是系统的问题，只是后续想做的东西：预设。具体是什么你可上网参考酒馆。
> 我有预设文件可以给你：`<本机下载目录>\赤狐DeepSeek(4).json`

**已做的调研**（读的就是那个文件，16787 字节，SillyTavern completion preset）：

| 部分 | 内容 | 与本项目的关系 |
|---|---|---|
| 采样参数 | `temperature / top_p / top_k / top_a / min_p / frequency_penalty / presence_penalty / repetition_penalty`、`openai_max_context`、`openai_max_tokens` | 本项目 `GenerationParams` 只**显式**支持 temperature / top_p / frequency_penalty / presence_penalty / stop / seed / max_tokens；`top_k / top_a / min_p / repetition_penalty` 目前会落进 `extra` **原样透传给上游**（对 OpenAI 兼容接口会被忽略）★ 这与"拒绝静默降级"的项目原则冲突，要做就得先补字段与校验 |
| 提示词块 | `prompts`: 21 个块，每块有 `identifier`（main / worldInfoBefore / charDescription / …）、`role`、`content`、`injection_position`、`injection_depth`、`system_prompt`、`forbid_overrides` | 本项目目前是**固定装配**（`prompt_builder.py`：角色卡字段 → 人设提示词 → 世界书 → 记忆 → 历史 → 尾注）。要做预设就得把"装配顺序"变成**数据驱动** |
| 顺序 | `prompt_order`: 按 `character_id` 指定各 `identifier` 的启用与顺序 | 对应"哪些块、按什么顺序进提示词" |

**结论**：这是**两件事** ——
(a) 采样参数预设；(b) 提示词块装配预设。
**用户已确认 (a)+(b) 一起做（P1→P4 全做）**，见下面 9.7。

---

## 9.7 ★★ 提示词预设（酒馆式 completion preset）—— 已实现

> 用户的话："角色卡设定通常设定在世界书里，而预设则主要是规范 AI 的行为，
> 比如一些破甲，规则都是写在里面的，所以预设是十分有必要的。"

### 一句话架构

```
角色卡  = 这个角色是谁
世界书  = 这个世界有什么
预设    = ★ 模型应当怎么工作（规则 / 破甲 / 输出格式 / 采样参数）
```
三者正交。同一张卡配不同预设，模型的听话程度可以完全不同 ——
所以预设是**独立的一等实体**，不能塞进角色卡字段里。

### 新增/改动的文件

| 文件 | 作用 |
|---|---|
| `app/narrative/presets.py` | ★ 核心：解析酒馆预设、宏替换、**按块装配**（纯函数，不碰数据库） |
| `app/db/models/prompt_preset.py` | `prompt_presets` 表（配置存 JSON，原样保真） |
| `app/services/prompt_preset_service.py` | 导入/CRUD/启用/导出/**装配预览** |
| `app/api/v1/prompt_presets.py` | 13 个接口（见文件头注释的接口一览） |
| `app/schemas/prompt_preset.py` | 请求/响应模型（含参数生效性分类） |
| `scripts/migrate_db.py` | ★ 幂等补列脚本（`--dry-run` 可预演），本项目不引入 Alembic |
| `web/js/views/presets.js` | 预设页：列表 / 导入 / 块清单（启停·排序·注入方式·深度）/ 装配预览 |
| `web/js/views/chat.js` | 会话设置里可选预设；「查看提示词」升级为**按块展示来源** |

### ★ 四个必须记住的实现要点

**1. `injection_position` 的语义曾经读错（最容易搞错的一处）**

```
0 = 归入**系统提示词**（在那个位置按顺序拼进去）
1 = 按 injection_depth **插进对话历史中间**
```
我一开始把 0 读成"按顺序装配"、1 读成"深度注入"，又看到酒馆给很多块都填了
`injection_depth`，于是误判"没进顺序表的块都是深度注入"。
**打开真实文件一看：所有块都是 pos=0** —— 那些 `role=user/assistant` 的块
（main / nsfw / 6 个 UUID 块）是"以 user 或 assistant 的语气写进系统提示词"，
并不是插进历史。判据因此改成**只看 `injection_position`**。

**2. 深度注入必须"裁剪之后才插回"**

`prepare_context` 会从**最早**的消息开始丢来满足预算。
深度块如果在拼装阶段就插进去，会被当成"旧消息"丢掉（而且不报错）；
如果不参与裁剪计数，又会挤爆预算。
正确顺序：**先裁剪，再按"倒数第 depth 条之前"插回**
（`context_manager.inject_depth_messages`，下标 = `总条数 - depth`，夹到合法范围）。

**3. 标记块（marker）——理解整套机制的钥匙**

`charDescription / charPersonality / scenario / worldInfoBefore / worldInfoAfter /
dialogueExamples / chatHistory / jailbreak` 这些块**自己没有正文**，
它们是"占位符"：把系统里对应的内容插在这里。
推论（反直觉但刻意保留）：**禁用某个标记块 = 那段内容不注入**。
关掉 `chatHistory` 就是"不给模型看对话历史"。

**4. 降级与不支持都要如实说**

| 情况 | 处理 |
|---|---|
| 块不认识/没内容源（`personaDescription`） | 保留在配置里，装配跳过，界面标注"本系统不支持" |
| 宏不认识（`{{getvar::x}}`） | **原样保留**（不是替换成空串）+ 界面列出，绝不悄悄吃掉半句话 |
| 云端无效的参数（`top_k / top_a / min_p / repetition_penalty`） | 照常保存（导出回酒馆不丢），但界面明确标注"云端会忽略" |
| Anthropic 不允许中途 system | 深度 system 块**降级成 user + 加 `[系统指令 · 并非用户发言]` 声明**，并把降级写进 notes |
| 会话绑定的预设被删除 | 外键 `SET NULL` → 自动回落"全局默认预设 → 内置装配"，**故事不受影响** |

### ★ 零回归的硬约定

`build_prompt(preset=None)` 必须产出与加这个功能**之前逐字节相同**的消息序列。
所以代码里"有预设"和"没预设"是**两条明确分开的路径**，不做隐式混合；
现有 500+ 条测试与用户既有会话都依赖这一点。

### 会话绑定优先级（写死并测出来）

```
会话显式绑定的预设  >  用户全局默认预设  >  不用预设（内置装配）
```
`SessionDetail` 同时返回 `prompt_preset`（绑定）与 `effective_preset`（实际生效，带 `from`），
因为用户需要知道"我没绑，但系统替我用了哪一套"。

### 验证

| 层 | 结果 |
|---|---|
| `tests/test_presets.py`（纯函数语义） | **24 项**：导入保真/顺序启停/宏/标记块/深度注入/真实文件 |
| `tests/test_prompt_presets.py`（接口接线） | **23 项**：导入/往返/权限/块编辑/绑定/**内容真的到了模型那里**/回落/预览 |
| `pytest -q` | **586 passed** |
| `smoke_test.py` | **142 项全过，exit=0** |
| `ui_probe.py` | **65 项全过**（10.8 预设 8 项 + 10.9「编辑后仍可点」4 项 + 10.10 HTML 开场白 3 项） |

> 数据库改动是**无损**的：`scripts/migrate_db.py --create-tables` 只建表 + 补一列
> （`narrative_sessions.prompt_preset_id`，允许 NULL → 加列瞬时完成）。
> 已核对：`users=1 / sessions=1 / messages=1`，用户 `真实用户（本机 DB 里的 id 已隐去）` 完好。

---

## 9.8 ★ 用户第三轮反馈（五个问题，全部处理）

> 这一轮的五个问题里，**两个是同一个根因的第二次复发**，
> 还有一个是"我自己改出来的白屏"。值得单独记一笔。

| # | 用户描述 | 真因 | 处理 |
|---|---|---|---|
| ① | "在角色卡界面中点击按钮后按钮就失效了" | `books.js` 里"监听器绑死"的老坑**再次出现**（第 22 条的同类）；`cards.js` 已修但 `books.js` 漏改 | 两个视图统一改成"记住绑过哪个信号 + 一律绑当前批次"，见 `docs/pitfalls.md` 第 30 条 |
| ② | "世界书点保存修改会报错" | 接口本身**正常**（已用真实 36 条 / 13 条世界书原样 PATCH 验证 200）。真正的问题是**错误信息看不出是哪个字段**：后端 422 的 `detail` 是数组，而前端 `toDisplay()` 只认对象里的 `detail.errors` | 修 `api.js::toDisplay`：数组与对象两种形状都认，toast 里直接列出 `字段：原因` + `suggestion`。★ 这条是"报错不可读"本身就该当成 bug 修 |
| ③ | "世界书点编辑后再回到界面一直卡在加载中" | **与①同一个根因**：`renderBookList` 里请求回来时 `signal.aborted` 为真 → 直接 return，页面永远停在"加载中"占位 | 随①一起修；探针加第 10.9 节（列表→编辑→保存→返回→再点）守住 |
| ④ | "提示词预设不支持拖动文件" | 导入弹窗只有文本框 | 新增 `ui.js::bindFileDrop`（公共函数，避免"有的地方能拖有的只能粘贴"），预设导入支持**拖 JSON 进来**并自动填名称与来源文件名 |
| ⑤ | "角色卡的美化没展示出来（HTML 包装的开场白）" | 开场白在规范里是纯文本，但现实中很多人用 HTML 写"状态栏卡片"。界面一律 `esc()` 输出，用户看到的是一堆源码 | 新增 `ui.js::sanitizeHtml` + `looksLikeHtml`：看起来像 HTML 就给「**渲染视图 / 源码**」两个页签；渲染前按白名单清洗（**不用 innerHTML 解析**，用惰性 `DOMParser`；只留排版标签与 `style/class`，任何 `on*` 与 `href/src` 全部丢弃） |

### ★ 顺带修掉的一个"我自己造的白屏"

做⑤与④时往 `ui.js` 插新段落，**吃掉了一个右花括号**，于是 `export` 跑进了函数体：

```
SyntaxError: Unexpected token 'export'   ← 全站模块加载失败，控制台白屏
```

**而 `node --check` 说没问题**（它默认按 CommonJS 解析，对 ES Module 既假阳性又假阴性）。
真正能验证的只有"按 ES Module 真的 import 一次"。
于是做了三件事（详见 `docs/pitfalls.md` 第 29 条）：

1. 前端改动一律用 `node --input-type=module -e "import('./web/js/ui.js')…"` 验证；
2. **探针把"模块可加载"改成前置条件**：加载失败就立即中止后续检查，
   只报这一条 —— 否则页面"半活"，后面的点击会产出 20 条假失败把真因埋掉；
3. 加固探针自身：`cdp.eval` 出错返回的是 dict，直接 `.strip()` / 做减法会让
   **整个探针崩溃**（前面已通过的检查连报告都写不出来）→ 新增 `cdp.text()` / `cdp.num()`。

### 验证

`pytest -q` **586 passed** · `smoke_test.py` **142 项全过** · `ui_probe.py` **65 项全过**
（新增：10.9「编辑→保存→返回→再点」4 项、10.10「HTML 开场白渲染 + 页签切换」3 项）。

> 你那份真实数据**没有被测试碰过**：测试全部用临时账号 + 数据副本。

---

## 10. ★★ 用户偏好与红线

### 红线（违反会造成不可逆损失）

1. **不要跑 `init_db.py --drop`！**
   数据库里有用户自己的真实数据：账号 `真实用户`(id=887)、
   模型配置 `Deepseek`、世界书 `——魔法少女的魔女审判——`(id=1207)。
   （用户自己会删掉那条填错 Base URL 的 `ChatGPT` 配置，**不要代劳**。）
   **这些绝不能删。** 需要重建表时先用 `--dump-sql` 对比，或先备份。

2. **清理数据只走 `scripts/cleanup_demo_data.py`**，它只删测试前缀的账号。
   绝不要写「删除所有用户」这类语句。

3. **不要删除任何看起来"多余"的测试**。用例数是质量基线。

4. **不要在测试里调用真实大模型 API**（`live` 标记的用例除外，且必须可跳过）。

### 用户偏好

| 偏好 | 说明 |
|---|---|
| **诚实报告** | 不确定就说不确定；做不到就说做不到。用户明确说过「防止你没用真实环境，而是空跑骗我」 |
| **根因优先** | 用户明确要求：遇到问题**先自己解决，解决不了再上网查，再尝试解决**。不要绕过去 |
| **先自测再交付** | 交付前必须自己跑过测试/浏览器验证，不要交付半成品让用户当测试员 |
| **注释要能看懂** | 中文注释，解释「为什么」，用户会亲自审查代码 |
| **不要冗余功能** | 用户否决过「记录多个模型的探测历史」。加功能前先想清楚是否必要 |
| **一次一步，等确认** | 用户要求一步一确认（但可合并相邻小步骤） |
| **成本敏感** | 见第 8 节 |

---

## 11. 文件地图（需要时按图索骥，不要整目录通读）

### 后端核心

| 文件 | 作用 |
|---|---|
| `app/main.py` | 应用工厂、lifespan、CORS、请求 ID、挂载 `/console` |
| `app/core/config.py` | pydantic-settings，`HNE_` 前缀 |
| `app/core/exceptions.py` | `AppException` + 7 类 LLM 错误 + 4 个全局处理器 |
| `app/core/security.py` | bcrypt、JWT、Fernet、密钥脱敏 |
| `app/db/mysql.py` | Engine / Session 单例（★ 有死锁修复的历史） |
| `app/db/chroma.py` | ChromaDB 客户端、嵌入指纹校验 |
| `app/llm/` | **论文核心**：`params.py`（参数与预算）、`schema.py`（数据结构）、
`base.py`、`openai_compatible.py`、`anthropic.py`、`errors.py`、`factory.py`、`diagnostics.py` |
| `app/narrative/` | **叙事引擎**：`prompt_builder.py`（拼提示词）、`context_manager.py`（token 估算与裁剪）、`sessions.py`（会话 CRUD）、`engine.py`（对话编排与落库）、`world_book_scanner.py`（关键词触发）、`memory.py`（长期记忆的业务层） |
| `app/api/v1/narrative.py` | 叙事接口，含唯一的 `async def`（SSE 流）。**改它之前先读文件顶部的说明** |
| `app/schemas/narrative.py` | 叙事相关的请求/响应模型 |

### 前端

| 文件 | 作用 |
|---|---|
| `web/js/api.js` | API 客户端、token、**请求日志广播** |
| `web/js/ui.js` | DOM 助手、`esc()`、弹窗、表单片段、`buttonLoading` |
| `web/js/app.js` | 路由 + **AbortController**（监听器生命周期）+ 请求日志面板 |
| `web/js/views/*.js` | `cards` / `books` / `providers` / `auth` / **`chat`（对话界面 + 手写 SSE 解析 + 消息级操作）** |
| `web/js/views/chat.js` | ★ 对话视图。**改它之前先读文件顶部的「监听器泄漏」说明**；流式回调统一走 `streamHandlers()`，不要另写一份 |

### 脚本

| 文件 | 作用 |
|---|---|
| `scripts/smoke_test.py` | 端到端冒烟：真实 HTTP + **直连 MySQL 双通道核对**，142 项 |
| `scripts/ui_probe.py` | ★ **真实浏览器探针**：自己起假模型 + 造数据 + 用 CDP 点一遍界面 + 收尾清场，65 项。改前端之后**必须跑它** |
| `scripts/cleanup_demo_data.py` | 清理测试遗留用户（前缀已转义）+ **报告**向量库里"拥有者已不存在"的孤儿集合（只报告不自动删，删除命令会打印出来） |
| `scripts/init_db.py` | 建表 / `--dump-sql` / `--drop`（**危险**） |
| `scripts/fake_openai_server.py` | 本地假 OpenAI 兼容服务（逐字吐字），用于演示流式与冒烟测试，**不花钱不联网** |

**跑浏览器探针**（前端改动后的标准动作）：

```powershell
# 先把服务跑起来
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 再跑探针（它自己管假模型、造数据、清场；报告与截图写在 data/ 下）
.\.venv\Scripts\python.exe scripts\ui_probe.py
#   退出码 0 = 全过，2 = 有失败项，1 = 环境问题
#   --headed 可以看着它点；--keep 保留测试数据方便自己翻库
```

> 探针里最有价值的一条断言是「**界面上没有意外的失败请求**」——
> 本轮它抓出了 `…/sessions/null/regenerate` 那个 422。
> 新增前端功能时建议顺手加一条断言进去（把临时脚本变成**常驻回归资产**，
> 而不是像上一轮那样用完就删）。

---

## 12. 给用户的一句话启动模板

★ 2026-09-26 起**推荐用 `docs/handoff-quick.md` 里第 8 节那段开场白**（更全：含命令、红线、
验收数字、可选清单）。下面是历史简短版，仍然可用：

```
读 docs/handoff-quick.md（需要细节再看 docs/handoff.md 的 §0 与 §14~§17）。
遵守里面的 Token 纪律、红线与验收标准；遇到问题或需要做选择时先暂停问我。
现在我要做的是：<需求>
```

如果只想修 bug：

```
读 docs/handoff-quick.md 与 docs/handoff.md §0。我遇到一个问题：<描述>。
先自己定位，解决不了再上网查，先别改代码，告诉我你的判断。
```

---

## 13. ★ 启动与用户验证（2026-09-20 更新）

### 13.1 怎么启动（三步，复制即可）

```powershell
cd E:\VSCode\hetero-narrative-engine

# ① 确认 MySQL 服务在跑（数据库里有你自己的真实数据，别 drop）
Get-Service MySQL80        # 期望 Status = Running

# ①.5 ★ 先确认 8000 端口上还没有服务，避免"起了第二个"然后报 10048
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
#   有输出 = 已经有一个在跑，直接用它即可（改了 app/ 下的 Python 才需要重启它）

# ② 启动后端（前端由它一起托管，不需要 npm）
#    ★ 改了 app/ 下的 Python 之后必须**重启这个进程**才生效；web/ 下的改动刷新页面即可
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# ③ 打开可交互界面
#    可视化控制台   http://127.0.0.1:8000/console     ← ★ 你要点的就是这个
#    接口文档       http://127.0.0.1:8000/docs
#    健康检查       http://127.0.0.1:8000/health
```

**不花钱体验流式打字机效果**（开第二个终端，然后到界面「模型配置」里新建：
Base URL `http://127.0.0.1:8123/v1`、模型名 `fake-model`）：

```powershell
.\.venv\Scripts\python.exe scripts/fake_openai_server.py --port 8123
#   默认每个字 0.12 秒，所以一条 120 字的回复大约要 15 秒 —— 这是刻意的，看得清逐字效果
#   想快一点：加 --delay 0.02
```

### 13.2 建议的验证顺序（照着点一遍即可）

| 顺序 | 在哪 | 点/填什么 | 期望看到 |
|---|---|---|---|
| 1 | `/console` | 用你自己的账号登录（或先注册） | 直接进入**叙事会话**页，顶栏出现用户名 |
| 2 | 模型配置 | 点「豆包」等预设按钮，或新建一个假模型（见上），点「测试」 | 成功显示延迟与模型回复；**失败会弹窗列出实际请求地址 + 上游原话 + 排查顺序** |
| 3 | 角色卡 | 新建一张卡（填人设 + 开场白） | 列表出现该卡 |
| 4 | 世界书 | 新建一本，加一条条目：关键词 `灯塔`、正文任意 | 条目保存成功 |
| 5 | 角色卡 | 编辑那张卡，关联刚建的世界书 | 卡片上出现世界书 |
| 6 | 叙事会话 | 「+ 新建会话」选卡 + 选模型 | 角色开场白立刻作为第一条消息出现 |
| 7 | 叙事会话 | 输入一句**不含**关键词的话，发送 | 逐字出现；头部**没有**「世界书命中」 |
| 8 | 叙事会话 | 输入一句**含**「灯塔」的话，发送 | 头部出现「世界书命中 1 条」；头部还有 **Token 统计（本轮 输入/输出/思考）** |
| 9 | 叙事会话 | 点「查看提示词」 | 里面能看到命中的那条设定原文 |
| 10 | 叙事会话 | **鼠标移到任意消息上** | 出现「复制 / 编辑 / 撤回 / 重新生成」按钮 |
| 11 | 叙事会话 | 点某条**角色回复**的「重新生成」 | 回复被重写；**用户消息数不变**（不会多出一条） |
| 12 | 叙事会话 | 点某条**你说的话**的「编辑」，改完保存 | 提示"删除了其后的 N 条消息"，然后自动重新生成 |
| 13 | 叙事会话 | 点某条**你说的话**的「撤回」 | 确认后该句及其之后内容消失，**原话回到输入框**（可改了再发） |
| 14 | 叙事会话 | 点「记忆」，检索框输入刚才聊过的事 | 能召回刚才那轮对话（相似度百分比） |
| 15 | 右上角 | 点「请求日志」，点开任意一条 | 能看到原始请求体与响应体 |
| 16 | 叙事会话 | 发送中途点「停止」 | 半截回复保留，并标注「已停止生成」 |
| 17 | 角色卡 | 删掉一张**有会话**的卡，两个勾都勾上（默认） | 回到会话页不再出现「会话不存在」的红条，而是「这个会话已经不在了」+ 下一步提示 |
| 18 | 世界书 | 新建一本条目较多（例如 20 条 × 一万多字）的书，回到列表 | 列表正常显示条数（**不再出现 `Out of sort memory` 500**） |
| 19 | 模型配置 | 连点两次某行的「测试」（每次关掉弹窗），再点「模型」 | 只弹**一个**窗（不再叠加） |

### 13.3 如果发现问题，请告诉我三样东西

1. **你点了什么**（哪个页面、哪个按钮）
2. **期望是什么、实际是什么**
3. **请求日志里那一条**（右上角「请求日志」→ 点开 → 里面的 `request_id`）

有 `request_id` 就能在 `data/logs/` 里精确捞出整条链路。
接不通第三方模型时，**先把「测试」失败弹窗里的「实际请求地址」发我** ——
十有八九是 Base URL 多写或少写了一段（见第 9.5 节第 ③ 条）。

### 13.4 本轮已知的、留给你的选择

| 项 | 现状 | 说明 |
|---|---|---|
| `recursive_scanning`（世界书递归扫描） | **未实现**，字段原样存取与导出 | 界面上**故意没有**开关（不做假按钮） |
| 滚动摘要是本地压缩 | 不额外调模型 | 想换成模型生成的真摘要，函数签名不用改，但每轮会多一次调用 |
| 删消息后的 `total_tokens` | 变成"剩余消息估算值" | 原来累加的是厂商真实用量（含输入），删完无法精确还原；已如实标注 |
| 重新生成会连删后续内容 | 行为如此 | 因为它后面的剧情是接着它写的；界面会先确认并告知删几条 |
| Electron 打包 | 未做 | 你说要做**便携免安装版**；等你验证通过后再做 |
| token 估算精度 | 启发式 + 5% 安全余量 | **未做过**「估算值 vs 厂商 `prompt_tokens`」的对照实测，论文里别写成"实测误差 <10%" |

### 13.5 ★ 有一条你自己的数据需要手动改（我没有擅自改）

数据库里 `ChatGPT` 这条模型配置的 API 地址是：

```
https://ai.zyyun.xyz"/v1        ← 中间多了一个英文双引号 "
```

这是**你的真实数据**，我没有动它。请在界面上编辑这条配置，把那个 `"` 删掉再保存
（保存时前端也会顺手削掉多余的接口路径并提示）。豆包那条 `…/api/v3/responses`
同理需要改成 `…/api/v3`。

---

## 14. 第四轮修复（2026-09-20 晚）：HTML 渲染 / 内置守卫 / 备选开场白 / 重新生成

### 14.1 这一轮改了什么

| # | 你报的问题 | 真正的根因 | 修复位置 |
|---|---|---|---|
| 1 | HTML 开场白不渲染 | ① `<style>` 与 `<img>` 被清洗整块丢掉 → 只剩裸文字；② 整段 HTML 常被 ``` 围栏包着 | `ui.js::sanitizeHtml` 保留并清洗 CSS、放行安全 `img src`；新增 `mountRich` 用 **iframe 沙箱**渲染 |
| 2 | AI 暴露身份 / 不按剧情走 / 回复太短 | 之前完全没有这类硬规则，且"不绑预设"就等于什么规则都没有 | 新增**内置守卫预设**（3 个块），**永远追加在用户预设之后**；可编辑 / 可删除 / 可「还原内置规则」 |
| 3 | 备选开场白"一行一条"太短 | 前端用 `join('\n')/split('\n')`，把 1 条多行开场白拆成 67 条 → 保存 422 | 改成**一条一页的翻页编辑器**；后端上限 20 → 100 |
| 4 | 重新生成 / 编辑后确认，AI 没反应 | 一个 id 同时被当成"要删的回复"和"要问的话" | `_prepare()` 返回两个 id；`_sse_stream_response` 新增 `delete_from_id`；编辑路径不再传 `message_id` |

### 14.2 内置守卫规则到底怎么生效的（重要）

```
没绑任何预设  →  内置装配（人设 / 世界书 / 记忆，逐字节不变）
                 + 守卫正文追加在**系统提示词最后**

绑了预设      →  用户预设的块（按它自己的顺序）
                 + 守卫块接在最后（order_index 整体偏移）
```

- **不是"二选一"**：守卫是叠加，不会顶掉角色卡人设（这一点我第一版写错了，测试当场抓住，见 `pitfalls.md` 第 33 条）。
- 三个块：`hneGuardIdentity`（不承认是 AI）、`hneGuardImmersion`（不替用户行动 / 不跳时间线 / 不 OOC）、
  `hneGuardLength`（不少于 300 字）。
- 它是一行**真实的预设数据**（`prompt_presets.is_builtin=1`），
  所以在「提示词预设」页可以像普通预设一样改正文、禁用块、删掉。
- **删除是真的删除**：靠 `users.builtin_preset_dismissed` 记住，系统不会偷偷重建；
  想恢复点右上角「还原内置规则」。
- 「最小回复长度」为什么不是 API 参数：OpenAI / Anthropic / DeepSeek **都没有** `min_tokens`。
  所以做法是"写进提示词 + 生成后真实量一遍"，短了就诚实提示（可调小或删规则），
  而不是假装支持（对应红线：拒绝静默降级）。

### 14.3 HTML 渲染的安全边界（改这里前先读）

- 容器是 `iframe sandbox="allow-same-origin"`，**故意不给 `allow-scripts`** ——
  卡片里的脚本一行都跑不了；同时父页面还能读高度做自适应。
- **为什么用 iframe**：卡片自带 `<style>`，直接插进主文档的话，
  一条 `body{display:none}` 就能把整个控制台变白。
- CSS 只挡 `@import`（能拉远程样式表）、`expression()`、`javascript:`、`behavior:`；
  **保留 `url()`** —— 背景图是角色卡美化的常用手段，代价（作者知道你看过这张卡）可接受，
  这一取舍是刻意写下来的，不是漏掉。
- 图片只放行 `http(s)://` / `data:image/*;base64` / 站内绝对路径（挡掉 `//evil.com`）。
- 渲染前会把整段外层的 ``` 围栏剥掉，并把 `{{char}}` / `{{user}}` 换成真名。
- 角色卡详情与**对话窗口**共用 `ui.js::mountRich`，不会再出现"一处能渲染、一处不能"。
  对话里是**流式结束之后**才切换成渲染视图（流式期间是纯文本，插了 iframe 就没地方写 delta 了）。

### 14.4 这一轮的验证数据

| 项 | 结果 |
|---|---|
| `pytest -q` | **587 passed** |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **69 项检查，0 失败**（新增 10.8.1 内置守卫、10.11 备选开场白翻页） |

> ⚠️ 不要**同时**跑 `pytest` 和 `smoke_test.py`：smoke 会在首尾各查一次全库行数，
> 并发的测试用户会让它误报"留下了垃圾数据"（这次就误报过一次，重跑即 142/0）。

### 14.5 测试断言被修改过（如实说明）

`tests/test_prompt_presets.py` 里两条断言原本写的是"没绑预设时不该出现任何规则文本"，
判据用了 `不是 AI 助手` —— 而内置守卫里也有同义表述，会被误判成"解绑失败"。
改成只属于用户预设的判据 `记住：你是`，并**补上**"守卫规则确实生效"的断言。
即：**加强而非放松**，没有删掉任何一条测试。

---

## 15. 第五轮（2026-09-26）：状态/故障转移/流式开关 + 用户实测的三个前端 bug

### 15.1 本轮新增能力（要验收的是这四件事）

| 需求 | 落点 |
|---|---|
| 状态栏可手动纠正 | `app/narrative/state.py`（解析/校验/合并）+ `PATCH /api/v1/narrative/sessions/{id}/state` + 会话页状态栏的「纠正」按钮 |
| 主模型失败自动切备用 | `app/llm/failover.py`（`FailoverAdapter`）、`llm_providers.fallback_provider_id`；**切换会明说**，消息上记的是真正作答的模型名 |
| 「流式传输」开关（默认开） | `llm_providers.stream_enabled` + `app/llm/whole_reply.py`（`WholeReplyAdapter`）；关掉后是「非流式取完整回复 → 一次性发出」，事件序列不变 |
| cleanup 白名单补齐 | `scripts/cleanup_demo_data.py` 的 `_TEST_PREFIXES` 增加 `tmp_ / mm_ / st_`（与本文档口径一致） |

### 15.2 用户实测反馈的三个 bug（本轮修复）

1. **列表页切一次筛选后，同页其它按钮全部点不动**（角色卡页点「公共卡库」之后
   「新建角色卡 / 导入 JSON / 搜索 / 重置」都没反应）。
   病根：`renderCardList` 用「这一批绑过就不再绑」的守卫**连工具栏一起**跳过了绑定，
   而工具栏节点每次重画都被 `mount()` 换掉 —— 新节点天生没有监听器。
   **教训**：挂在常驻 `#view` 上的事件委托才能"每批只绑一次"；
   属于本次渲染 DOM 的按钮必须**每次渲染后重绑**。cards.js / books.js 都已按这个口径拆成
   `bindToolbar()`（每次重绑）+ `bindCardActions()/bindBookActions()`（每批一次）。
   → 回归断言：探针 10.7「切到『公共卡库』再切回『我的卡库』后，顶部按钮没失效」。
2. **导入角色卡 JSON 只能粘贴、不能选文件**：弹窗补了拖拽区 + 文件选择
   （`ui.js::bindFileDrop`，读进文本框并顺手做一次 JSON 语法体检，导入仍由用户点按钮触发）。
   → 回归断言：探针 10.7「导入 JSON 弹窗里能选文件 / 拖文件」。
3. **整页卡在「⏳ 加载中…」**：用户反馈"世界书页点开编辑、不保存直接关闭后整页卡住"。
   已确认存在**这一类**病根并全部堵掉（未能复现他那一步的精确时序，如实说明）：
   - 旧批次的异步回调醒来时**先画占位、再发现信号已 abort 直接 return** → 整页永远停在加载中。
     现在统一改成「**动 DOM 之前先问 `isCurrent()`**」，不是当前批次就一个字都不写。
   - 列表接口加 20 秒超时（`api.js` 的 `timeoutMs`，只给"应当很快返回"的请求用，对话生成不加），
     失败时用 `ui.js::mountError()` 画**报错 + 重试按钮**，而不是留一个转不完的圈。
   - 整段渲染用 try/catch 兜底，渲染异常同样落到 `mountError`。
   - cards / books / presets / providers **四个列表页统一**这么处理。
   → 回归断言：探针 10.7.3「世界书『查看 / 编辑』→ 直接关闭后不卡在加载中」。

### 15.3 演示卡（给用户验收状态栏用）

`examples/demo-state-card.json` —— 一张自带状态协议的 Character Card V2：
system_prompt 与 post_history_instructions 都写了「每轮最后必须输出 `<state>{…}</state>`」，
卡片还带了 `character_book`（导入时会自动提取成一本世界书，验证世界书提取链路）。
导入路径：控制台 →「角色卡」→「导入 JSON」→ 把这个文件拖进方框 →「导入」。
（本轮已实测导入成功：开场白 / 尾注 / 系统提示词 / 1 条备选开场白 / 世界书 2 条设定齐全。）

### 15.4 第五轮验证时用户发现的问题（已修）

用户验收 ②（状态栏）时说了一句关键的话：
**"状态栏应该每一轮都输出，哪怕情况没有变化，也应该输出。"**
他用的真实模型（deepseek-chat）连续三轮都没吐状态块，状态栏一直是空的。
排查结论 + 修复（每一条都配了回归断言）：

1. **协议夹在中间被无视**：状态协议原来写在系统提示词中段，后面还压着身份守卫 /
   剧情守卫 / 长度要求一大段更"凶"的指令 —— 模型把那些都执行了，唯独漏掉状态块。
   → 拆成两段：事实（`state.render_for_prompt`，仍在守卫**之前**）
   + **输出契约**（`state.render_contract`，追加在整条系统提示词的**最末尾**，
   预设路径同样追加）。契约里明写"即使没有任何变化也必须再输出一次"。
   守卫里也补了一句"`<state>` 块是唯一例外的元信息"。
2. **模型不吐块时完全静默** → `state.apply_reply` 现在会返回一条提醒
   （"本轮回复里没有 <state> 状态块…"），前端以 info 横幅显示。
   建会话解析开场白时传 `notify_missing=False`（开场白没有块是常态）。
3. **状态栏从第 0 轮就能亮**：卡片可以自带初始状态，两条路都支持 ——
   `extensions.hne.initial_state`（推荐）与开场白正文里的 `<state>` 块（解析后**必须剥掉**）。
   落点：`app/narrative/sessions.py::create_session`。
4. **`state.normalize(raw, None)` 会炸**（`dict(None)` → TypeError）：现在允许传 None。
5. **★ 「纠正」按钮点了没反应**（探针 3.1 抓到的真 bug）：
   `chat.js::openStateEditor` 里写的是 `openDialog(...)` —— **这个函数根本不存在**
   （正确名字是 `modal`），于是点击直接抛 ReferenceError。空状态下它还会先 `return`，
   连窗都开不出来。现在：函数名修正，且**空状态也给出「手动填写」入口**（带骨架 JSON）。
6. **假模型从来不说状态块** → 整条"模型输出 → 剥离 → 校验 → 落库 → 前端显示"链路
   在浏览器探针里**一条断言都没有**（所以前几轮一直是绿的）。现在
   `scripts/fake_openai_server.py` 在用户消息含"状态"两个字时会扮演守规矩的模型，
   探针据此断言状态栏数值、正文无残留 JSON、以及 999 被夹成 100。

### 15.5 本轮（第五轮第二轮）验证数据

| 项 | 结果 |
|---|---|
| `pytest -q` | **617 passed**（`test_state.py` 9 → 14 条；改了一条断言判据，未删任何测试） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **77 项检查，0 失败**（新增状态栏 5 条 + 切卡库工具栏 1 条 + 导入选文件 1 条 + 世界书关弹窗 1 条） |

> 被改动判据的两条既有断言（**加强而非放松**，原因都写在代码注释里）：
> · `test_narrative.py::test_stream_reports_notes_from_adapter`：`notes[-1]` → `any(...)`
>   （done 的 notes 是累积的，现在会多一条状态提醒）。
> · `ui_probe.py`「没有多出重复的用户消息」：写死 `== 1` → 改成"重新生成前后对比"
>   （新增的那一轮对话会让写死的 1 变成 2）。


### 15.6 第一轮的验证数据（保留备查）

| 项 | 结果 |
|---|---|
| `pytest -q` | **612 passed**（新增 `tests/test_state.py` 9 条、`tests/test_failover.py` 10 条） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **73 项检查，0 失败**（新增 10.7 工具栏、10.7.2 导入选文件、10.7.3 世界书关弹窗） |

清理复核：`cleanup_demo_data.py` 跑完 users 5→1（只剩 真实用户 #887），
providers 8→2，Chroma 只剩 `narrative_user_887`（测试账号的集合已按脚本提示删除）。

---

## 16. 第五步：插件市场极简版（2026-09-26）

用户要的是"极简插件市场"，并明确选了三点：**只做声明式三类**、
**安装来源只允许 GitHub**、**默认插件两个（可停用、不预置规则）**。
（★ 第十三轮在此基础上扩到**四类**：加了 `dice` 跑团骰点，见 §26 ——
它同样是"数据 + 我们自己的求值器"，没有引入第三方可执行代码。）

### 16.1 能力与落点

| 类型 | 作用面 | 落点 |
|---|---|---|
| `regex` 正则替换 | **只改发给模型的提示词**（系统提示词 + 历史 + 尾注），不改数据库、不改界面显示 | `app/narrative/plugin_runtime.py::apply_to_plan` |
| `prompt` 提示词注入 | 往系统提示词插入固定文本，位置可选 `start` / `before_guard` / `end`（越靠后权重越高） | 同上 |
| `css` CSS 主题 | 注入控制台的 `<style>`（启动时取一次） | `GET /plugins/theme.css` + `web/js/views/plugins.js::applyPluginTheme` |
| `dice` 跑团骰点（**第十三轮新增，见 §26**） | 后端掷骰：规则进系统提示词，点数替换 `<roll>` 标签并落库 | `app/narrative/dice.py` + `plugin_runtime` + `engine.save_*` |

- 表：`plugins`（用户级，`config` 存 JSON，`source_url` 记安装来源，`is_builtin` 标默认空壳）。
- 迁移：`migrate_db.py --create-tables` 建表 + `users.plugin_defaults_seeded`（默认插件只发一次，
  删光后不会自己冒出来 —— 与 `builtin_preset_dismissed` 同一招）。
- 接口：`GET/POST /plugins`、`POST /plugins/install`、`GET /plugins/theme.css`、`PATCH/DELETE /plugins/{id}`。
  ★ `/install` 与 `/theme.css` 必须注册在 `/{plugin_id}` **之前**（否则 FastAPI 把 "install" 当 id 去校验，报 422）。
- 前端：顶栏新增「插件」页（`web/js/views/plugins.js`），列卡片 + 启停 + 上下移 + 编辑 + 删除
  + 「从 GitHub 安装」+ 安全边界说明；`index.html` 的 importmap 也要登记 `hne/plugins`。

### 16.2 安全边界（写死在代码里，也写在界面上）

1. **只允许 GitHub**：`github.com` / `raw.githubusercontent.com` / `gist.github.com` /
   `gist.githubusercontent.com` 的 https 地址；网页地址（`…/blob/…`）与 **gist 页面地址**
   （`gist.github.com/用户/编号`，多文件用 `?file=foo.json`）自动转 raw；
   **跟随重定向之后再查一次域名**，且只认 raw 类域名（302 绕过实测被拒）。
   > gist 是刻意支持的最轻分享方式：写一个插件不必建仓库。它同样是 GitHub 域名，
   > 而"只下载数据、不执行代码"这条边界不变，所以风险与仓库同级。
2. **只下载数据，不执行代码**：清单是 JSON，没有 JS 执行入口 → 拿不到 API Key、发不出请求。
3. **上限**：清单 ≤256KB、规则 ≤50 条、单条正则 ≤300 字、CSS ≤20KB；超限直接拒（不截断）。
4. **CSS 清洗**：挡 `</style>` 逃逸（转义成 `<\/`）、`@import`、`expression()`、`javascript:`、`behavior:`。
   `url()` 保留（与角色卡美化同一取舍），界面里如实说明"作者可能看到你的 IP"。
5. **失败隔离**：下载/解析/校验任一步出错都只影响这次安装（有断言：拒绝后插件数不变）。

### 16.3 那一类"预览骗人"的坑又出现了一次（并当场被测试抓住）

写完后端就顺手写了测试 `test_preview_matches_the_real_request`，
结果**红了**：插件只接在 `engine.prepare_turn` 那条路径上，
`/sessions/{id}?with_prompt=true`（界面上的「查看提示词」）没带插件 ——
预览看不到注入、实际却发了。已修（预览与真实请求都传 `plugins=plugin_service.load_enabled(...)`）。
这条断言的价值就在于此：**同一份提示词只能有一个装配入口**。

### 16.4 内置示例目录：对照 SillyTavern 的内置扩展（用户要求"先放在插件市场里面"）

用户问"酒馆有哪些默认插件，先放进来"。查了官方文档（[Extensions](https://docs.sillytavern.app/extensions/index.md)）后，
**只把"本项目声明式能力真能做到"的做成示例**，其余如实列出不做（插件不执行第三方 JS）。
目录条目**不会自动生效**：必须点「添加」才变成用户自己的插件（避免"内置示例偷偷改了提示词"）。

| 目录 key | 名称 | 对应酒馆的什么 |
|---|---|---|
| `trpg_dice` | 跑团骰点（Dice） | 内置扩展 Dice（本项目改为**声明式**：服务端受控求值，见 §26） |
| `authors_note` | 作者注（Author's Note） | 内置功能 Author's Note（把风格基调钉在提示词末尾） |
| `objective` | 当前目标（Objective） | 可安装扩展 Objective |
| `zh_reply` | 始终用中文回复 | 内置扩展 Chat Translation（只取"约束输出语言"这一层） |
| `strip_markdown` | 清理 Markdown 强调符号 | 内置扩展 Regex 的常用规则 |
| `dark_theme` | 深色主题（Dark Lite 风格） | 内置主题（换配色） |
| `big_text` | 大字号 · 宽松行距（护眼） | 本项目新增（无障碍） |

**如实写明做不到的**（界面上一并列出）：TTS、图片生成/看图、表情立绘、Quick Reply（脚本执行器）
——都需要执行代码或接外部服务；而 Summarize / Chat Vectorization / Token Counter 本项目**已有原生实现**，
不需要插件化。接口：`GET /plugins` 的响应里带 `catalog` 与 `unsupported` 两个字段；
`POST /plugins/catalog/{key}` 添加一条。

> ★ 与酒馆的关系要说清楚：酒馆的扩展是**要执行 JS 的项目**（`manifest.json` + `index.js`，靠 git clone 安装，
> 见 [UI Extensions](https://docs.sillytavern.app/for-contributors/writing-extensions/)），
> 我们的插件清单是**纯数据**，两边**互不通用**。
> 本项目真正与酒馆互通的是角色卡（PNG / V2 JSON）、`character_book`（世界书）、completion preset（提示词预设）。

### 16.5 验证数据

| 项 | 结果 |
|---|---|
| `pytest -q` | **660 passed**（新增 `tests/test_plugins.py` 35 条） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **93 项检查，0 失败**（插件一节 11 条，含内置示例目录与"添加后才生效"） |

> 探针的一个细节：默认插件是"只发一次"的（`users.plugin_defaults_seeded` 墓碑位），
> 所以 `seed()` **不再整体清空 plugins** —— 否则第二次跑时默认插件不会重建，
> "默认两个空壳"这条断言会永远不成立（这一轮真踩了）。探针只按名字删自己造的那些。


> 唯一被改的既有测试：`tests/test_console.py::test_console_module_urls_are_versioned`
> 的 importmap **集合**里补上 `"hne/plugins"` ——
> 它本来就是"importmap 与前端模块集合必须逐一对上"的守卫，加模块时必须同步，
> 不是放松判据（同文件另有一条"每个前端模块都要在 importmap 里"的断言，会自动覆盖新模块）。

---

## 17. 纯聊天会话（无角色）· 兼异构适配层的验收台（2026-09-26）

### 17.1 它是什么、为什么值得做

用户要一个"像 ChatBox 那样的纯聊天"：**不掺任何角色**，既能确认自己的 API 配置到底通不通，
也能在不想建角色卡时直接聊两句。落地形态刻意是「**同一条会话页 + 一个新入口**」，
而不是另做一套聊天界面 —— 复用同一套流式链路、消息操作、Token 统计，只维护一份。

> ★ 定位上有一处必须说清：本项目是 **BYOK（用户自带 URL/Key）**，
> 不存在"官方提供 API"这回事。所以这个功能**不是**"官方服务"，而是
> 「无角色会话 + 异构适配层验收台」。做"官方内置 key 的共享服务"会引入计费/滥用/合规问题，
> 答辩上很难答，本项目不做。
>
> ★ 对论文的价值：①**架构通用性证据** —— 角色卡只是提示词装配的**一个可选层**，
> "三层全空"（无卡/无世界书/无预设）也能跑，说明分层是干净的；
> ②**实验平台** —— 换 API / 协议 / 流式开关 / 思考强度后发一句就能对比，
> 直接产出异构适配那一章的实测数据。**它不是创新点，别包装成创新。**

### 17.2 实现要点（三条都是"必须这么写"的）

1. **判据是显式的 `narrative_sessions.kind` 列（`story` / `chat`），不是"角色卡 ID 是不是空"。**
   角色卡被删除时 `character_card_id` 会被置空（SET NULL）—— 那是"卡没了的故事会话"，
   要如实提示"人设丢了"；而纯聊天是用户主动选的。两者长得一样，只能靠显式字段区分。
   （迁移：`migrate_db.py` 给 `narrative_sessions` 补 `kind VARCHAR(16) NOT NULL DEFAULT 'story'`。）
2. **提示词走独立的「通用助手」分支**（`prompt_builder.PURE_CHAT_SYSTEM_PROMPT`）：
   不装人设、**不套预设与内置守卫**（守卫里写着"你是虚构故事里的角色，不是 AI 助手"，
   放在纯聊天里是错的）、不注入状态协议、不扫世界书、不召回长期记忆。
   若用户绑了预设，预览里会**明确警告**"本次没有使用它"，并且详情把
   `prompt_preset / effective_preset / builtin_guard` 一律报 `null`（不能假装生效）。
3. **状态协议在纯聊天里整体关闭**：`save_assistant_reply` 只把可能出现的 `<state>` 剥掉
   （绝不让用户看到原始 JSON），**不落库、也不提醒** —— 我们没要求过模型输出它，
   就不能反过来怪它（这条有测试守着）。

### 17.3 界面

- 会话页右上多一个「**+ 纯聊天**」按钮 → 弹窗只让选模型配置（并提示"换一个配置再发一句就能对比"）。
- 纯聊天会话的头部显示 `纯聊天` 徽标 + **协议 / 模型 / 流式·非流式 / 是否设备用模型**，
  以及累计 token —— 这一行就是"体检表"。
- 纯聊天里**不渲染状态栏**（HP/背包/任务是叙事会话的东西）。

### 17.4 验证数据

| 项 | 结果 |
|---|---|
| `pytest -q` | **660 passed**（新增 `tests/test_pure_chat.py` **8 条**） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **93 项检查，0 失败**（纯聊天一节 5 条：入口 / 建会话+体检表 / 无状态栏 / 能收到回复 / 预览是通用助手且不含守卫与状态协议） |

> 探针里的一个坑：这一节**不能**在界面正开着该会话时把它删掉 ——
> 会立刻产生一个 `GET /sessions/{id} -> 404`，把后面"界面上没有意外的失败请求"那条断言搞红
> （真踩了一次）。改成交给 `seed()` 的开场清场与收尾的账号删除一起处理。

---

## 18. 清理记录（2026-09-26，开新对话前）

**删掉的（每条都先用 grep 证明过没有任何引用）：**

| 项 | 依据 |
|---|---|
| `pytest-cache-files-*/`（4 个） | pytest 被强杀留下的临时目录；全库 grep 0 引用 |
| `.pytest_cache/` | 可重新生成（.gitignore 已忽略） |
| 590 个 `__pycache__/` | 可重新生成（.gitignore 已忽略） |
| `data/_chroma_probe/` | 早期"大 payload 向量库实测"实验的残留；全库 grep 0 引用。**注意：这个目录曾被 git 跟踪过**，后悔可用 `git checkout -- data/_chroma_probe` 还原（其结论已写在本文档 §「约 880KB 才稳定复现」那段，数据本身不需要留） |
| `app/api/v1/narrative.py` 的 `from datetime import datetime` | 全文只有 import 那一行 |
| `app/services/plugin_service.py` 的 `DEFAULT_LIMIT` / `MAX_LIMIT` | 只有定义、0 处使用（插件列表不分页） |
| `web/js/views/providers.js` 三个多余的 `export` 关键字 | `openModelsDialog / openTestDialog / openJsonDialog` 只在本文件内用，全库 grep 无外部导入 |

**刻意保留（不是"忘了删"，是判断过）：**

- `app/db/chroma.py` 的 `from app.db.base import Base  # noqa: F401`：**原注释写明**"保持数据层导入一致性，无实际用途"。
- `data/ui_probe_*.png` / `data/ui_probe_report.json`：探针每次重跑都会重写（gitignored），当前那份是最新验证证据。
- `docs/*-screenshot.png`：**探针脚本自己写的**（`scripts/ui_probe.py` 的 `shoot()`），README 与论文素材引用的是这几张。
- `examples/demo-state-card.json`：验收状态栏用的演示卡。
- `api.js` 的 `export ApiError` / `export request`、`ui.js` 的 `export sanitizeHtml`：模块内部在用，属于公开 API 面（文档里也按 `ui.js::sanitizeHtml` 引用）。
- **`.dsh-drop/`**：用户自己拖进来的文件（毕设选题笔记），一律不动。
- 所有测试文件：删测试是红线。

清理后重跑三层验证：**660 passed / 142-0 / 93 项 0 失败**（与清理前一致）。

---

## 19. 第六轮：状态栏字段改由角色卡 / 世界书定义（2026-09-26）

### 19.1 用户报的问题（原话）

> "角色卡通常会有对应的状态栏，而不是全部通用一个，例如测试用的灯塔守夜人里的 HP
> 不适用于魔法少女魔法裁判，这是一个错误的设计。状态栏应该在角色卡里读取
> （大部分作者是把状态栏格式和要求放在世界书里面）。"

**实测确认**（只读查库，未动任何数据）：

| 位置 | 灯塔卡（`character_cards.id=7912`）实际内容 |
|---|---|
| `extra_data.extensions.hne.initial_state` | `hp{100,100}` / `inventory` / `location` / `quests` / `flags` —— **确实写了 hp** |
| `system_prompt` / `post_history_instructions` | 卡内文本自己写了"字段固定为 hp/inventory/location/quests/flags" |
| 关联世界书 `id=2860`「北岸灯塔设定」 | 只有 `灯塔/灯/灯室` 与 `补给船/船长/航线` 两条**世界观**条目；`extra_data` 只有 `scan_depth/token_budget` —— **没有状态栏格式** |

所以"灯塔的 HP"不是世界书带来的，而是：**代码把字段清单写死了**
（`state.py:KNOWN_FIELDS`、`render_for_prompt` 的"字段固定为 hp{current,max}…"、
`chat.js` 的 HP 条、`state.normalize` 里唯一特殊照顾的 hp）。
于是**任何没声明 HP 的卡**（如魔法少女 / 魔女裁判）也会显示 `HP 100/100`。

### 19.2 改法（新增 `app/narrative/state_schema.py`）

解析优先级（用户拍板，勿改）：

```
① 卡 extensions.hne.state_schema      显式字段清单，最权威
② 世界书约定式条目                   名字以 [状态栏] 开头 或 keys 含 state_definition；
                                     正文里的 <state>{…}</state> 示例决定字段，
                                     正文原文进提示词（作者怎么写规则就怎么生效）
③ 卡 extensions.hne.initial_state 顶层键推断
④ 空 schema：不注入协议、不解析 <state>、状态栏显示"该卡未定义状态栏格式"
```

关键实现点：

- 解析结果**建会话时算一次并落库**：新列 `narrative_sessions.state_schema_json`
  （`scripts/migrate_db.py` 追加，**只加列不删**，已在本地库执行）。
- **兼容路径**：迁移前的旧会话该列为 NULL；若它**已有状态**，`state.effective_schema`
  退回旧五字段（解析+渲染都用它），这样用户历史会话的状态栏不会因这次改动变空白。
- meter 字段：`{"name":"hp","type":"meter","max_field":"max"}`，状态里扁平存
  `{"hp":88,"max":100}`；`_clean_meter` 同时兼容嵌套写法 `{"hp":{"current","max"}}`。
- `normalize(raw, previous, schema)` 只认 schema 里声明过的字段（连同 meter 的上限字段），
  没声明的一律忽略并记 note；`ensure_shape` 按 schema 生成骨架。
- `world_book_scanner.scan` 跳过格式条目（否则同段话进提示词两遍，示例 JSON 可能被抄进正文）。
- 接口：`PUT/PATCH /character-cards/{id}` 接受 `extensions`（传 null = 清空），
  `CharacterCardOut` 吐回 `extensions`（编辑界面要回填）；会话详情新增 `state_schema`。
- 前端：`chat.js::stateBarHTML(state, schema)` 按 schema 逐字段渲染（text/number/meter/list/
  tuples/flags），纠正弹窗骨架也来自 schema；`cards.js` 新增「★ 状态栏格式」编辑区。

### 19.3 演示卡与探针卡的改动（都是我们自己的测试数据，不涉用户真实数据）

- `examples/demo-state-card.json`：灯塔守夜人**去掉了 HP**，改成 `灯油（meter，上限字段
  「灯油上限」）` + `位置 / 携带物 / 任务 / 标记`，并显式写了 `state_schema`。
- `scripts/ui_probe.py` 的探针卡（探针账号自己造、每轮清场重建）声明了
  `hp/location/inventory` 的 schema + `initial_state` —— 现在"状态栏字段是卡给的"也进了探针。
- **没有动** `真实用户（本机 DB 里的 id 已隐去）` 的任何数据：他库里那张魔法少女卡仍然是"没声明状态栏"，
  因此界面上会显示"该卡未定义状态栏格式"（不再长 HP）；要给它加状态栏，
  在卡片编辑弹窗里填一次「状态栏格式」即可。

### 19.4 本轮的验收数字

| 项 | 结果 |
|---|---|
| `pytest -q` | **678 passed**（新增 `tests/test_state.py` §六 的 schema 测试 + 纯聊天里"没声明就没有协议"的回归守卫） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **98 项检查，0 失败**（新增 5 条：① 没声明 → 状态栏"未定义"+ 提示词无协议；② 声明了 → HP 条 / 提示词带字段清单 / 落库 `source=card`） |

> 踩到的坑（记一笔）：`CharacterCardOut` 一开始没加 `extensions` 字段，
> 于是**后端存进去了、接口却不返回** —— 探针的"声明了 schema 的卡"那三条一直红，
> 而库里 `character_cards.extra_data` 其实是空的（老卡）。教训：加了写入路径就要检查
> **响应模型**是否也吐得出来，否则"存了但看不见"会被误判成"没存进去"。

---

## 20. 第七轮：混合检索融合 + 可复现 benchmark（2026-09-26）

### 20.1 改之前是什么样（两条互不相干的通道）

| 通道 | 触发 | 打分/排序 | 预算 | 渲染位置 |
|---|---|---|---|---|
| 关键词（世界书） | 最近 `scan_depth` 条消息里字面命中 `keys` | **只有 `insertion_order`**，没有相关性分 | 世界书自己的 `token_budget` | `## 世界设定` |
| 语义（长期记忆） | 最后一条用户消息做查询 | Chroma 余弦距离 → `similarity ≥ 0.35` + 出戏过滤 + 去重 | `MAX_BLOCK_CHARS=1200` | `## 回忆` |

后果：同一段内容可能进提示词**两遍**（两路都召回）、短而精准的设定被长而含糊的回忆挤掉、
而且**没有任何统一排序**。论文标题里的"混合 RAG"当时只到"两条通道拼在一份提示词"。

### 20.2 现在是什么样（`app/narrative/retrieval.py`）

```
① 召回  世界书关键词通道（命中强度：命中数 + 0.1×命中字数）+ 记忆语义通道
② 融合  RRF：score = Σ w_c/(60 + rank_c)（默认，rank 级、免调参）
         或 min-max 归一化加权（weighted）—— 只用于消融对比
③ 去重  跨通道按归一化文本去重；同一内容只留一条，并把两路名次**合并**到胜者（RRF 吃两票）
④ 重排  默认 tiebreak：融合分说了算，完全同分时才按"每路归一化证据强度"裁决（1e-6 量级）
⑤ 装填  两路**共享一个 token 预算**，但**分两级**（第八轮按用户要求改）：
         ① 先按分数装**世界书条目**，② 再用**剩余预算**装记忆 ——
         回忆再高分也**不能挤掉**作者手写的设定
```

> ★ **优先级（用户拍板，勿改）**：**世界书 ≥ 预设 > 用户对话 / 回忆**。
> 原话："世界书一般是作者特意加在角色卡里面的，里面的设定比用户对话重要，
> 不然容易出戏，乱写和掉马甲之类的，**绝对不能挤掉**！"
> 所以 `pack()` 是两级装填，`tests/test_retrieval.py` 里
> `test_world_book_entries_are_never_evicted_by_memories` 就是这条规则的守卫。
> （第七轮曾做成纯分数竞争，高分回忆能顶掉设定 —— 那是错的，已改。）

- 入口唯一：`engine.build_session_prompt`（对话与「查看提示词」预览共用）。
  `prompt_preset_service` 的预设预览也改走它，只是**显式不接语义通道**
  （向量检索是外部依赖），并在摘要里说明"本次预览未含语义通道"。
- 旧结构保留：`SessionPrompt.scan` 仍是一个 `ScanResult`（数值来自融合结果），
  stats 与界面不用改。
- 可解释：`describe()` 一行摘要 + `debug_lines()` 逐条（来源/两路名次/最终分/去留原因），
  进 PromptInfo 的 `retrieval` / `retrieval_summary` / `retrieval_items`，
  前端「查看提示词」里显示（探针 10.11 有断言守着，防黑箱）。
- 降级：两个通道各自 try/except；`memory.recall` 自己把失败原因放在 `result.error` 里，
  `semantic_candidates` 必须把它翻译成降级说明（**静默降级是 bug** —— 这一点是
  `tests/test_memory.py::test_memory_failure_degrades_without_breaking_chat` 抓出来的）。
- 行为变更（第八轮修正）：两路共享预算但**世界书优先**；世界书条目只会因为
  "自己这一批就超预算"而被丢，**不会**再被回忆挤掉。丢弃时会在 warnings 里说明原因
  （"命中 N 条、注入 M 条"不再是个谜）。

### 20.3 benchmark（`scripts/benchmark.py` + `app/narrative/retrieval_metrics.py`）

```
.\.venv\Scripts\python.exe scripts\benchmark.py
  → 控制台表格 + data/benchmark_report.json + data/benchmark_report.md
```

- 数据集：`scripts/benchmark_data/retrieval_cases.json`（**合成小集**：12 query /
  10 世界书条目 / 11 记忆）+ `state_cases.json`（10 条状态校验用例，带期望下场）。
- 指标：Recall@1/3/5、MRR、nDCG@5（用**完整名次**，含被预算砍掉的候选）+
  注入条数 / 注入 token（均值·中位）/ 去重率 / 丢弃条数 + 状态一致率（原样/被修正/被丢弃）。
- 硬约定：**不调用任何大模型**（红线）；语义通道默认取标注相似度（模拟理想向量检索，
  `--semantic embedding` 可换成本地 onnx 嵌入模型）；结果可复现。
- 脚本自己会把"这是合成小集、只证明机制成立"打在报告里 —— 不夸大战绩。

**真实数据只读回放（`--from-db`）**：

```powershell
# 全部用户、最多 20 个会话、每个会话回放最后 3 轮（只 SELECT，不动任何数据）
.\.venv\Scripts\python.exe scripts\benchmark.py --from-db
# 只看某个用户 / 调小规模 / 不碰向量库
.\.venv\Scripts\python.exe scripts\benchmark.py --from-db --user 真实用户 --limit-sessions 10 --rounds 2 --replay-semantic none
```

它在真实语料上回放同一套管道，报三组**能算得准**的指标（真实语料没有相关性标注，
所以**不报 Recall@k** —— 这一点脚本会自己打在报告里，不许混着读）：

| 指标 | 口径 |
|---|---|
| 注入条数 / 注入 token（均值·中位）/ 去重率 / 丢弃条数 | 每个会话最后 N 轮用户消息，用"那一刻的上下文"重跑检索 |
| **状态块漏输出率** | 只统计**声明了状态栏**的会话：除开场白外的助手回复里有多少没带 `<state>` |
| **状态回放一致率** | 落库状态"再校验一遍"不会被改动（`normalize(stored, stored, schema)` 无提醒）|

★ **只读保证**：全程只用 ORM 查询，没有任何 add / commit / delete；
实测跑完前后 `users / character_cards / world_books / narrative_sessions / messages`
五张表的行数**完全一致**（这条也写进了 `tests/test_benchmark.py`：
回放前后消息条数必须相等）。

**实测结论（2026-09-26，标注集）：**

| 策略 | Recall@1 | Recall@3 | MRR | nDCG@5 | 注入条数(均) | 注入token(均) |
|---|---|---|---|---|---|---|
| `keyword_only` | 0.4861 | 0.5278 | 0.7917 | 0.5768 | 1.17 | 34.7 |
| `semantic_only` | 0.2917 | 0.3889 | 0.7083 | 0.4493 | 2.00 | 42.7 |
| `rrf(+tiebreak)` ★默认 | **0.5139** | **0.9167** | **0.9167** | **0.8962** | 3.08 | 74.5 |
| `weighted(+tiebreak)` | 0.5139 | 0.9167 | 0.9167 | 0.8962 | 3.08 | 74.5 |
| `rrf(+blend 词面重排)`（负面对照） | 0.3056 | 0.8750 | 0.8194 | 0.7765 | 3.08 | 74.5 |

两条**被数据推翻的直觉**（这才是这轮最有价值的部分）：

1. **"用与查询的字面重叠去重排"是有害的**：Recall@1 从 0.51 掉到 0.31。
   原因很直白 —— 语义通道的价值恰恰在于命中与查询**没有字面重叠**的同义改写
   （"我答应过她什么" ↔ "你答应过今晚把灯点上"），重叠项会把它们压下去。
   所以 `blend` 只留给消融表当负面对照，默认走 `tiebreak`。
2. **RRF 的同分非常常见，同分怎么裁决和融合策略本身一样重要**：
   按 key 字母序兜底 0.5139 / 按先验分（作者优先级·同会话）0.2639 / 只按语义分 0.3333 /
   按"每路归一化证据强度"0.5139。默认选了最后一种 —— 它站得住脚（不靠字母序碰巧排对）
   且实测不差。这条也写进了 `retrieval.rerank()` 的注释与 `tests/test_benchmark.py`。

### 20.4 真实数据回放的实测（2026-09-26，只读）

在本机全部真实会话上跑了一次 `--from-db --limit-sessions 20 --rounds 3`：

```
第一次（库里还有探针留下的测试会话）：
  会话：扫了 20 个、用了 18 个（共 53 条消息）；回放轮次 18
  注入条数(均) 1.11 · 注入token(均) 16.8 · 去重率 0.0% · 丢弃条数(均) 0.28
  状态回放一致率：100.0%（3/3 个会话的落库状态再校验一遍不会被改动）

清掉测试账号后（只剩 真实用户 的真实会话）：
  会话：扫了 2 个、用了 2 个（共 10 条消息）；回放轮次 4
  注入条数(均) 1.75 · 注入token(均) 1556.5 · 去重率 0.0% · 丢弃条数(均) 0.25
```

跑完前后五张表行数完全一致（`users/character_cards/world_books/narrative_sessions/messages`）。

两点必须如实说明：

- "状态块漏输出率"这一行为空：真实会话都是**迁移前**建的（`state_schema_json` 为 NULL），
  按"没定义状态栏就不要求输出状态块"的口径本就不统计 —— 这是设计如此，不是漏算。
- 第二组数字里"注入 token 1556"**超过了默认共享预算 1024**：因为该会话的世界书没配
  `token_budget`，而它那条设定本身就比预算大，`pack()` 的"至少保留最高分那条"
  （继承自旧世界书扫描器的规矩）会让它整条进来。这是**已知且刻意**的行为，
  代码注释与 `test_tiny_budget_still_injects_the_top_candidate` 都盯着它。

### 20.5 本轮的验收数字

| 项 | 结果 |
|---|---|
| `pytest -q` | **715 passed**（新增 `tests/test_retrieval.py` 22 条 + `tests/test_benchmark.py` 15 条） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **99 项检查，0 失败**（新增 1 条：「查看提示词」里有混合检索的计数与来源） |
| `scripts/benchmark.py` | 退出码 0，产出 `data/benchmark_report.json` / `.md` |
| `scripts/benchmark.py --from-db` | 退出码 0，真实数据只读回放（见 §20.4） |

> 踩到的坑（记一笔）：`state_schema.parse_schema` 一开始**丢掉了 `min`/`max`**，
> 于是"数值字段的上下限"声明了也不生效 —— 是 `state_cases.json` 里那条
> "自造数值字段超上下限：夹到边界"把期望标注和实际结果对不上才暴露的。
> 教训：benchmark 的用例**带期望值**（不只是跑出数字），它顺带就是校验语义的回归测试。
>
> 第二个坑：新写的测试文件**忘了往 `cleanup_demo_data.py` 的前缀清单里加用户名前缀**
> （`rt_` / `bk_`），于是反复跑 pytest 在真实库里堆了 60 多个测试账号。
> 因为夹具的 teardown 只在"用例正常跑到结尾"时才执行，中途失败就会留下账号 ——
> 这份清单就是给那种情况兜底的。**新增测试文件必须同步补前缀**（脚本注释里也写着这条）。

---

## 21. 第八轮：检索优先级修正 + 剧情总结改为**分层合并**（2026-09-26）

### 21.1 世界书绝不能被回忆挤掉（用户拍板的硬规则）

用户原话：

> "世界书一般是作者特意加在角色卡里面的，里面的设定比用户对话重要，不然容易出戏，
> 乱写和掉马甲之类的，**绝对不能挤掉**！以我个人看来重要度：世界书 ≥ 预设 > 用户对话聊天记录。"

第七轮我把装填做成了**纯分数竞争**（两路共享预算、谁分高谁先进），于是高分回忆可以顶掉
作者手写的设定 —— 这正是用户说的"出戏 / 掉马甲"来源。**已改成两级装填**（`retrieval.pack`）：

    ① 先按分数装**世界书条目**（彼此之间竞争，不与记忆竞争）
    ② 再用**剩余预算**装记忆（只能用世界书吃剩的）

- 世界书条目只会因为"自己这一批就超预算"而被丢，丢弃原因如实写明
  （"世界书优先，不会被回忆挤掉，但自己也装不下时只能丢弃"）。
- 守卫测试：`tests/test_retrieval.py::test_world_book_entries_are_never_evicted_by_memories`
  （记忆分数更高、预算只够一条 → 必须留世界书）、`test_memories_use_only_the_leftover_budget`、
  `test_world_book_too_big_is_dropped_for_its_own_reasons_only`。
- 预设本来就不参与检索预算（它是装配层的东西），所以"世界书 ≥ 预设"体现在**装配顺序**上：
  世界设定 → 回忆 → 当前状态 → 守卫 → 输出契约（预设块不塞进状态/回忆之间，见 §14 的说明）。

### 21.2 剧情总结：从"逐条摘录"改成"分层合并"（用户给的规则）

用户原话：

> "1~10 轮对话为一个总结；用户在第 20 轮总结的时候，系统先遍历之前的旧总结，
> 然后看 11~20 轮的新聊天记录，然后一起总结为一个新总结，标记为 1~20 轮对话，
> 然后将旧总结删除（防止重复扫描占 token）。"

**改之前**（3.8 的实现）有两处短板：只在超预算时才动，且是**本地逐条摘录**
（每条截 120 字）+ 每次**追加**在旧摘要后面，越叠越长、同一段剧情被反复扫描，
到 2000 字就掐中间留两头 —— 最该记住的中段最先被丢掉。

**现在**（`app/narrative/summary.py`）：

```
每积累 SUMMARY_BLOCK_ROUNDS（默认 10）**完整**轮 → 一次合并：
    输入 = 【旧前情提要】+【这一块的对话（每条留 400 字）】
    输出 = 一份新前情提要（模型写；200~500 字；保留人物/约定/事件后果/伏笔/当前处境）
    写回 = 正文开头标「（第 1~N 轮）」+ rolling_summary 替换（不是追加）
           + summary_from_round / summary_to_round / summarized_until_message_id
    效果 = 被覆盖的那一块**不再进提示词**（否则总结与原文同时占 token）
```

- **触发时机**：在"下一轮开始之前"（`engine.prepare_turn`）—— 第 10 轮答完后，
  你发下一句话时顺手合并；第二次在第 20 轮答完后。**不给流式回复的收尾再挂一次调用**。
- **轮数定义**：一问一答都齐了才算一轮（`summary.count_rounds`）。装配提示词时本轮
  用户消息已落库、回复还没生成，所以若按"用户条数"算，会把没答完的回合也总结进去
  （踩过：第一次实现就是这么错的）。
- **降级**：合并那次模型调用失败 → 退回本地压缩 + 在 warnings / PromptInfo 里
  **如实说明** `used_model=false`（静默降级是 bug）。
- **兜底**：预算提前爆掉时（还没攒够一整块），被裁掉的消息走 `summary.append_dropped`
  **并进同一份正文**（仍然只有一个表头、只占一份 token）。
- **开关**：`.env` 的 `HNE_SUMMARY_ENABLED`（默认 true；关掉就退回"只在超预算时本地压缩"）、
  `HNE_SUMMARY_BLOCK_ROUNDS`（默认 10）、`HNE_SUMMARY_MAX_CHARS`（默认 2000）。
- **落库**：`narrative_sessions` 加两列 `summary_from_round` / `summary_to_round`
  （`scripts/migrate_db.py` 追加，只加不删；已执行）。
- **界面**：会话「设置」里显示"剧情总结（前情提要 · 覆盖第 1~20 轮）"+ 规则说明；
  「查看提示词」会多一条 warning 说明"被覆盖的消息不再逐条发送"。
- 参考说明：用户提到可以看 `meimoai` 的做法，我搜过公开资料，只找到同名无关产品
  （Meemo 会议纪要、AI 电子魅魔游戏），**没有可参考的总结算法** —— 所以按用户给的规则实现，
  没有照抄任何不存在的资料。

### 21.3 本轮的验收数字

| 项 | 结果 |
|---|---|
| `pytest -q` | **728 passed**（新增 `tests/test_summary.py` 11 条 + `test_retrieval.py` 的 3 条优先级守卫） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **100 项检查，0 失败**（新增 1 条：「设置」对话框能打开且写明总结规则） |
| `scripts/benchmark.py` | 退出码 0（消耗预算的排序逻辑变了，但合成集指标不受影响 —— 那里没有"世界书被挤掉"的场景） |

---

## 22. 第九轮：记忆总结改成**用户可控**（「记忆管理面板」，2026-09-26）

### 22.1 用户的要求（原话 + 参考产品截图）

> "可自行决定是否开启总结提醒功能，比如对话每 8 轮弹出一个横幅提醒用户总结。轮次可由用户自己决定，
> 8 轮、10 轮、12 轮都行。总结与否也是由用户自己决定，**不强制**（因为总结要消耗 API 的 Token，
> 或者有些用户单纯不想总结）。总结的内容用户可修改，总结有提示词（这个可以写在守卫预设里），
> 分别有五个选项：1. 折叠-角色优先 2. 折叠-剧情优先 3. 表格总结 4. 照抄旧记忆生成新记忆 5. 自定义"

参考产品（meimodu 的记忆管理面板）里的分区：记忆总结开关 → 自动总结（每 N 轮 + 每次费用）→
总结内容（编辑 / 恢复上一次）→ 字数上限（分档）→ 总结模型（单独挑）→ 总结提示词（五选一）→ 记忆锚点。
★ 它的官网只有营销页、`meimoai15.com` 是需要登录的 JS 单壳，**没有公开算法说明**，
所以这一轮完全按用户口述 + 截图实现，没有照抄任何不存在的资料。

### 22.2 改了什么

**第八轮我做的"每 10 轮自动静默合并"是错的** —— 它会在用户没点过任何按钮的情况下花他的 token。
现在改成设置驱动（会话级，新列 `narrative_sessions.summary_settings_json`，只加列）：

| 设置 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 记忆总结总开关；关掉 = 不总结也不提醒 |
| `auto` | **false** | 到点自动总结（花一次模型调用）。★ 默认关，尊重"不强制" |
| `remind` | true | 到点弹横幅提醒（不花钱） |
| `rounds` | **8** | 每几轮触发（可在面板里改，2~50） |
| `mode` | `character` | 五种模式之一（见下） |
| `prompt` | "" | 自定义提示词（`mode=custom` 时用） |
| `use_preset_prompt` | false | 从提示词预设里的「记忆总结」块读提示词（identifier `memorySummary`） |
| `max_chars` | 2000 | 单份总结字数上限（1000 / 2000 / 4000 / 8000 / 20000 可选） |
| `provider_id` | null | 总结用哪个模型配置；null = 跟随会话模型（配置不可用会退回会话模型并记日志） |

**五种总结模式**（`summary.MODE_LABELS`，内置模板在 `_MODE_INSTRUCTIONS`）：

1. **折叠-角色优先**：以人物为骨架（身份/关系/称呼/态度/承诺），事件只留改变了关系的
2. **折叠-剧情优先**：以事件链为骨架（因果、不可逆改变、伏笔、当前处境）
3. **表格总结**：`| 角色 | 关系 | 关键事件 | 当前状态 | 未了结 |`
4. **照抄旧记忆生成新记忆**：★ **不调用模型**（0 token）—— 旧记忆原文照抄 + 追加这一段的本地摘录
5. **自定义**：面板里的文本框（也可勾「从守卫预设读取」）

**触发与界面**：

- 到点且 `auto=false` → 对话页顶部弹**横幅**：「已经积累 N 轮没总结了（第 X~Y 轮）·
  预计约 M token」+ [立即总结] [稍后] [不再提醒]。**刷新/切会话会重新出现**（稍后只压制当次视图）。
- `auto=true` → 到点自动合并，并给一条"已完成总结（第 X~Y 轮）"。
- 两个都关 → 什么都不做，纯手动（面板里的「立即总结」）。
- 面板（「记忆」→ 记忆管理面板）里：开关三连 + 每几轮 + 模式下拉 + 提示词 + 字数上限 + 总结模型 +
  **总结正文可编辑**（改完重新盖覆盖表头）+ **恢复上一次**（历史保留最近 5 版）+ 显示覆盖区间与预估消耗。
- 手写正文会被剥掉旧表头再重盖一个，避免出现两行表头。

**API**（都在会话下）：`GET /sessions/{id}/memory-summary`（面板状态，纯读）、
`PATCH`（保存设置 / 提交 `content` 直接改正文）、`POST /run`（**唯一**手动花 token 的入口）、
`POST /restore`（恢复上一次）。会话详情多了 `memory_summary`（开关 + 覆盖 + 待总结 + 预估消耗），
发送响应里 `prompt.summary_state` 也带上同一份状态。

**超预算兜底仍然保留**：预算提前爆掉时（还没到触发轮数），被裁消息走 `summary.append_dropped`
并进**同一份**正文（只有一个表头、只占一份 token），与用户是否开总结无关。

### 22.3 验证

| 项 | 结果 |
|---|---|
| `pytest -q` | **766 passed**（`tests/test_summary.py` 17 条：含"自动关着时一次模型调用都不许多花"、照抄模式 0 消耗、手动总结、编辑 + 恢复、面板五模式） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **104 项检查，0 失败**（新增 2 条：面板里五种模式齐全且能编辑正文；点「立即总结」真的生成总结） |

> 踩到的两个坑：① `SummarySettingsUpdate` 忘了 import，FastAPI 把 `payload` 当成了
> **查询参数** → PATCH 一直 422（报错信息还是"payload Field required"，很容易看歪）；
> ② 中文全角引号混进 Python 字符串用了 ASCII 双引号 → 连带三处语法错误。

---

## 23. 第十轮：记忆锚点（Stage 2，2026-09-26）

### 23.1 它是什么、和别的"记忆"差在哪

用户手写的**硬设定**（"主角是女性""绝不能承认自己是 AI""这个世界没有魔法"）：
**每轮整条注入系统提示词，既不参与召回、也不会被剧情总结折叠**。
参考产品的面板里就是那一块「记忆锚点 0/5 · 0/2000 字」+「+ 添加锚点」。

| 东西 | 谁写的 | 会不会被压缩/丢弃 |
|---|---|---|
| 世界书条目 | 角色卡作者 | 按关键词触发，本轮可能不注入 |
| 长期记忆（向量） | 系统自动记 | 按相似度召回，可能召回不到 |
| 剧情总结 | 模型写（用户可改） | 下一次合并会**替换**它（旧的被折叠） |
| **记忆锚点** | **用户手写** | **永远整条注入**（在系统提示词里，裁剪动不了它） |

### 23.2 实现（`app/narrative/anchors.py`，新）

- 上限：**最多 5 条**、合计 **2000 字**、单条 500 字；超限**拒绝并说明超在哪**
  （不静默截断 —— 悄悄存 5 条、下次打开发现少一条更糟）。空白条目剔除、重复去重并如实提示。
- 落库：`narrative_sessions.memory_anchors_json`（只加列，已迁移）。
- 注入位置：**系统提示词**里，排在「世界设定」之后、「回忆」之前 ——
  内置装配与预设装配**两条路径顺序一致**（`extra_parts` 统一）。纯聊天不注入。
- 预览一致：「查看提示词」里也能看到锚点（同一个装配入口，不是前端另拼一份）。
- API：`GET /sessions/{id}/memory-summary` 带 `anchors`（含配额余量）；
  `PUT /sessions/{id}/memory-summary/anchors` **整份替换**（面板就是"最多 5 条的清单"，
  整份提交最不容易出现"前端以为删了、后端还留着"）。
- 前端：面板新增 📌 记忆锚点 分区（计数 `N/5 · M/2000 字`、逐条输入 + 删除、
  「+ 添加锚点」到上限自动禁用、「保存锚点」）；`api.js` 补了 `put()`（整份替换语义）。

### 23.3 验证

| 项 | 结果 |
|---|---|
| `pytest -q` | **766 passed**（新增 `tests/test_anchors.py` **13 条**：上限拒绝、清洗去重、渲染、真的进系统提示词、**上下文裁光后锚点仍在**、超限 400、整份替换、纯聊天不注入） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **104 项检查，0 失败**（新增 2 条：面板能加锚点并保存、计数变 1/5；锚点出现在提示词预览里） |
| `scripts/benchmark.py` | 退出码 0 |

> 至此「记忆管理面板」和参考产品的分区已经对齐：开关 / 自动 / 轮数 / 模式五选一 /
> 字数上限 / 总结模型 / 总结提示词 / 可编辑正文 + 恢复上一次 / 记忆锚点。

---

## 24. 第十一轮：总结按钮的反馈 + 防重复总结（2026-09-26）

### 24.1 用户报的问题（真实事故）

> "横幅给的总结按钮点下去后没有给用户反馈是否正在总结，刚刚我就犯下一个错误。
> 不知道有没有开始总结，点了开几遍，结果 ai 也给我总结了好几遍。"

**根因有两条，缺一不可**：

1. **界面**：横幅上的「立即总结」点了之后没有任何"正在跑"的反馈
   （面板里的按钮有 `buttonLoading`，横幅那个没做）。→ 用户以为没点上，连点。
2. **后端**：同一会话的总结**没有串行化**。连点的请求并发进来时，
   每一个都还看到"有未总结的轮次"，于是**各跑一次模型调用、各写一份总结**。
   （顺序到达的第二次点击本来是无害的：覆盖区间已推进，没有新内容可总结。）

### 24.2 修法

**界面（反馈必须摆在第一个 `await` 之前 —— 这样点击是"同步可见"的）**：

- 横幅按钮：点下去**立刻**变 `⏳ 总结中…` 且 `disabled`，横幅上同时出现
  "正在总结（要调用一次模型，请稍等）…"；请求结束/失败后恢复并给结果提示。
- 面板里的「立即总结」同样改成 `正在总结…`。
- 失败（含 409）时横幅上**明说**"这次没有重复总结（上一次还在进行中，或已经没有新内容可总结）"。

**后端（`summary.py`）**：

- 每个会话一把锁（`_lock_for` / `is_busy`），`merge_block` **非阻塞抢锁**：
  抢不到就直接返回 `BUSY_REASON`（"上一次总结还在进行中…"），**不会再跑模型**。
- `POST /memory-summary/run` 拿到 `BUSY_REASON` 时返回 **409 Conflict**（前端据此提示）。
- 面板 `GET` 增加 `busy` 字段，前端据此可以把按钮置灰。

**顺手修掉一个真问题（探针诊断出来的）**：

- 之前的"立即总结"在一轮完整对话都没有时，会把**开场白**当成"第 1 轮"总结掉
  （实测看到总结内容就是开场白本身，还白花一次模型调用）。现在 `total <= 0` 直接拒绝：
  "还没有完成一轮对话（一问一答都齐了才能总结）"。

### 24.3 关于「照抄旧记忆生成新记忆」

用户给了参考产品里那一档的说明文字：

> 照抄旧总结并压缩最新的聊天记录生成新的记忆

**这正是我们模式 4 的语义**（旧总结原文照抄 + 把最新一段聊天本地压缩后追加），
差别只是我们没有"压缩"这一步的模型调用 —— 用的是本地逐条摘录（每条 120 字），
所以它是**唯一 0 token** 的模式。面板里的说明文案已改成与参考产品一致的措辞。
★ 五个模式的**原始提示词**用户和我都拿不到（官网只有营销页、网页版是登录壳），
模式 1/2/3 的模板是本项目按模式名称自己写的，将来若拿到原文可以只改 `_MODE_INSTRUCTIONS`。

### 24.4 验证

| 项 | 结果 |
|---|---|
| `pytest -q` | **766 passed**（`test_summary.py` 新增 4 条：连点两下只总结一次、并发被 409 挡住且不调模型、`busy` 状态可见、无完整轮次时拒绝） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **108 项检查，0 失败**（新增 3 条：横幅会出现、点下去**同步**变「总结中…」并禁用、完成后给结果） |
| `scripts/benchmark.py` | 退出码 0 |

> 探针这一节现在会先造 3 轮真实对话（因为"总结只认完整的一轮"，而前面的编辑/撤回
> 会把轮次清空 —— 探针的失败详情里直接打印了接口返回的 `{due, pending, rounds}`，
> 一眼看出 `pending: 1 < rounds: 2`，比猜快得多）。

---

## 25. 第十二轮：状态漂移曲线（2026-09-26）

### 25.1 先修掉一个**错的指标**（第七轮留下的）

`benchmark --from-db` 的"状态块漏输出率"当初是按
「助手正文里有没有 `<state>`」算的 —— 但 `<state>` 块在**落库前就被剥掉**了，
所以这个条件恒成立，指标**永远 100%**（`test_benchmark.py` 里那条断言"漏输出率 = 1.0"
其实是因为这个 bug 才通过的）。**第十一轮起改成读遥测**，并把这个错误写进了文档。

### 25.2 遥测落库（`messages.state_meta_json`）

每轮助手消息落库时（`engine.save_assistant_reply` 是唯一落库点）记下：

```json
{"required": true, "had_block": true, "malformed": false,
 "counts": {"clamped":1,"guarded":0,"rejected":0,"unknown":0,"other":0},
 "deviation": 0.12, "fields_reported": 4}
```

- `required=false` = 这一轮压根没要求状态块（纯聊天 / 卡没声明状态栏）→ **统计时不算它漏输出**。
- `counts` 由 `state.classify_notes` 分类：**四种机制分开计**
  （`clamped` 越界夹取 / `guarded` 上限跳变护栏 / `rejected` 脏值丢弃 / `unknown` 未知字段忽略）——
  合成一类就看不出是哪一层在起作用。
- `deviation` = 模型自报 vs 校验后落库的偏离度（数值按相对差、集合按 Jaccard 距离、文本按 0/1）。
- 迁移：`scripts/migrate_db.py` 给 `messages` 加一列（只加不删，已执行）。

### 25.3 漂移曲线（`app/narrative/state_drift.py` + `drift_script.json`）

同一批"模型输出"跑**两条各自闭环**的线（这点很关键）：

- **实际（有校验）**：模型每轮看到的是**校验后**的状态（被夹住的 `hp: 100`）
- **反事实（不校验）**：模型看到的是**它自己上一轮写下的**状态 → 漂移一轮轮累积

指标：`naive_out_of_range`（越界程度，标尺固定在**故事开始时的上限** ——
否则模型把 `max` 也写飘就等于自己给自己放宽区间，曲线会假性归零）、
四类机制的触发率、`deviation`、`naive_garbage_rate`（脏值被直接存进去的比例）。

**实测**（40 轮脚本：每 4 轮 HP +25，穿插越界/漏输出/上限跳变/脏值/未知字段/嵌套写法）：

| 轮次档 | 漏输出 | 夹取 | 护栏 | 脏值 | 未知字段 | 偏离度 | 不校验越界 | 校验后越界 | 反事实脏值率 |
|---|---|---|---|---|---|---|---|---|---|
| 1-5 | 0% | 40% | 0% | 0% | 0% | 0.021 | 0.40 | **0.00** | 0% |
| 6-10 | 20% | 20% | 0% | 0% | 0% | 0.008 | 0.94 | **0.00** | 0% |
| 11-15 | 0% | 40% | 20% | 0% | 0% | 0.038 | 3.20 | **0.00** | 0% |
| 16-20 | 0% | 40% | 0% | 0% | 0% | 0.043 | 3.55 | **0.00** | 40% |
| 21-25 | 0% | 20% | 0% | 0% | 20% | 0.008 | 3.85 | **0.00** | 100% |
| 26-30 | 0% | 20% | 0% | 0% | 0% | 0.008 | 4.15 | **0.00** | 100% |
| 31-35 | 20% | 20% | 0% | 20% | 0% | 0.048 | 4.50 | **0.00** | 80% |
| 36-40 | 0% | 40% | 0% | 20% | 0% | 0.056 | 4.75 | **0.00** | 100% |

> 总计：40 轮 · 漏输出 5.0% · 至少修正一次 40.0% · 不校验越界均值 3.10（峰值 4.75）
> **vs 校验后 0.00** · 反事实脏值率 52.5%。
> 产出：控制台 `[6]/[7]` 节 + `data/benchmark_report.json` 的 `drift` +
> `data/benchmark_report.md` + **`data/benchmark_drift.svg`**（零依赖 SVG，可直接进论文）。

`--from-db` 里还多了 `[7] 真实数据的状态遥测`：按轮次窗口聚合真实遥测，
并**如实报告有多少条是加遥测之前的历史数据**（反推不出来，不假装有）。

### 25.4 顺带修掉的两处口径问题

1. `replay_from_db` 的漏输出率/回放一致率在**没有遥测**时不再报 `0.0%`，而是报
   "无可用遥测" —— `0.0%` 会被读成"模型从不漏输出"，那是假的。
2. `apply_reply` 在**卡没声明状态栏**时不再提醒"模型没输出状态块"
   （我们压根没要求过，不该反过来怪模型）。为此把那条老测试的会话换成了
   声明了 schema 的卡（断言强度不变），并**新增**一条"没声明的卡必须静默"的断言。

### 25.5 验证

| 项 | 结果 |
|---|---|
| `pytest -q` | **766 passed**（新增 `tests/test_state_drift.py` 14 条：模拟可复现、反事实越界单调上升、标尺取自初始状态、算不出来时给 None、四类机制都覆盖、SVG 两条线、遥测真的落库、`required=false` 不参与统计、真实聚合只读） |
| `scripts/smoke_test.py` | **142 / 0** |
| `scripts/ui_probe.py` | **108 项检查，0 失败**（本轮没有前端改动） |
| `scripts/benchmark.py` / `--from-db` | 均退出码 0，产出漂移曲线 SVG |

> 踩坑：手工拼 Markdown 报告时，把条件表达式插进字符串拼接里漏了一个 `+`，
> 连续两次 `SyntaxError` —— 教训是这种"长表达式拼文档"最好拆成几步赋值。

---

## 26. 第十三轮：骰子插件 + 五套总结提示词 + 删除群聊（2026-09-26）

用户这一轮的原话（决定都在里面）：

> 先做骰子插件，群组聊天难度大就不做了！把群组聊天从任务书里删除！
> 角色卡VN模式和自动翻译可以做，Electron打包先别急，后面再做
> 五个总结模式的原始提示词：我不知道，你自己想一套提示词吧

### 26.1 骰子插件：为什么必须由后端掷

大模型**没有真随机**：它倾向于给出"恰到好处"的数字（需要成功写 18、需要失败写 3），
同一个骰点两次回答还能不一样。而跑团、检定、掉落表的乐趣恰恰建立在
"结果不由叙述者决定"上。所以骰点由后端的**受控求值器**掷出，模型只解释结果。

两条路都走同一个求值器：

```
玩家：/r 1d20+5 力量检定          → 🎲 力量检定 1d20+5 = 17（骰子 12）
模型：他抬手格挡。<roll>1d20+5</roll> → 系统替换成 （掷骰 1d20+5 = 17）（骰子 12）
                                     下一轮模型在历史里看到的就是真实点数
```

### 26.2 求值器（`app/narrative/dice.py`）

- **自己写的递归下降解析器**：分词 → 四则（`+ - * / // %`，`/` 按整除）→ 一次比较。
  **不用 `eval`、不放开 JS** —— 与 `plugins` 那条"绝不执行第三方 JS"是同一条红线。
  骰子记法：`NdM`（`d20` 省略个数）/ `kh`·`kl` 取高取低 / `!` 爆炸骰（有深度上限）/
  括号 / `<= >= < > = !=` 成功判定 / `# 说明`（没有 `#` 时表达式之后那段话也算说明）。
- **一切有界**：表达式 ≤240 字符、一次 ≤100 颗骰、单颗 ≤1000 面、token ≤40、
  括号 ≤8 层、爆炸 ≤10 次、中间结果绝对值上限。超限**拒绝并说明原因**。
- **任何输入都不抛异常**：坏表达式落在 `RollResult.error` 里（用户会看到中文原因）。
- 触发词**长的优先**（`/roll` 不能被更短的 `/r` 抢走 —— 第一版就踩了，
  剩下的 `oll 1d20` 被当成表达式，报"看不懂的字符 o"）。
- 触发词**必须紧贴行首**，一条消息最多认 5 次（超出的忽略并说明）。

### 26.3 点数落库：为什么不能"用的时候现掷"

`/r` 的结果在 **`engine.save_user_message`（用户消息唯一的落库点）** 掷定，
写进新列 `messages.rolls_json`（含逐颗骰面与取舍过程，可复核"这个 17 是怎么来的"）。

理由是本项目那条"**预览与实际必须一致**"：如果改成构建提示词时现掷，
用户点一次「查看提示词」就会掷出**另一个数字**（重掷等于抽卡）。
所以规则是：**掷一次、存下来、所有人读同一份**。

模型的 `<roll>` 在 `engine.save_assistant_reply`（助手消息唯一落库点）结算：
**无论插件是否启用都先剥掉标签**（绝不让用户看到原始协议文本），
启用时替换成明文点数；没闭合的标签只替换标签本身，
**不整段截断**（与 `<state>` 不同：掷骰常写在句子中间，截掉会把正文一起吞掉）。

### 26.4 提示词里的两个位置（刻意的）

| 内容 | 谁加的 | 放在哪 | 为什么 |
|---|---|---|---|
| **规则**（"你没有随机数能力 + 怎么写 `<roll>`"） | `plugin_runtime` | 系统提示词**末尾** | 它是"必须照做"的机械指令，越靠后权重越高 |
| **本轮点数**（`## 本轮骰点`） | `build_prompt(dice_block=…)` | 「当前状态」之后、守卫之前 | 它是**客观事实**不是指令，不该占末尾那个位置 |

纯聊天也会认骰子（用户既然启用了这个插件）：`build_prompt` 的纯聊天分支里
只多这一块，人设/预设/状态协议仍然一概不注入。

### 26.5 插件形态

`PLUGIN_KINDS` 从三种变四种（`regex` / `prompt` / `css` / **`dice`**）：
配置校验（触发词、默认表达式**当场掷一次做语法检查**、上限、三个开关）与
"从 GitHub 安装"走**同一个** `validate_config`（老规矩：两套校验迟早打架）。
内置目录第一条就是「跑团骰点（Dice）」，一键添加、可停用。
前端：消息底下 🎲 骰子气泡（成功/失败染色、悬停看逐颗骰面），插件编辑器有骰子专属表单。

### 26.6 五套总结提示词（本项目自拟）

参考产品只公开了模式名和一句说明，**没有公开原文提示词**。按那五个名字自己设计了一套，
写在 `summary.py._MODE_INSTRUCTIONS`：五档各写"组织方式"，
公共约束（不许编、不许推演、不许流水账）仍留在 `SUMMARIZE_SYSTEM_PROMPT`，
避免同一句话在五个地方各写一遍。

顺带修了预估消耗：以前只算公共提示词那一段，五档指令加上去之后会**少报**
（界面上"预计消耗"是用户决定点不点的依据）—— 现在按"公共 + 本档指令"或"用户原文"算。

### 26.7 群组聊天：删除

用户明确"难度大就不做了"。已从 `handoff-quick` §4 的可选表里**删除**并注明不再提
（它此前是文档里唯一出现过"群聊"的地方；README 计划书里本来就没有它）。

### 26.8 本轮踩到的三个坑

1. **探针里一个真实的竞态**（产品 bug 级）：骰子节收尾把界面留在**对话页**，
   下一节「纯聊天」的"点导航 → 立刻点 + 纯聊天"就撞上异步重渲染 ——
   弹窗拿到**旧 view signal**，重渲染 aborted 它之后
   「创建并开始」走 `if (signal?.aborted) return;`：**会话建好了、界面毫无反应**
   （两条断言变红）。修法：骰子节收尾停在**插件页**，并把原因写在注释里
   （下次谁改这一段都会看到）。
2. **目录点击不能靠顺序**：探针原来点"目录里第一个"添加按钮，我往目录最前面插了骰子条目
   之后就点错了东西 → 改成按 `data-catalog-add="authors_note"` 精确点。
3. `plugin_service.validate_config` 里插新分支时把 `# kind == "css"` 那段一起带走了
   （控制流/缩进错位）→ 加分支后**先跑一遍校验层测试**，立刻现形。

### 26.9 验证（串行跑完）

| 项 | 结果 |
|---|---|
| `pytest -q` | **799 passed**（新增 `tests/test_dice.py` 26 条、总结模式 4 条、benchmark 骰子 3 条） |
| `scripts/smoke_test.py` | **154 / 0**（新增 12 条：目录添加 / 坏表达式拒绝 / 掷骰落库 / 预览一致） |
| `scripts/ui_probe.py` | **116 项检查，0 失败**（新增 8 条骰子节，含"刷新后点数不变"） |
| `scripts/benchmark.py` | 退出码 0，新增 `[8] 骰子求值器体检`（8 种记法 × 6000 次、卡方、可复现性）与 MD 表 |

数据库只加一列：`messages.rolls_json`（`scripts/migrate_db.py` 已执行，只加不删）。

---

## 27. 第十四轮：角色卡 VN 模式（立绘 / 表情）（2026-09-26）

用户原话里的第三件事：**"角色卡VN模式和自动翻译可以做"**（Electron 先不做）。
这一节只讲 VN；自动翻译是下一轮。

### 27.1 核心设计：表情 = 状态栏里的一个字段

**没有新协议、没有新列、没有第二次模型调用。** 卡里这样声明：

```json
"extensions": {"hne": {"vn": {
    "background": "https://…/classroom.png",
    "sprites": {"平静": "https://…/calm.png", "生气": "https://…/angry.png"},
    "expression_field": "mood",
    "default": "平静",
    "position": "center"
}}}
```

模型照常每轮输出 `<state>{"mood": "生气", …}</state>`，后端算出"现在该显示哪张立绘"
（`SessionDetail.vn.sprite_url`），会话页的 🎭 舞台把它画出来（背景 + 立绘 + 名牌 + 台词框）。

为什么挂在状态栏上，而不是新加一个 `<expr>` 标签：

1. 模型**已经**每轮输出状态块（第六轮起字段由卡定义）——多一个字段不需要任何新协议，
   也不多一次模型调用（这对"省 token"这条硬要求很关键）；
2. 立绘与文字剧情天然同步，不会出现"嘴上说生气、脸上还在笑"；
3. 与第六轮那条规矩一致：**状态栏字段由卡/世界书说话**。作者声明了哪些表情，
   建会话时会把这些**可选值写进该字段的描述**，模型于是知道该填哪些词，
   而不是瞎写一个"没有立绘"的词。

### 27.2 三处关键实现

| 关注点 | 落点 | 为什么要在这里 |
|---|---|---|
| 校验 + 选图 + 舞台数据 | `app/narrative/vn.py` | 纯函数、无依赖，测试与探针都能直接断言 |
| 写入口就洗干净 | `character_card_service._sanitize_extensions`（create / update / import） | 这份配置会进 `<img src>`：存进去的就该是干净的，不能等渲染时才过滤 |
| 建会话时补表现字段 | `sessions.create_session` → `vn.ensure_expression_field` | 没这个字段立绘**永远不会变**，作者会以为功能坏了 |
| 接口吐舞台数据 | `SessionDetail.vn`（`serialize_detail`） | "表情 → 立绘"只有一份规则，前端不各写一套映射 |

```python
# 作者没定义表现字段 → 自动补一个，并把可选值写进描述
fields.append({"name": "mood", "label": "表情", "type": "text",
               "description": "当前表情；可选值：平静 / 生气（决定显示哪张立绘）"})
```

作者自己定义过就**一个字都不动**（标签、描述按他的来）——尊重卡的作者，与第六轮同一取舍。

### 27.3 安全边界（与插件 CSS / 富文本同一套规矩）

- 只收 `http(s)://` 与 `data:image/(png|jpeg|webp|gif);base64,`；
  `javascript:` / `file:` / **`data:image/svg+xml`** 一律拒绝
  （SVG 可能带脚本；`<img>` 里多数浏览器不执行，但"能不能执行"不该取决于浏览器版本）。
- 单条地址 ≤ 300KB 字符（内嵌 base64 用）、最多 24 张立绘；
  **坏图不废卡**：丢掉那一条 + 记 warning（会话里如实提示，而不是白屏）。
- 背景用 `<img>` 而不是 CSS `background-image`：地址来自用户，拼进 `style` 属性
  就要担心 CSS 字符串逃逸；`<img src>` 过一遍 `esc()` 就够。
- 舞台开关存浏览器本地（按会话）：它是"看的方式"不是数据，不该为它发一次 PATCH。
- 纯聊天 / 没声明 VN 的卡 → `detail.vn = null`，界面连开关都不显示。

### 27.4 又踩了一次"旧 view signal"（这次在探针里）

`11.拖动大小` 两条**偶发**变红：合成的 pointerdown/pointermove 派发了，宽度却一点没变。
根因与第十三轮那条同源 —— 点在"已经处于该路由"的导航上仍可能触发一次异步重渲染，
探针紧接着 `querySelector('#chat-resize')` 拿到的是**旧节点**，而它的监听器已经随旧
signal 被 abort。所以"拖不动 / 双击没反应"。

修法：拖动前**先切到别的页再切回来**（探针里已有这个手法），保证这次视图是新渲染的。
> 教训（第 35 条 pitfall 的第二次实例）：凡是要给某个元素派发事件，
> 先确认那个元素属于**当前这次渲染**。

### 27.5 验证（串行跑完）

| 项 | 结果 |
|---|---|
| `pytest -q` | **820 passed**（新增 `tests/test_vn.py` 21 条：地址白名单 / SVG 拒绝 / 体积与张数上限 / 两种写法 / 默认表情回退 / 大小写容错 / 补字段且尊重作者定义 / 写入口洗数据 / 舞台跟着状态变 / 未知表情回退并说明 / 纯聊天与普通卡无舞台 / 导出往返） |
| `scripts/smoke_test.py` | **162 / 0**（新增 8 条：非法字段名改回默认、舞台数据、可选表情清单、状态变→立绘换） |
| `scripts/ui_probe.py` | **121 项检查，0 失败**（新增 5 条 VN 节：舞台四层都在 / 开关能关能开 / 改状态换立绘；顺带修掉拖动竞态） |
| `scripts/benchmark.py` | 退出码 0（本轮不涉及检索/状态逻辑） |

数据库**没有新增任何列**：VN 配置住在角色卡的 `extra_data.extensions` 里，
与 `state_schema` / `initial_state` 同一个命名空间。

> 📷 探针顺手留了一张"舞台长什么样"的截图：`data/ui_probe_vn.png`（同时复制到
> `docs/vn-screenshot.png`，README 与论文素材都引它）。
> ★ 探针里的立绘/背景用的是**内嵌纯色小图**（探针必须离线可跑），而且刻意做成
> 80×140 / 320×180 —— 一开始用了 1×1 的图，截图里"等于没画"，
> 那张截图就失去了人工复核排版的意义（这是真实踩到的）。

---

## 28. 第十五轮：自动翻译中间件（跨语言对话）（2026-09-26）

用户原话里的第三件事的后半："角色卡VN模式和自动翻译可以做"。VN 在 §27，这一节是翻译。

### 28.1 它解决什么问题

大量角色卡是英文 / 日文写的。中文用户直接聊会有两个后果：模型被"卡的语言"带着走、
回你一串外语；以及用户自己写的句子模型可能理解偏。本项目不集成翻译 API，
而是复用**统一 LLM 调用层**里的任意模型配置来做这一步 —— 于是"用哪个模型翻译"
本身也成了一个可替换、可对比的实验变量（论文里可以当消融项）。

### 28.2 数据模型：一条消息最多两份文本

| | `content`（**永远是模型看到的文本**） | `translation.text` | `display` |
|---|---|---|---|
| 输出侧 `reply` | 模型原文（外语） | 译文 | `translation`（默认看译文） |
| 输入侧 `input` | **译文**（模型看到的就是它） | 用户原文 | `content`（默认看原文） |

把译文写进输入侧的 `content` 是刻意的：提示词装配本来就只读 `content`，
于是**不需要任何"替换历史"的机制** —— 少一处改动就少一处"预览与实际不一致"的机会。
`display` 只是"给人看哪一份"，前端据此渲染并把两份都放进 DOM（切换是纯前端的）。

### 28.3 三档模式：成本必须由用户选（默认关闭）

| 模式 | 额外调用 | 说明 |
|---|---|---|
| `off` | 0 | **默认**：本项目对"未经同意花用户 token"零容忍（与记忆总结同一条规矩） |
| `prompt` | **0** | 只在系统提示词**最末尾**加一句「正文一律用 X 书写」（越靠后权重越高） |
| `middleware` | 每轮 1 次 | 生成之后真的调一次模型翻译；原文/译文都留着，可切换 |

- 方向：`reply` / `input` / `both`。
- **可以指定另一个模型专门翻译**（与"总结用哪个模型"同一套容错：配置被删/停用就退回会话模型）。
- 界面如实显示"这一轮预估 N token / 已译 N 条 / 翻译累计 N token"。

### 28.4 两条省钱的判断（本地做，不调模型）

1. **已经是目标语言就不译**：中文看 CJK 占比 ≥40%、英文看 ASCII 字母占比 ≥70%
   → `used_model=false`、**0 token**。判得**保守**：宁可多译一次也不漏译。
2. **太长就不译**（>6000 字）：一轮几千字的翻译很贵，而且多半是整段剧情复述；如实说明原因。

### 28.5 一个隐蔽的坑：记忆库要写"给人看的那一份"

跨语言会话里，用户用中文提问、检索也是中文，而 `content` 在输入侧是译文、输出侧是原文。
如果直接把 `content` 写进长期记忆，下一轮用中文提问时**召回质量会明显下降**
（表现为"模型突然记不住约定了"）。所以 `engine._display_message()` 统一取
**用户读到的语言**那一份来写记忆 —— 翻译与长期记忆这两条链路在这里交汇。

### 28.6 落库、成本与失败

- `narrative_sessions.translate_settings_json`（会话级设置，与记忆面板同构）
- `messages.translation_json`（`text / direction / lang / display / used_model / skipped /
  tokens / model / provider_id / error`）
- 翻译消耗计进 `session.total_tokens`，并在这一轮的 `notes` 里写一句
  （"本轮回复已由翻译中间件译成简体中文（47 token）"）—— 花在翻译上的钱必须看得见。
- 失败**只记 notes / 日志**：回复一个字都不会丢（与剧情总结降级同一套哲学）。
- SSE 的 `done` 事件里也带上 `translation`：译好了前端立刻切成译文，
  不必等下一次刷新（否则用户先看到一串外语）。

### 28.7 假模型加了两条约定（探针/冒烟靠它驱动）

`scripts/fake_openai_server.py`：
- 系统提示词里出现 **「翻译中间件」** → 回固定的假译文 `【本地假译文】…`；
- 用户消息里出现 **「英文回复」** → 假模型**用英文回复**（驱动输出侧翻译的唯一办法）。

> 假模型保持**通用**：它不认"谁在调它"，只认提示词里写了什么。

### 28.8 验证（串行跑完）

| 项 | 结果 |
|---|---|
| `pytest -q` | **842 passed**（新增 `tests/test_translate.py` 22 条：默认关闭 / 三档模式 / 两个方向 / 语言短路 / 太长跳过 / 失败不抛 / 独立翻译模型 / 两份文本落库 / PATCH 语义） |
| `scripts/smoke_test.py` | **179 / 0**（新增 17 条：默认关闭与 0 消耗 / 译文落库 / 已是中文不译 / 输入侧原文不丢 / prompt 模式 0 token） |
| `scripts/ui_probe.py` | **128 项检查，0 失败**（新增 7 条翻译节：面板三档 / 开关生效 / 界面显示译文 / 一键切换 / 落库两份） |
| `scripts/benchmark.py` | 退出码 0（不涉及检索/状态逻辑） |

数据库新增两列（迁移脚本已执行，只加不删）：`messages.translation_json`、
`narrative_sessions.translate_settings_json`。截图留档：`docs/translate-screenshot.png`。

> ★ 本轮踩的坑：冒烟测试一开始整段 404 —— **服务还是旧进程**（端点是这一轮才加的）。
> 改 `app/**` 之后必须重启 uvicorn；这条在 §7 已知坑里，但每次加新端点都容易忘。

---

## 29. 第十六轮：翻译语言设置收敛 + PNG 导入文案纠错 + 两个"人工复核项"的证据边界（2026-09-27）

这一轮从"人工复核"开始，最后落成**一次功能改动 + 一次文案纠错**：

1. 用户接手后要人工复核两件事（① VN 换成真立绘后的排版；② 翻译中间件在真实模型上的
   效果与花费）。复核过程中发现一处**界面文案与实现不符**（PNG 导入），修掉它，
   并把这两件事的"自动化证明不了什么"写清楚（§29.1~§29.4），免得下一轮误以为已经验过。
2. 用户试用翻译中间件后提出**真实的可用性问题**："默认居然是中文翻译成英文，我还得自己
   切换语言；我的目标对象大部分是中国人"。于是把语言设置**收敛成一个目标语言 + 源语言
   自动识别**，并顺带修掉一个让"日文自动识别"静默失效的真 bug（§29.6~§29.9）。

### 29.1 修的是什么：PNG 导入弹窗说了做不到的事

`web/js/views/cards.js` 的「导入角色卡 PNG」弹窗原文写着：

> 卡片数据藏在图片的文本块里，**图片本身还能当人物立绘**。直接选文件即可，会自动识别。

**前半句是真的，后半句没有任何实现。** PNG 导入的完整链路是：

```
POST /character-cards/import-png            app/api/v1/character_cards.py:131-153
  → import_card_from_png()                   app/services/character_card_service.py:872-914
      ① 10MB 上限校验
      ② extract_card_json(raw)               app/utils/png_card.py:188   ← 只返回卡片 dict
      ③ 复用 import_card()（与 JSON 导入同一条路）
```

**图片字节在 ② 之后就被丢弃了**：没有任何一处把 PNG 存成 `avatar_url` / 立绘 / 背景。
导入后唯一可能带图的字段全部来自**卡 JSON 自己**：`extensions.hne.avatar_url`（→ 头像）、
`extensions.hne.vn.background` / `.sprites`（→ VN 立绘），而 `vn.extension_of()` 只认
`extensions.hne.vn`、**没有**任何回退（`vn.py:75-92`）。

新文案如实说明：**导入只取卡数据（名字 / 开场白 / 世界书），图片本身不入库**，
要当 VN 立绘或背景得在卡编辑器里填地址。

> ★ 顺手留下的判断：`app/api/v1/character_cards.py:132`、`app/utils/png_card.py:5`、
> `README.md` 里"图片本身还能正常显示成人物立绘"这几句**没有改** —— 它们描述的是
> PNG 这个格式本身（图片文件仍然是张能看的图），不是"导入会用它"。下一轮别再当成
> 漏改的文案去"修"一遍。

### 29.2 想真做"导入即自动当立绘"的话，先看这两个数字

| 约束 | 值 | 出处 |
|---|---|---|
| PNG 上传上限 | **10 MB** | `character_card_service.py:66` `MAX_PNG_BYTES` |
| VN 单条图片地址上限 | **300,000 字符** | `vn.py:57` `MAX_URL_CHARS` |
| base64 膨胀 | ×4/3 | 编码本身 |

10MB 的图内嵌成 data URI ≈ 13MB 字符，**必然超限**；而且导入响应会把整张卡（含 base64）
吐回前端。所以只有两条路：**导入时压缩/缩放再内嵌**，或**加静态文件挂载**
（目前 `app/main.py:332` 只挂了 `web/`）。两条都要改 `app/**` + 迁移/新增测试，本轮未做。

### 29.3 两个复核项的"证据边界"（写进论文请照抄这段）

| 项 | 自动化能证明 | **自动化证明不了（必须人工跑一轮）** |
|---|---|---|
| VN 舞台 | 地址取自卡、`表情 → 立绘`由后端算、开关生效（探针 5 条） | 透明底 PNG 的合成、真实长宽比取景、背景 `object-fit: cover` 裁切、`sprite_scale` 放大后被 `overflow: hidden` 裁顶 |
| 翻译中间件 | 三档模式、双向、语言短路 0 token、太长跳过、失败不抛、两份文本落库（pytest 22 + 探针 7） | **真模型的译文质量与真实 token 花费**（探针/冒烟一律用假模型） |

★ 两条容易误判的"不是 bug"：
1. **翻译的语种是面板设置、不是卡属性**（`translate.py::run`：两个方向都用 `target_lang`）
   → **没有外语卡也能测**：任意中文卡 + 目标设成英文（或反过来用外语卡、目标留简体中文）。
   你写中文、目标也是中文时会命中"已经是目标语言"→ `used_model=false`、**0 token、什么都不发生**。
2. **探针截图里的立绘是内嵌纯色小图**（探针必须离线可跑），观感不在断言范围内。

真模型花费的取证路径：🌐 面板「这一轮预估 / 翻译累计」→ 每轮提醒里的 `(N token)` →
`session.total_tokens`（翻译消耗计进会话累计，`engine.py:761-763` / `842-843`）→
逐条留痕 `data/logs/app_*.log` 的 `回复已翻译 | … tokens=…`。

### 29.4 本轮新增的测试资产

`data/test-cards/translate-test-emily.json` —— 一张**英文**角色卡（Emily Carter，灯塔守夜人），
用途是让"真模型翻译"这件事有个确定的起点：

- 卡的语言是英文（`description` / `personality` / `scenario` / `first_mes` / `mes_example` /
  `system_prompt` / 尾注 **全英文，0 个中文字**）；
- **★ v1.2（两次真机反馈后定的）：语言规则写在 `system_prompt`（system 角色）里，
  并且那是一份**英文的完整系统提示词**（人设 + 语言规则 + 文风）** —— 因为卡的
  `system_prompt` 在本项目里是**整体替换**，必须自带人设；尾注保留作强化。
  v1.0 / v1.1 两次失败的原因见 §29.10 与 §29.11；
- 带 `extensions.hne.state_schema`（`mood: text` / `affection: number`）+
  `initial_state` → 模型每轮会吐 `<state>`，正好用来验**译文是否保住了标签**
  （翻译系统提示词规则 2 要求 `<state>` / `<roll>` 原样保留，真模型最容易在这里出错）；
- 不带 VN 声明（这张卡是给翻译用的，别和 VN 复核混在一起）。

**配套的对照卡** `data/test-cards/translate-test-emily-pure-en.json`：与主卡几乎相同，
**唯一区别是不带 `state_schema`** —— 用于二分定位"中文的状态契约是否在把模型往中文带"
（主卡系统提示词 1239 字含 133 个中文字、纯英对照卡 964 字含 0 个）。用法见 §29.11。

导入方式：控制台 →「角色卡」→ 导入 JSON → 拖入文件。校验方式（临时脚本，已删）：
`parse_card_json` → `CharacterCardCreate` → `state_schema.resolve_schema` →
`prompt_builder.build_prompt` 全过（`system_prompt_source=card`、语言规则在 system、
尾注仍是最后一条消息、主卡带 `<state>` 契约且纯英卡系统提示词中文数 = 0）。

### 29.5 验证（串行跑完）

| 项 | 结果 | 与上一轮比 |
|---|---|---|
| `pytest -q` | **843 passed**（5:44） | +1（新增日文自动识别那条；没有删/放松任何既有断言） |
| `scripts/smoke_test.py` | **181 / 0** | +2（`input_lang` 已退休 / 默认目标就是简体中文） |
| `scripts/ui_probe.py` | **129 项检查，0 失败** | +1（只允许有一个语言控件 + 提示含"自动识别"）；控制台 1 条 404 是探针自己"删掉会话后再读"的预期请求 |
| `scripts/benchmark.py` | 退出码 0 | 不变（不涉及检索/状态逻辑） |
| `scripts/benchmark.py --from-db` | 退出码 0 | 不变 |

改动文件：`app/narrative/translate.py`、`app/schemas/narrative.py`、
`web/js/views/chat.js`、`web/js/views/cards.js`、`tests/test_translate.py`、
`scripts/smoke_test.py`、`scripts/ui_probe.py`、四份文档、新增一张测试卡 JSON。
**没有数据库迁移、没有新接口**；改了 `app/**` ⇒ **必须重启 uvicorn**（已重启后才跑的 smoke/探针）。

### 29.6 第十六轮（续）：翻译只留一个语言 + 源语言自动识别

用户接手试用翻译中间件后的原话：

> 在使用翻译插件的时候遇到不便，用户还需要自己切换语言。且默认居然是中文翻译成英文。
> 可我的目标对象大部分是中国人。我需要你修改默认是英文翻译成中文。其次…可不可以实现
> 自动识别语言翻译，用户只需要选择翻译成什么语言（默认简体中文）。

**根因不是默认值，而是两个语言框并排时的语义歧义。** 只读查了用户那个会话的落库设置：

```
session 7528 → {"direction": "reply", "target_lang": "英文", "input_lang": "简体中文"}
```

`direction=reply` 时翻译目标是 `target_lang=英文`，而那张卡（英文角色）本来就回英文
→ `looks_like(英文, 英文)=True` → **0 token 直接跳过**；输入侧没启用。于是"配好了却什么都不发生"。
两个框并排时用户读成"英文 → 中文"，而真实语义恰好相反（"把回复译成英文"+"把我的输入译成简体中文"）。

**改动**（用户拍板：彻底删掉输入侧语言；繁体也识别，除非目标本身就是繁体）：

| | 改前 | 改后 |
|---|---|---|
| 语言设置 | 输出侧 `target_lang` + 输入侧 `input_lang`（默认英文） | **只有一个 `target_lang`**（默认简体中文），两个方向共用 |
| 面板文案 | 「译文语言（输出侧）」+「译成哪种语言（输入侧）」 | 「翻译成什么语言」+ 提示"源语言自动识别" |
| 源语言 | 靠 `input_lang` 反向指定 | **本地自动识别**：假名 / 谚文 / 简繁 / 字母占比 |
| 界面控件 | `tr-target` + `tr-input-lang` | 只剩 `tr-target`（探针把"只许有一个语言控件"钉成断言） |

`input_lang` **退休**：`normalize_settings` 不再输出这个键，老会话 JSON 里残留的键被忽略、
用户下次保存即消失 —— **没有数据库迁移**。`app/schemas/narrative.py` 的 PATCH 模型同步删掉该字段。

### 29.7 顺带修掉的真 bug：源语言识别曾经是"假的"

旧判据（`looks_like`）：

```python
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")   # ← 假名、谚文也在里面
if any(key in target for key in ("中", "日", "韩")):
    return len(_CJK_RE.findall(sample)) / len(sample) >= 0.4
```

**日文（汉字+假名）与韩文（谚文）的占比轻松超过 40% ⇒ 被判成"已经是简体中文"** →
`used_model=false`、0 token、**静默不译**。也就是说"日文自动识别后翻成简体中文"这条需求，
只改 UI 不够 —— 判据本身是坏的，而且**没有任何报错**（界面显示已启用，只是该译的没译）。

改后的判据（全部只是"省钱的短路"，判不出来就老实译）：

| 目标 | 跳过（= 已经是目标语言）的条件 |
|---|---|
| 简体中文 | 无假名、无谚文、汉字占比 ≥40%、**且不含繁体专用字** |
| 繁體中文 | 无假名、无谚文、汉字占比 ≥40%、**且不含简体专用字** |
| 日文 | 出现假名 |
| 韩文 | 出现谚文 |
| 英文 | ASCII 字母占比 ≥70%（与旧版一致） |
| 其它 | 一律不跳过（老实译） |

简繁用**高频、且只在一边出现**的专用字表（`_TRAD_ONLY` / `_SIMP_ONLY`，各 ~150 字）判断，
并刻意让判错方向**不对称**：把简体判成繁体只是多花一次调用；把繁体判成简体才是漏译（用户看到繁体）。
已知边界（注释里也写了）：纯汉字、且只用了简繁同形字的日文仍可能被当成中文 ——
这类文本在自然语言里极罕见，且真判不出来时会落回"老实译"。

### 29.8 顺带修正：目标日文 / 韩文不再把中文误判成"已经是日文"

旧版 `("中","日","韩")` 共用同一条 CJK 判据 ⇒ 选目标=日文时**中文回复也会被判成
"已经是日文"**而跳过。现在目标日文要求"出现假名"、目标韩文要求"出现谚文"，
中文回复会老实译成日文 / 韩文。

### 29.9 本轮的测试加固（只增不减）

| 位置 | 改了什么 |
|---|---|
| `tests/test_translate.py` | ★ 新增 `test_japanese_reply_is_auto_detected_and_translated`（日文回复必须真的调模型翻译 —— 旧代码会跳过）+ 8 条 `looks_like` 断言（日/韩/繁/简/日文目标/韩文目标）+ 输入侧改成"同一目标语言"语义（含"中文输入 0 token 直发"）+ 面板断言 `input_lang` 已不在设置里 + 双向用例改成"一个目标同时管两边" |
| `scripts/smoke_test.py` | 新增 2 条（`input_lang` 已退休 / 默认目标就是简体中文）；输入侧用例改 `target_lang: 英文` |
| `scripts/ui_probe.py` | 新增 1 条（**只允许有一个语言控件** + 提示里必须出现"自动识别"）；控件清单去掉 `tr-input-lang` |

### 29.10 ★ 我判断错的地方：卡里写一句"用英文回复"压不住模型（用户真机反馈）

用户拿真模型（`deepseek-reasoner`）跟这张测试卡聊了几轮，反馈：

> 你写的这张卡一会中文回复一会英文回复的（不是翻译插件问题，我没动翻译，只是角色卡不给我回复外语）

**我在 §29.4 里写过"`system_prompt` 里写明 always reply in English → 模型必然用英文回复" ——
这句话是错的，被真机证伪了。** 根因在提示词装配，有两层：

1. **卡的 `custom system_prompt` 是"整体替换"，不是"追加"**（`prompt_builder.py:261-289`）：

   ```python
   custom = (getattr(card, "system_prompt", None) or "").strip()
   if custom:
       return f"{custom}\n\n{world_block}", "card"     # ← 人设字段不再拼进去
   ```

   我在卡里只写了 `"Always stay in character and always reply in English. Keep replies to a few
   sentences."` ⇒ **引擎内置的扮演规则和自动拼装的人设整份都没了**，整条系统提示词就剩这一句英文。
   没了"你正在扮演角色 X、不要以 AI 助手身份发言"的约束，模型自然**跟着用户的语言走**
   （用户用中文提问 → 多半回中文，偶尔按那一句英文回英文，于是"一会中文一会英文"）。
   ★ 顺带说明：这不影响状态协议 —— `render_contract` 是在**所有路径之后**才追加的
   （`prompt_builder.py:516-520`），所以 `<state>` 那部分没丢。

2. **语言这种"必须照做"的机械要求，位置比措辞重要得多。** 项目里已经写死过这条经验
   （状态契约、"输出语言"提示都刻意放在**最末尾**，见 §7 已知坑）。正确的位置是
   **尾注 `post_history_instructions`** —— `build_messages` 把它作为**最后一条消息**发出
   （`prompt_builder.py:321-325`），权重最高。

**修法（卡 v1.1）**：

| | v1.0（错） | v1.1（对） |
|---|---|---|
| 语言要求放哪 | `system_prompt`（整体替换掉内置提示词） | `post_history_instructions`（最后一条消息） |
| `system_prompt` | 那一句英文 | **留空** ⇒ 回到"自动拼装人设"（`source=assembled`，内置扮演规则回来） |
| 人设字段 | 只有 `description` 说英文 | `description` / `personality` 都写明 "English only"（不依赖尾注也能有点约束） |
| `mes_example` | 英文 | 英文（few-shot 范例，稳住语气） |

> 尾注里还加了一句「每次回复控制在 2~4 句」：用户实测单条回复 600~1000 输出 token，
> 而翻译一次要把这段文本整段当输入、再产出等量译文 ⇒ token 大致是回复本身的**两倍量级**
> （`translate.estimate_cost` 也是按"输入≈提示+原文、输出≈原文"估的），拿这种长度做翻译测试偏贵。

**验证方式（纯函数，不调真模型 —— 已跑，临时脚本已删）**：
`parse_card_json` → `CharacterCardCreate` → `resolve_system_prompt` → `build_messages`，
断言四件事：`system_prompt_source == "assembled"`（不再被卡替换）、拼出的提示词里
`「你正在扮演角色」` 回来了、**最后一条消息**里带 `英文（English）` 的语言要求、
`state_schema` 仍是 `source=card`（`mood/affection` 没丢）。四条全过。

**★ 但"模型真的稳定回英文"只能由真模型确认** —— 这次我不再替真模型下结论。
（`post_history_instructions` 也有一条边界：**绑了预设的会话由预设决定尾注位置**
（`prompt_builder.py:721-725`，尾注在预设里对应 `jailbreak` 块），这种会话里卡的尾注可能不生效。
用户那张会话 `prompt_preset_id = None`，所以尾注会生效。）

### 29.11 第二次真机反馈：**尾注也压不住** —— 语言规则必须回到 system 角色（卡 v1.2）

用户重新导入 v1.1（尾注版）后新建会话再试，反馈：

> 怎么还是回复中文？我是新导入的卡

**先证明"尾注到底有没有发出去"**（不猜，四步证据链）：

1. **DB 只读**：新卡 `id=14568`、`system_prompt` 为空、`post_history_instructions` 351 字符且含
   `English` / `2~4`；新会话 `id=7905`、`character_card_id=14568`、`prompt_preset_id=None`
   ⇒ 卡与全会话都用的是 v1.1，且不会被预设接管。
2. **追加位置**：`build_messages` 把尾注作为**最后一条消息**发出（`user` 角色 +
   `[系统指令 · 并非用户发言]` 头，`prompt_builder.py:321-325`）。
3. **裁剪层专门保住它**：`context_manager.py:299-304` 认出"以 `[系统指令` 结尾的尾注"、
   单独拎出来最后才丢，`final.append(tail)` 放回末尾 ⇒ 不会被 token 预算裁掉。
4. **适配器 1:1 全发**：`openai_compatible.py:163` 就是
   `"messages": [message.to_dict() for message in request.messages]`，不做任何过滤。
5. 顺手排除插件：用户两个插件都是**空壳**（`{"rules": []}` / `{"content": "", "position": "end"}`），
   没有注入任何语言规则（内置的「始终用中文回复」插件**没有**被启用）。

⇒ **尾注确实送到了模型面前，是 deepseek-flash 没照做。** 原因也清楚：这时 system 角色里是引擎用
**中文**拼装的人设 —— `assemble_persona_prompt` 的开头是「你正在扮演角色「X」。请始终保持…」，
中文框架 + 中文提问 + 中文历史三条合起来，把最后那条 `user` 角色的提醒压了过去。

> ★ 修正 §29.10 里那句"**位置比措辞重要**"：位置确实重要（它决定提示词内部的优先级），
> 但**"最后一条消息"不等于"模型一定照做"**；持续性的文风/语言约束属于 system 角色，
> 放在 system 里比放在尾注里硬得多。

**卡 v1.2 的做法：语言规则回到 system 角色，并写成英文的完整系统提示词。**
因为本项目里卡的 `system_prompt` 是**整体替换**（§29.10），所以它必须自带人设与文风：

| | v1.0 | v1.1 | **v1.2** |
|---|---|---|---|
| 语言规则位置 | card `system_prompt`（只有那一句） | 尾注 | **card `system_prompt`（英文完整提示词）** + 尾注强化 |
| `system_prompt` | 一句英文 | 留空 → 引擎中文拼装 | 英文：人设 + 语言规则（HIGHEST PRIORITY）+ 文风 |
| 真机结果 | 一会中文一会英文 | **仍然全中文** | 待真机验证 |

**另外准备了一张对照卡** `data/test-cards/translate-test-emily-pure-en.json`（内容几乎相同，
唯一区别：**不带 `state_schema`**）。理由：项目的「当前状态」块与状态输出契约
（`render_contract`）都是**中文**的，而且追加在系统提示词**最末尾** ——
纯函数实测：主卡的系统提示词 1239 字里**有 133 个中文字**（全来自这块），
纯英对照卡 964 字里**一个中文字都没有**。所以：

- 主卡回英文 ⇒ 问题解决，收工；
- 主卡仍中文、纯英卡回英文 ⇒ 结论是"**中文的状态契约在带偏模型**"，
  下一步就该考虑让状态契约也跟随输出语言（那属于 `app/**` 改动，要先问你）。

**验证方式（纯函数，不调真模型 —— 已跑，临时脚本已删）**：两张卡都断言
`system_prompt_source == "card"`、语言规则出现在 **system** 里、尾注仍是最后一条消息、
主卡带 `<state>` 契约且纯英卡系统提示词中文数 = 0。全部通过。

> ★★ 老实说：这已经是同一个问题的**第二次修正**。前两次我都替真模型下了结论
> （v1.0"必然回英文"、v1.1"位置最高所以有效"），都被真机打回。这次只声明
> **我改了什么、我证明了什么**（尾注送达、system 里有规则、状态契约不受影响），
> **"是否稳定回英文"由真机定**。

**顺带修的界面文案**（`web/js/views/cards.js`，两处 `hint` 都与实现不符/说不清）：

- 「自定义系统提示词」原来只说"留空则使用引擎自动拼装的那一份"，
  **没写"填了就整体替换、人设字段也不再拼进去"** —— 这正是我踩的坑，现在写明；
- 「尾注指令」补上"位置最靠后、权重最高，适合必须照做的硬要求（如输出语言）"，
  以及"绑了预设的会话由预设决定尾注位置，这里可能不生效"。

> 教训（可写进论文的"实现难点/工程判断"）：**"兼容酒馆"的决定（卡自带 system_prompt 优先）
> 会让一句很小的自定义提示词把整份内置规则挤掉**，而失败形态不是报错、是"模型行为变得不稳定"；
> 同类问题在本项目里出现过两次（"没绑预设的用户被当绑了预设处理，人设全被挤掉"，见
> `prompt_builder.py:414-419` 的注释）——**"替换 vs 追加"必须在界面文案里说清楚**。

### 29.12 第十六轮（续）：翻译中的提示 / 状态栏改版 / 原始状态块 / 翻译降思考

用户看完真机效果后提了两条体验问题（外加我顺手核到的一条花钱异常）：

> 1. 原文发出来之后，翻译过程没有任何提示，用户会觉得莫名其妙。请加一个正在翻译的提醒
> 2. 状态栏占用的空间有点大，导致对话很小，不好看。大部分状态栏其实都是放在模型回复的最下面，
>    而不是放在单独的一个框里（有些角色卡的状态栏很精致有美化比较复杂，你这个状态栏无法显示…

#### ① 翻译有提示了：SSE 新增 `translating` 事件

**问题定位**：`engine.py` 的流式生成器在最后一个 `delta` 之后、`done` 之前**同步**执行
`translate_reply()`（一次完整模型调用，实测好几秒）。这几秒里前端只能显示「生成中」，
用户看完外语原文就干等 —— 就是"莫名其妙"。

做法：
- `translate_mod.will_call_model(settings, direction, text)` —— **与 `run()` 的跳过判据逐条对齐**
  的纯本地预判（开关 / 方向 / 模式 / 非空 / 不超长 / 不是已目标语言）。
  ★ 为什么要预判：**提示了却没译**比不提示更让人困惑（用户会盯着一句永不消失的提示）。
- 生成器里只在它返回 True 时 `yield "translating", {direction, lang}`。
- 前端 `showTranslating()` 在气泡标题行插一个真实 DOM 节点（`.tr-hint`，
  文案「🌐 正在翻译回复成简体中文…」），`onMeta/onDone/onError` 三处都收掉；
  输入侧翻译跑在流开始之前，前端在发送后立刻挂同样的提示、第一个事件到达即收。
- CSS 里 `content: none` 掉重样的「生成中」（正文已经流完，那句会误导）。

#### ② 状态栏改版：从"顶部大框"到"回复下方一行"

- 位置：`#state-bar` 从页面顶部搬到 **`#messages` 末尾**（= 最新那条回复的下方）。
  `appendMessage` 里会把状态条重新挪到末尾，否则新气泡会插到它下面（位置就不对了）。
- 形态：默认**只占一行**（`label 值` 小 chip，超出省略），点「详情」展开原来那一整套渲染
  （血条 / 背包 / 任务 / 布尔），展开是**纯 DOM 类切换**、不重渲染
  （重渲染会 abort 当前 view signal —— 本项目的已知坑）。
- ★ 探针里凡是 `#messages .msg:last-child` / `.msg.assistant:last-of-type` 的选择器**全废了**
  （状态条也是 div、也在 `#messages` 里、还排在最后 ⇒ 选择器返回 null、`null.click()` 直接抛错，
  一次点击都没发生）。本轮把它们改成"取 `querySelectorAll` 的最后一个"，
  并把"状态块已从正文剥离"那条断言**收紧到只查 `.msg-body`**（原来查整个 `#messages`，
  而「看作者原格式」是**故意**展示原始块的）。

#### ③ 新增 `messages.state_raw_json`：留住模型输出的 `<state>` 原文

用户原话"有些角色卡的状态栏很精致有美化比较复杂，你这个状态栏无法显示" ——
本项目**不执行卡里的模板/HTML**（安全边界），状态是被按 `state_schema` 解析成字段的，
**原排版在解析那一步就没了**。折中方案（用户拍板选它）：留一份原文，界面给「看作者原格式」。

- `state.apply_reply_with_meta` 在每个返回分支上把 `raw`（`extract_state_block` 拿到的原文）
  塞进遥测 dict；`engine.save_assistant_reply` 把它 `pop` 出来**单独落一列**
  （遥测 JSON 里不带它 —— 漂移统计只认 counts/deviation，两份数据不能各说各话）。
- `MessageOut.state_raw` + `serialize_message` 暴露给前端；前端在状态条详情里折叠展示。
- 迁移：`scripts/migrate_db.py` 只加一列 `messages.state_raw_json TEXT NULL`（**已执行**）。

#### ④ 翻译降思考 + 花费口径修正（一次真机对账发现的）

用只读 SQL 核了用户那轮的账：**1618 字符的英文回复、生成用量 700 token，
翻译一次却花了 5291 token**（译文只 538 字）。原因：该模型**自带思考**（截图里回复也有
"思考 323"），而翻译调用复用了会话模型的参数；`estimate_cost` 只按原文字数粗估、
**完全不含思考 token** ⇒ 面板的「这一轮预估」严重偏低。

- `translate.run()` 现在传 `ChatRequest(reasoning_effort=...)`，**只在
  `ProviderConfig.effective_reasoning_support() is True` 时**才给 `ReasoningEffort.OFF`
  （OpenAI 兼容协议会映射成 `minimal`）。★ 为什么加这个闸门：统一层默认 `auto` = **完全不发**这个
  参数（很多网关不认识它、发了直接 400），翻译失败等于用户白等一场 —— 不值得为省这点钱冒险。
  用户那台 deepseek-flash 的探测结论是 `supported=0`（接受但不理会），所以对它**不生效**；
  他另一个 doubao 模型 `supported=1` ⇒ 指定它做翻译时才会真的降下来（面板里可单独指定）。
- 适配层对这个参数的说明（"OpenAI 兼容没有真正的关闭思考，已映射为 minimal"）记进日志与
  `outcome.extra`，不往每轮提醒里塞（否则每轮重复一句）。
- 面板文案改成「这一轮预估约 N token（**粗估，不含思考 token**）」。

#### 验证（冻结代码上串行跑完）

| 项 | 结果 | 变化原因 |
|---|---|---|
| `pytest -q` | **846 passed**（5:47） | +3：`will_call_model` 判据对齐 / 降思考的闸门 / `translating` 事件的位置与反向用例 |
| `scripts/smoke_test.py` | **184 / 0** | +3：`translating` 事件存在 / 在 `done` 之前 / 带目标语言 |
| `scripts/ui_probe.py` | **132 项检查，0 失败** | +3：状态条在消息区末尾 / 默认一行且点「详情」展开 / 「看作者原格式」有原文；另有 3 条**选择器**随 DOM 结构变化而修正（见上） |
| `scripts/benchmark.py` | 退出码 0 | — |
| `scripts/benchmark.py --from-db` | 退出码 0 | — |

数据库：新增 `messages.state_raw_json TEXT NULL`，**已跑 `scripts/migrate_db.py`（只加列）**。
改了 `app/**` ⇒ **必须重启 uvicorn**（已重启后才跑的 smoke/探针）。

### 29.14 第十六轮（三续）：逐条翻译按钮 + 深色主题根治

用户点单两条：① 每条消息（含开场白）加一个**手动翻译**入口（与现有翻译绑定、但不受开关限制，
适用于"忘了开"或"只想译这一条"）；② 内置深色主题"非常不好看"，优化或删除。
两条都按推荐方案做了（用户选定：所有消息都给按钮；主题**根治**而非删除）。

**① 逐条翻译（`POST /sessions/{id}/messages/{mid}/translate`）**

- `engine.translate_one(db, session, message)`：与自动翻译唯一的区别是**绕过总开关与模式**
  （点这一下就是用户的同意 —— 与「立即总结」同一个哲学）；其余全部复用：同一份设置
  （目标语言 / 指定模型）、同一套跳过判据、同一条降思考闸门、同一个 `translation_json` 形状。
- **已有译文不重译**：输入侧消息的 `translation.text` 是"用户原话"，重译会把它冲掉（数据损失）。
- 跳过/失败一律如实回一句话（"这条看起来已经是简体中文，不需要翻译。"），
  前端把它弹成提示 —— **点了没反应是最糟的体验**。
- 前端：`messageActions` 给所有角色都加了「🌐 翻译」，只更新那一条气泡
  （`dataset.altText` + 复用 `translationChipHTML`），**不整页重渲染**（会 abort view signal）。

**② 深色主题：不是审美问题，是硬编码颜色**

那个内置主题的 CSS 只重定义了 `:root` 变量 + `body`/`.topbar`，而 `styles.css` 里十几处
**写死的浅色**（`.btn.sec{background:#fff}`、`table.list th{#fafbfc}`、卡片/弹窗 `#fff`、
提示行 `#fafbfc`）换不掉 ⇒ 深色页面里冒出成片白块（用户原话"你注意那行白色的"）。

修法（根治）：新增语义变量 `--surface` / `--surface-2` 与四个提示文字色
（`--info-text/--warn-text/--danger-text/--ok-text`），把写死的颜色全部收进去 ——
**浅色值一个像素都没变**，主题插件补上这几个变量即可完整覆盖；新增组件也不会再漏。
守门人：探针在暗色主题下**采样计算若干关键组件的背景亮度**，任何一块偏亮都会报红
（这次就是这条断言能抓住的形态）。

验收：`pytest` **850 passed**（+1 逐条翻译）/ smoke **184 / 0** / 探针 **134 项 0 失败**（+2）
/ benchmark 与 `--from-db` 退出码 0。



### 29.13 第十六轮（再续）：降思考其实是"静默失效"的 + 逐轮真实用量落库 + 文档卫生

用户点单："先把 2、3、5 一起做了吧"（② 其它调用也降思考 / ③ token 估算精度对照实测 /
⑤ 文档卫生）。做 ② 的时候**当场抓到一个自己埋的 bug**，这一节的教训比功能本身更重要。

#### ★ 抓到的 bug：`cheap_reasoning_effort` 从来没有生效过

上一轮（§29.12 ④）我写了"翻译请求改为请求 `minimal` 思考"，实现是：

```python
params = getattr(adapter, "default_params", None)
support = params.effective_reasoning_support()   # ← 这个方法在 GenerationParams 上**不存在**
... except Exception: support = None             # ← 被这里吞掉，永远返回"不覆盖"
```

**三个事实叠在一起，构成了一个完美的静默失效**：

1. 探测结论（`reasoning_effort_supported` / `..._probed_model`）挂在 **`ProviderConfig`** 上；
2. 适配器只带走 `default_params`，而那是 **`GenerationParams`** —— 它**没有**任何探测字段
   （`GenerationParams(reasoning_effort_supported=True)` 这种写法会被 pydantic 静默忽略！）；
3. 我那个 `except Exception` 把 `AttributeError` 吃掉，函数永远返回 `None`（= 不覆盖）。

⇒ "把翻译/总结的思考降到最小"**一次都没发出去过**，而**单测是绿的** ——
因为我在测试里伪造了 `types.SimpleNamespace(effective_reasoning_support=lambda: support)`，
一个**真实代码里不存在的 API**。测试守的是幻觉。

**修法（三处）**：

| 位置 | 改动 |
|---|---|
| `app/llm/base.py` | 声明 `self.reasoning_support: bool \| None = None`（结论跟着适配器走） |
| `app/llm/factory.py` | `create_provider_from_config` 里 `adapter.reasoning_support = config.effective_reasoning_support()` |
| `app/llm/failover.py` / `whole_reply.py` | 包装层透传它（否则套一层就又丢了） |
| `app/llm/params.py` | `cheap_reasoning_effort` 只读 `adapter.reasoning_support`，**去掉 try/except**：接口变了就该炸出来 |
| 测试 | 替身改成只暴露**真实存在**的属性；新增守门测试 `test_probe_conclusion_travels_from_config_to_adapter`（未探测/不支持/结论过期 ⇒ 必须是 None） |

> ★ 教训（论文/答辩都能讲）：`except Exception` 包住一次属性访问，等于把"接口用错了"
> 变成一个**无声的 no-op**；而"用假 API 写的测试"会把这种 bug 保护得很好。
> 这与本项目红线"拒绝静默降级"是同一件事的两面 —— 只不过这次降级的是**代码质量**。

#### ② 剧情总结也走同一条闸门

`summary.py` 的总结调用原来就是 `ChatRequest(messages=prompt)`（继承 provider 默认思考）。
现在传 `cheap_reasoning_effort(adapter)` —— 与翻译共用同一个函数，
**探测结论为 True 才发**（`summary` 是机械任务，思考纯烧钱）。

#### ③ token 估算精度对照实测：新增 `scripts/token_accuracy.py`（只读，0 花费）

论文里一直写着"**未做过**估算值 vs 厂商 `prompt_tokens` 的对照实测"（`docs/handoff.md:1310`）。
根因是**数据没落库**：`messages.token_count` 只有 completion 那一半、
`sessions.total_tokens` 只有累计总和 ⇒ 事后无法对照。

- 新增 `messages.usage_json`：每轮存厂商的 `prompt/completion/reasoning/total` **加上**
  我们发出去之前对整段输入的估算（`usage_json = _usage_json(usage, turn.context.estimated_tokens)`，
  三个落库入口都传）。**迁移已执行（只加列）**。
- 新脚本 `scripts/token_accuracy.py`：只读回放，打印逐轮对照表 + 比值均值/中位/P90 +
  **低估占比**（估算 < 真实 ⇒ 预算可能被突破）+ 思考 token 占输出的比例；
  `--json` 另存 `data/token_accuracy_report.json`。
  ★ 样本只有"本轮起记录过的轮次"，脚本会如实写出 N（现在库里还是 0 —— 再聊几轮就有数据）。

#### ⑤ 文档卫生

`docs/paper-materials.md` 第 9 节那份"数据核对清单"抄的是很旧的快照（593 / 142 / 69），
与当时实际值差了好几轮 —— 已更新为当前值并加一句"**每次都要重跑再填**"，
另外补上 benchmark 与 token_accuracy 两条。

#### 验证

| 项 | 结果 |
|---|---|
| `pytest -q` | **849 passed**（+3：结论到达适配器的守门测试 / 总结降思考（双向）/ 逐轮用量落库） |
| `scripts/smoke_test.py` | **184 / 0**（服务已重启；本轮无新增冒烟项） |
| `scripts/ui_probe.py` | **132 项 0 失败**（前端本轮没动） |
| `scripts/benchmark.py` 与 `--from-db` | 退出码 0 |
| `scripts/token_accuracy.py` | 退出码 0（当前样本 0，如实说明"第十六轮起才开始记录"） |




### 29.15 第十六轮（四续）：跨页串台的删除弹窗 + 角色卡页那块白（2026-10-02）

#### ① 用户复现的 bug：在插件页点「删除」弹**两个**窗，第二个还报 404

用户原话："删插件弹了两个窗口"、"提示词预设不存在"。

**根因（代码级，不是猜的）**：`web/js/views/presets.js` 里那个
`nextController(current)` 只做两件事 —— abort 上一批、`return new AbortController()`。
它**丢掉了与路由信号的连接**：

```js
// 改之前
function nextController(current) { current?.abort(); return new AbortController(); }
// 调用处：listController = nextController(listController);
const signal = listController.signal;   // ← 这个信号谁都不认识它
withSignal(root, 'click', …);           // ← 挂到**常驻的 #view** 上
```

别的视图能活下来靠的是"切页时 `routeAbort.abort()` 顺着父子关系摘掉"，
而这个新控制器不在那条链上 —— **它永远不会被 abort**。
于是预设页的委托监听器在离开预设页之后依然活着，而
**预设页与插件页的卡片都是 `.cc`、删除按钮都叫 `data-act="del"`**，
插件页的点击被它接走：再弹一个"删除这套预设？"，再拿**插件的 id** 去
`DELETE /prompt-presets/{id}` → 404「提示词预设不存在」。

**为什么老断言（10.6「弹窗不能叠加」）是绿的**：它只在本页反复点击。
这条 bug 必须"先访问预设页、再切到插件页"才出现 —— 断言没走那条路。

**改法（三层，任一层都能挡住，但不留"只有一层"的侥幸）**：

1. `nextController(current, parent)` 增加 `parent`，把新控制器接到视图信号上
   （`listController` 与 `detailController` 两处调用都传了 `activeSignal?.signal`）；
2. **与信号无关的归属校验**：五个列表容器各带 `data-view="<页面名>"`，
   处理函数开头 `if (!btn.closest('[data-view="…"]')) return;`
   —— 认不出归属的按钮一律放行给真正的主人（透明背景/品牌色之外，这类
   "跨页串台"从此在结构上不可能发生）；
3. 探针新增**走用户那条路**的断言：预设页 → 插件页 → 点「删除」，
   必须**恰好 1 个弹窗且标题是「删除插件」**（旧 bug 下第二个窗的标题是
   「删除这套预设？」）。实测 `{"弹窗数": 1, "标题": "删除插件"}`。

顺带修掉一个同类隐患：`books.js` 的 `bindBookActions(root, signal)` 用的是**函数参数**
里的 signal，而不是同函数里已经算好的 `bindSignal`（两者在"调用方传旧信号"时不等价），
已改成 `bindSignal`。

#### ② 角色卡页那块白（第一次定案**错了**，见 ⑧ —— 先看结论再看过程）

> ★ **结论先行**：用户看到的那块白是 **`.cc-greeting`（开场白预览框）的浅色渐变**，
> 不是 iframe。② 修的是"确实也写死了白底"的另一处（真 bug，但不是他说的那块）。
> **我在这里犯的错**：把"我量到的一个写死白底"当成了"用户说的那块白"，
> 而没有让用户先确认位置 —— 记在下面，保留原样，别把这段美化掉。

角色卡的 HTML 开场白渲染在 `srcdoc` iframe 里。iframe 是一份**独立文档**，
父页面的 CSS 变量不会继承进去 —— 而那段文档头里写死了 `body{background:#fff;color:#1f2328}`，
所以深色主题下这里**确实**会是一块白。

修法：把常量 `_RICH_DOC_HEAD` 改成函数 `richDocHead()`，渲染时
`getComputedStyle(document.documentElement)` 读当前主题变量注入
（`--surface-2` / `--text` / `--border` / `--surface-3` / `--text-dim`），
并带上浅色兜底值。

**这一条有实测证据**（探针新增）：深色主题下打开那张 HTML 开场白的卡，
`iframe 底 = rgb(22, 28, 38)`（亮度 27）、`正文色 = rgb(230, 234, 240)`。

#### ③ 老主题缺变量：引擎补全（用户不用删了重加）

`--surface / --surface-2 / --surface-3 / --panel` 是**后加**的语义变量，
而 CSS 插件的内容是用户点「添加」时**拷进数据库**的：目录里那份升级了，
用户库里那份不会自动跟着变 → "深色主题下弹窗/卡片又是一块白"。

`theme_css()` 现在会在拼完用户 CSS 后补一段**只补缺**的映射
（`_theme_shim`）：

| 用户主题里有什么 | 引擎补什么 |
|---|---|
| 只有 `--panel`（更老） | `--surface:var(--panel)` → 再链出 `--surface-2/3` |
| 有 `--surface` 没 `--panel` | `--panel:var(--surface)` |
| 缺 `--surface-2/3` | `--surface-2:var(--surface)`、`--surface-3:var(--surface-2)` |
| 两边都没有（只改 `--brand` 的普通主题） | 什么都不做 |

浅色默认主题一个像素都没变；用户自己写过的变量一律不覆盖。

> ★ **诚实说明（别把两件事混起来）**：查库确认，用户**当前的**那条主题
> （id 2359「深色主题（Dark Lite 风格）」，他前后删掉重加过）**所有变量都写了**
> （含 `--surface`/`--surface-2`/`--surface-3`/`--panel` 与四个 `--*-soft`），
> 所以他看到的那块白**不是**缺变量造成的。③ 是**防御性**修复：机制上成立
> （插件内容是添加时拷贝的，目录升级不回头改），而且它让"以后再加变量"不再需要用户动手。
> ③ 是**防御性**修复：机制上成立（插件内容是添加时拷贝的，目录升级不回头改），
> 用户下一轮如果只写 `--surface` 就会遇到；而且它让"以后再加变量"不再需要用户动手。

#### ④ 探针的采样方式也修了（原来会漏也会误报）

老采样只比"`backgroundColor` 的亮度 > 140"，**三个**坑都踩过：

* **透明背景漏检**：`background:transparent` 的块白来自父级或**子文档**，
  只比它自己的背景色等于当它不存在 —— ②里的 iframe 就是这么漏掉的；
  现在把透明/缺失的元素**单独列出来报告**（`跳过` 字段），不再假装测过。
* **★ 渐变漏检（第三次才发现的，就是用户报的那块白）**：渐变画在
  `background-image` 上，`backgroundColor` 读出来是**透明** ——
  `.cc-greeting` 的 `linear-gradient(180deg, #f7f9fc, #f2f5fa)` 因此被当成透明块跳过。
  现在采样器会**解析 `background-image` 里的所有色标**，取最亮的那个参与判断
  （输出带 `grad` 前缀，报告里一眼能看出它是渐变）。
  ★ 而且当时采样清单里**根本没有** `.cc-greeting` 这个选择器 ——
  "没测到"和"通过"长得一模一样，这才是最危险的。
* **品牌色误报**：蓝色主按钮 `rgb(124,156,255)` 亮度 158 > 140，
  于是断言会去骂一个本来就应该亮的设计色；现在按
  "与 `--brand/--brand-dark/--ok/--warn/--danger` 相同就跳过"放行，
  **而不是按亮度放行**。

另外采样点从"对话页的 6 个选择器"扩到**角色卡页 + 已打开的详情弹窗**
（用户报白块的那一页），并把 `.cc-greeting` 列进**必采清单**：
`.cc`/`.cc-greeting`/`.modal` 缺任何一个，这条断言直接判失败。

#### ⑤ 换肤"当场生效"也进了断言

启停 CSS 插件后换肤走的是 `applyPluginTheme()`（重新取 `/plugins/theme.css`
替换 `<style>` 内容，见 `plugins.js:52`），原本就实现了，但**没人守**。
新增断言：同一个 `document` 内勾掉 → `--brand` 立刻变回 `#3b5bdb`；
再勾上 → 立刻变回 `#7c3aed`；并用 `window.__hne_probe_doc === document`
证明**中途没有重新加载文档**（防的是"其实偷偷刷新了才生效"）。

#### ⑥ 静态回归测试（五条，防"下次又忘"）

| 测试 | 守什么 |
|---|---|
| `test_view_delegated_actions_are_scoped_to_their_own_view` | 五个视图都必须有 `data-view` 容器 + 归属校验（老测试只数 `{ signal }` 的个数，所以漏了这次的 bug） |
| `test_presets_view_controllers_are_chained_to_the_route_signal` | `nextController(` 必须带第二个参数（视图信号） |
| `test_rich_iframe_document_follows_the_theme` | iframe 文档头必须是 `richDocHead()` 动态生成，且里面不许再出现写死的白底 |
| `test_card_greeting_preview_uses_theme_variables` | `.cc-greeting` 必须走 `--surface*` / `--text*`，且里面一个写死颜色都不许有 |
| `test_no_hardcoded_light_backgrounds_in_stylesheet` | **通用守门人**：`:root` 之外的 `background*` 声明里，凡是**亮于 200 的十六进制颜色**或**高不透明度的白**，一律判失败（`var(...)` 与低透明度提亮叠加放行） |

> 这两条新测试都**验过牙齿**：把 `.cc-greeting` 改回旧的写死渐变，两条**同时变红**；
> 改回来立刻恢复绿。（"加了断言"和"断言有用"是两件事。）

#### ⑦ 顺手取到的第一组真实用量对照（论文可直接引用）

跑 `scripts/token_accuracy.py`（**只读、0 花费**）时库里已经有样本了 ——
这是"估算口径准不准"第一次有真实数据（n=3，用户真机跑的）：

| 指标 | 值 |
|---|---|
| 估算 / 厂商 `prompt_tokens`（均值 / 中位） | **1.185 / 1.172**（区间 1.143 ~ 1.241） |
| 低估的样本 | **0 / 3**（估算一律偏高，没有"报少了"的情况） |
| 思考 token / 输出 token | **1416 / 2641 = 53.6%** |

> 论文口径：本地估算**系统性高估约 18%**（保守方向，符合"宁可早裁剪"的设计意图），
> 而**超过一半的输出 token 花在思考上** —— 这正是"翻译与总结要降思考"那条改动的量化依据。
> ★ 样本只有 3 条，只能写成"初步观测"，**不能**写成结论（这句务必照抄进论文）。

#### ⑧ 用户复测后仍看到的那块白：`.cc-greeting`（②的第一次定案错了）

用户刷新后回报："**深色主题下角色卡的白还是在**"。这次先看图，再查代码：

角色卡列表里每张卡的**开场白预览框**：

```css
/* 改之前 */
.cc-greeting {
  background: linear-gradient(180deg, #f7f9fc, #f2f5fa);  /* ← 写死的浅色渐变 */
  color: #4a5568;                                          /* ← 写死的深灰字 */
}
```

深色主题下它就是每张卡上一整块浅色框 —— **正是用户说的那块白**。改成：

```css
background: linear-gradient(180deg, var(--surface-3), var(--surface-2));
color: var(--text-dim);
```

顺带把 `.cc-avatar` 的 `box-shadow: inset 0 0 0 3px #fff`（头像的白色内圈）
改成 `var(--surface)`，深色下不再是一圈白环。

**为什么前两轮都没抓到它**：见 ④ 的第二条 —— 渐变在 `background-image` 上，
`backgroundColor` 是透明的；而且它当时**不在采样清单里**。
两个原因叠在一起，"没测到"被报告成了"通过"。

**我犯的错（写进论文"实现难点"比成功经验更有价值）**：
用户说"角色卡页有一块白"，我查到 iframe 里确实写死了 `#fff`，
就把它当成**用户的**那块白并宣布"定案" —— 其实用户看到的是渐变框。
**正确顺序应该是先让用户确认位置（截图/圈一下），再宣布定案**；
"我找到了一个真的 bug"不等于"我找到了你说的那个 bug"。

#### ⑨ 收尾清理：JS 内联浅色 + 控制台里那条"会话 404"

用户说"现在正常了"，随后问"还有什么没做完"。查了两处**同类残留**并修掉：

1. **JS 内联写死的浅色**：拖拽上传区"回到静止态"时 `zone.style.background = '#fafbfc'`
   （`ui.js` 一处、`cards.js` 两处）。同一个函数里 `hot()` 用的是 `var(--brand-soft)`，
   只有 `idle()` 漏了 —— **"一半走变量、一半写死"**。
   后果：拖过一次文件后，深色主题下上传框卡在浅色。
   顺手把静态守门人**扩到 JS**（`test_no_hardcoded_light_inline_styles_in_js`）：
   扫 `web/js/**/*.js` 里 `background*` 的十六进制字面量，亮于 200 就判失败
   （先剥掉注释 —— 注释里为了说明这个坑会**引用**旧写法，不剥就会把"记录 bug 的文档"当成 bug）。
   ★ 已验牙齿：把其中一处改回 `'#fafbfc'`，测试立刻变红。
2. **控制台那条"会话 404"**：会话在**背后**被删掉后（删角色卡勾了"连同对话记录"、
   另一个标签页删的、探针自己删的），前端还会白打一次注定 404 的请求
   （两条路径：进对话页 / 点刷新），浏览器控制台因此留一条红字 ——
   用户看到会以为前端坏了，而界面其实早就正确地走了"会话不存在"的分支。
   修法：**列表本来就已经取回来了，先查再请求**（`sessionIsGone`），
   并且只有"列表确实能代表这个会话"时才敢下结论（列表是按活跃/已归档过滤的，
   `state.detail.status` 与当前 tab 不一致时一律退回直接请求）。
   现在探针整轮 **控制台报错 0 条**，并新增第 99 条总检守住它。
3. **顺带修掉一个真 bug（偶发变红暴露的）**：两个"新建会话"弹窗在创建成功后都有
   `if (signal?.aborted) return;` —— 弹窗开着时视图若被重渲染，旧 signal 失效，
   于是**会话已经建好、界面却毫无反应**（用户看到"提示说创建成功"然后什么都没有）。
   改成一律用**当前**的 `activeSignal` 做后续刷新与打开。
   探针侧同时补一条"等面板落定再点"（`#chat-title` 出现 + 0.8s），
   消掉"在途的旧会话渲染把新面板覆盖回去"这个时序（这条偶发过两次，
   第一次我误判成自己的预检造成的，做了"关掉预检"的对照实验才排除）。
4. **提示框/失败气泡的边框色（我上次只报了 3 条，其实是 5 条）**：
   `.alert.warn` / `.alert.danger` / `.alert.info` / `.alert.ok` 与 `.msg.failed .msg-body`
   的 `border-color` 写死成四个浅色（`#f0d9a0` / `#f3c2bb` / `#c8d3f7` / `#b7e3cd` / `#f3c0b8`）。
   背景早就是 `--*-soft` 变量、能跟着主题变，**只有边框留在浅色** ——
   深色主题下每个提示框都镶了一圈**亮边**（横幅、失败回复都中招）。
   收进 `--{warn,danger,info,ok}-border`（`:root` 保持原来的浅色值，浅色主题一字未改），
   并由 `_theme_shim` 给老主题按"该主题自己的文字色 × 柔和底"的**中间调**补上
   （`color-mix`，不支持的浏览器退回文字色）—— 补出来是深色调，不是亮线。
   ★ 我上次漏报 info/ok 两条，因为只 grep 了 warn/danger/failed 三个词：
   **"我找到的那几条"不等于"全部"**；这次按 `border-color` 全量扫了一遍。
   ★ **守门人要单独写**：已有的两条颜色守门人扫的是 `background*`，探针采样也只量背景色
   —— **只检查背景的守门人天然看不见边框**。新增
   `test_alert_borders_use_theme_variables` 钉住这五条规则（已验牙齿：把 warn 改回
   `#f0d9a0` 立刻变红），另加两条插件测试守住引擎补全（只补缺、不覆盖用户写过的）。
5. **目录 ✦ 头像的字色（"一半走变量"的第三例）**：
   `plugins.js` 里 `style="background:var(--brand-soft);color:#6b46c1"` ——
   底色跟着主题变、字色不变，深色主题下深紫压在深藏青上**看不清**
   （不是白块，是"读不出"）。收进 `--catalog-accent`（`:root` 保持紫色，浅色一字未改），
   引擎给没写这一项的主题补 `var(--brand)`（与插件卡片的头像一致）。
   ★ 新增一条**通用守门人** `test_half_variablized_colors_are_rejected`：
   "底色是 `var(...)`、而同一处的**文字/边框色**写死深色（亮度 < 150）" 一律判失败。
   规则收得很窄，避开两类正当写法：品牌底上的白字（`background:var(--brand);color:#fff`）、
   以及代码块那种"永远深色"的部件（`background:#1f2933; color:#e6edf3`）。
   第一版规则写宽了、把这两类都误报，跑一次就收敛了 —— **窄规则才有用**。
   已验牙齿：把那一行改回 `#6b46c1` 立刻变红。

#### 验证（冻结代码上串行跑完）

| 项 | 结果 |
|---|---|
| `pytest -q` | **865 passed**（+15：四条静态回归 + 七条主题补全 + 四条颜色守门） |
| `scripts/smoke_test.py` | **184 / 0** |
| `scripts/ui_probe.py` | **140 项 0 失败，控制台报错 0 条**（"控制台 1 条 404"已消除并加了总检；采样器升级为渐变感知） |
| `scripts/benchmark.py` 与 `--from-db` | 退出码 0 |
| `scripts/token_accuracy.py` | 退出码 0（首次有样本，见 ⑦） |
| 人工复核项 | 无新增 |




## 30. 第十七轮：云梦枢的"脸"——应用图标 + 星云暗涌主题（2026-10-03）

### 30.1 系统定名与图标

用户确认系统名 **云梦枢（YunMeng Hub）**："云"=云端 AI，"梦"=沉浸式叙事，"枢"=系统中枢（谐音"书"）。

图标是用户给的设计稿（1024×1024 PNG）：紫云 + 翻开的书 + 金色罗盘星 + 环绕的星体。

**第一件事不是贴图，而是处理底色**：设计稿是**白底**的，直接放进深色顶栏就是一块白方块。
`scripts/make_icon_assets.ps1` 用**四角洪水填充**把它变透明 ——

> ★ 为什么不能"全图白色→透明"：图标里有一本**翻开的书**，书页本身就是白的，
> 那样会在书页上打出一堆窟窿。洪水填充只吃"与图像边界连通的近白"，
> 书页的白完整保留（实测：256px 版本 47% 像素为透明，四角全透明，中心 alpha=252）。

脚本同时产出运行时要用的两档（64/256 → `web/img/`）与打包/归档用的一份
（1024 与白底原稿 → `assets/icon/`）；**2026-10-03 整理过一轮**，详见 §30.7，
1024 档留给后面的 Electron 打包。踩到两个 Windows 上的坑（都写进脚本注释）：
`.ps1` 必须带 **UTF-8 BOM**（否则 PowerShell 5.1 按 ANSI 读，中文全乱码）；
`Add-Type` 必须显式 `-ReferencedAssemblies System.Drawing`（否则报"找不到 Drawing2D"）。

界面接线：顶栏 `.brand-mark`（原来那个蓝底 HNE 三个字母）换成
`<img class="brand-logo">` + 文字「云梦枢」+ 副标「交互式叙事引擎」；登录页同样加了图标与名字；
favicon / apple-touch-icon 换成图标（带 `?v=__WEB_VERSION__`，与 importmap 同一套缓存失效机制）；
`document.title` 与 `<title>` 也改成「… · 云梦枢」。

### 30.2 「云梦枢 · 星云暗涌」：内置 CSS 主题

设计稿（用户拿另一份 AI 方案来问"行不行"）的核心诉求：**沉浸但不打扰阅读**。
我按这个原则把它落成一条**声明式 CSS 主题插件**（内置目录 `yunmeng_nebula`），
而不是直接改基础样式 —— 这样它能一键开关、随时对比、完全可逆：

| 设计点 | 实现 | 为什么这么做 |
|---|---|---|
| 深空底 + 星云 | `body` 两层 `radial-gradient`（左上青蓝、右下紫，各 ~10% 透明度）+ `background-attachment:fixed` | 装饰只在**外围**，正文区对比度不受影响 |
| 毛玻璃面板 | `.panel/.cc/.modal/.session-item/.composer` 等 `backdrop-filter:blur(14px)` + 半透明 `--surface` | 星云能"透出来一点点"，而不是死板纯色块 |
| 文字不刺眼 | `--text:#e6eaf0`（不用纯白）、`--text-dim/#a3adbb` | 白字压深底久看会累（设计稿里专门提了这条） |
| 品牌色只走细线 | 顶栏底部一条 1px 渐变光带 + 选中项细下划线 + `inset` 光条 | **不做大面积色块**，否则会抢走对文字的注意力 |
| 不做动效 | 这一版刻意不加过渡/脉冲 | 用户明确要"不耀眼" |

**关键前提是前几轮把颜色都收进了变量**（`--surface*`/`--*-border`/`--catalog-accent`…），
所以这条主题 6047 字符里绝大多数只是变量赋值，规则只补了"变量表达不了"的部分
（渐变、毛玻璃、光带、滚动条）。新增的测试会**盯着这件事**：

* `test_catalog_yunmeng_theme_defines_every_variable`：从 `styles.css` 的 `:root` 里
  抓出所有"值是颜色"的变量，逐个检查主题有没有定义 —— 少一个，组件就会退回**浅色默认值**
  （也就是老账"深色下那块白"）。★ 只要求颜色变量，不要求 `--radius`/`--mono` 这类结构量：
  第一版把它们也算进去了，是条没人理的测试。★ 这条测试**当场就抓到了 4 个漏项**
  （`--mono/--radius/--radius-sm/--reqlog-w`）—— 正是它逼我把判据收窄的。
* `test_catalog_yunmeng_theme_composes_with_the_engine`：装上之后引擎**不该再补任何东西**
  （补了说明主题没给全），且 `sanitize_css` 不会削掉 `backdrop-filter` 与渐变。

### 30.2b 第二版：从"科技蓝"改成"量子紫 + 极光青"

第一版做完后用户的反馈很具体（他拿另一份 AI 评审来提意见）：**"方向对，但太像标准的深蓝后台管理系统"**，
缺"云梦"的梦幻感。五条修改意见我逐条落地：

| 用户意见 | 落地 |
|---|---|
| 亮蓝太"科技后台" | 主色 `--brand:#a78bfa`（**量子紫**）；新增主题内变量 `--aurora:#00e5ff`（**极光青**） |
| 颜色要有分工 | **量子紫 = 状态**（选中、"我"的声音）；**极光青 = 交互**（悬停、聚焦、AI 侧微光） |
| 背景太平 | 星云改成**左上紫 / 右下青**，透明度压到 8% 上下（原来 10% 且都是蓝紫） |
| 气泡缺区分度 | 玩家气泡 `border:1px solid rgba(167,139,250,.38)`（紫）+ 淡紫底；AI 气泡 `rgba(0,229,255,.18)`（青）**加一圈极弱青色发光** |
| 头像突兀、选中不够"梦" | 会话头像改**圆形** + 深空径向渐变 + 紫色细环；选中项 = `rgba(167,139,250,.15)` 底 + 最左 **3px 紫色实线** |
| "进行中/已归档"是实心蓝块 | 改成 `rgba(167,139,250,.20)` 半透明紫 + 底部细紫线（不再是实心大色块） |

★ `--aurora` **刻意只写在这个主题里**、不进 `styles.css` 的 `:root`：
一旦进 `:root`，我的"变量给全"测试就会要求**所有**主题定义它，
而引擎的补缺逻辑又不知道该怎么补 —— 那是给别的主题挖坑。

#### ★ 一条新断言：**覆盖到底有没有赢，靠量不靠看**

改完之后我盯着截图怀疑"'进行中'那块看着比 20% 透明紫更实" —— 于是没有靠眼睛下结论，
而是加了一条断言直接读**计算样式**：

```
tabBg      = rgba(167, 139, 250, 0.2)      ← 正是我要的半透明紫
itemBg     = rgba(167, 139, 250, 0.15)
itemShadow = rgb(167, 139, 250) 3px 0 0 0 inset
avatarRadius = 50%
userBorder = rgba(167, 139, 250, 0.38)     aiBorder = rgba(0, 229, 255, 0.18)
```

**结论：覆盖一直是对的，是我的眼睛错了。** 这条断言同时守住两类事故：
①选择器优先级/注入顺序哪天变了，选中态会悄悄退回"实心大色块"；
②有人把主题里这几条规则删了。写的时候自己踩了一次：
计算样式里 `#a78bfa` 会被**归一化成 `rgb(167, 139, 250)`**，
第一版判据写成十六进制，于是断言假红了一次 —— 判据要用 rgb 三通道。

### 30.2c 第三版：四笔"星辉润色"（用户第二轮反馈）

用户这一轮的原话是"底色、布局已经完美，只差'云梦枢'的灵魂"，四条都很克制：

| 用户要求 | 落地 | 我加的判断 |
|---|---|---|
| 导航栏/标题加"星辉"；选中下划线改成**由中间向两边渐变的极光青线** | `.brand-text` 叠两层极淡紫/青 text-shadow；图标 drop-shadow 同理；选中项下划线用 `background-image: linear-gradient(90deg,transparent,rgba(0,229,255,.95),transparent)` + `background-position:bottom center` + `background-size:92% 2px` | 下划线用**背景层**而不是伪元素：`.nav-item` 是按钮，加 `::after` 要动 `position`，而背景层零结构改动 |
| 卡片"魔法卡牌"悬停：微微上浮 + 边框泛紫光 | `.cc:hover{transform:translateY(-3px)}` + `0 0 0 1px rgba(167,139,250,.28)` 的贴边泛光 + 更大的紫投影 | 上浮**只给 3px**：幅度大了像廉价动效；1px 贴边泛光才有"牌在灯光下"的质感 |
| 卡片左上角方头像→圆形 + 梦幻深空渐变 | `.cc-avatar{border-radius:50%}` + 径向渐变 + 内圈暗环 + 极淡紫晕 | 与左侧会话头像**用同一套语言**，两处不再各行其是 |
| 状态栏那条"还没有状态…"加微弱青色外发光 | `.state-bar`/`.state-bar.empty` 加 `box-shadow:0 0 10~12px rgba(0,229,255,.15~.18)` | 它是本项目的创新点（状态探针），值得一眼看到；但**不做闪烁** —— 闪烁会把人从正文里拽出来 |
| 背景不要死黑，要有"深夜里的许多星星" | `body` 叠 **14 层点状径向渐变**（1.1~1.6px，白/浅紫/极光青三色）+ 两层星云；三层 tile 尺寸（260/380/520px）错开，看不出重复规律 | 星点全部放在**最底层**（`body` 的 background），不上浮成 DOM/伪元素 —— 不然会盖在文字上，正文明暗受影响 |

★ 这一版踩到一个 CSS 细节并写进注释：**必须用长写法**（`background-image` / `-size` / `-repeat` 分开写）。
用 `background` 简写会把它自己的 `background-size` / `background-repeat` 一起重置，
星点层就没法各自平铺了（`background-size` 是逗号列表，必须与层数一一对应；本次 16 层）。

#### ★ 视觉参数不能凭感觉定：星点是**量出来**的

第一版星点我"觉得差不多"，于是量了一下 `docs/nebula-cards-screenshot.png` 的背景区域：

| 版本 | 背景均值 | 最亮像素 | 说明 |
|---|---|---|---|
| 第一版（alpha .24~.50） | 16.3 | **31.5** | 整块区域只有 1 个像素 > 30 —— 等于**看不见** |
| 第二版（alpha .34~.80） | 15.0 | **56.1** | 亮点（>25）占 0.03%：约每 4800px² 一颗，峰值约为文字（~230）的 1/4 |

结论：**"淡雅"和"看不见"之间需要数字**。现在的参数是"能看出是星空，但峰值只有文字的四分之一"。

#### ★ 采样器的第四次修正：点状高光 ≠ 浅色块

星点一加，采样器立刻把 `body` 判成"浅色块 207" —— 它把所有渐变色标混在一起取最大值，
于是"14 层星点里最亮的那颗白点"被当成了"一整块 80% 白"。

修法：**按层切分，并按声明尺寸分流**：

* 用**括号配对**切出每一个 `radial-gradient(...)` / `linear-gradient(...)` 层
  （不能按逗号 split —— `rgba(...)` 里也有逗号）；
* 声明尺寸 ≤ 4px 的径向渐变判定为**点状高光**：不参与"亮块"判定，
  但**单独报告**（`点状高光` 字段），并且单独有一条断言守它（合成峰值 40~210）；
* 其余层（星云、面板渐变）照旧参与"亮块"判定。

> 四次修正的完整清单（透明 → 渐变 → 半透明 → 点状高光）说明同一件事：
> **"看起来亮不亮"是个语义问题，不是一个字段能回答的**。
> 每加一种"画法"，断言就要跟着长一格；而**每次都要把"我跳过了什么、为什么跳过"打印出来**，
> 否则"没测到"永远长得像"通过"。

### 30.2e 第四版：星点"看得见"（用户说"背景我看不出有什么不一样"）

用户第二次看图后直接说：**"背景我没有发现有什么不一样，你说做了，但是我看不出来。"**

**先查证据，不先改代码**（这一步救了后面所有判断）：

1. **逐像素量他发的那张截图**：左侧页边 153×585 = 89,505 个像素里，
   **没有一个像素亮度 > 26**，基线恒为 R11 G15 B25（= 主题的 `--bg`）—— 那块背景是**完全平的**。
2. **查库**：他库里那份主题与目录**逐字节一致**（9501 字符，星点/青下划线都在）——
   也就是说"服务端发出去的是最新版"。
3. 两者一对照，结论只有一个：**那张截图是被"旧版主题"渲染出来的**（v2 没有星点），
   而他**现在**库里那份已经是最新 —— 他截图时那页还没拿到新 CSS。

但**他的感受是真的**：即便在最新版下，我先前量到的星点渲染峰值也只有 31~56，
而正文文字是 230 —— 那确实"看着像没有"。所以这一版做两件事：

**(1) 把星点真正调大调密**（尺寸/密度/透明度全是量出来的）：

| 版本 | 做法 | 渲染后峰值 | 亮点(>45) 占比 | 用户反馈 |
|---|---|---|---|---|
| v1 | 14 个点，1.1~1.6px，alpha .24~.50 | **31** | ~0% | "背景我看不出有什么不一样" |
| v2 | 14 个点，1.1~1.6px，alpha .34~.80 | **56** | 0.03% | 我自己都得凑近看 |
| v3 | 14 个点，**1.8~2.6px**，alpha .36~.80 | **116** | 0.052% | 用户："更新了，但好像没区别，背景还是一片黑" |
| **v4** | **40 个点分三档**（小 24 / 中 10 / 亮点带光晕 6），1.7~3.6px，三档 tile 190/300/460px | **160** | **0.161%**（3.2×） | 满屏星空，仍只有正文（230）的 2/3 |

★ 关键结论：**"看不见"的主因不是亮度，而是"又小又稀"**。
把 v3 的截图放大 3 倍才看清"145×260 的区域里只有 4~5 个灰点"——
参数上"有星星"，观感上就是"一片黑"。**密度**才是"星空感"的来源。

★ 这一版把星点改成**代码生成**（`_starfield_css()`）：40 层星点在
`background-image` / `-size` / `-repeat` 三个列表里各占一位，手写极易错位，
而**错位的表现是"背景整条声明失效"，浏览器不报错**。
现在三个列表来自同一份数据，另加一条测试直接解析目录 CSS 数长度（`test_catalog_yunmeng_starfield_lists_are_consistent`）。

**(2) 把"用户看不到更新"这件事从根上修掉** —— 见 §30.4。

> ★ 另外：探针的留档截图与采样现在统一跑在**用户的实际窗口尺寸 1920×1020** 下
> （`Emulation.setDeviceMetricsOverride`）。星点只有 2px 上下，视口/缩放一变，
> "看不看得见"就变了 —— **测量必须和我看到的、用户看到的在同一个条件下**，
> 否则我量的和他看到的根本不是一回事。

### 30.4 内置插件升级：从"删掉重加"到"一键更新"

这一轮暴露的不是视觉问题，而是一个**结构性**问题：
内置插件（尤其主题）会随版本升级，而**插件内容在"添加"那一刻就被拷进数据库**了 ——
用户看到的永远是他当初添加的那一版。前三轮我每次改完主题，用户都得"删掉→重新添加"，
而这件事**没有任何地方会告诉他**（他甚至不知道我改过）。

修法（后端 3 处 + 前端 2 处，语义很小但把这条路走通了）：

| 位置 | 改动 |
|---|---|
| `plugin_service.catalog_for` | 目录条目新增 `update_available`：已添加**且**内容与当前目录不同才为 true（同名但**类型不同**的插件是用户自己建的，不算旧版） |
| `plugin_service.add_from_catalog` | 同一个函数三种情况：没添加 → 新建（201）；添加过但内容旧 → **覆盖成最新**（200）；已经最新 → 400「已经是最新版」（不假装成功）。返回 `(row, updated)` |
| `api/v1/plugins.py` | `POST /plugins/catalog/{key}` 按情况回 201/200，message 分别是「已添加」「已更新到最新」 |
| `web/js/views/plugins.js` | 目录卡片：有更新时显示 `有更新` 徽章 + 「**更新到最新**」按钮（高亮）；已是最新则是禁用的「已添加」。同一个按钮复用同一个接口，前端按 `update_available` 决定文案与 toast |

三条测试钉住它：
`test_catalog_can_update_a_stale_builtin_copy`（改旧 → 标"有更新" → 一键更新 → 覆盖同一个插件、不多出条目）、
`test_catalog_does_not_clobber_same_name_different_kind`（自己建的同名插件不许被覆盖）、
以及原有那条"重复添加不产生副本"改成断言新话术「已经是最新版」。

> ★ **注意这是一次有意的行为变更**：旧行为是"重复添加 → 400 已经添加过"（只能添加、不能更新）。
> 我改的是**产品语义**，不是把测试改松 —— 旧断言守的"不许偷偷产生第二份"仍然被守住
> （已是最新 → 400；更新 → 覆盖同一个 id、总数不变）。

### 30.7 依赖与文件清理（2026-10-03，用户要求"确保干净整洁"）

**方法：用证据判断，不用 grep 猜。**

1. **先修掉自己上一次扫描的 bug**：先前用 PowerShell 的 `app/**/*.py` 通配去查引用 ——
   Windows PowerShell 5.1 里 `**` 等价于 `*`（只匹配一层），于是 `app/api/v1/*.py` 这些
   **深层模块全被误判成"没人 import"**。改用 **AST 解析全仓库 import** 才可靠。
   （这条本身值得记：**"我扫过了"和"我扫对了"是两件事**。）
2. **死模块**：AST 扫描 83 个模块，`app/` 下**没有任何**无人 import 的模块；
   列出 6 个 `scripts/*` 是**直接运行的脚本**（不是被 import 的库），不算死代码。
3. **死函数**：扫 `app/` 下所有**私有**函数（跳过带装饰器的、`__x__`、公开 API），
   统计"全仓库只出现一次"的 —— **0 个**。
4. **未使用的直接依赖**：AST 扫 import，发现四个**声明了但零引用**的：
   `tenacity`（重试是**自己手写**的 `post_with_retry`）、`tiktoken`
   （本项目**刻意不用**它做 token 估算，`context_manager.py` 顶部专门解释了理由 ——
   依赖清单里留着它反而与设计说明自相矛盾）、`orjson`（连 `ORJSONResponse` 都没用）、
   `pytest-asyncio`（测试全同步，0 个 async 测试、无 asyncio 配置）。
   **已从 `requirements.txt` 删除**，并在文件末尾留一段"清理记录 + 核实方法"。
   `alembic` 虽然也零引用，但注释写明"第二阶段启用"，属于**有意的预留**，保留并把注释改成如实描述。
   （`uvicorn` / `python-multipart` / `python-dotenv` 同样"我们的代码没 import"，
   但分别是**启动命令**、**FastAPI 表单/上传的运行时依赖**、**pydantic-settings 读 .env 的依赖** —— 必须留。）
5. **无用文件**：
   · `web/img/` 里 6 个图标有 4 个页面根本不引用（合计约 3.6MB），而且 `web/` 是**静态站点根目录**，
     放进去就会被伺服。现在分两处：`web/img/`（页面真正引用的 64/256）+
     `assets/icon/`（打包与归档用的 1024 与原稿）；删掉两个无用中间尺寸（32/512，一行命令可重生）。
     生成脚本 `scripts/make_icon_assets.ps1` 同步改成两处输出，**并已重跑验证**。
   · `docs/card-detail-screenshot.png` 由探针每轮生成却**没有任何文档引用** ——
     不删，而是**用起来**：补进 README 的「HTML 开场白会被真正渲染」一节（它正好展示沙箱 iframe 渲染效果）。
6. **顺手修掉一处文档/代码不一致**：`styles.css` 的注释指向 `plugin_service._theme_shim`，
   而实际函数叫 `_surface_shim`（它早已不只管 surface）。已重命名为 `_theme_shim` 并同步文档引用。

> 结论：`app/` 的代码本身没有可清理的死代码（这与项目"先证明没有调用者再删"的纪律一致）；
> 真正的冗余在**依赖清单**与**静态目录里的素材**。

### 30.3 探针：新增 3 条断言 + 2 张留档截图

1. 目录里确实有这条主题（用户一键可开关）；
2. **星空渐变 / 顶栏毛玻璃 / 新图标真的生效**（`getComputedStyle` 读出来 + 图标 `naturalWidth>0`，
   不是"只写了个变量"）；
3. 星云暗涌下同样**没有浅色块**；
4. 顺手把对话页与插件页各截一张图（`docs/nebula-screenshot.png` / `docs/nebula-plugins-screenshot.png`），
   README 与论文素材直接可用。

### 30.4 探针采样器的**第三个盲点**：半透明必须先合成

星云主题一上，采样器立刻误报 3 条"浅色块"（`body` / `.session-item` / `.btn.sec`）：

| 元素 | 它的背景 | 旧算法 | 正确算法 |
|---|---|---|---|
| `body` | 星云光 `rgba(124,156,255,.10)` | 拿 RGB 算亮度 = **158** → 误判"亮" | 按 alpha 合成到 `--bg` 上再算 = **29** |
| `.btn.sec` | `rgba(255,255,255,.05)` | = **255** → 误报 | ≈ **26** |

**根因**：只比"颜色分量"而不看 alpha。**"看起来有多亮" = 把颜色按 alpha 合成到底色上再算**。
采样器现在取 `body` 的 `background-color` 作为底色（主题的 `--bg` 就落在这一层），
对所有实色与渐变**色标**统一先合成再量。

这是这个采样器的第三次修正（前两次：透明背景漏检、渐变漏检），
教训是同一个：**断言必须对着"人眼看到的结果"写，而不是对着"最容易取到的那几个字段"写**。

### 30.5 顺带修掉的探针竞态

`7.撤回` 偶发变红：点完「撤回」后原话要等一次接口返回才回填，而旧写法直接 `eval` 一次就断言
（而且**条件与"详情"是两次独立读取** —— 详情里明明打印着 `钟声响了三次`，条件那次读到的却是空串）。
改成先 `wait_for` 回填再断言，与文件里其它"等加载完再判"的写法一致。

### 30.6 验证

| 项 | 结果 |
|---|---|
| `pytest -q` | **874 passed**（+2 skipped：真实预设用例需设 `HNE_REAL_PRESET`）（+3：内置插件可更新 / 不覆盖同名异类 / 星点三列表等长） |
| `scripts/ui_probe.py` | **147 项 0 失败，控制台报错 0 条**（+7：主题存在 / 视觉生效 / 无浅色块 / 覆盖生效 / 星辉细节 / 卡牌悬停 / 星点亮度区间；留档截图改为 **1920×1020**） |
| `scripts/smoke_test.py` | **184 / 0** |
| `scripts/benchmark.py` 与 `--from-db` | 退出码 0 |
| `docs/nebula-screenshot.png` 等 | 探针每轮自动刷新（对话页 / 角色卡页 / 插件页三张） |

> ★ 这一轮**没有改任何功能**：图标是静态资源 + 几处 HTML/CSS，主题是一条可开关的插件。
> 想回到浅色后台风格，在「插件」页把「云梦枢 · 星云暗涌」停用即可（数据与功能都不受影响）。

---

## 31. 第十八轮：Electron 桌面壳 + 上传 GitHub（2026-10-03 起）

**本轮只做两件事**（交接文档：`docs/next-electron-and-github.md`）：
① 把云梦枢做成 Electron 桌面应用；② 把代码整理成可公开发布的形态（**不含任何使用痕迹**）。

### 31.1 三条红线（违反即回滚）

1. **绝不删除用户文件或数据**。要"排除"某样东西，先**复制一份到仓库外的备份目录**，再用
   `.gitignore` 不跟踪；**不许 `del` / `rm`**。
2. **公开仓库不能有使用痕迹**：个人账号名、本机绝对路径、邮箱、API Key、
   真实角色卡 / 世界书 / 会话 / 向量库 / 日志 / 导出。
3. **不许削弱测试**：可以改测试的**意图**（如把写死的本机路径改成环境变量），
   但**不能删断言让它变绿**。

### 31.2 开工基线（本轮实测，改动后必须重跑并不得低于此）

| 项 | 命令 | 基线结果 |
|---|---|---|
| 单测 | `pytest -q` | **874 passed + 2 skipped**（skip = 真实预设用例，需 `HNE_REAL_PRESET`） |
| 冒烟 | `scripts/smoke_test.py`（需先起后端） | **184 / 0** |
| 探针 | `scripts/ui_probe.py` | **147 项 0 失败，控制台报错 0** |
| 基准 | `scripts/benchmark.py` 与 `--from-db` | **退出码 0** |
| 闸门 | `scripts/prepublish_check.py --name <真名>` | **0 BLOCKER**，1 WARN（测试夹具假密钥，已人工确认） |

> ★ 本轮开始前先修掉一个**环境故障**（与代码无关）：工作区目录缺少写入权限，DSH 文件沙箱
> 无法为它授权（`SetNamedSecurityInfoW failed (Win32 5)`）。已用 ACL 诊断脚本补上当前用户的
> 完全控制项（备份与回滚脚本在 `E:\VSCode\dsh-acl-reports\`，**仓库外**，不入库）。

### 31.3 路线：先 A 后 B（`next-electron-and-github.md` §3.1）

* **路线 A（本轮主线）**：Electron 只当**壳** —— 选空闲端口 → 拉起本机后端 → 轮询 `/health`
  → 就绪后 `loadURL` 控制台。**不改后端契约**，所以四件套与现有验收方式全部继续有效。
* **路线 B（A 跑通后演进）**：PyInstaller 把后端打成 `backend.exe` 作 sidecar，用户免装 Python。

### 31.4 本轮待办清单

**阶段 1：壳与启动流程（A）** —— ✅ **本轮已完成并实测通过**
- [x] 新建 `desktop/`（**不把 Electron 混进 `web/`**）：`package.json`、`src/main.js`、`src/preload.js`
- [x] 主进程：选**空闲端口**（不写死 8000）→ 拉起后端 → 轮询 `/health` → `loadURL`
- [x] 后端进程管理：退出/崩溃清理子进程（Windows 用进程树方式杀，避免残留）
- [x] 窗口安全：`contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`
- [x] 启动中显示**加载页**（file:// 加载，不动 `web/`），失败时给"日志路径 + 常见原因 + 手动复现"
- [x] 菜单：文件（刷新/重新打开控制台/退出）· 视图 · 数据（打开数据/日志/后端日志/.env）· 帮助（关于/接口文档）
- [x] `desktop/README.md` 写清"怎么跑、怎么打包、出错看哪里"
- [x] **Icon**：`desktop/scripts/make-ico.ps1` 由 `assets/icon/logo-1024.png` 生成
      `desktop/build/icon.ico`（16/24/32/48/64/128/256，7 档，已验证 ICO 结构与 PNG 签名）
- [x] **`desktop/test/` 53 条 Node 自测**（Node 内置 test runner，**零测试框架依赖**）

**阶段 2：打包后端（B）** —— ✅ **本轮已完成并实测通过**
- [x] `PyInstaller` 打后端：入口 `desktop/backend_entry.py` + `desktop/scripts/backend.spec`
      （**`app/` 一行未改**，只是换了个启动入口）
- [x] `--hidden-import` / `collect_all` 处理 uvicorn / chromadb / onnxruntime 的动态导入；
      ★ `tqdm` 必须显式带上（ChromaDB 下载模型时用，缺了报错完全看不出跟它有关）
- [x] **关键坑已解决**：chromadb 的 ONNX 模型缓存路径是**写死的** `~/.cache/chroma/...`，
      **不读任何环境变量**（读源码确认）→ 改为"模型随包带 + 首启安装到该位置"
- [x] 端口与 `HNE_*` 由 Electron 注入；打包版数据落 **userData**（`--data-dir`/`--log-dir`），
      非打包版**不受影响**（照旧读 `.env`）
- [x] 一键构建脚本 `desktop/scripts/build_backend.ps1`：独立 `.build-venv`（**不碰验收用的 `.venv`**）
      + 构建后**深度自检**（逐项检查 `/health` 的组件，任一非 ok 就失败）
- [x] 壳侧：**优先用 `backend.exe`，找不到回退本机 Python**；`HNE_DESKTOP_BACKEND` 显式指定
- [ ] 首启向导（让用户填 MySQL 连接与 JWT 密钥写进 userData 的 `.env`）—— **仍未做**，
      当前打包版仍要求用户自己准备仓库根的 `.env`

**阶段 3：安装包与图标** —— 未开始
- [ ] `electron-builder` 出 Windows NSIS 安装包；安装目录只放程序，**数据一律在 userData**
- [ ] `desktop/dist/backend/` 作为 `extraResources` 带进安装包
- [ ] 卸载不删用户数据

**首启向导** —— ✅ **本轮已完成并实测通过**
- [x] 没有任何可用配置时**先进设置页**，而不是报"数据库连不上"
- [x] 表单：MySQL 主机/端口/用户/口令/库名 + 签名密钥（可一键随机生成）+ 加密口令 + 高级项
- [x] 「保存并开始」会**真的起一次后端并逐项检查组件**（database / vector_store 都 ok 才放行）
- [x] 配置写进 **userData**（`%APPDATA%\云梦枢\config\.env`），安装目录之外；**不打包任何 `.env`**
- [x] 已有配置被改坏（必填项为空）时**重新进设置页**，并明确指出缺哪一项
- [x] 读-改-写：**用户手工加过的其它配置原样保留**（不悄悄抹掉）
- [ ] 安装包（阶段 3）里把向导一起打进去

**并行线：公开发布**
- [ ] 生成**零历史**干净副本（`robocopy` 排除 `.git` / `.venv` / `data/chroma|logs|exports` / `node_modules`）后跑闸门
- [ ] README 顶部补桌面版说明；`.env.example` 确认只有占位符
- [ ] **git 提交由用户自己操作**，AI 只负责把工作区改干净 + 给出"该提交什么"的清单

### 31.5 本轮验收标准

1. 四件套数字**不低于** §31.2 基线（874+2 / 184 / 147 / benchmark 0）；`prepublish_check` 仍 **0 BLOCKER**。
2. `desktop/` 的 **Node 自测**（`npm test`，用 Node 内置 test runner，**不引入测试框架依赖**）全绿，
   覆盖：空闲端口选择、健康检查轮询（就绪/超时/中途连不上）、后端进程树清理。
3. 手工点测清单（Electron 侧，逐条打勾并记录实测证据）：
   - [x] 启动后能进控制台（不是白屏、不是默认 Electron 图标）
   - [x] 启动阶段能看到加载页，成败都有明确文案
   - [x] 关窗 / 强杀主进程后，后端子进程**不残留**（`Get-Process python` 核对）
   - [x] 端口被占用时能换成别的端口启动
   - [x] 后端没起来时，界面给出**可操作**的报错（含日志路径与常见原因）
   - [x] `web/` 一行没改；后端契约一行没改
4. 打包产物与 `node_modules/` **不入库**；要排除的东西**先在仓库外有备份**。

> ★ 诚实边界（先写下来，免得后面自我感觉良好）：路线 A 交付的是**桌面壳**，
> 用户仍需自备 Python + MySQL —— 这一点必须写进桌面版的 README 与报错文案，
> 不能让人以为"双击就全都有了"。真正的"双击即用"是路线 C，不在本轮范围。

### 31.6 阶段 1 实测记录（路线 A）

**自动化**：`desktop/` 下 `npm test` → **53 passed / 0 failed（2.2s）**。

**真实进程验收**（写了一套临时脚手架驱动真 Electron，跑完已删除；结论如下）：

| 场景 | 结果 | 证据 |
|---|---|---|
| 优先端口被占（8000 被占位进程占用） | ✅ 换成空闲端口启动 | 日志 `端口选定 63230（来源 preferred-busy）`，且 63230 确实在监听 |
| 后端不在时由壳自己拉起 | ✅ | 日志 `已启动后端 PID=... port=...`；健康检查 `4 次尝试 / 2229ms` |
| 加载控制台 | ✅ 窗口真的出现 | 窗口标题 `云梦枢 · 交互式叙事引擎`，截图确认登录页渲染完整、菜单为中文四项 |
| **关窗后不残留** | ✅ | 日志 `应用退出：结束后端进程树 PID=32044`；关窗后 `Get-Process python` = 0 |
| **强杀壳进程树后不残留** | ✅ | `taskkill /T /F` 之后 python 0 个、8000 无监听 |
| 后端起不来时报错可操作 | ✅ | 日志 `你指定的 Python 解释器（HNE_DESKTOP_PYTHON）用不了` + `→ 无法执行：ENOENT`；壳停在失败页而不是静默退出 |
| 已有健康后端时**不重复拉起** | ✅ | 日志 `8000 上已有健康的后端，直接使用（不另起进程）` |
| 端口决策与实际一致 | ✅ | `端口选定 8000（来源 preferred）` 时窗口标题里的端口一致 |

> ★ 全程 `web/` 与 `app/` **一行未改**（`git diff --stat` 可核对），
> 所以四件套验收方式与探针断言完全不受影响。

### 31.7 本轮踩到的三个坑（都写进 `desktop/README.md` 了）

1. **`ELECTRON_RUN_AS_NODE=1` 会写在宿主环境里** —— 这台机器的环境变量中带着它，
   于是 `electron.exe` 退化成纯 Node：`require('electron')` 返回 npm 包导出的**路径字符串**，
   主进程什么都不做就退出（**退出码 0、没有任何报错**）。
   症状极易误判成"我的 main.js 写错了"。`desktop/README.md` 已写明排查办法。
2. **npm 11 默认不执行安装脚本** —— Electron 的二进制靠 `postinstall` 下载，
   于是 `node_modules/electron/dist/` 是空的（装完看着"成功"）。
   修法：`npm rebuild electron --foreground-scripts` 或直接 `node node_modules/electron/install.js`。
3. **`extract-zip` 解压到一半被系统中断** —— 75 个条目只落地 `locales/`，
   进程静默结束、**退出码还是 0**。改用 PowerShell 自带解压即可（缓存里的 zip 是完整的）。
   两条修法都写进 `desktop/README.md`；它们**只影响本机开发环境**，与"该提交什么"无关。

> ★ 另一处顺带的设计收紧：`HNE_DESKTOP_PYTHON` 指定的解释器不可用时，
> 默认行为是**继续回退**试别的（对多数人省事），但这正是本项目反复强调要避免的静默降级。
> 因此新增 `HNE_DESKTOP_PYTHON_STRICT=1`：**指定了就只用它**，用不了就明确报错、
> 并把"你指定的是哪个、为什么不能用"打出来。两条行为都有测试钉住。

### 31.8 新增/改动的文件（本轮）

```
desktop/package.json              只依赖 electron@38.8.6（唯一 devDependency，已锁精确版本）
desktop/package-lock.json         锁文件（进库：保证别人装到同一个版本）
desktop/README.md                 怎么跑 / 出错看哪里 / 打包路线 / 三个坑
desktop/src/main.js               主进程：启动流程、菜单、生命周期、IPC
desktop/src/preload.js            具名、有限的能力（不暴露 ipcRenderer）
desktop/src/loading.html          启动中与失败页（file:// + 收紧的 CSP）
desktop/src/paths.js              路径解析（数据目录与安装目录分开）
desktop/src/backend-config.js     .env 解析 / Python 探测（含严格模式）/ 环境与端口决策
desktop/src/backend-process.js    子进程启动、日志接线、进程树清理
desktop/src/net-utils.js          空闲端口、健康检查轮询（含单次探测硬超时）
desktop/src/port-check.js         "这个端口上是不是已经有后端"
desktop/scripts/make-ico.ps1      生成 desktop/build/icon.ico（带 UTF-8 BOM）
desktop/build/icon.ico            生成物（随包发布，进库）
desktop/test/*.test.js            53 条 Node 自测
.gitignore                        补 desktop/ 的忽略规则（依赖与打包产物）
README.md                         状态数字更新 + 补"运行测试""桌面版"两节
docs/handoff.md                   本节
```

> ★ `desktop/build/icon.ico` **进库**是刻意的：它随安装包发布，别人 clone 后要能直接打出带图标的包。
> 根 `.gitignore` 里的 `build/` 只匹配**仓库根**，不会误伤 `desktop/build/`（已在 `.gitignore` 注释里说明）。

### 31.9 还欠着的（下一步）

1. **阶段 3**（NSIS 安装包 + `extraResources` 带 sidecar）—— 见上面清单。
2. **首启向导**：让用户填 MySQL 连接与 JWT 密钥、写进 userData 下的 `.env`
   （当前打包版仍要求用户自己准备仓库根的 `.env`，这是"能不能真的交给别人用"的最后一道坎）。
3. **公开发布线**：干净副本 + 闸门 + README/`.env.example` 复核（§31.4 并行线）。
4. `desktop/README.md` 里的手工点测清单，建议用户自己再点一遍**安装包形态**（阶段 3 之后）。

### 31.11 阶段 2 实测记录（打包后端 / sidecar）

**产物**：`desktop/dist/backend/backend.exe`（整目录 **293.8 MB**，其中自带 ONNX 模型 87.1 MB）。

| 场景 | 结果 | 证据 |
|---|---|---|
| 构建 + 深度自检 | ✅ | `database: ok` / `vector_store: ok` / 顶层 `ok`；自检能自己退出 |
| 壳优先用打包后端 | ✅ | 日志 `使用打包后端 backend.exe（无需本机 Python）` + `已启动后端 PID=... mode=sidecar` |
| 端口被占时换端口 | ✅ | `端口选定 60625（来源 preferred-busy）`，且 60625 确实在监听 |
| 数据落 userData | ✅ | `%APPDATA%\云梦枢\data\chroma` 创建成功；后端日志显示 `HNE_CHROMA_PERSIST_DIR=...\云梦枢\data\chroma` |
| **关窗后 sidecar 不残留** | ✅ | 关窗后 `backend.exe` 进程数 = 0 |
| 显式指定不存在的后端 | ✅ | 明确报错并停在失败页；**没有**悄悄回退本机 Python |
| `desktop` Node 自测 | ✅ | **73 passed / 0 failed**（阶段 1 是 53 条） |

**本轮在阶段 2 抓到的三个真 bug**（都不是"多虑"，全是靠"让它失败"的用例逼出来的）：

1. **自检假通过**：第一版自检只看 HTTP 200 就算过，而后端当时的 MySQL 与向量库**都是坏的**
   —— 因为本项目的 `/health` 在组件降级时**照样返回 200**。等于"打包成功"是句空话。
   已改成逐项检查 `components.*.status`。
2. **`tqdm` 没打进包**：ChromaDB 的 ONNX 嵌入函数在下载模型时要用它，缺了直接 `ValueError`，
   而报错信息里完全看不出与 tqdm 有关。
3. **`HNE_DESKTOP_BACKEND` 被无声忽略**（两层，见 §31.12）——最严重的一个。

### 31.12 ★ 本轮的教训：一个"看似接好了、其实没接线"的 bug

`HNE_DESKTOP_BACKEND`（用户显式指定用哪个打包后端）**被完全忽略**，而且是两层：

* **第一层**：`main.js` 把 `explicit` 传给了 `chooseBackend`，**却忘了传给
  `sidecarCandidates`** —— 于是用户指定的那个路径根本没进候选列表，等于从没检查过它。
  更糟的是 `chooseBackend` 仍返回 `explicitSpecified: true`，日志里赫然写着
  "使用打包后端 backend.exe"，**用户指定 A、程序用了 B**。
* **第二层**（修完第一层才暴露）：把 `explicit` 也传进候选列表后，如果指定的那个不存在，
  代码会**悄悄落到默认候选**（`desktop/dist/backend/backend.exe`）上 —— 还是"用了别的"。
  正解比"传参数"更简单：**显式指定 = 只认它**，`candidates = [explicit]`，不存在就失败。

> ★ 为什么单测没抓到它：单测直接调 `chooseBackend({explicit, candidates: [...]})`，
> **自己把 explicit 放进了 candidates** —— 而那正是 `main.js` 忘做的事。
> 教训：**测一个函数的时候，我按"我以为调用方会怎么调"来写参数；
> 只有真的把程序跑起来，"调用方实际怎么调"才会现形。**
> 现在 `desktop/test/sidecar.test.js` 里有两条标了「★★ 回归（接线）」的用例，
> 专门复现 `main.js` 的调用形状，并把"混入默认候选会静默用到别的后端"写成反面教材。

### 31.13 ★ 清掉 git 历史里的使用痕迹（2026-10-03）

**为什么必须动历史**：工作区早就干净了，但**旧提交里全是痕迹** ——
贡献者的**真名与真邮箱**出现在**全部 10 个提交**的作者与提交者字段；
文档多行写着真名与真账号 id、`C:\Users\<真名>\Downloads\...`、
以及测试里写死的本机路径；`.dsh-drop/` 的 3 个个人文件（ChatGPT 截图、Screenshot、毕设选题稿）
也在最后一次提交里。**push 出去这些照样能被翻出来。**

> ★ 本节**刻意不写出**那个真名与真邮箱 —— 否则"为了清痕迹"的文档本身
> 就成了新的痕迹（本轮真发生过一次：写证据表时把真名真邮箱抄了进去，
> 立刻被 `prepublish_check.py --name <真名>` 判为 BLOCKER 抓出来）。

**做法（用户确认：重建成单个干净提交 + 中性署名 + 先备份）**：

1. **先备份（红线：不许删用户文件）**，备份在**仓库外**
   `E:\VSCode\hetero-narrative-engine-backup-20261003-223643\`：
   · `git/hetero-narrative-engine-full-history.bundle`（**完整旧历史**，7.25 MB，
     `git bundle verify` 通过 → 可用 `git clone <bundle>` 完整还原）
   · `dsh-drop/`（3 个原文件，两张图都验证过能正常打开）
   · `working-tree/`（工作区副本）
2. 旧 `.git` **改名保留**后用 `git init -b main` 建全新仓库；
   署名设为 `tangyu233-7312 <279469304+tangyu233-7312@users.noreply.github.com>`
   （GitHub 给该账号分配的 **noreply** 地址，不含任何个人信息；
   最早一版曾用 `云梦枢 <yunmeng-hub@users.noreply.github.com>` ——
   后来用户提供了自己的 GitHub 身份，于是换成了能真正归属到账号的地址）。
3. `git add -A` + 单次提交 → **1 个提交、193 个文件**（与闸门模拟的待发布集合一致）。
   > 第十九轮收尾时又压了一次：把那个提交与安装包阶段的新改动合成**最终那一个提交**
   > （**1 个提交 / 201 个文件**），旧哈希就连中间态也一并 `gc` 清除 ——
   > 中间态同样备份在仓库外（`...-1commit-b86a26a.bundle`），要对照随时能取。
4. 验证通过后才删除改名保留的旧 `.git`，并 `git reflog expire --expire=now --all`
   + `git gc --prune=now`，让旧对象彻底不在本仓库里。

**验证证据**（不是"应该没事"）：

| 检查 | 结果 |
|---|---|
| `git rev-list --all --count` | **1** |
| `git ls-files` 文件数 | **193**（= 发布闸门候选数） |
| 作者/提交者 | `tangyu233-7312 <279469304+tangyu233-7312@users.noreply.github.com>`，**0 处真名真邮箱** |
| `git grep <真名> $(git rev-list --all)` | **0 命中** |
| `git grep <真邮箱前缀>` / `<邮箱域名>` | **0 命中** |
| 旧哈希 `cf30214` / `4c55534` / `08d309d` / `15a26cf` | `git cat-file` → **已清除** |
| `git fsck --unreachable` / reflog | **0 不可达对象 / 0 条目** |
| `prepublish_check.py --name <真名>` | **0 BLOCKER** |
| 工作区与备份逐字节对比 | 抽样 4 个文件**完全一致**（唯一不同的 `.gitignore` 是本次有意新增的忽略规则） |
| 远端 / 标签 | **无远端、0 标签** |

> ★ 残留的"疑似命中"只有 3 类，都已逐条确认**不是泄漏**：
> 占位符本身（`C:\Users\<你>`、`C:\Users\someone`、`<账号注册名>`）、
> `.gitignore` 里解释"为什么忽略 `.dsh-drop/`"的注释、以及真名检查器的自测夹具。

**踩到的一个坑（值得记）**：把旧 `.git` 改名成 `.git.old-preserved/` 之后，它只是个**普通目录**，
`git add -A` 会把里面 **613 个旧 git 内部文件**（含全部旧提交对象）一起暂存 ——
等于把"要清掉的痕迹"原封不动搬进新仓库。修法：在 `.gitignore` 里显式加
`.git.old-preserved/` 与 `*.bundle`（这条规则保留下来，防止将来重演）。
另外：`git check-ignore` 对**已被跟踪**的路径默认**不报**忽略 —— 判断规则是否生效要用
`git check-ignore --no-index`（本轮因此误判了一次）。

**用户接下来要做的**（推送由用户自己操作）：

```powershell
# 仓库里现在没有远端；到 GitHub 建好空仓库后：
git remote add origin https://github.com/<你的账号>/<仓库名>.git
git push -u origin main
```

> ⚠️ 若想让提交"算到自己头上"，把 `279469304+tangyu233-7312@users.noreply.github.com`
> 加进 GitHub 账号的邮箱设置即可（并可在 `Settings → Emails` 打开 "Keep my email
> addresses private"），**不需要再改历史**。

### 31.14 首启向导与三个真 bug（2026-10-03 续）

**首启向导**（见 §31.4 的勾选清单）已实测通过
（临时脚手架驱动真 Electron + CDP 填表，跑完已删除）：

| 场景 | 结果 |
|---|---|
| 全新空配置 → 进**设置页**（不是控制台、不是报错页） | ✅ |
| 填表保存 → 后端**深度自检**（database ok / vector_store ok） | ✅ |
| 配置写进 `userData/config/.env`，含口令/密钥，固定值正确 | ✅ |
| 保存后**自动进控制台** | ✅ |
| 配置被改坏（缺口令）→ 重新进设置页，并指出缺 `HNE_MYSQL_PASSWORD` | ✅ |
| 保留用户手工加过的其它配置项 | ✅（单元测试） |

**顺带抓到的三个真 bug**（都不是"多虑"）：

1. **默认端口策略崩了**（★ 用户截图抓到的）—— 我把默认端口从"写死 8000"改成
   "默认让系统分配"（`preferredPort` 返回 `null`），而 `startBackend()` 里有**两处**
   用到这个值：我给"探测已有后端"那处加了 null 守卫，**却漏了"判断优先端口是否空闲"那处**，
   于是 `net.connect({ port: null })` 抛
   `The "options.port" property must be one of type number or string` —— **默认路径直接启动失败**。
   > 教训：**同一个值在多处使用，只守了一处**；而且我所有验收都显式传了端口，
   > 于是"新的默认值"从没被走过。**改了默认值就必须测新的默认值。**
2. **打包产物自检的退出码一直是 -1** —— PyInstaller 的 exe 是"bootloader 父进程 + 真身"，
   硬杀子进程时 bootloader 报 **-1**（不是 0）。于是 `--self-check` 组件全 ok、调用方却看到失败。
   修法：有序关闭 uvicorn（先 `should_exit`/`force_exit` 再 join，预算放宽到 30s），
   并用 `os._exit(main())` 而不是 `sys.exit()`（避免解释器关闭阶段污染退出码）。
3. **构建脚本"假通过"** —— `& $Exe ... | ForEach-Object {...}` 会让 `$LASTEXITCODE`
   被管道最后一环覆盖成 0，于是**产物自检失败也被判成通过**。
   修法：重定向到文件、**只按退出码判断**，要看内容再单独读文件。

**另有两次"我自己的测试骗了我"**（值得记，因为比 bug 更浪费时间）：
① 验收脚本开头**没清理上一轮残留的 Electron**，CDP 连到了旧窗口，读到的全是旧变量；
② 两个场景**共用一个 profile**，后一个把前一个正在用的配置文件删了，于是"应用看起来是坏的"，
   我为此白改了几次**本来正确**的代码。现在的规则：**验收第一步是确保环境干净**，
   并且每个场景用独立 profile。

### 31.10 收尾验收（全部重跑，串行）

| 项 | 基线（§31.2） | 本轮结果 | 判定 |
|---|---|---|---|
| `pytest -q` | 874 passed + 2 skipped | **877 passed + 2 skipped**（+3：`--name` 命中改为 BLOCKER 的三条新用例） | ✅ 未削弱 |
| `scripts/smoke_test.py` | 184 / 0 | **184 / 0** | ✅ 一致 |
| `scripts/ui_probe.py` | 147 项 0 失败 | **147 项 0 失败，控制台报错 0 条** | ✅ 一致 |
| `scripts/benchmark.py` 与 `--from-db` | 退出码 0 | **退出码 0 / 0** | ✅ 一致 |
| `desktop/` Node 自测 | （本轮新增） | **53 passed / 0 failed** | ✅ |
| `prepublish_check.py --name <真名>` | 0 BLOCKER | **工作区 0 BLOCKER**；**188 个待提交文件**的干净副本同样 **0 BLOCKER** | ✅ |

> ★ **为什么 pytest 从 874 变 877 不是"放宽"**：新增的三条全部是**加严**的断言
> （`--name` 命中从 WARN 升为 BLOCKER、BLOCKER 措辞不得复述真名、真名检查要覆盖所有文本文件）。
> 原有的断言一条没删 —— 这一点可以用"基线 874 条仍然全绿"来核对。

#### ★ 数据没有被本次验收破坏（核对了，不是"应该没事"）

`ui_probe.py` 跑完会调用 `scripts/cleanup_demo_data.py` 清理**测试账号**（前缀表见该文件注释）。
因此本轮的库内计数变化是：

| 项 | 变化 | 说明 |
|---|---|---|
| `users` | 13 → 1 | 12 个是**测试前缀账号**（`pv_`/`cc_`/`smoke_`/`ui_` …），被探针的清理脚本按前缀删掉；唯一留下的就是真实账号 |
| `character_cards` / `world_books` / `narrative_sessions` / `messages` | **4 / 3 / 4 / 39，全程不变** | 这些是真实账号名下的数据，一个没动（逐会话核对过：5 + 25 + 2 + 7 = 39） |

> ★ 顺带发现基线本身是"脏"的：我第一次抓到的 13 个用户，其实**主要是 pytest 跑完留下的
> 测试账号**（所以后来 `smoke_test.py` 报的"测试前后一致"是自洽的）。
> 也就是说 —— 这轮验收反而**顺手把库里积压的测试垃圾清掉了**，真实数据完好。
> 这一条值得记：**"前后一致"不等于"没有垃圾"，只等于"没再多出来"。**

#### ★ `.gitignore` 的一个真坑（本轮踩到，已修）

第一版我在 `.gitignore` 里写「根 `build/` 只匹配仓库根，不会影响 `desktop/build/`」，
**这是错的**：gitignore 里**不含斜杠的模式匹配任意层级**，所以 `build/` 也吃掉了
`desktop/build/` —— `desktop/build/icon.ico` 被**静默忽略**，
`git status` 里根本看不到它，而 README 却写着"图标随包发布"（文档与事实相反）。

修法：显式负向豁免（必须先豁免目录本身，再豁免内容）：

```gitignore
!desktop/build/
!desktop/build/**
```

> 这条的教训与项目里其它几条一样：**"我以为 git 会怎么处理"不能当证据**。
> 核对方式是 `git check-ignore -v <路径>` 与 `git ls-files -co --exclude-standard`
> （后者才是"`git add -A` 真正会包含什么"的权威答案）。

### 31.15 阶段 3：Windows 安装包（2026-10-04）

**产出**：`release\云梦枢 Setup 0.1.0.exe` —— **234.8 MB**，Windows x64 NSIS，
SHA256 `6AA3ED22E75EDA7C4B6DEA6752F4ABD866C4CB9BCF31C481563FF7EE44B4B9E0`。
安装包**不入库**（`release/` 已在 `.gitignore`），由下面两条命令随时重建：

```powershell
cd desktop
npm run build:backend       # ① sidecar（PyInstaller，约 290MB）
npm run build:installer     # ② 安装包（electron-builder/NSIS）
powershell -File scripts\verify_installer.ps1 -EnvFile ..\.env   # ③ 自动验收
```

**装出来的那一份里有什么**（`electron-builder` 的 `build` 段，见 `desktop/package.json`）：

| 位置 | 内容 | 为什么 |
|---|---|---|
| `resources/app.asar` | 只有 `src/**` 与 `package.json`（13 个文件） | 白名单而不是黑名单：以后新增目录默认**不带**进去（`desktop/test/` 里的写死路径不该发给用户） |
| `resources/backend/` | sidecar 整份（863 个文件 / 293.8 MB） | `extraResources`；落点必须与 `src/sidecar.js` 的候选路径一致 |
| 安装目录**之外** | 配置、日志、向量库全在 userData | 卸载重装不丢；卸载**不删**用户数据 |

**自动验收（15 项全绿）**：静默装到临时目录 → 用**全新空配置**启动 →
核对安装目录（有后端、**没有 `.env`**、asar 内容正确）→ 通过 CDP 在**真实页面**里调
「保存并开始」的入口 → 必须进控制台 → 静默卸载 → **用户数据仍在**。
全程不碰真实 `%APPDATA%\云梦枢` 与仓库 `.env`。

#### 阶段 3 抓到的三个真问题（都不是"多虑"）

1. **打包态会去读安装目录里的 `.env`**（安全向，最值得记）。
   `appRoot = __dirname/../..` 在打包后就是**安装目录**，于是"仓库根 `.env` 兜底"
   变成了"读 `resources/.env`"。后果两条：① 安装目录常常写不进去（往 `Program Files`
   写配置直接 EPERM）；② 更严重的是，若那里恰好有一份 `.env`（打包误带、用户自己复制），
   应用会拿**别人的**数据库口令与签名密钥去连库。
   > 也就是说，"**绝不把 `.env` 打进安装包**"只做到了**一半** —— 另一半是
   > "**打包态绝不去读它**"。修法：新增 `src/packaging.js`，`packaged === true` 时
   > 一律不做兜底（纯函数，`test/packaging.test.js` 与验收脚本两处钉住）。
2. **electron-builder 会重新下一份 Electron**（约 136MB）。本机实测那个下载只有
   约 0.1 MB/s（4 分钟走到 3.67MB 就基本不动了）。修法：`build.electronDist` 指向
   `node_modules/electron/dist`（`npm install` 早就解压好的那一份），从此不再下载。
3. **NSIS 工具链的下载/解压会静默卡死**。它去 GitHub 下 `nsis-3.0.4.1.7z` 与
   `nsis-resources-3.4.1.7z`，然后用自己那套 Node 侧流程装进 electron-builder 缓存；
   实测**无网络活动、无输出、CPU 不动**地卡住（缓存里只留下一个空的解压目录与 `.lock`），
   而同一时刻用 PowerShell 直接下同一个 URL 只要十几秒、SHA256 与官方值一致。
   更糟的是它**不报错**，只是不吭声地等着。
   修法：`build_installer.ps1` 里**自己下、校验 SHA256、自己用 7z 解压**，
   并**复刻 electron-builder 的缓存目录命名**（`<文件名>-<URL 的 djb2 base36 前 5 位>`）
   与它认的 `.state`，于是它那边直接命中缓存、不下载不解压不加锁。
   顺带做到"离线可重复"：缓存备好之后打包不再需要网络。

#### 我在这一轮"自己骗自己"的四处（比 bug 更浪费时间）

| 现象 | 真相 | 现在的做法 |
|---|---|---|
| 验收脚本报"没有确认打包形态""没有进入首次设置页"（其实应用完全正常） | Windows PowerShell 5.1 的 `Get-Content` **默认按 ANSI 解**，UTF-8 中文日志全成乱码 | ① 一律 `-Encoding UTF8`；② 让应用打印一行**纯 ASCII** 的 `[state] packaged=... step=...`，判据不赌编码 |
| 验收脚本报"app.asar 里有多余内容：`\src`、`\package.json`" | `asar list` 会同时列**目录项**；我只按"文件名"正则过滤了目录项 | 只拿文件项判断（`\src\main.js` 这种） |
| 验收脚本报"后端日志里没有 `[self-check] OK`" | 自检子进程的 stdout 由**壳**接管，写在**桌面日志**里；后端日志只有访问日志 | 判据改成桌面日志里的"退出码=0 且后端结论=ok" |
| smoke 报 2 项红（向量库写入/删除） | 我把后端的向量库指到 temp，而 smoke 自己直连的是仓库 `data/chroma` —— **环境不一致，不是 bug** | 两边都设 `HNE_CHROMA_PERSIST_DIR` 后 **184/0**（见下表） |

另外三个 PowerShell/Node 侧的坑（都在脚本注释里）：
`Get-Content -AsByteStream` 是 PowerShell 7 的（5.1 没有）；
PowerShell 脚本**必须带 UTF-8 BOM**，否则 5.1 按 ANSI 读、中文全乱（`edit` 工具会掉 BOM，
所以每次改完脚本都要补）；
**函数参数不能叫 `$Input`**（那是自动变量，会让函数算出与输入无关的常数、而且不报错）。

#### 阶段 3 验收（全部串行重跑；`app/**` 与 `web/**` 一行未改）

> ★ 本阶段全部改的是 `desktop/`（打包形态判定、安装包配置、构建与验收脚本），
> 所以下面"基线不变"是可预期的 —— 但四件套仍然**实跑核对过**，不用"应该没变"当证据。
> ★ 收尾时把工作区（30 个改动）与那次提交压成**最终的一个提交**：
> **1 个提交 / 201 个文件**，署名与 §31.13 一致；旧哈希（含中间态，以及 amend 前的两次）
> 已 `gc` 清除，`git fsck --unreachable` 0 条、reflog 0 条；
> 仓库外另存了两个中间态 bundle（`...-1commit-b86a26a.bundle` / `...-1commit-18f3588.bundle`）。
> 最终那个提交的哈希以 `git log --oneline` 为准 —— 这里**刻意不写死**：
> 每改一次交接文档再 amend，哈希就变一次（本轮为此返工了两回）。

| 项 | 基线（§31.2） | 结果 | 判定 |
|---|---|---|---|
| `pytest -q` | 874+2 skipped | **877 passed + 2 skipped** | ✅ 未削弱 |
| `smoke_test.py`（对**打包后端**） | 184 / 0 | **184 / 0** | ✅ 一致 |
| `ui_probe.py`（对**打包后端**） | 147 / 0 | **147 / 0，控制台报错 0 条** | ✅ 一致 |
| `benchmark.py` + `--from-db`（对**打包后端**） | 退出码 0 | **0 / 0** | ✅ 一致 |
| `desktop/` Node 自测 | 53 → 99 | **115 passed / 0 failed** | ✅ |
| 安装包验收 `verify_installer.ps1` | （本轮新增） | **15 项通过 / 0 失败** | ✅ |
| `prepublish_check.py --name <真名>` | 0 BLOCKER | **0 BLOCKER** | ✅ |
| 库内计数（真实账号名下） | 4 卡 / 3 书 / 4 会话 / 39 消息 | **完全一致**（users 1，测试账号已被探针清理） | ✅ |

> ★ 为什么这一次也**没有动 `app/**` 与 `web/**`**：安装包这一阶段改的全是
> `desktop/`（打包形态判定、安装包配置、构建/验收脚本），所以上面四件套的
> "基线不变"是可以预期的 —— 但仍然**实跑核对过**，不用"应该没变"当证据。

#### 还欠着的（诚实边界）

- 安装包**没有代码签名**（要用户自己的证书）→ 首次运行有 SmartScreen 提示；
- 只出 **Windows x64**（NSIS），没有 macOS / Linux 产物；
- **MySQL 仍要用户自己装**（应用不附带数据库；首启向导只负责把连接信息填对并当场验证）；
- 安装包 **234.8 MB**（其中后端 293.8 MB 压缩后）—— 想更小只能裁依赖或让模型联网下载，
  两条都有代价，本轮没做。







