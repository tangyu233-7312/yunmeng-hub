/* ============================================================
   启动守卫（必须最先加载，且早于 app.js）

   ==================== 它解决什么问题？====================
   真实事故：用户打开 /console **一片空白**，控制台只有一行红字——

     Uncaught SyntaxError: The requested module '../ui.js' does not
     provide an export named 'freshViewSignal' (at chat.js:29:3)

   原因是前端用原生 ES Module，浏览器**按 URL 缓存**每个 .js。
   只要改动了其中一部分文件，用户浏览器里就会出现
   「新的 views/chat.js + 旧的 ui.js」这种**版本混用**；
   而 ES Module 一旦 import 一个目标模块里不存在的导出，
   整个模块图直接加载失败 —— 页面全白。
   致命之处在于：**用户按 Ctrl+F5 也未必能救回来**
   （实测浏览器宁可复用旧副本，日志里全是 304）。

   ==================== 现在怎么防？====================
   1. index.html 里的 <script type="importmap"> 由后端**动态生成**，
      给每个模块加上本次构建的版本号：ui.js?v=<指纹>。
      → 版本一变，URL 就变，浏览器**没有旧副本可用**，必须重新下载。
      → 版本没变（只改后端）时 URL 不变，浏览器照旧复用缓存，不牺牲速度。

   2. 本文件是那个**自动自愈**的保险：index.html 自己也可能被缓存住，
      于是它带着**旧版本号**的 importmap。这里用 no-store 向服务器
      要一次真实的入口地址；只要发现对不上，就立刻 replace 重载。
      用户不需要懂缓存，也不需要按任何快捷键。

   ==================== 为什么用文件 mtime+size 当指纹？====================
   故意**不用** CSS/JS 内容的哈希：那需要每次请求都读一遍全部文件。
   而前端源码的 mtime 只在"这次真的改过"时才变，语义刚好吻合，
   代价是一次 os.scandir（见 app/main.py 的 _frontend_version）。
   ============================================================ */

const KEY = 'hne_web_version';
const TARGET = '/console/';
/** 一分钟内最多自动重载 2 次，避免任何意外情况下变成"刷新死循环" */
const MAX_RELOADS = 2;

/** 当前 HTML 内嵌的版本号（后端注入到 <body data-web-version>） */
const embedded = document.body?.dataset?.webVersion || '';

(async () => {
  try {
    // cache: 'no-store' —— 这一步的目的就是绕过缓存问到真话，绝不能走缓存
    const response = await fetch(TARGET, { cache: 'no-store' });
    const live = response.headers.get('X-HNE-Web-Version') || '';
    if (!live || !embedded || live === embedded) return;

    // 情况一：地址栏里已经带着这个版本号了，说明上次就是这么重载的，
    //         结果拿到的 HTML 仍然是旧的（本页 embedded 没变）→ 不再重载，
    //         直接给用户一条看得懂的提示，别让他看着白屏转圈。
    const already = new URLSearchParams(window.location.search).get('v') === live;

    // 情况二：兜底计数。正常情况下每个新版本最多重载一次；
    //         万一有别的未知原因导致反复触发，这里也能在一分钟内刹住。
    let stamps = [];
    try {
      // 只保留最近一分钟内的重载记录（超时的自然作废，不影响下次真正需要重载时）
      stamps = (JSON.parse(sessionStorage.getItem(KEY) || '[]') || []).filter(
        (t) => Date.now() - t < 60_000
      );
    } catch {
      /* 隐私模式下 sessionStorage 不可用，退化成"只靠情况一"防抖 */
    }

    if (already || stamps.length >= MAX_RELOADS) {
      showBanner(embedded, live);
      return;
    }
    try {
      sessionStorage.setItem(KEY, JSON.stringify([...stamps, Date.now()]));
    } catch {
      /* 同上 */
    }
    // 用 replace 避免在浏览器历史里留记录，用户按"后退"不会转圈
    window.location.replace(`${TARGET}?v=${encodeURIComponent(live)}`);
  } catch {
    /* 服务不可达等情况不在这里处理，app.js 自己会报错 */
  }
})();

/**
 * 极端兜底：自动重载也没救回来时，至少让用户看到"发生了什么"，
 * 而不是对着一片空白发呆。
 */
function showBanner(stale, live) {
  const bar = document.createElement('div');
  bar.className = 'boot-stale';
  bar.innerHTML =
    '<strong>检测到浏览器缓存了旧版前端代码</strong>' +
    '<span>请按 Ctrl+F5 强制刷新；若仍无效，按 F12 → Application → Clear site data 后重新打开。</span>' +
    `<code>页面版本 ${stale} / 服务器版本 ${live}</code>`;
  document.body.appendChild(bar);
}
