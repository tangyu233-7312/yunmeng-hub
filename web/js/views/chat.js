/* ============================================================
   叙事会话视图（对话界面 + SSE 流式打字机）

   ==================== 为什么不用 EventSource？====================
   浏览器原生的 EventSource 只能发 GET、**且不能自定义请求头**，
   意味着 JWT 只能塞进 URL 的查询串里。那会把登录令牌写进服务器访问日志、
   浏览器历史、以及任何中间代理的日志里，是明确的安全隐患。

   所以这里用 fetch + ReadableStream 手动解析 SSE：
     · 可以照常带 Authorization: Bearer 头
     · 可以随时 abort（"停止生成"按钮）
     · 事件格式与后端 sse_event() 严格对应
       （event: <名字>\ndata: <JSON>\n\n）

   ==================== 监听器泄漏（必读）====================
   #view 是常驻元素，切页只替换内部内容。所有挂在 root 上的委托监听器
   必须带 { signal }，否则会泄漏到其它页面（详见 app.js 里的说明）。
   ============================================================ */

import { api, session as authSession } from 'hne/api';
import {
  $,
  $$,
  buttonLoading,
  confirmDialog,
  esc,
  fmtRelative,
  formValues,
  freshViewSignal,
  looksLikeHtml,
  modal,
  mount,
  mountRich,
  textField,
  toastErr,
  toastOk,
  toastWarn,
} from 'hne/ui';

const API_PREFIX = '/api/v1';

const state = {
  status: 'active',
  sessionId: null,
  detail: null,
  streaming: false,
  /** 最近一轮的模型用量（TokenUsage 形状），用于头部统计条的"本轮"一栏 */
  usage: null,
  /** 用户点了「稍后」之后，本次会话视图内不再弹总结横幅（刷新/切会话后重置） */
  summaryDismissed: false,
};

/** 当前导航批次的信号（切页时 abort） */
let activeSignal = null;
/** 正在进行的流式请求控制器，用于「停止生成」 */
let streamController = null;

/** 协议类型 → 人话。纯聊天头部要用它把"这套 API 到底怎么被调用的"说清楚。 */
const PROTOCOL_LABEL = {
  openai_compatible: 'OpenAI 兼容协议',
  anthropic: 'Anthropic Messages 协议',
};

/* ==================================================================
   入口
   ================================================================== */
export async function renderChat(root, ctx = {}) {
  // ★ 每次调用都换一个新的监听器批次（旧的会被 abort）：
  //   `#view` 是常驻元素，反复渲染本视图会让委托监听器叠加 ——
  //   表现就是"一次点击弹出 N 个窗"（真实事故，另外两个视图也有同样的坑）。
  const fresh = freshViewSignal('chat', ctx.signal);
  activeSignal = fresh.signal;
  const signal = activeSignal;

  mount(root, shell());
  bindStatic(root, signal);

  const known = await refreshSessions(root, signal);

  // 优先打开 URL 里指定的会话（从会话列表跳转过来时用），否则沿用上次的
  const wanted = Number(ctx.sessionId || state.sessionId || 0);
  if (wanted) {
    if (sessionIsGone(known, wanted)) {
      state.sessionId = null;
      state.detail = null;
      showMissingSession(root);
    } else {
      await openSession(root, wanted, signal);
    }
  }
}

/**
 * ★ "这个会话还活着吗" —— 用**已经取回来的列表**判断，而不是再发一次请求。
 *
 * 真实场景：会话在别处被删掉了（删角色卡时勾了「连同对话记录一起删除」，
 * 或在另一个标签页删的），而本页 `state.sessionId` 还指着它。
 * 以前"重进对话页 / 点刷新"都会**无条件**再请求一次那个已删的会话，
 * 浏览器控制台于是留下一条红色 404（探针每轮都报它）——
 * 用户看到的是"前端好像坏了"，而界面其实早就正确地走了"会话不存在"的分支。
 *
 * ★ 只有"列表确实能代表这个会话"时才敢下结论：列表是按当前标签页
 *   （活跃 / 已归档）过滤的。若已知这个会话属于**另一个**筛选
 *   （`state.detail.status` 与当前 tab 不一致），它本来就不该出现在列表里，
 *   这时一律返回 false，退回直接请求（老的 404 兜底仍在 `openSession` 里）。
 *   `known` 为 null（列表没取到 / 请求被 abort）时同样不下结论。
 */
function sessionIsGone(known, id) {
  if (!known || !id) return false;
  const listMatchesFilter =
    !state.detail || state.detail.id !== id || state.detail.status === state.status;
  return listMatchesFilter && !known.has(id);
}

function shell() {
  return `
  <div class="page-head">
    <div>
      <h1>叙事会话</h1>
      <div class="sub">
        选一张角色卡开始一段故事。助手回复是<strong>流式</strong>生成的 ——
        文字会逐字出现，而不是等全部生成完再一次性显示。
      </div>
    </div>
    <div class="page-actions">
      <!-- ★ 纯聊天（无角色）：不掺人设、不注入预设与状态协议，走「通用助手」提示词。
           它的第二个用途是当**异构适配层的验收台** —— 换 API / 协议 / 流式开关后
           发一句就能看出这套配置到底通不通、参数有没有生效。 -->
      <button class="btn sec" id="btn-pure-chat" title="不选角色卡，直接和模型聊天（也用来验证 API 是否正常）">+ 纯聊天</button>
      <button class="btn" id="btn-new-session">+ 新建会话</button>
    </div>
  </div>

  <div class="chat-layout" id="chat-layout">
    <aside class="chat-side panel">
      <div class="segmented" id="chat-status">
        <button data-status="active" class="active">进行中</button>
        <button data-status="archived">已归档</button>
      </div>
      <div id="session-list" class="session-list">
        <div class="empty" style="padding:20px"><div class="big">⏳</div><div>加载中…</div></div>
      </div>
    </aside>

    <section class="chat-main panel" id="chat-main">
      <div class="empty" style="padding:60px">
        <div class="big">💬</div>
        <div><strong>还没有打开任何会话</strong></div>
        <div class="small mt8">从左边选一个，或者点右上角「+ 新建会话」。</div>
      </div>
    </section>

    <!--
      ★ 拖动调节大小（用户明确要求："考虑放大或者像窗口一样让用户自己决定大小"）。
        用窗口右下角那种"两个方向一起拖"的手柄：
          左右拖 → 改左侧会话列表宽度
          上下拖 → 改整个对话区高度
        尺寸存在 localStorage 里，下次进来还是你调好的样子。
        纯 CSS 的 resize 做不到"同时改宽和高"，所以这里用 JS 自己实现。
    -->
    <div class="chat-resize" id="chat-resize" role="separator"
         title="拖动调整大小：左右改会话列表宽度，上下改对话区高度"
         aria-label="拖动调整对话区大小"></div>
  </div>`;
}

/* ==================================================================
   静态事件绑定（只要带 { signal }，切页时自动摘掉）
   ================================================================== */
function bindStatic(root, signal) {
  $('#btn-new-session', root).addEventListener('click', () => openCreateDialog(root, signal));
  $('#btn-pure-chat', root).addEventListener('click', () => openPureChatDialog(root, signal));

  /* ---------------- 拖动调节对话区大小 ---------------- */
  initChatResize(root, signal);

  $('#chat-status', root).addEventListener(
    'click',
    (e) => {
      const btn = e.target.closest('button[data-status]');
      if (!btn) return;
      state.status = btn.dataset.status;
      $$('#chat-status button', root).forEach((b) =>
        b.classList.toggle('active', b.dataset.status === state.status),
      );
      refreshSessions(root, signal);
    },
    { signal },
  );

  // ---------------- 会话列表的委托监听器 ----------------
  $('#session-list', root).addEventListener(
    'click',
    async (e) => {
      const item = e.target.closest('.session-item');
      if (!item) return;
      const id = Number(item.dataset.id);
      if (!id) return;

      if (e.target.closest('[data-act="rename"]')) return renameSession(root, id, signal);
      if (e.target.closest('[data-act="archive"]')) return toggleArchive(root, id, signal);
      if (e.target.closest('[data-act="delete"]')) return deleteSession(root, id, signal);

      await openSession(root, id, signal);
    },
    { signal },
  );

  // ---------------- 消息区的委托监听器 ----------------
  $('#chat-main', root).addEventListener(
    'click',
    async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const act = btn.dataset.act;

      if (act === 'send') return sendMessage(root, signal);
      if (act === 'stop') return stopStreaming();
      if (act === 'refresh') {
        // ★ 刷新按钮面对的是"内存里记着的会话 id"，它可能已经被删掉了。
        //   先用列表确认再请求 —— 否则会白打一次注定 404 的请求（控制台留红字）。
        const known = await refreshSessions(root, signal);
        if (sessionIsGone(known, state.sessionId)) {
          state.sessionId = null;
          state.detail = null;
          showMissingSession(root);
          return;
        }
        return openSession(root, state.sessionId, signal);
      }
      // ★ 记忆总结横幅（用户要求：到点只提醒，总结与否由他自己决定、不强制花 token）
      if (act === 'summary-run') return runMemorySummary(root);
      if (act === 'summary-later') {
        state.summaryDismissed = true;
        const bar = $('#summary-banner', root);
        if (bar) bar.remove();
        return;
      }
      if (act === 'summary-mute') return muteMemoryReminder(root);
      if (act === 'prompt') return showPromptDialog();
      if (act === 'memories') return openMemoryDialog(root, signal);
      if (act === 'translate') return openTranslateDialog(root, signal);
      if (act === 'toggle-translation') return toggleTranslation(btn);
      if (act === 'translate-msg') return translateOneMessage(root, btn);
      // ★ VN 舞台开关：只改"看的方式"，不落库（所以是本地开关，不发请求）。
      //   切换后重画面板即可 —— 舞台数据（该显示哪张立绘）是后端算好的。
      if (act === 'vn-stage') {
        const detail = state.detail;
        if (!detail?.vn) return;
        setVnStage(detail, !vnStageOn(detail));
        renderChatPanel(root, detail);
        return;
      }
      if (act === 'settings') return openSettingsDialog(root, signal);
      if (act === 'regen') return regenerateMessage(root, signal, msgOf(btn));
      if (act === 'edit-state') return openStateEditor(root);
      if (act === 'toggle-state') {
        // ★ 纯 DOM 展开/收起：**绝不重渲染**（重渲染会 abort 当前 view signal，
        //   把用户正在看的东西打断 —— 这是本项目的已知坑，见 docs/handoff-quick.md §7）
        const bar = root.querySelector('#state-bar');
        const detail = bar?.querySelector('.state-bar-detail');
        if (detail) {
          detail.classList.toggle('hidden');
          bar.classList.toggle('expanded');
        }
        return;
      }
      if (act === 'copy-msg') return copyMessage(btn);
      if (act === 'edit-msg') return openEditDialog(root, signal, msgOf(btn));
      if (act === 'retract-msg') return retractMessage(root, signal, msgOf(btn));
      if (act === 'scroll-bottom') {
        const box = $('#messages', root);
        if (box) box.scrollTop = box.scrollHeight;
      }
    },
    { signal },
  );

  // 输入框：Enter 发送，Shift+Enter 换行（与聊天软件的习惯一致）
  $('#chat-main', root).addEventListener(
    'keydown',
    (e) => {
      if (e.target.id !== 'composer-input') return;
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage(root, signal);
      }
    },
    { signal },
  );

  // 输入时让发送按钮跟着启用/禁用，避免用户点了没反应还以为坏了
  $('#chat-main', root).addEventListener(
    'input',
    (e) => {
      if (e.target.id !== 'composer-input') return;
      const btn = $('#btn-send', root);
      if (btn) btn.disabled = !e.target.value.trim() || state.streaming;
    },
    { signal },
  );

  // 用户往上翻看历史时，给一个「回到最新」的浮动按钮：
  // 长会话里流式输出会把新内容顶到下面，没有它就得手动滚很久
  $('#chat-main', root).addEventListener(
    'scroll',
    (e) => {
      if (e.target.id !== 'messages') return;
      const btn = $('.scroll-bottom', root);
      if (!btn) return;
      const box = e.target;
      const away = box.scrollHeight - box.scrollTop - box.clientHeight > 160;
      btn.classList.toggle('hidden', !away);
    },
    { capture: true, signal },
  );
}

/* ==================================================================
   ★ 拖动调节对话区大小
   ================================================================== */
const CHAT_SIZE_KEY = 'hne_chat_size';
//: 会话列表宽度的可调范围。下限保证会话标题还能看，上限避免把对话区挤没
const SIDE_W_MIN = 220;
const SIDE_W_MAX = 560;
//: 对话区高度的可调范围（相对"对话区顶部到窗口底部"的可用高度做留白）
const SIDE_H_MIN = 320;
const SIDE_H_GAP = 150;

/** 把两侧尺寸写到 CSS 变量上（布局全靠 CSS，JS 只负责给值） */
function applyChatSize(root, w, h) {
  const layout = $('#chat-layout', root);
  if (!layout) return;
  layout.style.setProperty('--chat-side-w', `${w}px`);
  if (h === null) {
    // 没调过高度 → 交回 CSS 的 calc(100vh - 220px)
    layout.style.removeProperty('--chat-h');
    delete layout.dataset.height;
  } else {
    layout.style.setProperty('--chat-h', String(Math.round(h)));
    layout.dataset.height = '1';
  }
}

function initChatResize(root, signal) {
  const layout = $('#chat-layout', root);
  const handle = $('#chat-resize', root);
  if (!layout || !handle) return;

  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  const maxHeight = () => Math.max(SIDE_H_MIN, window.innerHeight - SIDE_H_GAP);

  // 恢复上次调好的尺寸（存 localStorage，刷新后仍然是你调的样子）
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(CHAT_SIZE_KEY) || 'null');
  } catch {
    saved = null;
  }
  if (saved && Number.isFinite(saved.w)) {
    applyChatSize(root, clamp(saved.w, SIDE_W_MIN, SIDE_W_MAX), Number.isFinite(saved.h) ? saved.h : null);
  }

  const save = (w, h) => {
    try {
      localStorage.setItem(CHAT_SIZE_KEY, JSON.stringify({ w, h }));
    } catch {
      /* 隐私模式下写不进去，忽略即可，不影响本次拖动 */
    }
  };

  // 用 pointer 事件 + setPointerCapture：
  // 指针跑出那个小三角之后仍然能收到移动事件，不会"拖到一半断掉"
  handle.addEventListener(
    'pointerdown',
    (e) => {
      e.preventDefault();
      const startW = layout.querySelector('.chat-side')?.getBoundingClientRect().width ?? 300;
      const startH = layout.getBoundingClientRect().height;
      const startX = e.clientX;
      const startY = e.clientY;
      try {
        // 捕获指针：拖出那个小三角之后仍然能收到移动事件，不会"拖到一半断掉"。
        // 用 try 包起来是因为合成事件（自动化测试里）没有真实 pointerId，
        // 这里失败也不该让整个拖动逻辑崩掉 —— 退化成"必须一直按在手柄上"。
        handle.setPointerCapture(e.pointerId);
      } catch {
        /* 忽略：没有真实指针时不需要捕获 */
      }
      layout.dataset.resizing = '1';

      const onMove = (ev) => {
        const w = clamp(Math.round(startW + (ev.clientX - startX)), SIDE_W_MIN, SIDE_W_MAX);
        const h = clamp(Math.round(startH + (ev.clientY - startY)), SIDE_H_MIN, maxHeight());
        applyChatSize(root, w, h);
      };
      const onUp = () => {
        handle.removeEventListener('pointermove', onMove);
        handle.removeEventListener('pointerup', onUp);
        handle.removeEventListener('pointercancel', onUp);
        delete layout.dataset.resizing;
        const rect = layout.getBoundingClientRect();
        const w = layout.querySelector('.chat-side')?.getBoundingClientRect().width ?? 300;
        save(Math.round(w), Math.round(rect.height));
        // 高度变了会影响"消息区还能滚多少"，让视图里的滚动逻辑重新判断一次
        window.dispatchEvent(new Event('resize'));
      };
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onUp);
      handle.addEventListener('pointercancel', onUp);
    },
    { signal },
  );

  // 双击手柄 = 恢复默认大小（用户调歪了之后有个"一键回去"的出口）
  handle.addEventListener(
    'dblclick',
    () => {
      layout.style.removeProperty('--chat-side-w');
      layout.style.removeProperty('--chat-h');
      delete layout.dataset.height;
      try {
        localStorage.removeItem(CHAT_SIZE_KEY);
      } catch {
        /* 同上 */
      }
      window.dispatchEvent(new Event('resize'));
    },
    { signal },
  );

  // 窗口尺寸变化时，别让用户调过的高度超出新的可视范围
  window.addEventListener(
    'resize',
    () => {
      if (!layout.dataset.height) return;
      const side = layout.querySelector('.chat-side');
      const w = side ? side.getBoundingClientRect().width : SIDE_W_MIN;
      const h = clamp(Number(layout.style.getPropertyValue('--chat-h')) || 0, SIDE_H_MIN, maxHeight());
      applyChatSize(root, clamp(Math.round(w), SIDE_W_MIN, SIDE_W_MAX), h);
    },
    { signal },
  );
}

