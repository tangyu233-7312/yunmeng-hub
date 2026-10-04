/* ============================================================
   角色卡视图（我的卡库 / 公共卡库）

   包含用户明确要求的交互：删除角色卡时，
   先看后端返回的「牵连清单」，再用**默认勾选**的复选框决定
   要不要一并删除对话记录 / 世界书。
   ============================================================ */

import { api, session as authSession } from 'hne/api';
import {
  $,
  $$,
  bindFileDrop,
  buttonLoading,
  checkboxField,
  emptyState,
  esc,
  fmtDate,
  fmtRelative,
  freshViewSignal,
  initials,
  modal,
  mount,
  mountError,
  mountRich,
  selectField,
  textareaField,
  textField,
  toastErr,
  toastOk,
} from 'hne/ui';

/** 当前用户名：供开场白里的 `{{user}}` 宏替换（拿不到就留空，宏会被清掉） */
const currentUserName = () => authSession.user?.username || '';

const FIELDS = [
  ['name', '角色名'],
  ['description', '一句话简介'],
  ['personality', '性格特征'],
  ['background', '背景故事'],
  ['speaking_style', '说话风格'],
  ['scenario', '初始场景'],
  ['example_dialogue', '对话示例'],
  ['greeting', '开场白'],
  ['system_prompt', '自定义系统提示词'],
  ['post_history_instructions', '尾注指令'],
];

let currentState = { scope: 'mine', q: '', tag: '', sort: 'recent', page: 1, limit: 12 };

/** 当前这一批监听器的信号（由 freshViewSignal 管理，见 ui.js 的说明） */
let activeSignal = null;
/**
 * 已经绑过监听器的那个信号。
 *
 * ★ 用"记住了哪个信号"而不是布尔标志：布尔标志无法区分"绑的是哪一批"，
 *   上一批的监听器已经随信号失效、标志却还写着"绑过了"，
 *   新批次就永远绑不上 —— 表现是"这个页面的按钮全点不动"。
 *   （同类事故：docs/pitfalls.md 第 22 条，cards.js 与 books.js 各栽过一次。）
 */
let boundSignal = null;

/* ==================================================================
   列表
   ================================================================== */
export async function renderCards(root, opts = {}) {
  // ★ 每次调用都换一个新的监听器批次：本视图会在增删改之后重新渲染自己，
  //   不换的话委托监听器会叠加，一次点击弹 N 个窗（真实事故）。
  activeSignal = freshViewSignal('cards', opts.signal);
  boundSignal = null; // 新批次 → 允许重新绑一次
  await renderCardList(root, { ...opts, signal: activeSignal.signal });
}

/**
 * 列表渲染的**外壳**：只做一件事 —— 保证"渲染过程中出任何异常"都不会
 * 把页面留在「加载中…」（那是一个用户既看不懂、也走不出去的界面）。
 * 具体渲染见 renderCardListInner。
 */
async function renderCardList(root, opts = {}) {
  try {
    await renderCardListInner(root, opts);
  } catch (err) {
    const signal = activeSignal?.signal;
    if (signal?.aborted) return;
    console.error('[cards] 列表渲染失败', err);
    mountError(root, `角色卡页渲染失败：${err?.message || err}`, () =>
      renderCardList(root, { signal: activeSignal?.signal }),
    );
  }
}

