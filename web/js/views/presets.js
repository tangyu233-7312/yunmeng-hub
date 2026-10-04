/* ============================================================
   提示词预设（prompt preset）视图

   ==================== 这个页面解决什么问题？====================
   角色卡说明「这是谁」、世界书说明「世界有什么」，
   而**模型该怎么表现**（规则、破甲、输出格式、采样参数）既不属于卡也不属于书 ——
   它是跨角色共用的一套规范，必须能独立保存、切换、按顺序装配。

   ==================== 页面上最该被看懂的三件事 ====================
   1. **块的顺序**：谁先谁后直接决定模型看到什么（可上下移动、可启停）
   2. **深度注入**：`injection_position=1` 的块会插进对话历史，
      破甲通常靠它生效（界面上要写清楚，不然用户不知道它和普通块的区别）
   3. **哪些东西没生效**：被禁用的块、被跳过的块、不认识的宏、
      云端会忽略的采样参数 —— 全部如实标出来

   ==================== 监听器约定（别再踩同一个坑）====================
   #view 是常驻元素，本视图会在增删改之后重渲染自己。
   所有挂在 root 上的委托监听器必须用**当前批次**的信号绑定，
   且"只绑一次"的守卫要用同一个信号判断死活 ——
   用调用方传进来的旧信号会导致监听器一诞生就是死的
   （见 docs/pitfalls.md 第 22 条，cards.js 上真实发生过）。
   ============================================================ */

import { api } from 'hne/api';
import {
  $,
  $$,
  bindFileDrop,
  buttonLoading,
  confirmDialog,
  emptyState,
  esc,
  freshViewSignal,
  modal,
  mount,
  mountError,
  toastErr,
  toastOk,
  toastWarn,
} from 'hne/ui';

const API = '/prompt-presets';

let activeSignal = null;
/** 当前展开查看的预设详情（null = 只看列表） */
let openDetail = null;

/**
 * ★ 本页有**两个会重画的视图**（列表 / 详情），而两者的委托监听器都挂在 root 上。
 *   只用一个布尔标志位会出现这种情况：
 *     列表 → 详情（列表标志仍是 true）→ 返回列表（标志还在，于是**绑不上**监听器）
 *     结果"返回列表"之后所有按钮都点不动。
 *
 *   这里改成"谁重画谁换控件"：每次重画某个视图前先 abort 掉上一次的监听器，
 *   画完再绑新的。旧控制器一 abort，旧监听器立刻失效 —— 既不会叠加，也不会漏绑。
 *
 *   （同类事故见 docs/pitfalls.md 第 22 条：监听器被绑在已 abort 的信号上。）
 */
let listController = null;
let detailController = null;

function nextController(current, parent) {
  current?.abort();
  const controller = new AbortController();
  // ★ parent 必须传本视图的信号（activeSignal.signal），这不是可选项。
  //
  //   这一条控制器是给**挂在常驻 #view 上的委托监听器**用的。只 abort 上一批
  //   并不够：上一批之所以在切页后失效，靠的是它当初接在路由信号上；
  //   新控制器如果不接力，它**永远不会被 abort**，于是在别的页面继续被触发。
  //
  //   真实事故（用户复现）：预设页与插件页的卡片都是 `.cc` + `data-act="del"`，
  //   预设页留下的监听器接到了插件页的删除按钮上 →
  //   点一次「删除」弹**两个**窗，第二个还拿着插件的 id 去删预设，
  //   报 404「提示词预设不存在」。
  if (parent) {
    if (parent.aborted) controller.abort();
    else parent.addEventListener('abort', () => controller.abort(), { once: true });
  }
  return controller;
}

/* ==================================================================
   入口
   ================================================================== */
export async function renderPresets(root, ctx = {}) {
  activeSignal = freshViewSignal('presets', ctx.signal);
  await renderList(root, activeSignal.signal);
}

/* ==================================================================
   列表
   ================================================================== */