/* ==================================================================
   会话列表
   ================================================================== */
async function refreshSessions(root, signal) {
  const box = $('#session-list', root);
  if (!box) return null;

  let page;
  try {
    page = await api.get('/narrative/sessions', { status: state.status, limit: 50 });
  } catch (err) {
    box.innerHTML = `<div class="alert danger" style="margin:10px">${esc(err.toDisplay())}</div>`;
    return null;
  }
  if (signal?.aborted) return null;

  if (!page.items.length) {
    box.innerHTML = `<div class="empty" style="padding:26px">
      <div class="big">📭</div>
      <div class="small">${state.status === 'archived' ? '没有已归档的会话' : '还没有会话'}</div>
    </div>`;
    return new Set();
  }

  box.innerHTML = page.items
    .map(
      (s) => `
      <div class="session-item ${s.id === state.sessionId ? 'active' : ''}" data-id="${s.id}">
        <div class="si-top">
          <span class="si-avatar">${esc((s.character_card?.name || '?')[0] || '?')}</span>
          <span class="si-title" title="${esc(s.title)}">${esc(s.title)}</span>
        </div>
        <div class="si-preview">${
          s.last_message_preview
            ? `${s.last_message_role === 'user' ? '我：' : ''}${esc(s.last_message_preview)}`
            : '<span class="faint">（还没有消息）</span>'
        }</div>
        <div class="si-meta">
          <span>${s.message_count} 条</span>
          <span>·</span>
          <span>${esc(s.model_name || '未配置模型')}</span>
          <span class="spacer"></span>
          <span class="faint">${esc(fmtRelative(s.last_active_at || s.created_at))}</span>
        </div>
        ${s.warnings?.length ? `<div class="si-warn" title="${esc(s.warnings.join('；'))}">⚠ ${esc(s.warnings[0])}</div>` : ''}
        <div class="si-actions">
          <button class="btn link small" data-act="rename">改名</button>
          <button class="btn link small" data-act="archive">${s.status === 'archived' ? '恢复' : '归档'}</button>
          <button class="btn link small danger-link" data-act="delete">删除</button>
        </div>
      </div>`,
    )
    .join('');
  return new Set(page.items.map((s) => s.id));
}

/* ==================================================================
   打开一个会话
   ================================================================== */
async function openSession(root, id, signal) {
  if (!id) return;
  // 切会话时先停掉上一个会话正在进行的生成，避免回复串到别的会话里
  stopStreaming();

  state.sessionId = id;
  // 换会话时重置"稍后"状态：横幅该出现就出现（它是会话级设置决定的）
  state.summaryDismissed = false;
  const main = $('#chat-main', root);
  main.innerHTML = `<div class="empty" style="padding:60px"><div class="big">⏳</div><div>加载会话…</div></div>`;

  let detail;
  try {
    detail = await api.get(`/narrative/sessions/${id}`);
  } catch (err) {
    if (signal?.aborted) return;
    // ★ 会话已经不存在了（用户删了它，或删角色卡时勾了「连同对话记录」）。
    //   以前这里直接把 "会话不存在" 摊在右侧，用户既不知道是哪个会话，
    //   也不知道发生了什么 —— 而左侧列表其实是空的，两边信息互相矛盾。
    //   正确做法：**清掉记住的会话 id**，回到干净的未选中状态并说明原因。
    if (err.status === 404) {
      state.sessionId = null;
      state.detail = null;
      showMissingSession(root);
      refreshSessions(root, signal);
      return;
    }
    main.innerHTML = `<div class="alert danger" style="margin:16px">${esc(err.toDisplay())}</div>`;
    return;
  }
  if (signal?.aborted) return;

  state.detail = detail;
  // 换了会话，上一轮的用量就不该继续显示（否则会让人以为这个会话花过这些 token）
  state.usage = null;
  renderChatPanel(root, detail);
  hydrateRichMessages(root);
  $$('.session-item', root).forEach((el) =>
    el.classList.toggle('active', Number(el.dataset.id) === id),
  );
}

/**
 * 「这个会话已经不存在了」的空状态。
 *
 * ★ 为什么不做成一个错误提示条？
 *   会话不存在**不是错误**：删掉角色卡时勾上「连同对话记录」就会这样，
 *   而用户此刻已经在「角色卡」页做过一次确认了。这条路走回来再看到红字，
 *   只会让人以为系统坏了（这是用户实际反馈过的问题）。
 *   说清"发生了什么 + 现在能做什么"，比抛一个错误码有用得多。
 */
function showMissingSession(root) {
  const main = $('#chat-main', root);
  if (!main) return;
  mount(
    main,
    `<div class="empty" style="padding:60px">
      <div class="big">🗂</div>
      <div><strong>这个会话已经不在了</strong></div>
      <div class="small mt8">
        它可能被删除了 —— 删除角色卡时如果勾选了「连同对话记录一起删除」，
        用它开的会话也会一起消失。
      </div>
      <div class="small mt14">从左边选一个会话，或者点右上角「+ 新建会话」重新开始。</div>
    </div>`,
  );
}

function roleLabel(role) {
  return { user: '我', assistant: '角色', system: '系统' }[role] || role;
}

/**
 * 消息正文 + 悬停时出现的操作按钮。
 *
 * ★ 谁能用哪个按钮，是**刻意**的：
 *   · 复制      任何消息都能复制
 *   · 编辑      只有 user 消息能改（assistant 是模型产物，改它等于伪造历史）
 *   · 撤回      只有 user 消息能撤回 —— 会连带删掉它之后的全部内容
 *   · 重新生成  只有 assistant 消息能重新生成 —— 同样会删掉它之后的内容，
 *               但**不会**多出一条重复的用户发言（这是它存在的意义）
 * 把不可能的操作藏起来，比点下去报错体验好得多。
 */
function messageActions(m) {
  const buttons = [
    `<button class="btn link small" data-act="copy-msg">复制</button>`,
    // ★ 逐条翻译（第十六轮）：适用于"忘了开翻译中间件"或"只想看这一条"的用户。
    //   它与全局设置**绑定但不依赖开关** —— 点这一下就是同意花这一次调用。
    `<button class="btn link small" data-act="translate-msg" ` +
      `title="把这一条译成翻译面板里设定的目标语言（花一次模型调用；已经有译文的不重复翻译）">🌐 翻译</button>`,
  ];
  if (m.role === 'user') {
    buttons.push(`<button class="btn link small" data-act="edit-msg">编辑</button>`);
    buttons.push(
      `<button class="btn link small danger-link" data-act="retract-msg" ` +
        `title="删掉这句话以及它之后的全部内容">撤回</button>`,
    );
  } else if (m.role === 'assistant') {
    buttons.push(
      `<button class="btn link small" data-act="regen" ` +
        `title="用同一句话让模型重新写一遍（会删掉这条回复之后的内容，但不会多出发言）">重新生成</button>`,
    );
  }
  return `<div class="msg-actions">${buttons.join('')}</div>`;
}

/**
 * 把气泡里"看起来是 HTML 卡片"的消息真正渲染出来（开场白最常见）。
 *
 * ★ 为什么流式期间不渲染、等收尾才渲染：
 *   流式是往 `.msg-body` 里不断追加纯文本的；一旦插进 iframe，
 *   后续的 delta 就没地方写了。所以流式全程纯文本，收尾时才切换。
 * ★ 为什么要给用户留「源码」页签：
 *   HTML 卡片渲染失败时，用户得能看到原始内容，否则只会以为"系统卡了"。
 */
function hydrateRichMessages(root, onlyBubble) {
  const bubbles = onlyBubble ? [onlyBubble] : Array.from($$('.msg', root));
  const char = state.detail?.character_card?.name || '';
  const user = authSession.user?.username || '';
  for (const bubble of bubbles) {
    const body = bubble.querySelector('.msg-body');
    if (!body || body.dataset.richMounted) continue;
    const text = body.textContent || '';
    if (!looksLikeHtml(text)) continue;
    body.dataset.richMounted = '1';
    mountRich(body, text, { char, user });
  }
}

function renderMessages(detail) {
  if (!detail.messages.length) {
    return `<div class="empty" style="padding:40px">
      <div class="big">✨</div>
      <div><strong>故事还没开始</strong></div>
      <div class="small mt8">这张角色卡没有开场白，说句话让它开口吧。</div>
    </div>`;
  }

  const total = detail.messages.length;
  return (
    (detail.messages_truncated
      ? `<div class="alert info" style="margin:12px 16px">
           这里只显示了最近 ${total} / ${detail.messages_total} 条消息
           （更早的内容仍然保存在数据库里，界面上不再展开）。
         </div>`
      : '') +
    // ★ 气泡里必须有 .msg-reason：推理模型流式的思考内容往这个容器里写
    //   （appendReasoning）。以前这里漏了，而 appendMessage 造的新气泡有 ——
    //   于是"打开历史会话 → 点重新生成"时 appendReasoning 拿到 null 直接崩，
    //   用户看到的就是 Cannot read properties of null (reading 'classList')。
    detail.messages
      .map(
        (m, index) => `
      <div class="msg ${m.role}" data-id="${m.id}"${altTextAttrs(m)}>
        <div class="msg-role">${esc(roleLabel(m.role))}${
          m.model_name ? `<span class="faint"> · ${esc(m.model_name)}</span>` : ''
        }${m.token_count ? `<span class="faint msg-tokens" title="这条消息占用/消耗的 token"> · ${m.token_count} token</span>` : ''}${
          m.latency_ms ? `<span class="faint"> · ${(m.latency_ms / 1000).toFixed(1)}s</span>` : ''
        }
        </div>
        <div class="msg-reason hidden"></div>
        <div class="msg-body">${esc(displayText(m))}</div>
        ${translationChipHTML(m)}
        ${rollsHTML(m.rolls)}
        ${messageActions(m)}
      </div>`,
      )
      .join('')
  );
}

/* ==================================================================
   自动翻译中间件（原文 / 译文可切换）
   ★ 一条消息最多两份文本，**不变量**：
       `content`     = 模型看到的文本（输入侧是译文、输出侧是模型原文）
       `translation` = 另一份 + 元信息；其中 `translation.text` 就是**给人看的文本**
                       （输出侧=译文、输入侧=用户原话），`display` 把这条规则显式写出来
     两份都放进 DOM（`data-alt-text`），切换是**纯前端**的 —— 不发请求、不改数据。
   ================================================================== */
function displayText(m) {
  const t = m.translation;
  if (!t || !t.text) return m.content;
  return t.display === 'content' ? m.content : t.text;
}

function altText(m) {
  const t = m.translation;
  if (!t || !t.text) return '';
  return t.display === 'content' ? t.text : m.content;
}

function altTextAttrs(m) {
  const alt = altText(m);
  if (!alt) return '';
  // 富文本（HTML 卡片）消息不做切换：换 textContent 会把挂好的 iframe 一起毁掉
  if (looksLikeHtml(String(m.content || '')) || looksLikeHtml(alt)) return '';
  const showing = alt === m.content ? 'content' : 'translation';
  return ` data-alt-text="${esc(alt)}" data-showing="${showing}"`;
}

function translationChipHTML(m) {
  const t = m.translation;
  if (!t || !t.text) return '';
  const parts = [];
  if (t.direction === 'input') parts.push(`已译成${esc(t.lang || '')}再发给模型`);
  else parts.push(`${esc(t.lang || '')}译文`);
  if (t.used_model && t.tokens) parts.push(`${t.tokens} token`);
  if (!t.used_model) parts.push('未调用模型');
  if (t.error) parts.push(`失败：${esc(t.error)}`);
  const toggle = altText(m)
    ? `<button class="btn link small" data-act="toggle-translation">看看另一份</button>`
    : '';
  return `<div class="msg-trans">🌐 ${parts.join(' · ')}${toggle}</div>`;
}

/** 切换某条消息的原文/译文（纯前端：两份文本都在 DOM 里）。 */
function toggleTranslation(btn) {
  const bubble = btn.closest('.msg');
  const body = bubble?.querySelector('.msg-body');
  const alt = bubble?.dataset.altText;
  if (!body || !alt) return;
  const current = body.textContent;
  body.textContent = alt;
  bubble.dataset.altText = current;
  bubble.dataset.showing = bubble.dataset.showing === 'content' ? 'translation' : 'content';
  btn.textContent = bubble.dataset.showing === 'content' ? '看译文' : '看原文';
}

/**
 * 骰子气泡（骰子插件）。
 *
 * ★ 点数是**后端掷的**，这里只负责显示：`/r 1d100` 与模型写的 `<roll>` 都走这一份。
 *   界面上必须能一眼看出"这是系统掷的"（🎲），否则用户会以为模型在编数字 ——
 *   而"模型编不出真随机"正是这个插件存在的理由。
 * ★ 掷骰失败（表达式写坏了）也要显示出来，且要写清原因：
 *   静默丢掉会让用户以为插件坏了。
 */
function rollsHTML(rolls) {
  if (!Array.isArray(rolls) || !rolls.length) return '';
  const chips = rolls
    .map((r) => {
      const detail = String(r.text || '');
      if (r.error) {
        return `<span class="roll-chip bad" title="${esc(detail)}">🎲 ${esc(
          r.expression || '掷骰',
        )} · 失败</span>`;
      }
      const label = r.label ? `<span class="roll-label">${esc(r.label)}</span>` : '';
      const judge = r.compare
        ? `<span class="roll-judge ${r.success ? 'ok' : 'bad'}">${
            r.success ? '成功' : '失败'
          } ${esc(r.compare)}${esc(String(r.target))}</span>`
        : '';
      const cls = r.compare ? (r.success ? 'win' : 'lose') : '';
      return `<span class="roll-chip ${cls}" title="${esc(detail)}">🎲 ${label}${esc(
        r.expression,
      )} = <b>${esc(String(r.total))}</b>${judge}</span>`;
    })
    .join('');
  return `<div class="msg-rolls">${chips}</div>`;
}

/**
 * 「该总结了」横幅。
 *
 * ★ 为什么做成横幅而不是自动总结：用户的原话是"总结与否也是由用户自己决定，不强制
 *   （因为总结要消耗 API 的 Token，或者有些用户单纯不想总结）"。
 *   所以到点只提醒、并**如实写出这次大概要花多少 token**，点不点由用户决定；
 *   自动总结要在「记忆」面板里自己打开（`memory_summary.auto`）。
 */
function summaryBannerHTML(detail) {
  const info = detail?.memory_summary;
  if (!info || !info.enabled || !info.due || info.auto || state.summaryDismissed) return '';
  if (info.remind === false) return '';
  const cost = Number(info.cost_tokens || 0);
  const costText = cost > 0 ? `预计约 ${cost} token（用你的模型配置计费）` : '这个模式不调用模型，0 消耗';
  return `<div class="alert info" id="summary-banner" style="display:flex;align-items:center;gap:10px">
    <span style="flex:1">
      🧠 已经积累 <strong>${esc(String(info.pending ?? info.rounds))} 轮</strong>没总结了
      （第 ${esc(String(info.from_round))}~${esc(String(info.to_round))} 轮）。
      ${esc(costText)} —— 不总结也完全不影响继续聊。
    </span>
    <button class="btn sm" data-act="summary-run">立即总结</button>
    <button class="btn sec sm" data-act="summary-later">稍后</button>
    <button class="btn link small" data-act="summary-mute" title="这条会话不再弹总结提醒（可在「记忆」面板里改回来）">不再提醒</button>
  </div>`;
}

