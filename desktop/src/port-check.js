'use strict';

/**
 * 本机端口探测（只用于"这个端口上是不是已经有一个后端"）。
 *
 * ★ 与 `pickFreePort` 的区别：
 *   · `pickFreePort` 监听 :0 让系统分配一个**空**端口 —— 用于"要新起一个后端"；
 *   · `isPortBusy`   连一下已有端口 —— 用于"这里是不是已经有后端了，直接用它"。
 *   桌面版两种情况都要支持：用户可能已经自己按文档起了 8000 上的后端。
 */

const net = require('node:net');

/**
 * @param {number} port
 * @param {string} [host]
 * @param {object} [options]
 * @param {number} [options.timeoutMs]
 * @param {typeof net} [options.netModule]
 * @returns {Promise<boolean>}
 */
function isPortBusy(port, host = '127.0.0.1', options = {}) {
  const timeoutMs = options.timeoutMs || 1200;
  const netModule = options.netModule || net;

  return new Promise((resolve) => {
    const socket = new netModule.Socket();
    let settled = false;
    const finish = (busy) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(busy);
    };
    socket.setTimeout(timeoutMs);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
    socket.connect({ port, host });
  });
}

module.exports = { isPortBusy };