/** 只重新拉列表并重画，**不重新绑监听器**（监听器委托在 root 上，一直在） */
async function renderCardListInner(root, opts = {}) {
  const bindSignal = activeSignal?.signal ?? opts.signal;
  if (!bindSignal) {
    // 理论上到不了这里（renderCards 一定会先建好 activeSignal）。
    // 真到了就明确报错，而不是绑一个永远不会触发的监听器 —— 后者极难排查。
    console.error('[cards] 没有可用的监听器信号，卡片操作会全部失效');
    return;
  }
  // 内部一律用归一化后的信号，避免"某个调用方漏传 signal"再次引发同类问题
  const signal = bindSignal;
  /**
   * ★★ "我还是当前这一批吗"——**动 DOM 之前必须先问这一句**。
   *
   *   病根：这个函数会被反复调用（筛选 / 翻页 / 保存后刷新），
   *   每次都是"先画『加载中…』→ 等接口 → 再画内容"。
   *   如果中途换了批次（用户切页 / 又点了一次筛选），旧的那次回调醒来时
   *   会把**新批次的画面**盖成"加载中…"，然后因为信号已 abort 直接 return ——
   *   于是整页永远停在"加载中"。判据只能是 activeSignal 本身。
   */
  const isCurrent = () => activeSignal?.signal === signal && !signal.aborted;

  if (opts.scope && opts.scope !== currentState.scope) {
    currentState = { ...currentState, scope: opts.scope, page: 1 };
  }
  const st = currentState;
  const isLibrary = st.scope === 'public';

  if (!isCurrent()) return;
  mount(root, `<div class="empty"><div class="big">⏳</div><div>加载中…</div></div>`);

  let page;
  try {
    page = await api.get(
      '/character-cards',
      {
        scope: st.scope,
        q: st.q,
        tag: st.tag,
        sort: st.sort,
        limit: st.limit,
        offset: (st.page - 1) * st.limit,
      },
      // ★ 列表接口必须很快返回；20 秒还没回来说明后端卡住了，
      //   宁可报错给用户一个"重试"按钮，也不要让他对着转圈等到放弃
      { timeoutMs: 20000 },
    );
  } catch (err) {
    if (!isCurrent()) return;
    mountError(root, err.toDisplay ? err.toDisplay() : String(err), () =>
      renderCardList(root, { signal: activeSignal?.signal }),
    );
    return;
  }
  if (!isCurrent()) return;

  const cardsHTML = page.items
    .map((c) => {
      const avatar = c.avatar_url
        ? `<img class="cc-avatar" src="${esc(c.avatar_url)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'cc-avatar',textContent:'${esc(initials(c.name))}'}))" />`
        : `<div class="cc-avatar">${esc(initials(c.name))}</div>`;

      const tags = (c.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join('');

      const badges = [
        c.is_public ? '<span class="badge info">公开</span>' : '<span class="badge neutral">私有</span>',
        c.is_owner ? '<span class="badge ok">我的</span>' : '<span class="badge warn">他人的</span>',
        c.session_count ? `<span class="badge neutral">${c.session_count} 个会话</span>` : '',
        c.world_book_name ? `<span class="badge neutral">📖 ${esc(c.world_book_name)}</span>` : '',
      ]
        .filter(Boolean)
        .join(' ');

      // 他人公开卡只能看和复制
      const ownerActions = c.is_owner
        ? `<button class="btn sm sec" data-act="edit">编辑</button>
           <button class="btn sm sec" data-act="dup">复制</button>
           <button class="btn sm sec" data-act="del" style="color:var(--danger)">删除</button>`
        : `<button class="btn sm sec" data-act="dup">复制到我名下</button>`;

      return `
      <div class="cc" data-id="${c.id}">
        <div class="cc-top">
          ${avatar}
          <div class="cc-headline">
            <div class="cc-name" title="${esc(c.name)}">${esc(c.name)}</div>
            <div class="cc-desc">${esc(c.description || '（没有简介）')}</div>
          </div>
        </div>
        <div class="cc-body">
          <div class="mb8">${badges}</div>
          <div>${tags || '<span class="faint small">无标签</span>'}</div>
          ${
            c.greeting_preview
              ? `<div class="cc-greeting">“${esc(c.greeting_preview)}”</div>`
              : '<div class="cc-greeting faint">（没有开场白 —— 建会话时会是一片空白）</div>'
          }
        </div>
        <div class="cc-foot">
          <button class="btn sm sec" data-act="view">详情</button>
          <button class="btn sm sec" data-act="export">导出</button>
          ${ownerActions}
          <span class="spacer"></span>
          <span class="faint small">${esc(fmtRelative(c.updated_at))}</span>
        </div>
      </div>`;
    })
    .join('');

  const totalPages = Math.max(1, Math.ceil(page.total / st.limit));

  mount(
    root,
    `
    <div class="page-head">
      <div>
        <h1>角色卡</h1>
        <div class="sub">
          ${
            isLibrary
              ? '其他用户公开分享的角色卡。你只能查看和复制 —— 复制一份到自己名下才能修改。'
              : '角色卡描述「AI 要扮演谁」，是叙事引擎的人设来源。支持导入 SillyTavern 的 PNG / JSON 角色卡。'
          }
        </div>
      </div>
      <div class="page-actions">
        ${
          isLibrary
            ? ''
            : `<button class="btn sec" id="btn-import-json">导入 JSON</button>
               <button class="btn sec" id="btn-import-png">导入 PNG</button>
               <button class="btn" id="btn-new">+ 新建角色卡</button>`
        }
      </div>
    </div>

    <div class="toolbar">
      <div class="segmented" id="scope-tabs">
        <button data-scope="mine" class="${st.scope === 'mine' ? 'active' : ''}">我的卡库</button>
        <button data-scope="public" class="${st.scope === 'public' ? 'active' : ''}">公共卡库</button>
        <button data-scope="all" class="${st.scope === 'all' ? 'active' : ''}">全部可见</button>
      </div>
      <div class="grow">
        <input type="text" id="f-q" placeholder="搜索名字或简介…" value="${esc(st.q)}" />
      </div>
      <input type="text" id="f-tag" placeholder="按标签筛选" value="${esc(st.tag)}" style="width:130px" />
      <select id="f-sort">
        <option value="recent" ${st.sort === 'recent' ? 'selected' : ''}>最近更新</option>
        <option value="created" ${st.sort === 'created' ? 'selected' : ''}>最近创建</option>
        <option value="name" ${st.sort === 'name' ? 'selected' : ''}>按名称</option>
      </select>
      <button class="btn sec sm" id="btn-reset">重置</button>
    </div>

    <div class="small muted mb8">
      共 <b>${page.total}</b> 张${st.q ? `（搜索「${esc(st.q)}」）` : ''}${st.tag ? `（标签「${esc(st.tag)}」）` : ''}
      · 第 ${st.page} / ${totalPages} 页
    </div>

    ${
      page.items.length
        ? `<div class="grid cards" data-view="cards">${cardsHTML}</div>`
        : isLibrary
          ? emptyState('📚', '公共卡库里还没有人公开角色卡', '去「我的角色卡」把某张卡设为公开，它就会出现在这里。')
          : emptyState('🎭', '还没有角色卡', '点右上角「新建角色卡」，或者直接导入一张 PNG 角色卡。')
    }

    ${
      totalPages > 1
        ? `<div class="center mt20">
             <button class="btn sec sm" id="pg-prev" ${st.page <= 1 ? 'disabled' : ''}>← 上一页</button>
             <span class="muted" style="margin:0 12px">${st.page} / ${totalPages}</span>
             <button class="btn sec sm" id="pg-next" ${st.page >= totalPages ? 'disabled' : ''}>下一页 →</button>
           </div>`
        : ''
    }`,
  );

  /* ---------------- 事件绑定 ----------------
     ★★ 这里必须分成**两类**来绑，混在一起就是"切一次卡库之后按钮全失效"的病根：

       1. 工具栏按钮 / 输入框（#scope-tabs、#f-q、#f-tag、#f-sort、#btn-reset、
          #pg-*、#btn-new、#btn-import-*）——它们**属于本次渲染出来的 DOM**，
          mount() 会把旧节点整个换掉，旧监听器跟着节点一起消失。
          所以：**每次渲染后都必须重新绑**。
          （以前这里用"这一批绑过就不再绑"的守卫，于是第二次渲染出来的按钮
            天生没有监听器：点一下「公共卡库」重新渲染之后，别的按钮全都点不动了
            —— 用户实际反馈过这个现象。）

       2. 挂在常驻 #view 上的事件委托（卡片上的 data-act 按钮）——
          节点不会被换掉，所以**每批只能绑一次**，重复绑会"点一下弹 N 个窗"。
   */
  if (bindSignal.aborted) return;
  bindToolbar(root, bindSignal);
  if (boundSignal !== bindSignal) {
    boundSignal = bindSignal;
    bindCardActions(root, bindSignal);
  }
}

/** 工具栏：属于本次渲染出来的节点，每次渲染后都要重绑 */
function bindToolbar(root, signal) {
  const st = currentState;

  $('#scope-tabs', root).addEventListener('click', (e) => {
    const b = e.target.closest('button[data-scope]');
    if (!b) return;
    // 「公共卡库」也是同一套渲染，只是 scope 不同（不分两条路径，少一个走岔的机会）
    renderCardList(root, { scope: b.dataset.scope, signal: activeSignal?.signal });
  });

  // 搜索/标签共用同一个防抖计时器：任何一个被清掉都重新计时
  let debounce;
  const debounced = (patch) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      currentState = { ...currentState, ...patch, page: 1 };
      renderCardList(root, { signal: activeSignal?.signal });
    }, 350);
  };
  $('#f-q', root).addEventListener('input', (e) => debounced({ q: e.target.value.trim() }));
  $('#f-tag', root).addEventListener('input', (e) => debounced({ tag: e.target.value.trim() }));
  $('#f-sort', root).addEventListener('change', (e) => {
    currentState = { ...currentState, sort: e.target.value, page: 1 };
    renderCardList(root, { signal: activeSignal?.signal });
  });
  $('#btn-reset', root).addEventListener('click', () => {
    currentState = { ...currentState, q: '', tag: '', sort: 'recent', page: 1 };
    renderCardList(root, { signal: activeSignal?.signal });
  });
  $('#pg-prev', root)?.addEventListener('click', () => {
    currentState = { ...currentState, page: Math.max(1, st.page - 1) };
    renderCardList(root, { signal: activeSignal?.signal });
  });
  $('#pg-next', root)?.addEventListener('click', () => {
    currentState = { ...currentState, page: st.page + 1 };
    renderCardList(root, { signal: activeSignal?.signal });
  });

  $('#btn-new', root)?.addEventListener('click', () => openCardForm(root, null));
  $('#btn-import-png', root)?.addEventListener('click', () => openPngImport(root));
  $('#btn-import-json', root)?.addEventListener('click', () => openJsonImport(root));
}

