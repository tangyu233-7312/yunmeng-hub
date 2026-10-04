/* ============================================================
   世界书视图（世界观设定集 / lorebook）

   世界书 = 一组「关键词 → 设定文本」条目，可被多张角色卡共用。
   这里提供一个条目编辑器，并且**保留我们不认识的字段**
   （规范里那些 position / selective / priority 等），
   否则用户把卡导回 SillyTavern 时这些设置就丢了。
   ============================================================ */

import { api } from 'hne/api';
import {
  $,
  buttonLoading,
  emptyState,
  esc,
  fmtRelative,
  freshViewSignal,
  modal,
  mount,
  mountError,
  textField,
  toastErr,
  toastOk,
} from 'hne/ui';

const state = { q: '', page: 1, limit: 12 };

/** 当前这一批监听器的信号（由 freshViewSignal 管理，见 ui.js 的说明） */
let activeSignal = null;

/**
 * 已经绑过监听器的那个信号。
 *
 * ★ 为什么用"记住了哪个信号"而不是一个布尔标志？
 *   布尔标志无法区分"绑的是哪一批"：上一批的监听器已经随信号失效了，
 *   但标志还写着"绑过了"，于是新的批次永远绑不上 ——
 *   表现就是"这个页面的按钮全部点不动"。
 *   （同类事故：docs/pitfalls.md 第 22 条。）
 */
let boundSignal = null;


/* ==================================================================
   列表
   ================================================================== */
export async function renderBooks(root, ctx = {}) {
  // ★ 每次调用都换一个新的监听器批次：本视图会在增删改之后重新渲染自己，
  //   不换的话委托监听器会叠加，一次点击弹 N 个窗（真实事故）。
  activeSignal = freshViewSignal('books', ctx.signal);
  boundSignal = null; // 新批次 → 允许重新绑一次
  await renderBookList(root, activeSignal.signal);
}

/** 只重新拉列表并重画，**不重新绑监听器**（监听器委托在 root 上，一直在） */
/**
 * 列表渲染的外壳：只保证一件事 —— 渲染过程中出任何异常都不会把页面
 * 留在「加载中…」上（用户反馈过：世界书页点开编辑、不保存直接关掉，
 * 整页就永远停在"加载中"，既没有报错也没有出口）。
 */
async function renderBookList(root, signal) {
  try {
    await renderBookListInner(root, signal);
  } catch (err) {
    const cur = activeSignal?.signal;
    if (cur?.aborted) return;
    console.error('[books] 列表渲染失败', err);
    mountError(root, `世界书页渲染失败：${err?.message || err}`, () =>
      renderBookList(root, activeSignal?.signal),
    );
  }
}

