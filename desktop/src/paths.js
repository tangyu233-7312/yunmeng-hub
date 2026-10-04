'use strict';

/**
 * 目录与路径解析。
 *
 * ★ 设计原则（来自本轮红线）：**数据目录与安装目录分开**。
 *   · 安装目录：只在开发/打包时存在，里面是程序本身；
 *   · userData ：Electron 给每个应用划的"用户数据目录"，日志、首启写入的配置都放这里；
 *   这样"卸载重装"不会碰到用户的会话与角色卡。
 *
 * ★ 本文件不 import electron：`baseDir` / `appRoot` / `userDataDir` 全部由调用方注入。
 *   于是它可以被 `node --test` 直接测（传临时目录即可）。
 */

const fs = require('node:fs');
const path = require('node:path');

/**
 * 解析一套路径。
 *
 * @param {object} options
 * @param {string} options.baseDir     本文件所在目录（`desktop/src`）——用于定位仓库根、图标等
 * @param {string} options.appRoot     仓库根（含 app/ 与 web/ 的那一层）
 * @param {string} options.userDataDir Electron 的 userData
 * @param {boolean} [options.packaged] 这次跑的是"装出来的那份"吗（判据见 packaging.js）
 * @returns {object} 一组绝对路径
 */
function resolvePaths(options) {
  const baseDir = options.baseDir;
  const appRoot = options.appRoot;
  const userDataDir = options.userDataDir;
  const packaged = Boolean(options.packaged);

  const desktopDir = path.resolve(baseDir, '..');
  const logsDir = path.join(userDataDir, 'logs');
  // ★ 打包版（backend.exe）的向量库与模型缓存放这里：**安装目录之外**。
  //   非打包版不用它（照旧读 .env），所以这个目录只会被 sidecar 用到。
  const userDataDataDir = path.join(userDataDir, 'data');
  // ★ 后端的真实数据目录：开发态是仓库根的 `data/`（`.env` 里的相对路径就指它），
  //   打包态是 userData 下的 `data/`（见 userDataDataDir）。
  //   以前这里写死 `appRoot/data` —— 开发态对，但**打包态是错的**：
  //   打包后 appRoot 是安装目录，于是"关于"里显示的数据目录指向一个
  //   根本不会被写入的位置（用户照着去看，什么也没有）。
  const dataDir = packaged ? userDataDataDir : path.join(appRoot, 'data');
  // ★★ `dataRoot` 是**数据根**：后端会在它下面自己拼 `data/app.sqlite3` 与
  //   `config/.secrets.env`（见 app/core/config.py 的 sqlite_file、
  //   app/db/bootstrap.py 的 secrets_file）。所以它 = dataDir 的父级：
  //     开发态 → 仓库根（数据落在 <仓库根>\data，与引入桌面壳之前完全一致）
  //     打包态 → userData（数据落在 <userData>\data，安装目录之外）
  //   ★ 它与 `dataDir` **不是一个东西**，本轮就因为把两者混用，
  //     在打包产物里建出了 `<userData>\data\data\app.sqlite3`（多一层 data）。
  //     这两个名字像、语义差一层，所以各自都留一句话。
  const dataRoot = packaged ? userDataDir : appRoot;

  return {
    baseDir,
    desktopDir,
    appRoot,
    packaged,
    userDataDir,
    userDataDataDir,
    logsDir,
    backendLogFile: path.join(logsDir, 'backend.log'),
    desktopLogFile: path.join(logsDir, 'desktop.log'),
    // ★ 后端的 .env 在仓库根（已被 .gitignore 忽略，绝不入库）
    envFile: path.join(appRoot, '.env'),
    envExampleFile: path.join(appRoot, '.env.example'),
    /**
     * ★ 首启向导写出来的配置：**userData 下**，与安装目录和仓库都无关。
     *   打包版用它（因为安装目录里不该带 .env，也不能假设用户在仓库里跑）；
     *   开发态优先用仓库根的 .env，让现有开发/验收流程一字不变。
     */
    userEnvFile: path.join(userDataDir, 'config', '.env'),
    setupPage: path.join(baseDir, 'setup.html'),
    // 后端的数据目录（打包态在 userData 下，开发态在仓库根 —— 见上面的 dataDir）
    dataDir,
    // 数据**根**（相对路径的基准；见上面的 dataRoot）
    dataRoot,
    // 页面引用的图标 / 打包用的图标
    runtimeIcon: path.join(appRoot, 'web', 'img', 'logo-256.png'),
    packagedIcon: path.join(desktopDir, 'build', 'icon.ico'),
    loadingPage: path.join(baseDir, 'loading.html'),
    version: null,
  };
}

/** 目录不存在就建（日志目录必须能写，否则后面所有诊断都没了）。 */
function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

module.exports = { resolvePaths, ensureDir };