/** 卡片上的操作按钮：委托在常驻 #view 上，每批只绑一次 */
function bindCardActions(root, signal) {
  // ★ 必须带 { signal }：这个委托监听器挂在常驻的 #view 上，
  //   不随 innerHTML 替换而消失。否则离开本页后它仍然存活，
  //   会在别的页面（例如世界书）上被触发，拿着错误的 id 去请求角色卡接口。
  root.addEventListener(
    'click',
    async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      // ★ 归属校验（纵深防御）：角色卡 / 插件 / 预设 / 世界书的卡片都是 `.cc`，
      //   只认 class 分不出属于哪一页；列表容器各带 data-view，认不出来就不管。
      //   没有这一条时，别的页面残留的监听器会打进这里（见 presets.js 的事故注释）。
      if (!btn.closest('[data-view="cards"]')) return;
      const cardEl = btn.closest('.cc');
      if (!cardEl) return; // 不属于角色卡列表的按钮
      const id = Number(cardEl.dataset.id);
      // 判断"这一批还活着吗"要认**当前**批次：列表刷新后旧信号会被 abort
      const isStale = () => activeSignal?.signal.aborted ?? true;
      const restore = buttonLoading(btn);

      try {
        const act = btn.dataset.act;

        if (act === 'view') {
          const card = await api.get(`/character-cards/${id}`);
          if (isStale()) return;
          restore();
          openCardDetail(root, card);
        } else if (act === 'export') {
          const exported = await api.get(`/character-cards/${id}/export`);
          if (isStale()) return;
          downloadJson(exported.data, `${exported.data.name || 'character'}.json`);
          toastOk(`已导出 ${exported.data.name}.json（可直接导入 SillyTavern）`);
        } else if (act === 'dup') {
          const copy = await api.post(`/character-cards/${id}/duplicate`);
          if (isStale()) return;
          toastOk(`已复制到你的卡库：${copy.name}`);
          renderCardList(root, { signal: activeSignal.signal });
        } else if (act === 'edit') {
          const card = await api.get(`/character-cards/${id}`);
          if (isStale()) return;
          restore();
          openCardForm(root, card);
        } else if (act === 'del') {
          restore();
          await deleteCardFlow(root, id, activeSignal.signal);
        }
      } catch (err) {
        if (!isStale()) toastErr(err.toDisplay ? err.toDisplay() : String(err));
      } finally {
        restore();
      }
      },
      { signal },
    );
}

/* ==================================================================
   ★ 删除流程：这是用户要求的"勾选式删除"
   ================================================================== */
