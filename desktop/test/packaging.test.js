'use strict';

/**
 * `desktop/src/packaging.js` 的自测。
 *
 * ★ 这一层钉住的是**安装包形态**才成立的几条规则。它们平时在开发态都是反的，
 *   所以"开发时跑得好好的"完全不能说明它们是对的 —— 只有自测能。
 *   其中 two 条属于红线：
 *     · 打包态**不许**读安装目录里的 `.env`（否则会拿别人的凭据连库）；
 *     · 卸载**不许**删用户数据（会话、角色卡、世界书没了是不可逆的）。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  APP_ARCHIVE, isPackagedLayout, allowRepoFallback, electronBuilderConfig,
} = require('../src/packaging');

// ------------------------------------------------------------------
//  形态判定
// ------------------------------------------------------------------
test('isPackagedLayout：resources/app.asar 存在 = 打包形态（这是磁盘上唯一确定的标记）', () => {
  const resources = 'C:\\Program Files\\云梦枢\\resources';
  const packaged = isPackagedLayout({
    resourcesPath: resources,
    exists: (p) => p === path.join(resources, APP_ARCHIVE),
  });
  assert.equal(packaged, true);

  // 同一个 resources 目录，但没有 app.asar（= 开发态 `electron .`）→ 不是打包形态
  assert.equal(isPackagedLayout({ resourcesPath: resources, exists: () => false }), false);
});

test('isPackagedLayout：Electron 自己说 isPackaged 时，不看磁盘也认', () => {
  assert.equal(isPackagedLayout({ isPackaged: true, exists: () => false }), true);
});

test('isPackagedLayout：没有 resourcesPath 时安全返回 false（不抛异常）', () => {
  assert.equal(isPackagedLayout({}), false);
  assert.equal(isPackagedLayout(), false);
});

// ------------------------------------------------------------------
//  ★★ 红线：打包态不许读安装目录里的 .env
// ------------------------------------------------------------------
test('★★ 红线：打包态**不许**仓库根/安装目录兜底读 .env', () => {
  // 背景：开发态的 `.env` 兜底很省事，但打包态读它有两个后果 ——
  //   ① 安装目录常常写不进去（向 Program Files 写配置直接 EPERM）；
  //   ② 更严重：那个目录里若有**别人**留下的 .env，应用会拿它的数据库口令
  //      与签名密钥去连库。所以"不把 .env 打进安装包"只是第一半，
  //      第二半是"打包态绝不去读它"。
  assert.equal(allowRepoFallback({ packaged: true }), false);
  assert.equal(allowRepoFallback({ packaged: true, strictConfig: true }), false);
});

test('allowRepoFallback：开发态照旧允许（现有开发与验收流程一字不变）', () => {
  assert.equal(allowRepoFallback({ packaged: false }), true);
  assert.equal(allowRepoFallback({}), true);
});

test('allowRepoFallback：HNE_DESKTOP_STRICT_CONFIG=1 在开发态也能关掉兜底', () => {
  assert.equal(allowRepoFallback({ packaged: false, strictConfig: true }), false);
});

// ------------------------------------------------------------------
//  安装包配置
// ------------------------------------------------------------------
test('electronBuilderConfig：★★ 卸载不删用户数据（红线，必须是显式的 false）', () => {
  const config = electronBuilderConfig({ desktopDir: 'E:\\repo\\desktop' });
  assert.equal(
    config.nsis.deleteAppDataOnUninstall,
    false,
    '卸载删数据是不可逆的：会话、角色卡、世界书、向量库都会没',
  );
});

test('electronBuilderConfig：只装给当前用户（不要求管理员权限）', () => {
  const config = electronBuilderConfig({ desktopDir: 'E:\\repo\\desktop' });
  assert.equal(config.nsis.perMachine, false);
  assert.equal(config.nsis.oneClick, false, '一步装完会让用户不知道装到哪了');
  assert.equal(config.nsis.allowToChangeInstallationDirectory, true);
});

test('electronBuilderConfig：sidecar 作为 extraResources 打进 resources/backend', () => {
  const config = electronBuilderConfig({ desktopDir: 'E:\\repo\\desktop' });
  const entry = config.extraResources.find((r) => r.to === 'backend');
  assert.ok(entry, `extraResources 里必须有 sidecar：${JSON.stringify(config.extraResources)}`);
  assert.equal(entry.from, path.join('E:\\repo', 'desktop', 'dist', 'backend'));
  assert.deepEqual(entry.filter, ['**/*'], 'sidecar 目录里的 _internal/ 必须整份带上');
});

test('★ 接线：extraResources 的落点必须与 sidecar.js 的候选路径一致', () => {
  // 这两处一旦不一致，症状是"装完打开了，却说找不到后端"（而且开发态完全正常）。
  const { sidecarCandidates } = require('../src/sidecar');
  const config = electronBuilderConfig({ desktopDir: 'E:\\repo\\desktop' });
  const to = config.extraResources.find((r) => r.to === 'backend').to;

  const resourcesPath = 'C:\\Users\\u\\AppData\\Local\\Programs\\云梦枢\\resources';
  const candidates = sidecarCandidates({ resourcesPath, platform: 'win32' });
  const expected = path.join(resourcesPath, to, 'backend.exe');
  assert.ok(
    candidates.includes(expected),
    `sidecar.js 必须去 ${expected} 找后端，实际候选：\n${candidates.join('\n')}`,
  );
});

