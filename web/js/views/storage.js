/* ============================================================
   存储设置（数据库后端）—— 浏览器端的"切换存储方式"

   ==================== 为什么要有它 ====================
   在此之前，浏览器端想换存储只能**自己找到 .env 再改、然后重启后端**。
   桌面版早就有菜单「数据 → 切换存储方式…」，浏览器端却只有一句提示 ——
   用户的原话是"web 端并没有选择数据库的选项，只是提示选择了什么数据库"。

   ==================== ★ 两处入口，**同一份实现** ====================
   1. 顶栏导航「存储设置」（登录后）；
   2. **登录页**上的按钮（`openStorageDialog`）。

   为什么登录页也要有：用户的真实困境是"**登录不上**，因为连错了库" ——
   而第一版只把入口放在登录后的顶栏里，于是"登录不上"时根本进不去，
   **修不了它本该修的那个问题**（文案还写着"点顶部导航"，那是登录后才有的）。
   所以后端也相应放开了登录要求（见 app/api/v1/storage.py 顶部：改用
   本机来源 + Host 校验 + 自定义头三道闸）。

   ★ 两条入口**必须共用这一份代码**：本项目吃过"同一个决策两处各写一份、
     只接线了一处"的亏（见 pitfalls §32）。所以这里是同一个 bodyHTML/bind，
     只是外层容器不同（页面 vs 弹窗）。

   ==================== ★ 能力边界（必须如实告诉用户）====================
   桌面版切完能**自动重启后端**，因为后端进程是壳自己起的。
   浏览器端**做不到**：后端是独立的 uvicorn 进程，网页管不到它。
   所以保存后只能说"去重启后端才生效"，绝不能假装已经生效了。
   ============================================================ */

import { api } from 'hne/api';
import {
  $,
  confirmDialog,
  esc,
  freshViewSignal,
  modal,
  mount,
  mountError,
  toastErr,
  toastOk,
} from 'hne/ui';

/** 路由视图用的信号（与弹窗分开，互不干扰）。 */
let viewSignal = null;

/** 后端字段名 → 中文标签（别让用户看 HNE_MYSQL_HOST 这种东西）。 */
const LABELS = {
  HNE_DB_BACKEND: '存储方式',
  HNE_SQLITE_PATH: '本机文件路径',
  HNE_MYSQL_HOST: 'MySQL 主机',
  HNE_MYSQL_PORT: 'MySQL 端口',
  HNE_MYSQL_USER: 'MySQL 用户名',
  HNE_MYSQL_PASSWORD: 'MySQL 口令',
  HNE_MYSQL_DB: '数据库名',
};

/* ==================================================================
   入口 1：顶栏导航（登录后）
   ================================================================== */
export async function renderStorage(root, ctx = {}) {
  viewSignal = freshViewSignal('storage', ctx.signal);
  await renderRoute(root, viewSignal.signal);
}

async function renderRoute(root, signal) {
  const isCurrent = () => viewSignal?.signal === signal && !signal?.aborted;
  if (!isCurrent()) return;

  let state;
  try {
    state = await readState();
  } catch (err) {
    if (!isCurrent()) return;
    mountError(root, err?.toDisplay ? err.toDisplay() : String(err), () =>
      renderRoute(root, viewSignal?.signal),
    );
    return;
  }
  if (!isCurrent()) return;

  mount(
    root,
    `
    <div class="page-head">
      <div>
        <h1>存储设置</h1>
        <div class="sub">决定数据存在哪里：一个本机文件（SQLite），或你已有的 MySQL。</div>
      </div>
    </div>
    ${bodyHTML(state)}
    <div class="page-actions" style="margin-top:12px">
      <button class="btn" id="btn-save">保存</button>
      <button class="btn sec" id="btn-test">测试连接</button>
      <span class="spacer"></span>
      <span id="storage-status" class="small faint"></span>
    </div>`,
  );
  bind(root, state, {
    onSaved: () => renderRoute(root, viewSignal?.signal),
  });
}