async function deleteCardFlow(root, id, signal) {
  // ---- 第一步：先不带 force 试一次 ----
  // 卡上没挂东西 -> 直接删掉；挂了东西 -> 后端返回 409 + 牵连清单
  let conflict = null;
  try {
    const summary = await api.del(`/character-cards/${id}`);
    if (signal?.aborted) return;
    toastOk(summaryMessage(summary));
    await renderCardList(root, { signal: activeSignal.signal });
    return;
  } catch (err) {
    if (signal?.aborted) return;
    if (err.status === 409 && err.detail) conflict = err.detail;
    else {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
      return;
    }
  }

  // ---- 第二步：把 409 里的牵连清单渲染成勾选项（默认全勾） ----
  const d = conflict;
  const hasSessions = (d.session_count || 0) > 0;
  const hasBook = Boolean(d.has_world_book);
  const sharedBy = d.world_book_shared_by_other_cards || 0;

  const bookNote = sharedBy
    ? `这本世界书还被<b>另外 ${sharedBy} 张角色卡</b>共用，即使勾选删除也会为它们保留。`
    : '这本世界书只被当前这张卡使用，勾选后会真正删除。';

  const m = modal({
    title: '删除角色卡',
    width: 'narrow',
    bodyHTML: `
      <div class="alert warn">
        这张角色卡还牵连着其它数据。请确认要一并处理哪些 —— 不勾选的会被<b>保留</b>。
      </div>

      <div class="opt-list">
        ${
          hasSessions
            ? `<label class="opt">
                 <input type="checkbox" id="opt-sessions" checked />
                 <span>
                   <span class="opt-title">一并删除 ${d.session_count} 个对话记录</span>
                   <div class="opt-desc">
                     会删除这些叙事会话及其中的全部消息，<b>不可恢复</b>。
                     取消勾选则故事保留下来，只是不再关联这张卡。
                   </div>
                 </span>
               </label>`
            : '<div class="opt disabled"><span class="opt-desc">这张卡没有任何对话记录。</span></div>'
        }

        ${
          hasBook
            ? `<label class="opt">
                 <input type="checkbox" id="opt-book" checked />
                 <span>
                   <span class="opt-title">一并删除世界书「${esc(d.world_book_name || '未命名')}」</span>
                   <div class="opt-desc">${bookNote}</div>
                 </span>
               </label>`
            : '<div class="opt disabled"><span class="opt-desc">这张卡没有关联世界书。</span></div>'
        }
      </div>

      <div class="mt14 small muted">
        角色卡本身一定会被删除。<br>
        ${hasSessions ? '' : ''}
        ${hasBook ? '世界书如果被别的卡共用，会为它们保留。' : ''}
      </div>`,
    footHTML: `
      <button class="btn sec" data-close>取消</button>
      <button class="btn danger" id="btn-confirm-del">确认删除</button>`,
  });

  $('#btn-confirm-del', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '删除中');
    try {
      const summary = await api.del(`/character-cards/${id}`, {
        force: true,
        delete_sessions: hasSessions ? $('#opt-sessions', m.root).checked : true,
        delete_world_book: hasBook ? $('#opt-book', m.root).checked : true,
      });
      m.close();
      if (signal?.aborted) return;
      toastOk(summaryMessage(summary));
      await renderCardList(root, { signal: activeSignal.signal });
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

/** 把删除摘要拼成一句人话，包括"想删但没删掉"的情况 */
function summaryMessage(s) {
  if (!s) return '角色卡已删除';
  const parts = ['角色卡已删除'];
  if (s.deleted_sessions) parts.push(`连同 ${s.deleted_sessions} 个对话记录`);
  if (s.deleted_world_book) parts.push('连同关联的世界书');
  let text = parts.join('，');
  if (s.world_book_kept) text += `\n注意：${s.world_book_kept_reason}`;
  return text;
}

/* ==================================================================
   备选开场白：一条一页的翻页编辑器
   ================================================================== */
/**
 * ★ 为什么不是"一行一条"的文本域（这是修掉的一个真实 bug）：
 *   一条备选开场白**本身就可能有很多行**（正文分段、状态栏、HTML 卡片都是多行的）。
 *   用换行拼起来再拆开，会把 1 条 3107 字、含 66 个换行的开场白变成 **67 条**，
 *   保存时撞上限直接 422 —— 用户只看到「保存修改失败」，完全猜不到原因。
 *   所以：一条占一个页面，翻页编辑，**永远不按换行拆分**。
 */
function altGreetingsField(list) {
  return `
    <div class="field">
      <label>备选开场白</label>
      <div class="alt-box" data-alt-box>
        <div class="alt-head">
          <button type="button" class="btn sec sm" data-alt="prev">← 上一条</button>
          <span class="alt-count">第 <b data-alt-pos>1</b> / <span data-alt-total>1</span> 条</span>
          <button type="button" class="btn sec sm" data-alt="next">下一条 →</button>
          <span class="alt-gap"></span>
          <button type="button" class="btn sec sm" data-alt="add">＋ 新增</button>
          <button type="button" class="btn sec sm" data-alt="dup">复制本条</button>
          <button type="button" class="btn sec sm" data-alt="del">删除本条</button>
        </div>
        <textarea data-alt-text rows="10"
          placeholder="这一条备选开场白的完整内容（可以有很多行、可以是 HTML）"></textarea>
        <div class="hint">
          一条 = 一个完整开场白，可以有很多行；用「上一条 / 下一条」翻页。
          空白的条目不会保存。
        </div>
      </div>
    </div>`;
}

/* ==================================================================
   VN 立绘（角色卡 VN 模式）
   ★ 表情是**状态栏里的一个字段**（不是新协议）：这里配"表情名 → 图片地址"，
     会话页的舞台按当前状态自动换图。后端会做同一套校验（甚至更严），
     前端这里只是让作者看得见、改得动。
   ================================================================== */
const VN_POSITIONS = [
  { value: 'left', label: '左侧' },
  { value: 'center', label: '居中' },
  { value: 'right', label: '右侧' },
];

function vnSpriteRow(item, index) {
  return `
    <div class="row" data-sprite="${index}" style="margin-bottom:6px">
      <div style="flex:2">
        <input type="text" data-vk="name" value="${esc(item?.name || '')}" placeholder="表情名，如 生气" />
      </div>
      <div style="flex:5">
        <input type="text" data-vk="url" value="${esc(item?.url || '')}"
               placeholder="https://… 或 data:image/png;base64,…" />
      </div>
      <div style="flex:0 0 auto">
        <button class="btn sm sec" type="button" data-remove-sprite="${index}">删</button>
      </div>
    </div>`;
}

/** 绑定立绘行编辑器，返回"读出全部立绘"的函数（保存时调用）。 */
function bindVnSprites(root) {
  const box = $('#vn-sprite-box', root);
  if (!box) return () => [];
  const bindRemove = () => {
    $$('[data-remove-sprite]', box).forEach((btn) => {
      btn.onclick = () => {
        btn.closest('[data-sprite]')?.remove();
      };
    });
  };
  const addBtn = $('#btn-add-sprite', root);
  if (addBtn) {
    addBtn.onclick = () => {
      box.insertAdjacentHTML('beforeend', vnSpriteRow({}, box.children.length));
      bindRemove();
    };
  }
  bindRemove();
  return () =>
    $$('[data-sprite]', box)
      .map((row) => ({
        name: row.querySelector('[data-vk=name]').value.trim(),
        url: row.querySelector('[data-vk=url]').value.trim(),
      }))
      .filter((item) => item.name && item.url);
}

/** 绑定翻页编辑器，返回"读出全部条目"的函数（保存时调用）。 */
function bindAltGreetings(root, initial) {  const box = $('[data-alt-box]', root);
  const text = $('[data-alt-text]', box);
  const items = (initial || []).slice();
  if (!items.length) items.push(''); // 至少留一个空槽，用户不用先点「新增」
  let index = 0;

  // 翻页前必须把当前页写回数组，否则改了一半就翻页 = 丢改动
  const flush = () => {
    items[index] = text.value;
  };
  const paint = () => {
    text.value = items[index] ?? '';
    $('[data-alt-pos]', box).textContent = String(index + 1);
    $('[data-alt-total]', box).textContent = String(items.length);
    $('[data-alt="prev"]', box).disabled = index === 0;
    $('[data-alt="next"]', box).disabled = index >= items.length - 1;
    $('[data-alt="del"]', box).disabled = items.length === 1 && !text.value;
  };
  const go = (target) => {
    flush();
    index = Math.max(0, Math.min(items.length - 1, target));
    paint();
  };

  box.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-alt]');
    if (!btn) return;
    const act = btn.dataset.alt;
    if (act === 'prev') go(index - 1);
    else if (act === 'next') go(index + 1);
    else if (act === 'add') {
      flush();
      items.splice(index + 1, 0, '');
      go(index + 1);
    } else if (act === 'dup') {
      flush();
      items.splice(index + 1, 0, items[index]);
      go(index + 1);
    } else if (act === 'del') {
      items.splice(index, 1);
      if (!items.length) items.push('');
      else if (index >= items.length) index = items.length - 1;
      paint();
    }
    if (!text.disabled) text.focus();
  });

  paint();
  return () => {
    flush();
    // 空槽不保存：用户点了几次「新增」又没写内容，不该变成空条目
    return items.map((s) => String(s ?? '').trim()).filter(Boolean);
  };
}