/** 点横幅上的「立即总结」：唯一会花 token 的手动入口。 */
async function runMemorySummary(root) {
  if (!state.sessionId) return;
  const btn = $('#summary-banner [data-act="summary-run"]', root);
  // ★ 反馈必须**在第一个 await 之前**就摆上（同步执行到 await 时按钮已经禁用/变字），
  //   否则用户会以为没点上、连点几下 —— 真实事故：他连点几次，AI 连着总结了好几遍、白花 token。
  const restore = btn ? buttonLoading(btn, '总结中…') : () => {};
  const banner = $('#summary-banner', root);
  if (banner) {
    const hint = document.createElement('span');
    hint.className = 'small muted';
    hint.id = 'summary-running';
    hint.textContent = '正在总结（要调用一次模型，请稍等）…';
    banner.appendChild(hint);
  }
  try {
    const res = await api.post(`/narrative/sessions/${state.sessionId}/memory-summary/run`, {});
    toastOk(res?.message ? res.message : '已总结');
    state.summaryDismissed = false;
    await openSession(root, state.sessionId, null);
  } catch (err) {
    // 409 = 上一次还在跑：如实说出来，而不是静默再跑一遍
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
    const running = $('#summary-running', root);
    if (running) running.remove();
    if (banner) {
      const note = document.createElement('span');
      note.className = 'small';
      note.id = 'summary-running';
      note.textContent = '这次没有重复总结（上一次还在进行中，或已经没有新内容可总结）。';
      banner.appendChild(note);
    }
  } finally {
    restore();
  }
}

/** 「不再提醒」：把这条会话的提醒开关关掉（只影响这条会话）。 */
async function muteMemoryReminder(root) {
  if (!state.sessionId) return;
  try {
    await api.patch(`/narrative/sessions/${state.sessionId}/memory-summary`, { remind: false });
    state.summaryDismissed = true;
    const bar = $('#summary-banner', root);
    if (bar) bar.remove();
    toastOk('这条会话不再提醒总结（可在「记忆」面板里改回来）');
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
  }
}

/**
 * 结构化状态栏：**字段由角色卡 / 世界书决定**（不再是写死的 HP/背包/位置/任务）。
 *
 * ★ 状态由后端解析模型每轮输出的 `<state>` 块后**落库在会话上**
 *   （见 app/narrative/state.py），字段清单由 `app/narrative/state_schema.py`
 *   从「卡 extensions.hne.state_schema > 世界书 [状态栏] 条目 > 卡 initial_state」
 *   解析一次并落库（detail.state_schema）。
 * ★ 前端只负责"按 schema 画" —— 千万别在这里"自己算一份"或"猜一套字段"，
 *   否则显示给用户的与模型看到的会不一致（老实现硬编码 HP，于是没声明 HP 的
 *   魔法少女卡也长出 `HP 100/100` 血条，这是用户指出的设计错误）。
 */
/** 最后一轮模型回复里输出的 `<state>` 原文（给状态条「看作者原格式」用；没有就是空串）。 */
function lastRawState(detail) {
  const rows = Array.isArray(detail?.messages) ? detail.messages : [];
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    if (rows[i]?.role === 'assistant') return rows[i].state_raw || '';
  }
  return '';
}

function stateBarHTML(st, schema, rawState) {
  const fields = Array.isArray(schema?.fields) ? schema.fields : [];
  const editBtn = (label, title) =>
    `<button class="btn link small" data-act="edit-state" title="${esc(title)}">${label}</button>`;
  const toggleBtn = `<button class="btn link small" data-act="toggle-state" title="展开 / 收起状态详情（默认收起，不占地方）">详情</button>`;

  // ★ 一行摘要：只放"值"，不放血条/背包那种占地方的渲染 —— 那些在详情里。
  //   用户反馈："状态栏占用的空间有点大，导致对话很小" ⇒ 默认只占一行。
  const compact = fields
    .map((f) => {
      const value = st ? st[f.name] : null;
      if (value === undefined || value === null || String(value) === '') return '';
      const label = esc(f.label || f.name);
      if (Array.isArray(value)) return `<span class="state-chip">${label} ${value.length}</span>`;
      if (typeof value === 'object') return `<span class="state-chip">${label} ${Object.keys(value).length}</span>`;
      return `<span class="state-chip">${label} ${esc(String(value))}</span>`;
    })
    .filter(Boolean)
    .join('');

  // ★ 「看作者原格式」：模型这一轮输出的 `<state>` 原文。
  //   为什么要留：状态被按 schema 解析成字段后，卡作者写的复杂/美化排版就没了，
  //   用户看复杂卡时仍然需要能看到"作者原本写的是什么样"。
  const raw = String(rawState || '').trim();
  const rawHTML = raw
    ? `<div class="state-raw"><div class="small muted">作者原格式（模型这一轮输出的
       <code>&lt;state&gt;</code> 原文，未解析）</div>
       <pre class="state-raw-pre">${esc(raw)}</pre></div>`
    : '';

  const wrap = (line, detail = '', empty = false) =>
    `<div class="state-bar${empty ? ' empty' : ''}" id="state-bar">` +
    `<div class="state-bar-line">${line}</div>` +
    (detail ? `<div class="state-bar-detail hidden">${detail}</div>` : '') +
    `</div>`;

  // ---------------- 这张卡没有定义状态栏 ----------------
  if (!fields.length) {
    // ★ 库里还有"迁移前建的老会话"（没有 schema 记录但有状态数据）：
    //   那时后端会退回旧五字段渲染，这里就不该说"未定义"（否则是界面骗人）。
    if (st && Object.keys(st).length) {
      const detail = stateBarFieldsHTML(st, schema);
      return wrap(
        `🧭 这条会话是旧版建的（当时状态栏字段是写死的），现在按旧格式显示。` +
          `${toggleBtn}${editBtn('编辑', '这条旧会话没有字段定义，直接改 JSON')}`,
        detail,
      );
    }
    const howto = `想让角色有状态栏，就在卡里写 <code>extensions.hne.state_schema</code>
      （或 <code>initial_state</code>），或者在它关联的世界书里加一条名字以
      <code>[状态栏]</code> 开头的条目、正文里放一段 <code>&lt;state&gt;{…}&lt;/state&gt;</code> 示例。`;
    return wrap(
      `🧭 这张角色卡没有定义状态栏格式，所以这里没有状态可显示。${toggleBtn}`,
      `<div class="small muted">${howto}</div>${rawHTML}${editBtn('手动填写', '模型没输出时，也可以自己先建一份')}`,
      true,
    );
  }

  // ---------------- 有字段定义、但模型还没输出过状态 ----------------
  if (!st || !Object.keys(st).length) {
    const names = fields.map((f) => f.label || f.name).join(' / ');
    return wrap(
      `🧭 还没有状态：模型每轮回复末尾输出 <code>&lt;state&gt;</code> 块后，` +
        `这里会显示 ${esc(names)}。${toggleBtn}` +
        editBtn('手动填写', '模型没输出时，也可以自己先建一份'),
      `<div class="small muted">字段由角色卡 / 世界书定义：${esc(names)}</div>${rawHTML}`,
      true,
    );
  }

  // ---------------- 正常：一行摘要 + 详情（详情里是原来那套完整渲染）----------------
  const line =
    `🧭 <span class="state-bar-chips">${compact}</span>` +
    `${toggleBtn}${editBtn('纠正', '模型写错了就自己改（校验规则与模型输出时同一套）')}`;
  return wrap(line, `${stateBarFieldsHTML(st, schema)}${rawHTML}`);
}

/**
 * 状态栏**详情**：按 schema 把每个字段画出来（血条 / 数字 / 背包 / 任务 / 布尔）。
 *
 * ★ 这一段是第十六轮从 `stateBarHTML` 原样搬过来的 —— 探针按
 *   `#state-bar .state-hp` 与文案断言，所以类名与文案一个都不能改。
 */
function stateBarFieldsHTML(st, schema) {
  const fields = Array.isArray(schema?.fields) ? schema.fields : [];
  const editBtn = (label, title) =>
    `<button class="btn link small" data-act="edit-state" title="${esc(title)}">${label}</button>`;

  // ---------------- 这张卡没有定义状态栏 ----------------
  if (!fields.length) {
    // ★ 兜底：正常情况下"没有字段定义"由 stateBarHTML 的一行摘要处理掉了；
    //   这里只在被直接调用时给一个不重复 id 的片段（绝不产生第二个 #state-bar）。
    if (st && Object.keys(st).length) {
      return `<div class="state-inner">🧭 这条会话是旧版建的（当时状态栏字段是写死的），现在按旧格式显示。
        重新新建一条会话就会按角色卡自己声明的字段显示。
        ${editBtn('编辑', '这条旧会话没有字段定义，直接改 JSON')}
      </div>`;
    }
    return `<div class="state-inner">🧭 这张角色卡没有定义状态栏格式，所以这里没有状态可显示。
      <br /><span class="small">想让角色有状态栏，就在卡里写
      <code>extensions.hne.state_schema</code>（或 <code>initial_state</code>），
      或者在它关联的世界书里加一条名字以 <code>[状态栏]</code> 开头的条目、
      正文里放一段 <code>&lt;state&gt;{…}&lt;/state&gt;</code> 示例。</span>
    </div>`;
  }

  // ---------------- 有字段定义、但模型还没输出过状态 ----------------
  if (!st || !Object.keys(st).length) {
    return `<div class="state-inner">
      🧭 还没有状态：模型每轮回复末尾输出 <code>&lt;state&gt;</code> 块后，
      这里会显示 ${esc(fields.map((f) => f.label || f.name).join(' / '))}。
      ${editBtn('手动填写', '模型没输出时，也可以自己先建一份')}
    </div>`;
  }

  const parts = [];
  for (const f of fields) {
    const name = f.name;
    const value = st[name];
    const icon = f.icon ? `${esc(f.icon)} ` : '';
    const label = esc(f.label || name);
    const type = f.type || 'text';
    if (type === 'meter') {
      const maxField = f.max_field || 'max';
      const cur = Number(value);
      const max = Number(st[maxField]);
      if (!Number.isFinite(cur) || !Number.isFinite(max) || max <= 0) continue;
      const pct = Math.max(0, Math.min(100, Math.round((cur / max) * 100)));
      const level = pct <= 25 ? 'low' : pct <= 60 ? 'mid' : 'high';
      parts.push(
        `<span class="state-item state-hp" title="${esc(
          f.description || `${f.label || name}（由模型每轮状态块更新，越界会被后端夹住）`,
        )}">` +
          `<b>${icon}${label}</b>` +
          `<span class="state-hp-track"><span class="state-hp-fill ${level}" style="width:${pct}%"></span></span>` +
          `<span class="state-hp-text">${cur} / ${max}${esc(f.unit || '')}</span></span>`,
      );
    } else if (type === 'number') {
      if (value === undefined || value === null || value === '') continue;
      parts.push(
        `<span class="state-item" title="${esc(f.description || '')}">${icon}${label} <b>${esc(String(value))}</b>${esc(f.unit || '')}</span>`,
      );
    } else if (type === 'list') {
      if (!Array.isArray(value) || !value.length) continue;
      parts.push(
        `<span class="state-item" title="${esc(f.description || '')}">${icon}${label} ` +
          value.map((item) => `<span class="state-chip">${esc(String(item))}</span>`).join('') +
          `</span>`,
      );
    } else if (type === 'tuples') {
      if (!Array.isArray(value) || !value.length) continue;
      const mark = { active: '●', done: '✔', failed: '✘' };
      parts.push(
        `<span class="state-item state-quests" title="${esc(f.description || '')}">${icon}${label} ` +
          value
            .map(
              (q) =>
                `<span class="state-quest ${esc(String(q?.status || 'active'))}">` +
                `${mark[q?.status] || '●'} ${esc(String(q?.title || ''))}</span>`,
            )
            .join('') +
          `</span>`,
      );
    } else if (type === 'flags') {
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue;
      const chips = Object.entries(value)
        .map(([k, v]) => `<span class="state-chip">${esc(k)}：${esc(String(v))}</span>`)
        .join('');
      if (!chips) continue;
      parts.push(`<span class="state-item" title="${esc(f.description || '')}">${icon}${label} ${chips}</span>`);
    } else {
      if (value === undefined || value === null || String(value) === '') continue;
      parts.push(
        `<span class="state-item" title="${esc(f.description || '')}">${icon}${label} ${esc(String(value))}</span>`,
      );
    }
  }
  if (!parts.length) {
    return `<div class="state-inner">🧭 状态块已解析，但里面还没有可显示的字段。
      ${editBtn('填写', '把模型没写的字段补上')}</div>`;
  }
  parts.push(editBtn('纠正', '模型写错了就自己改（校验规则与模型输出时同一套）'));
  return (
    `<div class="state-inner" title="这些状态来自模型每轮输出的 &lt;state&gt; 块，已由后端校验并落库；字段由角色卡 / 世界书定义">` +
    parts.join('') +
    `</div>`
  );
}

/**
 * 手动纠正状态：弹一个文本框让用户改当前状态的 JSON。
 *
 * ★ 为什么值得做：模型一定会写错（HP 记错、把丢掉的钥匙又写回背包）。
 *   没有这个入口时用户只能忍着让错误一路滚下去，或者重开会话。
 * ★ 校验仍然在后端（PATCH /sessions/{id}/state 走的是与模型输出同一套 normalize），
 *   所以"人写的"不会绕过"HP 越界夹住 / 上限跳变拒绝 / 脏字段丢弃"。
 *
 * ★ 这里原先写的是 `openDialog(...)` —— chat.js 里根本没有这个函数（它叫 `modal`），
 *   于是点「纠正」抛 ReferenceError、弹窗根本不开（探针 3.1 抓到的真实 bug）。
 *   教训：整页加载/弹窗这类"点了没反应"的问题，只有真实点击的断言能发现。
 */
async function openStateEditor(root) {
  const current = state.detail?.state;
  const fields = Array.isArray(state.detail?.state_schema?.fields)
    ? state.detail.state_schema.fields
    : [];
  // ★ 骨架按**这张卡声明的字段**生成（老实现硬编码 hp/inventory/location/quests，
  //   连没声明 HP 的卡也弹出一份带 HP 的骨架 —— 字段与提示词/状态栏就对不上了）。
  const scaffold = {};
  for (const f of fields) {
    if (f.type === 'meter') {
      scaffold[f.name] = f.initial_current ?? f.initial ?? 0;
      scaffold[f.max_field || 'max'] = f.initial_max ?? 100;
    } else if (f.type === 'number') {
      scaffold[f.name] = f.initial ?? 0;
    } else if (f.type === 'list' || f.type === 'tuples') {
      scaffold[f.name] = [];
    } else if (f.type === 'flags') {
      scaffold[f.name] = {};
    } else {
      scaffold[f.name] = '';
    }
  }
  // ★ 没有状态时**也要能编辑**（以前这里直接 return，点了"手动填写"毫无反应，
  //   属于静默无操作）。给一份骨架让用户手填，保存后 PATCH 会把第一份状态写进会话。
  const skeleton = current || scaffold;
  const fieldHint = fields.length
    ? fields
        .map((f) => `${f.label || f.name}（${f.type === 'meter' ? `${f.name} + ${f.max_field || 'max'}` : f.name}）`)
        .join('、')
    : '这张卡没有定义字段，可以直接写一个 JSON 对象（保存后以后就按它显示）';
  const m = modal({
    title: current ? '纠正当前状态' : '手动建立状态',
    bodyHTML: `
      <div class="hint mb8">
        直接改这段 JSON 即可（字段：${esc(fieldHint)}）。保存后会再过一遍校验：
        越界的数值会被夹住，缺失字段沿用原值。
        ${current ? '' : '现在还没有状态，下面是一份骨架，改成你要的数值即可。'}
      </div>
      <textarea name="state" rows="14" style="width:100%;font-family:var(--mono);font-size:12px">${esc(
        JSON.stringify(skeleton, null, 2),
      )}</textarea>`,
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-save-state">保存</button>`,
  });
  $('#btn-save-state', m.root).addEventListener('click', async () => {
    let parsed = null;
    try {
      parsed = JSON.parse($('[name=state]', m.root).value);
    } catch (err) {
      toastErr(`JSON 格式不对：${err.message}`);
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      toastErr('状态必须是一个 JSON 对象');
      return;
    }
    try {
      const res = await api.patch(`/narrative/sessions/${state.detail.id}/state`, {
        state: parsed,
      });
      m.close();
      toastOk(res?.message ? `状态已保存（${res.message}）` : '状态已保存');
      await silentReload(root);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    }
  });
}