/* ==================================================================
   入口 2：登录页上的弹窗（**登录前**也能用）
   ================================================================== */
/**
 * 打开存储设置弹窗。
 *
 * @param {object} [opts]
 * @param {() => void} [opts.onSaved] 保存成功后回调（登录页用它刷新提示）
 */
export async function openStorageDialog({ onSaved } = {}) {
  const m = modal({
    title: '存储设置',
    width: 'wide',
    bodyHTML: '<div class="empty"><div class="big">⏳</div><div>读取当前存储方式…</div></div>',
    footHTML: `
      <span id="storage-status" class="small faint" style="margin-right:auto"></span>
      <button class="btn sec" id="btn-test">测试连接</button>
      <button class="btn" id="btn-save">保存</button>
      <button class="btn sec" data-close>关闭</button>`,
  });

  // ★ 弹窗自己一个信号：它与路由视图是**两个独立实例**，
  //   共用同一个信号会导致"关掉弹窗把页面也 abort 了"这种串台。
  const controller = new AbortController();

  try {
    const state = await readState();
    mount(m.body, bodyHTML(state, { compact: true }));
    bind(m.root, state, {
      signal: controller.signal,
      onSaved: async () => {
        // 存完重新读一遍（口令是否已设、键来源都可能变了）
        try {
          const fresh = await readState();
          mount(m.body, bodyHTML(fresh, { compact: true }));
          bind(m.root, fresh, { signal: controller.signal, onSaved });
        } catch {
          /* 重新读取失败不影响"已保存"这个事实 */
        }
        if (typeof onSaved === 'function') onSaved();
      },
    });
  } catch (err) {
    mount(m.body, `
      <div class="empty">
        <div class="big">⚠</div>
        <div>${esc(err?.message || String(err))}</div>
        <div class="small faint mt8">
          如果你是通过 127.0.0.1 打开的页面，这里通常不会失败。
        </div>
      </div>`);
  }

  return m;
}

/** 读一次当前状态（两个入口共用）。 */
async function readState() {
  return api.get('/system/storage', undefined, { timeoutMs: 15000 });
}

/* ==================================================================
   表单本体（两个入口共用 —— 这是"不各写一份"的关键）
   ================================================================== */