/* ==================================================================
   新建 / 编辑表单
   ================================================================== */
async function openCardForm(root, existing) {
  const isEdit = Boolean(existing);

  // 世界书下拉：需要先拿到列表
  let books = [];
  try {
    const page = await api.get('/world-books', { limit: 100 });
    books = page.items;
  } catch {
    /* 拿不到就不显示下拉，不阻塞建卡 */
  }

  const bookOptions = [
    { value: '', label: '（不关联世界书）' },
    ...books.map((b) => ({ value: String(b.id), label: `${b.display_name}（${b.entry_count} 条）` })),
  ];

  const tagsValue = (existing?.tags || []).join(', ');

  // ★ 状态栏格式：优先显示卡里显式声明的 state_schema；
  //   没有就显示 initial_state（作者只写了初始值，字段/类型由后端推断）。
  //   —— 用户的原话："状态栏应该在角色卡里读取"，所以这里必须看得见、改得动。
  const hneExt = existing?.extensions?.hne || {};
  const stateSchemaRaw = hneExt.state_schema ?? hneExt.initial_state ?? null;
  const stateSchemaValue = stateSchemaRaw ? JSON.stringify(stateSchemaRaw, null, 2) : '';

  // ★ VN 立绘：表单里要用到的当前值（读卡里声明的 extensions.hne.vn，缺项给默认）。
  //   sprites 两种写法（对象 / 数组）都收 —— 网上下载的卡两种都见过。
  const vnRaw = hneExt.vn || {};
  const vnSpritesRaw = vnRaw.sprites ?? vnRaw.expressions ?? {};
  const vnSpriteList = Array.isArray(vnSpritesRaw)
    ? vnSpritesRaw
        .map((item) =>
          Array.isArray(item)
            ? { name: String(item[0] ?? ''), url: String(item[1] ?? '') }
            : { name: String(item?.name ?? item?.expression ?? ''), url: String(item?.url ?? '') },
        )
        .filter((item) => item.name || item.url)
    : Object.entries(vnSpritesRaw).map(([name, url]) => ({ name, url: String(url ?? '') }));
  const vn = {
    enabled: Boolean(vnRaw.enabled ?? (vnSpriteList.length || vnRaw.background)),
    background: String(vnRaw.background ?? ''),
    field: String(vnRaw.expression_field ?? vnRaw.field ?? 'mood'),
    defaultExpression: String(vnRaw.default ?? ''),
    position: String(vnRaw.position ?? 'center'),
    showName: vnRaw.show_name !== false,
    sprites: vnSpriteList,
  };

  const m = modal({
    title: isEdit ? `编辑角色卡 · ${existing.name}` : '新建角色卡',
    width: 'wide',
    bodyHTML: `
      <div class="grid" style="grid-template-columns:1fr 1fr;gap:22px">
        <div>
          <h3 style="margin-top:0;font-size:13px;color:var(--text-dim)">基础信息</h3>
          ${textField('角色名', 'name', existing?.name || '', {
            placeholder: '如「爱丽丝」',
            hint: '只有这一项是必填的，其余可以之后再补',
          })}
          ${textField('头像地址', 'avatar_url', existing?.avatar_url || '', {
            placeholder: 'https://… 或留空',
          })}
          ${textareaField('一句话简介', 'description', existing?.description || '', { rows: 2 })}
          ${textField('标签', 'tags', tagsValue, {
            placeholder: '奇幻, 侦探',
            hint: '用英文逗号分隔；会自动去空白、去重，最多 20 个',
          })}
          ${selectField('关联世界书', 'world_book_id', bookOptions, existing?.world_book?.id ?? '', '一本世界书可以被多张角色卡共用')}
          ${checkboxField('公开到卡库（其他用户可查看并复制）', 'is_public', existing?.is_public ?? false)}
        </div>

        <div>
          <h3 style="margin-top:0;font-size:13px;color:var(--text-dim)">人设（会拼进系统提示词）</h3>
          ${textareaField('性格特征', 'personality', existing?.personality || '', { rows: 3 })}
          ${textareaField('背景故事', 'background', existing?.background || '', { rows: 3 })}
          ${textareaField('说话风格', 'speaking_style', existing?.speaking_style || '', { rows: 2 })}
          ${textareaField('初始场景', 'scenario', existing?.scenario || '', { rows: 2 })}
        </div>
      </div>

      <div class="form-section">
        <h3>开场白（★ 故事从这里开始）</h3>
        ${textareaField('开场白', 'greeting', existing?.greeting || '', {
          rows: 4,
          placeholder: '（她抬起头看了你一眼）……你来了。',
          hint: '建会话时会作为角色的第一条消息。留空的话用户建完会话会是一片空白，得自己先说话。',
        })}
        ${altGreetingsField(existing?.alternate_greetings || [])}
      </div>

      <div class="form-section">
        <h3>高级：提示词覆盖</h3>
        ${textareaField('自定义系统提示词', 'system_prompt', existing?.system_prompt || '', {
          rows: 3,
          hint: '留空则使用引擎按上面的人设字段自动拼装的那一份。★ 只要填了就会整体替换它：人设字段不再拼进去，引擎内置的扮演规则也一起消失（真实踩过：卡里只写一句"用英文回复"，结果模型跟着用户的语言走）。不确定就留空。',
        })}
        ${textareaField('尾注指令', 'post_history_instructions', existing?.post_history_instructions || '', {
          rows: 3,
          hint: '追加在对话历史之后（位置最靠后、权重最高），适合"必须照做"的硬要求，比如输出语言、输出格式。★ 绑了预设的会话由预设决定尾注位置，这里可能不生效。',
        })}
      </div>

      <div class="form-section">
        <h3>★ 状态栏格式（每张卡自己定义）</h3>
        <div class="hint mb8">
          状态栏**不属于代码，属于这张卡**：这里写这张卡要显示哪些字段，
          模型每轮就按这些字段输出 <code>&lt;state&gt;</code> 块，会话页的状态栏也照它渲染。
          留空 = 这张卡**没有状态栏**（不会硬塞 HP 那一套）。<br />
          两种写法：① 直接写字段清单（推荐）：
          <code>[{"name":"魔力","label":"魔力","type":"number"}]</code>；
          ② 只写初始值：<code>{"魔力": 80, "携带物": ["魔杖"]}</code>
          （字段与类型会自动推断）。<br />
          type 取 <code>text / number / meter / list / tuples / flags</code>；
          meter（血条那类）写 <code>{"name":"灯油","type":"meter","max_field":"灯油上限"}</code>。<br />
          也可以把格式写进**世界书**：加一条名字以 <code>[状态栏]</code> 开头的条目，
          正文里放一段 <code>&lt;state&gt;{…}&lt;/state&gt;</code> 示例即可。
        </div>
        ${textareaField('状态栏定义（JSON，留空 = 不显示状态栏）', 'state_schema_json', stateSchemaValue, {
          rows: 6,
          placeholder:
            '[{"name":"魔力","label":"魔力","type":"meter","max_field":"上限","icon":"✨"},\n {"name":"携带物","label":"携带物","type":"list","icon":"🎒"}]',
          hint: '这里只影响这张卡的状态栏；会话页的「纠正」仍然会按同一套规则校验',
        })}
      </div>

      <div class="form-section">
        <h3>🎭 VN 立绘（背景 + 表情立绘，可选）</h3>
        <div class="hint mb8">
          ★ **表情不是新协议**：它就是状态栏里的一个字段（默认 <code>mood</code>）。
          上面「状态栏格式」里没写这个字段也没关系 —— 建会话时会自动补一个文本字段，
          并把这里的表情名写成**可选值**告诉模型。<br />
          图片地址支持 <code>https://…</code> 或内嵌 <code>data:image/png;base64,…</code>
          （<b>不支持 SVG</b>：它可能带脚本）。写坏的地址会被丢掉并在会话里如实提示，
          不会让整张卡不能用。
        </div>
        ${checkboxField('这张卡启用 VN 舞台（对话页会出现 🎭 开关）', 'vn_enabled', vn.enabled)}
        <div class="row">
          <div style="flex:3">
            ${textField('背景图地址', 'vn_background', vn.background, {
              placeholder: 'https://… 或 data:image/png;base64,…',
            })}
          </div>
          <div style="flex:1">
            ${textField('表情字段名', 'vn_field', vn.field, {
              hint: '状态栏里哪个字段决定换哪张图',
            })}
          </div>
        </div>
        <div class="row">
          <div style="flex:1">
            ${textField('默认表情', 'vn_default', vn.defaultExpression, {
              hint: '状态里没有该字段/对不上时用这张',
            })}
          </div>
          <div style="flex:1">
            ${selectField(
              '立绘位置',
              'vn_position',
              VN_POSITIONS,
              vn.position,
              '立绘靠在舞台的哪一侧',
            )}
          </div>
          <div style="flex:1">
            ${checkboxField('显示名牌', 'vn_show_name', vn.showName)}
          </div>
        </div>
        <div class="mt8">
          <label class="small muted">立绘（表情名 → 图片地址）</label>
          <div id="vn-sprite-box">
            ${vn.sprites.map((item, index) => vnSpriteRow(item, index)).join('')}
          </div>
          <button class="btn sec sm" type="button" id="btn-add-sprite">+ 添加表情</button>
          <div class="hint mt8">
            表情名要与模型写进状态栏的词一致（例如「生气」）。
            模型写了一个没有立绘的词时，舞台上会退回默认表情，并如实说明。
          </div>
        </div>
      </div>

      <div class="form-section">
        <h3>对话示例（few-shot）</h3>
        ${textareaField('对话示例', 'example_dialogue', existing?.example_dialogue || '', {
          rows: 5,
          placeholder: '<START>\n{{user}}: 你好\n{{char}}: 你好呀',
          hint: '给模型看几轮范例，能明显稳定输出风格',
        })}
      </div>`,
    footHTML: `
      <button class="btn sec" data-close>取消</button>
      <button class="btn" id="btn-save">${isEdit ? '保存修改' : '创建角色卡'}</button>`,
  });

  const readAltGreetings = bindAltGreetings(m.root, existing?.alternate_greetings || []);
  const readVnSprites = bindVnSprites(m.root);

  $('#btn-save', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '保存中');
    try {
      const v = {};
      for (const node of $$('[name]', m.root)) {
        v[node.getAttribute('name')] = node.type === 'checkbox' ? node.checked : node.value;
      }

      const body = {};
      for (const [key] of FIELDS) {
        if (key === 'name') body.name = v.name.trim();
        else body[key] = v[key]?.trim() || null;
      }
      body.avatar_url = v.avatar_url.trim() || null;
      body.tags = v.tags
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
      // ★ 一条一个槽位，**绝不再按换行拆**：换行是开场白正文的一部分。
      //   （旧实现 join('\n') / split('\n')，把 1 条多行开场白变成 67 条 → 422）
      body.alternate_greetings = readAltGreetings();
      body.is_public = v.is_public;
      body.world_book_id = v.world_book_id ? Number(v.world_book_id) : null;

      // ★ 状态栏格式：写进 extensions.hne（V2 规范的自定义命名空间）。
      //   沿用卡里已有的其它 extensions 键（background / speaking_style 等），
      //   不能因为改一次状态栏就把它们抹掉。
      const ext = JSON.parse(JSON.stringify(existing?.extensions || {}));
      // ★ VN 立绘：写进 extensions.hne.vn（与 state_schema 同一个命名空间）。
      //   没启用且没配任何东西时**删掉这个键**，不要留一个空壳在卡里。
      const vnSprites = readVnSprites();
      const vnEnabled = Boolean(v.vn_enabled);
      const vnBackground = (v.vn_background || '').trim();
      const vnAny = vnEnabled || vnBackground || vnSprites.length;
      if (!vnAny) {
        if (ext.hne) {
          delete ext.hne.vn;
          if (!Object.keys(ext.hne).length) delete ext.hne;
        }
      } else {
        const hne = { ...(ext.hne || {}) };
        const sprites = {};
        vnSprites.forEach((item) => {
          sprites[item.name] = item.url;
        });
        hne.vn = {
          enabled: vnEnabled,
          background: vnBackground,
          sprites,
          expression_field: (v.vn_field || 'mood').trim() || 'mood',
          default: (v.vn_default || '').trim(),
          position: v.vn_position || 'center',
          show_name: Boolean(v.vn_show_name),
        };
        ext.hne = hne;
      }
      const rawSchema = (v.state_schema_json || '').trim();
      if (!rawSchema) {
        if (ext.hne) {
          delete ext.hne.state_schema;
          delete ext.hne.initial_state;
          if (!Object.keys(ext.hne).length) delete ext.hne;
        }
      } else {
        let parsed;
        try {
          parsed = JSON.parse(rawSchema);
        } catch (err) {
          toastErr(`状态栏定义的 JSON 格式不对：${err.message}`);
          return;
        }
        const hne = { ...(ext.hne || {}) };
        // 数组（或 {fields:[…]}）= 字段清单 → state_schema；
        // 纯对象 = 初始值 → initial_state（字段与类型由后端按值推断）。
        const looksLikeFields =
          Array.isArray(parsed) ||
          (parsed && typeof parsed === 'object' && Array.isArray(parsed.fields));
        if (looksLikeFields) {
          delete hne.initial_state;
          hne.state_schema = Array.isArray(parsed) ? parsed : parsed.fields;
        } else if (parsed && typeof parsed === 'object') {
          delete hne.state_schema;
          hne.initial_state = parsed;
        } else {
          toastErr('状态栏定义必须是 JSON 数组（字段清单）或对象（初始值）');
          return;
        }
        ext.hne = hne;
      }
      body.extensions = Object.keys(ext).length ? ext : null;

      if (isEdit) {
        await api.patch(`/character-cards/${existing.id}`, body);
        toastOk('角色卡已更新');
      } else {
        await api.post('/character-cards', body);
        toastOk('角色卡已创建');
      }
      m.close();
      await renderCardList(root, { signal: activeSignal.signal });
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

/* ==================================================================
   详情
   ================================================================== */
function openCardDetail(root, card) {
  const row = (label, value) =>
    value
      ? `<tr><td style="width:26%;color:var(--text-dim)">${esc(label)}</td>
             <td style="white-space:pre-wrap">${esc(value)}</td></tr>`
      : '';

  // ★ 开场白/备选开场白可能是 HTML 卡片（"状态栏 / 属性面板"，非常常见）。
  //   这里只放一个空槽，真正的内容交给 ui.js::mountRich 挂载 ——
  //   它与对话窗口共用同一套渲染，不会再出现"详情页能渲染、对话里不能"。
  const greetingRow = card.greeting
    ? `<tr>
         <td style="width:26%;color:var(--text-dim)">开场白</td>
         <td><div data-rich="greeting"></div></td>
       </tr>`
    : '';

  const altGreetings = card.alternate_greetings || [];
  const altRow = altGreetings.length
    ? `<tr>
         <td style="width:26%;color:var(--text-dim)">备选开场白</td>
         <td>
           <div class="alt-head">
             <button class="btn sec sm" data-alt-view="prev">← 上一条</button>
             <span class="alt-count">第 <b data-alt-view-pos>1</b> / ${altGreetings.length} 条</span>
             <button class="btn sec sm" data-alt-view="next">下一条 →</button>
           </div>
           <div data-rich="alt"></div>
         </td>
       </tr>`
    : '';

  const m = modal({
    title: card.name,
    width: 'wide',
    bodyHTML: `
      <div class="mb14">
        ${card.is_public ? '<span class="badge info">公开</span>' : '<span class="badge neutral">私有</span>'}
        ${card.is_owner ? '<span class="badge ok">我的</span>' : '<span class="badge warn">他人的（只读）</span>'}
        <span class="badge neutral">${card.session_count} 个会话</span>
        ${
          card.world_book
            ? `<span class="badge neutral">📖 ${esc(card.world_book.display_name)}（${card.world_book.entry_count} 条）</span>`
            : ''
        }
      </div>
      <div class="mb14">${(card.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join('') || '<span class="faint small">无标签</span>'}</div>

      <table class="list" style="background:transparent">
        ${row('简介', card.description)}
        ${row('性格', card.personality)}
        ${row('背景', card.background)}
        ${row('说话风格', card.speaking_style)}
        ${row('初始场景', card.scenario)}
        ${greetingRow}
        ${altRow}
        ${row('自定义系统提示词', card.system_prompt)}
        ${row('尾注指令', card.post_history_instructions)}
        ${row('对话示例', card.example_dialogue)}
      </table>

      <div class="form-section small faint">
        创建于 ${esc(fmtDate(card.created_at))} · 更新于 ${esc(fmtDate(card.updated_at))}
      </div>`,
    footHTML: `
      <button class="btn sec" data-close>关闭</button>
      ${
        card.is_owner
          ? '<button class="btn" id="btn-detail-edit">编辑</button>'
          : '<button class="btn" id="btn-detail-dup">复制到我名下</button>'
      }`,
  });

  // 关闭当前弹窗后再做下一步（直接持有 modal 引用，比去 DOM 里找更可靠）
  const then = (fn) => {
    m.close();
    fn();
  };

  // 开场白：渲染 HTML 卡片（{{char}} 换成卡名，{{user}} 留空由用户自己认）
  mountRich($('[data-rich="greeting"]', m.root), card.greeting || '', {
    char: card.name,
    user: currentUserName(),
  });

  // 备选开场白：一条一页 —— 一条本身就可能有上千字/几十行，
  // 全展开既没法看，也看不出"这是第几条"。
  if (altGreetings.length) {
    let altIndex = 0;
    const paintAlt = () => {
      $('[data-alt-view-pos]', m.root).textContent = String(altIndex + 1);
      mountRich($('[data-rich="alt"]', m.root), altGreetings[altIndex], {
        char: card.name,
        user: currentUserName(),
      });
    };
    $$('[data-alt-view]', m.root).forEach((btn) => {
      btn.addEventListener('click', () => {
        const next = altIndex + (btn.dataset.altView === 'next' ? 1 : -1);
        if (next < 0 || next >= altGreetings.length) return;
        altIndex = next;
        paintAlt();
      });
    });
    paintAlt();
  }

  $('#btn-detail-edit', m.root)?.addEventListener('click', () =>
    then(() => openCardForm(root, card)),
  );

  $('#btn-detail-dup', m.root)?.addEventListener('click', () =>
    then(async () => {
      try {
        const copy = await api.post(`/character-cards/${card.id}/duplicate`);
        toastOk(`已复制到你的卡库：${copy.name}`);
        await renderCardList(root, { signal: activeSignal.signal });
      } catch (err) {
        toastErr(err.toDisplay ? err.toDisplay() : String(err));
      }
    }),
  );
}

/* ==================================================================
   导入：PNG / JSON
   ================================================================== */
function openPngImport(root) {
  const m = modal({
    title: '导入角色卡 PNG',
    bodyHTML: `
      <div class="alert info">
        SillyTavern 生态里角色卡主要以 <b>PNG 图片</b>传播 —— 卡片数据藏在图片的文本块里，
        直接选文件即可，会自动识别卡片数据（名字 / 开场白 / 世界书）。
        ★ 导入<b>只取卡数据，图片本身不入库</b>：想让这张图当 VN 立绘或背景，
        要导入后在卡编辑器里把图片地址填进 🎭 VN 立绘（或头像地址）。
      </div>

      <div id="drop-zone" style="border:2px dashed var(--border-strong);border-radius:10px;
           padding:34px;text-align:center;color:var(--text-dim);cursor:pointer;background:var(--surface-2)">
        <div style="font-size:28px">🖼</div>
        <div class="mt8">把 PNG 拖到这里，或点击选择文件</div>
        <input type="file" id="png-file" accept="image/png,.png" class="hidden" />
      </div>

      <div class="form-section">
        ${textField('覆盖卡名（可选）', 'name_override', '', { placeholder: '留空则用卡片自带的名字' })}
        ${checkboxField('导入后直接公开', 'is_public', false)}
      </div>
      <div id="png-result" class="mt8"></div>`,
    footHTML: '<button class="btn sec" data-close>关闭</button>',
  });

  const dropZone = $('#drop-zone', m.root);
  const fileInput = $('#png-file', m.root);

  dropZone.addEventListener('click', () => fileInput.click());
  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.style.borderColor = 'var(--brand)';
    dropZone.style.background = 'var(--brand-soft)';
  });
  dropZone.addEventListener('dragleave', () => {
    dropZone.style.borderColor = 'var(--border-strong)';
    dropZone.style.background = 'var(--surface-2)';
  });
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.style.borderColor = 'var(--border-strong)';
    dropZone.style.background = 'var(--surface-2)';
    if (e.dataTransfer.files[0]) doUpload(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) doUpload(fileInput.files[0]);
  });

  async function doUpload(file) {
    const box = $('#png-result', m.root);
    box.innerHTML = '<div class="alert info">上传解析中…</div>';
    try {
      const form = new FormData();
      form.append('file', file);
      const override = $('[name=name_override]', m.root).value.trim();
      if (override) form.append('name_override', override);
      if ($('[name=is_public]', m.root).checked) form.append('is_public', 'true');

      const card = await api.postForm('/character-cards/import-png', form);
      box.innerHTML = `
        <div class="alert ok">
          ✓ 导入成功：<b>${esc(card.name)}</b>
          ${card.world_book ? `<br>同时提取出一本世界书：${esc(card.world_book.display_name)}（${card.world_book.entry_count} 条）` : ''}
          ${card.greeting ? '<br>包含开场白 ✓' : ''}
        </div>`;
      toastOk(`已导入「${card.name}」`);
      setTimeout(() => {
        m.close();
        renderCardList(root, { signal: activeSignal.signal });
      }, 1200);
    } catch (err) {
      box.innerHTML = `<div class="alert danger">${esc(err.toDisplay ? err.toDisplay() : String(err))}</div>`;
    }
  }
}

