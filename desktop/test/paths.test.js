'use strict';

/**
 * `desktop/src/paths.js` 的自测。
 *
 * 守的是一条红线：**数据目录与安装目录必须分开**。
 * 如果哪天有人把日志/配置又写回安装目录里，"卸载重装不丢数据"就悄悄失效了。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { resolvePaths, ensureDir } = require('../src/paths');

const SRC_DIR = path.resolve(__dirname, '..', 'src');

function makePaths(userDataDir) {
  return resolvePaths({
    baseDir: SRC_DIR,
    appRoot: path.resolve(SRC_DIR, '..', '..'),
    userDataDir,
  });
}

test('resolvePaths：所有日志都落在 userData 下，绝不落在仓库/安装目录里', () => {
  const userData = path.join(os.tmpdir(), 'hne-ud-1');
  const p = makePaths(userData);

  assert.equal(p.userDataDir, userData);
  assert.equal(p.logsDir, path.join(userData, 'logs'));
  assert.equal(p.backendLogFile, path.join(userData, 'logs', 'backend.log'));
  assert.equal(p.desktopLogFile, path.join(userData, 'logs', 'desktop.log'));

  for (const file of [p.logsDir, p.backendLogFile, p.desktopLogFile]) {
    assert.ok(file.startsWith(userData), `${file} 必须位于 userData 内`);
    assert.ok(!file.startsWith(p.appRoot), `${file} 不能落在仓库/安装目录里`);
  }
});

test('resolvePaths：安装目录内的路径（图标 / 加载页）被正确解析出来', () => {
  const p = makePaths(path.join(os.tmpdir(), 'hne-ud-2'));

  assert.equal(p.desktopDir, path.resolve(SRC_DIR, '..'));
  assert.equal(p.loadingPage, path.join(SRC_DIR, 'loading.html'));
  assert.equal(p.packagedIcon, path.join(p.desktopDir, 'build', 'icon.ico'));
  assert.equal(p.runtimeIcon, path.join(p.appRoot, 'web', 'img', 'logo-256.png'));
  assert.ok(fs.existsSync(p.loadingPage), '加载页文件应该真实存在（路径写错就会白屏）');
  assert.ok(fs.existsSync(p.runtimeIcon), '运行时图标应该真实存在');
  assert.ok(fs.existsSync(p.envExampleFile), '.env.example 应该真实存在（配置模板）');
});

test('resolvePaths：打包版的数据目录在 userData 下（安装目录之外）', () => {
  const userData = path.join(os.tmpdir(), 'hne-ud-4');
  const p = makePaths(userData);

  assert.equal(p.userDataDataDir, path.join(userData, 'data'));
  assert.ok(p.userDataDataDir.startsWith(userData), '打包版数据必须落在 userData 内');
  assert.ok(!p.userDataDataDir.startsWith(p.appRoot), '打包版数据不能落在仓库/安装目录里');
  // 与"开发态的 data 目录"必须是两个不同的地方，否则会互相踩
  assert.notEqual(p.userDataDataDir, p.dataDir);
});

test('resolvePaths：.env 在仓库根，且它本身是被 gitignore 的（绝不入库）', () => {
  const p = makePaths(path.join(os.tmpdir(), 'hne-ud-3'));
  assert.equal(p.envFile, path.join(p.appRoot, '.env'));
  assert.equal(path.basename(p.envFile), '.env');
});

test('ensureDir：目录不存在时创建，已存在时不报错（可重复调用）', () => {
  const dir = path.join(os.tmpdir(), `hne-ensure-${Date.now()}`, 'logs');
  assert.equal(fs.existsSync(dir), false);

  assert.equal(ensureDir(dir), dir);
  assert.equal(fs.existsSync(dir), true);

  // 第二次调用不能抛
  assert.equal(ensureDir(dir), dir);
});
