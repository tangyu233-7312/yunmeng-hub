/* ============================================================
   通用 UI 组件：DOM 助手、轻提示、弹窗、表单片段

   都是手写的，没有框架。所有插入 HTML 的地方都必须经过 esc()，
   否则用户输入的内容（角色卡名、设定文本）会变成可执行的 HTML。
   ============================================================ */

/* ---------------- DOM 助手 ---------------- */

/** HTML 转义 —— 凡是把用户数据拼进 innerHTML，都必须经过它 */
export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

/** 用 HTML 字符串创建元素 */
export function h(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 把容器内容替换掉 */
export function mount(container, html) {
  container.innerHTML = html;
  return container;
}

/* ---------------- 格式化 ---------------- */
export function fmtDate(value) {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtRelative(value) {
  if (!value) return '—';
  const t = new Date(value).getTime();
  if (Number.isNaN(t)) return '—';
  const diff = Date.now() - t;
  if (diff < 60_000) return '刚刚';
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`;
  if (diff < 30 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`;
  return fmtDate(value);
}

/** 角色名首字（没有头像时显示） */
export function initials(name) {
  const s = String(name || '?').trim();
  return s ? s[0].toUpperCase() : '?';
}

/* ---------------- 轻提示 ---------------- */
export function toast(message, kind = 'info', ms = 4200) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const node = h(`<div class="toast ${kind}">${esc(message)}</div>`);
  box.appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .2s';
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 220);
  }, ms);
}

export const toastOk = (m) => toast(m, 'ok');
export const toastErr = (m) => toast(m, 'err', 7000);
export const toastWarn = (m) => toast(m, 'warn', 5600);

/* ---------------- 弹窗 ---------------- */
/**
 * 打开一个弹窗。
 * @returns {{root:HTMLElement, body:HTMLElement, foot:HTMLElement, close:Function}}
 */
export function modal({ title, bodyHTML = '', footHTML = '', width = '' } = {}) {
  const mask = h(`
    <div class="modal-mask">
      <div class="modal ${width}">
        <div class="modal-head">
          <h2>${esc(title)}</h2>
          <button class="x-btn" data-close>×</button>
        </div>
        <div class="modal-body">${bodyHTML}</div>
        <div class="modal-foot">${footHTML}</div>
      </div>
    </div>`);

  document.getElementById('modal-root').appendChild(mask);

  const body = $('.modal-body', mask);
  const foot = $('.modal-foot', mask);

  function close() {
    mask.remove();
    document.removeEventListener('keydown', onKey);
  }
  function onKey(e) {
    if (e.key === 'Escape') close();
  }
  document.addEventListener('keydown', onKey);

  mask.addEventListener('click', (e) => {
    if (e.target === mask) close(); // 点遮罩关闭
    if (e.target.closest('[data-close]')) close();
  });

  // 弹窗里的第一个输入框自动聚焦，省一次点击
  const firstInput = $('input:not([type=checkbox]), textarea, select', body);
  if (firstInput) setTimeout(() => firstInput.focus(), 30);

  return { root: mask, body, foot, close };
}

/**
 * 简单的确认框（只有确定/取消，没有额外选项时用它）。
 * 需要"勾选项"的场景请用 deleteCardDialog 那种自定义弹窗。
 */
export function confirmDialog({
  title,
  message,
  confirmText = '确定',
  danger = false,
} = {}) {
  return new Promise((resolve) => {
    const m = modal({
      title,
      width: 'narrow',
      bodyHTML: `<div>${esc(message)}</div>`,
      footHTML: `
        <button class="btn sec" data-cancel>取消</button>
        <button class="btn ${danger ? 'danger' : ''}" data-ok>${esc(confirmText)}</button>`,
    });
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      m.close();
      resolve(v);
    };
    $('[data-ok]', m.root).addEventListener('click', () => finish(true));
    $('[data-cancel]', m.root).addEventListener('click', () => finish(false));
    m.root.querySelector('.x-btn').addEventListener('click', () => finish(false));
  });
}

/* ---------------- 表单片段 ---------------- */

/** 一个表单字段 */
export function field(label, inputHTML, hint = '') {
  return `
    <div class="field">
      <label>${esc(label)}</label>
      ${inputHTML}
      ${hint ? `<div class="hint">${hint}</div>` : ''}
    </div>`;
}