function renderChatPanel(root, detail) {
  const main = $('#chat-main', root);
  const pureChat = Boolean(detail.pure_chat);
  const p = detail.provider;
  // ★ 纯聊天 = 适配层验收台：把"这一轮到底怎么被调用的"摊在头上
  //   （协议 / 模型 / 流式开关 / 备用模型），换配置之后一眼能看出差别。
  const providerLine = p
    ? `${esc(p.name)} / ${esc(p.model_name)} · ${esc(PROTOCOL_LABEL[p.provider_type] || p.provider_type)} · ${
        p.stream_enabled === false ? '<b>非流式</b>' : '流式'
      }${p.fallback_provider_id ? ' · 已设备用模型' : ''}`
    : '未配置模型';
  main.innerHTML = `
    <div class="chat-head">
      <div class="chat-head-main">
        <div class="chat-title" id="chat-title">${esc(detail.title)}</div>
        <div class="small muted" id="chat-sub">
          ${pureChat ? '<span class="badge info">纯聊天</span> 无角色' : esc(detail.character_card?.name || '（角色卡已删除）')}
          · ${providerLine}
          · 共 ${detail.message_count} 条 · 累计 ${detail.total_tokens} token
        </div>
        <!-- ★ Token 统计：把「上一轮花了多少」摊开给用户看。
             reasoning_tokens 单独列出来，是因为推理模型可能把 88% 的输出配额
             花在思考上（实测数据），不单独显示就完全看不出钱花在哪了。
             这一块每次流式结束时由 updateTokenStats() 就地更新，不重建整个面板。 -->
        <div class="small faint token-stats" id="token-stats">${tokenStatsHTML(null, detail)}</div>
      </div>
      <div class="chat-head-actions">
        <button class="btn sec sm" data-act="memories" title="看看模型记住了什么（向量长期记忆）">记忆</button>
        <button class="btn sec sm${detail.translate?.settings?.enabled ? ' on' : ''}" data-act="translate" id="btn-translate" title="跨语言对话：原文 / 译文可切换（默认关闭，开会多花钱）">🌐 翻译${detail.translate?.settings?.enabled ? '开' : ''}</button>
        <button class="btn sec sm" data-act="prompt" title="看看这一轮到底给模型发了什么">查看提示词</button>
        ${detail.vn ? `<button class="btn sec sm${vnStageOn(detail) ? ' on' : ''}" data-act="vn-stage" id="btn-vn-stage" title="立绘舞台：背景 + 立绘 + 台词框（表情跟着状态栏变）">🎭 舞台${vnStageOn(detail) ? '开' : '关'}</button>` : ''}
        <button class="btn sec sm" data-act="settings">设置</button>
        <button class="btn sec sm" data-act="refresh">刷新</button>
      </div>
    </div>

    <div id="chat-alerts">${detail.warnings?.length ? alertsHTML(detail.warnings) : ''}</div>

    ${pureChat ? '' : summaryBannerHTML(detail)}

    ${pureChat ? '' : vnStageHTML(detail)}

    <!-- ★ 第十六轮：状态条挪到**消息区末尾**（= 最新那条回复的下方），
         并且默认只占一行 —— 用户反馈"顶部那个框太大，把对话挤小了"。 -->
    <div class="messages" id="messages">${renderMessages(detail)}${
      pureChat ? '' : stateBarHTML(detail.state, detail.state_schema, lastRawState(detail))
    }</div>
    <button class="btn sec sm scroll-bottom hidden" data-act="scroll-bottom">↓ 回到最新</button>

    <div class="composer">
      <textarea id="composer-input" rows="2"
        placeholder="说点什么…（Enter 发送，Shift+Enter 换行）"></textarea>
      <div class="composer-side">
        <!-- ★ data-act 不能少：sendMessage 是靠 #chat-main 上的**委托监听器**
             根据 button[data-act] 分发的。少了这个属性，点「发送」会毫无反应
             （而且不报错）—— 这正是浏览器探针抓出来的问题。 -->
        <button class="btn" id="btn-send" data-act="send" disabled>发送</button>
        <button class="btn sec hidden" id="btn-stop" data-act="stop">停止</button>
      </div>
    </div>
    <div class="composer-hint small faint">
      上下文预算：${detail.provider ? `窗口 ${detail.provider.context_window} / 最大输出 ${detail.provider.max_tokens}（<strong>含思考过程</strong>）` : '未配置模型，无法对话'}
    </div>
  `;

  const box = $('#messages', root);
  if (box) box.scrollTop = box.scrollHeight;
}

function alertsHTML(list) {
  return list.map((t) => `<div class="alert warn" style="margin:10px 16px">⚠ ${esc(t)}</div>`).join('');
}

/* ==================================================================
   VN 舞台（角色卡立绘）
   ★ 数据全部由**后端**算好（`detail.vn`）：现在该显示哪张立绘、表情叫什么、
     有哪些可选表情，都是 `app/narrative/vn.py` 按"状态栏里的表情字段"算出来的。
     前端只画 —— 这样"表情 → 立绘"的规则只有一份，探针也能直接断言。
   ★ 开关状态存在浏览器本地（按会话记）：这是**看的方式**，不是数据，
     所以不落库、也不该占一次 PATCH。
   ================================================================== */
const VN_STAGE_KEY = 'hne_vn_stage';

function vnStageOn(detail) {
  if (!detail?.vn) return false;
  let stored = null;
  try {
    stored = window.localStorage.getItem(`${VN_STAGE_KEY}:${detail.id}`);
  } catch {
    stored = null;
  }
  if (stored === null) return true; // 卡既然开了 VN，默认就把舞台打开
  return stored === '1';
}

function setVnStage(detail, on) {
  try {
    window.localStorage.setItem(`${VN_STAGE_KEY}:${detail.id}`, on ? '1' : '0');
  } catch {
    /* 隐私模式下 localStorage 可能不可用：舞台照常显示，只是记不住开关 */
  }
}

function vnStageHTML(detail) {
  const vn = detail?.vn;
  if (!vn || !vnStageOn(detail)) return '';
  const position = ['left', 'center', 'right'].includes(vn.position) ? vn.position : 'center';
  const dim = Math.max(0, Math.min(80, Number(vn.background_dim ?? 20))) / 100;
  const scale = Math.max(0.3, Math.min(2, Number(vn.sprite_scale ?? 1)));
  // 台词框显示**最后一句角色台词**（最后一条 assistant 消息）；
  // 还没有台词时给一句提示，而不是空白框。
  const lines = (detail.messages || []).filter((m) => m.role === 'assistant');
  const last = lines[lines.length - 1];
  const line = last ? String(last.content || '').trim() : '';
  const dialogue = line || '（还没有台词 —— 在下面说一句话让角色开口）';
  const sprite = vn.sprite_url
    ? `<img class="vn-sprite" src="${esc(vn.sprite_url)}" alt="立绘"
            style="--vn-scale:${scale}" />`
    : `<div class="vn-no-sprite">这张卡没有可用的立绘<br /><span class="small faint">（地址写坏了？在角色卡编辑里能看到原因）</span></div>`;
  const mood = vn.expression
    ? `<span class="vn-mood" title="表情来自状态栏字段：${esc(vn.expression_field)}">${esc(vn.expression)}</span>`
    : '';
  const warn = vn.warnings?.length
    ? `<div class="vn-warn">⚠ ${vn.warnings.map((w) => esc(w)).join('；')}</div>`
    : '';
  return `
    <div class="vn-stage pos-${position}" id="vn-stage">
      ${
        vn.background
          ? `<img class="vn-bg" src="${esc(vn.background)}" alt="" />
             <div class="vn-bg-dim" style="--vn-dim:${dim}"></div>`
          : ''
      }
      ${sprite}
      ${vn.show_name ? `<div class="vn-nameplate">${esc(vn.name || '')}${mood}</div>` : ''}
      <div class="vn-line">${esc(dialogue.slice(0, 400))}</div>
      ${warn}
    </div>`;
}

/**
 * Token 统计条的 HTML。
 *
 * @param usage 最近一轮的模型用量（来自 SSE 的 done 事件；null 表示还没聊过）
 * @param detail 会话详情（用它显示累计值）
 *
 * ★ 为什么"累计"和"本轮"要分开显示？
 *   累计值包含全部历史（会随对话无限增长，且删消息后会重算），
 *   而"本轮"才是用户刚刚为这一次点击付的钱 —— 后者才是排查成本问题的依据。
 */
function tokenStatsHTML(usage, detail) {
  const total = detail?.total_tokens ?? 0;
  if (!usage) {
    return `<span title="累计值包含输入与输出 token；删消息后会按剩余消息重新估算">累计 ${total} token · 还没有可统计的调用</span>`;
  }
  const parts = [
    `本轮 输入 ${usage.prompt_tokens || 0}`,
    `输出 ${usage.completion_tokens || 0}`,
  ];
  if (usage.reasoning_tokens) {
    parts.push(
      `<span style="color:var(--warn)" title="思考也算在输出配额里（实测推理模型可能占 80% 以上）">其中思考 ${usage.reasoning_tokens}</span>`,
    );
  }
  parts.push(`合计 ${usage.total_tokens || 0}`);
  return `${parts.join(' · ')} ｜ <span title="累计值包含输入与输出 token；删消息后会按剩余消息重新估算">累计 ${total} token</span>`;
}

/** 流式结束后就地更新统计条（不重建面板，避免打断用户正在看的滚动位置）。 */
function updateTokenStats(root, usage, detail) {
  const box = $('#token-stats', root);
  if (!box) return;
  box.innerHTML = tokenStatsHTML(usage, detail ?? state.detail);
}

/**
 * 组装 consumeStream 需要的回调（发消息与重新生成共用一份）。
 *
 * ★ 抽出来是为了避免两条流式路径各写一遍、然后各自演化 ——
 *   这个项目已经因为"预览与真实请求走两条路径"踩过一次坑（见 pitfalls 第 15 条）。
 */
function streamHandlers(root, bubble) {
  return {
    onMeta: (data) => {
      // ★ 输入侧翻译发生在"流开始之前"（后端先落用户消息再生成），
      //   所以第一个事件到了就说明那一步已经过去，把提示收掉换回"生成中"。
      clearTranslating(bubble);
      setMeta(root, data);
    },
    onNotes: (data) => showNotes(root, data.notes || []),
    onReason: (delta) => appendReasoning(bubble, delta),
    onDelta: (delta) => {
      appendDelta(bubble, delta);
      scrollBottom(root, false);
    },
    // ★ 正文已经流完了，但译文还要等**一次完整的模型调用**（实测好几秒）。
    //   不提示的话用户看完外语原文就干等，以为界面卡住了（真实反馈）。
    onTranslating: (data) => showTranslating(bubble, data.lang, data.direction),
    onDone: (data) => {
      clearTranslating(bubble);
      finishBubble(bubble, data);
      // ★ 记下来：silentReload 会重建头部，统计条要能补回来
      state.usage = data.usage || null;
      updateTokenStats(root, state.usage);
    },
    onError: (data) => {
      clearTranslating(bubble);
      failBubble(bubble, data);
    },
  };
}

/**
 * 在气泡的标题行上显示「正在翻译…」。
 *
 * ★ 为什么要有它：翻译中间件是**同步**跑在流末尾的（`engine.py::translate_reply`），
 *   用户看到最后一个字之后会有几秒"什么都没发生" —— 这段等待必须可见。
 * ★ 用真实 DOM 节点而不是 CSS `content`：探针要能断言到这句话。
 */
function showTranslating(bubble, lang, direction) {
  if (!bubble) return;
  bubble.classList.add('translating');
  const role = bubble.querySelector('.msg-role');
  if (!role) return;
  let hint = role.querySelector('.tr-hint');
  if (!hint) {
    hint = document.createElement('span');
    hint.className = 'tr-hint';
    role.appendChild(hint);
  }
  const where = direction === 'input' ? '你的输入' : '回复';
  hint.textContent = `🌐 正在翻译${where}成${lang || '目标语言'}…`;
}

/** 收掉「正在翻译…」提示（译好了 / 出错 / 开始生成 都要收）。 */
function clearTranslating(bubble) {
  if (!bubble) return;
  bubble.classList.remove('translating');
  bubble.querySelector('.tr-hint')?.remove();
}

/**
 * 逐条翻译：把**这一条**消息译成面板里设定的目标语言（开场白也可以）。
 *
 * ★ 不走 autoReload、只改这一条气泡：整页重渲染会 abort 当前 view signal，
 *   把用户正在看的滚动位置和展开状态全打断（本项目的已知坑）。
 * ★ 后端会把"跳过/失败"的原因放在响应壳的 message 里（例如"这条看起来已经是简体中文"），
 *   这里必须如实弹出来 —— 点了没反应是最糟的体验。
 * ★ 两份文本都留在 DOM 里（`dataset.altText`），切换仍是纯前端。
 */
async function translateOneMessage(root, btn) {
  const bubble = btn.closest('.msg');
  const id = Number(bubble?.dataset.id);
  const sessionId = state.sessionId;
  if (!bubble || !id || !sessionId) return;

  const body = bubble.querySelector('.msg-body');
  const original = body?.textContent || '';
  btn.disabled = true;
  showTranslating(bubble, state.detail?.translate?.settings?.target_lang, 'reply');
  try {
    const envelope = await api.postRaw(
      `/narrative/sessions/${sessionId}/messages/${id}/translate`,
    );
    clearTranslating(bubble);
    const translation = envelope?.data?.message?.translation;
    if (!translation?.text) {
      // 没有译文 = 被跳过了（已有译文 / 已经是目标语言 / 太长 / 没配模型）⇒ 如实说
      toastErr(envelope?.message || '这条没有可翻译的内容');
      return;
    }
    if (body && !looksLikeHtml(original)) {
      bubble.dataset.altText = original;
      bubble.dataset.showing = 'translation';
      body.textContent = translation.text;
    }
    if (!bubble.querySelector('[data-act="toggle-translation"]')) {
      bubble.insertAdjacentHTML(
        'beforeend',
        translationChipHTML({ translation, content: original }),
      );
    }
    toastOk(envelope?.message || '翻译完成');
  } catch (err) {
    clearTranslating(bubble);
    toastErr(err?.toDisplay ? err.toDisplay() : String(err));
  } finally {
    btn.disabled = false;
  }
}

/* ==================================================================
   消息级操作：复制 / 编辑 / 撤回 / 重新生成
   ================================================================== */

/** 从被点的按钮找到它所在的那条消息（data-id 是后端消息 id）。 */
function msgOf(btn) {
  const node = btn.closest('.msg');
  if (!node) return null;
  return {
    id: Number(node.dataset.id),
    role: node.classList.contains('user') ? 'user' : 'assistant',
    content: node.querySelector('.msg-body')?.textContent || '',
  };
}

/** 复制一条消息的正文。 */
async function copyMessage(btn) {
  const msg = msgOf(btn);
  if (!msg?.content) return;
  try {
    await navigator.clipboard.writeText(msg.content);
    toastOk('已复制到剪贴板');
  } catch {
    // 无头浏览器 / 非 https 环境下 clipboard API 可能不可用，退回到旧办法
    const ta = document.createElement('textarea');
    ta.value = msg.content;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand?.('copy');
    ta.remove();
    ok ? toastOk('已复制到剪贴板') : toastErr('复制失败，请手动选中文本');
  }
}

