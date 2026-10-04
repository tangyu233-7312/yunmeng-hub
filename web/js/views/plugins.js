/* ============================================================
   插件页（极简插件市场）

   ★ 安全边界（界面上也要说清楚，不能只写在代码注释里）：
     插件只有四种**声明式**能力，不执行任何第三方 JS：
       · 正则替换   —— 只作用于"发给模型的提示词"，不改数据库、不改界面显示
       · 提示词注入 —— 往系统提示词里插一段固定文本
       · CSS 主题   —— 换控制台的配色/字号
       · 跑团骰点   —— 随机数由**后端**受控求值（不执行 JS、不用 eval），
                       模型与用户都只能"请求"点数，不能自己造
     因此插件拿不到 API Key、发不出请求、读不到会话内容。
     安装来源只允许 GitHub（后端强制，前端只是提前给提示）。

   ★ 这里也踩过一次的坑：列表页的按钮监听器**必须每次渲染后重绑**
     （mount() 会换掉节点），只有挂在常驻 #view 上的委托才能"每批绑一次"。
     详见 cards.js 顶部那段说明。
   ============================================================ */

import { api } from 'hne/api';
import {
  $,
  $$,
  buttonLoading,
  confirmDialog,
  emptyState,
  esc,
  freshViewSignal,
  modal,
  mount,
  mountError,
  textField,
  toastErr,
  toastOk,
} from 'hne/ui';

const KIND_LABEL = {
  regex: '正则替换',
  prompt: '提示词注入',
  css: 'CSS 主题',
  dice: '跑团骰点',
};
const KIND_BADGE = { regex: 'info', prompt: 'ok', css: 'warn', dice: 'ok' };
const POSITION_LABEL = { start: '提示词开头', before_guard: '守卫之前', end: '提示词末尾（推荐）' };

let state = { items: [], note: '', hosts: [] };
let activeSignal = null;
let boundSignal = null;

/* ==================================================================
   主题注入：把启用中的 CSS 插件应用到这个页面
   ================================================================== */
export async function applyPluginTheme() {
  try {
    const css = await api.getText('/plugins/theme.css');
    let el = document.getElementById('hne-plugin-theme');
    if (!el) {
      el = document.createElement('style');
      el.id = 'hne-plugin-theme';
      document.head.appendChild(el);
    }
    el.textContent = css || '';
  } catch {
    /* 主题是"锦上添花"：取不到就保持默认样式，绝不能因此打断界面 */
  }
}

/* ==================================================================
   页面
   ================================================================== */
export async function renderPlugins(root, ctx = {}) {
  activeSignal = freshViewSignal('plugins', ctx.signal);
  boundSignal = null;
  await renderList(root, activeSignal.signal);
}

async function renderList(root, signal) {
  try {
    await renderListInner(root, signal);
  } catch (err) {
    const cur = activeSignal?.signal;
    if (cur?.aborted) return;
    console.error('[plugins] 列表渲染失败', err);
    mountError(root, `插件页渲染失败：${err?.message || err}`, () =>
      renderList(root, activeSignal?.signal),
    );
  }
}

