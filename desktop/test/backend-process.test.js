'use strict';

/**
 * `desktop/src/backend-process.js` 的自测 —— 重点只有一个：**别留下残余进程**。
 *
 * ★ 为什么这是本轮最该测的东西：
 *   桌面版退出后如果 python 还活着，端口就一直被占。用户下次启动会遇到
 *   "端口被占 / 数据库连不上"，而这两句话都指不到真正的原因。
 *   这件事**必须在自动化里钉住**，不能靠"我记得关的时候看了一眼"。
 *
 * ★ 这里用 node 自己当"被托管的子进程"，不去真的起 uvicorn：
 *   测试要验的是**进程管理**（活着吗 / 杀得掉吗），不是后端能不能跑。
 *   真起 uvicorn 会让单测依赖 MySQL，那是冒烟测试的职责。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const {
  isAlive, killTree, spawnBackend, healthUrl, consoleUrl,
} = require('../src/backend-process');

/** 起一个"活着但什么都不做"的子进程；用 `stdio:'ignore'` 避免依赖管道。 */
function spawnSleeper() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hne-desktop-proc-'));
  const script = path.join(dir, 'sleeper.js');
  // ★ 不用 `-e` 拼接命令：脚本文件更稳，也不怕引号在不同 shell 上的差异
  fs.writeFileSync(script, 'setTimeout(function () {}, 600000);\n', 'utf8');

  const child = spawn(process.execPath, [script], {
    stdio: 'ignore',
    windowsHide: true,
  });
  return { child, dir };
}

function waitForExit(child, timeoutMs = 10000) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve('timeout'), timeoutMs);
    child.once('exit', (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal });
    });
  });
}

// ------------------------------------------------------------------
//  isAlive
// ------------------------------------------------------------------
test('isAlive：自己的进程算活着，非法 PID 直接是 false', () => {
  assert.equal(isAlive(process.pid), true);
  assert.equal(isAlive(0), false);
  assert.equal(isAlive(-1), false);
  assert.equal(isAlive(undefined), false);
});

test('isAlive：子进程活着 → true；杀掉之后 → false（不依赖任何命令输出）', async () => {
  const { child } = spawnSleeper();
  try {
    assert.equal(isAlive(child.pid), true, '刚起的子进程应该算活着');

    const killed = killTree(child.pid);
    assert.equal(killed, true, '第一次杀应该真的执行了动作');
    await waitForExit(child);

    assert.equal(isAlive(child.pid), false, '杀完之后必须判定为不存在');
  } finally {
    if (isAlive(child.pid)) killTree(child.pid);
  }
});

// ------------------------------------------------------------------
//  killTree
// ------------------------------------------------------------------
test('killTree：对已经死掉的进程是幂等的（返回 false，不抛异常）', async () => {
  const { child } = spawnSleeper();
  killTree(child.pid);
  await waitForExit(child);

  assert.equal(killTree(child.pid), false, '第二次杀不该再执行动作');
  assert.equal(killTree(999999999), false, '不存在的 PID 也不该抛异常');
});