/** 编辑一条用户消息：改完会删掉它之后的所有内容，然后自动重新生成。 */
async function openEditDialog(root, signal, msg) {
  if (!msg) return;
  const m = modal({
    title: '编辑这条消息',
    width: 'wide',
    bodyHTML: `
      <div class="alert warn">
        保存后会<strong>删除这条消息之后的全部内容</strong>（包括角色的回复），
        然后用新的内容重新生成。被删的消息无法恢复。
      </div>
      <div class="field">
        <label>你说的话</label>
        <textarea name="content" rows="5">${esc(msg.content)}</textarea>
      </div>`,
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-save-edit">保存并重新生成</button>`,
  });

  const input = $('[name=content]', m.root);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);

  const save = async () => {
    const content = (input.value || '').trim();
    if (!content) {
      toastErr('内容不能为空');
      return;
    }
    const btn = $('#btn-save-edit', m.root);
    const restore = buttonLoading(btn, '保存中');
    // ★ PATCH 与随后的重渲染之间存在一个"危险窗口"：
    //   此刻界面上还是**编辑前的旧消息**（旧的 id、不 pending），用户可以点它上面的
    //   「撤回 / 重新生成」，那两个操作会按旧 id 去删数据 —— 编辑的内容会被抹掉。
    //   用 state.streaming 把整个窗口罩住（发消息和这两个操作都会先看这个标志），
    //   等重渲染完成、拿到新的 id 之后再放开，最后才走重新生成。
    state.streaming = true;
    try {
      const res = await api.patch(
        `/narrative/sessions/${state.sessionId}/messages/${msg.id}`,
        { content },
      );
      m.close();
      if (signal?.aborted) return;
      if (res.deleted_messages) {
        toastOk(`已更新，并删除了其后的 ${res.deleted_messages} 条消息`);
      } else {
        toastOk('已更新');
      }
      await openSession(root, state.sessionId, signal);
      // 新的历史已经渲染出来了，危险窗口结束，放开标志再交给重新生成
      state.streaming = false;
      // 编辑的意义就是"改完重来"：紧接着重新生成这一轮的回复。
      // ★ 走 regenerate 而不是"再发一条"，历史里才不会出现两条重复的用户发言。
      //   这里先插一个空白气泡当目标（原来的回复刚被后端删掉了），
      //   否则重新生成的内容没有地方显示。
      const freshBubble = appendMessage(root, { role: 'assistant', content: '', pending: true });
      // ★ 不传 msg：让后端用"最后一条用户消息"当锚点。
      //   刚才的 PATCH 已经把这条消息之后的内容全删了，所以它必然就是最后一条。
      await regenerateMessage(root, signal, null, freshBubble);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      state.streaming = false;
      restore();
    }
  };

  $('#btn-save-edit', m.root).addEventListener('click', save);
  // Ctrl/Cmd + Enter 快速保存（文本域里 Enter 是换行，不能直接提交）
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      save();
    }
  });
}

/** 撤回最后一轮：删掉最后一条用户消息及之后的内容，并把原话放回输入框。 */
async function retractMessage(root, signal, msg) {
  if (!msg) return;
  const ok = await confirmDialog({
    title: '撤回这一轮',
    message:
      `会删除你说的话以及之后角色的回复（共若干条），无法恢复。\n\n` +
      `撤回后这句话会放回输入框，你可以改了再发。`,
    confirmText: '撤回',
    danger: true,
  });
  if (!ok) return;

  try {
    const res = await api.post(
      `/narrative/sessions/${state.sessionId}/messages/${msg.id}/retract`,
    );
    toastOk(`已撤回（删除了 ${res.deleted_messages} 条消息）`);
    await openSession(root, state.sessionId, signal);
    await refreshSessions(root, signal);
    // 把原话放回输入框：撤回之后十有八九是要改一改再发
    const input = $('#composer-input', root);
    if (input && res.retracted_content) {
      input.value = res.retracted_content;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
    }
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
  }
}

/**
 * 重新生成某一条回复。
 *
 * ★ 与"撤回后重发"的区别：不会多出一条重复的用户消息，
 *   提示词里的历史与上一轮完全一致（见后端 …/regenerate 的说明）。
 * ★ 会删掉这条回复**之后**的内容 —— 那些内容是接着它写的，留着会前后矛盾。
 *   如果它后面确实还有内容，先让用户确认（这和撤回一样是破坏性操作）。
 */
async function regenerateMessage(root, signal, msg, bubbleOverride) {
  if (state.streaming) return;
  if (!state.sessionId) return;
  // ★ msg 允许为空：编辑用户消息后要重写这一轮回复，那时被替换的回复
  //   已经不存在了（后端 PATCH 就删掉了），没有 id 可传 → 让后端按
  //   "最后一条用户消息"自己找锚点。以前这里传的是**用户消息的 id**，
  //   而后端只接受 assistant 的 id，于是必然 400（红条一闪就没了）。
  const useLastUser = !msg || !msg.id;

  // 这条回复之后还有消息吗？有的话必须先确认（会一起删掉）
  const box = $('#messages', root);
  const nodes = Array.from(box?.querySelectorAll('.msg') || []);
  const index = useLastUser ? -1 : nodes.findIndex((el) => Number(el.dataset.id) === msg.id);
  const followerCount = index >= 0 ? nodes.length - index - 1 : 0;

  if (followerCount > 0) {
    const go = await confirmDialog({
      title: '重新生成这条回复',
      message:
        `这条回复之后还有 ${followerCount} 条消息，它们会一起被删除（无法恢复）。\n\n` +
        `用户消息本身不会被改动，也不会多出一条重复的发言。`,
      confirmText: '删除并重新生成',
      danger: true,
    });
    if (!go) return;
  }

  // ★ 目标气泡优先用调用方传进来的：
  //   「编辑后自动重新生成」这条路上，被编辑的 assistant 回复在界面上还不存在
  //   （数据库里刚被删掉），只能由调用方先插一个空白气泡再交给这里填充。
  const bubble = bubbleOverride || nodes[index];
  if (!bubble) return;
  // 就地变成"生成中"，而不是先删再插（避免闪烁）
  bubble.classList.add('pending');
  bubble.classList.remove('failed');
  bubble.querySelector('.msg-reason')?.classList.add('hidden');
  // 旧的按钮先摘掉：重新生成结束后 finishBubble 会按新的消息 id 重新补上
  bubble.querySelector('.msg-actions')?.remove();
  const body = bubble.querySelector('.msg-body');
  if (body) body.textContent = '';
  bubble.querySelectorAll('.msg-error, .msg-warn').forEach((el) => el.remove());

  state.streaming = true;
  const sendBtn = $('#btn-send', root);
  const stopBtn = $('#btn-stop', root);
  if (sendBtn) sendBtn.disabled = true;
  stopBtn?.classList.remove('hidden');

  streamController = new AbortController();
  try {
    // ★ 路径里绝不能出现 undefined/null：那个会话已经被删掉时，
    //   模板串会拼出 `…/sessions/null/regenerate`，后端返回 422，
    //   用户看到的是"请求失败"而不是"这个会话已经不在了"。
    //    （这条是浏览器探针的"没有意外失败请求"抓出来的。）
    if (state.sessionId === null || state.sessionId === undefined || !Number(state.sessionId)) {
      throw Object.assign(new Error('会话已不存在，请从左侧重新选一个'), {
        display: '会话已不存在，请从左侧重新选一个',
        status: 404,
      });
    }
    const response = await fetch(
      `${API_PREFIX}/narrative/sessions/${state.sessionId}/regenerate` +
        (useLastUser ? '' : `?message_id=${msg.id}`),
      {
        method: 'GET',
        headers: authSession.token ? { Authorization: `Bearer ${authSession.token}` } : {},
        signal: streamController.signal,
      },
    );
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw Object.assign(new Error(payload?.message || `请求失败（HTTP ${response.status}）`), {
        display: payload?.message || `请求失败（HTTP ${response.status}）`,
        status: response.status,
      });
    }
    await consumeStream(response, streamHandlers(root, bubble));
  } catch (err) {
    if (err.name === 'AbortError') {
      bubble.classList.remove('pending');
      bubble.classList.add('aborted');
    } else {
      const text = err.display || err.message || '重新生成失败';
      // ★ 必须额外弹一次 toast：失败气泡会在 finally 的 silentReload 里
      //   随列表一起被重建掉，用户只看到"红条闪了一下"就没了（真实反馈）。
      toastErr(text);
      failBubble(bubble, {
        message: text,
        code: err.status === 0 ? 'NETWORK_ERROR' : 'REQUEST_FAILED',
      });
    }
  } finally {
    state.streaming = false;
    streamController = null;
    if (sendBtn) sendBtn.disabled = false;
    stopBtn?.classList.add('hidden');
    await silentReload(root);
  }
}

/* ==================================================================
   发消息 + 流式接收
   ================================================================== */
async function sendMessage(root, signal) {
  if (state.streaming) return;
  const input = $('#composer-input', root);
  const text = (input?.value || '').trim();
  if (!text) return;

  input.value = '';
  const sendBtn = $('#btn-send', root);
  const stopBtn = $('#btn-stop', root);
  sendBtn.disabled = true;
  stopBtn.classList.remove('hidden');
  state.streaming = true;

  // ★ 用户消息先落到界面上（后端也是先落库再调模型）：
  //   这样即使模型失败，用户也看得到自己说了什么
  appendMessage(root, { role: 'user', content: text });
  const bubble = appendMessage(root, { role: 'assistant', content: '', pending: true });

  // ★ 输入侧翻译也是"同步跑在前面"的（后端先落用户消息再做生成），
  //   所以在这里先把提示挂上；第一个事件（meta）到了就收掉换回"生成中"。
  const tr = state.detail?.translate?.settings || {};
  if (tr.enabled && tr.mode === 'middleware' && (tr.direction === 'input' || tr.direction === 'both')) {
    showTranslating(bubble, tr.target_lang, 'input');
  }

  streamController = new AbortController();
  const { signal: abortSignal } = streamController;

  try {
    const url =
      `${API_PREFIX}/narrative/sessions/${state.sessionId}/stream` +
      `?content=${encodeURIComponent(text)}`;

    const response = await fetch(url, {
      method: 'GET',
      headers: authSession.token ? { Authorization: `Bearer ${authSession.token}` } : {},
      signal: abortSignal,
    });

    if (!response.ok) {
      // 校验失败（404 / 400 / 401）走的是普通 JSON 响应，此时流还没开始
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      throw Object.assign(new Error(payload?.message || `请求失败（HTTP ${response.status}）`), {
        display: payload?.message || `请求失败（HTTP ${response.status}）`,
        status: response.status,
      });
    }

    await consumeStream(response, streamHandlers(root, bubble));
  } catch (err) {
    if (err.name === 'AbortError') {
      bubble.classList.remove('pending');
      bubble.classList.add('aborted');
      if (!bubble.querySelector('.msg-body').textContent.trim()) {
        bubble.querySelector('.msg-body').innerHTML = '<span class="faint">（已停止生成）</span>';
      } else {
        bubble.querySelector('.msg-body').insertAdjacentHTML(
          'beforeend',
          '<span class="faint"> ……（已停止生成）</span>',
        );
      }
    } else {
      failBubble(bubble, {
        message: err.display || err.message || '发送失败',
        code: err.status === 0 ? 'NETWORK_ERROR' : 'REQUEST_FAILED',
      });
    }
  } finally {
    state.streaming = false;
    streamController = null;
    sendBtn.disabled = false;
    stopBtn.classList.add('hidden');
    // 拉一次最新详情：消息 id、token 统计、裁剪情况都以服务端为准
    await silentReload(root);
  }
}

function stopStreaming() {
  if (streamController) streamController.abort();
}

function scrollBottom(root, force) {
  const box = $('#messages', root);
  if (!box) return;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  if (force || nearBottom) box.scrollTop = box.scrollHeight;
}

function appendMessage(root, { role, content, pending }) {
  const box = $('#messages', root);
  if (!box) return null;
  const node = document.createElement('div');
  node.className = `msg ${role}${pending ? ' pending' : ''}`;
  node.innerHTML = `
    <div class="msg-role">${esc(roleLabel(role))}<span class="typing"></span></div>
    <div class="msg-reason hidden"></div>
    <div class="msg-body">${esc(content)}</div>`;
  box.appendChild(node);
  // ★ 状态条要始终待在消息区**最后**（它是"最新那条回复的下方"）：
  //   新气泡插进来之后把它挪到末尾，否则它会显示在新回复的上方（位置就不对了）。
  const bar = box.querySelector('#state-bar');
  if (bar) box.appendChild(bar);
  scrollBottom(root, true);
  return node;
}

function appendReasoning(bubble, delta) {
  if (!bubble) return;
  let box = bubble.querySelector('.msg-reason');
  // ★ 兜底：缺容器就现造一个，别让"第一个思考增量"把整条流打断。
  //   气泡有两条来源（appendMessage / renderMessages），少写一个 div 的代价
  //   就是重新生成整个失败 —— 所以这里不再假设它一定存在。
  if (!box) {
    box = document.createElement('div');
    box.className = 'msg-reason hidden';
    const body = bubble.querySelector('.msg-body');
    if (body) bubble.insertBefore(box, body);
    else bubble.appendChild(box);
  }
  box.classList.remove('hidden');
  box.textContent += delta;
}

function appendDelta(bubble, delta) {
  if (!bubble) return;
  const body = bubble.querySelector('.msg-body');
  body.textContent += delta;
  // ★ 状态块是"给系统看的"，不该在聊天里滚出来（流式时会看到一串原始 JSON）。
  //   协议规定它在回复末尾，所以一出现 `<state>` 就把可见部分截到它之前；
  //   最终正文以服务端落库的为准（save_assistant_reply 已经把它剥掉了）。
  const cut = body.textContent.search(/<state>/i);
  if (cut >= 0) body.textContent = body.textContent.slice(0, cut).trimEnd();
}

function setMeta(root, data) {
  const sub = $('#chat-sub', root);
  if (!sub) return;
  const trimmed = data.trimmed
    ? ` · ⚠ 上下文已裁剪（丢弃 ${data.history_dropped} 条，保留 ${data.history_kept} 条）`
    : '';
  // 3.9：世界书命中数与记忆召回数要显示出来，
  // 否则用户完全不知道"关键词触发"和"长期记忆"到底有没有在工作
  const book = data.world_book_entries
    ? ` · 世界书命中 ${data.world_book_entries} 条${
        data.world_book_matched > data.world_book_entries
          ? `（共 ${data.world_book_matched} 条，超出预算）`
          : ''
      }`
    : '';
  const memory = data.recalled_memories ? ` · 回忆 ${data.recalled_memories} 条` : '';
  const text = `估算输入 ${data.estimated_input_tokens} / 预算 ${data.input_budget} token${book}${memory}${trimmed}`;
  // 先移除上一次的，避免反复发消息时越堆越多
  sub.querySelectorAll('.chat-meta').forEach((node) => node.remove());
  sub.insertAdjacentHTML(
    'beforeend',
    `<span class="chat-meta" title="输入预算 = 上下文窗口 − 最大输出（含思考）− 安全余量；世界书只注入命中的条目；回忆按语义检索">` +
      ` · ${esc(text)}</span>`,
  );
  if (data.memory_error) {
    sub.insertAdjacentHTML(
      'beforeend',
      `<span class="chat-meta" style="color:var(--warn)"> · ⚠ 记忆检索失败：${esc(
        data.memory_error,
      )}</span>`,
    );
  }
}

function showNotes(root, notes) {
  if (!notes?.length) return;
  const box = $('#chat-alerts', root);
  if (!box) return;
  box.insertAdjacentHTML(
    'beforeend',
    notes
      .map((t) => `<div class="alert info" style="margin:10px 16px">ℹ ${esc(t)}</div>`)
      .join(''),
  );
}

/**
 * 把消息操作按钮补进一个**流式生成出来的**气泡。
 *
 * ★ 必须在这里补，不能只等流结束后的整页刷新：
 *   appendMessage 造的空气泡只有 role/正文，按钮是 renderMessages 才渲染的，
 *   而 renderMessages 要等 silentReload 回来才跑。中间那段时间里
 *   "重新生成 / 复制"按钮是不存在的 —— 浏览器探针正好卡在这个窗口上抓到了失败
 *   （表现出来就像"功能没做"）。所以流一结束就就地补上，不再依赖刷新时机。
 */
function attachActions(bubble, data) {
  if (!bubble || bubble.querySelector('.msg-actions')) return;
  const id = Number(data?.message_id) || Number(bubble.dataset.id) || null;
  if (id) bubble.dataset.id = String(id);
  const role = Number(id) && bubble.classList.contains('user') ? 'user' : 'assistant';
  bubble.insertAdjacentHTML('beforeend', messageActions({ role, id }));
}