async function renderListInner(root, signal) {
  const bindSignal = activeSignal?.signal ?? signal;
  const isCurrent = () => activeSignal?.signal === signal && !signal?.aborted;
  if (!isCurrent()) return;
  mount(root, `<div class="empty"><div class="big">⏳</div><div>加载中…</div></div>`);

  let page;
  try {
    page = await api.get('/plugins', undefined, { timeoutMs: 20000 });
  } catch (err) {
    if (!isCurrent()) return;
    mountError(root, err.toDisplay ? err.toDisplay() : String(err), () =>
      renderList(root, activeSignal?.signal),
    );
    return;
  }
  if (!isCurrent()) return;

  state = {
    items: page.items || [],
    note: page.security_note || '',
    hosts: page.allowed_hosts || [],
    catalog: page.catalog || [],
    unsupported: page.unsupported || [],
  };

  const cards = state.items.map(pluginCard).join('');
  mount(
    root,
    `
    <div class="page-head">
      <div>
        <h1>插件</h1>
        <div class="sub">声明式小扩展：改提示词（正则替换 / 注入一段文本）或换控制台样式。</div>
      </div>
      <div class="page-actions">
        <button class="btn sec" id="btn-install">从 GitHub 安装</button>
        <button class="btn" id="btn-new-plugin">+ 新建插件</button>
      </div>
    </div>

    <div class="alert info" id="plugin-note">
      🔒 ${esc(state.note)}
      允许的来源：<b>${esc(state.hosts.join(' / ')) || 'GitHub'}</b>。
    </div>

    ${state.items.length ? `<div class="grid cards" data-view="plugins">${cards}</div>` : emptyState('🧩', '还没有插件', '点「新建插件」自己写一个，或从 GitHub 安装。')}

    ${catalogHTML()}
  `,
  );

  if (bindSignal.aborted) return;
  bindToolbar(root, bindSignal);
  if (boundSignal !== bindSignal) {
    boundSignal = bindSignal;
    bindActions(root, bindSignal);
  }
}

/**
 * 内置示例目录。
 *
 * ★ 为什么要有这一块：用户问"酒馆有哪些默认插件，先放进来"。
 *   酒馆的内置扩展大多要执行 JS 或接外部服务（TTS / 生图 / 快捷回复…），
 *   本项目**不执行第三方代码**，所以只把"能声明式表达"的那些做成示例，
 *   剩下的一律如实列出来（不装作支持）。
 * ★ 目录条目不会自动生效：点「添加」才变成你自己的插件。
 */
function catalogHTML() {
  if (!state.catalog.length) return '';
  return `
    <div class="panel" style="padding:14px;margin-top:16px">
      <div class="mb8">
        <strong>内置示例</strong>
        <span class="muted small">（对照 SillyTavern 的内置扩展挑出来的声明式版本；点「添加」才会生效）</span>
      </div>
      <div class="grid cards">
        ${state.catalog
          .map(
            (item) => `
          <div class="cc catalog-item" data-key="${esc(item.key)}">
            <div class="cc-top">
              <div class="cc-avatar" style="background:var(--brand-soft);color:var(--catalog-accent)">✦</div>
              <div class="cc-headline">
                <div class="cc-name">${esc(item.name)}</div>
                <div class="cc-desc">${esc(item.description)}</div>
              </div>
            </div>
            <div class="cc-body">
              <div class="mb8">
                <span class="badge ${KIND_BADGE[item.kind] || 'neutral'}">${esc(KIND_LABEL[item.kind] || item.kind)}</span>
                <span class="badge neutral">${esc(item.summary)}</span>
                ${
                  item.update_available
                    ? '<span class="badge warn">有更新</span>'
                    : item.installed
                      ? '<span class="badge ok">已添加</span>'
                      : ''
                }
              </div>
              <div class="small faint">来源：${esc(item.source)}</div>
            </div>
            <div class="cc-foot">
              <button class="btn sm ${item.update_available ? '' : 'sec'}" data-catalog-add="${esc(item.key)}" ${
                item.installed && !item.update_available ? 'disabled' : ''
              }>${
                item.update_available ? '更新到最新' : item.installed ? '已添加' : '添加'
              }</button>
            </div>
          </div>`,
          )
          .join('')}
      </div>
      ${
        state.unsupported.length
          ? `<div class="small faint mt12">
               以下 SillyTavern 内置扩展本项目**刻意不做**（需要执行代码或接外部服务，插件不执行第三方 JS）：
               ${state.unsupported.map((u) => `${esc(u.name)}（${esc(u.reason)}）`).join('；')}
             </div>`
          : ''
      }
    </div>`;
}

