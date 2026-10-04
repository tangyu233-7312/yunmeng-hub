'use strict';

/**
 * 后端启动配置的解析：读 `.env`、找 Python 解释器、决定用哪个端口。
 *
 * ★ 为什么自己解析 `.env` 而不用 `dotenv`：
 *   本项目的桌面壳**刻意零运行时依赖**（`web/` 是零构建，`desktop/` 是零 runtime 依赖，
 *   只有 electron 一个 devDependency）。这里只需要"KEY=VALUE + 跳过注释"，
 *   引一个包反而多一份供应链与升级负担。
 *
 * ★ 为什么必须读 `.env`：后端用 pydantic-settings 读它（`HNE_*` 前缀）。
 *   Electron 拉起子进程时，`.env` 里的 MySQL 口令 / JWT 密钥必须能被后端看见 ——
 *   否则后端起得来、连不上库，而报错会发生在很后面，很难定位。
 */

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

/**
 * 解析 .env 文本（只做 KEY=VALUE，够用且不会误解复杂语法）。
 *
 * @param {string} text
 * @returns {Record<string,string>}
 */
function parseEnvText(text) {
  const out = {};
  // ★ 必须先去 BOM：用 Notepad / PowerShell 保存过的 .env 常常带 U+FEFF，
  //   它会粘在**第一行的键名**上，让 `^[A-Za-z_]` 判定失败 —— 表现是
  //   "明明配了第一项却不生效"，而且只影响第一行，非常难查（本文件的测试就抓到了它）。
  const source = String(text).replace(/^\uFEFF/, '');
  for (const rawLine of source.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    let value = line.slice(eq + 1).trim();
    // 去掉成对的引号（只去一层，且必须成对，避免把 "a'b 误伤）
    if (value.length >= 2
      && ((value.startsWith('"') && value.endsWith('"'))
        || (value.startsWith("'") && value.endsWith("'")))) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

/** 读 .env（不存在就返回空对象，不报错 —— 首次运行还没配 .env 是正常状态）。 */
function readEnvFile(envFile) {
  try {
    return parseEnvText(fs.readFileSync(envFile, 'utf8'));
  } catch (error) {
    if (error && error.code === 'ENOENT') return {};
    throw error;
  }
}

/**
 * 找一个"装了 uvicorn"的 Python 解释器。
 *
 * ★ 为什么用 `-c "import uvicorn, app.main"` 而不是看文件是否存在：
 *   `.venv\Scripts\python.exe` 存在不等于它有依赖（刚 clone 的人可能还没 pip install）。
 *   真的 import 一次是唯一可靠的判据。代价是每次启动多花 ~0.2s —— 值得。
 *
 * ★ 关于"显式指定"（`HNE_DESKTOP_PYTHON` / `HNE_DESKTOP_PYTHON_STRICT`）：
 *   默认行为是"显式指定的没成功，就继续往后试别的解释器"（对多数人更省事）。
 *   但"我明明指定了它，它却悄悄换了另一个"正是本项目反复强调要避免的**静默降级**，
 *   所以提供严格模式：设 `HNE_DESKTOP_PYTHON_STRICT=1` 时，指定的解释器不可用就**直接失败**，
 *   并在报错里说清"你指定的是哪一个、为什么不能用"。
 *
 * @param {object} options
 * @param {string} options.appRoot
 * @param {NodeJS.ProcessEnv} [options.env]
 * @param {typeof spawnSync} [options.spawnSyncImpl] 可注入（测试用）
 * @returns {{ python: string|null, candidates: Array<{path: string, ok: boolean, reason?: string}>, strictExplicit: boolean }}
 */
function detectPython(options) {
  const appRoot = options.appRoot;
  const env = options.env || process.env;
  const spawn = options.spawnSyncImpl || spawnSync;

  const explicit = env.HNE_DESKTOP_PYTHON && env.HNE_DESKTOP_PYTHON.trim()
    ? env.HNE_DESKTOP_PYTHON.trim()
    : null;
  const strictExplicit = Boolean(explicit) && isTruthy(env.HNE_DESKTOP_PYTHON_STRICT);

  /** @type {string[]} */
  const candidates = [];
  // 1) 用户显式指定（最高优先级）：桌面版出问题时让人能一行环境变量指定解释器
  if (explicit) candidates.push(explicit);
  // 2) 项目自带的虚拟环境（本项目的标准做法）
  candidates.push(path.join(appRoot, '.venv', 'Scripts', 'python.exe'));
  candidates.push(path.join(appRoot, '.venv', 'bin', 'python'));
  // 3) PATH 上的 python（没建虚拟环境的人）
  candidates.push('python');
  candidates.push('python3');

  const seen = new Set();
  const results = [];
  for (const candidate of candidates) {
    const key = candidate.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);

    const probe = spawn(candidate, ['-c', 'import uvicorn, app.main'], {
      cwd: appRoot,
      env: { ...env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      encoding: 'utf8',
      timeout: 30000,
      windowsHide: true,
      // ★ 关键：不捕获管道。DSH/CI 等受限环境下"通过管道抓子进程输出"会被拒绝，
      //   而这里的探测**本来就不需要**输出（只看退出码）。
      stdio: ['ignore', 'ignore', 'pipe'],
    });

    if (probe.error) {
      results.push({ path: candidate, ok: false, reason: `无法执行：${probe.error.code || probe.error.message}` });
    } else if (probe.status === 0) {
      results.push({ path: candidate, ok: true });
      return { python: candidate, candidates: results, strictExplicit };
    } else {
      const stderr = (probe.stderr || '').trim().split(/\r?\n/).filter(Boolean).pop();
      results.push({ path: candidate, ok: false, reason: stderr ? truncate(stderr, 160) : `退出码 ${probe.status}` });
    }

    // ★ 严格模式：显式指定的解释器不可用就到此为止，不再往后试 ——
    //   否则用户设了变量却用了别的解释器，而界面上完全看不出来。
    if (strictExplicit && candidate === explicit) {
      return { python: null, candidates: results, strictExplicit };
    }
  }

  return { python: null, candidates: results, strictExplicit };
}

/** 环境变量里的"真"（1/true/yes/on）。 */
function isTruthy(value) {
  if (value === undefined || value === null) return false;
  return ['1', 'true', 'yes', 'on'].includes(String(value).trim().toLowerCase());
}

function truncate(text, max) {
  return text.length <= max ? text : `${text.slice(0, max)}…`;
}

/**
 * 组装子进程的环境变量。
 *
 * ★ 只做"注入"，绝不打印 / 记录这些值（里面可能有 MySQL 口令）。
 *
 * ★ `dataDir` 是**打包版专用**的：PyInstaller 打的 backend.exe 不在仓库目录里跑，
 *   所以它的向量库与模型缓存必须由壳**显式指定**到 userData 下（安装目录之外），
 *   否则数据会跟着安装目录走 —— "卸载重装不丢数据"就失效了。
 *   非打包版（python -m uvicorn）**不注入**这些：它照旧读自己的 `.env`，
 *   行为与本轮之前完全一致（这条很重要，否则会悄悄改变开发/验收时的数据位置）。
 */
function buildBackendEnv(options) {
  const base = options.baseEnv || process.env;
  const fileEnv = options.fileEnv || {};
  const overrides = options.overrides || {};

  return {
    // ★ 顺序有讲究：`.env` 先铺底，**真正的进程环境变量后压**，最后才是我们的显式覆盖。
    //   这正是 pydantic-settings 的优先级（环境变量 > .env）。反过来写的话，
    //   用户"临时想用环境变量试一下某个值"会静默失效 —— 界面上什么都看不出来。
    ...fileEnv,
    ...base,
    ...overrides,
    // ★ Windows 上必须有：后端日志里有大量中文，默认 cp936 会 UnicodeEncodeError，
    //   表现是"后端莫名退出/日志乱码"，而根因只是编码。
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
  };
}

/**
 * 打包版 sidecar 需要的"数据落在 userData"的环境变量。
 *
 * ★ 实测结论（很重要，别再往回加）：**嵌入模型的缓存目录没法用环境变量改**。
 *   ChromaDB 的 `ONNXMiniLM_L6_V2.DOWNLOAD_PATH` 是写死的
 *   `Path.home()/".cache"/"chroma"/"onnx_models"/"all-MiniLM-L6-v2"`，
 *   模块里既没有 `os.environ` 也没有 `os.getenv`（我们直接读过源码确认）。
 *   所以模型只能由后端自己"安装"到那个位置（见 `desktop/backend_entry.py`
 *   的 `ensure_bundled_model`），而不是靠这里注入 `CHROMA_CACHE_DIR` / `XDG_CACHE_HOME`。
 *   第一版我确实注入了这两个变量 —— 它们**毫无作用**，属于"看着在做事"的假动作，已删除。
 *
 * @param {object} options
 * @param {string} options.dataDir  userData 下的数据目录
 * @returns {Record<string,string>}
 */
function sidecarDataEnv(options) {
  return {
    // 向量库实体（长期记忆）落在 userData，安装目录之外
    HNE_CHROMA_PERSIST_DIR: path.join(options.dataDir, 'chroma'),
  };
}

/**
 * 决定这次用哪个端口。
 *
 * @param {object} options
 * @param {number|null} options.preferredPort 想用的端口（来自 HNE_DESKTOP_PORT / --port）
 * @param {number} options.freePort           探测到的空闲端口
 * @param {boolean} [options.preferredIsFree] 想用的端口当前是否空闲
 */
function choosePort(options) {
  const preferred = options.preferredPort;
  if (Number.isInteger(preferred) && preferred > 0 && preferred < 65536 && options.preferredIsFree) {
    return { port: preferred, source: 'preferred' };
  }
  if (Number.isInteger(preferred) && preferred > 0 && preferred < 65536) {
    return { port: options.freePort, source: 'preferred-busy' };
  }
  return { port: options.freePort, source: 'ephemeral' };
}

/**
 * 解析"端口字符串" → 数字，或 null（表示无效/没指定）。
 *
 * ★ 必须先用正则卡"纯数字"：`Number.parseInt('3.5')` 会得到 3、
 *   `parseInt('3306abc')` 会得到 3306 —— 都不该被当成合法端口。
 *   这个坑本项目犯了两次（向导里的 MySQL 端口、这里的桌面端口），
 *   所以在这一处集中修好，两个地方都用它。
 */
function parsePortValue(raw) {
  if (raw === undefined || raw === null) return null;
  const text = String(raw).trim();
  if (!/^\d+$/.test(text)) return null;
  const parsed = Number.parseInt(text, 10);
  return parsed > 0 && parsed < 65536 ? parsed : null;
}

/**
 * 想优先用哪个端口。
 *
 * ★ 默认返回 **null（= 让系统分配一个空闲端口）**，不是 8000。
 *   原因很实际：用户机器上 8000 常常被别的东西占着（开发服务器、别的工具），
 *   而"能用"比"端口好看"重要得多。想固定端口的用户设 `HNE_DESKTOP_PORT`
 *   （或 `.env` 里的 `HNE_PORT`）即可 —— 这时我们才去抢它。
 *
 * ★ 返回值可能是 null 这件事**必须被每个使用点处理**：
 *   本轮就踩了"两处使用、只守了一处"的坑 —— 见 `desktop/src/main.js` 里
 *   `startBackend()` 中那段注释。
 *
 * @param {Record<string,string>} [envFileValues]  .env 里读出来的键值
 * @param {NodeJS.ProcessEnv} [env]                进程环境（HNE_DESKTOP_PORT 优先）
 * @returns {number|null}
 */
function preferredPort(envFileValues, env) {
  const source = env || process.env;
  const raw = source.HNE_DESKTOP_PORT || (envFileValues || {}).HNE_PORT;
  return parsePortValue(raw);
}

module.exports = {
  parseEnvText,
  readEnvFile,
  detectPython,
  isTruthy,
  buildBackendEnv,
  sidecarDataEnv,
  choosePort,
  preferredPort,
  parsePortValue,
  truncate,
};