test('killTree：连子进程的子孙一起带走（uvicorn 会 fork，只杀根会留残余）', async (t) => {
  if (process.platform !== 'win32') {
    t.skip('这条针对 Windows 的 taskkill /T 行为');
    return;
  }

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hne-desktop-tree-'));
  const grandchildPidFile = path.join(dir, 'grandchild.pid');
  const parentScript = path.join(dir, 'parent.js');
  const childScript = path.join(dir, 'child.js');

  fs.writeFileSync(childScript, 'setTimeout(function () {}, 600000);\n', 'utf8');
  fs.writeFileSync(
    parentScript,
    [
      "const { spawn } = require('node:child_process');",
      'const fs = require("node:fs");',
      `const child = spawn(process.execPath, [${JSON.stringify(childScript)}], { stdio: 'ignore', windowsHide: true });`,
      `fs.writeFileSync(${JSON.stringify(grandchildPidFile)}, String(child.pid));`,
      'setTimeout(function () {}, 600000);',
    ].join('\n'),
    'utf8',
  );

  const root = spawn(process.execPath, [parentScript], { stdio: 'ignore', windowsHide: true });

  try {
    // 等孙进程的 PID 落盘（最多 5 秒）
    let grandchildPid = null;
    for (let i = 0; i < 50; i += 1) {
      if (fs.existsSync(grandchildPidFile)) {
        grandchildPid = Number.parseInt(fs.readFileSync(grandchildPidFile, 'utf8').trim(), 10);
        if (Number.isInteger(grandchildPid)) break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    assert.ok(Number.isInteger(grandchildPid), '没能拿到孙进程 PID，测试本身没跑起来');
    assert.equal(isAlive(grandchildPid), true, '孙进程应该活着');

    killTree(root.pid);
    await waitForExit(root);

    // taskkill /T 是同步的；再给系统一点收尾时间
    let grandchildGone = false;
    for (let i = 0; i < 20; i += 1) {
      if (!isAlive(grandchildPid)) { grandchildGone = true; break; }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    assert.equal(grandchildGone, true, `孙进程 ${grandchildPid} 没被带走 —— 这就是"残余进程"`);

    if (isAlive(grandchildPid)) {
      await new Promise((resolve) => {
        spawn('taskkill', ['/pid', String(grandchildPid), '/T', '/F'], { stdio: 'ignore' }).once('exit', resolve);
      });
    }
  } finally {
    if (isAlive(root.pid)) killTree(root.pid);
  }
});

// ------------------------------------------------------------------
//  spawnBackend
// ------------------------------------------------------------------
test('spawnBackend：参数、HNE_HOST/HNE_PORT 注入与 cwd 都对', () => {
  const calls = [];
  const fakeSpawn = (command, args, options) => {
    calls.push({ command, args, options });
    return {
      pid: 4242,
      stdout: null,
      stderr: null,
      once() {},
    };
  };

  const result = spawnBackend({
    appRoot: 'E:\\repo',
    python: 'E:\\repo\\.venv\\Scripts\\python.exe',
    port: 51234,
    fileEnv: { HNE_MYSQL_DB: 'narrative_engine' },
    env: { PATH: '/usr/bin' },
    spawnImpl: fakeSpawn,
  });

  assert.equal(result.pid, 4242);
  assert.equal(calls.length, 1);

  const call = calls[0];
  assert.equal(call.command, 'E:\\repo\\.venv\\Scripts\\python.exe');
  assert.deepEqual(call.args, [
    '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '51234',
  ]);
  // ★ cwd 必须是仓库根：后端的 HNE_CHROMA_PERSIST_DIR / HNE_LOG_DIR 都是相对路径
  assert.equal(call.options.cwd, 'E:\\repo');
  assert.equal(call.options.env.HNE_HOST, '127.0.0.1');
  assert.equal(call.options.env.HNE_PORT, '51234');
  assert.equal(call.options.env.HNE_MYSQL_DB, 'narrative_engine');
  assert.equal(call.options.env.PYTHONUTF8, '1');
});

test('spawnBackend：真的能拿起一个进程并让它跑起来（用 node 冒充解释器）', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hne-desktop-spawn-'));

  // ★ 为什么用 node 冒充：`spawnBackend` 完整地走了一遍 spawn（cwd / env / stdio 都真实生效），
  //   而这次 spawn 注定失败（node 没有名为 uvicorn 的模块）—— 失败**正是**我们要的：
  //   它证明"参数真的传到了子进程、cwd 真的是 appRoot、进程真的起来了"。
  //   真正"后端起得来"由 scripts/smoke_test.py 覆盖（那是它的职责）。
  const { child, pid } = spawnBackend({
    appRoot: dir,
    python: process.execPath,
    port: 54321,
    fileEnv: { HNE_MYSQL_DB: 'narrative_engine' },
    env: process.env,
    logFile: null,
  });

  try {
    assert.ok(Number.isInteger(pid) && pid > 0, `spawn 应返回真实 PID，实际 ${pid}`);
    assert.equal(isAlive(pid), true, '刚 spawn 的进程应该活着');

    // ★ stderr 必须在 spawn 之后**立刻**开始收集：进程可能在我们挂监听之前就退出，
    //   那时流已经 end，再去读只会得到空串（这条用例第一版就是这么假红的）。
    let stderr = '';
    if (child.stderr) {
      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (chunk) => { stderr += chunk; });
    }

    const outcome = await waitForExit(child, 15000);
    assert.notEqual(outcome, 'timeout', '子进程应该很快自行退出');

    // ★ 断言只认"子进程确实拿到了我们那串参数"这件事，不猜报错措辞：
    //   node 把 `-m` 当成自己的选项，所以它会说 `bad option: -m`（而不是去找 uvicorn 模块）。
    //   只要 stderr 里出现 `-m`，就证明参数**真的传到了子进程**（而不是被吞在中间层）。
    assert.notEqual(stderr, '', '应该能收到子进程的 stderr（证明 stdio 接线是对的）');
    assert.match(stderr, /-m/, `stderr 里应该能看到传进去的 -m 参数，实际：${stderr.slice(0, 200)}`);
  } finally {
    if (isAlive(pid)) killTree(pid);
  }
});

// ------------------------------------------------------------------
//  spawnBackend：sidecar 模式（阶段 2）
// ------------------------------------------------------------------
test('spawnBackend：sidecar 模式传的是 backend.exe 的参数，不带 uvicorn 那一套', () => {
  const calls = [];
  const fakeSpawn = (command, args, options) => {
    calls.push({ command, args, options });
    return { pid: 7, stdout: null, stderr: null, once() {} };
  };

  spawnBackend({
    appRoot: 'E:\\repo',
    python: 'E:\\app\\backend\\backend.exe',
    port: 51234,
    mode: 'sidecar',
    cwd: 'E:\\app\\backend',
    extraArgs: ['--data-dir', 'E:\\userData\\data'],
    envOverrides: { HNE_CHROMA_PERSIST_DIR: 'E:\\userData\\data\\chroma' },
    env: {},
    spawnImpl: fakeSpawn,
  });

  const call = calls[0];
  assert.equal(call.command, 'E:\\app\\backend\\backend.exe');
  assert.deepEqual(call.args, [
    '--host', '127.0.0.1', '--port', '51234',
    '--data-dir', 'E:\\userData\\data',
  ]);
  // ★ 不该出现 uvicorn 的痕迹：sidecar 自己就是后端
  assert.ok(!call.args.includes('uvicorn'), `sidecar 参数里不该有 uvicorn：${call.args.join(' ')}`);
  assert.equal(call.options.cwd, 'E:\\app\\backend');
  assert.equal(call.options.env.HNE_CHROMA_PERSIST_DIR, 'E:\\userData\\data\\chroma');
  assert.equal(call.options.env.HNE_PORT, '51234');
});

test('spawnBackend：显式给 args 时完全按给的来（不被默认参数覆盖）', () => {
  let captured = null;
  spawnBackend({
    appRoot: 'E:\\repo',
    python: 'x.exe',
    port: 1,
    args: ['--custom', 'yes'],
    spawnImpl: (command, args) => {
      captured = args;
      return { pid: 1, stdout: null, stderr: null, once() {} };
    },
  });
  assert.deepEqual(captured, ['--custom', 'yes']);
});

// ------------------------------------------------------------------
//  URL 组装
// ------------------------------------------------------------------
test('healthUrl / consoleUrl：控制台地址带尾斜杠（省掉一次 307 跳转）', () => {
  assert.equal(healthUrl(8000), 'http://127.0.0.1:8000/health');
  assert.equal(consoleUrl(8000), 'http://127.0.0.1:8000/console/');
  assert.equal(consoleUrl(51234, 'localhost'), 'http://localhost:51234/console/');
  assert.ok(consoleUrl(1234).endsWith('/'), '尾斜杠不能丢');
});
