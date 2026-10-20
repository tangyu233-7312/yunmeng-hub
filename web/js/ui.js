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
  // ★ 某些确认框的内容较长（例如"换存储路径会看到空库"要写清后果），
  //   窄框里会挤成一团。默认仍是 narrow，与既有调用方行为一致。
  width = 'narrow',
} = {}) {
  return new Promise((resolve) => {
    const m = modal({
      title,
      width,
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
export function sanitizeHtml(html, opts = {}) {
  const { proseLineBreaks = false } = opts;
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
  // ★ 顺序不能反：`clean` 是**重建 DOM**（文本节点都重新 append 过），
  //   所以必须等它跑完再动文本节点；反过来做会被它当纯文本转义掉。
  if (proseLineBreaks) proseNewlinesToBreaks(box);
  return box.innerHTML;
}

/* ---------------- HTML 卡片：真正把它渲染出来 ---------------- */

/**
 * 把「有实际内容的文本节点」里的换行变成 `<br>`。
 *
 * ==================== 为什么必须做（真实故障）====================
 * 酒馆正则"美化"出来的 HTML 长这样（实测那条 1669 字符的消息）：
 *
 *     <div class="dream-scene-bar">…状态栏…</div>
 *     你弯下腰，钻进左边那条矮巷道。
 *
 *     第一下没算准高度。后脑勺蹭过岩壁下沿…
 *
 *     <details><summary>平行事件</summary>…</details>
 *
 * 也就是说：**正文段落是裸文本，靠 `\n\n` 分段**，没有 `<p>`/`<br>` 包着。
 * 聊天区的 `.msg-body` 是 `white-space: pre-wrap`，所以那样显示是正常的；
 * 但 `mountRich` 把内容放进 **iframe**（独立文档，默认 `white-space: normal`），
 * HTML 会把连续换行**折叠成一个空格** —— 于是整段正文挤成一坨，
 * 用户的原话是"文字排版变得十分难看，没有分段"。
 *
 * ==================== 为什么不是简单地给 body 加 pre-wrap ====================
 * 那些美化 HTML 里有大量**用于缩进的换行**（标签之间、`<style>` 内部），
 * 实测 63 处。给整篇加 `pre-wrap` 会把它们全部显形，排版反而更乱
 * （三种做法都截图比对过：只转"有内容的文本节点"这一种效果正确）。
 *
 * 所以判据是两条**同时**成立：
 *   · 这个文本节点**去掉空白后还有内容**（纯缩进节点跳过 → 不会被显形）；
 *   · 它里面**确实含换行**（没有就不用动）。
 * 另外 `<style>`/`<script>`/`<textarea>`/`<pre>` 里的换行是**语义的一部分**，
 * 一概不能碰（往 `<style>` 里塞 `<br>` 会直接破坏样式表）。
 *
 * ★ 只在"渲染视图"里做：用户切到「源码」页签看到的仍是原样文本，
 *   所以这不是"改数据"，只是"怎么显示"。
 *
 * @param {Document|Element} root 已清洗过的 DOM（会被**就地**修改）
 */
function proseNewlinesToBreaks(root) {
  if (!root) return;
  const SKIP = new Set(['style', 'script', 'textarea', 'pre', 'code']);
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  /** @type {Text[]} */
  const targets = [];
  let node = walker.nextNode();
  while (node) {
    const parent = node.parentElement;
    const value = node.nodeValue || '';
    if (
      parent &&
      !SKIP.has(parent.tagName.toLowerCase()) &&
      value.includes('\n') &&
      value.trim() // ★ 纯缩进/空白的节点跳过，否则会把缩进也顶出来
    ) {
      targets.push(node);
    }
    node = walker.nextNode();
  }
  for (const text of targets) {
    const frag = root.ownerDocument.createDocumentFragment();
    const lines = (text.nodeValue || '').split('\n');
    lines.forEach((line, index) => {
      if (index > 0) frag.appendChild(root.ownerDocument.createElement('br'));
      frag.appendChild(root.ownerDocument.createTextNode(line));
    });
    text.parentNode?.replaceChild(frag, text);
  }
}

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

/* ---------------- 分段：把一条消息切成「正文」与「卡片」 ---------------- */

/**
 * 这条消息里"能不能被当成卡片"的标签。
 *
 * ★ 为什么和 `_HTML_HINTS` 分开：那个是**粗检测**（整段文本里有没有标签），
 *   用来回答"要不要渲染"；这个用来回答"**哪一段**该渲染"。
 *   判据也更严：只认块级容器，不认 `<b>`/`<br>` 这类行内标签 ——
 *   否则一句 `这是<b>重点</b>` 会被切成正文+卡片+正文，碎得没法看。
 */
const _BLOCK_TAGS = new Set([
  'div', 'p', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'caption',
  'section', 'article', 'header', 'footer', 'blockquote', 'pre', 'ul', 'ol', 'dl',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'figure', 'figcaption', 'details',
  'center', 'style', 'img',
]);

/** 这些标签出现在**行内**时不算分段边界（正文里夹一个 <b> 不该被切开） */
const _INLINE_TAGS = new Set([
  'b', 'strong', 'i', 'em', 'u', 's', 'small', 'span', 'code', 'font', 'br', 'a',
]);

/**
 * 判断一段**纯文本**是不是"围栏卡片"（酒馆助手的 ```` ``` ```` + `<body>` 约定）。
 *
 * ==================== 为什么要有这条规则 ====================
 * 中文区做"前端卡"的作者，事实标准是把整页 HTML 放进代码围栏里：
 *
 *     ```html
 *     <html><body>…</body></html>
 *     ```
 *
 * 酒馆助手（JS-Slash-Runner）的判据是**两条同时成立**：在代码块里、且有
 * `<body>` 标签。我们照抄这个口径 —— 好处是作者按同一套习惯写，两边都认。
 *
 * ★ 为什么还要认"没有 `<body>`、但整段就是一个 `<html>`/`<style>`+结构"的情况：
 *   作者手写时经常省掉 body。这种整段被围栏包住、且里面是块级 HTML 的，
 *   其实就是一张卡片，不认会让用户看到一坨源码 + 两个孤零零的 ```。
 *   （与 `stripFences` 的老行为一致，那个函数当初就是为这种卡写的。）
 *
 * @returns {{html: string} | null} 认出来就返回围栏里的 HTML，否则 null
 */
function fencedCardBody(text) {
  const s = String(text ?? '').trim();
  const m = s.match(/^```[a-zA-Z0-9_-]*[ \t]*\r?\n([\s\S]*?)\r?\n?```$/);
  if (!m) return null;
  return fencedInnerIsCard(m[1]) ? m[1] : null;
}

/** 围栏**里面**的内容算不算一张卡片（判据口径见 `fencedCardBody` 的注释） */
function fencedInnerIsCard(inner) {
  // 强判据：有 <body> 就是卡片（与酒馆助手完全一致）
  if (/<body[\s>]/i.test(inner)) return true;
  // 弱判据：整段是一个 html 文档、或有 <style>、或首尾都是块级标签
  const trimmed = inner.trim();
  if (!trimmed) return false;
  if (/^<!doctype\s+html/i.test(trimmed) || /^<html[\s>]/i.test(trimmed)) return true;
  if (/<style[\s>]/i.test(inner)) return true;
  const firstTag = trimmed.match(/^<\s*([a-zA-Z][\w-]*)/)?.[1]?.toLowerCase();
  const lastTag = trimmed.match(/<\s*\/\s*([a-zA-Z][\w-]*)\s*>\s*$/)?.[1]?.toLowerCase();
  // ★ 必须是块级容器：`<span>` 之类的行内标签包住了也不算卡片
  return Boolean(firstTag && lastTag && firstTag === lastTag && _BLOCK_TAGS.has(firstTag));
}

/**
 * 逐块扫出"围栏里是卡片"的代码块（支持一条消息里混排多张）。
 *
 * ★ 为什么不能只在"整条就是一个围栏"时才认：
 *   作者实际写法经常是「正文 + 围栏卡片 + 正文」。按整条判会漏掉，
 *   用户看到的是一大段带 ``` 的源码 —— 这正是这条规则要消灭的症状。
 * ★ 不是卡片的围栏（```js 代码块、普通引用）**原样留在正文里**，
 *   否则会把"模型给的代码片段"也当成卡片渲染掉。
 */
function splitFencedBlocks(src) {
  const out = [];
  let last = 0;
  let found = false;
  _FENCED_BLOCK.lastIndex = 0;
  let m;
  while ((m = _FENCED_BLOCK.exec(src)) !== null) {
    if (!fencedInnerIsCard(m[1])) continue;
    found = true;
    const before = src.slice(last, m.index);
    if (before) out.push({ kind: 'text', html: before });
    out.push({ kind: 'card', html: m[1] });
    last = m.index + m[0].length;
  }
  if (!found) return null;
  const tail = src.slice(last);
  if (tail) out.push({ kind: 'text', html: tail });
  return out;
}

/** 围栏块的正则（内容非贪婪；语言标记可省略） */
const _FENCED_BLOCK = /```[a-zA-Z0-9_-]*[ \t]*\r?\n([\s\S]*?)\r?\n?```/g;

/**
 * 把一条消息切成按**原始顺序**排列的若干段。
 *
 * ==================== 为什么要有这个函数（真实需求）====================
 * 模型的实际输出经常是**混排**的：
 *
 *     你推开门，风灌了进来。
 *
 *     <div class="status-bar">…状态栏…</div>
 *
 *     她抬起眼。
 *
 * 以前只有两条路：整条渲染成卡片（正文被塞进 iframe，排版全由卡片样式决定），
 * 或者整条按纯文本显示（状态栏只剩一堆标签源码）。用户要的是
 * **正文照原样排版，卡片在它出现的位置显示成卡片** —— 也就是分段渲染。
 *
 * ==================== 为什么按 DOM 切而不是按字符串切 ====================
 * 早期想法是用正则找 `<div …>…</div>`，但 HTML 嵌套很深、还有 `<details>`
 * 和 `<table>`，正则找闭合标签**必然出错**（而且错得很隐蔽：多切一段，
 * 剩下的文本会带着半个标签显示出来）。
 * 这里改成：把整段文本交给 `DOMParser` 解析（惰性文档，不执行、不加载），
 * 然后按**顶层节点**分组 —— 连续的非块级节点算"正文"，连续的块级节点算"卡片"。
 * 这样嵌套问题交给浏览器，我们只做分组。
 *
 * @param {string} text 消息正文（已去掉外层围栏时传剥过的内容）
 * @returns {Array<{kind:'text'|'card', html:string}>}
 *   调用方按顺序渲染即可；`kind:'card'` 的 `html` 是要交给 `mountRich` 的。
 *   没有卡片时**只返回一段 text**（调用方据此走原来的单段老路，行为不变）。
 */
export function splitRichSegments(text) {
  const src = String(text ?? '');
  if (!src.trim()) return [{ kind: 'text', html: src }];

  // ★ 围栏卡片（酒馆助手的 ```` ``` ```` + `<body>` 约定）先判：
  //   整条消息就是一个围栏包裹的页面时，它是**一张卡片**，
  //   不该让用户看到两个孤零零的 ``` 和一堆源码。剥出来的内容按卡片走，
  //   走的是同一条沙箱渲染路径（安全边界完全不变）。
  //   ★ 必须在 looksLikeHtml 之前判：围栏里的 `<body>` 不在 `_HTML_HINTS`
  //   的提示词表里（那张表是给"裸标签"用的），先走快路会漏判。
  const fenced = fencedCardBody(src);
  if (fenced !== null) {
    // 空卡片（围栏里什么都没有）当正文处理，别给用户一个空白框
    return fenced.trim() ? [{ kind: 'card', html: fenced }] : [{ kind: 'text', html: src }];
  }

  // ★ 围栏卡片混在正文里（作者常见写法：正文 + ```html 卡片 + 正文）：
  //   先把围栏块切出来，剩下的正文再按 DOM 细分（一段正文里可能还有裸 HTML 卡片）。
  const blocks = splitFencedBlocks(src);
  if (blocks) {
    const out = [];
    for (const seg of blocks) {
      if (seg.kind !== 'text') {
        out.push(seg);
      } else if (looksLikeHtml(seg.html)) {
        out.push(...splitHtmlSegments(seg.html));
      } else {
        out.push(seg);
      }
    }
    return finalizeSegments(out, src);
  }

  // 没有任何 HTML 迹象就不用解析了（绝大多数消息走这条快路）
  if (!looksLikeHtml(src)) return [{ kind: 'text', html: src }];
  return finalizeSegments(splitHtmlSegments(src), src);
}

/**
 * 按 DOM 顶层节点把**纯 HTML** 切成正文段与卡片段（`splitRichSegments` 的内部实现）。
 */
function splitHtmlSegments(src) {
  const doc = new DOMParser().parseFromString(src, 'text/html');
  // ★ 必须把 <head> 里的 <style> 一起算进来：
  //   DOMParser 会把 `<style>` 这种"head 才许有的元素"自动挪进 `<head>`，
  //   只遍历 body 的话，卡片作者写在最前面的样式表会**静默消失**
  //   （卡片渲染出来没样式，且没有任何报错 —— 正是最难排查的那类）。
  const headStyles = Array.from(doc.head.querySelectorAll('style')).map((n) => n.outerHTML);
  const nodes = Array.from(doc.body.childNodes).filter(
    // 注释、纯空白文本节点不参与分段（但它们要保留在相邻正文里）
    (n) => n.nodeType === Node.ELEMENT_NODE ||
      (n.nodeType === Node.TEXT_NODE && String(n.nodeValue || '').trim()),
  );
  // ★ 连 head 里的 <style> 都没有、body 也空 → 没什么可切的，退回老路
  if (!nodes.length && !headStyles.length) return [{ kind: 'text', html: src }];

  /** @type {Array<{kind:'text'|'card'|'style', html:string}>} */
  const segments = [];
  // 先把 head 里的 <style> 放进来（作者一般把样式写在结构前面，
  // 保持这个顺序，后面"并进相邻卡片"才能对上）。
  for (const css of headStyles) segments.push({ kind: 'style', html: css });
  /** 当前累积中的那一段（kind 相同就继续追加） */
  let current = null;
  const push = (kind, html) => {
    if (current && current.kind === kind) current.html += html;
    else {
      current = { kind, html };
      segments.push(current);
    }
  };

  for (const node of nodes) {
    if (node.nodeType === Node.TEXT_NODE) {
      push('text', node.nodeValue || '');
      continue;
    }
    const tag = node.tagName.toLowerCase();
    // ★ <style> 永远跟卡片走：单独一段 <style> 渲染出来是一片空白，
    //   而样式与它要作用的元素分进两个 iframe 就彻底失效了（等于没美化）。
    //   ★ 这里**必须单独成段**（不能 push 进当前正文段）：
    //     一旦并进"正文+style"的混合段，第二步就认不出它是纯 style 了，
    //     于是样式留在正文里、卡片在 iframe 里 —— 实测就是这个失效。
    if (tag === 'style') {
      segments.push({ kind: 'style', html: node.outerHTML });
      current = null; // 打断连续合并：style 是独立段
      continue;
    }
    // 块级 → 卡片；行内 / 未知 → 正文（未知标签保持原样，交给清洗去管）
    push(_BLOCK_TAGS.has(tag) ? 'card' : 'text', node.outerHTML);
  }

  // 第二步：把**独立成段的 `<style>`** 并进相邻的卡片段。
  for (let i = 0; i < segments.length; i += 1) {
    const seg = segments[i];
    if (seg.kind !== 'style') continue;
    // 优先并到**后面**的卡片（作者一般把 <style> 写在结构前面）
    const next = segments[i + 1];
    const prev = segments[i - 1];
    if (next && next.kind === 'card') {
      next.html = seg.html + next.html;
      seg.html = '';
    } else if (prev && prev.kind === 'card') {
      prev.html += seg.html;
      seg.html = '';
    } else {
      // 两边都没有卡片：这条 style 只能当正文（清洗时 <style> 会被保留，
      // 样式在聊天区里也能生效 —— 虽然它没东西可作用，至少不丢内容）
      seg.kind = 'text';
    }
  }

  // 丢掉空的正文段（比如标签之间的纯缩进）
  return segments.filter((s) => s.kind === 'card' || s.html.trim());
}

/** 收尾：合并相邻正文段、去掉空段；没切出东西时退回整条原文（老行为） */
function finalizeSegments(list, src) {
  const out = [];
  for (const seg of list) {
    if (seg.kind === 'text' && !seg.html.trim()) continue;
    const prev = out[out.length - 1];
    if (prev && prev.kind === 'text' && seg.kind === 'text') prev.html += seg.html;
    else out.push({ ...seg });
  }
  // ★ 一段都没切出来的话退回老路，让调用方保持原行为
  if (!out.length || (out.length === 1 && out[0].kind === 'text')) {
    return [{ kind: 'text', html: src }];
  }
  return out;
}

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
 * @param opts  { char, user, view: 'html' | 'raw', keepProseLineBreaks: boolean }
 *   `keepProseLineBreaks`：把"有内容的文本节点"里的换行转成 `<br>`。
 *   专给**酒馆正则美化**出来的内容用（它的正文是裸换行，见
 *   `proseNewlinesToBreaks` 的说明）；角色卡开场白默认**不开**，
 *   免得悄悄改掉它们已经稳定的排版。
 */
export function mountRich(host, html, opts = {}) {
  if (!host) return;
  const {
    char = '',
    user = '',
    view = 'html',
    keepProseLineBreaks = false,
    // ★ 已经剥过围栏的内容（分段渲染切出来的段）不要再剥一次：
    //   分段是按 DOM 顶层节点切的，正文段里如果恰好在首尾有 ``` 是真实内容。
    noStrip = false,
  } = opts;

  let text = noStrip ? String(html ?? '') : stripFences(html);
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
    frame.srcdoc =
      richDocHead() +
      sanitizeHtml(text, { proseLineBreaks: keepProseLineBreaks }) +
      '</body></html>';
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
 * 把一条**混排**消息按原始顺序渲染：正文照原样排版，卡片在自己出现的位置渲染。
 *
 * ==================== 与 mountRich 的分工 ====================
 * `mountRich` 管"一段内容怎么画"（沙箱、清洗、源码切换）；
 * 这个函数管"一条消息怎么排"（切段、按序摆开）。
 *
 * ★ 关键约束：`.msg-body` 里必须始终有一份能 `textContent` 读回原文的东西。
 *   聊天区有多处（复制、编辑、重新生成、切换译文）靠
 *   `body.textContent` 拿模型原话 —— 如果整条只剩 iframe，那些功能
 *   会拿到空字符串，**而且不报错**（静默失效，这是本项目最忌讳的一类 bug）。
 *   所以这里把**原始的完整文本**放在一个隐藏的 `<pre class="msg-raw">` 里，
 *   由调用方决定是挂在 .msg-body 下还是别处；正文段则正常渲染。
 *
 * ★ 为什么不用「渲染视图 / 源码」这对页签承载整条消息：
 *   混排消息里正文和卡片是**不同性质**的内容，把整条塞进一个 iframe 会
 *   让正文的排版完全由卡片的 CSS 决定（实测正文会被卡片样式带跑）。
 *   分段后正文用聊天区原本的 `pre-wrap` 排版，卡片才进 iframe，各自独立。
 *
 * @param host     容器（会被清空重填）
 * @param segments `splitRichSegments()` 的返回值
 * @param opts     { char, user, keepProseLineBreaks }
 * @returns {boolean} 是否**真的分段了**（只有一个正文段时返回 false，
 *   调用方据此走原来的 mountRich 老路，保持"整段卡片"的既有体验）
 */
export function mountSegmented(host, segments, opts = {}) {
  if (!host) return false;
  const list = Array.isArray(segments) ? segments : [];
  const cards = list.filter((s) => s.kind === 'card');
  const texts = list.filter((s) => s.kind === 'text' && s.html.trim());
  // 没有卡片、或只有卡片没有正文 → 不算混排，交回调用方按老路整体渲染。
  // （纯卡片的消息保持「一个卡片 + 渲染/源码切换」的老体验更好，
  //   而且能省一层 iframe。）
  if (!cards.length || !texts.length) return false;

  const { char = '', user = '', keepProseLineBreaks = true } = opts;
  host.textContent = '';
  // ★ 标在宿主上而不是靠 :has()：外层 .msg-body 是 pre-wrap（给整条纯文本用的），
  //   分段后必须取消，否则段与段之间的空白会被显形出来，多出一堆空行。
  host.classList.add('has-segs');
  const wrap = document.createElement('div');
  wrap.className = 'msg-segs';

  for (const seg of list) {
    // ★ 防御：`style` 段按设计在切分阶段就被并进卡片了，走不到这里。
    //   真漏过来时当正文画——总比整条不显示强（宁可样式失效，也不许吞内容）。
    if (seg.kind === 'style') {
      const styleEl = document.createElement('style');
      styleEl.textContent = seg.html.replace(/^<style[^>]*>|<\/style>$/gi, '');
      wrap.appendChild(styleEl);
      continue;
    }
    const box = document.createElement('div');
    if (seg.kind === 'card') {
      box.className = 'msg-seg msg-seg-card';
      wrap.appendChild(box);
      // ★ 每一处 mountRich 都开 keepProseLineBreaks：卡里的正文常是裸换行
      mountRich(box, seg.html, {
        char,
        user,
        noStrip: true,
        keepProseLineBreaks: true,
      });
    } else {
      if (!seg.html.trim()) continue;
      // 正文段：用聊天区原本的换行排版，**不进 iframe** —— 保持聊天区的字体、
      // 行距与主题一致，也让 .msg-body 里的文字能被复制选中。
      box.className = 'msg-seg msg-seg-text';
      let prose = seg.html;
      if (char) prose = prose.replace(/\{\{\s*char\s*\}\}/gi, char);
      if (user) prose = prose.replace(/\{\{\s*user\s*\}\}/gi, user);
      // 正文里可能夹着行内标签（<b>、<i>）：清洗后按 HTML 排版，
      // 裸换行也要转成 <br>（否则 div 会把它们折叠成空格）。
      box.innerHTML = sanitizeHtml(prose, { proseLineBreaks: keepProseLineBreaks });
      wrap.appendChild(box);
    }
  }
  host.appendChild(wrap);
  return true;
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