export function textField(label, name, value = '', opts = {}) {
  const { placeholder = '', hint = '', type = 'text', maxlength = '' } = opts;
  return field(
    label,
    `<input type="${type}" name="${esc(name)}" value="${esc(value ?? '')}"
       placeholder="${esc(placeholder)}" ${maxlength ? `maxlength="${maxlength}"` : ''} />`,
    hint,
  );
}

export function textareaField(label, name, value = '', opts = {}) {
  const { placeholder = '', hint = '', rows = 4 } = opts;
  return field(
    label,
    `<textarea name="${esc(name)}" rows="${rows}" placeholder="${esc(placeholder)}">${esc(value ?? '')}</textarea>`,
    hint,
  );
}

export function selectField(label, name, options, value, hint = '') {
  const opts = options
    .map(
      (o) =>
        `<option value="${esc(o.value)}" ${String(o.value) === String(value) ? 'selected' : ''}>${esc(o.label)}</option>`,
    )
    .join('');
  return field(label, `<select name="${esc(name)}">${opts}</select>`, hint);
}

export function numberField(label, name, value, opts = {}) {
  const { min = '', max = '', step = '1', hint = '' } = opts;
  return field(
    label,
    `<input type="number" name="${esc(name)}" value="${esc(value ?? '')}"
       ${min !== '' ? `min="${min}"` : ''} ${max !== '' ? `max="${max}"` : ''} step="${step}" />`,
    hint,
  );
}

export function rangeField(label, name, value, opts = {}) {
  const { min = 0, max = 2, step = 0.1, hint = '' } = opts;
  return field(
    label,
    `<div class="row tight" style="align-items:center">
       <input type="range" name="${esc(name)}" min="${min}" max="${max}" step="${step}"
              value="${esc(value)}" oninput="this.nextElementSibling.textContent=this.value" />
       <output style="flex:0 0 46px;text-align:right" class="mono">${esc(value)}</output>
     </div>`,
    hint,
  );
}

export function checkboxField(label, name, checked = false, hint = '') {
  return `
    <div class="field">
      <label class="checkbox-line">
        <input type="checkbox" name="${esc(name)}" ${checked ? 'checked' : ''} />
        <span>${esc(label)}</span>
      </label>
      ${hint ? `<div class="hint">${hint}</div>` : ''}
    </div>`;
}

/* ---------------- 从表单读值 ---------------- */
export function formValues(root) {
  const out = {};
  for (const node of $$('[name]', root)) {
    const name = node.getAttribute('name');
    if (node.type === 'checkbox') out[name] = node.checked;
    else if (node.type === 'number' || node.type === 'range') {
      out[name] = node.value === '' ? null : Number(node.value);
    } else out[name] = node.value;
  }
  return out;
}

/* ---------------- 视图监听器生命周期 ---------------- */

/**
 * 给一个视图拿一个"可重复调用"的信号，用来绑挂在常驻 `#view` 上的委托监听器。
 *
 * ★ 为什么需要它（真实事故）：
 *   `#view` 是常驻元素，切页只替换内部内容，挂在它上面的委托监听器**不会自动消失**。
 *   app.js 已经用路由级 AbortController 解决了"切页泄漏"，
 *   但**视图自己重渲染自己**（例如"测试"完刷新列表 → 再次调用 renderXxx）
 *   时会再挂一个监听器，越积越多：
 *       点一次「模型」按钮 → 弹出 N 个一模一样的弹窗
 *       （N = 累计渲染次数；用户实际反馈过，截图里有 3 个）
 *
 *   这里保证**一个视图同时只有一个活跃监听器批次**：
 *   每次调用都先 abort 上一个，再返回新的 signal。
 *
 * @param viewName    视图名（每个视图各自计数，互不干扰）
 * @param routeSignal 路由级信号（切页时 abort）。当前批次会跟着它一起失效。
 * @returns {{ signal: AbortSignal, dispose: () => void }}
 */
