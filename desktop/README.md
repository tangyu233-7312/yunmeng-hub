# 云梦枢 · 桌面版（Electron 壳）

> 这个目录把云梦枢装进一个桌面窗口。**它只是一个壳** —— 页面、接口、SSE、探针断言
> 全都还是原来那一套，后端契约一行都没改。

---

## 0. ★ 首次打开会发生什么

装好之后第一次打开，应用**不会**直接报"数据库连不上" —— 它会先显示一张**首次设置**页，
让你填 MySQL 连接与两个密钥（签名密钥可以点「随机生成」）。填完点「保存并开始」，
它会**真的把后端起起来、逐项检查数据库与向量库**，通过才进控制台。

配置写在哪：**应用数据目录**（Windows 是 `%APPDATA%\云梦枢\config\.env`），
**不在安装目录里** —— 所以**卸载重装不会丢配置**。想改配置：菜单「数据 → 重新运行首次设置」。

> ⚠️ 诚实边界：仍然需要你自己准备 **MySQL**（桌面版不附带数据库）。
> 打包版**不需要** Python（后端是打包好的 `backend.exe`）。
>
> ★ 开发模式（`npm start`）下，如果**仓库根已有 `.env`**，应用会直接用那份、跳过向导 ——
> 这是为了开发方便。想按"用户机的方式"试：设 `HNE_DESKTOP_STRICT_CONFIG=1`。

---

## 1. 先认清它是什么（诚实边界）

云梦枢不是静态网站：控制台由 **FastAPI 后端**伺服（`/console/`），后端还要
**MySQL** 与一个本地的 **ChromaDB 向量库**目录。所以桌面壳做的事情只有四件：

1. 选一个**空闲端口**（不写死 8000）；
2. 拉起后端 —— **优先用打包好的 `backend.exe`**，找不到才退回本机 Python；
3. 轮询 `/health`，就绪后把窗口导航到 `http://127.0.0.1:<端口>/console/`；
4. 退出时把后端子进程**整棵树**带走，不留残余。

> ★ **两种形态，说清楚差别**（壳会在日志里如实写明用的是哪一种）：
>
> | 形态 | 需要用户装什么 | 数据在哪 | 怎么来的 |
> |---|---|---|---|
> | **打包后端**（`backend.exe`） | 只需要 **MySQL** —— 不用装 Python | userData 下（安装目录之外） | `scripts/build_backend.ps1` 构建 |
> | **本机 Python** | Python 依赖 + MySQL | 仓库根的 `data/`（照旧读 `.env`） | 没构建 sidecar 时的自动回退 |
>
> 打包产物**不入库**（体积约 290MB），所以任何一份 clone 下来默认走"本机 Python"那条路。
> 想要免装 Python 的形态，就先跑一次构建脚本 —— 见 §7。
>
> 桌面版的价值在于：不用记命令、不用手敲 URL、出错时有日志路径可看、
> 退出不会留下抢占端口的僵尸进程。

---

## 2. 怎么跑（开发态）

```powershell
# 1) 后端依赖（在仓库根，一次性）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env      # 然后填 HNE_MYSQL_PASSWORD 等

# 2) 桌面壳依赖（只装 electron 一个，一次性）
cd desktop
npm install

# 3) 起桌面版
npm start
```

### 装不上 electron（`npm install` 卡住 / 装完没有 `electron.exe`）

本机实测踩到过两种，**都不是项目代码的问题**，但都会让人以为"代码坏了"：

1. **npm 11 默认不执行安装脚本**，而 Electron 的二进制正是靠 `postinstall` 下载的。
   症状：装完 `node_modules/electron/dist/` 是空的。修法（二选一）：

   ```powershell
   npm rebuild electron --foreground-scripts     # 让它把 postinstall 跑完
   # 或
   node node_modules/electron/install.js
   ```