function finishBubble(bubble, data) {
  if (!bubble) return;
  bubble.classList.remove('pending');
  // ★ 先捞纯文本、最后才挂渲染：
  //   下面的"截断警告"是插进 .msg-body 里的，等它插完再取 textContent，
  //   会把警告文字也当成消息正文渲染进卡片里。
  const body = bubble.querySelector('.msg-body');
  const rawText = body?.textContent || '';
  if (data.truncated) {
    bubble.insertAdjacentHTML(
      'beforeend',
      '<div class="msg-warn">⚠ 输出达到「最大输出 token」上限被截断（该上限包含思考过程）</div>',
    );
  }
  const role = bubble.querySelector('.msg-role');
  if (data.model_name) role.insertAdjacentHTML('beforeend', `<span class="faint"> · ${esc(data.model_name)}</span>`);
  (data.notes || []).forEach((t) => {
    bubble.insertAdjacentHTML('beforeend', `<div class="msg-warn">ℹ ${esc(t)}</div>`);
  });
  attachActions(bubble, data);
  // 骰子气泡：随 "done" 事件一起到（不等刷新，用户立刻看到点数）
  const rolls = rollsHTML(data.rolls);
  if (rolls) bubble.insertAdjacentHTML('beforeend', rolls);
  // 翻译中间件：译好了就把正文切成"给人看的那一份"，并给出切换按钮。
  // ★ 两份文本都留在 DOM 里（data-alt-text），切换是纯前端的 —— 不发请求、不改数据。
  if (data.translation?.text) {
    const alt =
      data.translation.display === 'content' ? data.translation.text : rawText;
    const shown =
      data.translation.display === 'content' ? rawText : data.translation.text;
    if (body && !looksLikeHtml(rawText)) {
      bubble.dataset.altText = alt;
      bubble.dataset.showing =
        data.translation.display === 'content' ? 'content' : 'translation';
      body.textContent = shown;
    }
    bubble.insertAdjacentHTML(
      'beforeend',
      translationChipHTML({ translation: data.translation, content: rawText }),
    );
  }
  // ★ 最小回复长度：云端接口都没有 min_tokens 参数，所以这条要求只能写进提示词 +
  //   生成后真实量一遍。量到偏短就如实说出来，而不是假装没这回事
  //   （用户要的是"硬性规则"，静默放过就等于没有）。
  const minChars = Number(state.detail?.builtin_guard?.min_reply_chars || 0);
  const actual = rawText.trim().length;
  if (minChars > 0 && actual > 0 && actual < minChars) {
    bubble.insertAdjacentHTML(
      'beforeend',
      `<div class="msg-warn">⚠ 本次回复只有 ${actual} 字，低于内置守卫规则要求的 ${minChars} 字。` +
        `可以点「重新生成」，或在「提示词预设」里把这条规则调小/删掉。</div>`,
    );
  }
  // HTML 卡片（尤其是开场白）在这一刻才换成"渲染视图"
  if (body && looksLikeHtml(rawText)) {
    body.dataset.richMounted = '1';
    mountRich(body, rawText, {
      char: state.detail?.character_card?.name || '',
      user: authSession.user?.username || '',
    });
  }
}

function failBubble(bubble, data) {
  if (!bubble) return;
  bubble.classList.remove('pending');
  bubble.classList.add('failed');
  bubble.insertAdjacentHTML(
    'beforeend',
    `<div class="msg-error">⚠ ${esc(data.message || '生成失败')}${
      data.code ? ` <span class="faint">(${esc(data.code)})</span>` : ''
    }</div>`,
  );
  // 失败的回复同样要能"重新生成"与"复制"，否则用户只能重开会话
  attachActions(bubble, data);
}

async function silentReload(root) {
  if (!state.sessionId) return;
  try {
    // ★ 先把这一轮的「上下文用量」记下来：
    //   renderChatPanel 会重建整个头部，如果不带过去，
    //   刚显示出来的用量提示会在流结束的瞬间被抹掉（探针里实测到了这个闪烁）。
    const previousMeta = $('#chat-sub .chat-meta', root)?.textContent || '';
    const detail = await api.get(`/narrative/sessions/${state.sessionId}`);
    state.detail = detail;
    // 只更新标题栏统计与消息列表，不重置用户正在看的滚动位置
    const box = $('#messages', root);
    const atBottom = box ? box.scrollHeight - box.scrollTop - box.clientHeight < 120 : true;
    renderChatPanel(root, detail);
    // 重建头部会把 token 统计重置成"还没调用"，这里用记住的上一轮用量补回来
    updateTokenStats(root, state.usage, detail);
    if (previousMeta) {
      const sub = $('#chat-sub', root);
      if (sub) sub.insertAdjacentHTML('beforeend', `<span class="chat-meta">${esc(previousMeta)}</span>`);
    }
    if (atBottom) scrollBottom(root, true);
    refreshSessions(root, activeSignal);
  } catch {
    /* 刷新失败不影响已经显示的内容 */
  }
}

/* ==================================================================
   SSE 解析（与后端 sse_event 严格对应）
   ================================================================== */
async function consumeStream(response, handlers) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let eventName = 'message';
  let dataLines = [];

  const flush = () => {
    if (!dataLines.length) {
      eventName = 'message';
      return;
    }
    const raw = dataLines.join('\n');
    dataLines = [];
    const name = eventName;
    eventName = 'message';
    let data = {};
    try {
      data = JSON.parse(raw);
    } catch {
      return; // 解析不了的片段直接跳过，不能因此中断整个流
    }
    if (name === 'meta') handlers.onMeta?.(data);
    else if (name === 'notes') handlers.onNotes?.(data);
    else if (name === 'reason') handlers.onReason?.(data.delta || '');
    else if (name === 'delta') handlers.onDelta?.(data.delta || '');
    else if (name === 'translating') handlers.onTranslating?.(data);
    else if (name === 'done') handlers.onDone?.(data);
    else if (name === 'error') handlers.onError?.(data);
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let index;
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, '');
      buffer = buffer.slice(index + 1);

      if (line === '') {
        // 空行 = 一条事件结束（SSE 规范的硬性约定）
        flush();
      } else if (line.startsWith(':')) {
        // 注释行（心跳），忽略
      } else if (line.startsWith('event:')) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trim());
      }
    }
  }
  flush();
}

/* ==================================================================
   新建会话
   ================================================================== */
