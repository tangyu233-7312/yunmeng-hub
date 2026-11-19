# 云梦枢（YunMengHub）

支持自配大模型 API 的交互式叙事应用。将角色卡、世界书、长期记忆和可编辑的提示词预设组合成对话上下文，提供浏览器控制台和 Windows 桌面端。

支持 OpenAI 兼容接口与 Anthropic 协议、流式输出、提示词预览、对话分支、状态栏、自动翻译、VN 立绘和可选生图插件。前端使用原生 HTML / CSS / JavaScript，无构建步骤。

## 下载与使用

[最新 Release](https://github.com/tangyu233-7312/yunmeng-hub/releases/latest) 提供两种 Windows x64 版本：

| 版本 | 文件 | 使用方式 |
|---|---|---|
| Web 版 | `YunMengHub-Web-<版本>.zip` | 解压后双击「启动云梦枢.cmd」，在浏览器中使用 |
| 桌面版 | `YunMengHub-Setup-<版本>.exe` | 安装后从桌面或开始菜单打开 |

发行包内置 Python 后端，默认使用 SQLite，无需另装数据库。首次启动自动建表和生成本机密钥；使用对话、生图或外部嵌入服务时，需要自行配置相应 API。发行包未签名，下载后可按 Release 提供的 SHA256 核对文件。

## 从源码运行

已验证环境：Python 3.13、Windows x64。

```powershell
git clone https://github.com/tangyu233-7312/yunmeng-hub.git YunMengHub
cd YunMengHub
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

打开 [控制台](http://127.0.0.1:8000/console)。接口文档位于 `/docs`、`/redoc`，健康检查位于 `/health`。首次使用本地嵌入模型会下载约 80 MB，缓存后可离线推理。

默认无需创建 `.env`。高级配置可参考 [.env.example](.env.example)；环境变量继续使用 `HNE_` 前缀，以兼容已有配置。SQLite 数据默认位于 `data/`，自动生成的密钥位于 `config/.secrets.env`。

详细安装、MySQL 配置及桌面构建见 [快速开始](docs/快速开始.md) 和 [桌面开发说明](desktop/README.md)。

## 测试与质量检查

原 README 的 **“1121 passed / 3 skipped”缺少对应运行报告，不能追认为已验证事实**。测试套件混合了纯函数、接口、SQLite / Chroma 集成、源码契约和真实 API 测试。通过数量也不代表每个测试都能有效发现错误：本轮故意注入错误，发现并修正了两处会漏报的检查。实际运行记录、变异抽查、跳过原因与验证范围见 [测试与验证](docs/测试与验证.md)。历史论文材料里的数字保留为历史记录。

```powershell
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m pytest -q --junitxml=.pytest-data/results.xml
./.venv/Scripts/python.exe -m ruff check app scripts tests desktop/backend_entry.py desktop/scripts
./.venv/Scripts/python.exe scripts/check_docs.py
cd desktop
npm test
```

默认测试使用隔离数据目录 `.pytest-data/`，不读取开发者 `.env`，真实 LLM 请求须显式传入 `--live`。Chroma 集成测试仍使用真实本地 ONNX 模型，首次运行需要下载模型。仓库包含 Windows CI 配置，运行后端回归、桌面测试和静态检查，并保存 JUnit 报告。

## 文档

| 文档 | 内容 |
|---|---|
| [快速开始](docs/快速开始.md) | 安装、配置、数据位置与开发命令 |
| [测试与验证](docs/测试与验证.md) | 可复现命令、实测记录、测试边界 |
| [项目结构](docs/项目结构.md) | 目录职责、响应格式、数据库结构与迁移 |
| [系统总览](docs/overview.md) | 功能、技术栈与对话数据流 |
| [可视化控制台](docs/可视化控制台.md) | 控制台交互与前端缓存机制 |
| [大模型适配](docs/大模型适配.md) | 协议差异、重试、思考与错误处理 |
| [账号与模型配置](docs/账号与模型配置.md) | 认证、API Key 与模型管理 |
| [提示词预设](docs/提示词预设.md) | 导入、块装配与深度注入 |
| [角色卡与世界书](docs/角色卡与世界书.md) | 卡片、PNG 导入、世界书与主题 |
| [记忆与检索](docs/记忆与检索.md) | 向量检索、关键词触发、总结与记忆锚点 |
| [对话与流式](docs/对话与流式.md) | 会话、SSE、消息编辑与状态块 |
| [扩展功能](docs/扩展功能.md) | 骰子、生图、立绘、翻译与对话分支 |
| [桌面版安装](docs/桌面版安装.md) | 安装、数据目录和验收 |
| [版本与进度](docs/版本与进度.md) | 功能变更记录 |
| [生成参数](docs/generation-params.md) | 参数字段及适用范围 |
| [踩坑精选](docs/踩坑精选.md) | 常见故障与修复依据 |
| [维护记录](docs/维护记录.md) | 本轮清理、兼容范围与验证限制 |

[论文素材](docs/paper-materials.md) 和 [完整开发记录](docs/pitfalls.md) 保留设计背景与历史实验，引用数值前应按对应条件重跑。

## 数据与许可

用户密码以 bcrypt 哈希保存；用户配置的 API Key 经 Fernet 加密后入库。手工配置可保存在 `.env`，自动密钥保存在数据根目录的 `config/.secrets.env`。备份数据库时应同时备份配置与密钥，否则已保存的 API Key 可能无法解密。对话会发送给用户选择的第三方 API 服务商。

本项目为**源码可见、保留权利**，不是开源许可。允许阅读、克隆和本地修改；未经授权不得分发衍生版本或用于商业用途。Release 可用于个人非商业使用。完整许可与免责声明见 [LICENSE](LICENSE)。