2. **`extract-zip` 解压到一半被系统中断**（本机实测：75 个条目只落地 `locales/`，
   进程静默结束、退出码还是 0）。修法：换用 PowerShell 自带的解压。
   缓存里的 zip 是完整的、可以直接用：

   ```powershell
   cd desktop
   $zip = Get-ChildItem "$env:LOCALAPPDATA\electron\Cache" -Recurse -Filter 'electron-v*-win32-x64.zip' |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
   $dest = "$env:TEMP\electron-unzip"
   Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
   Expand-Archive -Path $zip.FullName -DestinationPath $dest -Force
   Remove-Item -Recurse -Force node_modules\electron\dist -ErrorAction SilentlyContinue
   Copy-Item -Recurse -Force "$dest\*" node_modules\electron\dist\
   Set-Content -Path node_modules\electron\path.txt -Value 'electron.exe' -NoNewline -Encoding ascii
   .\node_modules\electron\dist\electron.exe --version   # 能打印版本就成功了
   ```

   > 这几条**只影响本机的开发环境**，与仓库里该提交什么无关：
   > `node_modules/` 本来就被 `.gitignore` 忽略。

### 自测（不需要 MySQL、不需要 Electron）

```powershell
cd desktop
npm test        # Node 自带 test runner，138 条用例
```

覆盖的都是"起后端"这条路上真正会出错的地方：

| 测的东西 | 为什么值得测 |
|---|---|
| 空闲端口选择 | 选到被占的端口 → 界面表现为"永远转圈" |
| 健康检查轮询（就绪 / 超时 / 放弃 / 原因变化） | 判错就会"没起来却说好了" |
| **单次探测卡死时整体仍会超时** | 端口被"只接受连接、从不回话"的程序占着时的真实故障 |
| `.env` 解析（含 BOM、引号、值里的 `=`） | 读错一个值 → 表现是"后端没起来"，根因却在这里 |
| 环境变量优先级（进程 > `.env` > 显式注入） | 反了就会"临时用环境变量试一下"静默失效 |
| **后端子进程树清理**（`taskkill /T`） | 杀不干净 → 端口一直被占，下次启动报"数据库连不上" |
| 日志必须落在 userData | 写进安装目录 → "卸载重装不丢数据"悄悄失效 |
| **sidecar 选择**（有就用它、没就回退、显式指定就只认它） | 判错会"用户指定 A、程序用了 B"，而界面上看不出来 |
| Python 探测的严格模式 | 指定了解释器却被悄悄换成另一个，同样是静默降级 |

> 这些用例**刻意不依赖 MySQL**：真起后端的验证由 `scripts/smoke_test.py` 负责（那是它的职责）。
> 单测里用 `node` 冒充解释器，验的是**进程管理**（起得来 / 认得活 / 收得掉）。

---

## 3. 目录结构

```
desktop/
  package.json            依赖 electron@38.8.6 + electron-builder（都锁精确/限定版本）
                          ★ 安装包配置在它的 `build` 段里（真正生效的那份）
  backend_entry.py        ★ 打包后端的入口（PyInstaller 用）：先设环境变量再 import app
  src/
    main.js               主进程：窗口 / 菜单 / 启动流程 / 生命周期
    preload.js            只暴露具名、有限的能力（不暴露 ipcRenderer）
    loading.html          启动中与失败时的界面（file:// 加载，CSP 收紧）
    setup.html            首次设置页（同样 file:// + CSP；字段由 setup-config 定义）
    setup-config.js       首次设置的数据层：字段定义 / .env 读写 / 校验 / 随机密钥
    sidecar.js            决定"用打包后端还是本机 Python"（含"显式指定就只认它"的规则）
    packaging.js          ★ 打包形态与开发形态的差别（安装包配置也从这里生成，便于测）
    paths.js              路径解析（数据目录与安装目录分开；打包态数据落 userData）
    backend-config.js     .env 解析 / Python 探测（含严格模式）/ 环境变量与端口决策
    backend-process.js    子进程启动（python / sidecar 两种模式）、日志接线、**进程树清理**
    net-utils.js          空闲端口选择、健康检查轮询（含单次探测硬超时）
    port-check.js         "这个端口上是不是已经有后端了"
  scripts/
    make-ico.ps1          由 assets/icon/logo-1024.png 生成 build/icon.ico
    backend.spec          PyInstaller 打包描述
    build_backend.ps1     ★ 一键构建打包后端（含深度自检与 .gitignore 体检）
    build_installer.ps1   ★ 一键出 NSIS 安装包（含产物核对，见 §8）
    verify_installer.ps1  ★ 装→首启向导→控制台→卸载不删数据 的自动验收
    verify_installer_cdp.py  上面那个脚本里"填真实表单"的那一步（CDP）
  test/                   Node 自测（npm test，138 条）
  build/icon.ico          生成物（16/24/32/48/64/128/256）——★ 要入库，随包发布
  build/pyinstaller 等    构建中间物 —— 不入库（.gitignore）
  dist/backend/           打包产物（backend.exe + _internal/，约 290MB）—— 不入库
  .build-venv/            打包专用 venv（约 400MB）—— 不入库，与验收用的 .venv 隔离
release/                  安装包产物（仓库根）—— 不入库
```