let _viewControllers = new Map();
export function freshViewSignal(viewName, routeSignal) {
  // 同一个视图的上一批监听器先摘掉（这是修 bug 的关键一步）
  _viewControllers.get(viewName)?.abort();

  const controller = new AbortController();
  _viewControllers.set(viewName, controller);

  if (routeSignal) {
    if (routeSignal.aborted) controller.abort();
    else routeSignal.addEventListener('abort', () => controller.abort(), { once: true });
  }

  return {
    signal: controller.signal,
    /** 主动失效（视图内部想提前收摊时用） */
    dispose: () => controller.abort(),
  };
}

/* ---------------- 富文本（角色卡开场白里的 HTML 卡片） ---------------- */

/** 可能出现在角色卡里的块级标签（用来判断"这段文字是不是一张 HTML 卡片"） */
const _HTML_HINTS =
  /<\s*(div|p|br|h[1-6]|ul|ol|li|table|thead|tbody|tr|td|th|section|article|header|footer|blockquote|pre|span|b|strong|i|em|hr|img|style|center|font|details|summary)\b/i;

/** 允许保留的标签：只留排版类，不留任何能执行/加载外部资源的东西 */
const _ALLOWED_TAGS = new Set([
  'div', 'span', 'p', 'br', 'hr', 'b', 'strong', 'i', 'em', 'u', 's', 'small',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'dl', 'dt', 'dd',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'caption', 'colgroup', 'col',
  'blockquote', 'pre', 'code', 'section', 'article', 'header', 'footer', 'figure', 'figcaption',
  'center', 'font', 'details', 'summary', 'img',
]);

/** 允许保留的属性（只留样式与表格结构，不留任何 URL / 事件） */
const _ALLOWED_ATTRS = new Set([
  'style', 'class', 'colspan', 'rowspan', 'align', 'valign', 'title',
  'width', 'height', 'alt',
]);

/** 危险标签：连同**内容**一起丢掉（script/style 里的文本不是给人看的） */
const _DROP_WITH_CONTENT = new Set([
  'script', 'iframe', 'object', 'embed', 'noscript', 'template', 'svg', 'math',
  // ★ head 里的这些不是"给人看的内容"：留着会把 <title> 之类当正文显示出来
  'meta', 'title', 'link', 'base',
]);

/**
 * 判断一段文本"看起来是不是 HTML"。
 *
 * 为什么需要它：角色卡的 `greeting` 在规范里是**纯文本**，
 * 但现实中很多人都用 HTML 写"状态栏 / 属性面板"卡片。
 * 全按纯文本渲染，用户看到的就只是一堆源码（真实反馈过）。
 */
export function looksLikeHtml(text) {
  return Boolean(text) && _HTML_HINTS.test(String(text));
}

/**
 * 把一段 HTML 清洗成"只能排版、不能乱来"的安全片段。
 *
 * ★ 为什么不用现成的 DOMPurify？
 *   本项目的前端是**零依赖、零构建**的（这本身是一个刻意的取舍），
 *   为这一个功能引入一个构建步骤不划算。
 *
 * ★ 安全边界（写清楚，别让下一个人以为这是万能过滤器）：
 *   · **不使用 innerHTML 来解析**，而是用 `DOMParser(..., 'text/html')` 得到一个
 *     **惰性文档**：里面的 `<img src>`、`<script>`、`<link>` 都不会被加载或执行；
 *   · 白名单之外的元素一律丢掉（危险标签连内容一起丢，其余标签"脱壳保留文本"）；
 *   · 属性只留 `style/class/表格结构` —— **任何 `on*` 事件、`href/src` 都不会留下**；
 *   · 因此即使角色卡是别人公开分享的，也不会在你的控制台里执行脚本。
 */
