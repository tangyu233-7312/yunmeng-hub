'use strict';

/**
 * 打包形态（electron-builder 装出来的那份）与开发形态的**差别**，以及由此必须改的判断。
 *
 * ==================== 为什么要把这几行单独放一个文件 ====================
 * 这些判断平时在开发态都是"反过来"的：开发态**允许**读仓库根的 `.env`、
 * 安装目录就在仓库里；打包态**绝不允许**（安装目录里不该有 `.env`，
 * 而且那里的 `.env` 会是**别人机器上的一份**，读它等于读了不相干的凭据）。
 *
 * 把判断留在 `main.js` 里就只能靠"装一遍试试"来验证；抽成纯函数后
 * `node --test` 能直接钉住，不必真的打包。
 * 同理，`electron-builder` 的配置也从这里生成 —— 于是"配置里少写了一条"
 * 也能在自测里被发现，而不是等打完包才看出问题。
 */

const fs = require('node:fs');
const path = require('node:path');

/** `electron-builder` 把应用代码打成这个文件放在 resources/ 下。 */
const APP_ARCHIVE = 'app.asar';

/**
 * 这次跑的是"装出来的那份"，还是仓库里的开发副本？
 *
 * ★ 为什么不用 `app.isPackaged` 当**唯一**判据：
 *   它来自 Electron 自己的判断，在自测里没法伪造。这里需要"假装打包态"来测
 *   `repoFallbackAllowed()`，所以判据要能被注入。
 *   `resources/app.asar` 是打包形态在磁盘上**唯一确定**的标记：
 *   electron-builder 无论出 NSIS 还是免安装包，都会生成它；
 *   而开发态（`electron .`）跑的 resources 目录里根本没有它。
 *
 * @param {object} options
 * @param {string} [options.resourcesPath]  `process.resourcesPath`
 * @param {boolean} [options.isPackaged]    `app.isPackaged`
 * @param {(p: string) => boolean} [options.exists]
 * @returns {boolean}
 */
function isPackagedLayout(options = {}) {
  if (options.isPackaged) return true; // Electron 说是打包态，就一定是
  if (!options.resourcesPath) return false;
  const exists = options.exists || ((p) => {
    try {
      return fs.existsSync(p);
    } catch {
      return false;
    }
  });
  return exists(path.join(options.resourcesPath, APP_ARCHIVE));
}

/**
 * 允许"仓库根 `.env` 兜底"吗？
 *
 * ★ 开发态：允许。`npm start` 直接用仓库根的配置，现有开发与验收流程一字不变。
 * ★ 打包态：**绝不**。理由有两条，每条都足以否决：
 *   ① 安装目录（`resources/` 或安装根目录）不应该、也常常**写不进去**
 *      —— 向 `C:\Program Files\...` 写配置会直接报 EPERM，用户会以为程序坏了；
 *   ② 更严重的是**安全**：如果那份安装目录里恰好有一份 `.env`（打包时误带、
 *      或用户自己复制进去的），应用就会拿**别人的**数据库口令与签名密钥去连库。
 *      这正是"绝不把 .env 打进安装包"那条红线在**读取侧**的对应约束
 *      —— 光保证"不打包"还不够，还得保证"打包态不去读"。
 *
 * @param {object} options
 * @param {boolean} options.packaged
 * @param {boolean} [options.strictConfig]  `HNE_DESKTOP_STRICT_CONFIG=1`
 * @returns {boolean}
 */
function allowRepoFallback(options = {}) {
  if (options.strictConfig) return false;
  return !options.packaged;
}