function pluginCard(p) {
  const rows = [];
  rows.push(
    `<span class="badge ${KIND_BADGE[p.kind] || 'neutral'}">${esc(KIND_LABEL[p.kind] || p.kind)}</span>`,
    p.enabled ? '<span class="badge ok">已启用</span>' : '<span class="badge neutral">已停用</span>',
    p.is_builtin ? '<span class="badge neutral">默认</span>' : '',
    p.version ? `<span class="badge neutral">v${esc(p.version)}</span>` : '',
  );
  return `
    <div class="cc" data-id="${p.id}">
      <div class="cc-top">
        <div class="cc-avatar" style="background:var(--brand-soft);color:var(--brand)">🧩</div>
        <div class="cc-headline">
          <div class="cc-name" title="${esc(p.name)}">${esc(p.name)}</div>
          <div class="cc-desc">${esc(p.description || '（没有说明）')}</div>
        </div>
      </div>
      <div class="cc-body">
        <div class="mb8">${rows.filter(Boolean).join(' ')}</div>
        <div class="small muted">${esc(p.summary || '')}</div>
        <div class="small faint mt8">
          顺序 ${p.priority}${p.author ? ` · 作者 ${esc(p.author)}` : ''}
          ${p.source_url ? ` · <a href="${esc(p.source_url)}" target="_blank" rel="noreferrer noopener">来源</a>` : ''}
        </div>
      </div>
      <div class="cc-foot">
        <label class="checkbox-line" title="停用后立刻不再生效">
          <input type="checkbox" data-act="toggle" ${p.enabled ? 'checked' : ''} />
          <span class="small">启用</span>
        </label>
        <button class="btn sm sec" data-act="edit">编辑</button>
        <button class="btn sm sec" data-act="up">↑ 上移</button>
        <button class="btn sm sec" data-act="down">↓ 下移</button>
        <button class="btn sm sec" data-act="del" style="color:var(--danger)">删除</button>
      </div>
    </div>`;
}

function bindToolbar(root, signal) {
  $('#btn-install', root)?.addEventListener('click', () => openInstall(root));
  $('#btn-new-plugin', root)?.addEventListener('click', () => openEditor(root, null));
}

function bindActions(root, signal) {
  root.addEventListener(
    'click',
    async (e) => {
      // 内置示例目录的「添加」按钮（用 data-catalog-add，避免与插件的 data-act 混在一起）
      const addBtn = e.target.closest('button[data-catalog-add]');
      if (addBtn) {
        // ★ 同一个按钮承担"添加"与"更新"两件事（接口也是同一个）：
        //   插件内容是**添加时拷贝**的，内置主题升级之后用户那份不会自动变 ——
        //   目录里标了"有更新"时，这个按钮就是「更新到最新」。
        const key = addBtn.dataset.catalogAdd;
        const item = state.catalog.find((x) => x.key === key);
        const isUpdate = Boolean(item?.update_available);
        const restoreAdd = buttonLoading(addBtn, isUpdate ? '更新中' : '添加中');
        try {
          const saved = await api.post(`/plugins/catalog/${key}`, {});
          toastOk(isUpdate ? `「${saved.name}」已更新到最新` : `已添加「${saved.name}」`);
          await applyPluginTheme();
          await renderList(root, activeSignal?.signal);
        } catch (err) {
          toastErr(err.toDisplay ? err.toDisplay() : String(err));
        } finally {
          restoreAdd();
        }
        return;
      }

      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      // ★ 归属校验（纵深防御）：插件 / 预设 / 角色卡 / 世界书的卡片都是 `.cc`，
      //   光看 class 分不出属于哪一页 —— 而"内置示例"那一块同样是 `.cc` 卡片。
      //   列表容器带 data-view，认不出来的按钮放行给真正的主人。
      if (!btn.closest('[data-view="plugins"]')) return;
      const cardEl = btn.closest('.cc');
      if (!cardEl) return;
      const id = Number(cardEl.dataset.id);
      const isStale = () => activeSignal?.signal.aborted ?? true;
      const restore = buttonLoading(btn);
      try {
        const act = btn.dataset.act;
        if (act === 'edit') {
          const row = state.items.find((x) => x.id === id);
          restore();
          openEditor(root, row);
        } else if (act === 'del') {
          restore();
          await removePlugin(root, id);
        } else if (act === 'up' || act === 'down') {
          restore();
          await movePlugin(root, id, act === 'up' ? -1 : 1);
        }
      } catch (err) {
        if (!isStale()) toastErr(err.toDisplay ? err.toDisplay() : String(err));
      } finally {
        restore();
      }
    },
    { signal },
  );

  // 启停用 change 事件（放在委托里，跟着同一批信号失效）
  root.addEventListener(
    'change',
    async (e) => {
      const box = e.target.closest('input[data-act="toggle"]');
      if (!box) return;
      const cardEl = box.closest('.cc');
      if (!cardEl) return;
      try {
        const updated = await api.patch(`/plugins/${Number(cardEl.dataset.id)}`, {
          enabled: box.checked,
        });
        toastOk(`「${updated.name}」已${box.checked ? '启用' : '停用'}`);
        // CSS 插件启停后主题要立刻重算
        await applyPluginTheme();
        await renderList(root, activeSignal?.signal);
      } catch (err) {
        box.checked = !box.checked;
        toastErr(err.toDisplay ? err.toDisplay() : String(err));
      }
    },
    { signal },
  );
}