export function sanitizeHtml(html) {
  const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');

  const clean = (node, out) => {
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) {
        out.appendChild(document.createTextNode(child.nodeValue || ''));
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue; // 注释等一律不要

      const tag = child.tagName.toLowerCase();
      if (_DROP_WITH_CONTENT.has(tag)) continue;

      // ★ <style> 必须留下：角色卡的美化**基本全靠它**。
      //   以前整块丢掉，于是卡片只剩一堆没有样式的裸文字 ——
      //   用户的原话就是"HTML 没有渲染出来"。
      //   这里只清洗 CSS 里真正危险的东西；执行层面的兜底是 iframe 沙箱
      //   （没有 allow-scripts，卡片里的脚本根本不会跑）。
      if (tag === 'style') {
        const css = _sanitizeCss(child.textContent || '');
        if (css.trim()) {
          const styleEl = document.createElement('style');
          styleEl.textContent = css;
          out.appendChild(styleEl);
        }
        continue;
      }

      if (!_ALLOWED_TAGS.has(tag)) {
        // 未知标签：脱壳，只保留里面的文字（不丢内容，也不引入风险）
        clean(child, out);
        continue;
      }

      const el = document.createElement(tag);
      for (const attr of Array.from(child.attributes)) {
        const name = attr.name.toLowerCase();
        if (name === 'src') {
          // ★ 只有 <img> 且协议安全时才保留 src：
          //   图片是角色卡美化的常用手段（状态栏、头像框），但
          //   data:text/html 之类的"伪图片"必须挡掉。
          if (tag === 'img' && _SAFE_IMG_SRC.test(String(attr.value || '').trim())) {
            el.setAttribute('src', attr.value);
            el.setAttribute('loading', 'lazy');
            el.setAttribute('referrerpolicy', 'no-referrer');
          }
          continue;
        }
        if (!_ALLOWED_ATTRS.has(name)) continue;
        if (name === 'style') {
          el.setAttribute('style', _sanitizeCss(attr.value || ''));
        } else {
          el.setAttribute(name, attr.value);
        }
      }
      clean(child, el);
      out.appendChild(el);
    }
  };

  const box = document.createElement('div');
  clean(doc.body, box);
  return box.innerHTML;
}

/* ---------------- HTML 卡片：真正把它渲染出来 ---------------- */

/**
 * 清洗 CSS：只挡"能执行 / 能对外发请求做探针"的写法。
 *
 * ★ 为什么**不**顺手把 `url(...)` 也删掉？
 *   因为角色卡的背景图、状态栏图标全靠它 —— 删了就等于没美化。
 *   `url()` 最坏情况是让卡片作者知道"你看过这张卡"，代价可接受；
 *   而 `@import` 能拉一整个外部样式表、`expression()`/`javascript:`
 *   在老浏览器里能执行脚本，这两类必须挡。
 */
