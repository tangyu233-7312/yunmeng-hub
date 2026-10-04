'use strict';

/**
 * `desktop/src/net-utils.js` 的自测。
 *
 * ★ 为什么这些用例值得写：
 *   "起后端"这条路上真正会出错的就是这两件事 —— **端口选错**（选了个被占的）
 *   与**健康检查判错**（超时了还说成功 / 探到了却说失败）。
 *   它们在界面上的表现都是"转圈转到天荒地老"，靠手点是很难定位的。
 *
 * ★ 用法说明：这是 Node 自带的 test runner（`node --test`），**刻意不引 jest/vitest** ——
 *   这个仓库的纪律是"依赖要有实际引用"，桌面壳除了 electron 一个 devDependency 之外零依赖。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');

const { pickFreePort, waitForHealth, defaultHttpProbe, describeFetchError } = require('../src/net-utils');

/** 起一个只在本机、跑完就关的小 HTTP 服务。 */
async function withServer(handler, run) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen({ port: 0, host: '127.0.0.1' }, resolve));
  const port = server.address().port;
  try {
    return await run(port);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

async function withTcpServer(run) {
  const server = net.createServer();
  // ★ 收到连接后什么都不发：模拟"端口被别的程序占了"（TCP 连得上，但不是我们的 HTTP 后端）
  const sockets = new Set();
  server.on('connection', (socket) => {
    sockets.add(socket);
    socket.on('error', () => {});
    socket.once('close', () => sockets.delete(socket));
  });
  await new Promise((resolve) => server.listen({ port: 0, host: '127.0.0.1' }, resolve));
  const port = server.address().port;
  try {
    return await run(port);
  } finally {
    // ★ 必须**自己**把所有连接销毁掉，否则 `server.close()` 的回调永远不会触发，
    //   测试就挂死在这里（本用例第一版正是这么挂的，而且症状是"超时"，
    //   看着像被测代码的问题，其实是测试收尾的问题）。
    //   注意 `server.closeAllConnections()` 在这种"裸 socket、没有完整 HTTP 请求"的场景下
    //   靠不住 —— 所以这里不依赖它，自己记账自己销毁。
    for (const socket of sockets) socket.destroy();
    sockets.clear();
    if (typeof server.closeAllConnections === 'function') server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

// ------------------------------------------------------------------
//  pickFreePort
// ------------------------------------------------------------------
test('pickFreePort：返回一个真能监听上去的端口', async () => {
  const port = await pickFreePort({ host: '127.0.0.1' });
  assert.ok(Number.isInteger(port) && port > 0 && port < 65536, `端口不合法：${port}`);

  await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen({ port, host: '127.0.0.1' }, () => server.close(resolve));
  });
});

test('pickFreePort：连续两次不会给同一个端口（系统分配而非写死）', async () => {
  const first = await pickFreePort({ host: '127.0.0.1' });
  const second = await pickFreePort({ host: '127.0.0.1' });
  assert.notEqual(first, second, '两次拿到同一个端口说明没走系统分配');
});

// ------------------------------------------------------------------
//  defaultHttpProbe
// ------------------------------------------------------------------
test('defaultHttpProbe：2xx 判为健康，并回报状态码', async () => {
  await withServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{"status":"ok"}');
  }, async (port) => {
    const result = await defaultHttpProbe(`http://127.0.0.1:${port}/health`);
    assert.equal(result.ok, true);
    assert.equal(result.status, 200);
  });
});

test('defaultHttpProbe：503 判为不健康（"起来了但不可用"不能算健康）', async () => {
  await withServer((_req, res) => {
    res.writeHead(503);
    res.end('degraded');
  }, async (port) => {
    const result = await defaultHttpProbe(`http://127.0.0.1:${port}/health`);
    assert.equal(result.ok, false);
    assert.equal(result.status, 503);
  });
});

test('defaultHttpProbe：连不上时报 ECONNREFUSED 的人话', async () => {
  // 拿一个刚关掉的端口来保证"没人监听"
  const port = await pickFreePort({ host: '127.0.0.1' });
  const result = await defaultHttpProbe(`http://127.0.0.1:${port}/health`, { timeoutMs: 2000 });
  assert.equal(result.ok, false);
  assert.match(result.error, /连接被拒绝/);
});

test('describeFetchError：超时与拒绝各自有可读说法', () => {
  assert.equal(describeFetchError({ name: 'AbortError' }), '请求超时');
  assert.equal(describeFetchError({ cause: { code: 'ECONNREFUSED' } }), '连接被拒绝（后端还没监听）');
  assert.equal(describeFetchError({ code: 'ECONNRESET' }), '连接被重置（后端正在启动或已退出）');
  assert.equal(describeFetchError({ message: 'boom' }), 'boom');
});

// ------------------------------------------------------------------
//  waitForHealth
// ------------------------------------------------------------------
test('waitForHealth：先失败几次、后来就绪 → reason=ready 且尝试次数对得上', async () => {
  let calls = 0;
  const result = await waitForHealth({
    url: 'http://stub/health',
    timeoutMs: 10000,
    intervalMs: 1,
    probe: async () => {
      calls += 1;
      return calls < 3 ? { ok: false, error: '连接被拒绝（后端还没监听）' } : { ok: true, status: 200 };
    },
    sleep: async () => {}, // 测试里不真等
  });

  assert.equal(result.ok, true);
  assert.equal(result.reason, 'ready');
  assert.equal(result.attempts, 3);
});

