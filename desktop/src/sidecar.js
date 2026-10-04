'use strict';

/**
 * 选择"用哪个后端"：打包好的 sidecar（`backend.exe`）还是本机 Python。
 *
 * ==================== 为什么需要这一层 ====================
 * 阶段 1 的壳只能拉起 `python -m uvicorn`，所以用户必须自备 Python 与依赖。
 * 阶段 2 把后端用 PyInstaller 打成 `backend.exe`：**有它就用它，没有就退回 Python**。
 * 这个"退回"很重要 —— 开发机上不会有 backend.exe（那是打包产物），
 * 而如果只有打包版能跑，开发与四件套验收就没法在同一份代码上进行了。
 *
 * ★ 选了什么必须**如实回报**（reason）：用户看到的报错/日志里要能分辨
 *   "我用的是打包后端"还是"我在用你机器上的 Python"，否则排查会走错方向。
 */

const fs = require('node:fs');
const path = require('node:path');

/** 打包产物的默认名字（PyInstaller 的 --name 要保持一致）。 */
const SIDECAR_NAME = 'backend';

/**
 * 候选的 sidecar 可执行文件路径（按优先级）。
 *
 * 为什么要这么多位置：开发态、`electron-builder` 打包后、以及"用户手动把 exe 放到旁边"
 * 三种情况下文件所在的地方都不一样；缺一个就会出现"明明放了却找不到"。
 *
 * @param {object} options
 * @param {string} options.desktopDir   `desktop/`
 * @param {string} [options.resourcesPath] 打包后的 resources 目录（Electron 的 process.resourcesPath）
 * @param {string} [options.execDir]    可执行文件所在目录（打包后是安装目录）
 * @param {string} [options.appRoot]    仓库根
 * @returns {string[]}
 */
function sidecarCandidates(options) {
  const platform = options.platform || process.platform;
  const exeName = platform === 'win32' ? `${SIDECAR_NAME}.exe` : SIDECAR_NAME;

  const candidates = [];
  if (options.explicit) candidates.push(options.explicit);
  if (options.resourcesPath) {
    // electron-builder 的 extraResources 会进这里
    candidates.push(path.join(options.resourcesPath, 'backend', exeName));
    candidates.push(path.join(options.resourcesPath, exeName));
  }
  if (options.execDir) candidates.push(path.join(options.execDir, exeName));
  if (options.desktopDir) {
    // 开发态：构建脚本把产物放在 desktop/dist/backend/ 下
    candidates.push(path.join(options.desktopDir, 'dist', 'backend', exeName));
    candidates.push(path.join(options.desktopDir, 'build', 'backend', exeName));
  }
  if (options.appRoot) candidates.push(path.join(options.appRoot, exeName));

  // 去重但保持顺序（第一个命中的优先）
  const seen = new Set();
  return candidates.filter((item) => {
    const key = item.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * 决定这次用什么跑后端（只管 sidecar 与"显式指定"这两件事）。
 *
 * ★ 契约（这条曾经写错过，值得写清楚）：
 *   本函数**不负责探测 Python**，也不该决定"要不要回退"。
 *   它只回答"有没有可用的打包后端"：
 *     · kind='sidecar' → 用它；
 *     · kind='none' + explicitSpecified=true → **用户显式指定了但没有**，调用方必须
 *       报错停下，**不许**改去探测/使用别的后端（否则就是静默降级）；
 *     · kind='none' + explicitSpecified=false → 没指定也没找到，调用方自行回退。
 *
 *   为什么第一版是错的：我在 `chooseBackend` 里加了 `python` 参数，并用
 *   "`kind==='none'` 且 reason 以 HNE_DESKTOP_BACKEND 开头"来判断"显式指定失败"。
 *   但调用方（main.js）在探测 Python **之前**就调用了它，传的是 `python: null` ——
 *   于是显式指定失败时函数走的是"python 为空"那条分支，**根本不会**返回那个 reason，
 *   结果主进程转而去探测 Python 并成功启动。用户明明指定了后端、日志里却写着
 *   "使用打包后端 backend.exe"，两件事对不上（实测抓到的）。
 *   现在把这个判据做成显式字段 `explicitSpecified`，不再依赖字符串前缀这种间接信号。
 *
 * @param {object} options
 * @param {string} [options.explicit]      `HNE_DESKTOP_BACKEND`（用户显式指定，最高优先级）
 * @param {string[]} options.candidates    sidecar 候选路径
 * @param {(p: string) => boolean} [options.exists]
 * @returns {{ kind: 'sidecar'|'none', command: string|null, reason: string,
 *             explicitSpecified: boolean, checked: string[] }}
 */
function chooseBackend(options) {
  const exists = options.exists || ((p) => {
    try {
      return fs.statSync(p).isFile();
    } catch {
      return false;
    }
  });
  const explicit = options.explicit && String(options.explicit).trim()
    ? String(options.explicit).trim()
    : null;
  const candidates = options.candidates || [];
  const checked = [];

  for (const candidate of candidates) {
    checked.push(candidate);
    if (exists(candidate)) {
      return {
        kind: 'sidecar',
        command: candidate,
        reason: explicit && candidate === explicit
          ? `使用 HNE_DESKTOP_BACKEND 指定的打包后端：${candidate}`
          : `使用打包后端 backend.exe（无需本机 Python）：${candidate}`,
        explicitSpecified: Boolean(explicit),
        checked,
      };
    }
  }

  if (explicit) {
    // ★ 显式指定了却找不到 → 调用方必须失败退出，**不得**回退。
    return {
      kind: 'none',
      command: null,
      reason: `HNE_DESKTOP_BACKEND 指定的后端不存在：${explicit}`,
      explicitSpecified: true,
      checked,
    };
  }

  return {
    kind: 'none',
    command: null,
    reason: '没有找到打包后端 backend.exe',
    explicitSpecified: false,
    checked,
  };
}

/**
 * 组装 sidecar 的启动参数。
 *
 * ★ sidecar 的契约（由 `desktop/backend_entry.py` 实现）：
 *   `--host` / `--port` 与 uvicorn 同名同义，所以命令行与其他消费者一致；
 *   `--data-dir` / `--log-dir` 指向 **userData**（安装目录之外），
 *   这样"卸载重装不丢数据"对打包版同样成立。
 *
 * @param {object} options
 * @param {number} options.port
 * @param {string} [options.host]
 * @param {string} [options.dataDir]
 * @param {string} [options.logDir]
 * @returns {string[]}
 */
function sidecarArgs(options) {
  const args = ['--host', options.host || '127.0.0.1', '--port', String(options.port)];
  if (options.dataDir) args.push('--data-dir', options.dataDir);
  if (options.logDir) args.push('--log-dir', options.logDir);
  return args;
}

module.exports = { SIDECAR_NAME, sidecarCandidates, chooseBackend, sidecarArgs };