function _sanitizeCss(css) {
  return String(css || '')
    .replace(/@import[^;]*;?/gi, '')
    .replace(/expression\s*\(/gi, 'blocked(')
    .replace(/javascript\s*:/gi, '')
    .replace(/-moz-binding\s*:[^;]*;?/gi, '')
    .replace(/behavior\s*:[^;]*;?/gi, '');
}

/** img 的 src 白名单：http(s) / data:image / 站内绝对路径（挡掉 `//evil.com`） */
const _SAFE_IMG_SRC =
  /^(?:https?:\/\/|data:image\/(?:png|jpe?g|gif|webp|avif|svg\+xml);base64,|\/(?!\/))/i;

/**
 * 去掉包裹在整段 HTML 外面的 markdown 代码围栏。
 *
 * ★ 为什么必须做：非常多的角色卡把开场白写成
 *     ```html
 *     <!DOCTYPE html>…
 *     ```
 *   不剥掉的话，页面上会先出现一个孤零零的 "```"，看起来就像"没渲染成功"。
 *   只在**整段**被围栏包住时才剥，不做全文替换（正文里的 ``` 是内容）。
 */
function stripFences(text) {
  const s = String(text ?? '').trim();
  const m = s.match(/^```[a-zA-Z0-9_-]*[ \t]*\r?\n([\s\S]*?)\r?\n?```$/);
  return m ? m[1] : s;
}

/**
 * 把一段（可能含 HTML 的）文本渲染进 host，并附「渲染视图 / 源码」切换。
 *
 * ★ 为什么要用 iframe 而不是直接插 DOM：
 *   角色卡（尤其来自公开卡库的）自带 <style>，直接插进来会连控制台自己的
 *   样式一起改掉 —— 一条 `body{display:none}` 就能让整个界面消失。
 *   iframe 天然隔离样式；`sandbox` 用 **allow-same-origin 但不给 allow-scripts**：
 *   父页面能读内部高度做自适应，卡片里的脚本一行都跑不了。
 *
 * ★ 为什么要替换 {{char}} / {{user}}：
 *   角色卡正文里到处都是这两个宏，不替换就会把大括号原样显示给用户看。
 *
 * @param host  容器元素（会被清空重填）
 * @param html  原始文本（HTML 或纯文本）
 * @param opts  { char, user, view: 'html' | 'raw' }
 */
export function mountRich(host, html, opts = {}) {
  if (!host) return;
  const { char = '', user = '', view = 'html' } = opts;

  let text = stripFences(html);
  if (char) text = text.replace(/\{\{\s*char\s*\}\}/gi, char);
  if (user) text = text.replace(/\{\{\s*user\s*\}\}/gi, user);

  host.textContent = '';
  const isHtml = looksLikeHtml(text);

  const raw = document.createElement('pre');
  raw.className = 'prompt-view hidden';
  raw.textContent = text;

  const pane = document.createElement('div');
  pane.className = 'rich-pane';

  // 纯文本：没有"渲染"这回事，直接排版显示，不给用户假的切换按钮
  if (!isHtml) {
    pane.classList.remove('rich-pane');
    pane.style.whiteSpace = 'pre-wrap';
    pane.textContent = text;
    host.appendChild(pane);
    return;
  }

  const bar = document.createElement('div');
  bar.className = 'segmented mb8';
  const tabHtml = document.createElement('button');
  const tabRaw = document.createElement('button');
  tabHtml.textContent = '渲染视图';
  tabRaw.textContent = '源码';
  bar.append(tabHtml, tabRaw);

  const paintFrame = () => {
    const frame = document.createElement('iframe');
    frame.className = 'rich-frame';
    // ★ 故意不写 allow-scripts：脚本被浏览器直接禁掉，比任何清洗都可靠
    frame.setAttribute('sandbox', 'allow-same-origin');
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.setAttribute('title', '角色卡 HTML 渲染');
    frame.srcdoc = richDocHead() + sanitizeHtml(text) + '</body></html>';
    frame.addEventListener('load', () => {
      // 挂上"图片加载失败"的兜底：外链图床被墙时，用户看到的不该是破图小图标
      patchBrokenImages(frame);
      // 图片是异步加载的，高度会变 —— 分几次量，别只量第一次
      [0, 120, 400, 1000].forEach((delay) => setTimeout(() => fitFrame(frame), delay));
    });
    pane.textContent = '';
    pane.appendChild(frame);
  };

  const select = (want) => {
    const rich = want === 'html';
    tabHtml.classList.toggle('active', rich);
    tabRaw.classList.toggle('active', !rich);
    pane.classList.toggle('hidden', !rich);
    raw.classList.toggle('hidden', rich);
    if (rich && !pane.querySelector('iframe')) paintFrame();
  };
  tabHtml.addEventListener('click', () => select('html'));
  tabRaw.addEventListener('click', () => select('raw'));

  host.append(bar, pane, raw);
  select(view === 'raw' ? 'raw' : 'html');
}

/**
 * iframe 包裹文档：自带一份"只影响卡片自己"的基础排版。
 *
 * ★ 为什么现在是**函数**而不是常量：以前这里写死了 `background:#fff;color:#1f2328`，
 *   于是深色主题下"角色卡的 HTML 开场白"永远是刺眼的一整块白 —— iframe 是一份
 *   独立文档，父页面的 CSS 变量**不会**自动继承进去，写死就等于把主题锁死在浅色。
 *   现在每次渲染都从父页面把当前主题变量取出来注入，浅色/深色/插件换肤都能跟上。
 */
function richDocHead() {
  const fallback = {
    '--surface-2': '#fafbfc',
    '--surface-3': '#fbfcfd',
    '--text': '#1f2937',
    '--text-dim': '#6b7280',
    '--text-faint': '#9aa5b1',
    '--border': '#d8dbe0',
  };
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => (cs.getPropertyValue(name) || '').trim() || fallback[name];
  return (
    '<!DOCTYPE html><html><head><meta charset="utf-8">' +
    '<style>' +
    'html,body{margin:0;padding:0}' +
    `body{padding:10px 12px;background:${v('--surface-2')};color:${v('--text')};` +
    'font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;' +
    'font-size:14px;line-height:1.75;word-break:break-word;overflow-wrap:anywhere}' +
    'img{max-width:100%;height:auto}' +
    // 外链图片挂掉时的占位（由父页面 patchBrokenImages 换上）
    `.hne-img-broken{display:inline-block;padding:1px 8px;border:1px dashed ${v('--border')};` +
    `border-radius:6px;background:${v('--surface-3')};color:${v('--text-dim')};font-size:12px;` +
    'vertical-align:middle;word-break:break-all}' +
    'table{border-collapse:collapse;max-width:100%}' +
    `td,th{border:1px solid ${v('--border')};padding:3px 6px}` +
    `hr{border:none;border-top:1px solid ${v('--border')};margin:8px 0}` +
    'h1,h2,h3{font-size:16px}' +
    '</style></head><body>'
  );
}

/** 让 iframe 高度贴合内容（超过 460px 就内部滚动，避免弹窗被撑爆） */
function fitFrame(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc) return;
    const height = Math.max(
      doc.documentElement?.scrollHeight || 0,
      doc.body?.scrollHeight || 0,
    );
    if (!height) return;
    frame.style.height = `${Math.min(height + 2, 460)}px`;
  } catch {
    /* 读不到就退回默认高度，不影响正文 */
  }
}