---

## 4. 刻意不做的事（与项目既有约定一致）

| 不做 | 原因 |
|---|---|
| 用 `file://` 加载控制台 | 前端是**原生 ES Module + importmap**，走 file:// 会踩模块 MIME / 相对路径 / CORS；后端伺服它本来就是对的 |
| 引入打包器改 `web/` | 前端"零构建"是硬约定 |
| 把 `.env` 或密钥打进包 | 配置只在仓库根 / userData；**绝不打包 `.env`** |
| 给窗口开 `nodeIntegration` | `contextIsolation: true` + `sandbox: true`，与"插件从不执行第三方代码"的立场一致 |
| 往 `web/` 里塞 Electron 相关文件 | `web/` 是**静态站点根**，放什么都会被伺服 |

---

## 5. 出错了看哪里

菜单 → **数据** → `打开日志目录`，里面两份日志：

| 文件 | 内容 |
|---|---|
| `backend.log` | 后端的 stdout/stderr（**真正的报错通常在这里**） |
| `desktop.log` | 壳自己的记录：选了哪个端口、用了哪个解释器、健康检查几次通过、怎么收的子进程 |

两者的位置是 Electron 的 `userData/logs`（Windows 一般是
`%APPDATA%\云梦枢\logs`），**在安装目录之外** —— 卸载重装不会丢。

启动页在失败时会直接显示：**日志路径 + 常见原因 + 手动复现命令**，
并给「重试 / 打开日志目录 / 看后端日志」三个按钮。

### 常见原因对照

| 现象 | 先查 |
|---|---|
| 提示"找不到可用的 Python" | 仓库根有没有 `.venv`，且装没装 `requirements.txt`；或用 `HNE_DESKTOP_PYTHON` 指定解释器。**更省事的做法**：构建打包后端（§7），从此不需要 Python |
| 提示"你指定的打包后端（HNE_DESKTOP_BACKEND）不存在" | 检查那个路径；或清掉该变量让它自动查找（先找 `desktop/dist/backend/backend.exe`，找不到再退回本机 Python）。★ 设了它却找不到时**不会**静默改用别的后端，这是刻意的 |
| 提示"后端起进程后立刻退出" | `backend.log` 末尾：多半是 MySQL 没启动，或 `.env` 里的 `HNE_MYSQL_*` 不对 |
| 提示"等待后端就绪超时" | 首次启动要加载本地 ONNX 嵌入模型（打包版已自带，普通版可能联网下载约 90MB），慢是正常的；反复超时看 `backend.log` |
| 想固定端口 | 设环境变量 `HNE_DESKTOP_PORT`，或改 `.env` 里的 `HNE_PORT`。**默认不指定**：直接让系统分配一个空闲端口（避免和别人抢 8000） |
| 想强制只用指定解释器 | `HNE_DESKTOP_PYTHON` + `HNE_DESKTOP_PYTHON_STRICT=1`（不设严格模式时会回退到别的解释器） |
| 想按"用户机"的方式试（跳过仓库 `.env` 兜底） | `HNE_DESKTOP_STRICT_CONFIG=1` |
| 想换一个用户数据目录（测试用） | `HNE_DESKTOP_PROFILE=<目录>` —— 自动化验收靠它跑"全新空配置"的场景 |

