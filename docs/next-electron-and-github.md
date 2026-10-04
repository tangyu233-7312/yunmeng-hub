# 交接：Electron 打包 + 上传 GitHub

> **给新会话看的第一份文档。** 目标两件事：① 把「云梦枢」打成桌面应用；② 把代码发到 GitHub
> 公开仓库，**且不能带上任何使用痕迹**（登录信息 / API Key / 角色卡 / 世界书 / 会话 / 截图）。
>
> 上一段工作（第十七轮：系统定名、图标、星云暗涌主题、依赖与文件清理）已全部完成并验收通过，
> 细节见 `docs/handoff.md` §30 与 `docs/handoff-quick.md` §21。

---

## 0. 三条红线（先看这个）

1. **绝不删除用户的任何文件或数据。** 需要"排除某样东西"时，一律用
   **① gitignore 不跟踪** 或 **② 复制一份到仓库外的备份目录**，**不要 `del` / `rm`**。
   本次已经这样做了：备份在 `E:\VSCode\hetero-narrative-engine-backup-<时间戳>\`，
   原文件全部留在原处（`.dsh-drop/` 只是**取消 git 跟踪**，磁盘上还在）。
2. **公开仓库里不能有使用痕迹**：个人账号名、本机绝对路径、邮箱、API Key、
   真实角色卡 / 世界书 / 会话 / 向量库 / 日志 / 导出、AI 传来的个人截图。
3. **不许削弱测试**：可以改测试的**意图**（例如把写死的本机路径改成环境变量），
   但不能删断言来"让它变绿"。本次改的两处都保留了原有断言与 skip 行为。

---

## 1. 当前状态（可直接接手）

| 项 | 值 |
|---|---|
| 系统名 | **云梦枢（YunMeng Hub）** |
| 定位 | 异构大模型交互式叙事引擎（本科毕设，本地单机） |
| 服务 | `http://127.0.0.1:8000`（控制台 `/console/`，接口文档 `/docs`）；启动：`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| 后端 | Python **3.13.12** + FastAPI + MySQL **8.0.28** + ChromaDB（本地 ONNX 嵌入 all-MiniLM-L6-v2 / 384 维） |
| 前端 | **零构建**：原生 ES Module + importmap 版本指纹；11 个 JS 文件（4 核心 + 7 视图） |
| 验收（冻结代码，串行跑过） | `pytest` **874 passed + 2 skipped** · `scripts/smoke_test.py` **184 / 0** · `scripts/ui_probe.py` **147 项 0 失败、控制台报错 0 条** · `scripts/benchmark.py` 与 `--from-db` **退出码 0** · `scripts/token_accuracy.py` **退出码 0** |
| 新增文档 | **`docs/overview.md`**（系统总结：做了什么/功能/技术/怎么验证）——给人看的入口 |
| 改动即重启 | 改 `app/**` 要重启 uvicorn；只改 `web/**` 刷新页面即可（有版本指纹自动失效） |

> 想快速了解系统全貌：先读 `docs/overview.md`。想了解"每一轮为什么这么改"：`docs/handoff.md`。

---

## 2. ★ 上传 GitHub：本次已经做好的准备

### 2.1 已完成的清理（都是"有证据"的，不是凭印象）

| 项 | 处理 | 证据 |
|---|---|---|
| **`.dsh-drop/` 里 3 个文件被 git 跟踪**（一张个人截图、一份毕设选题、设计稿） | **`git rm -r --cached .dsh-drop`**（磁盘文件保留）+ `.gitignore` 忽略 | `git check-ignore -v` 确认已被忽略 |
| 贡献者本机的**账号名**与 `C:\Users\<账号>\...` 路径出现在 5 个已跟踪文件 | 文档与注释里改成「真实用户」「<本机下载目录>」；**测试里写死的本机路径改成环境变量 `HNE_REAL_PRESET`**（不在就 skip，行为不变） | `git grep <账号名>` → 0 命中；`git grep -F 'C:\Users'` → 0 命中 |