/**
 * 把**加载失败的外链图片**换成看得懂的占位提示，而不是浏览器那个"破图"小图标。
 *
 * ★ 为什么由父页面代劳：iframe 没有 allow-scripts，卡片里的脚本一行都跑不了，
 *   所以"图挂了怎么办"只能父页面做 —— 沙箱给了 allow-same-origin，
 *   `contentDocument` 可读可改（`fitFrame` 用的也是这条路）。
 * ★ 只认 `complete && naturalWidth === 0`：卡片的图带 `loading="lazy"`，
 *   还没开始加载的图 `complete` 是 false，一起判会把好图也换掉。
 * ★ 真实反馈：用户看到"渲染 HTML 时图片全是裂的"，根因是图床
 *   （files.catbox.moe）在本地网络连不通（连接被重置），**不是**清洗把 src 滤掉了。
 *   所以这里只改善提示、给出原始 URL，不假装图片能加载；图能不能显示取决于网络。
 */
function patchBrokenImages(frame) {
  let doc = null;
  try {
    doc = frame.contentDocument;
  } catch {
    return; // 读不到（跨域等）就当没这回事，不影响正文
  }
  if (!doc) return;

  const patch = (img) => {
    if (!img.isConnected || img.naturalWidth > 0) return;
    const src = img.getAttribute('src') || '';
    let host = '';
    try {
      host = new URL(src, doc.baseURI).host;
    } catch {
      host = '';
    }
    const alt = (img.getAttribute('alt') || '').trim();
    const tip = doc.createElement('span');
    tip.className = 'hne-img-broken';
    tip.title = src ? `图片加载失败：${src}` : '图片加载失败';
    tip.textContent = `🖼 ${alt ? `${alt} ` : ''}图片加载失败${host ? `（${host} 打不开）` : ''}`;
    img.replaceWith(tip);
    fitFrame(frame); // 破图换成文字后高度会变，重新量一次
  };

  doc.querySelectorAll('img').forEach((img) => {
    // 已经失败的（命中缓存、早于监听器）当场换；还在加载的等它自己的 error 事件
    img.addEventListener('error', () => patch(img));
    if (img.complete) patch(img);
  });
}

/* ---------------- 文件拖放 ---------------- */

/**
 * 给一个"拖放区"接上文件读取：拖进来 → 读出文本 → 回调。
 *
 * ★ 为什么做成公共函数：
 *   用户明确说过"不支持拖动文件，用户肯定不喜欢自己复制粘贴到指定地方"。
 *   项目里有好几处要读文件（角色卡 JSON、提示词预设 JSON），
 *   各写一遍必然会出现"有的地方支持拖、有的地方只能粘贴"这种不一致。
 *
 * @param zone      拖放区元素（点击也会触发选择文件）
 * @param accept    给 <input type=file> 的 accept 值，例如 '.json,application/json'
 * @param onText    (text, file) => void，读出为 UTF-8 文本后的回调
 */