---

## 6. 验收：手工点测清单

自动化能覆盖的东西都写进 `npm test`（138 条）与四件套了；下面这些是**必须手点**的。

- [ ] `npm start` 后能进控制台（不是白屏）
- [ ] 启动阶段能看到加载页；故意把 `.env` 改坏后能看到**可操作**的报错
- [ ] 关窗后 `Get-Process python`（或 `backend.exe`）里没有残留的后端进程
- [ ] 强杀主进程（任务管理器结束 Electron）后同样不残留
- [ ] 8000 被占用时自动换端口，且界面/日志如实说明换了
- [ ] 任务栏与窗口图标是云梦枢（不是默认 Electron 图标）
- [ ] 暂停 MySQL 后启动 → 报错指向日志，而不是静默转圈
- [ ] 构建过 sidecar 之后：菜单「帮助 → 关于」里"后端形态"写着**打包后端**

> ★ 「关窗后不残留」这条尤其要在**改了 `main.js` 或 `backend-process.js` 之后**重测：
> 它是这一层唯一"出错了也不报错、只是下次启动莫名失败"的故障。

---

## 7. 打包后端（阶段 2，已完成）

把后端打成 `backend.exe`，用户就**只需要 MySQL、不需要 Python**。

```powershell
pwsh -File scripts/build_backend.ps1            # 常规构建（默认自带嵌入模型）
pwsh -File scripts/build_backend.ps1 -NoModel   # 不带模型（产物小 ~90MB，首次用到长期记忆时联网下载）
pwsh -File scripts/build_backend.ps1 -SkipSelfCheck   # 快速迭代
```

脚本会：① 用**独立的** `desktop/.build-venv`（**不碰验收用的 `.venv`**）；
② 把本地 ONNX 模型暂存进包；③ PyInstaller 构建到 `desktop/dist/backend/`；
④ **自动跑一次深度自检**（起服务 → 检查 `/health` 的**每个组件**都是 ok → 自己退出），
不通过就直接失败。产物约 **290MB**（含 87MB 模型）。

### 三条踩过的坑（都在脚本/spec 注释里）

1. **自检不能只看 HTTP 200**：本项目的 `/health` 在组件降级时**照样返回 200**。
   第一版自检因此把"MySQL 连不上 + 向量库起不来"的产物判成"通过"——
   这正是项目最反对的静默降级。现在逐项检查 `components.*.status`。
2. **`tqdm` 必须显式打进包**：ChromaDB 的 ONNX 嵌入函数在**下载模型**时用它，
   缺了会 `ValueError`，而报错信息里根本看不出跟 tqdm 有关。
3. **`onnxruntime.transformers.*` 要从 hidden imports 里剔掉**：它依赖 `onnx` 包、
   我们只做推理；不剔就会刷几十条 `ERROR: Hidden import ... not found`，
   把真正的告警淹掉（日志一旦不可读，告警等于没有）。

### ★ 嵌入模型的缓存路径：改不了，只能"安装"

ChromaDB 的 `ONNXMiniLM_L6_V2.DOWNLOAD_PATH` 是**写死在源码里**的
`Path.home()/".cache"/"chroma"/"onnx_models"/"all-MiniLM-L6-v2"`，
模块里既没有 `os.environ` 也没有 `os.getenv`（我们直接读过源码确认）。

所以：**不要把模型缓存指到 userData 这件事当成可配置项**（第一版我注入过
`CHROMA_CACHE_DIR` / `XDG_CACHE_HOME` —— 两个都是**毫无作用的假动作**，已删除）。
现在的做法是：模型随包带，后端首启时把它"安装"到上面那个固定位置；
已经存在就什么都不做（见 `backend_entry.py::ensure_bundled_model`）。

### 数据落在哪

打包后端的数据目录由壳通过 `--data-dir` / `--log-dir` 指定到 **userData**
（`%APPDATA%\云梦枢\data`、`...\logs`），**在安装目录之外** —— 卸载重装不丢数据。
非打包模式**不受影响**：它照旧读 `.env`，数据仍在仓库根的 `data/`。