> ⚠️ **两个被 skip 的用例（本次唯一"覆盖面变化"，请知悉）**：
> `tests/test_presets.py::test_real_preset_file_if_present` 与
> `tests/test_prompt_presets.py::test_real_preset_import_if_file_present`
> 以前写死了本机绝对路径，所以在你这台机器上**会真的跑**（那个赤狐预设文件存在）；
> 为了不让个人路径进公开仓库，路径改成环境变量后它们变成 **skip**。
> 想恢复本机覆盖（推荐，答辩前跑一次真机数据更踏实）：
> ```powershell
> $env:HNE_REAL_PRESET = "C:\Users\<你>\Downloads\赤狐DeepSeek(4).json"
> .\.venv\Scripts\python.exe -m pytest tests/test_presets.py tests/test_prompt_presets.py -q
> ```
> **断言一条没删**，只是路径来源变了；文件不存在时照旧 skip（别人 clone 也不会红）。
| `.env` / `data/chroma` / `data/logs` / `data/exports` | 未被跟踪（`.gitignore` 里），**且磁盘上保留** | `scripts/prepublish_check.py` 报告为"已忽略，不会上传" |
| 4 个零引用依赖（`tenacity` / `tiktoken` / `orjson` / `pytest-asyncio`） | 从 `requirements.txt` 删除（AST 核实全仓库零 import） | `docs/handoff.md` §30.7 |
| 图标素材 3.6MB 无用文件 | 从被伺服的 `web/img/` 分出 `assets/icon/`（1024 + 原稿），删掉 2 个可再生的中间尺寸 | 生成脚本 `make_icon_assets.ps1` 已同步并重跑 |
| 无 API Key 形状泄漏 | `git grep` 未命中任何真实密钥；测试里的假密钥是夹具 | 见下 2.2 的自检输出 |

### 2.2 上传前的**闸门**：`scripts/prepublish_check.py`

```bash
python scripts/prepublish_check.py                 # 检查当前目录
python scripts/prepublish_check.py --root DIR       # 检查"干净副本"
python scripts/prepublish_check.py --name 你的真名   # 追加个人标识关键词（可多次）
python scripts/prepublish_check.py --json           # 机器可读
```

- **在 git 仓库里**：以 `git ls-files`（**实际会被提交的文件**）为准，并用 `git check-ignore` 判断是否被忽略。
- **不在 git 仓库里**（干净副本）：遍历文件系统。
- 三档结论：`BLOCKER`（绝不能发布）/ `WARN`（人工确认）/ 说明。**退出码非 0 就别上传。**
- 当前结果：**0 BLOCKER**，WARN 都是"测试范围内的假密钥"与文档里的 `<账号>` 占位符（已人工确认）。

> ★ **2026-10-03 修正（第十八轮）**：`--name` 命中**从 WARN 改成了 BLOCKER**。
> 原因是真实事故：`docs/next-electron-and-github.md` 里写着贡献者的真账号名，
> 而它当时只报 WARN，于是"闸门通过"却带着真名准备发布。
> `--name` 是你亲口声明"这是我的真名"的输入 —— 它出现就说明有使用痕迹，必须拦住。
> 另外 BLOCKER 的**措辞里不会复述那个真名**（否则为了排掉痕迹的过程又把它写了一遍）。
>
> ★ 还有一个坑：`--root <干净副本>` 时它是**遍历文件系统**的，
> 而 `robocopy` 之类的复制**不认 `.gitignore`** —— 被忽略的运行期数据（`data/*.png`、
> `data/*_report.*`）会被一起复制过去，于是报出一堆假 BLOCKER（本轮实测 13 条）。
> 要么按"复制真实会提交的文件"来造副本，要么知道这些是复制方式的产物：
> ```powershell
> git ls-files -co --exclude-standard   # 这才是 git add -A 真正会包含的文件
> ```

### 2.3 ★ 但**历史里还有**——所以不要直接 push 现有仓库

个人截图与用户名虽然已经停止跟踪，但它们**在已有的提交历史中**（`git log` 里那几个"完成！/OK"）。
直接把这个仓库 push 上去，历史照样能被翻出来。**推荐做法：生成一份零历史的干净副本**（不动原仓库）：

```powershell
# 0) 先确认当前工作区是干净的（本次改动尚未提交时，先自己提交一次，或按下面 robocopy 直接复制工作区）
cd E:\VSCode\

# 1) 复制出一份"干净副本"（排除 .git / 虚拟环境 / 运行期数据 / 个人暂存目录）
$src = 'E:\VSCode\hetero-narrative-engine'
$dst = 'E:\VSCode\hetero-narrative-engine-clean'
robocopy $src $dst /E `
  /XD "$src\.git" "$src\.venv" "$src\data\chroma" "$src\data\logs" "$src\data\exports" `
      "$src\.dsh-drop" "$src\node_modules" "$src\__pycache__" `
  /XF ".env" `
  /NFL /NDL /NJH /NJS

# 2) 在副本里跑闸门（必须 0 BLOCKER；这里没有 .git，所以它会遍历文件系统）
cd $dst
python ..\hetero-narrative-engine\scripts\prepublish_check.py --root . --name 你的真名

# 3) 全新仓库：只有一个初始提交，历史干净
git init
git add -A
git commit -m "云梦枢：异构大模型交互式叙事引擎（首次公开）"
git branch -M main
git remote add origin https://github.com/<你的账号>/<仓库名>.git
git push -u origin main
```