/* ==================================================================
   增删改
   ================================================================== */
async function removePlugin(root, id) {
  const row = state.items.find((x) => x.id === id);
  const ok = await confirmDialog({
    title: '删除插件',
    message: `确定删除「${row?.name || id}」？删除后不再生效。`,
    confirmText: '删除',
    danger: true,
  });
  if (!ok) return;
  await api.del(`/plugins/${id}`);
  toastOk('插件已删除');
  await applyPluginTheme();
  await renderList(root, activeSignal?.signal);
}

async function movePlugin(root, id, delta) {
  const items = [...state.items];
  const index = items.findIndex((x) => x.id === id);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= items.length) {
    toastErr(delta < 0 ? '已经是第一个了' : '已经是最后一个了');
    return;
  }
  // ★ 交换"顺序值"而不是重排整列：少一次批量写，也不会因为并发点击把顺序搞乱。
  //   两端顺序值相同时（默认插件都是 100/110）用下标兜底，保证一定能换动。
  const a = items[index];
  const b = items[target];
  const pa = a.priority;
  const pb = b.priority;
  const newA = pa === pb ? (delta < 0 ? pb - 1 : pb + 1) : pb;
  const newB = pa === pb ? pb : pa;
  await api.patch(`/plugins/${a.id}`, { priority: newA });
  await api.patch(`/plugins/${b.id}`, { priority: newB });
  await applyPluginTheme();
  await renderList(root, activeSignal?.signal);
}

