'use strict';

/**
 * 网络相关的小工具：选空闲端口 + 轮询后端健康检查。
 *
 * ★ 为什么单独成一个文件、并且**不 import electron**：
 *   这两件事是本模块里唯一"有逻辑"的部分（会失败、会超时、要重试），
 *   所以它们必须能在**没有 Electron 运行时**的情况下被 `node --test` 直接测。
 *   主进程只是调用方；把逻辑塞进 main.js 会让它们只能靠"手点"验证。
 */

const net = require('node:net');

/**
 * 选一个本机可用的空闲端口。
 *
 * ★ 写死 8000 是本项目明确要避免的：开发商用 8000 起后端时，桌面版就打不开。
 *   做法是监听 :0 让系统分配，拿到端口后立刻关掉再交给子进程 ——
 *   这里有极小的"被别的进程抢走"的可能（TOCTOU），所以调用方必须**允许失败后重试**。
 *
 * @param {{ host?: string, netModule?: typeof net }} [options]
 * @returns {Promise<number>}
 */
function pickFreePort(options = {}) {
  const host = options.host || '127.0.0.1';
  const netModule = options.netModule || net;

  return new Promise((resolve, reject) => {
    const server = netModule.createServer();
    server.unref();
    server.on('error', reject);
    server.listen({ port: 0, host, exclusive: true }, () => {
      const address = server.address();
      if (!address || typeof address === 'string') {
        server.close(() => reject(new Error('无法取得系统分配的空闲端口')));
        return;
      }
      const port = address.port;
      server.close(() => resolve(port));
    });
  });
}

/**
 * 真正的健康检查：请求 `url`，返回是否 2xx。
 *
 * ★ 判据只认 `response.ok`，**不看响应体内容**：
 *   `/health` 在数据库不通时会返回 503（组件降级），那种情况后端其实"起来了但不可用"，
 *   交给调用方去决定怎么提示，而不是在这里假装健康。
 *
 * @param {string} url
 * @param {{ timeoutMs?: number, fetchImpl?: typeof fetch }} [options]
 * @returns {Promise<{ ok: boolean, status?: number, error?: string }>}
 */
async function defaultHttpProbe(url, options = {}) {
  const timeoutMs = options.timeoutMs || 4000;
  const fetchImpl = options.fetchImpl || globalThis.fetch;
  if (typeof fetchImpl !== 'function') {
    return { ok: false, error: '当前 Node 运行时没有 fetch（需要 Node 18+）' };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, { signal: controller.signal, redirect: 'manual' });
    return { ok: response.ok === true, status: response.status };
  } catch (error) {
    return { ok: false, error: describeFetchError(error) };
  } finally {
    clearTimeout(timer);
  }
}

/** 把 fetch 的报错翻译成"人能看懂的一句话原因"。 */
function describeFetchError(error) {
  if (!error) return '未知错误';
  if (error.name === 'AbortError' || error.name === 'TimeoutError') return '请求超时';
  const code = error.cause && error.cause.code ? error.cause.code : error.code;
  if (code === 'ECONNREFUSED') return '连接被拒绝（后端还没监听）';
  if (code === 'ECONNRESET') return '连接被重置（后端正在启动或已退出）';
  if (code === 'EACCES') return '没有权限连接该端口';
  return error.message || String(error);
}

/**
 * 给"每次探测"套一个**硬超时**。
 *
 * ★ 为什么需要在 fetch 自带 abort 之外再加一层：
 *   探测卡住的成因不止一种（连接被接受但永不应答、连接池里的死连接被复用…），
 *   而 `waitForHealth` 的循环是 `await` 每一次探测的 —— **只要有一次探测永远不返回，
 *   整个健康检查就再也走不到"超时"那一步**，界面会永远转圈（这是本轮实测到的真实场景：
 *   端口被一个"只接受连接、不说话"的程序占着）。
 *   所以这里用 race 保证"单次探测一定在预算内返回"，与探测器内部实现无关。
 *
 * @template T
 * @param {() => Promise<T>} fn
 * @param {number} timeoutMs
 * @param {T} timeoutValue 超时返回的兜底值
 */
function withHardTimeout(fn, timeoutMs, timeoutValue) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => finish(timeoutValue), timeoutMs);
    Promise.resolve()
      .then(fn)
      .then(finish)
      .catch((error) => finish({ ok: false, error: describeFetchError(error) }));
  });
}