function openJsonImport(root) {
  const m = modal({
    title: '导入角色卡 JSON',
    bodyHTML: `
      <div class="alert info">
        支持 Character Card V2（<span class="mono">{spec, data}</span>）与 V1 扁平格式。
        <b>把 .json 文件拖进来（或点下面的方框选文件）</b>，也可以直接把 JSON 粘贴进来。
      </div>

      <!-- ★ 用户反馈过：只能粘贴、不能选文件，而"我的卡都在硬盘上" —— 补上拖拽与文件选择。
           读到的内容会填进下面的文本框，先让人看一眼再点「导入」，不做"选完就偷偷提交"。 -->
      <div id="json-drop" style="border:2px dashed var(--border-strong);border-radius:10px;
           padding:18px;text-align:center;color:var(--text-dim);cursor:pointer;background:var(--surface-2)">
        <div style="font-size:24px">📄</div>
        <div class="mt8">把 <b>.json</b> 角色卡拖到这里，或点击选择电脑里的文件</div>
      </div>

      ${textareaField('角色卡 JSON', 'card_json', '', {
        rows: 12,
        placeholder: '{\n  "spec": "chara_card_v2",\n  "spec_version": "2.0",\n  "data": { "name": "爱丽丝", "first_mes": "你好，旅人。" }\n}',
      })}
      ${textField('覆盖卡名（可选）', 'name_override', '')}
      ${checkboxField('导入后直接公开', 'is_public', false)}
      <div id="json-result" class="mt8"></div>`,
    footHTML: `
      <button class="btn sec" data-close>关闭</button>
      <button class="btn" id="btn-json-import">导入</button>`,
  });

  // 拖拽 / 选文件：只把内容读进文本框 + 顺手做一次语法体检，导入动作仍由用户按按钮触发
  bindFileDrop($('#json-drop', m.root), {
    accept: '.json,application/json',
    onText: (text, file) => {
      $('[name=card_json]', m.root).value = text;
      const box = $('#json-result', m.root);
      try {
        const parsed = JSON.parse(text);
        const name = parsed?.data?.name || parsed?.name || '';
        box.innerHTML = `<div class="alert ok">✓ 已读入 ${esc(file?.name || '文件')}${
          name ? `：<b>${esc(name)}</b>` : ''
        }　确认无误后点「导入」。</div>`;
      } catch (err) {
        box.innerHTML = `<div class="alert danger">文件读进来了，但 JSON 语法有误：
          ${esc(err.message)}</div>`;
      }
    },
  });

  $('#btn-json-import', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '导入中');
    const box = $('#json-result', m.root);
    try {
      const raw = $('[name=card_json]', m.root).value.trim();
      if (!raw) throw new Error('请先粘贴 JSON');
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (err) {
        throw new Error(`JSON 语法错误：${err.message}`);
      }
      const body = { card: parsed };
      const override = $('[name=name_override]', m.root).value.trim();
      if (override) body.name_override = override;
      body.is_public = $('[name=is_public]', m.root).checked;

      const card = await api.post('/character-cards/import', body);
      box.innerHTML = `<div class="alert ok">✓ 导入成功：<b>${esc(card.name)}</b>${
        card.world_book ? `<br>同时建立世界书：${esc(card.world_book.display_name)}` : ''
      }</div>`;
      toastOk(`已导入「${card.name}」`);
      setTimeout(() => {
        m.close();
        renderCardList(root, { signal: activeSignal.signal });
      }, 1000);
    } catch (err) {
      box.innerHTML = `<div class="alert danger">${esc(err.toDisplay ? err.toDisplay() : String(err))}</div>`;
    } finally {
      restore();
    }
  });
}

/* ---------------- 下载 JSON 文件 ---------------- */
function downloadJson(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
