'use strict';

/**
 * 预加载脚本：把**具名、有限**的能力交给页面。
 *
 * ★ 安全立场（与本项目"插件从不执行第三方代码"一致）：
 *   · 暴露的是 `window.yunmeng` 这个**具名对象**，不是 `ipcRenderer`；
 *   · 每个方法都对应主进程里一条固定通道，参数还要在主进程二次校验
 *     （例如 `openPath` 只接受主进程自己算出来的那几个路径）；
 *   · `contextIsolation: true` + `sandbox: true`，页面拿不到 Node 的任何东西。
 *
 * ★ 注意：控制台页面（`/console/`）是后端伺服的另一源，**加载不到**这个对象；
 *   它只有加载页用。控制台自己该怎么跑还是怎么跑（零改动）。
 */

const { contextBridge, ipcRenderer } = require('electron');

/** 只转发这几种阶段，避免把主进程的内部状态整包递给页面。 */
const FORWARDED_KEYS = [
  'phase', 'message', 'consoleUrl', 'healthUrl', 'port', 'portSource',
  'pythonPath', 'backendMode', 'backendCommand', 'backendPid',
  'logsDir', 'backendLogFile', 'error', 'hint', 'detail',
];

function pick(source) {
  const out = {};
  if (!source || typeof source !== 'object') return out;
  for (const key of FORWARDED_KEYS) {
    if (key in source) out[key] = source[key];
  }
  return out;
}

contextBridge.exposeInMainWorld('yunmeng', {
  /** 取一次当前状态（加载页可能比主进程的广播晚到，需要主动拉一次）。 */
  getState: () => ipcRenderer.invoke('app:state').then(pick),
  getPaths: () => ipcRenderer.invoke('app:paths'),
  /** 用户点"重试"：重新走一遍选端口 → 起后端 → 健康检查。 */
  retry: () => ipcRenderer.invoke('app:retry').then(pick),
  /** 只接受主进程白名单里的路径。 */
  openPath: (target) => ipcRenderer.invoke('app:open-path', String(target)),
  openExternal: (url) => ipcRenderer.invoke('app:open-external', String(url)),
  /**
   * 订阅状态变化。
   * ★ 返回值是"取消订阅"函数；不返回它的话，页面重载会累积监听器。
   */
  onState: (handler) => {
    if (typeof handler !== 'function') return () => {};
    const listener = (_event, payload) => handler(pick(payload));
    ipcRenderer.on('app:state', listener);
    return () => ipcRenderer.removeListener('app:state', listener);
  },
});

/**
 * 首启设置页用的能力。
 *
 * ★ 与 `yunmeng` 分开暴露是刻意的：设置页要处理的**表单里含口令与密钥**，
 *   所以它需要的通道（读字段定义、生成随机值、测试、保存）与日常页面完全不是一组。
 *   分开之后，"日常页面"即使被注入脚本也碰不到配置写入这条路。
 *   ★ 另外：这些通道**不会**把已保存的口令回传给页面（只回传"哪些必填项还缺"），
 *     页面重新填一遍即可 —— 少一份"把密钥读回渲染进程"的风险。
 */
contextBridge.exposeInMainWorld('yunmengSetup', {
  getFields: () => ipcRenderer.invoke('setup:fields'),
  generate: (key) => ipcRenderer.invoke('setup:generate', String(key)),
  test: (values) => ipcRenderer.invoke('setup:test', values),
  save: (values) => ipcRenderer.invoke('setup:save', values),
  openConfigDir: () => ipcRenderer.invoke('setup:open-config-dir'),
});
