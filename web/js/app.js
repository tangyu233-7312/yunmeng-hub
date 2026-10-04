/* ============================================================
   应用入口：路由、顶栏、请求日志面板

   ★ 请求日志是这个界面「用来测试功能」的关键部分：
     它把每一次 API 调用的方法、路径、状态码、耗时、
     以及**原始请求体与原始响应体**都摊开给你看。
     这样界面上任何一个操作，你都能立刻看到它到底发了什么、后端回了什么。
   ============================================================ */

// ★ 用裸名导入（hne/*），真实 URL 由 index.html 里的 importmap 决定，
//   并且带版本号。原因见 index.html 顶部与 js/boot.js 的说明。
import { api, auth, onRequestLogged, session } from 'hne/api';
import { $, $$, esc, mount, safeJson, toastErr, toastOk } from 'hne/ui';
import { renderAuth } from 'hne/auth';
import { renderCards } from 'hne/cards';
import { renderBooks } from 'hne/books';
import { renderChat } from 'hne/chat';
import { renderProviders } from 'hne/providers';
import { applyPluginTheme, renderPlugins } from 'hne/plugins';
import { renderPresets } from 'hne/presets';

const view = document.getElementById('view');

const ROUTES = {
  providers: { title: '模型配置', render: renderProviders },
  // 提示词预设：规范"模型怎么工作"（规则 / 破甲 / 采样参数），
  // 与角色卡（角色是谁）、世界书（世界有什么）三者正交，所以单独一页。
  presets: { title: '提示词预设', render: renderPresets },
  // 「我的卡库 / 公共卡库 / 全部可见」三种范围是**同一个页面内的切换**，
  // 不再各占一个顶部导航项 —— 那样既重复又让人以为要去两个地方。
  cards: { title: '角色卡', render: (root, ctx) => renderCards(root, { scope: 'mine', ...ctx }) },
  books: { title: '世界书', render: renderBooks },
  // 插件：声明式小扩展（改提示词 / 换控制台样式），安装来源只允许 GitHub。
  plugins: { title: '插件', render: renderPlugins },
  // 叙事会话（对话界面）。renderChat 支持 ctx.sessionId，方便从别处跳进某个会话。
  chat: { title: '叙事会话', render: (root, ctx) => renderChat(root, { ...ctx }) },
};

// ★ 默认落在「叙事会话」页：页面的价值最终体现在这里（顶栏也把它排在第一位）。
//   以前默认是角色卡页，进来先看到一堆卡片，与顶栏的顺序观感不一致。
let currentRoute = 'chat';

/**
 * ★ 每次切页都会换一个 AbortController，并把它的 signal 传给视图。
 *
 * 为什么必须这么做？
 *   #view 这个元素是**常驻**的，切页只是替换它内部的内容。
 *   如果视图把监听器挂在 #view 上（事件委托），那么这个监听器**永远不会被移除** ——
 *   访问过的每个页面都会在 #view 上留下一个监听器，越积越多。
 *
 * 这个 bug 的表现很有迷惑性：
 *   · 在世界书页点卡片，会同时触发之前"角色卡页"留下的监听器，
 *     它拿着世界书的 id 去请求 /character-cards/{id}，于是弹出一堆「角色卡不存在」
 *   · 一个按钮被 N 个监听器各开一次弹窗，要关 N 次
 *   · 多个监听器依次把按钮改成"加载中"，后一个会把前一个的 spinner 当成原文存下来，
 *     结果按钮变成空白且卡在禁用状态
 *
 * 用 AbortController 之后，切页时 signal.abort() 会一次性摘掉该页所有监听器。
 */
let routeAbort = null;

function showChrome(loggedIn) {
  document.getElementById('topbar').hidden = !loggedIn;
  if (!loggedIn) return;

  const user = session.user;
  $('#user-chip').textContent = user ? `${user.username}（#${user.id}）` : '';

  // 顶栏的监听器也只绑一次，避免重复绑定导致一次点击触发多次
  if (showChrome.bound) return;
  showChrome.bound = true;

  $('#nav').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-route]');
    if (btn) navigate(btn.dataset.route);
  });
  $('#btn-logout').addEventListener('click', () => {
    auth.logout();
    toastOk('已退出登录');
    start();
  });
}

function highlightNav() {
  $$('#nav .nav-item').forEach((b) =>
    b.classList.toggle('active', b.dataset.route === currentRoute),
  );
  document.title = `${ROUTES[currentRoute]?.title || ''} · 云梦枢`;
}

function navigate(route) {
  if (!ROUTES[route]) route = 'cards';

  // 先摘掉上一个页面的所有监听器，再渲染新页面
  if (routeAbort) routeAbort.abort();
  routeAbort = new AbortController();

  currentRoute = route;
  highlightNav();

  ROUTES[route]
    .render(view, { signal: routeAbort.signal })
    .catch((err) => {
      if (routeAbort.signal.aborted) return; // 已经切走了，忽略旧页面的报错
      mount(
        view,
        `<div class="alert danger">渲染失败：${esc(err.message || String(err))}</div>`,
      );
    });
}

/* ==================================================================
   请求日志面板
   ================================================================== */
const logEntries = [];
const MAX_LOG = 80;