/**
 * 生成 `electron-builder` 的配置片段。
 *
 * ★ 为什么要有这个函数：安装包的配置里有一条**红线级**的项
 *   （`deleteAppDataOnUninstall: false` —— 卸载不删用户数据）。
 *   它写在 package.json 里没人会去复查，写错了也只会在"用户卸载后再装"时
 *   才发现数据没了 —— 那时候已经来不及。放成纯函数就能用自测钉住。
 *
 * @param {object} [options]
 * @param {string} [options.backendDir] sidecar 产物目录（默认 desktop/dist/backend）
 * @param {string} [options.iconPath]   图标（默认 desktop/build/icon.ico）
 * @param {string} [options.outputDir]  安装包输出目录（默认仓库根的 release/）
 * @returns {object}
 */
function electronBuilderConfig(options = {}) {
  const desktopDir = options.desktopDir || __dirname;
  const dir = path.resolve(desktopDir, '..');
  const backendDir = options.backendDir || path.join(dir, 'desktop', 'dist', 'backend');
  const iconPath = options.iconPath || path.join(dir, 'desktop', 'build', 'icon.ico');
  const outputDir = options.outputDir || path.join(dir, 'release');
  // ★ electron-builder 默认会再去 GitHub 下一份 Electron zip（约 136MB），
  //   而 `npm install` 早就把**同一份**解压到 node_modules/electron/dist 了。
  //   实测本机连 GitHub 的 CDN 只有约 0.1MB/s，4 分钟才走到 3.67MB ——
  //   于是"打个包"要等十几分钟，还随时可能断。
  //   指到已装好的那份，既省这一次下载，也让打包在离线下可重复。
  const electronDist = options.electronDist || path.join(desktopDir, 'node_modules', 'electron', 'dist');

  return {
    appId: 'com.yunmeng.hub',
    productName: '云梦枢',

    // ★ 输出目录在仓库根的 `release/`（已被 .gitignore 忽略）：
    //   安装包 200MB+ 绝不能进仓库，也不该混进 desktop/dist（那是 sidecar 的目录）。
    directories: { output: outputDir },

    // ★ 只把壳自己的代码放进 asar：`src/**` + package.json。
    //   不写这个白名单的话，desktop/test/ 也会被打进去（里面还有写死的主机路径），
    //   而它们对用户毫无用处。用白名单而不是"排除 test/scripts"，
    //   是因为"以后新增一个目录"时白名单默认**不带**它，黑名单默认**带**它。
    files: ['src/**/*', 'package.json'],

    // 用本机已装好的 Electron，不再去 GitHub 下一份（理由见上面的 electronDist 注释）
    electronDist,

    extraResources: [
      {
        // sidecar：`desktop/dist/backend/`（backend.exe + _internal/）
        // → 安装后的 `resources/backend/`
        // （这个布局必须与 src/sidecar.js 里的候选路径一致，自测会钉住）
        from: backendDir,
        to: 'backend',
        filter: ['**/*'],
      },
    ],

    win: {
      icon: iconPath,
      target: [{ target: 'nsis', arch: ['x64'] }],
      // ★ 刻意**不签名**：签名需要用户自己的代码签名证书（有费用）。
      //   不签名的后果要如实写进文档：SmartScreen 会提示"未知发布者"。
      //   这里不做任何"绕过提示"的事，也不假称已签名。
    },

    nsis: {
      // 一步到位的安装（不弹向导）会让用户连装到哪都不知道；这里给选择权
      oneClick: false,
      perMachine: false, // 只装给当前用户：不需要管理员权限，也不动系统目录
      allowToChangeInstallationDirectory: true,
      createDesktopShortcut: true,
      createStartMenuShortcut: true,
      shortcutName: '云梦枢',
      // ★★ 红线：卸载**不删**用户数据（会话、角色卡、世界书、向量库都在
      //    userData 下，与安装目录无关）。electron-builder 的默认值是 false，
      //   这里显式写出来，并由自测钉住 —— 免得日后"顺手"改成 true。
      deleteAppDataOnUninstall: false,
    },
  };
}

module.exports = {
  APP_ARCHIVE,
  isPackagedLayout,
  allowRepoFallback,
  electronBuilderConfig,
};