function bodyHTML(state, { compact = false } = {}) {
  const isMysql = state.backend === 'mysql';
  const v = state.values || {};

  const pinned = (state.pinned || []).length
    ? `<div class="card" style="border-left:3px solid var(--warn,#d97706)">
         <b>⚠ 有些设置被环境变量钉住了</b>
         <div class="small" style="line-height:1.7">
           这些键在<code>环境变量</code>里已经存在，而<b>环境变量优先于配置文件</b>，
           所以在这里改了<b>不会生效</b>（而且不会报错）：
           <code>${(state.pinned || []).map(esc).join('</code> <code>')}</code>
           <br>要改它们，得先去掉对应的环境变量（或在启动脚本里改）。
         </div>
       </div>`
    : '';

  const sourceTag = (key) => {
    const src = (state.sources || {})[key];
    if (src === 'env') return ' <span class="badge warn">来自环境变量</span>';
    if (src === 'file') return ' <span class="badge info">来自配置文件</span>';
    return '';
  };

  const field = (key, { type = 'text', placeholder = '', hint = '' } = {}) => {
    // 口令**从不回显**（后端也不回传），留空 = 不改它
    const value = key === 'HNE_MYSQL_PASSWORD' ? '' : (v[key] ?? '');
    const secretHint = key === 'HNE_MYSQL_PASSWORD' && state.secret_set?.HNE_MYSQL_PASSWORD
      ? '<div class="hint">已保存过口令。<b>留空 = 不改它</b>（推荐）；只有想换口令时才填。</div>'
      : '';
    return `
      <div class="field">
        <label for="f-${key}">${esc(LABELS[key] || key)}${sourceTag(key)}</label>
        <input id="f-${key}" data-key="${key}" type="${type}"
               value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" />
        ${hint ? `<div class="hint">${hint}</div>` : ''}
        ${secretHint}
      </div>`;
  };

  // ★ 当前这份本机文件的体检结果要**显示出来**：
  //   用户问得最多的就是"我现在这份库里有我的数据吗"。
  //   （这个字段后端一直在返回，前端起初没消费 —— 属于"取回来没用"的冗余。）
  const cur = state.current_sqlite;
  const currentLine = !isMysql && cur
    ? `<br><b>这份库里有什么：</b>`
      + (cur.exists
        ? `${esc(cur.summary)}`
        : '<span class="badge warn">文件还不存在</span>')
    : '';

  return `
    <div class="card">
      <div class="small" style="line-height:1.8">
        <b>当前在用：</b>${isMysql ? 'MySQL 数据库' : '本机文件（SQLite）'}
        &nbsp;<span class="faint">${esc(state.database || '')}</span>
        ${currentLine}
        <br><b>配置文件：</b><code>${esc(state.env_file)}</code>
        ${compact ? '' : `
          <br><b>数据目录：</b><code>${esc(state.default_sqlite_path)}</code> 所在的那一层`}
      </div>
    </div>

    ${pinned}

    <div class="card">
      <div class="field">
        <label for="f-HNE_DB_BACKEND">存储方式</label>
        <select id="f-HNE_DB_BACKEND" data-key="HNE_DB_BACKEND">
          <option value="sqlite"${isMysql ? '' : ' selected'}>本机文件（SQLite）· 装完即用</option>
          <option value="mysql"${isMysql ? ' selected' : ''}>MySQL 数据库 · 需要你已经有 MySQL 8</option>
        </select>
      </div>

      <div id="sec-sqlite" style="display:${isMysql ? 'none' : ''}">
        ${field('HNE_SQLITE_PATH', {
          placeholder: state.default_sqlite_path || '',
          hint: '留空 = 用默认位置（数据目录下的 <code>data/app.sqlite3</code>）。'
            + '★ <b>换到别处不会把现有数据搬过去</b> —— 见下面的"旧数据在哪"。',
        })}
        <div id="sqlite-path-check" class="hint"></div>
      </div>

      <div id="sec-mysql" style="display:${isMysql ? '' : 'none'}">
        ${field('HNE_MYSQL_HOST', { placeholder: '127.0.0.1' })}
        ${field('HNE_MYSQL_PORT', { placeholder: '3306' })}
        ${field('HNE_MYSQL_USER', { placeholder: 'narrative_app' })}
        ${field('HNE_MYSQL_PASSWORD', { type: 'password' })}
        ${field('HNE_MYSQL_DB', { placeholder: 'narrative_engine' })}
      </div>
    </div>

    <div class="card" style="border-left:3px solid var(--info,#3b82f6)">
      <b>★ 保存之后要重启后端</b>
      <div class="small" style="line-height:1.8">
        这里改的是<b>配置文件</b>，而当前正在跑的后端进程还在用旧配置 ——
        浏览器<b>无法替你重启后端</b>（那个进程不归网页管）。
        <br>所以保存后请回到你把服务跑起来的那个窗口，<b>停掉再重新启动一次</b>。
        <br><span class="faint">桌面版的「数据」菜单能做到"改完自动重启"，浏览器端做不到，这是环境差异。</span>
      </div>
    </div>

    <div class="card">
      <b>两种存储的关系</b>
      <div class="small" style="line-height:1.8">
        · 它们是<b>两个互相独立的库</b>：数据与账号都不互通，切过去要重新注册账号。
        <br>· 角色卡 / 世界书可以用「导出 / 导入」在两边搬运；会话对话目前没有搬运工具。
        <br>· <b>长期记忆（向量库）是本地文件</b>，与数据库分开存放 —— 想让桌面版与浏览器共用，
          两边要指向同一个目录（<code>HNE_CHROMA_PERSIST_DIR</code>）。
        <br>· 保存时会自动备份一份原配置（<code>.env.bak</code>），改坏了可以自己还原。
      </div>
    </div>

    ${historyHTML(state)}`;
}