export function bindFileDrop(zone, { accept = '', onText } = {}) {
  if (!zone) return null;

  const input = document.createElement('input');
  input.type = 'file';
  input.accept = accept;
  input.className = 'hidden';
  zone.appendChild(input);

  const idle = () => {
    zone.style.borderColor = 'var(--border-strong)';
    zone.style.background = 'var(--surface-2)';
  };
  const hot = () => {
    zone.style.borderColor = 'var(--brand)';
    zone.style.background = 'var(--brand-soft)';
  };

  const read = (file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => onText?.(String(reader.result || ''), file);
    reader.onerror = () => toastErr(`读不出文件内容：${file.name}`);
    reader.readAsText(file, 'utf-8');
  };

  zone.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    read(input.files?.[0]);
    input.value = ''; // 让"再拖同一个文件"也能触发 change
  });
  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    hot();
  });
  zone.addEventListener('dragleave', idle);
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    idle();
    read(e.dataTransfer?.files?.[0]);
  });

  return input;
}

/* ---------------- 其它 ---------------- */
export function spinner(text = '处理中') {
  return `<span class="spin"></span> ${esc(text)}`;
}

/**
 * 让按钮进入"加载中"状态，返回一个恢复函数。
 *
 * ★ 两个必须防住的坑（都真实踩到过）：
 *
 *   1. **不传 text 时保留原文字。**
 *      最初调用方传的是空字符串，结果按钮里只剩一个转圈图标、文字全没了，
 *      看起来像是"按钮坏了"。
 *
 *   2. **重复调用要能正确恢复。**
 *      如果同一个按钮被连续调用两次（比如页面上残留了旧的监听器），
 *      第二次会把**第一次的 spinner** 当成"原始内容"存下来，
 *      恢复时自然恢复成一片空白，而且 disabled 状态也对不上。
 *      所以原始内容记在元素自己身上（dataset），第二次调用直接复用。
 */
export function buttonLoading(btn, text) {
  // 只在第一次调用时记录原始内容
  if (btn.dataset.originalHtml === undefined) {
    btn.dataset.originalHtml = btn.innerHTML;
  }
  // 不传 text 就用按钮本来的文字（用 textContent 避免把 HTML 当文本转义）
  const label = text !== undefined ? text : btn.textContent.trim();

  btn.disabled = true;
  btn.innerHTML = spinner(label);

  return () => {
    const original = btn.dataset.originalHtml;
    // ★ 已经被别的调用恢复过了就什么都不做。
    //   少了这一行会出大问题：第二个恢复函数读到的是 undefined，
    //   而 `btn.innerHTML = undefined` 会被浏览器转成字面量字符串 "undefined"，
    //   按钮上就真的显示出了 "undefined" 七个字母（实测见过）。
    if (original === undefined) return;

    btn.disabled = false;
    btn.innerHTML = original;
    delete btn.dataset.originalHtml;
  };
}

/**
 * 画一段"出错了"的提示，并给一个**重试按钮**。
 *
 * ★ 为什么必须有它：列表页的加载路径是「先画『加载中…』→ 拉数据 → 再画内容」。
 *   只要中间任何一步抛异常、或者请求一直不回来，页面就会**永远停在『加载中…』**——
 *   用户看到的是一个转不完的圈，既不知道出了什么事，也没有任何出口
 *   （真实反馈：世界书页点开编辑再关掉后整页卡在"加载中"）。
 *   约定：所有"整页加载"的地方，失败时必须换成这个，而不是留着加载中不管。
 */
export function mountError(container, message, onRetry) {
  mount(
    container,
    `<div class="alert danger" style="white-space:pre-wrap">${esc(message || '加载失败')}</div>
     <div class="center mt12"><button class="btn sec" data-retry>重试</button></div>`,
  );
  $('[data-retry]', container)?.addEventListener('click', () => onRetry?.());
}

export function emptyState(icon, title, desc = '') {
  return `
    <div class="empty">
      <div class="big">${icon}</div>
      <div><strong>${esc(title)}</strong></div>
      ${desc ? `<div class="small mt8">${esc(desc)}</div>` : ''}
    </div>`;
}

/** 判断字符串里是否是"看起来像 JSON"的内容 */
export function safeJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
