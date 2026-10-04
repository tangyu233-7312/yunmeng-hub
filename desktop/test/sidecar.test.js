'use strict';

/**
 * `desktop/src/sidecar.js` 的自测。
 *
 * ★ 这一层决定"用打包后端还是本机 Python"，判错的两种后果都很糟：
 *   · 明明有 `backend.exe` 却没用 → 用户机上报"找不到 Python"（本该免装 Python）；
 *   · 显式指定了后端却**悄悄**回退 → 用户以为在用打包版，其实在用另一个，
 *     排查时会完全走错方向（本项目反复强调要避免的静默降级）。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const { SIDECAR_NAME, sidecarCandidates, chooseBackend, sidecarArgs } = require('../src/sidecar');

// ------------------------------------------------------------------
//  sidecarCandidates
// ------------------------------------------------------------------
test('sidecarCandidates：Windows 下找 backend.exe，且覆盖开发态多个可能位置', () => {
  const candidates = sidecarCandidates({
    desktopDir: 'E:\\repo\\desktop',
    appRoot: 'E:\\repo',
    resourcesPath: 'E:\\app\\resources',
    execDir: 'E:\\app',
    platform: 'win32',
  });

  assert.ok(candidates.every((c) => c.endsWith('backend.exe')), candidates.join('\n'));
  // 开发态：构建脚本默认把产物放在 desktop/dist/backend/
  assert.ok(
    candidates.includes(path.join('E:\\repo\\desktop', 'dist', 'backend', 'backend.exe')),
    `开发态位置必须在内：\n${candidates.join('\n')}`,
  );
  // 打包态：electron-builder 的 extraResources
  assert.ok(candidates.some((c) => c.startsWith('E:\\app\\resources')));
  assert.ok(candidates.some((c) => c.startsWith('E:\\app')));
});

test('sidecarCandidates：非 Windows 下不带 .exe 后缀', () => {
  const candidates = sidecarCandidates({ desktopDir: '/repo/desktop', platform: 'linux' });
  assert.ok(candidates.every((c) => c.endsWith(SIDECAR_NAME)), candidates.join('\n'));
  assert.ok(candidates.every((c) => !c.endsWith('.exe')), candidates.join('\n'));
});

test('sidecarCandidates：显式指定的路径排在最前面（优先级最高）', () => {
  const candidates = sidecarCandidates({
    desktopDir: 'E:\\repo\\desktop',
    appRoot: 'E:\\repo',
    explicit: 'D:\\my\\backend.exe',
    platform: 'win32',
  });
  assert.equal(candidates[0], 'D:\\my\\backend.exe');
});

test('sidecarCandidates：去重但保持顺序（同一路径不会出现两次）', () => {
  const candidates = sidecarCandidates({
    desktopDir: 'E:\\repo\\desktop',
    appRoot: 'E:\\repo',
    execDir: 'E:\\repo',
    platform: 'win32',
  });
  const lowered = candidates.map((c) => c.toLowerCase());
  assert.equal(new Set(lowered).size, lowered.length, `有重复：\n${candidates.join('\n')}`);
});

test('sidecarCandidates：没有可选路径时返回空数组（不抛异常）', () => {
  assert.deepEqual(sidecarCandidates({ platform: 'win32' }), []);
});

// ------------------------------------------------------------------
//  chooseBackend
// ------------------------------------------------------------------
test('chooseBackend：找到打包后端就用它，并说明"无需本机 Python"', () => {
  const result = chooseBackend({
    candidates: ['E:\\a\\backend.exe', 'E:\\b\\backend.exe'],
    exists: (p) => p === 'E:\\b\\backend.exe',
  });

  assert.equal(result.kind, 'sidecar');
  assert.equal(result.command, 'E:\\b\\backend.exe');
  assert.equal(result.explicitSpecified, false);
  assert.match(result.reason, /打包后端/);
  // 探过的路径要如实列出（排查"为什么没找到"时全靠它）
  assert.deepEqual(result.checked, ['E:\\a\\backend.exe', 'E:\\b\\backend.exe']);
});

test('chooseBackend：没指定也没找到 → kind=none 且 explicitSpecified=false（调用方自行回退）', () => {
  const result = chooseBackend({
    candidates: ['E:\\a\\backend.exe'],
    exists: () => false,
  });

  assert.equal(result.kind, 'none');
  assert.equal(result.command, null);
  assert.equal(result.explicitSpecified, false, '没指定就不该让调用方以为"用户指定失败了"');
  assert.match(result.reason, /没有找到打包后端/);
});

test('chooseBackend：显式指定的后端不存在 → kind=none 且 explicitSpecified=true（调用方必须停下）', () => {
  const result = chooseBackend({
    candidates: ['D:\\bogus\\backend.exe', 'E:\\repo\\desktop\\dist\\backend\\backend.exe'],
    explicit: 'D:\\bogus\\backend.exe',
    exists: () => false,
  });

  assert.equal(result.kind, 'none');
  assert.equal(result.command, null);
  assert.equal(result.explicitSpecified, true, '← 这个字段就是"不许静默回退"的判据');
  assert.match(result.reason, /HNE_DESKTOP_BACKEND/);
});

test('★ 回归：显式指定失败时，判据不能依赖"有没有传 python"（曾经因此静默回退）', () => {
  // 背景：第一版 chooseBackend 多了一个 python 参数，并用
  // "kind==='none' && reason 以 HNE_DESKTOP_BACKEND 开头" 来判"显式指定失败"。
  // 但调用方在探测 Python **之前**就调用它，于是显式失败时函数走的是
  // "python 为空"分支、返回了别的 reason，判据不命中 → 主进程转头探测 Python 并成功启动：
  // 用户指定了一个不存在的后端，日志里却写着"使用打包后端 backend.exe"。
  // 现在判据是显式字段，与调用顺序/额外参数都无关。
  const onlyCandidates = chooseBackend({
    candidates: ['E:\\a\\backend.exe'],
    explicit: 'C:\\nope\\backend.exe',
    exists: () => false,
  });
  assert.equal(onlyCandidates.explicitSpecified, true);

  // 无论调用方额外传什么（旧签名里的 python），结论都不能变
  const withExtra = chooseBackend({
    candidates: ['E:\\a\\backend.exe'],
    explicit: 'C:\\nope\\backend.exe',
    python: 'C:\\python\\python.exe',
    exists: () => false,
  });
  assert.equal(withExtra.kind, 'none');
  assert.equal(withExtra.explicitSpecified, true, '额外参数不该改变"用户指定失败"这个事实');
});

test('chooseBackend：显式指定的后端存在 → 用它，并在理由里点明来自环境变量', () => {
  const result = chooseBackend({
    candidates: ['D:\\mine\\backend.exe', 'E:\\repo\\desktop\\dist\\backend\\backend.exe'],
    explicit: 'D:\\mine\\backend.exe',
    exists: (p) => p === 'D:\\mine\\backend.exe',
  });

  assert.equal(result.kind, 'sidecar');
  assert.equal(result.command, 'D:\\mine\\backend.exe');
  assert.equal(result.explicitSpecified, true);
  assert.match(result.reason, /HNE_DESKTOP_BACKEND/);
});

test('chooseBackend：显式指定为空白串时当作没指定（避免空值被当成路径）', () => {
  const result = chooseBackend({
    candidates: ['E:\\a\\backend.exe'],
    explicit: '   ',
    exists: () => false,
  });
  assert.equal(result.explicitSpecified, false);
});

test('chooseBackend：候选顺序决定优先级（第一个命中的赢）', () => {
  const result = chooseBackend({
    candidates: ['first.exe', 'second.exe'],
    exists: () => true, // 两个都存在
  });
  assert.equal(result.command, 'first.exe');
});

// ------------------------------------------------------------------
//  sidecarArgs
// ------------------------------------------------------------------
test('★★ 回归（接线）：显式指定时，main.js 必须只把"那一个"路径当候选', () => {
  // 背景：真实事故，两层。
  //  ① main.js 里 `explicit` 只传给了 chooseBackend、忘了传给 sidecarCandidates ——
  //     用户指定的路径压根没进候选列表，等于没检查过它；
  //  ② 更深一层：即使传了，如果把"默认候选"也一起交进来，
  //     指定的那个不存在时会**悄悄落到默认的 backend.exe** 上（用户指 A、程序用 B）。
  //
  // 所以 main.js 的规则是：**指定了就只认它**（candidates = [explicit]）。
  // 这条用例把这个规则钉死，并说明"混入默认候选"会怎样出错。
  const explicit = 'D:\\mine\\backend.exe';

  // 规则：显式指定 → 候选只有它
  const onlyIt = chooseBackend({ explicit, candidates: [explicit], exists: () => false });
  assert.equal(onlyIt.kind, 'none');
  assert.equal(onlyIt.explicitSpecified, true);
  assert.ok(onlyIt.checked.includes(explicit));

  // 反面教材：如果混入默认候选（且默认的那个存在），就会悄悄用到别的后端 ——
  // 这正是我们要避免的静默降级，所以 main.js **不许**那么写。
  const defaultCandidate = 'E:\\repo\\desktop\\dist\\backend\\backend.exe';
  const polluted = chooseBackend({
    explicit,
    candidates: [explicit, defaultCandidate],
    exists: (p) => p === defaultCandidate,
  });
  assert.equal(polluted.kind, 'sidecar', '（这就是为什么不能混入默认候选）');
  assert.notEqual(polluted.command, explicit);
});

test('★★ 回归（接线）：显式指定的路径存在时，用的必须是**它**', () => {
  const explicit = 'D:\\mine\\backend.exe';
  const result = chooseBackend({ explicit, candidates: [explicit], exists: () => true });
  assert.equal(result.kind, 'sidecar');
  assert.equal(result.command, explicit);
});

test('sidecarArgs：--host/--port 与 uvicorn 同名同义（消费者不用记两套）', () => {
  assert.deepEqual(sidecarArgs({ port: 51234 }), ['--host', '127.0.0.1', '--port', '51234']);
});

test('sidecarArgs：数据与日志目录会作为参数传下去（落在 userData，不在安装目录）', () => {
  const args = sidecarArgs({
    port: 1234,
    host: '127.0.0.1',
    dataDir: 'C:\\Users\\u\\AppData\\Roaming\\云梦枢\\data',
    logDir: 'C:\\Users\\u\\AppData\\Roaming\\云梦枢\\logs',
  });

  assert.deepEqual(args, [
    '--host', '127.0.0.1',
    '--port', '1234',
    '--data-dir', 'C:\\Users\\u\\AppData\\Roaming\\云梦枢\\data',
    '--log-dir', 'C:\\Users\\u\\AppData\\Roaming\\云梦枢\\logs',
  ]);
});

test('sidecarArgs：不传目录时只给 host/port（不硬塞空值）', () => {
  assert.deepEqual(sidecarArgs({ port: 80, dataDir: null, logDir: undefined }), ['--host', '127.0.0.1', '--port', '80']);
});