/**
 * 「旧数据在哪」——最近用过的本机文件路径。
 *
 * ==================== 为什么要有这一块 ====================
 * 用户真实踩过：改了「本机文件路径」之后登录不上，而他**找不到原来那份数据在哪**，
 * 于是合理怀疑"你是不是把我文件换位置了 / 改我密码了"。
 * 光有"数据不会自动搬家"这句提示不够 —— 必须**把旧路径列出来**，
 * 让他能自己回去看、甚至改回去。
 */
function historyHTML(state) {
  const items = state.sqlite_history || [];
  if (!items.length) return '';
  const current = (state.values || {}).HNE_SQLITE_PATH || '';
  const rows = items.map((item, idx) => {
    const isCurrent = item.path === current;
    // 只展示账号数这类"能不能用"的信息，不暴露任何密钥
    const status = item.exists
      ? `<span class="badge ${item.user_count > 0 ? 'ok' : 'warn'}">${esc(item.summary)}</span>`
      : '<span class="badge warn">文件已不在</span>';
    return `
      <div class="small" style="margin:6px 0;line-height:1.7">
        <code style="word-break:break-all">${esc(item.path)}</code>
        ${isCurrent ? '<span class="badge info">当前在用</span>' : ''}
        ${status}
        ${item.last_used ? `<span class="faint"> · 上次使用 ${esc(item.last_used)}</span>` : ''}
        ${isCurrent ? '' : `<button class="ghost-btn mini" data-use-path="${idx}">用这个路径</button>`}
      </div>`;
  }).join('');

  return `
    <div class="card">
      <b>旧数据在哪（最近 ${items.length} 条）</b>
      <div class="small faint" style="line-height:1.7">
        换路径时<b>原文件不会被删除、也不会被搬走</b>，它还在原来的地方。
        下面是最近用过的几个位置，你可以照着去找回旧数据（或直接点「用这个路径」切回去）。
      </div>
      ${rows}
    </div>`;
}

/* ==================================================================
   交互（两个入口共用）
   ================================================================== */
/**
 * @param {Element} scope  查询按钮/输入框的范围（页面的根 或 弹窗的 mask）
 * @param {object}  state  当前状态
 * @param {object}  opts
 * @param {AbortSignal} [opts.signal]
 * @param {() => void}  [opts.onSaved] 保存成功后的回调
 */