function initRequestLog() {
  const panel = document.getElementById('reqlog');
  const list = document.getElementById('reqlog-list');
  const countEl = document.getElementById('reqlog-count');

  /**
   * ★ 开关抽屉必须同时做两件事：
   *   1. 设置 hidden 属性（CSS 里有 [hidden]{display:none!important} 兜底）
   *   2. 给 body 加/去 log-open，让主内容区让出宽度
   *
   * 之前只做了第 1 件，而且因为 .reqlog 写了 display:flex，
   * 作者样式盖过了浏览器内置的 [hidden]{display:none}，
   * 结果「关闭」按钮点了完全没反应。这类问题只在浏览器里才看得见。
   */
  function setOpen(open) {
    panel.hidden = !open;
    document.body.classList.toggle('log-open', open);
    $('#btn-reqlog').classList.toggle('active', open);
    if (open) renderLog();
  }

  $('#btn-reqlog').addEventListener('click', () => setOpen(panel.hidden));
  $('#reqlog-close').addEventListener('click', () => setOpen(false));
  $('#reqlog-clear').addEventListener('click', () => {
    logEntries.length = 0;
    renderLog();
  });

  // 键盘用户可以按 Esc 关掉抽屉（和弹窗的操作习惯保持一致）
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !panel.hidden) setOpen(false);
  });

  function renderLog() {
    countEl.textContent = logEntries.length ? `${logEntries.length} 条` : '暂无';
    if (!logEntries.length) {
      list.innerHTML = `<div class="rl" style="cursor:default;color:#7c8899">
        还没有请求。在界面上点任何东西，这里就会记录下来。</div>`;
      return;
    }
    list.innerHTML = logEntries
      .map((e, i) => {
        const st = e.status || 0;
        const cls = st >= 500 || st === 0 ? 'rl-st-5' : st >= 400 ? 'rl-st-4' : 'rl-st-2';
        return `
        <div class="rl" data-i="${i}">
          <div class="rl-line">
            <span class="rl-method">${esc(e.method)}</span>
            <span class="rl-path" title="${esc(e.url)}">${esc(e.url)}</span>
            <span class="rl-status ${cls}">${st || 'ERR'}</span>
            <span class="rl-dur">${e.durationMs}ms</span>
          </div>
          <div class="rl-body hidden" data-body="${i}"></div>
        </div>`;
      })
      .join('');
  }

  list.addEventListener('click', (e) => {
    const row = e.target.closest('.rl');
    if (!row) return;
    const i = row.dataset.i;
    const bodyEl = row.querySelector(`[data-body="${i}"]`);
    if (!bodyEl) return;

    if (bodyEl.classList.contains('hidden')) {
      const entry = logEntries[Number(i)];
      bodyEl.innerHTML = `
        ${entry.requestId ? `<div><b>request_id</b> ${esc(entry.requestId)}</div>` : ''}
        ${entry.error ? `<div style="color:#ef6d5a"><b>网络错误</b> ${esc(entry.error)}</div>` : ''}
        ${
          entry.requestBody !== undefined
            ? `<div class="mt8"><b>请求体</b>\n${esc(safeJson(entry.requestBody))}</div>`
            : ''
        }
        ${
          entry.responseBody !== undefined
            ? `<div class="mt8"><b>响应体</b>\n${esc(safeJson(entry.responseBody))}</div>`
            : ''
        }`;
      bodyEl.classList.remove('hidden');
    } else {
      bodyEl.classList.add('hidden');
    }
  });

  onRequestLogged((entry) => {
    logEntries.unshift(entry);
    if (logEntries.length > MAX_LOG) logEntries.length = MAX_LOG;
    if (!panel.hidden) renderLog();
    else {
      // 面板关着时也更新计数，让用户知道有请求发生
      countEl.textContent = `${logEntries.length} 条`;
    }
  });

  renderLog();
}

/* ==================================================================
   启动
   ================================================================== */
async function start() {
  const topbar = document.getElementById('topbar');

  if (!session.isLoggedIn) {
    topbar.hidden = true;
    renderAuth(view, {
      onLoggedIn: async () => {
        showChrome(true);
        // 顶栏的事件监听每次登录都要重新绑，所以先重置整个顶栏
        location.reload();
      },
    });
    return;
  }

  // 有令牌，但用户信息可能丢了（比如手动清了 localStorage 的一部分）
  if (!session.user) {
    try {
      await auth.loadMe();
    } catch {
      auth.logout();
      start();
      return;
    }
  }

  showChrome(true);
  highlightNav();
  // ★ 启用中的 CSS 主题插件在这里生效（取一次拼好的样式，注入 <style>）。
  //   不 await：主题是锦上添花，失败了不能挡住主界面（applyPluginTheme 自己吞异常）。
  applyPluginTheme();
  navigate(currentRoute);
}

/* 令牌过期时自动退回登录页 —— 拦截所有 401 太分散，
   这里用最直接的办法：在 api.js 抛错后由各视图提示，
   同时在全局加一个 fetch 结果的兜底检查。 */
window.addEventListener('unhandledrejection', (e) => {
  const err = e.reason;
  if (err?.status === 401) {
    auth.logout();
    toastErr('登录状态已失效，请重新登录');
    start();
  }
});

initRequestLog();
start();