function configFieldsHTML(kind, config) {
  const c = config || {};
  if (kind === 'regex') {
    const rules = Array.isArray(c.rules) ? c.rules : [];
    return `
      <div class="form-section">
        <div class="hint mb8">
          按顺序对**发给模型的提示词**做正则替换（正文/历史/系统提示词都算）。
          不改数据库、不改界面显示 —— 你在会话里看到的仍然是原话。
        </div>
        <div id="rule-box">
          ${rules.map((r, i) => ruleRowHTML(r, i)).join('')}
        </div>
        <button class="btn sec sm" type="button" id="btn-add-rule">+ 添加规则</button>
        <div class="hint mt8">
          例：pattern <span class="mono">你</span>、replacement <span class="mono">阁下</span>、
          flags 留空（i=忽略大小写，m=多行，s=点号匹配换行）。
        </div>
      </div>`;
  }
  if (kind === 'prompt') {
    const position = String(c.position || 'end');
    return `
      <div class="form-section">
        <div class="row">
          <div>
            <label class="small muted">注入位置</label>
            <select name="position">
              ${Object.entries(POSITION_LABEL)
                .map(
                  ([value, label]) =>
                    `<option value="${value}" ${position === value ? 'selected' : ''}>${esc(label)}</option>`,
                )
                .join('')}
            </select>
          </div>
        </div>
        <div class="mt8">
          <label class="small muted">注入内容（会追加进系统提示词）</label>
          <textarea name="content" rows="5" placeholder="例如：每轮回复控制在 200 字以内，多用短句。">${esc(c.content || '')}</textarea>
        </div>
        <div class="hint mt8">越靠后的位置对模型影响力越大；「守卫之前」需要当前装配里有内置守卫块。</div>
      </div>`;
  }
  if (kind === 'dice') {
    const triggers = (Array.isArray(c.triggers) ? c.triggers : ['/r', '/roll', '掷骰', '投掷']).join(' ');
    const on = (value, fallback = true) => (value === undefined ? fallback : Boolean(value));
    return `
      <div class="form-section">
        <div class="hint mb8">
          ★ 大模型**没有真随机**（需要成功就写 18、需要失败就写 3），
          所以骰点一律由**后端**掷出：自己在输入框里写
          <span class="mono">/r 1d20+5</span>，或让模型写
          <span class="mono">&lt;roll&gt;1d20&lt;/roll&gt;</span> 请求系统掷。
          支持 <span class="mono">2d6+3</span>、<span class="mono">4d6kh3</span>（取高）、
          <span class="mono">4d6kl1</span>（取低）、<span class="mono">2d6!</span>（爆炸骰）、
          括号与 <span class="mono">1d20+5&lt;=15</span> 成功判定。
        </div>
        <div class="row">
          <div style="flex:2">
            <label class="small muted">触发词（空格分隔，行首生效）</label>
            <input type="text" name="triggers" value="${esc(triggers)}" placeholder="/r /roll 掷骰" />
          </div>
          <div style="flex:1">
            <label class="small muted">默认表达式（只写触发词时用）</label>
            <input type="text" name="default_expr" value="${esc(c.default_expr || '1d100')}" placeholder="1d100" />
          </div>
        </div>
        <div class="row mt8">
          <div style="flex:1">
            <label class="small muted">单次最多骰子数</label>
            <input type="number" name="max_dice" min="1" max="1000" value="${esc(c.max_dice ?? 100)}" />
          </div>
          <div style="flex:1">
            <label class="small muted">单颗最多面数</label>
            <input type="number" name="max_sides" min="2" max="100000" value="${esc(c.max_sides ?? 1000)}" />
          </div>
        </div>
        <label class="row mt8" style="align-items:center;gap:8px">
          <input type="checkbox" name="allow_model_roll" ${on(c.allow_model_roll) ? 'checked' : ''} />
          <span>允许模型用 <span class="mono">&lt;roll&gt;</span> 请求掷骰（关掉后只有玩家能掷）</span>
        </label>
        <label class="row mt8" style="align-items:center;gap:8px">
          <input type="checkbox" name="show_detail" ${on(c.show_detail) ? 'checked' : ''} />
          <span>显示每颗骰子的点数（关掉只给总和）</span>
        </label>
        <label class="row mt8" style="align-items:center;gap:8px">
          <input type="checkbox" name="explain" ${on(c.explain) ? 'checked' : ''} />
          <span>把掷骰规则写进系统提示词（关掉 = 静默掷骰，模型不知道有骰子）</span>
        </label>
      </div>`;
  }
  return `
    <div class="form-section">
      <div class="hint mb8">
        纯样式，注入控制台的 &lt;style&gt;。已挡掉 <span class="mono">@import</span>、
        <span class="mono">expression()</span>、<span class="mono">javascript:</span>
        以及 <span class="mono">&lt;/style&gt;</span> 逃逸。
      </div>
      <textarea name="css" rows="10" class="mono" placeholder=":root { --brand: #7c3aed; }">${esc(c.css || '')}</textarea>
    </div>`;
}