async function renderBookListInner(root, signal) {
  // ★ 一律用**当前批次**的信号绑监听器（绝不用调用方传进来的旧信号：
  //   旧信号可能已经 abort，监听器会被绑成"一诞生就是死的"，
  //   表现就是"这个页面的按钮全都点不动"，见 docs/pitfalls.md 第 22 条）。
  const bindSignal = activeSignal?.signal ?? signal;
  if (!bindSignal) {
    console.error('[books] 没有可用的监听器信号，本页按钮会全部失效');
    return;
  }
  /** ★ 动 DOM 之前先确认"我还是当前这一批"，否则会覆盖新批次的画面 */
  const isCurrent = () => activeSignal?.signal === signal && !signal?.aborted;
  if (!isCurrent()) return;
  mount(root, `<div class="empty"><div class="big">⏳</div><div>加载中…</div></div>`);

  let page;
  try {
    page = await api.get(
      '/world-books',
      {
        q: state.q,
        limit: state.limit,
        offset: (state.page - 1) * state.limit,
      },
      // ★ 20 秒没响应就报错给"重试"按钮，不让页面永远转圈
      { timeoutMs: 20000 },
    );
  } catch (err) {
    if (!isCurrent()) return;
    mountError(root, err.toDisplay ? err.toDisplay() : String(err), () =>
      renderBookList(root, activeSignal?.signal),
    );
    return;
  }
  if (!isCurrent()) return;

  const cards = page.items
    .map(
      (b) => `
      <div class="cc" data-id="${b.id}">
        <div class="cc-top">
          <div class="cc-avatar" style="background:var(--warn-soft);color:var(--warn)">📖</div>
          <div class="cc-headline">
            <div class="cc-name" title="${esc(b.name || '')}">${esc(b.display_name)}</div>
            <div class="cc-desc">${esc(b.description || '（没有简介）')}</div>
          </div>
        </div>
        <div class="cc-body">
          <div class="mb8">
            <span class="badge neutral">${b.entry_count} 条设定</span>
            ${b.enabled_entry_count !== b.entry_count ? `<span class="badge warn">${b.enabled_entry_count} 条启用</span>` : ''}
            <span class="badge ${b.card_count ? 'info' : 'neutral'}">${b.card_count} 张卡在用</span>
          </div>
        </div>
        <div class="cc-foot">
          <button class="btn sm sec" data-act="view">查看 / 编辑</button>
          <button class="btn sm sec" data-act="del" style="color:var(--danger)">删除</button>
          <span class="spacer"></span>
          <span class="faint small">${esc(fmtRelative(b.updated_at))}</span>
        </div>
      </div>`,
    )
    .join('');

  const totalPages = Math.max(1, Math.ceil(page.total / state.limit));

  mount(
    root,
    `
    <div class="page-head">
      <div>
        <h1>世界书</h1>
        <div class="sub">
          描述「这个故事发生在什么样的世界里」。一本世界书可以被多张角色卡共用，
          删角色卡时也可以选择保留它。
        </div>
      </div>
      <div class="page-actions">
        <button class="btn" id="btn-new">+ 新建世界书</button>
      </div>
    </div>

    <div class="toolbar">
      <div class="grow">
        <input type="text" id="f-q" placeholder="搜索名称或简介…" value="${esc(state.q)}" />
      </div>
      <button class="btn sec sm" id="btn-reset">重置</button>
    </div>

    <div class="small muted mb8">共 <b>${page.total}</b> 本 · 第 ${state.page} / ${totalPages} 页</div>

    ${
      page.items.length
        ? `<div class="grid books" data-view="books">${cards}</div>`
        : emptyState('📖', '还没有世界书', '世界书可以从角色卡 PNG/JSON 里自动提取，也可以在这里手动新建。')
    }

    ${
      totalPages > 1
        ? `<div class="center mt20">
             <button class="btn sec sm" id="pg-prev" ${state.page <= 1 ? 'disabled' : ''}>← 上一页</button>
             <span class="muted" style="margin:0 12px">${state.page} / ${totalPages}</span>
             <button class="btn sec sm" id="pg-next" ${state.page >= totalPages ? 'disabled' : ''}>下一页 →</button>
           </div>`
        : ''
    }`,
  );

  // ★ 事件分两类绑（详见 cards.js 里的同一段说明）：
  //   · 工具栏节点属于本次渲染出来的 DOM，mount() 会换掉它们 → **每次渲染后都要重绑**；
  //   · 挂在常驻 #view 上的 data-act 委托 → **每批只绑一次**，重复绑会一次点击弹 N 个窗。
  if (bindSignal.aborted) return;
  bindToolbar(root);
  if (boundSignal !== bindSignal) {
    boundSignal = bindSignal;
    bindBookActions(root, bindSignal);
  }
}