async function openCreateDialog(root, signal) {
  let cards;
  let providers;
  try {
    [cards, providers] = await Promise.all([
      api.get('/character-cards', { scope: 'all', limit: 100 }),
      api.get('/providers'),
    ]);
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
    return;
  }
  if (signal?.aborted) return;

  if (!cards.items.length) {
    toastWarn('还没有可用的角色卡，请先到「角色卡」页新建或复制一张');
    return;
  }

  const cardOptions = cards.items
    .map(
      (c) =>
        `<option value="${c.id}">${esc(c.name)}${c.is_owner ? '' : '（公开卡）'} #${c.id}${
          c.has_greeting ? '' : ' · 无开场白'
        }</option>`,
    )
    .join('');

  const providerOptions = providers.length
    ? providers
        .map(
          (p) =>
            `<option value="${p.id}" ${p.is_default ? 'selected' : ''}>${esc(p.name)} · ${esc(
              p.model_name,
            )}</option>`,
        )
        .join('')
    : '';

  const m = modal({
    title: '新建叙事会话',
    bodyHTML: `
      <div class="field">
        <label>角色卡</label>
        <select name="character_card_id" id="sel-card">${cardOptions}</select>
        <div class="hint">「全部可见」范围内包含别人公开的卡 —— 能用来开局，但不能修改它。</div>
      </div>
      <div class="field">
        <label>模型配置</label>
        ${
          providerOptions
            ? `<select name="llm_provider_id">${providerOptions}</select>`
            : `<div class="alert warn">你还没有配置任何模型。会话可以先建好，但要点「模型配置」加一个才能对话。</div>`
        }
      </div>
      ${textField('会话标题（可选）', 'title', '', { placeholder: '留空则用「角色名 · 时间」' })}
      <div class="alert info">
        这张卡的开场白会成为对话的第一条消息 —— 你打开会话就能看到角色先开口。
      </div>`,
    footHTML: `
      <button class="btn sec" data-close>取消</button>
      <button class="btn" id="btn-create">创建会话</button>`,
  });

  $('#btn-create', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '创建中');
    try {
      const values = formValues(m.root);
      const payload = {
        character_card_id: Number(values.character_card_id),
        title: values.title?.trim() || null,
      };
      if (providerOptions) payload.llm_provider_id = Number(values.llm_provider_id);

      const created = await api.post('/narrative/sessions', payload);
      m.close();
      // ★ 创建**已经成功**了，就不能因为"手里那个视图信号失效了"而丢掉它。
      //   弹窗开着的这段时间里视图可能被重渲染过（切页 / 再次进入对话页），
      //   此时旧 signal 已 abort —— 以前这里直接 return，用户看到的是
      //   "提示说会话已创建，界面却毫无反应"（探针偶发变红就是撞上了这个）。
      //   改用**当前**的视图信号做后续刷新与打开。
      const live = activeSignal;
      toastOk('会话已创建');
      state.sessionId = created.id;
      await refreshSessions(root, live);
      await openSession(root, created.id, live);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

/* ==================================================================
   纯聊天（无角色）
   ================================================================== */
async function openPureChatDialog(root, signal) {
  let providers;
  try {
    providers = await api.get('/providers');
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
    return;
  }
  if (signal?.aborted) return;

  const providerOptions = (providers || [])
    .map(
      (p) =>
        `<option value="${p.id}" ${p.is_default ? 'selected' : ''}>${esc(p.name)} · ${esc(
          p.model_name,
        )} · ${esc(PROTOCOL_LABEL[p.provider_type] || p.provider_type)}</option>`,
    )
    .join('');

  const m = modal({
    title: '新建纯聊天（无角色）',
    bodyHTML: `
      <div class="alert info">
        纯聊天<strong>不选角色卡</strong>：不装人设、不套提示词预设与内置守卫、不注入状态协议，
        用的是「通用助手」提示词。适合两件事：<strong>确认自己的 API 配置到底通不通</strong>、
        以及不想建角色卡时直接聊两句。
      </div>
      <div class="field">
        <label>模型配置</label>
        ${
          providerOptions
            ? `<select name="llm_provider_id">${providerOptions}</select>
               <div class="hint">换一个配置再发一句话，就能对比不同 API / 协议 / 参数的表现（适配层验收台）。</div>`
            : `<div class="alert warn">你还没有配置任何模型。会话可以先建好，但要点「模型配置」加一个才能对话。</div>`
        }
      </div>
      ${textField('会话标题（可选）', 'title', '', { placeholder: '留空则用「纯聊天 · 时间」' })}`,
    footHTML: `
      <button class="btn sec" data-close>取消</button>
      <button class="btn" id="btn-create-pure">创建并开始</button>`,
  });

  $('#btn-create-pure', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '创建中');
    try {
      const values = formValues(m.root);
      const payload = { character_card_id: null, title: values.title?.trim() || null };
      if (providerOptions) payload.llm_provider_id = Number(values.llm_provider_id);
      const created = await api.post('/narrative/sessions', payload);
      m.close();
      // 理由同上面「新建会话」：创建成功就必须打开，别被旧信号吞掉。
      const live = activeSignal;
      toastOk('纯聊天会话已创建');
      state.sessionId = created.id;
      await refreshSessions(root, live);
      await openSession(root, created.id, live);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

/* ==================================================================
   会话操作
   ================================================================== */
async function renameSession(root, id, signal) {
  const current = state.detail?.id === id ? state.detail.title : '';
  const m = modal({
    title: '重命名会话',
    width: 'narrow',
    bodyHTML: textField('新标题', 'title', current, { maxlength: 200 }),
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-ok">保存</button>`,
  });

  $('#btn-ok', m.root).addEventListener('click', async (e) => {
    const title = ($('[name=title]', m.root).value || '').trim();
    if (!title) {
      toastErr('标题不能为空');
      return;
    }
    const restore = buttonLoading(e.target, '保存中');
    try {
      await api.patch(`/narrative/sessions/${id}`, { title });
      m.close();
      toastOk('已重命名');
      await refreshSessions(root, signal);
      if (state.sessionId === id) await openSession(root, id, signal);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

async function toggleArchive(root, id, signal) {
  const item = $$('.session-item', root).find((el) => Number(el.dataset.id) === id);
  const isArchived = item?.querySelector('[data-act="archive"]')?.textContent.includes('恢复');
  const next = isArchived ? 'active' : 'archived';
  try {
    await api.patch(`/narrative/sessions/${id}`, { status: next });
    toastOk(next === 'archived' ? '已归档（不会删掉任何消息）' : '已恢复到进行中');
    if (state.sessionId === id && next === 'archived' && state.status === 'active') {
      // 归档后它就不在当前列表里了，清掉右侧面板避免"看着像还在"
      state.sessionId = null;
      $('#chat-main', root).innerHTML = `<div class="empty" style="padding:60px">
        <div class="big">🗂</div><div>这个会话已归档</div>
        <div class="small mt8">切到左边「已归档」标签可以继续查看或恢复它。</div></div>`;
    }
    await refreshSessions(root, signal);
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
  }
}

async function deleteSession(root, id, signal) {
  const ok = await confirmDialog({
    title: '删除会话',
    message: '会话与其中的全部消息都会被永久删除，无法恢复。确定吗？',
    confirmText: '删除',
    danger: true,
  });
  if (!ok) return;

  try {
    const result = await api.del(`/narrative/sessions/${id}`);
    toastOk(`已删除（连同 ${result.deleted_messages} 条消息）`);
    if (state.sessionId === id) {
      state.sessionId = null;
      $('#chat-main', root).innerHTML = `<div class="empty" style="padding:60px">
        <div class="big">💬</div><div>会话已删除</div></div>`;
    }
    await refreshSessions(root, signal);
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
  }
}

/* ==================================================================
   提示词 / 设置
   ================================================================== */
function showPromptDialog() {
  const detail = state.detail;
  if (!detail?.prompt) {
    toastErr('请先打开一个会话');
    return;
  }
  const sourceText = {
    card: '角色卡自带的自定义系统提示词',
    assembled: '引擎按角色卡的人设字段自动拼装',
    world_book_only: '只有世界书设定（没有角色卡）',
    none: '没有任何设定（角色卡可能已被删除）',
    pure_chat: '纯聊天会话 · 通用助手提示词（不套预设与守卫）',
    preset: `提示词预设装配${detail.effective_preset ? ` · ${detail.effective_preset.name}` : ''}`,
  }[detail.prompt.system_prompt_source] || detail.prompt.system_prompt_source;

  const used = detail.prompt.preset_blocks_used || [];
  const skipped = detail.prompt.preset_blocks_skipped || [];
  const disabled = detail.prompt.preset_blocks_disabled || [];
  const unknownMacros = detail.prompt.preset_unknown_macros || [];
  const depths = detail.prompt.depth_blocks || [];

  // ★ 混合检索（关键词 + 语义融合）也要摊开：
  //   加了融合/去重/重排之后，排序不再是"命中就进"，用户没法自己判断
  //   "为什么这条进来了、那条没有" —— 所以计数 + 逐条来源/分数/去留原因都给出来。
  const retrieval = detail.prompt.retrieval || {};
  const retrievalItems = detail.prompt.retrieval_items || [];
  const retrievalHTML = detail.prompt.retrieval_summary
    ? `
      <div class="hint mb8" id="retrieval-summary">
        🔎 ${esc(detail.prompt.retrieval_summary)}
        ${
          retrievalItems.length
            ? `<details class="mt8"><summary>逐条候选明细（${retrievalItems.length} 条：来源 / 两路名次 / 最终分 / 去留原因）</summary>
                 <pre class="prompt-view small">${esc(retrievalItems.join('\n'))}</pre></details>`
            : ''
        }
        ${
          (retrieval.errors || []).length
            ? `<div class="small" style="color:var(--warn)">${esc((retrieval.errors || []).join('；'))}</div>`
            : ''
        }
      </div>`
    : '';

  // ★ 预设生效时必须把"每一块的下场"摊开：
  //   破甲这类东西看不见就等于不知道有没有生效，用户会反复怀疑。
  //   这里只给汇总与去向，逐条内容交给下面的"完整装配预览"（由服务端同一套逻辑构建），
  //   避免前端自己再拼一份、然后和服务端不一致。
  const presetHTML = detail.prompt.preset_id
    ? `
      <div class="form-section mt14">
        <h3>预设装配明细 · ${esc(detail.prompt.preset_name || '')}</h3>
        <div class="mb8">
          <span class="badge ok">生效 ${used.length} 块</span>
          ${depths.length ? `<span class="badge info">深度注入 ${depths.length} 块（depth ${depths.join('/')}）</span>` : ''}
          ${disabled.length ? `<span class="badge neutral">被禁用 ${disabled.length} 块</span>` : ''}
          ${skipped.length ? `<span class="badge warn">跳过 ${skipped.length} 块</span>` : ''}
        </div>
        <div class="hint">
          生效的块：${used.map((x) => `<code>${esc(x)}</code>`).join('、')}
        </div>
        ${disabled.length ? `<div class="hint mt8">被禁用（你关掉的，不会注入）：${disabled.map(esc).join('、')}</div>` : ''}
        ${skipped.length ? `<div class="hint mt8">被跳过（没有内容 / 本系统不支持）：${skipped.map(esc).join('；')}</div>` : ''}
        ${
          unknownMacros.length
            ? `<div class="alert warn mt8">预设里用到本系统不认识的宏：${unknownMacros
                .map((x) => `<code>${esc(x)}</code>`)
                .join('、')} —— 它们会<strong>原样保留</strong>在提示词里（不会生效，但也不会被悄悄删掉）。</div>`
            : ''
        }
        <div class="mt8">
          <button class="btn sec sm" id="btn-preview-preset">查看完整装配预览（含深度注入插在哪里）</button>
        </div>
        <div id="preset-preview" class="mt8"></div>
      </div>`
    : '';

  const m = modal({
    title: '本次会发给模型的提示词',
    width: 'wide',
    bodyHTML: `
      <div class="alert info">
        来源：<strong>${esc(sourceText)}</strong>
        ${detail.prompt.world_book_entries ? ` · 注入世界书 ${detail.prompt.world_book_entries} 条` : ''}
        ${detail.prompt.has_post_history_instructions ? ' · 含尾注指令' : ''}
      </div>
      <div class="hint mb8">
        这份预览按当前的全部历史构建。真正请求时如果超出上下文预算，
        最早的消息会被裁掉并压进「前情提要」——界面上会在对话里提示。
        另外，被「剧情总结」覆盖的若干轮不会逐条发送（它们由前情提要代表，
        全文可在「设置」里看到）。
      </div>
      ${retrievalHTML}
      <pre class="prompt-view">${esc(detail.prompt.system_prompt || '（空）')}</pre>
      ${presetHTML}`,
    footHTML: `<button class="btn sec" data-close>关闭</button>`,
  });

  // 完整预览走服务端的同一套装配逻辑（不是前端再猜一遍）
  const btn = $('#btn-preview-preset', m.root);
  if (btn) {
    btn.addEventListener('click', async () => {
      const box = $('#preset-preview', m.root);
      const restore = buttonLoading(btn, '构建中');
      try {
        const preview = await api.get('/prompt-presets/preview', {
          session_id: detail.id,
        });
        box.innerHTML = renderPresetPreview(preview);
      } catch (err) {
        box.innerHTML = `<div class="alert danger">${esc(err.toDisplay ? err.toDisplay() : String(err))}</div>`;
      } finally {
        restore();
      }
    });
  }
}

/** 把装配预览渲染成"逐条消息 + 来源块"的清单。 */
function renderPresetPreview(preview) {
  const notes = (preview.notes || []).map((n) => `<div class="hint">${esc(n)}</div>`).join('');
  const warnings = (preview.warnings || [])
    .map((w) => `<div class="alert warn mt8">${esc(w)}</div>`)
    .join('');
  const rows = (preview.messages || [])
    .map((msg) => {
      const where =
        msg.position === 'depth'
          ? `<span class="badge info">插进历史 · depth ${msg.depth}</span>`
          : msg.position === 'history'
            ? '<span class="badge neutral">对话历史</span>'
            : '<span class="badge ok">系统提示词</span>';
      return `
        <div class="card-sm">
          <div class="mb8">${where} <span class="badge neutral">${esc(msg.role)}</span>
            <span class="faint small">来源：${(msg.from_blocks || []).map(esc).join('、')}</span></div>
          <pre class="prompt-view" style="max-height:220px">${esc((msg.content || '').slice(0, 4000))}</pre>
        </div>`;
    })
    .join('');

  return `
    <div class="alert info">
      实际生效的预设：<strong>${esc(preview.preset_name || '（未使用预设）')}</strong>
      · 本次带上 ${preview.history_messages} 条历史消息
    </div>
    ${warnings}
    <div class="stack">${rows}</div>
    <div class="mt8">${notes}</div>`;
}

/* ==================================================================
   长期记忆（3.9）：看看模型"记住"了什么
   ================================================================== */
async function openMemoryDialog(root, signal) {
  const detail = state.detail;
  if (!detail) return;

  const m = modal({
    title: '记忆管理面板',
    width: 'wide',
    bodyHTML: `
      <div id="mem-summary"><div class="empty" style="padding:16px">加载记忆总结设置…</div></div>
      <div class="form-section mt14">
        <h3>长期记忆（向量召回）</h3>
        <div class="hint mb8">
          每聊完一轮，对话内容都会被存进向量库；之后你说到相关的事情时，
          系统会按<strong>语义</strong>把相关的几条回忆找出来，附在系统提示词里 ——
          所以哪怕字面上完全不同（"我的宠物叫什么" vs "我养了只橘猫"），也能想起来。
          记忆默认<strong>只在当前会话内</strong>检索，不会把别的故事的剧情串进来。
        </div>
        <div class="row" style="align-items:flex-end">
          <div style="flex:2">
            <label class="small muted">按语义检索（留空则随便看看）</label>
            <input type="text" id="mem-q" placeholder="例如：我们约在哪里见面" />
          </div>
          <div style="flex:0 0 auto">
            <button class="btn sec" id="btn-mem-search">检索</button>
          </div>
        </div>
        <div class="mt14" id="mem-list"><div class="empty" style="padding:20px">加载中…</div></div>
      </div>
      <div class="form-section mt14">
        <h3>手动记住一条</h3>
        <div class="hint mb8">剧情里定下来的事（约定、承诺、关键道具）值得单独记一条。</div>
        <div class="row" style="align-items:flex-end">
          <div style="flex:2">
            <input type="text" id="mem-new" placeholder="例如：约定每逢满月在小镇钟楼碰面" />
          </div>
          <div style="flex:0 0 auto">
            <button class="btn" id="btn-mem-add">记住</button>
          </div>
        </div>
      </div>`,
    footHTML: `
      <button class="btn sec danger-link" id="btn-mem-clear">清空本会话记忆</button>
      <button class="btn link small danger-link" id="btn-mem-clear-all"
        title="把所有会话的长期记忆一起清掉（一开多张卡时慎用）">清空全部会话…</button>
      <span class="spacer"></span>
      <button class="btn sec" data-close>关闭</button>`,
  });

  // ---------------- 记忆总结（「记忆管理面板」的核心一块）----------------
  //
  // ★ 设计要点（用户拍板）：
  //   · 总结与否**由用户决定**：到点只弹横幅提醒，绝不偷偷花 token（auto 默认关）
  //   · 轮数可调（默认 8）、五种总结提示词、字数上限、可以单独指定总结模型
  //   · 总结内容**可编辑**，并且能「恢复上一次」
  async function loadSummary() {
    const box = $('#mem-summary', m.root);
    if (!box) return;
    let data;
    try {
      data = await api.get(`/narrative/sessions/${detail.id}/memory-summary`);
    } catch (err) {
      box.innerHTML = `<div class="alert danger">${esc(err.toDisplay ? err.toDisplay() : String(err))}</div>`;
      return;
    }
    const s = data.settings || {};
    const cov = data.coverage;
    const rem = data.reminder || {};
    const modeOptions = (data.modes || [])
      .map((item) => `<option value="${esc(item.value)}" ${s.mode === item.value ? 'selected' : ''}>${esc(item.label)}</option>`)
      .join('');
    const modeHint = (data.modes || []).find((item) => item.value === s.mode)?.hint || '';
    const providerOptions = [
      `<option value="">跟随会话模型</option>`,
      ...(data.providers || []).map(
        (p) =>
          `<option value="${p.id}" ${String(s.provider_id ?? '') === String(p.id) ? 'selected' : ''}>${esc(p.name)} · ${esc(p.model_name)}</option>`,
      ),
    ].join('');
    const charOptions = (data.max_chars_choices || []).map(
      (n) => `<option value="${n}" ${Number(s.max_chars) === Number(n) ? 'selected' : ''}>${n} 字</option>`,
    ).join('');
    box.innerHTML = `
      <div class="form-section">
        <h3>记忆总结</h3>
        <div class="hint mb8">
          每攒够若干轮就把「旧总结 + 这一块对话」合并成<strong>一份</strong>新总结（旧总结被替换，
          所以同一段剧情只占一份 token）。被覆盖的对话不再逐条发给模型。
          <strong>总结会消耗你的 API token</strong>，所以默认只提醒、不自动做。
        </div>
        <label class="row" style="gap:8px;align-items:center">
          <input type="checkbox" name="ms-enabled" ${s.enabled ? 'checked' : ''} />
          <span>启用记忆总结</span>
        </label>
        <label class="row mt8" style="gap:8px;align-items:center">
          <input type="checkbox" name="ms-auto" ${s.auto ? 'checked' : ''} />
          <span>到点<strong>自动</strong>总结（会自己花 token；不勾就只弹横幅等你点）</span>
        </label>
        <label class="row mt8" style="gap:8px;align-items:center">
          <input type="checkbox" name="ms-remind" ${s.remind ? 'checked' : ''} />
          <span>到点在对话上方弹横幅提醒（不花钱）</span>
        </label>
        <div class="row mt14" style="gap:14px;flex-wrap:wrap;align-items:flex-end">
          <div>
            <label class="small muted">每几轮触发</label>
            <input type="number" name="ms-rounds" min="${data.rounds_range?.[0] ?? 2}" max="${data.rounds_range?.[1] ?? 50}"
              value="${esc(String(s.rounds ?? 8))}" style="width:90px" />
          </div>
          <div>
            <label class="small muted">总结模式</label>
            <select name="ms-mode">${modeOptions}</select>
          </div>
          <div>
            <label class="small muted">字数上限</label>
            <select name="ms-max-chars">${charOptions}</select>
          </div>
          <div>
            <label class="small muted">总结用哪个模型</label>
            <select name="ms-provider">${providerOptions}</select>
          </div>
        </div>
        <div class="hint mt8" id="ms-mode-hint">${esc(modeHint)}</div>
        <label class="row mt8" style="gap:8px;align-items:center">
          <input type="checkbox" name="ms-preset-prompt" ${s.use_preset_prompt ? 'checked' : ''} />
          <span>从「提示词预设」里的「记忆总结」块读取提示词</span>
        </label>
        <div class="mt8">
          <label class="small muted">自定义总结提示词（选「自定义」模式时生效）</label>
          <textarea name="ms-prompt" rows="4" style="width:100%;font-family:var(--mono);font-size:12px"
            placeholder="留空则用所选模式的内置模板">${esc(s.prompt || '')}</textarea>
        </div>

        <div class="mt14">
          <div class="row" style="align-items:center;gap:10px">
            <strong>总结内容</strong>
            <span class="small muted">
              ${cov ? `覆盖第 ${cov.from_round}~${cov.to_round} 轮` : '还没有总结'}
              ${rem.due ? ` · 已积累 ${rem.pending} 轮待总结` : ''}
              ${rem.cost_tokens > 0 ? ` · 本次约 ${rem.cost_tokens} token` : ' · 该模式不消耗 token'}
            </span>
            <span class="spacer"></span>
            <button class="btn sm" id="btn-ms-run">立即总结</button>
            <button class="btn sec sm" id="btn-ms-save">保存设置</button>
            <button class="btn sec sm" id="btn-ms-restore" ${data.history?.length ? '' : 'disabled'}
              title="把总结换回上一个版本">恢复上一次</button>
          </div>
          <textarea name="ms-content" rows="8" class="mt8"
            style="width:100%;font-family:var(--mono);font-size:12px"
            placeholder="还没有总结。点「立即总结」生成，或直接在这里手写一份。">${esc(data.content || '')}</textarea>
          <div class="hint">
            可以直接改上面的内容并点「保存设置」—— 改完会重新盖上覆盖表头；
            历史版本 ${data.history?.length || 0} 份，随时能「恢复上一次」。
          </div>
        </div>

        <div class="mt14" id="ms-anchors">
          <div class="row" style="align-items:center;gap:10px">
            <strong>📌 记忆锚点</strong>
            <span class="small muted" id="ms-anchor-count">
              ${(data.anchors?.count || 0)}/${(data.anchors?.max_items || 5)} ·
              ${(data.anchors?.chars || 0)}/${(data.anchors?.max_chars || 2000)} 字
            </span>
            <span class="spacer"></span>
            <button class="btn sec sm" id="btn-ms-anchor-save">保存锚点</button>
          </div>
          <div class="hint mb8">
            锚点是你亲手写下的<strong>硬设定</strong>（例如"主角是女性""绝不能承认自己是 AI"）：
            每轮都会<strong>整条</strong>注入系统提示词，既不会被回忆顶掉，也不会被剧情总结折叠。
            最多 ${(data.anchors?.max_items || 5)} 条、合计 ${(data.anchors?.max_chars || 2000)} 字。
          </div>
          <div id="ms-anchor-list"></div>
          <button class="btn sec sm mt8" id="btn-ms-anchor-add">+ 添加锚点</button>
        </div>
      </div>`;

    // ---------------- 记忆锚点（最多 5 条 / 2000 字）----------------
    const anchorList = $('#ms-anchor-list', m.root);
    const anchorItems = Array.isArray(data.anchors?.items) ? [...data.anchors.items] : [];
    const anchorMax = Number(data.anchors?.max_items || 5);
    const anchorMaxChars = Number(data.anchors?.max_chars || 2000);
    const anchorOneMax = Number(data.anchors?.max_one_chars || 500);

    function refreshAnchorCount() {
      const total = anchorItems.reduce((sum, item) => sum + (item || '').length, 0);
      const box = $('#ms-anchor-count', m.root);
      if (box) box.textContent = `${anchorItems.length}/${anchorMax} · ${total}/${anchorMaxChars} 字`;
      const add = $('#btn-ms-anchor-add', m.root);
      if (add) add.disabled = anchorItems.length >= anchorMax;
    }

    function renderAnchors() {
      anchorList.innerHTML = anchorItems
        .map(
          (item, index) => `
          <div class="row mt8" style="gap:8px;align-items:center">
            <input type="text" data-anchor="${index}" maxlength="${anchorOneMax}"
              value="${esc(item || '')}" placeholder="例如：主角是女性，名叫薇拉" style="flex:1" />
            <button class="btn link small danger-link" data-anchor-del="${index}">删除</button>
          </div>`,
        )
        .join('') || `<div class="small faint">还没有锚点。加一条就会每轮都带上它。</div>`;
      $$('[data-anchor]', anchorList).forEach((input) => {
        input.addEventListener('input', () => {
          anchorItems[Number(input.dataset.anchor)] = input.value;
          refreshAnchorCount();
        });
      });
      $$('[data-anchor-del]', anchorList).forEach((btn) => {
        btn.addEventListener('click', () => {
          anchorItems.splice(Number(btn.dataset.anchorDel), 1);
          renderAnchors();
          refreshAnchorCount();
        });
      });
      refreshAnchorCount();
    }
    renderAnchors();
    $('#btn-ms-anchor-add', m.root).addEventListener('click', () => {
      if (anchorItems.length >= anchorMax) {
        toastWarn(`最多只能加 ${anchorMax} 条锚点`);
        return;
      }
      anchorItems.push('');
      renderAnchors();
    });
    $('#btn-ms-anchor-save', m.root).addEventListener('click', async (e) => {
      const restore = buttonLoading(e.target, '保存中');
      try {
        // 空条目由后端清洗（去空白/去重），超限会被拒绝并说明原因
        const res = await api.put(`/narrative/sessions/${detail.id}/memory-summary/anchors`, {
          anchors: anchorItems.filter((item) => (item || '').trim()),
        });
        toastOk(res?.message ? res.message : '锚点已保存');
        await loadSummary();
        await openSession(root, detail.id, null);
      } catch (err) {
        toastErr(err.toDisplay ? err.toDisplay() : String(err));
      } finally {
        restore();
      }
    });

    const save = async (body, okMsg) => {
      const res = await api.patch(`/narrative/sessions/${detail.id}/memory-summary`, body);
      toastOk(res?.message ? res.message : okMsg);
      await loadSummary();
      await openSession(root, detail.id, null);
    };

    $('#btn-ms-save', m.root).addEventListener('click', async () => {
      try {
        await save(
          {
            enabled: $('[name=ms-enabled]', m.root).checked,
            auto: $('[name=ms-auto]', m.root).checked,
            remind: $('[name=ms-remind]', m.root).checked,
            rounds: Number($('[name=ms-rounds]', m.root).value) || 8,
            mode: $('[name=ms-mode]', m.root).value,
            max_chars: Number($('[name=ms-max-chars]', m.root).value) || 2000,
            provider_id: $('[name=ms-provider]', m.root).value || null,
            use_preset_prompt: $('[name=ms-preset-prompt]', m.root).checked,
            prompt: $('[name=ms-prompt]', m.root).value,
            content: $('[name=ms-content]', m.root).value,
          },
          '已保存',
        );
      } catch (err) {
        toastErr(err.toDisplay ? err.toDisplay() : String(err));
      }
    });
    $('[name=ms-mode]', m.root).addEventListener('change', (e) => {
      const hint = (data.modes || []).find((item) => item.value === e.target.value)?.hint || '';
      const box2 = $('#ms-mode-hint', m.root);
      if (box2) box2.textContent = hint;
    });
    $('#btn-ms-run', m.root).addEventListener('click', async () => {
      const btn = $('#btn-ms-run', m.root);
      // ★ 同样"先给反馈、再 await"：否则用户以为没点上 → 连点 → 重复总结（真实事故）
      const restore = buttonLoading(btn, '正在总结…');
      try {
        const res = await api.post(`/narrative/sessions/${detail.id}/memory-summary/run`, {});
        toastOk(res?.message ? res.message : '已总结');
        await loadSummary();
        await openSession(root, detail.id, null);
      } catch (err) {
        toastErr(err.toDisplay ? err.toDisplay() : String(err));
      } finally {
        restore();
      }
    });
    $('#btn-ms-restore', m.root).addEventListener('click', async () => {
      try {
        const res = await api.post(`/narrative/sessions/${detail.id}/memory-summary/restore`, {});
        toastOk(res?.message ? res.message : '已恢复');
        await loadSummary();
        await openSession(root, detail.id, null);
      } catch (err) {
        toastErr(err.toDisplay ? err.toDisplay() : String(err));
      }
    });
  }
  loadSummary();

  async function load(query = '') {
    const box = $('#mem-list', m.root);
    box.innerHTML = `<div class="empty" style="padding:20px"><div class="big">⏳</div><div>检索中…</div></div>`;
    try {
      const data = await api.get(`/narrative/sessions/${detail.id}/memories`, { q: query });
      if (!data.hits.length) {
        box.innerHTML = `<div class="empty" style="padding:20px">
          <div class="big">🧠</div>
          <div>${query ? '没有找到相关记忆' : '这个会话还没有记忆'}</div>
          <div class="small mt8">先聊几轮，记忆就会自动积累。</div>
        </div>`;
        return;
      }
      box.innerHTML = data.hits
        .map(
          (h) => `
        <div class="panel" style="padding:10px;margin-bottom:8px">
          <div class="small">
            <span class="badge ${h.kind === 'fact' ? 'info' : 'neutral'}">${
              h.kind === 'fact' ? '手动记住' : '对话'
            }</span>
            <span class="faint mono">相似度 ${(h.similarity * 100).toFixed(0)}%</span>
          </div>
          <div class="mt8 mem-text">${esc(h.text)}</div>
        </div>`,
        )
        .join('');
    } catch (err) {
      // ★ 记忆面板里的失败必须显示出来：这里的降级（悄悄跳过）会让用户
      //   以为"我什么都没记住"，从而反复重写设定
      box.innerHTML = `<div class="alert danger">${esc(err.toDisplay())}</div>`;
    }
  }

  $('#btn-mem-search', m.root).addEventListener('click', (e) => {
    load($('#mem-q', m.root).value.trim());
  });
  $('#mem-q', m.root).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') load($('#mem-q', m.root).value.trim());
  });

  $('#btn-mem-add', m.root).addEventListener('click', async (e) => {
    const input = $('#mem-new', m.root);
    const text = input.value.trim();
    if (!text) {
      toastErr('先写点要记住的内容');
      return;
    }
    const restore = buttonLoading(e.target, '保存中');
    try {
      await api.post(`/narrative/sessions/${detail.id}/memories`, { text });
      input.value = '';
      toastOk('已记住');
      await load(text);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });

  $('#btn-mem-clear', m.root).addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: '清空本会话的长期记忆',
      message:
        `只会让模型忘掉**这个会话**里的回忆，其它会话一条都不动。` +
        `\n\n对话记录不会被删除。此操作不可恢复。`,
      confirmText: '清空本会话',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/narrative/sessions/${detail.id}/memories`);
      toastOk('已清空本会话的长期记忆（对话记录不受影响）');
      await load();
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    }
  });

  // 次级入口：真的想一次清干净时用（多开几张卡的人很容易误伤，所以放在这里）
  $('#btn-mem-clear-all', m.root).addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: '清空全部会话的长期记忆',
      message:
        '<strong>所有会话</strong>的回忆都会被清掉（一开多张卡时请慎用）。' +
        '\n\n对话记录不会被删除。此操作不可恢复。',
      confirmText: '全部清空',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del('/narrative/memories');
      toastOk('已清空全部长期记忆（对话记录不受影响）');
      await load();
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    }
  });

  await load();
}

/* ==================================================================
   自动翻译中间件（跨语言对话）
   ★ 三种模式，成本差别很大，所以界面上必须写清楚：
       off        关闭
       prompt     只在系统提示词末尾加一句"请用 X 回复"—— 0 token，但模型可能不听话
       middleware 生成之后真的调一次模型翻译 —— 每轮多一次调用，原文/译文可切换
   ★ 默认关闭：本项目对"未经同意花用户 token"零容忍（与记忆总结同一条规矩）。
   ================================================================== */
async function openTranslateDialog(root, signal) {
  const detail = state.detail;
  if (!detail?.id) return;

  let data;
  try {
    data = await api.get(`/narrative/sessions/${detail.id}/translate`);
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
    return;
  }

  const m = modal({
    title: '翻译中间件（跨语言对话）',
    width: 'wide',
    bodyHTML: `<div id="tr-body"><div class="empty" style="padding:16px">加载中…</div></div>`,
    footHTML: `<button class="btn sec" data-close>关闭</button>
               <button class="btn" id="btn-save-translate">保存设置</button>`,
  });

  let panel = data;

  function render() {
    const s = panel.settings || {};
    const modeOptions = (panel.modes || [])
      .map(
        (item) =>
          `<option value="${esc(item.value)}" ${s.mode === item.value ? 'selected' : ''}>${esc(item.label)}</option>`,
      )
      .join('');
    const dirOptions = (panel.directions || [])
      .map(
        (item) =>
          `<option value="${esc(item.value)}" ${s.direction === item.value ? 'selected' : ''}>${esc(item.label)}</option>`,
      )
      .join('');
    const langOptions = (panel.lang_choices || [])
      .map((lang) => `<option value="${esc(lang)}" ${s.target_lang === lang ? 'selected' : ''}>${esc(lang)}</option>`)
      .join('');
    const providerOptions = [
      `<option value="">跟随会话模型</option>`,
      ...(panel.providers || []).map(
        (p) =>
          `<option value="${p.id}" ${Number(s.provider_id) === Number(p.id) ? 'selected' : ''}>${esc(p.name)} · ${esc(p.model_name)}</option>`,
      ),
    ].join('');
    const modeHint = (panel.modes || []).find((item) => item.value === s.mode)?.hint || '';
    const latest = panel.latest;
    $('#tr-body', m.root).innerHTML = `
      <div class="alert info">
        角色卡是外语、或者你想用中文聊外语卡时用它。<strong>默认关闭</strong>：
        <span class="mono">middleware</span> 模式每轮会**多调一次模型**（花钱），
        <span class="mono">prompt</span> 模式只写一句提示词（0 token，但模型可能不听话）。
        <br />★ 只需要选<strong>翻译成什么语言</strong>（默认简体中文）——
        <strong>源语言自动识别</strong>：英文、日文、韩文…都能认出来再译，不用你自己挑。
      </div>
      <label class="checkbox-line mb8">
        <input type="checkbox" id="tr-enabled" ${s.enabled ? 'checked' : ''} />
        <span>启用翻译中间件</span>
      </label>
      <div class="row">
        <div style="flex:2">
          <label class="small muted">模式</label>
          <select id="tr-mode">${modeOptions}</select>
          <div class="hint">${esc(modeHint)}</div>
        </div>
        <div style="flex:2">
          <label class="small muted">方向</label>
          <select id="tr-direction">${dirOptions}</select>
        </div>
      </div>
      <div class="row mt8">
        <div style="flex:1">
          <label class="small muted">翻译成什么语言</label>
          <select id="tr-target">${langOptions}</select>
          <div class="hint">源语言自动识别（英文 / 日文 / 韩文…），两个方向都用这个目标</div>
        </div>
        <div style="flex:2">
          <label class="small muted">用哪个模型翻译</label>
          <select id="tr-provider">${providerOptions}</select>
          <div class="hint">可以指定一个更便宜的模型专门做翻译（与"总结用哪个模型"同一套）。
            ★ 翻译是机械任务：本项目会把它的思考强度降到最小
            （若该模型不认这个参数，适配器会自动去掉并重试，不会让翻译失败），
            但预估只按原文字数粗算、<b>不含思考 token</b> —— 实测有模型译一次就烧掉几千 token。</div>
        </div>
      </div>
      <label class="checkbox-line mt8">
        <input type="checkbox" id="tr-keep" ${s.keep_original !== false ? 'checked' : ''} />
        <span>保留原文（消息下方可切换原文 / 译文）</span>
      </label>
      <div class="form-section mt14">
        <h3>现在的状态</h3>
        <div class="small muted">${esc(panel.summary || '')}</div>
        <div class="small faint mt8">
          已译消息 ${panel.translated_messages || 0} 条 ·
          翻译累计消耗 ${panel.spent_tokens || 0} token ·
          这一轮预估约 ${panel.cost_tokens || 0} token（粗估，不含思考 token）
          ${latest ? ` · 最近一次：${esc(latest.direction === 'input' ? '输入' : '回复')}→${esc(latest.lang || '')}${latest.used_model ? '' : '（未调用模型）'}` : ''}
        </div>
      </div>`;
  }

  function readForm() {
    return {
      enabled: Boolean($('#tr-enabled', m.root)?.checked),
      mode: $('#tr-mode', m.root)?.value || 'off',
      direction: $('#tr-direction', m.root)?.value || 'reply',
      target_lang: $('#tr-target', m.root)?.value || '简体中文',
      provider_id: $('#tr-provider', m.root)?.value ? Number($('#tr-provider', m.root).value) : null,
      keep_original: Boolean($('#tr-keep', m.root)?.checked),
    };
  }

  render();

  $('#btn-save-translate', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '保存中');
    try {
      panel = await api.patch(`/narrative/sessions/${detail.id}/translate`, readForm());
      render();
      toastOk(panel.summary ? `已保存：${panel.summary}` : '已保存');
      // 头部按钮的"开/关"状态与面板同步（不发第二次请求）
      if (state.detail) state.detail.translate = panel;
      const btn = $('#btn-translate', root);
      if (btn) {
        btn.classList.toggle('on', Boolean(panel.settings?.enabled));
        btn.textContent = `🌐 翻译${panel.settings?.enabled ? '开' : ''}`;
      }
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

async function openSettingsDialog(root, signal) {
  const detail = state.detail;
  if (!detail) return;

  let providers;
  let cards;
  let presets;
  try {
    [providers, cards, presets] = await Promise.all([
      api.get('/providers'),
      api.get('/character-cards', { scope: 'all', limit: 100 }),
      // ★ 预设列表接口返回的是**数组**（不是分页壳），所以这里统一成数组再挑默认项
      api
        .get('/prompt-presets')
        .then((res) => ({ items: Array.isArray(res) ? res : res?.items || [] }))
        .catch(() => ({ items: [] })),
    ]);
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
    return;
  }

  const boundId = detail.prompt_preset?.id ?? '';
  const globalDefault = (presets.items || []).find((p) => p.is_active);

  const m = modal({
    title: '会话设置',
    bodyHTML: `
      <div class="field">
        <label>角色卡</label>
        <select name="character_card_id">
          ${cards.items
            .map(
              (c) =>
                `<option value="${c.id}" ${c.id === detail.character_card?.id ? 'selected' : ''}>${esc(
                  c.name,
                )} #${c.id}</option>`,
            )
            .join('')}
        </select>
        <div class="hint">换卡只影响之后的对话，已经聊过的内容不会改写。</div>
      </div>
      <div class="field">
        <label>模型配置</label>
        <select name="llm_provider_id">
          ${providers
            .map(
              (p) =>
                `<option value="${p.id}" ${p.id === detail.provider?.id ? 'selected' : ''}>${esc(
                  p.name,
                )} · ${esc(p.model_name)}</option>`,
            )
            .join('')}
        </select>
        <div class="hint">不同模型对同一段剧情的人设把握差异很大，可以随时切换对比。</div>
      </div>
      <div class="field">
        <label>提示词预设（决定模型怎么工作：规则 / 破甲 / 输出格式）</label>
        <select name="prompt_preset_id">
          <option value="" ${boundId === '' ? 'selected' : ''}>
            ${globalDefault ? `不单独绑定（用全局默认：${esc(globalDefault.name)}）` : '不单独绑定（用系统内置装配）'}
          </option>
          ${(presets.items || [])
            .map(
              (p) =>
                `<option value="${p.id}" ${p.id === boundId ? 'selected' : ''}>${esc(p.name)}${
                  p.is_active ? '（全局默认）' : ''
                }</option>`,
            )
            .join('')}
        </select>
        <div class="hint">
          角色卡说明「这是谁」、世界书说明「世界有什么」，预设则规范
          <strong>模型该怎么表现</strong>（含深度注入的规则块）。
          当前实际生效：
          <strong>${
            detail.effective_preset
              ? `${esc(detail.effective_preset.name)}（${
                  detail.effective_preset.from === 'session' ? '本会话绑定' : '全局默认'
                }）`
              : '系统内置装配'
          }</strong>
          <br />
          另外<strong>永远叠加</strong>着一份内置守卫规则：
          ${
            detail.builtin_guard
              ? `<strong>${esc(detail.builtin_guard.name)}</strong>` +
                (detail.builtin_guard.min_reply_chars
                  ? `（要求每次回复不少于 ${detail.builtin_guard.min_reply_chars} 字）`
                  : '')
              : '<strong>已被删除（当前不生效）</strong>'
          }
          —— 可在「提示词预设」页修改或还原。
        </div>
      </div>
      ${
        detail.rolling_summary
          ? `<div class="field">
               <label>剧情总结（前情提要${
                 detail.summary_coverage
                   ? ` · 覆盖第 ${detail.summary_coverage.from_round}~${detail.summary_coverage.to_round} 轮`
                   : ''
               }）</label>
               <div class="hint mb8">
                 每积累若干轮自动合并一次：把「旧总结 + 新一块对话」重写成**一份**新总结，
                 旧总结被替换（所以同一段剧情只占一份 token）。被覆盖的对话不再逐条发送给模型。
               </div>
               <pre class="prompt-view">${esc(detail.rolling_summary)}</pre>
             </div>`
          : ''
      }`,
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-save-settings">保存</button>`,
  });

  $('#btn-save-settings', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '保存中');
    try {
      const values = formValues(m.root);
      await api.patch(`/narrative/sessions/${detail.id}`, {
        character_card_id: Number(values.character_card_id),
        llm_provider_id: Number(values.llm_provider_id),
        // ★ 空字符串的语义是"解绑"（后端用 model_fields_set 区分"没提交"与"提交了 null"），
        //   所以这里必须显式发 null，不能省略这个字段。
        prompt_preset_id: values.prompt_preset_id ? Number(values.prompt_preset_id) : null,
      });
      m.close();
      toastOk('设置已更新');
      await openSession(root, detail.id, signal);
      await refreshSessions(root, signal);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}