---

## 8. 安装包（阶段 3，已完成）

出一个 Windows x64 的 **NSIS 安装包**（`electron-builder`）。它把壳（Node 代码进 `app.asar`）
与后端（`desktop/dist/backend/` 整份 → `resources/backend/`）装进同一个安装目录。

```powershell
cd desktop
npm install                                       # 装依赖（含 electron-builder）

cd ..
.\.venv\Scripts\python.exe ...                    # （略）先把 MySQL 与 .env 准备好
cd desktop
npm run build:backend                             # ① 先出 sidecar（约 290MB，十几分钟）
npm run build:installer                           # ② 再出安装包 → 仓库根 release\
```

产物（都在仓库根的 `release/`，已被 `.gitignore` 忽略）：

| 产物 | 说明 |
|---|---|
| `云梦枢 Setup 0.1.0.exe` | 安装包本体（双击可装，SHA256 由脚本打印） |
| `win-unpacked\` | 安装包解出来的样子 —— 用来核对内容，不必真装 |

### 自动验收：装出来的那一份

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_installer.ps1 -EnvFile ..\.env
```

它用**临时目录**（`%TEMP%\yunmeng-installer-*`）静默装一遍、用**全新空配置**启动、
通过 CDP 在**真实页面**里调「保存并开始」的入口、核对进了控制台、再静默卸载并确认
**用户数据还在**。全程不碰你真实的 `%APPDATA%\云梦枢` 与仓库 `.env`。

为什么非要"装一遍再验"：开发态跑得好**完全不能**说明安装包是好的 —— 这两件事的差别
正是阶段 3 的全部内容（Node 代码进了 asar、后端换到了 `resources/backend`、
userData 换了位置、`.env` 从仓库根消失）。任何一条对不上，症状都是
"开发时好好的，装完打不开"。

### 关于"没有代码签名"（诚实边界）

这个安装包**没有代码签名** —— 签名要用户自己的代码签名证书（要花钱）。直接后果：
用户第一次运行时 Windows SmartScreen 会拦一下，提示"**未知发布者**"，要点"更多信息 → 仍要运行"。
这不是缺陷，也不做任何"绕过提示"的事，脚本与文档都不假称已签名。

### 卸载

控制面板 →「云梦枢」→ 卸载。卸载只删安装目录，**不动用户数据**：
配置（`%APPDATA%\云梦枢\config\.env`）、会话、角色卡、世界书、向量库、日志都留着。

> ⚠️ 这一条是**红线**，并且在 `desktop/test/packaging.test.js` 里被钉住了
> （`nsis.deleteAppDataOnUninstall` 必须是显式的 `false`）——
> 因为"卸载删数据"是不可逆的，而它写错了只会在"用户卸载后再装"时才发现。

### ★ 打包态不再读安装目录里的 `.env`（本轮补的第二个洞）

"绝不把 `.env` 打进安装包"只做到了**一半**。另一半在**读取侧**：打包后 `appRoot`
就是安装目录，如果那里恰好有一份 `.env`（打包时误带、或用户自己复制进去的），
应用会拿**别人的**数据库口令与签名密钥去连库 —— 而且安装目录常常写不进去
（往 `Program Files` 写配置直接 EPERM）。

所以现在 `packaged === true` 时**一律不做"仓库根兜底"**（判据见 `src/packaging.js`，
由 `test/packaging.test.js` 与验收脚本两处钉住）。打包态的配置只有一个来源：
首启向导写进 userData 的那份。

### 打包产物与 `.gitignore`

`node_modules/`、`release/`、`desktop/dist/`、`desktop/.build-venv/`、
`desktop/build/pyinstaller|model-data|selfcheck-model-cache/` 都不入库；
`build/icon.ico` 是**例外：要入库**（随包发布）。两个脚本结束前都会用
`git check-ignore` 复查这件事 —— 因为 `.gitignore` 里那条 Python 的 `build/`
一旦漏掉前导斜杠，就会**静默**把图标也忽略掉（本轮为此返工三次，
详见 `.gitignore` 里的注释）。