/** 工具栏：属于本次渲染出来的节点，每次渲染后都要重绑 */
function bindToolbar(root) {
  let debounce;
  $('#f-q', root).addEventListener('input', (e) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      state.q = e.target.value.trim();
      state.page = 1;
      renderBookList(root, activeSignal?.signal);
    }, 350);
  });
  $('#btn-reset', root).addEventListener('click', () => {
    state.q = '';
    state.page = 1;
    renderBookList(root, activeSignal?.signal);
  });
  $('#pg-prev', root)?.addEventListener('click', () => {
    state.page = Math.max(1, state.page - 1);
    renderBookList(root, activeSignal?.signal);
  });
  $('#pg-next', root)?.addEventListener('click', () => {
    state.page += 1;
    renderBookList(root, activeSignal?.signal);
  });
  $('#btn-new', root).addEventListener('click', () => openBookEditor(root, null));
}

/** 列表里「查看 / 编辑」「删除」：委托在常驻 #view 上，每批只绑一次 */
function bindBookActions(root, signal) {
  // ★ 必须带 { signal }：这个委托监听器挂在常驻的 #view 上，不会随 innerHTML 消失。
  //   不加的话，访问过「角色卡」页之后再来到这里，点世界书会同时触发那边残留的监听器，
  //   它拿着世界书的 id 去请求 /character-cards/{id}，于是弹出一堆「角色卡不存在」。
  root.addEventListener(
    'click',
    async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      // ★ 归属校验（纵深防御）：世界书 / 插件 / 预设 / 角色卡的卡片都长得像 `.cc`，
      //   列表容器各带 data-view，认不出来的按钮一概不管（事故背景见 presets.js）。
      if (!btn.closest('[data-view="books"]')) return;
      const cardEl = btn.closest('.cc');
      if (!cardEl) return; // 不属于世界书列表的按钮
      const id = Number(cardEl.dataset.id);
      // 判断"这一批还活着吗"要认**当前**批次：列表刷新后旧信号会被 abort
      const isStale = () => activeSignal?.signal.aborted ?? true;
      const restore = buttonLoading(btn);
      try {
        if (btn.dataset.act === 'view') {
          const book = await api.get(`/world-books/${id}`);
          if (isStale()) return;
          restore();
          openBookEditor(root, book);
        } else if (btn.dataset.act === 'del') {
          restore();
          await deleteBookFlow(root, id, activeSignal.signal);
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
   删除（被角色卡使用时需要二次确认）
   ================================================================== */
async function deleteBookFlow(root, id, signal) {
  try {
    await api.del(`/world-books/${id}`);
    if (signal?.aborted) return;
    toastOk('世界书已删除');
    await renderBookList(root, activeSignal.signal);
    return;
  } catch (err) {
    if (signal?.aborted) return;
    if (err.status !== 409) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
      return;
    }
    const d = err.detail || {};
    const cards = d.cards || [];
    const m = modal({
      title: '这本世界书正在被使用',
      width: 'narrow',
      bodyHTML: `
        <div class="alert warn">
          有 <b>${d.card_count || cards.length}</b> 张角色卡在使用这本世界书。
          删除后它们会失去世界观设定（卡片本身不会被删，只是解除关联）。
        </div>
        <div class="panel" style="padding:12px">
          ${cards
            .map(
              (c) =>
                `<div class="small" style="padding:3px 0">🎭 ${esc(c.name)} <span class="faint mono">#${c.id}</span></div>`,
            )
            .join('')}
          ${d.card_count > cards.length ? `<div class="small faint">…还有 ${d.card_count - cards.length} 张</div>` : ''}
        </div>`,
      footHTML: `
        <button class="btn sec" data-close>取消</button>
        <button class="btn danger" id="btn-force-del">仍然删除</button>`,
    });

    $('#btn-force-del', m.root).addEventListener('click', async (e) => {
      const restore = buttonLoading(e.target, '删除中');
      try {
        await api.del(`/world-books/${id}`, { force: true });
        m.close();
        if (signal?.aborted) return;
        toastOk('世界书已删除，相关角色卡已解除关联');
        await renderBookList(root, activeSignal.signal);
      } catch (e2) {
        toastErr(e2.toDisplay ? e2.toDisplay() : String(e2));
      } finally {
        restore();
      }
    });
  }
}

/* ==================================================================
   查看 / 编辑（含条目编辑器）
   ================================================================== */
function openBookEditor(root, existing) {
  const isEdit = Boolean(existing);

  // ★ 用一份 JS 数组保存条目，这样"我们不认识的字段"能被原样带过去
  let entries = (existing?.entries || []).map((e) => ({ ...e }));

  const m = modal({
    title: isEdit ? `世界书 · ${existing.display_name}` : '新建世界书',
    width: 'wide',
    bodyHTML: `
      ${
        isEdit && (existing.used_by_cards || []).length
          ? `<div class="alert info">
               正在被 ${existing.used_by_cards.length} 张角色卡使用：
               ${existing.used_by_cards.map((c) => esc(c.name)).join('、')}
               —— 改动会立刻影响它们。
             </div>`
          : ''
      }

      <div class="grid" style="grid-template-columns:1fr 1fr;gap:18px">
        ${textField('名称', 'name', existing?.name || '', {
          placeholder: '如「克苏鲁世界」',
          hint: isEdit ? '留空表示这本世界书原本没有名字（规范允许）' : '建议起个名字，便于在角色卡里选择',
        })}
        ${textField('简介', 'description', existing?.description || '', {})}
      </div>

      <div class="form-section">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <h3 style="margin:0">条目（<span id="entry-count">${entries.length}</span> 条）</h3>
          <button class="btn sec sm" type="button" id="btn-add-entry">+ 添加条目</button>
        </div>
        <div class="hint mb8">
          每条包含「触发关键词」和「命中后注入提示词的设定文本」。
          不认识的字段会被原样保留（保证导出回 SillyTavern 时不丢设置）。
        </div>

        <!-- ★ 关键词触发检索的参数：3.9 起真正生效，
             不填就不好判断"为什么这条设定没被注入" -->
        <div class="panel" style="padding:12px;margin-bottom:12px">
          <div class="small muted mb8">
            命中的条目才会被注入提示词。若某条设定"明明写了却没生效"，
            多半是关键词没提到，或者命中了但被 token 预算挤掉。
          </div>
          <div class="row">
            <div>
              <label class="small muted">扫描最近多少条消息（scan_depth）</label>
              <input type="number" name="scan_depth" min="1" max="200"
                     value="${esc(existing?.scan_depth ?? 8)}" />
            </div>
            <div>
              <label class="small muted">注入预算 token（token_budget）</label>
              <input type="number" name="token_budget" min="0" max="100000"
                     value="${esc(existing?.token_budget ?? 1024)}" />
            </div>
          </div>
        </div>

        <div id="entries-box"></div>
      </div>`,
    footHTML: `
      <button class="btn sec" data-close>关闭</button>
      <button class="btn" id="btn-save">${isEdit ? '保存修改' : '创建世界书'}</button>`,
  });

  const box = $('#entries-box', m.root);

  function renderEntries() {
    $('#entry-count', m.root).textContent = entries.length;
    if (!entries.length) {
      box.innerHTML = `<div class="empty" style="padding:26px">还没有条目。点「+ 添加条目」开始。</div>`;
      return;
    }
    box.innerHTML = entries
      .map((e, i) => {
        const unknownKeys = Object.keys(e).filter(
          (k) => !['keys', 'content', 'enabled', 'insertion_order', 'extensions'].includes(k),
        );
        return `
        <div class="panel" style="margin-bottom:10px" data-index="${i}">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <strong class="small">条目 ${i + 1}</strong>
            <button class="btn link danger-link" type="button" data-remove="${i}">删除条目</button>
          </div>
          <div class="row">
            <div style="flex:2">
              <label class="small muted">触发关键词（英文逗号分隔）</label>
              <input type="text" data-k="keys" data-i="${i}"
                     value="${esc((e.keys || []).join(', '))}" placeholder="龙, 巨龙" />
            </div>
            <div style="flex:1">
              <label class="small muted">插入顺序</label>
              <input type="number" data-k="insertion_order" data-i="${i}"
                     value="${esc(e.insertion_order ?? 0)}" />
            </div>
            <div style="flex:0 0 auto;display:flex;align-items:flex-end;padding-bottom:6px">
              <label class="checkbox-line">
                <input type="checkbox" data-k="enabled" data-i="${i}" ${e.enabled !== false ? 'checked' : ''} />
                <span class="small">启用</span>
              </label>
            </div>
          </div>
          <div class="mt8">
            <label class="small muted">设定正文（命中关键词后注入提示词）</label>
            <textarea data-k="content" data-i="${i}" rows="3"
                      placeholder="世上最后一条龙已在三百年前死去……">${esc(e.content || '')}</textarea>
          </div>
          ${
            unknownKeys.length
              ? `<div class="small faint mt8">
                   保留了 ${unknownKeys.length} 个本系统不认识的字段：
                   <span class="mono">${esc(unknownKeys.join(', '))}</span>
                 </div>`
              : ''
          }
        </div>`;
      })
      .join('');
  }

  // 输入时就同步回 entries 数组，避免"改了没生效"
  box.addEventListener('input', (e) => {
    const node = e.target;
    const i = Number(node.dataset.i);
    const k = node.dataset.k;
    if (Number.isNaN(i) || !k) return;
    if (k === 'keys') {
      entries[i].keys = node.value
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
    } else if (k === 'enabled') {
      entries[i].enabled = node.checked;
    } else if (k === 'insertion_order') {
      entries[i].insertion_order = Number(node.value) || 0;
    } else {
      entries[i][k] = node.value;
    }
  });
  box.addEventListener('change', (e) => {
    if (e.target.dataset.k === 'enabled') entries[Number(e.target.dataset.i)].enabled = e.target.checked;
  });

  box.addEventListener('click', (e) => {
    const rm = e.target.closest('[data-remove]');
    if (!rm) return;
    entries.splice(Number(rm.dataset.remove), 1);
    renderEntries();
  });

  $('#btn-add-entry', m.root).addEventListener('click', () => {
    entries.push({ keys: [], content: '', enabled: true, insertion_order: 0, extensions: {} });
    renderEntries();
    // 滚到新条目并把焦点放上去
    const last = box.querySelector('.panel:last-child');
    last?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    last?.querySelector('input[data-k=keys]')?.focus();
  });

  renderEntries();

  $('#btn-save', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '保存中');
    try {
      const name = $('[name=name]', m.root).value.trim();
      const description = $('[name=description]', m.root).value.trim();

      // 保存前做一次前端体检，给出比后端 422 更好懂的提示
      for (let i = 0; i < entries.length; i += 1) {
        if (!String(entries[i].content || '').trim()) {
          toastErr(`第 ${i + 1} 条没有填设定正文（content）。每条都必须有内容。`);
          return;
        }
      }

      const payload = { description: description || null, entries };

      // 3.9：扫描参数一起提交（两个输入框始终有值，不会误清空）
      const scanDepth = Number($('[name=scan_depth]', m.root).value);
      const tokenBudget = Number($('[name=token_budget]', m.root).value);
      if (Number.isFinite(scanDepth) && scanDepth >= 1) payload.scan_depth = scanDepth;
      if (Number.isFinite(tokenBudget) && tokenBudget >= 0) payload.token_budget = tokenBudget;

      if (isEdit) {
        // name 传 null 表示"不改"（后端把必填字段的 null 当作保持原值）
        payload.name = name || null;
        await api.patch(`/world-books/${existing.id}`, payload);
        toastOk('世界书已更新');
      } else {
        if (!name) {
          toastErr('新建世界书必须填名称');
          return;
        }
        payload.name = name;
        await api.post('/world-books', payload);
        toastOk('世界书已创建');
      }
      m.close();
      await renderBookList(root, activeSignal.signal);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}