async function renderList(root, signal) {
  // ★ 先确认"我还是当前这一批"再动 DOM：旧批次的回调醒来时，
  //   如果先去画「加载中…」、又因为信号失效直接 return，整页就会永远停在加载中。
  const isCurrent = () => activeSignal?.signal === signal && !signal?.aborted;
  if (!isCurrent()) return;
  mount(root, `<div class="empty"><div class="big">⏳</div><div>加载中…</div></div>`);

  let items = [];
  try {
    // ★ 注意：预设列表接口返回的是**数组**（不是 {items, total} 那种分页壳，
    //   因为预设数量天然很少）。写成 `.items` 会永远是空列表且不报错 ——
    //   第一次就是这么错的：导入成功、列表却一张卡片都没有。
    const res = await api.get(API, undefined, { timeoutMs: 20000 });
    items = Array.isArray(res) ? res : res?.items || [];
  } catch (err) {
    if (!isCurrent()) return;
    mountError(root, err.toDisplay ? err.toDisplay() : String(err), () =>
      renderList(root, activeSignal?.signal),
    );
    return;
  }
  if (!isCurrent()) return;

  const cards = items
    .map(
      (p) => `
      <div class="cc" data-id="${p.id}">
        <div class="cc-top">
          <div class="cc-avatar" style="background:var(--brand-soft);color:var(--brand)">🧩</div>
          <div class="cc-headline">
            <div class="cc-name" title="${esc(p.name)}">${esc(p.name)}${
              p.is_active ? ' <span class="badge ok">全局默认</span>' : ''
            }${p.is_builtin ? ' <span class="badge info">内置·永远生效</span>' : ''}</div>
            <div class="cc-desc">${esc(p.description || '（没有说明）')}</div>
          </div>
        </div>
        <div class="cc-body">
          <div class="mb8">
            <span class="badge neutral">${p.block_count} 块</span>
            <span class="badge ${p.enabled_block_count ? 'ok' : 'warn'}">启用 ${p.enabled_block_count}</span>
            ${
              p.depth_block_count
                ? `<span class="badge info">深度注入 ${p.depth_block_count}</span>`
                : '<span class="badge warn">没有深度注入块</span>'
            }
            <span class="badge neutral">${p.source_format === 'sillytavern' ? '酒馆导入' : '本系统'}</span>
          </div>
          ${p.source_filename ? `<div class="faint small">来源文件：${esc(p.source_filename)}</div>` : ''}
        </div>
        <div class="cc-foot">
          <button class="btn sm sec" data-act="open">打开</button>
          <button class="btn sm sec" data-act="activate" ${p.is_active ? 'disabled' : ''}>设为默认</button>
          <button class="btn sm sec" data-act="export">导出</button>
          <button class="btn sm sec" data-act="rename">改名</button>
          <button class="btn sm sec" data-act="del" style="color:var(--danger)">删除</button>
          <span class="spacer"></span>
          <span class="faint small">${esc((p.updated_at || '').slice(0, 16).replace('T', ' '))}</span>
        </div>
      </div>`,
    )
    .join('');

  mount(
    root,
    `
    <div class="page-head">
      <div>
        <h1>提示词预设</h1>
        <div class="sub">
          预设规范<strong>模型该怎么表现</strong>（规则、破甲、输出格式）。
          与角色卡（这是谁）、世界书（世界有什么）相互独立 ——
          同一张卡配不同预设，模型的听话程度可以完全不同。
        </div>
      </div>
      <div class="page-actions">
        <button class="btn sec" id="btn-restore-builtin" title="把「内置守卫规则」恢复成出厂内容（删掉之后也能用它找回来）">
          还原内置规则
        </button>
        <button class="btn sec" id="btn-import-tavern">导入酒馆预设 JSON</button>
        <button class="btn sec" id="btn-import-own">导入本系统 JSON</button>
        <button class="btn" id="btn-new-preset">+ 新建预设</button>
      </div>
    </div>

    <div class="alert info">
      <strong>怎么用：</strong>先在「叙事会话 → 设置」里把某个会话绑到这套预设，
      或者把它设为<strong>全局默认</strong>（所有没单独绑定的会话都用它）。
      绑定之后，块里的规则会按顺序进入提示词；
      标了「深度注入」的块会<strong>插进最近几条对话之前</strong> —— 破甲类写法通常靠这一步生效。
    </div>

    <div class="alert info">
      <strong>内置守卫规则（身份认知 / 剧情不跑偏 / 输出长度）：</strong>
      它<strong>永远追加在你绑定的预设之后</strong>（越靠后的指令权重越高），
      所以一个预设都不绑时它也照样生效。它和普通预设一样能改正文、禁用块、甚至删掉；
      删掉之后就不再生效（系统不会偷偷重建），随时点右上角「还原内置规则」找回来。
    </div>

    ${
      items.length
        ? `<div class="grid cards" data-view="presets">${cards}</div>`
        : emptyState(
            '🧩',
            '还没有提示词预设',
            '点右上角「导入酒馆预设 JSON」把你现成的预设搬进来，或者「新建预设」从系统内置装配开始改。',
          )
    }`,
  );

  bindList(root);
}