> 为什么用 robocopy 而不是 `git clone`／`git archive`：**只有复制文件系统才能同时排除
> 未提交改动、被忽略的运行期数据与个人目录**；`git archive HEAD` 会漏掉还没提交的改动。

### 2.4 上传前还需**人工**看一眼的四件事（脚本查不出）

1. **截图里有没有真实内容**：`docs/*.png` 都是**探针假账号**跑出来的（探针角色 / 探针卡），
   但请扫一眼确认没有你真实角色卡的标题、会话内容。
2. **`README.md` 里的链接与命令**：有没有指向本机路径的（已清过一轮，仍值得扫一眼）。
3. **`.env.example`**：只应有占位符（`your-password` 之类），不能有真实口令。
4. **许可证与仓库信息**：要不要加 `LICENSE`（毕设通常 MIT 或"保留所有权利"）、
   仓库描述、Topics；论文/学校若有"代码公开需注明"的要求，写进 README 顶部。

---

## 3. Electron 打包：新对话的主线任务

### 3.1 先认清难点（别按"纯前端项目"的思路开工）

本项目**不是**一个静态网站：它需要一个跑着的 Python 后端（FastAPI）+ 一个 MySQL 数据库 +
一个本地向量库目录。所以"Electron 打包"有三种量级的做法：

| 路线 | 做法 | 优点 | 代价 |
|---|---|---|---|
| **A. 壳 + 外部后端**（最轻） | Electron 只当浏览器壳：启动时检查本地 8000 端口，没有就提示用户按文档启动后端 | 一天能做完，不改后端 | 用户仍需装 Python/MySQL —— **不像"桌面软件"** |
| **B. 壳 + 内置后端 sidecar**（推荐） | 用 **PyInstaller** 把后端打成 `backend.exe`，Electron 启动时拉起它（随机端口 + `/health` 探活），退出时杀掉 | 用户只需装 MySQL（或便携版） | 打包体积大（含 chromadb/onnx 模型，可能 300MB+）；首次打包要解决依赖收集 |
| **C. 全内置**（最重） | B + 便携 MySQL（或改用 SQLite 方言）+ 预置嵌入模型 | 真正的"双击即用" | 工作量以周计；MySQL 便携化与数据目录规划都要重做 |

> **建议**：先做 **A**（把壳、数据目录、启动流程跑通并交付一版），再演进到 **B**。
> 每一步都保持"后端仍能单独用命令行启动"——这样探针/冒烟测试与现有验收方式全都继续有效。

### 3.2 具体待办（可勾选）

**阶段 1：壳与启动流程（A）**
- [ ] 新建 `desktop/`（**不要**把 Electron 混进 `web/`）：`package.json`、`src/main.js`、`src/preload.js`
- [ ] 主进程：选**空闲端口**（别写死 8000）→ 拉起后端 → 轮询 `/health` 直到就绪 → `loadURL`
- [ ] 后端进程管理：退出/崩溃时清理子进程；日志写到 `app.getPath('userData')/logs`
- [ ] 窗口：`contextIsolation: true`、`nodeIntegration: false`（**必须**，这个项目的安全立场是"不执行第三方代码"）
- [ ] 失败路径要**说清楚**：健康检查超时 → 显示"后端没起来 + 日志路径 + 常见原因（MySQL 没启动/端口占用）"
- [ ] 菜单与快捷键：刷新、打开数据目录、打开日志、关于（版本 + 图标）

**阶段 2：打包后端（B）**
- [ ] `PyInstaller` 打 `app.main:app`（`--hidden-import` 处理 uvicorn/chromadb/onnxruntime 之类的动态导入）
- [ ] 关键坑预判：**chromadb 的 onnx 模型缓存路径**（默认在用户 `~/.cache/chroma`）要能随包走或在首启时下载
- [ ] 后端端口与 `HNE_*` 环境变量由 Electron 注入（`--env` / 临时 `.env` 写到 userData，**不要**写进安装目录）
- [ ] 首启向导：让用户填 MySQL 连接与 JWT 密钥，写进 userData 下的 `.env`（**不要**打包任何 `.env`）