function ruleRowHTML(rule, index) {
  return `
    <div class="row" data-rule="${index}" style="margin-bottom:6px">
      <div style="flex:3"><input type="text" data-k="pattern" value="${esc(rule.pattern || '')}" placeholder="正则，如 你" /></div>
      <div style="flex:3"><input type="text" data-k="replacement" value="${esc(rule.replacement || '')}" placeholder="替换成，如 阁下" /></div>
      <div style="flex:1"><input type="text" data-k="flags" value="${esc(rule.flags || '')}" placeholder="i" /></div>
      <div style="flex:0 0 auto"><button class="btn sm sec" type="button" data-remove-rule="${index}">删</button></div>
    </div>`;
}

function readConfig(root, kind) {
  if (kind === 'regex') {
    const rules = $$('#rule-box [data-rule]', root)
      .map((row) => ({
        pattern: row.querySelector('[data-k=pattern]').value,
        replacement: row.querySelector('[data-k=replacement]').value,
        flags: row.querySelector('[data-k=flags]').value.trim(),
      }))
      .filter((r) => r.pattern.trim());
    return { rules };
  }
  if (kind === 'prompt') {
    return {
      position: $('[name=position]', root)?.value || 'end',
      content: $('[name=content]', root)?.value || '',
    };
  }
  if (kind === 'dice') {
    // 触发词按空白切分；后端还会再校验一次（空串会在那里被拦下并给出中文原因）
    const triggers = ($('[name=triggers]', root)?.value || '')
      .split(/\s+/)
      .map((t) => t.trim())
      .filter(Boolean);
    return {
      triggers,
      default_expr: $('[name=default_expr]', root)?.value?.trim() || '1d100',
      allow_model_roll: Boolean($('[name=allow_model_roll]', root)?.checked),
      show_detail: Boolean($('[name=show_detail]', root)?.checked),
      explain: Boolean($('[name=explain]', root)?.checked),
      max_dice: Number($('[name=max_dice]', root)?.value || 100),
      max_sides: Number($('[name=max_sides]', root)?.value || 1000),
    };
  }
  return { css: $('[name=css]', root)?.value || '' };
}