test('waitForHealth：一直探不到 → reason=timeout 且带最后一次错误', async () => {
  let clock = 0;
  const result = await waitForHealth({
    url: 'http://stub/health',
    timeoutMs: 100,
    intervalMs: 40,
    probe: async () => ({ ok: false, error: '连接被拒绝（后端还没监听）' }),
    // 每次 sleep 把假时钟往前推，避免测试真的等
    sleep: async (ms) => { clock += ms; },
    now: () => clock,
  });

  assert.equal(result.ok, false);
  assert.equal(result.reason, 'timeout');
  assert.match(result.lastError, /连接被拒绝/);
});

test('waitForHealth：探测器抛异常不会被当成成功', async () => {
  let clock = 0;
  const result = await waitForHealth({
    url: 'http://stub/health',
    timeoutMs: 60,
    intervalMs: 30,
    probe: async () => { throw new Error('探测本身炸了'); },
    sleep: async (ms) => { clock += ms; },
    now: () => clock,
  });

  assert.equal(result.ok, false);
  assert.equal(result.reason, 'timeout');
  assert.equal(result.lastError, '探测本身炸了');
});

test('waitForHealth：shouldAbort 为真时立刻放弃（后端子进程已经退了，再等是白等）', async () => {
  let probed = 0;
  const result = await waitForHealth({
    url: 'http://stub/health',
    timeoutMs: 100000,
    probe: async () => { probed += 1; return { ok: false, error: '连接被拒绝' }; },
    shouldAbort: () => true,
    sleep: async () => {},
  });

  assert.equal(result.ok, false);
  assert.equal(result.reason, 'abandoned');
  assert.equal(probed, 0, '既然要放弃，就不该再发探测请求');
});

test('waitForHealth：onProgress 只在"原因变了"时回报（不刷屏）', async () => {
  let clock = 0;
  const progress = [];
  await waitForHealth({
    url: 'http://stub/health',
    timeoutMs: 200,
    intervalMs: 50,
    probe: async () => ({ ok: false, error: '连接被拒绝（后端还没监听）' }),
    onProgress: (info) => progress.push(info),
    sleep: async (ms) => { clock += ms; },
    now: () => clock,
  });

  assert.equal(progress.length, 1, `同一个原因不该反复回报，实际回报了 ${progress.length} 次`);
  assert.equal(progress[0].attempt, 1);
});

test('waitForHealth：原因变化时会再回报一次', async () => {
  let clock = 0;
  const reasons = [];
  let round = 0;
  await waitForHealth({
    url: 'http://stub/health',
    timeoutMs: 300,
    intervalMs: 50,
    probe: async () => {
      round += 1;
      if (round <= 2) return { ok: false, error: '连接被拒绝（后端还没监听）' };
      if (round <= 4) return { ok: false, status: 503 };
      return { ok: true, status: 200 };
    },
    onProgress: (info) => reasons.push(info.error || `status:${info.status}`),
    sleep: async (ms) => { clock += ms; },
    now: () => clock,
  });

  assert.deepEqual(reasons, ['连接被拒绝（后端还没监听）', 'status:503']);
});

test('waitForHealth：某次探测永远不返回时，整体仍然会超时（不会永远转圈）', async () => {
  let clock = 0;
  const result = await waitForHealth({
    url: 'http://stub/health',
    timeoutMs: 50,
    intervalMs: 20,
    // ★ 这一条守的是一类真实故障：端口被"只接受连接、从不回话"的程序占着。
    //   如果没有单次探测的硬超时，await 会永远停在这里，界面就永远转圈。
    probe: () => new Promise(() => {}),
    sleep: async (ms) => { clock += ms; },
    now: () => clock,
  });

  assert.equal(result.ok, false);
  assert.equal(result.reason, 'timeout');
  assert.match(result.lastError, /仍未返回/);
});

test('waitForHealth：缺少 url 时明确报错，而不是静默转圈', () => {
  assert.throws(() => waitForHealth({}), /需要 url/);
});

test('waitForHealth 与真实 HTTP 服务端到端：先 503 后 200', async () => {
  let ready = false;
  await withServer((_req, res) => {
    res.writeHead(ready ? 200 : 503);
    res.end(ready ? 'ok' : 'starting');
  }, async (port) => {
    setTimeout(() => { ready = true; }, 150);
    const result = await waitForHealth({
      url: `http://127.0.0.1:${port}/health`,
      timeoutMs: 10000,
      intervalMs: 60,
      probeTimeoutMs: 1000,
    });
    assert.equal(result.ok, true);
    assert.ok(result.attempts >= 2, `应该在 503 之后才成功，实际尝试 ${result.attempts} 次`);
  });
});

test('defaultHttpProbe：端口有人监听但不说 HTTP → 超时，不假装健康', async () => {
  await withTcpServer(async (port) => {
    const result = await defaultHttpProbe(`http://127.0.0.1:${port}/health`, { timeoutMs: 700 });
    assert.equal(result.ok, false);
    // ★ 这条正是"端口被别的程序占了"的现场：连得上 ≠ 是我们的后端
    assert.equal(result.error, '请求超时');
  });
});

test('waitForHealth 与真实 TCP 服务：一直不说 HTTP → 超时而不是假装成功', async () => {
  await withTcpServer(async (port) => {
    const result = await waitForHealth({
      url: `http://127.0.0.1:${port}/health`,
      timeoutMs: 900,
      intervalMs: 250,
      probeTimeoutMs: 300,
    });
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'timeout');
  });
});