**阶段 3：安装包与图标**
- [ ] 图标：用 `assets/icon/logo-1024.png` 生成 `build/icon.ico`（Windows）与 `.icns`（macOS）；
      `electron-builder` 的 `build.icon` 指向它
- [ ] `electron-builder` 出 Windows 安装包（NSIS）；安装目录只放程序，**数据目录一律在 userData**
- [ ] 卸载不删用户数据（数据目录与安装目录分开的意义就在这）

### 3.3 必须遵守的项目既有约定

- 前端**零构建**：不要在 Electron 里引入打包器改前端产物；`web/` 仍由后端 `/console` 伺服，
  Electron 只加载那个 URL（`file://` 加载会遇到模块/相对路径/CORS 一堆问题，别走那条路）。
- **不改动后端契约**：Electron 只是壳，接口、SSE、探针断言都不该受影响。
- 打包产物**不入库**：`node_modules/`、`dist_electron/`、`out/`、`release/` 已在 `.gitignore`。

### 3.4 Electron 侧的验收

现有四件套（pytest / smoke / ui_probe / benchmark）在改动后端时照旧要跑。
Electron 本身是**手工点测清单**（新会话里请先把它写进 `docs/handoff.md` 再动手）：

- [ ] 双击安装 → 首次启动能进控制台（或给出可操作的报错）
- [ ] 关窗/强杀进程后，后端子进程**不残留**（`Get-Process python` 核对）
- [ ] 端口被占用时能换端口启动
- [ ] 数据全在 userData：卸载重装后会话/角色卡还在
- [ ] 图标（任务栏/开始菜单/窗口）正确，不是默认 Electron 图标

---

## 4. 命令速查

```powershell
# 跑服务（改 app/** 后必须重启）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 验收四件套（串行跑，别并行，都会碰数据库）
.\.venv\Scripts\python.exe -m pytest -q                      # 870 passed
.\.venv\Scripts\python.exe scripts\smoke_test.py             # 184 / 0
.\.venv\Scripts\python.exe scripts\ui_probe.py               # 147 项 0 失败（会自动起假模型+浏览器）
.\.venv\Scripts\python.exe scripts\benchmark.py ; .\.venv\Scripts\python.exe scripts\benchmark.py --from-db
.\.venv\Scripts\python.exe scripts\token_accuracy.py

# 上传前闸门
.\.venv\Scripts\python.exe scripts\prepublish_check.py --name 你的真名
```

---

## 5. 已知坑（都踩过，别再踩）

| 坑 | 说明 |
|---|---|
| Windows PowerShell 的 `**` 通配 | 5.1 里 `app/**/*.py` 只匹配一层；查引用请用 **AST 解析**，别用通配 |
| `.ps1` 脚本中文乱码 | 必须带 **UTF-8 BOM**（否则 PS 5.1 按 ANSI 读） |
| `Add-Type` 用 System.Drawing | 必须 `-ReferencedAssemblies System.Drawing` |
| 控制台编码 | Windows 默认 GBK，打印 `✓/✗` 会崩；脚本里用 ASCII 标记或 `reconfigure(encoding="utf-8")` |
| 端口残留 | 改完后端要重启；`Get-NetTCPConnection -LocalPort 8000 -State Listen` 查明后再 `Stop-Process` |
| 探针/冒烟/基准并行跑 | 会互相抢数据库与端口的，**串行**跑 |
| 主题改了看不到变化 | 内置插件是"添加时拷贝"，去插件页点「**更新到最新**」（目录会显示`有更新`） |
| Electron 直接 `loadFile(index.html)` | 会踩模块/相对路径/CORS；**始终加载后端 URL** |

---

## 6. 新会话开始时的检查表

1. 读本文件 + `docs/overview.md`（10 分钟能读完，别通读 `handoff.md`）。
2. 跑一次四件套（确认基线是绿的：870 / 184 / 147 / benchmark 0）。
3. 跑 `scripts/prepublish_check.py --name <真名>`，确认 0 BLOCKER。
4. 动 Electron 之前，先在 `docs/handoff.md` 追加一节写清"这一轮要做什么、验收标准是什么"。
5. 打包产物与 `node_modules` 永远不进库；任何"要排除"的东西，**先备份到仓库外，再排除**。