function bind(scope, state, { signal, onSaved } = {}) {
  const status = (msg, kind = '') => {
    const el = $('#storage-status', scope);
    if (!el) return;
    el.textContent = msg || '';
    el.style.color = kind === 'err' ? 'var(--danger,#dc2626)'
      : kind === 'ok' ? 'var(--ok,#16a34a)' : '';
  };

  const select = $('#f-HNE_DB_BACKEND', scope);
  const syncSections = () => {
    const mysql = select.value === 'mysql';
    $('#sec-sqlite', scope).style.display = mysql ? 'none' : '';
    $('#sec-mysql', scope).style.display = mysql ? '' : 'none';
  };
  select.addEventListener('change', syncSections, { signal });
  syncSections();

  function collect() {
    const out = {};
    for (const el of scope.querySelectorAll('[data-key]')) out[el.dataset.key] = el.value;
    return out;
  }

  // ---- 「用这个路径」：把历史里的路径填回输入框（不直接保存，仍走保存流程）----
  const pathInput = $('#f-HNE_SQLITE_PATH', scope);
  const pathCheck = $('#sqlite-path-check', scope);
  for (const btn of scope.querySelectorAll('[data-use-path]')) {
    btn.addEventListener('click', () => {
      const item = (state.sqlite_history || [])[Number(btn.dataset.usePath)];
      if (!item || !pathInput) return;
      pathInput.value = item.path;
      checkPath();
      toastOk('已填入路径，点「保存」才会生效');
    }, { signal });
  }

  // ---- 换路径前的实时体检：告诉用户"这里有没有你的账号" ----
  //
  //  ★ 这是用户真实要求的（方案 A），也是他真实踩过的坑：
  //    换了个路径 → 登录不上 → 以为"你把我的密码改了"。
  let checkTimer = null;
  async function checkPath() {
    if (!pathCheck || !pathInput) return;
    const target = pathInput.value.trim();
    pathCheck.innerHTML = '<span class="faint">正在检查这个路径…</span>';
    try {
      const info = await api.post('/system/storage/inspect', { path: target });
      if (info.level === 'ok') {
        pathCheck.innerHTML = `✅ 这个文件里有数据：${esc(info.summary)}`;
        pathCheck.style.color = 'var(--ok,#16a34a)';
      } else {
        pathCheck.innerHTML = `⚠ ${esc(info.warning || info.summary)}`
          + `<br><span class="faint">当前用：${esc(info.summary)}</span>`;
        pathCheck.style.color = 'var(--warn,#d97706)';
      }
    } catch (err) {
      // 体检失败不该挡住保存（后端保存时还会再拦一次）
      pathCheck.innerHTML = `<span class="faint">（检查失败：${esc(err?.message || '未知')}）</span>`;
    }
  }
  if (pathInput) {
    pathInput.addEventListener('input', () => {
      clearTimeout(checkTimer);
      checkTimer = setTimeout(checkPath, 400);
    }, { signal });
  }

  $('#btn-test', scope).addEventListener('click', async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    status('测试中…');
    try {
      const result = await api.post('/system/storage/test', collect());
      status(result.message || (result.ok ? '连接成功' : '连接失败'), result.ok ? 'ok' : 'err');
      (result.ok ? toastOk : toastErr)(result.message || (result.ok ? '连接成功' : '连接失败'));
    } catch (err) {
      status(err?.message || '测试失败', 'err');
      toastErr(err?.message || '测试失败');
    } finally {
      btn.disabled = false;
    }
  }, { signal });

  $('#btn-save', scope).addEventListener('click', async (event) => {
    // ★★★ 必须在**任何 await 之前**取 currentTarget（真实 bug，见下）。
    //
    //   浏览器规则：事件处理函数里 `event.currentTarget` 只在**同步执行期间**有效，
    //   一旦 await 让出控制权，它就被置为 **null**。
    //   我原来把 `const btn = event.currentTarget` 写在 `await confirmDialog(...)`
    //   **之后** —— 于是 `btn.disabled = true` 抛 TypeError，
    //   整个处理函数在这里**静默中断**：
    //     · PUT 请求根本没发出去（后端日志里一次都没有）；
    //     · 用户点完"保存"看不到任何反应，连错误提示都没有。
    //   这个 bug 只在"点保存 → 弹确认 → 点确认"这条真人路径上才出现，
    //   而我的自动探针当时停在弹窗出现那一步就收工了 —— **所以没抓到**。
    //   教训：验收要走到"最后一步真的生效"，不能停在"界面看起来对"。
    const btn = event.currentTarget;
    const payload = collect();
    const target = payload.HNE_DB_BACKEND === 'mysql' ? 'MySQL 数据库' : '本机文件（SQLite）';

    // ★ 换本机文件路径时，先在**界面上**明确确认一次（方案 A）。
    //   比后端那句拦截更早、更友好：用户还没点保存就知道"这个路径里有什么"。
    const isSqlite = payload.HNE_DB_BACKEND !== 'mysql';
    const newPath = String(payload.HNE_SQLITE_PATH || '').trim();
    const curPath = String((state.values || {}).HNE_SQLITE_PATH || '').trim();
    // ★ `confirmedEmpty` 只在**真的给用户看过警告并得到确认**后才置 true。
    //   若体检本身失败（网络/接口异常），保持 false —— 让后端那道兜底去拦，
    //   绝不能"因为没检查成功就默认放行"。
    let confirmedEmpty = false;
    if (isSqlite && newPath !== curPath) {
      try {
        const info = await api.post('/system/storage/inspect', { path: newPath });
        if (info.level !== 'ok') {
          const proceed = await confirmDialog({
            title: '⚠ 这个路径里没有你的数据',
            width: 'wide',
            confirmText: '我知道了，仍然要换过去',
            danger: true,
            message: `${info.warning || info.summary}\n\n`
              + `目标路径：${info.path}\n`
              + `这份文件现在的内容：${info.summary}\n\n`
              + '★ 换过去之后，你在原来那份库里的账号与数据**不会跟过来**'
              + '（原文件不会被删，仍留在原处，可在本页"旧数据在哪"里找到）。\n'
              + '★ 换完需要重启后端，然后在新库里**重新注册**账号。',
          });
          if (!proceed) return;
          confirmedEmpty = true;
        }
      } catch {
        // 体检失败不阻断 → 但也不置 confirm_empty，交给后端兜底拦一次
      }
    }

    const ok = await confirmDialog({
      title: '保存存储设置',
      message: `将把存储方式保存为「${target}」。\n\n`
        + '★ 数据不会自动搬家；★ 保存后需要你手动重启后端才会生效。',
      confirmText: '保存',
    });
    if (!ok) return;

    btn.disabled = true;
    status('保存中…');
    try {
      // ★ 只有用户在界面上**真的确认过**"我知道这是空库"时才带这个标记；
      //   否则让后端那道兜底拦下来（它是给"绕过界面直接调接口"准备的）。
      const result = await api.put('/system/storage', {
        ...payload,
        confirm_empty: confirmedEmpty,
      });
      // ★ 保存成功要有**看得见**的反馈（用户明确提过："建议加个提醒保存成功之类的"）。
      //   三重反馈，因为单靠其中任何一种都可能被错过：
      //     ① 页面上那行状态文字；
      //     ② 右下角 toast 轻提示；
      //     ③ 一个明确的弹窗 —— 把"下一步要做什么（重启后端）"讲清楚。
      status(`✅ 已保存：${target}（重启后端后生效）`, 'ok');
      toastOk('已保存');
      const done = modal({
        title: '✅ 已保存',
        width: 'narrow',
        bodyHTML: `
          <div style="line-height:1.9">
            存储方式已保存为 <b>${esc(target)}</b>。
            <div class="mt8 small faint">
              写入的配置文件：<br><code>${esc(result?.env_file || '')}</code>
              ${result?.backup ? `<br>原配置已备份到：<br><code>${esc(result.backup)}</code>` : ''}
            </div>
            <div class="mt14" style="border-left:3px solid var(--warn,#d97706);padding-left:10px">
              <b>还差最后一步：重启后端</b><br>
              <span class="small">当前进程仍在用旧配置。请回到启动服务的那个窗口，
              停掉再重新启动一次，改动才会生效。</span>
            </div>
          </div>`,
        footHTML: '<button class="btn" data-close>知道了</button>',
      });
      // 弹窗关掉之后再刷新状态，避免用户还没看完数字就跳变
      done.root.addEventListener('click', (e) => {
        if (e.target.closest('[data-close]') || e.target === done.root) {
          if (typeof onSaved === 'function') onSaved(result);
        }
      });
    } catch (err) {
      // 逐字段错误要摊开给用户看（只报一句"保存失败"等于让他猜）
      const detail = err?.data?.detail || err?.detail;
      const errors = detail?.errors || {};
      const lines = Object.entries(errors).map(([k, msg]) => `· ${LABELS[k] || k}：${msg}`);
      const pinned = detail?.pinned || [];
      const text = lines.length
        ? lines.join('\n')
        : pinned.length
          ? `这些键被环境变量钉住了，改配置文件不会生效：${pinned.join('、')}`
          : (err?.message || '保存失败');
      status(text.split('\n')[0], 'err');
      toastErr(text);
    } finally {
      btn.disabled = false;
    }
  }, { signal });
}
