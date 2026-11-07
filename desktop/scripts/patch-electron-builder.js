#!/usr/bin/env node
/*
 * electron-builder 26.15.3 的 NSIS 构建在 Windows 上会执行一个中间产物
 * （Setup*.exe，几百 KB）来生成卸载器。它通过 WineVmManager 起这个进程时
 * 只传了 env={__COMPAT_LAYER:"RunAsInvoker"}；而 builder-util 在 Windows 上
 * 对显式 env 是**整体替换**，于是中间产物拿不到 TEMP / SYSTEMROOT / PATH，
 * NSIS 弹：
 *     Error writing temporary file. Make sure your temp folder is valid.
 *
 * 这里只把这一处改成"继承当前进程环境 + 调用方覆盖"，不动 electron-builder 的
 * 其它逻辑。脚本是幂等的，重复执行不会重复改。
 */
const fs = require('fs');
const path = require('path');

const target = path.join(
  __dirname, '..', 'node_modules', 'app-builder-lib', 'out', 'vm', 'WineVm.js'
);
if (!fs.existsSync(target)) {
  console.error('[patch-electron-builder] 找不到 ' + target);
  process.exit(1);
}

let source = fs.readFileSync(target, 'utf8');
if (source.includes('env: { ...process.env, ...((options')) {
  console.log('[patch-electron-builder] 已经是打过补丁的状态，跳过');
  process.exit(0);
}

const marker = '        if (process.platform === "win32") {\n'
  + '            return (0, builder_util_1.exec)(target, appArgs, options);\n'
  + '        }';
const replacement = '        if (process.platform === "win32") {\n'
  + '            // [hne-patch] NSIS 中间产物需要继承 TEMP/SYSTEMROOT/PATH；\n'
  + '            // electron-builder 26.15.3 在这里只传 {__COMPAT_LAYER}，会丢掉整个环境。\n'
  + '            return (0, builder_util_1.exec)(target, appArgs, { ...options, env: { ...process.env, ...((options && options.env) || {}) } });\n'
  + '        }';

if (!source.includes(marker)) {
  console.error('[patch-electron-builder] WineVm.js 结构与预期不符，未修改。');
  console.error('请检查 electron-builder 版本是否变化，再更新本脚本。');
  process.exit(1);
}

source = source.replace(marker, replacement);
fs.writeFileSync(target, source, 'utf8');
console.log('[patch-electron-builder] 已修补 ' + target);