/* ==================================================================
   列表事件绑定
   ================================================================== */
function bindList(root) {
  // 路由级信号已经死了（切页了）就什么都不做 ——
  // 绑一个死信号上的监听器永远不会触发，还会让"已绑定"的判断失效。
  const routeSignal = activeSignal?.signal;
  if (!routeSignal || routeSignal.aborted) return;

  listController = nextController(listController, routeSignal);
  const signal = listController.signal;

  const withSignal = (el, type, handler) => {
    if (el) el.addEventListener(type, handler, { signal });
  };

  withSignal($('#btn-new-preset', root), 'click', () => openCreateDialog(root));
  withSignal($('#btn-restore-builtin', root), 'click', async (e) => {
    const restore = buttonLoading(e.target, '还原中');
    try {
      await api.post(`${API}/builtin/restore`);
      toastOk('内置守卫规则已还原为出厂内容');
      await renderList(root, activeSignal.signal);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
      restore();
    }
  });
  withSignal($('#btn-import-tavern', root), 'click', () => openImportDialog(root, 'sillytavern'));
  withSignal($('#btn-import-own', root), 'click', () => openImportDialog(root, 'hne'));

  withSignal(root, 'click', async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    // ★ 归属校验（纵深防御）：预设 / 插件 / 角色卡 / 世界书四个页面的卡片都是
    //   `.cc` 卡片，光看 class 分不出这张卡属于哪一页。列表容器各自带 data-view，
    //   认不出来的按钮直接放行给真正的主人 —— 即使将来哪个视图的信号接错了，
    //   也不会出现"在 A 页点删除，B 页的弹窗冒出来"（真实事故见 nextController 的注释）。
    if (!btn.closest('[data-view="presets"]')) return;
    const card = btn.closest('.cc');
    if (!card) return;
    const id = Number(card.dataset.id);
    const act = btn.dataset.act;
    const restore = buttonLoading(btn);

    try {
      if (act === 'open') {
        restore();
        await openDetailView(root, id);
        return;
      }
      if (act === 'activate') {
        await api.patch(`${API}/${id}`, { is_active: true });
        toastOk('已设为全局默认预设');
      } else if (act === 'export') {
        const payload = await api.get(`${API}/${id}/export`);
        downloadJson(payload, `${card.querySelector('.cc-name')?.textContent?.trim() || 'preset'}.json`);
        toastOk('已导出（可直接导回 SillyTavern）');
      } else if (act === 'rename') {
        restore();
        await openRenameDialog(root, id);
        return;
      } else if (act === 'del') {
        restore();
        const ok = await confirmDialog({
          title: '删除这套预设？',
          message:
            '删除后，绑定过它的会话会自动回到「全局默认预设 / 系统内置装配」——\n' +
            '对话记录不会丢，只是模型的行为规范变回默认。',
          confirmText: '删除',
          danger: true,
        });
        if (!ok) return;
        if (card.querySelector('.badge.info')?.textContent?.includes('内置')) {
          // 删内置预设 = 关掉"身份认知 / 不跑偏 / 输出长度"这套硬规则。
          // 必须说清"不会被自动重建"，否则用户会以为删不掉。
          const sure = await confirmDialog({
            title: '删除内置守卫规则？',
            message:
              '这是系统自带的沉浸感硬规则（不让角色承认自己是 AI、剧情不跑偏、回复不过短）。\n\n' +
              '删掉之后它就**不再生效**了，而且系统不会自动重建 ——\n' +
              '想找回来随时点右上角「还原内置规则」。',
            confirmText: '仍然删除',
            danger: true,
          });
          if (!sure) return;
        }
        await api.del(`${API}/${id}`);
        toastOk('预设已删除');
      }
      await renderList(root, activeSignal.signal);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

/* ==================================================================
   详情（块清单 / 启停 / 排序 / 改正文 / 预览）
   ================================================================== */
async function openDetailView(root, presetId) {
  let detail;
  try {
    detail = await api.get(`${API}/${presetId}`);
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
    return;
  }
  openDetail = detail;
  mountDetail(root, detail);
}

function mountDetail(root, detail) {
  const blocks = [...(detail.blocks || [])].sort((a, b) => a.order_index - b.order_index);

  const rows = blocks
    .map((b, index) => {
      const isMarker = b.kind === 'marker';
      const builtin = isMarker || BUILTIN_IDS.has(b.identifier);
      return `
      <tr data-id="${esc(b.identifier)}" class="${b.enabled ? '' : 'row-off'}">
        <td>
          <input type="checkbox" data-field="enabled" ${b.enabled ? 'checked' : ''} />
        </td>
        <td>
          <div class="block-name">${esc(b.name)}</div>
          <div class="faint small"><code>${esc(b.identifier)}</code>${
            isMarker ? ' · 占位符' : ''
          }${b.supported ? '' : ' · <span style="color:var(--warn)">本系统不支持</span>'}</div>
        </td>
        <td>
          <select data-field="role">
            ${['system', 'user', 'assistant']
              .map((r) => `<option value="${r}" ${b.role === r ? 'selected' : ''}>${r}</option>`)
              .join('')}
          </select>
        </td>
        <td>
          <select data-field="injection_position">
            <option value="0" ${b.injection_position === 0 ? 'selected' : ''}>进系统提示词</option>
            <option value="1" ${b.injection_position === 1 ? 'selected' : ''}>插进对话历史</option>
          </select>
        </td>
        <td>
          <input type="number" data-field="injection_depth" min="0" max="64"
                 value="${b.injection_depth}" style="width:64px"
                 ${b.injection_position === 0 ? 'disabled' : ''} />
        </td>
        <td class="nowrap">
          <button class="btn link" data-act="up" ${index === 0 ? 'disabled' : ''}>↑</button>
          <button class="btn link" data-act="down" ${index === blocks.length - 1 ? 'disabled' : ''}>↓</button>
        </td>
        <td class="nowrap">
          ${
            isMarker
              ? '<span class="faint small">内容来自角色卡/世界书</span>'
              : `<button class="btn sm sec" data-act="edit-block">改正文</button>`
          }
          ${
            builtin
              ? ''
              : `<button class="btn sm sec" data-act="del-block" style="color:var(--danger)">删除</button>`
          }
        </td>
      </tr>`;
    })
    .join('');

  mount(
    root,
    `
    <div class="page-head">
      <div>
        <h1>${esc(detail.name)}</h1>
        <div class="sub">${esc(detail.description || '（没有说明）')}</div>
      </div>
      <div class="page-actions">
        <button class="btn sec" id="btn-back">← 返回列表</button>
        <button class="btn sec" id="btn-detail-activate" ${detail.is_active ? 'disabled' : ''}>
          设为全局默认
        </button>
        <button class="btn" id="btn-add-block">+ 加一个规则块</button>
      </div>
    </div>

    ${
      (detail.import_notes || []).length
        ? `<div class="alert warn">
             <strong>导入时的提醒（原样保留，没有替你改动）</strong>
             <ul style="margin:6px 0 0 18px">${detail.import_notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>
           </div>`
        : ''
    }

    <div class="alert info">
      <strong>「插进对话历史」和「进系统提示词」的区别：</strong>
      进系统提示词 = 对模型"嘱咐"一遍；
      插进对话历史 = 让这段文字以 user/assistant 的身份出现在最近几条消息之前，
      模型会以为双方已经就此达成过一致 —— <strong>破甲通常靠后者生效</strong>。
      「深度」表示插在倒数第几条消息之前（0 表示追加到最后）。
    </div>

    <div class="panel">
      <div class="reqlog-head" style="border-bottom:1px solid var(--border)">
        <strong>块清单（按装配顺序）</strong>
        <span class="faint small">改动会立即保存</span>
      </div>
      <div style="overflow:auto">
        <table class="list blocks-table">
          <thead>
            <tr>
              <th style="width:40px">启用</th>
              <th>块</th>
              <th style="width:110px">角色</th>
              <th style="width:140px">注入方式</th>
              <th style="width:80px">深度</th>
              <th style="width:70px">顺序</th>
              <th style="width:180px">操作</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>

    <div class="panel mt14">
      <div class="reqlog-head" style="border-bottom:1px solid var(--border)"><strong>采样参数</strong></div>
      <div style="padding:12px 14px">
        <div class="mb8">
          ${samplingBadges(detail.sampling)}
        </div>
        ${
          (detail.sampling?.ignored_by_cloud || []).length
            ? `<div class="alert warn">${esc(detail.sampling.note || '')}</div>`
            : '<div class="hint">这些参数都是本系统显式支持的，会真的发给模型。</div>'
        }
      </div>
    </div>

    <div class="panel mt14">
      <div class="reqlog-head" style="border-bottom:1px solid var(--border)">
        <strong>装配预览</strong>
        <span class="faint small">按真实会话上下文构建（不调用模型）</span>
      </div>
      <div style="padding:12px 14px">
        <div class="row" style="align-items:flex-end">
          <div style="flex:2">
            <label class="small muted">选一个会话来预览（可选，选了才有真实的角色卡/世界书/历史）</label>
            <select id="preview-session"><option value="">（只做结构预览）</option></select>
          </div>
          <div style="flex:0 0 auto">
            <button class="btn sec" id="btn-run-preview">生成预览</button>
          </div>
        </div>
        <div id="preview-box" class="mt14"></div>
      </div>
    </div>`,
  );

  bindDetail(root, detail);
  loadPreviewSessions(root);
}

/** 内置块 id 集合（用于"不给删"的判断；与后端 presets.BLOCK_LABELS 对应） */
const BUILTIN_IDS = new Set([
  'main',
  'worldInfoBefore',
  'worldInfoAfter',
  'charDescription',
  'charPersonality',
  'scenario',
  'dialogueExamples',
  'chatHistory',
  'jailbreak',
  'personaDescription',
]);

function samplingBadges(sampling) {
  if (!sampling) return '<span class="faint small">没有携带采样参数</span>';
  const native = ['temperature', 'top_p', 'frequency_penalty', 'presence_penalty', 'max_tokens'];
  const ignored = new Set(sampling.ignored_by_cloud || []);
  const out = [];
  native.forEach((key) => {
    const value = sampling[key];
    if (value === null || value === undefined) return;
    out.push(`<span class="badge ok">${key} = ${esc(String(value))}</span>`);
  });
  ignored.forEach((key) => {
    const value = sampling[key];
    if (value === null || value === undefined) return;
    out.push(`<span class="badge warn" title="云端接口会忽略它">${key} = ${esc(String(value))}（云端忽略）</span>`);
  });
  if (sampling.context_window) {
    out.push(`<span class="badge neutral">预设想要的上下文窗口 ${esc(String(sampling.context_window))}</span>`);
  }
  return out.length ? out.join(' ') : '<span class="faint small">没有携带采样参数</span>';
}

function bindDetail(root, detail) {
  const routeSignal = activeSignal?.signal;
  if (!routeSignal || routeSignal.aborted) return;

  detailController = nextController(detailController, routeSignal);
  const signal = detailController.signal;
  const on = (el, type, handler) => el && el.addEventListener(type, handler, { signal });

  on($('#btn-back', root), 'click', async () => {
    openDetail = null;
    await renderList(root, activeSignal.signal);
  });

  on($('#btn-detail-activate', root), 'click', async () => {
    try {
      await api.patch(`${API}/${detail.id}`, { is_active: true });
      toastOk('已设为全局默认预设');
      await openDetailView(root, detail.id);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    }
  });

  on($('#btn-add-block', root), 'click', () => openAddBlockDialog(root, detail));

  on($('#btn-run-preview', root), 'click', async () => {
    const btn = $('#btn-run-preview', root);
    const restore = buttonLoading(btn, '构建中');
    try {
      const box = $('#preview-box', root);
      const sessionId = $('#preview-session', root).value;
      const preview = await api.get(`${API}/preview`, {
        preset_id: detail.id,
        session_id: sessionId || undefined,
      });
      box.innerHTML = renderPreview(preview);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });

  // ---- 块表格：委托监听（change / click 两种）----
  const table = $('.blocks-table', root);
  if (!table) return;

  on(table, 'change', async (e) => {
    const row = e.target.closest('tr[data-id]');
    if (!row) return;
    const identifier = row.dataset.id;
    const field = e.target.dataset.field;
    if (!field) return;
    const body =
      field === 'enabled'
        ? { enabled: e.target.checked }
        : field === 'injection_depth'
          ? { injection_depth: Number(e.target.value) }
          : { [field]: e.target.value };
    await saveBlock(root, detail.id, identifier, body);
  });

  on(table, 'click', async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const row = btn.closest('tr[data-id]');
    if (!row) return;
    const identifier = row.dataset.id;
    const act = btn.dataset.act;

    if (act === 'edit-block') {
      const block = detail.blocks.find((b) => b.identifier === identifier);
      openBlockContentDialog(root, detail.id, block);
      return;
    }
    if (act === 'del-block') {
      const ok = await confirmDialog({
        title: '删除这个块？',
        message: '删除后它会从装配顺序里消失。这个操作不能撤销。',
        confirmText: '删除',
        danger: true,
      });
      if (!ok) return;
      try {
        await api.del(`${API}/${detail.id}/blocks/${encodeURIComponent(identifier)}`);
        toastOk('块已删除');
        await openDetailView(root, detail.id);
      } catch (err) {
        toastErr(err.toDisplay ? err.toDisplay() : String(err));
      }
      return;
    }
    if (act === 'up' || act === 'down') {
      await moveBlock(root, detail, identifier, act === 'up' ? -1 : 1);
    }
  });
}

async function saveBlock(root, presetId, identifier, body) {
  try {
    await api.patch(`${API}/${presetId}/blocks/${encodeURIComponent(identifier)}`, body);
    toastOk('已保存');
    // 只刷新数据、整页重画：位置/禁用状态会影响其他控件（例如深度输入框的禁用态）
    await openDetailView(root, presetId);
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
  }
}

async function moveBlock(root, detail, identifier, delta) {
  const blocks = [...detail.blocks].sort((a, b) => a.order_index - b.order_index);
  const index = blocks.findIndex((b) => b.identifier === identifier);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= blocks.length) return;
  [blocks[index], blocks[target]] = [blocks[target], blocks[index]];
  try {
    // 整体替换时正文由后端保留（我们只带了顺序），所以不会截断用户的规则文本
    await api.patch(`${API}/${detail.id}`, { blocks });
    await openDetailView(root, detail.id);
  } catch (err) {
    toastErr(err.toDisplay ? err.toDisplay() : String(err));
  }
}

function renderPreview(preview) {
  const warnings = (preview.warnings || [])
    .map((w) => `<div class="alert warn mt8">${esc(w)}</div>`)
    .join('');
  const messages = (preview.messages || [])
    .map((msg) => {
      const where =
        msg.position === 'depth'
          ? `<span class="badge info">插进历史 · 倒数第 ${msg.depth} 条之前</span>`
          : msg.position === 'history'
            ? '<span class="badge neutral">对话历史</span>'
            : '<span class="badge ok">系统提示词</span>';
      return `
        <div class="panel" style="padding:10px 12px;margin-bottom:10px">
          <div class="mb8">${where} <span class="badge neutral">${esc(msg.role || '')}</span>
            <span class="faint small">来源块：${(msg.from_blocks || []).map(esc).join('、')}</span></div>
          <pre class="prompt-view" style="max-height:240px">${esc((msg.content || '').slice(0, 4000))}</pre>
        </div>`;
    })
    .join('');

  return `
    <div class="alert info">
      生效的块：${(preview.used_blocks || []).map((x) => `<code>${esc(x)}</code>`).join('、') || '（无）'}
      ${preview.history_messages ? ` · 带上 ${preview.history_messages} 条历史` : ''}
    </div>
    ${warnings}
    ${(preview.disabled_blocks || []).length ? `<div class="hint">被禁用：${preview.disabled_blocks.map(esc).join('、')}</div>` : ''}
    ${(preview.skipped_blocks || []).length ? `<div class="hint">被跳过：${preview.skipped_blocks.map(esc).join('；')}</div>` : ''}
    ${(preview.unknown_macros || []).length ? `<div class="hint">不认识的宏（原样保留）：${preview.unknown_macros.map(esc).join('、')}</div>` : ''}
    <div class="stack mt8">${messages}</div>
    <div class="mt8">${(preview.notes || []).map((n) => `<div class="hint">${esc(n)}</div>`).join('')}</div>`;
}

async function loadPreviewSessions(root) {
  const select = $('#preview-session', root);
  if (!select) return;
  try {
    const res = await api.get('/narrative/sessions', { limit: 50 });
    const sessions = Array.isArray(res) ? res : res?.items || [];
    select.innerHTML =
      '<option value="">（只做结构预览）</option>' +
      sessions
        .map((s) => `<option value="${s.id}">${esc(s.title)} #${s.id}</option>`)
        .join('');
  } catch {
    /* 列表拿不到不影响结构预览，静默即可 */
  }
}

/* ==================================================================
   弹窗：新建 / 导入 / 改名 / 改正文 / 加块
   ================================================================== */
function openCreateDialog(root) {
  const m = modal({
    title: '新建提示词预设',
    bodyHTML: `
      <div class="alert info">
        新建出来的预设 <strong>等价于系统内置装配</strong>（顺序、启停都一样），
        你可以照着改：调顺序、加规则块、关掉不想注入的部分。
      </div>
      <div class="field"><label>名称</label><input type="text" name="name" placeholder="例如：我的破甲规则" /></div>
      <div class="field"><label>说明（可选）</label><input type="text" name="description" /></div>`,
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-do-create">创建</button>`,
  });
  $('#btn-do-create', m.root).addEventListener('click', async (e) => {
    const values = formValuesOf(m.root);
    if (!values.name) {
      toastWarn('请填一个名称');
      return;
    }
    const restore = buttonLoading(e.target, '创建中');
    try {
      const created = await api.post(
        `${API}?name=${encodeURIComponent(values.name)}${
          values.description ? `&description=${encodeURIComponent(values.description)}` : ''
        }`,
      );
      m.close();
      toastOk('已创建');
      await openDetailView(root, created.id);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

function openImportDialog(root, kind) {
  const isTavern = kind === 'sillytavern';
  const m = modal({
    title: isTavern ? '导入 SillyTavern 补全预设' : '导入本系统导出的预设',
    width: 'wide',
    bodyHTML: `
      <div class="alert info">
        ${
          isTavern
            ? `在酒馆里打开「<strong>补全预设</strong>」→ 导出 JSON，把文件<strong>直接拖到下面</strong>
               （或点一下选文件、也可以把内容粘进文本框）。
               本系统会<strong>原样保留</strong>认不出来的块与参数，并把风险明确告诉你 ——
               不会偷偷删东西，导出的 JSON 还能拿回酒馆继续用。`
            : '把本系统「导出」出来的 JSON <strong>拖到下面</strong>，或者粘贴内容。'
        }
      </div>
      <div class="field"><label>名称（留空则用文件名）</label><input type="text" name="name" /></div>

      <div id="preset-drop" class="drop-zone">
        <div style="font-size:26px">📄</div>
        <div class="mt8">把 JSON 文件拖到这里，或点击选择文件</div>
        <div class="faint small mt8">也可以直接把内容粘到下面的文本框里</div>
      </div>

      <div class="field">
        <label>预设 JSON</label>
        <textarea name="raw" rows="10" placeholder='{"prompts": [...], "temperature": 1.4, ...}'></textarea>
      </div>
      <div class="field"><label>来源文件名（可选，便于追查）</label><input type="text" name="filename" /></div>`,
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-do-import">导入</button>`,
  });

  // ★ 拖文件进来：读成文本，填进文本框与文件名（用户明确要求不要只能粘贴）
  bindFileDrop($('#preset-drop', m.root), {
    accept: '.json,application/json',
    onText: (text, file) => {
      $('[name=raw]', m.root).value = text;
      if (!$('[name=filename]', m.root).value) {
        $('[name=filename]', m.root).value = file.name;
        if (!$('[name=name]', m.root).value) {
          $('[name=name]', m.root).value = file.name.replace(/\.json$/i, '');
        }
      }
      toastOk(`已读入 ${file.name}（${Math.round(text.length / 1024)} KB），点「导入」即可`);
    },
  });

  $('#btn-do-import', m.root).addEventListener('click', async (e) => {
    const values = formValuesOf(m.root);
    let parsed;
    try {
      parsed = JSON.parse(values.raw || '');
    } catch {
      toastErr('JSON 解析失败，请检查是不是完整复制了文件内容');
      return;
    }
    const restore = buttonLoading(e.target, '导入中');
    try {
      const body = {
        name: values.name || null,
        source_filename: values.filename || null,
        ...(isTavern ? { raw: parsed } : { preset: parsed }),
      };
      const created = await api.post(`${API}/import`, body);
      m.close();
      const notes = created.import_notes || [];
      toastOk(`已导入：${created.block_count} 个块`);
      if (notes.length) {
        // ★ 导入的"副作用说明"必须让人看到：哪些块不支持、哪些参数云端无效
        modal({
          title: '导入完成，有几件事要告诉你',
          width: 'wide',
          bodyHTML: `<ul style="margin:6px 0 0 18px">${notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`,
          footHTML: `<button class="btn" data-close>知道了</button>`,
        });
      }
      await openDetailView(root, created.id);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

async function openRenameDialog(root, presetId) {
  const detail = await api.get(`${API}/${presetId}`);
  const m = modal({
    title: '改名 / 改说明',
    bodyHTML: `
      <div class="field"><label>名称</label><input type="text" name="name" value="${esc(detail.name)}" /></div>
      <div class="field"><label>说明</label><input type="text" name="description" value="${esc(detail.description || '')}" /></div>`,
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-do-rename">保存</button>`,
  });
  $('#btn-do-rename', m.root).addEventListener('click', async (e) => {
    const values = formValuesOf(m.root);
    const restore = buttonLoading(e.target, '保存中');
    try {
      await api.patch(`${API}/${presetId}`, {
        name: values.name || detail.name,
        description: values.description,
      });
      m.close();
      toastOk('已保存');
      await renderList(root, activeSignal.signal);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

function openBlockContentDialog(root, presetId, block) {
  const m = modal({
    title: `编辑块：${block.name}`,
    width: 'wide',
    bodyHTML: `
      <div class="alert info">
        支持的宏：<code>{{char}}</code> 角色名、<code>{{user}}</code> 你的用户名、
        <code>{{lastusermessage}}</code> 最后一条用户消息、
        <code>{{description}}</code> / <code>{{personality}}</code> / <code>{{scenario}}</code>。
        <strong>其它宏会原样保留</strong>（不会生效，但也不会被悄悄删掉）。
      </div>
      <div class="field">
        <label>正文</label>
        <textarea name="content" rows="12">${esc(block.content || '')}</textarea>
      </div>`,
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-do-block">保存</button>`,
  });
  $('#btn-do-block', m.root).addEventListener('click', async (e) => {
    const content = $('[name=content]', m.root).value;
    const restore = buttonLoading(e.target, '保存中');
    try {
      await api.patch(`${API}/${presetId}/blocks/${encodeURIComponent(block.identifier)}`, {
        content,
      });
      m.close();
      toastOk('已保存');
      await openDetailView(root, presetId);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

function openAddBlockDialog(root, detail) {
  const m = modal({
    title: '加一个规则块',
    width: 'wide',
    bodyHTML: `
      <div class="alert info">
        新块默认<strong>不插进历史</strong>（进系统提示词）。
        想让它像"破甲"那样生效，保存后把「注入方式」改成
        <strong>插进对话历史</strong>，并设置深度（例如 4 = 插在最近 4 条消息之前）。
      </div>
      <div class="field"><label>名称</label><input type="text" name="name" placeholder="例如：输出格式要求" /></div>
      <div class="field">
        <label>正文</label>
        <textarea name="content" rows="8" placeholder="例如：每次回复结尾都要描写天气。"></textarea>
      </div>
      <div class="row">
        <div class="field" style="flex:1">
          <label>角色</label>
          <select name="role">
            <option value="system">system（系统提示词里的规则）</option>
            <option value="user">user（以"你"的口吻写出的话）</option>
            <option value="assistant">assistant（以角色口吻回应的话）</option>
          </select>
        </div>
        <div class="field" style="flex:1">
          <label>注入方式</label>
          <select name="injection_position">
            <option value="0">进系统提示词</option>
            <option value="1">插进对话历史（破甲用这个）</option>
          </select>
        </div>
        <div class="field" style="flex:0 0 110px">
          <label>深度</label>
          <input type="number" name="injection_depth" value="4" min="0" max="64" />
        </div>
      </div>`,
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-do-addblock">添加</button>`,
  });

  $('#btn-do-addblock', m.root).addEventListener('click', async (e) => {
    const values = formValuesOf(m.root);
    if (!values.content.trim()) {
      toastWarn('正文不能为空');
      return;
    }
    const restore = buttonLoading(e.target, '添加中');
    try {
      await api.post(`${API}/${detail.id}/blocks`, {
        identifier: `custom-${Date.now().toString(36)}`,
        name: values.name || '自定义规则',
        kind: 'rule',
        content: values.content,
        role: values.role,
        system_prompt: values.role === 'system',
        injection_position: Number(values.injection_position),
        injection_depth: Number(values.injection_depth || 0),
        enabled: true,
        supported: true,
      });
      m.close();
      toastOk('已添加');
      await openDetailView(root, detail.id);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

/* ---------------- 小工具 ---------------- */
function formValuesOf(scope) {
  const out = {};
  $$('input[name], textarea[name], select[name]', scope).forEach((el) => {
    out[el.name] = el.value;
  });
  return out;
}

function downloadJson(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename.endsWith('.json') ? filename : `${filename}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