/**
 * 轮询健康检查，直到就绪 / 超时 / 放弃。
 *
 * 三种结束方式（每一种都要能区分，否则界面只能给一句没用的"失败了"）：
 *   · ready     —— 探到了
 *   · timeout   —— 一直没探到
 *   · abandoned —— 调用方说"别等了"（比如后端子进程已经退出，再等下去是白等）
 *
 * @param {object} options
 * @param {string} options.url                 健康检查地址
 * @param {number} [options.timeoutMs]          总超时（默认 180s：首次启动要加载 ONNX 模型）
 * @param {number} [options.intervalMs]         轮询间隔
 * @param {(info: {attempt: number, elapsedMs: number, error?: string, status?: number}) => void} [options.onProgress]
 * @param {() => boolean} [options.shouldAbort] 返回 true 就放弃等待
 * @param {() => Promise<{ok: boolean, status?: number, error?: string}>} [options.probe] 可注入的探测器
 * @param {(ms: number) => Promise<void>} [options.sleep] 可注入的等待（测试里不真等）
 * @param {() => number} [options.now]          可注入的时钟
 * @returns {Promise<{ ok: boolean, reason: 'ready'|'timeout'|'abandoned', attempts: number, elapsedMs: number, lastError?: string, lastStatus?: number }>}
 */
function waitForHealth(options) {
  const url = options.url;
  // ★ 参数错误要**同步抛出**：如果这个函数是 async，`throw` 会变成 rejected promise，
  //   调用方一旦忘了 await 就变成 unhandledRejection ——
  //   一个"参数写错了"的 bug 会伪装成"随机崩溃"。（本文件的测试抓到过这一点。）
  if (!url) throw new Error('waitForHealth 需要 url');
  return runHealthLoop(options);
}

async function runHealthLoop(options) {
  const url = options.url;
  const timeoutMs = options.timeoutMs || 180000;
  const intervalMs = options.intervalMs || 500;
  const probe = options.probe || ((u) => defaultHttpProbe(u, { timeoutMs: options.probeTimeoutMs }));
  // 单次探测的硬上限：比探测本身的自带超时略长，留出收尾时间
  const probeDeadlineMs = Math.max(1, (options.probeTimeoutMs || 4000) + 500);
  const sleep = options.sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const now = options.now || (() => Date.now());
  const shouldAbort = options.shouldAbort || (() => false);

  const startedAt = now();
  let attempts = 0;
  let lastError;
  let lastStatus;
  // ★ 只在"原因变了"的时候回报一次：每 500ms 刷同一句"连接被拒绝"会把界面刷成噪音，
  //   而真正的变化（连接被拒 → 超时 → 起反了）会被淹没。
  let reportedError;
  let reportedStatus;

  for (;;) {
    if (shouldAbort()) {
      return {
        ok: false,
        reason: 'abandoned',
        attempts,
        elapsedMs: now() - startedAt,
        lastError,
        lastStatus,
      };
    }

    attempts += 1;
    // ★ 两次 hard timeout 相加：即使 await probe 卡死，也一定会在预算内回到循环
    const perProbeBudget = Math.max(1, Math.min(probeDeadlineMs, timeoutMs - (now() - startedAt)));
    const result = await withHardTimeout(
      () => probe(url),
      perProbeBudget,
      { ok: false, error: `请求超过 ${perProbeBudget}ms 仍未返回` },
    );

    if (result && result.ok) {
      return {
        ok: true,
        reason: 'ready',
        attempts,
        elapsedMs: now() - startedAt,
        lastStatus: result.status,
      };
    }

    lastError = result && result.error;
    lastStatus = result && result.status;

    const elapsedMs = now() - startedAt;
    if (typeof options.onProgress === 'function'
      && (lastError !== reportedError || lastStatus !== reportedStatus)) {
      reportedError = lastError;
      reportedStatus = lastStatus;
      options.onProgress({ attempt: attempts, elapsedMs, error: lastError, status: lastStatus });
    }

    if (elapsedMs >= timeoutMs) {
      return { ok: false, reason: 'timeout', attempts, elapsedMs, lastError, lastStatus };
    }

    // 最后一次等待不要超过剩余预算，否则超时会"晚到"
    const remaining = timeoutMs - elapsedMs;
    await sleep(Math.max(0, Math.min(intervalMs, remaining)));
  }
}

module.exports = { pickFreePort, waitForHealth, defaultHttpProbe, describeFetchError };