test('electronBuilderConfig：app.asar 里只放壳自己的代码（白名单，不是黑名单）', () => {
  const config = electronBuilderConfig({ desktopDir: 'E:\\repo\\desktop' });
  // 白名单的好处：以后 desktop/ 下新增一个目录，默认**不会**被打进去。
  // 用黑名单（"排除 test/scripts"）就会默认带进去 —— 比如 desktop/test 里的
  // 用例含有写死的主机路径，对用户毫无用处，也不该出现在安装包里。
  assert.deepEqual(config.files, ['src/**/*', 'package.json']);
  assert.ok(!config.files.some((f) => /test|script/i.test(f)));
});

test('electronBuilderConfig：输出到仓库根的 release/（安装包绝不进仓库）', () => {
  const config = electronBuilderConfig({ desktopDir: 'E:\\repo\\desktop' });
  assert.equal(config.directories.output, path.join('E:\\repo', 'release'));
});

test('electronBuilderConfig：Windows x64 + NSIS + 图标', () => {
  const config = electronBuilderConfig({ desktopDir: 'E:\\repo\\desktop' });
  assert.deepEqual(config.win.target, [{ target: 'nsis', arch: ['x64'] }]);
  assert.equal(config.win.icon, path.join('E:\\repo', 'desktop', 'build', 'icon.ico'));
  assert.equal(config.appId, 'com.yunmeng.hub');
  assert.equal(config.productName, '云梦枢');
});

// ------------------------------------------------------------------
//  与真实文件对账：图标真的在，sidecar 的落点真的与 sidecar.js 一致
// ------------------------------------------------------------------
test('★ 对账：desktop/build/icon.ico 真的存在（否则打包会中途失败）', () => {
  const real = path.resolve(__dirname, '..', 'build', 'icon.ico');
  assert.ok(fs.existsSync(real), `缺少 ${real}；先跑 pwsh -File desktop/scripts/make-ico.ps1`);
});

test('★ 对账：package.json 里的 build 配置与 packaging.js 生成的一致', () => {
  // 为什么对账：真正生效的是 package.json 里那份（electron-builder 自己读它），
  // packaging.js 只是"能测的那一份"。两边不一致时**生效的**是 package.json，
  // 于是自测全绿也拦不住一个配错的安装包。这条用例把它们绑在一起。
  //
  // ★ 比的是**语义**而不是字面：electron-builder 把 build 段里的相对路径
  //   都按"desktop 目录"解析（`directories.output: "../release"` 与
  //   `from: "dist/backend"` 都是这个规矩），绝对路径也接受。
  //   所以这里把生成的那份（绝对路径）转成相对 desktop 的形式再比 ——
  //   否则会变成"两种写法里只准用我这一种"，那不是我们要守的约束。
  const pkg = require('../package.json');
  assert.ok(pkg.build, 'package.json 里必须有 build 段（真正生效的安装包配置）');

  const desktopDir = path.resolve(__dirname, '..');
  const generated = electronBuilderConfig({ desktopDir });
  /** 统一成"相对 desktop 的路径"，两种写法就能比了。 */
  const rel = (p) => path.relative(desktopDir, path.resolve(desktopDir, p)).replace(/\\/g, '/');

  assert.equal(rel(pkg.build.directories.output), rel(generated.directories.output),
    'build.directories.output');
  assert.equal(rel(pkg.build.win.icon), rel(generated.win.icon), 'build.win.icon');
  assert.equal(
    rel(pkg.build.extraResources[0].from),
    rel(generated.extraResources[0].from),
    'build.extraResources[0].from',
  );

  // 纯字面量、与 CWD 无关的几项直接比
  for (const key of ['appId', 'productName', 'files', 'nsis']) {
    assert.deepEqual(pkg.build[key], generated[key], `package.json 的 build.${key} 与生成的不一致`);
  }
  // win.target 是字面量，win.icon 是路径（已单独比过）
  assert.deepEqual(pkg.build.win.target, generated.win.target, 'build.win.target');
  // electronDist 也是路径：指向本机已装好的 Electron（不去 GitHub 再下一份）
  assert.equal(rel(pkg.build.electronDist), rel(generated.electronDist), 'build.electronDist');
  // extraResources 除 from 之外的部分
  assert.deepEqual(
    { ...pkg.build.extraResources[0], from: null },
    { ...generated.extraResources[0], from: null },
    'build.extraResources[0]（from 已单独比过）',
  );
});

// ------------------------------------------------------------------
//  真实打包产物的旁证（没打包过就跳过，不算失败）
// ------------------------------------------------------------------
test('（旁证）若 sidecar 已构建，产物目录里确实有 backend.exe', (t) => {
  const backendDir = path.resolve(__dirname, '..', 'dist', 'backend');
  if (!fs.existsSync(backendDir)) {
    t.skip('还没构建 sidecar（先跑 desktop/scripts/build_backend.ps1）');
    return;
  }
  const exeName = process.platform === 'win32' ? 'backend.exe' : 'backend';
  assert.ok(
    fs.existsSync(path.join(backendDir, exeName)),
    `${backendDir} 存在但没有 ${exeName}`,
  );
  // onedir 形态必须有 _internal/（依赖都躺在里面），否则装到用户机上起不来
  assert.ok(fs.existsSync(path.join(backendDir, '_internal')), 'onedir 产物缺少 _internal/');
});