/** 新建 / 编辑插件 */
function openEditor(root, existing) {
  const isEdit = Boolean(existing);
  let kind = existing?.kind || 'regex';

  const m = modal({
    title: isEdit ? `编辑插件 · ${existing.name}` : '新建插件',
    width: 'wide',
    bodyHTML: `
      <div class="grid" style="grid-template-columns:1fr 1fr;gap:18px">
        ${textField('插件名', 'name', existing?.name || '', { placeholder: '如「统一称呼为阁下」' })}
        <div>
          <label class="small muted">类型</label>
          <select name="kind" ${isEdit ? 'disabled' : ''}>
            ${Object.entries(KIND_LABEL)
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${kind === value ? 'selected' : ''}>${esc(label)}</option>`,
              )
              .join('')}
          </select>
          ${isEdit ? '<div class="hint">类型建好之后不能改（改了配置结构就对不上了）</div>' : ''}
        </div>
      </div>
      ${textField('说明（可选）', 'description', existing?.description || '', {})}
      <div id="config-box">${configFieldsHTML(kind, existing?.config)}</div>
      <div class="hint mt8">顺序值越小越先应用（默认 100）。</div>
      <div class="row" style="max-width:200px">
        <div>
          <label class="small muted">顺序</label>
          <input type="number" name="priority" min="0" max="10000" value="${existing?.priority ?? 100}" />
        </div>
      </div>`,
    footHTML: `<button class="btn sec" data-close>取消</button>
               <button class="btn" id="btn-save-plugin">${isEdit ? '保存' : '创建'}</button>`,
  });

  const kindSelect = $('[name=kind]', m.root);
  if (!isEdit && kindSelect) {
    kindSelect.addEventListener('change', () => {
      kind = kindSelect.value;
      $('#config-box', m.root).innerHTML = configFieldsHTML(kind, {});
      bindRuleBox(m.root);
    });
  }
  bindRuleBox(m.root);

  function bindRuleBox(scope) {
    $('#btn-add-rule', scope)?.addEventListener('click', () => {
      const box = $('#rule-box', scope);
      const index = box.children.length;
      box.insertAdjacentHTML('beforeend', ruleRowHTML({}, index));
      bindRemove(scope);
    });
    bindRemove(scope);
  }
  function bindRemove(scope) {
    $$('[data-remove-rule]', scope).forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', () => {
        btn.closest('[data-rule]').remove();
      });
    });
  }

  $('#btn-save-plugin', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '保存中');
    try {
      if (!isEdit) kind = $('[name=kind]', m.root).value;
      const body = {
        name: $('[name=name]', m.root).value.trim(),
        description: $('[name=description]', m.root).value.trim() || null,
        priority: Number($('[name=priority]', m.root).value) || 0,
        config: readConfig(m.root, kind),
      };
      if (!body.name) throw new Error('请填插件名');
      if (isEdit) {
        await api.patch(`/plugins/${existing.id}`, body);
        toastOk('插件已保存');
      } else {
        await api.post('/plugins', { ...body, kind, enabled: true });
        toastOk('插件已创建');
      }
      m.close();
      await applyPluginTheme();
      await renderList(root, activeSignal?.signal);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

/** 从 GitHub 安装 */
function openInstall(root) {
  const m = modal({
    title: '从 GitHub 安装插件',
    bodyHTML: `
      <div class="alert info">
        只接受 GitHub 的 https 地址，网页地址会自动转成 raw 地址（<b>gist 也可以</b>，写插件不必建仓库）：<br />
        <span class="mono small">https://github.com/用户/仓库/blob/main/plugins/foo.json</span><br />
        <span class="mono small">https://raw.githubusercontent.com/用户/仓库/main/plugins/foo.json</span><br />
        <span class="mono small">https://gist.github.com/用户/gist编号</span>（多文件时加
        <span class="mono small">?file=foo.json</span>）
      </div>
      ${textField('插件清单地址', 'url', '', { placeholder: 'https://github.com/…/blob/main/xxx.json' })}
      <div class="hint mt8">
        清单是一个 JSON：<span class="mono">{ "spec": "hne_plugin_v1", "name": "…",
        "kind": "regex|prompt|css", "config": { … } }</span>。
        下载体积 / 规则条数都有上限，清单非法时会明确报错且不会留下半装状态。
      </div>
      <div id="install-result" class="mt8"></div>`,
    footHTML: `<button class="btn sec" data-close>关闭</button>
               <button class="btn" id="btn-do-install">安装</button>`,
  });

  $('#btn-do-install', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '安装中');
    const box = $('#install-result', m.root);
    try {
      const url = $('[name=url]', m.root).value.trim();
      if (!url) throw new Error('请填插件地址');
      const res = await api.post('/plugins/install', { url });
      box.innerHTML = `<div class="alert ok">✓ 已安装：<b>${esc(res.plugin.name)}</b>
        （${esc(KIND_LABEL[res.plugin.kind] || res.plugin.kind)}，${esc(res.plugin.summary)}）
        <div class="small faint">来源：${esc(res.source_url)}（${res.fetched_bytes} 字节）</div></div>`;
      toastOk(`插件「${res.plugin.name}」已安装`);
      await applyPluginTheme();
      await renderList(root, activeSignal?.signal);
    } catch (err) {
      box.innerHTML = `<div class="alert danger" style="white-space:pre-wrap">${esc(
        err.toDisplay ? err.toDisplay() : String(err),
      )}</div>`;
    } finally {
      restore();
    }
  });
}
