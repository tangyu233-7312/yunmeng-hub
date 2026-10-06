/* ============================================================
   模型配置（Provider）视图

   用户可以在这里配任意厂商的大模型端点，并调整生成参数。
   重点是让参数"看得懂"：
     · 最大输出 token 明确标注「包含思考过程」
     · 实时算出上下文预算（能塞多少历史进提示词）
     · 把后端的 warnings / hints 原样展示出来
   ============================================================ */

import { api } from 'hne/api';
import {
  $,
  $$,
  confirmDialog,
  buttonLoading,
  checkboxField,
  esc,
  fmtRelative,
  modal,
  mount,
  mountError,
  numberField,
  rangeField,
  selectField,
  textField,
  toastErr,
  toastOk,
} from 'hne/ui';

const REASONING_OPTIONS = [
  { value: 'auto', label: 'auto —— 不干预（最安全，推荐）' },
  { value: 'off', label: 'off —— 尽量关闭思考' },
  { value: 'low', label: 'low —— 轻度思考' },
  { value: 'medium', label: 'medium —— 中度思考' },
  { value: 'high', label: 'high —— 深度思考' },
];

const PROVIDER_TYPES = [
  {
    value: 'openai_compatible',
    label: 'openai_compatible —— DeepSeek / 通义 / Kimi / Ollama / vLLM …',
  },
  { value: 'anthropic', label: 'anthropic —— Claude Messages 协议' },
];

/**
 * 常见厂商的 Base URL 与模型名写法。
 *
 * ★ 为什么需要这个？
 *   实测最多的连通失败原因**不是** Key 错，而是 Base URL 少写/多写了版本段：
 *   本项目的请求地址是 `{base_url}/chat/completions`，
 *   所以 base_url 必须正好停在版本目录上，例如
 *       https://api.deepseek.com/v1   → …/v1/chat/completions  ✓
 *       https://api.deepseek.com      → …/chat/completions     ✓（官方兼容）
 *       https://ark.cn-beijing.volces.com/api/v3  → …/api/v3/chat/completions ✓
 *   而少写一段（如只写域名）或把完整路径粘进来，就会得到
 *   "model not found / 无权访问" 这类**误导性的报错** —— 因为对方网关
 *   根本没走到模型那一步。这里把正确写法直接摆出来，比让人猜便宜得多。
 */
const BASE_URL_PRESETS = [
  {
    name: 'DeepSeek',
    base_url: 'https://api.deepseek.com/v1',
    model: 'deepseek-chat（或 deepseek-reasoner / deepseek-flash）',
  },
  {
    name: '火山方舟（豆包）',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    model: '在方舟控制台「开通管理」里看到的模型 ID，或推理接入点 ID（ep-…）',
  },
  {
    name: '阿里云百炼（通义）',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: 'qwen-plus / qwen-max …',
  },
  {
    name: '月之暗面 Kimi',
    base_url: 'https://api.moonshot.cn/v1',
    model: 'moonshot-v1-8k …',
  },
  {
    name: '智谱 GLM',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    model: 'glm-4-plus …',
  },
  {
    name: '硅基流动',
    base_url: 'https://api.siliconflow.cn/v1',
    model: 'deepseek-ai/DeepSeek-V3 …',
  },
  {
    name: '本机 Ollama',
    base_url: 'http://127.0.0.1:11434/v1',
    model: 'qwen2.5:7b（本地模型，不需要 Key）',
  },
];

/**
 * 把用户粘进来的「完整接口地址」收拾成 base_url。
 *
 * ★ 为什么要在前端做这件事（后端也会拦）？
 *   最常见的填错方式不是敲错域名，而是**从厂商文档里整条复制**：
 *       https://ark.cn-beijing.volces.com/api/v3/chat/completions
 *   而本项目的请求地址 = base_url + /chat/completions，
 *   于是会打到 .../chat/completions/chat/completions，对方返回
 *   「模型不存在」这种**指向完全错误**的报错（用户真实踩过）。
 *   与其存下一个必然失效的地址再让他猜，不如提交前就削掉多出来的那一段，
 *   并在界面上说明"我帮你改成了什么"。
 *
 * 只削**已知**的接口结尾，避免误伤 /compatible-mode/v1 这类正常路径。
 */
const ENDPOINT_TAILS = [
  '/chat/completions',
  '/completions',
  '/responses',
  '/messages',
  '/chat',
];

function normalizeBaseUrl(raw) {
  let url = String(raw || '').trim().replace(/\/+$/, '');
  if (!url) return { url: '', changed: false };
  const lower = url.toLowerCase();
  for (const tail of ENDPOINT_TAILS) {
    if (lower.endsWith(tail)) {
      return { url: url.slice(0, -tail.length).replace(/\/+$/, ''), changed: true, removed: tail };
    }
  }
  return { url, changed: false };
}

/** 把测试结果渲染成一段能直接照着改的说明。 */function testResultHTML(res) {
  const d = res.detail || {};
  const rows = [];
  if (d.endpoint) {
    rows.push(`<div><b>实际请求地址</b><div class="mono small">${esc(d.endpoint)}</div></div>`);
  }
  if (d.http_status) rows.push(`<div><b>上游 HTTP 状态</b> ${esc(String(d.http_status))}</div>`);
  if (d.upstream_error_type) {
    rows.push(`<div><b>上游错误类型</b> <span class="mono">${esc(d.upstream_error_type)}</span></div>`);
  }
  if (d.upstream_message) {
    rows.push(
      `<div><b>上游原话</b><div class="small mono">${esc(d.upstream_message)}</div></div>`,
    );
  }
  if (d.raw_body_preview && d.raw_body_preview !== d.upstream_message) {
    rows.push(
      `<div><b>上游原始响应</b><div class="small mono">${esc(String(d.raw_body_preview).slice(0, 500))}</div></div>`,
    );
  }
  if (d.sample_reply) {
    rows.push(`<div><b>模型回复</b> ${esc(d.sample_reply)}</div>`);
  }

  const fixHint = resolveFixHint(d, res);
  return `
    <div class="alert ${res.ok ? 'ok' : 'danger'}">
      ${res.ok ? '✓ 连接正常' : '✗ 连接失败'}（${res.latency_ms} ms）
      <div class="mt8">${esc(res.message || '')}</div>
    </div>
    ${rows.length ? `<div class="prompt-view small">${rows.join('<hr style="border:none;border-top:1px solid #33404d;margin:8px 0">')}</div>` : ''}
    ${fixHint ? `<div class="alert warn mt8">${fixHint}</div>` : ''}`;
}

/** 按错误类型给出"下一步该改什么"，而不是让用户对着英文报错发呆。 */
function resolveFixHint(d, res) {
  const failed = !(res && res.ok);
  // ★★ 只有在**测试失败**时才给排查建议。
  //
  //   为什么必须显式判 `ok`（实测抓到的 bug）：这个函数原来只看 `http_status`，
  //   而**成功的**测试结果里根本没有这个字段（它不是错误详情的一部分）。
  //   于是 `Number(undefined || 0)` 得到 0，命中下面那条"网络层没通"分支 ——
  //   界面就出现了自相矛盾的一幕：上面绿条写着「✓ 连接正常（1068 ms）」、
  //   下面紧跟着一条红色警告「网络层没通」。
  //   ★ 教训：**"字段缺失"和"字段等于默认值"是两件事**，别用 `||` 把它们揉成一个。
  if (!failed) return '';

  const status = Number(d.http_status ?? 0);
  const text = `${d.upstream_message || ''} ${d.raw_body_preview || ''}`;
  if (status === 404 || /not\s*found|不存在|unsupported\s*model/i.test(text)) {
    return `排查顺序：① 先点「模型」按钮拉一次可用模型列表 —— 拉不到说明 <b>Base URL</b> 不对；
      ② 列表里有模型但名字对不上，说明<b>模型名</b>写错了（方舟等平台要用控制台里的模型 ID 或接入点 ep-…）；
      ③ 都对则可能是<b>该账号没有开通这个模型</b>。`;
  }
  if (status === 401 || status === 403) {
    return '鉴权失败：确认 API Key 是这家厂商的（不要混用别家的 Key），以及该 Key 有权限访问这个模型。';
  }
  if (status === 400) {
    return '上游认为请求参数不合法：最常见的是「上下文窗口」填得比模型实际支持的还大，或模型名不属于该端点。';
  }
  // ★ 只有**确实没拿到 HTTP 状态**（网络层异常）才说"网络层没通"。
  //   注意：这里不再用 `|| /connect|dns|timeout/` 去猜文本 ——
  //   成功时的 message 也可能含这些词，靠文本猜是上一版误报的根源之一。
  if (d.exception_type || status === 0) {
    return '网络层没通：检查 Base URL 域名是否写错、是否需要代理、本机能否访问该地址。';
  }
  return '';
}

/** 前端也算一遍上下文预算，用于表单里实时预览（以后端返回的为准） */
function calcBudget(contextWindow, maxTokens) {
  const cw = Number(contextWindow) || 0;
  const mt = Number(maxTokens) || 0;
  const margin = Math.max(128, Math.round(cw * 0.05));
  return { contextWindow: cw, maxTokens: mt, margin, input: cw - mt - margin };
}

/* ==================================================================
   列表
   ================================================================== */

/**
 * 本视图的监听器与刷新状态。
 *
 * ★ 为什么要 `refreshing` 这个闸门（真实事故：用户反馈"点一下弹出好几个一样的窗"）：
 *   本视图的操作（测试 / 删除 / 保存）结束后都会刷新列表，
 *   而刷新会重画 `#view` 的内容、也就**重新绑一次委托监听器**。
 *   只要有两条路径先后触发刷新，监听器就会叠加成 2 个、4 个……
 *   点一次按钮就被分发 N 次 → 弹出 N 个一模一样的弹窗。
 *
 *   所以：① `refreshing` 保证同一时刻只有一次刷新在跑（重复调用直接忽略）；
 *        ② `controller` 保证每次真正刷新都换新的信号，旧的监听器立刻被 abort。
 */
const renderState = {
  controller: null,
  signal: null,
  refreshing: false,
};

export async function renderProviders(root, ctx = {}) {
  // 新一批（进入本页 / 切回来）：先摘掉上一批监听器，再绑新的
  renderState.controller = new AbortController();
  if (ctx.signal) {
    if (ctx.signal.aborted) renderState.controller.abort();
    else ctx.signal.addEventListener('abort', () => renderState.controller.abort(), { once: true });
  }
  renderState.signal = renderState.controller.signal;
  renderState.refreshing = false;
  attachProviderListeners(root, renderState.signal);
  await refreshProviderList(root);
}

/** 刷新列表（带并发保护）：重复调用会被忽略，因此不会重复重画、也不会叠加监听器。 */
async function refreshProviderList(root) {
  // ★ 这道闸门是修"点一下弹 N 个窗"的关键：
  //   并发/连续两次刷新会各自重画一次 DOM，而重画前的监听器绑定会让
  //   委托监听器叠加 —— 点一次按钮就被分发 N 次、弹出 N 个一样的窗。
  if (renderState.refreshing) return;
  renderState.refreshing = true;
  try {
    await renderProviderList(root, renderState.signal);
  } finally {
    renderState.refreshing = false;
  }
}

/** 只重新拉列表并重画，**不重新绑监听器**（监听器是委托在 root 上的，一直在）。 */
async function renderProviderList(root, signal) {
  // ★ 先确认"我还是当前这一批"再动 DOM（同 cards/books/presets：
  //   旧批次回调先画「加载中…」再因信号失效 return，会让整页卡在加载中）
  const isCurrent = () => renderState.signal === signal && !signal?.aborted;
  if (!isCurrent()) return;
  mount(root, `<div class="empty"><div class="big">⏳</div><div>加载中…</div></div>`);

  let list;
  try {
    list = await api.get('/providers', undefined, { timeoutMs: 20000 });
  } catch (err) {
    if (!isCurrent()) return; // 已经切走了，别用旧数据覆盖新页面
    mountError(root, err.toDisplay ? err.toDisplay() : String(err), () =>
      refreshProviderList(root),
    );
    return;
  }
  if (!isCurrent()) return;

  const rows = list
    .map((p) => {
      const keyCell = p.has_api_key
        ? `<span class="mono small">${esc(p.api_key_masked)}</span>
           ${p.api_key_decryptable ? '' : '<div class="small" style="color:var(--danger)">无法解密，请重填</div>'}`
        : '<span class="badge neutral">未配置</span>';

      const testCell =
        p.last_test_ok === true
          ? `<span class="badge ok">连通正常</span><div class="small faint">${esc(fmtRelative(p.last_tested_at))}</div>`
          : p.last_test_ok === false
            ? `<span class="badge danger">连通失败</span><div class="small faint">${esc(fmtRelative(p.last_tested_at))}</div>`
            : '<span class="badge neutral">未测试</span>';

      return `
      <tr data-id="${p.id}">
        <td>
          <div style="font-weight:600">${esc(p.name)}
            ${p.is_default ? ' <span class="badge info">默认</span>' : ''}
            ${p.is_active ? '' : ' <span class="badge neutral">已停用</span>'}
          </div>
          <div class="small faint mono">${esc(p.provider_type)}</div>
        </td>
        <td>
          <div class="mono small">${esc(p.model_name)}</div>
          <div class="small faint mono" style="max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
               title="${esc(p.base_url)}">${esc(p.base_url)}</div>
        </td>
        <td>${keyCell}</td>
        <td class="small">
          <div>温度 ${esc(p.generation.temperature)}</div>
          <div>输出 ${esc(p.generation.max_tokens)} <span class="faint">(含思考)</span></div>
          <div>思考 ${esc(p.generation.reasoning_effort)}</div>
        </td>
        <td class="small">${testCell}</td>
        <td class="nowrap">
          <button class="btn sm sec" data-act="test">测试</button>
          <button class="btn sm sec" data-act="models">模型</button>
          <button class="btn sm sec" data-act="edit">编辑</button>
          <button class="btn sm sec" data-act="del" style="color:var(--danger)">删除</button>
        </td>
      </tr>`;
    })
    .join('');

  mount(
    root,
    `
    <div class="page-head">
      <div>
        <h1>模型配置</h1>
        <div class="sub">
          配置任意厂商的大模型端点。密钥用 Fernet 加密后入库，响应里只回显脱敏形式（如 sk-s****3456）。
        </div>
      </div>
      <div class="page-actions"><button class="btn" id="btn-new">+ 新增配置</button></div>
    </div>
    ${
      list.length
        ? `<div class="panel" style="padding:0;overflow:hidden">
             <table class="list" data-view="providers">
               <thead><tr>
                 <th style="width:17%">配置名 / 协议</th>
                 <th style="width:20%">模型 / 地址</th>
                 <th style="width:14%">密钥</th>
                 <th style="width:16%">生成参数</th>
                 <th style="width:14%">诊断状态</th>
                 <th style="width:19%">操作</th>
               </tr></thead>
               <tbody>${rows}</tbody>
             </table>
           </div>`
        : `<div class="empty">
             <div class="big">🔌</div>
             <div><strong>还没有任何模型配置</strong></div>
             <div class="small mt8">点右上角「新增配置」开始。建议先用表单里的「试连」确认能通，再保存。</div>
           </div>`
    }`,
  );

  $('#btn-new', root).addEventListener('click', () => openForm(root, null));
}

/**
 * 绑定委托监听器（**每次进入本页只绑一次**，与列表刷新无关）。
 *
 * ★ 这就是修"点一下弹 N 个窗"的关键：刷新列表只重画内容，绝不重绑监听器。
 */
function attachProviderListeners(root, signal) {
  // ★ 一定要带 { signal }：这个委托监听器挂在常驻的 #view 上，
  //   不随 innerHTML 替换而消失，离开本页时必须由 signal 摘掉。
  root.addEventListener(
    'click',
    async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      // ★ 归属校验（纵深防御）：模型列表的按钮必须落在本页自己那张表里。
      //   委托挂在常驻 #view 上，只要有残留监听器，别的页面的 data-act 按钮
      //   就会打进来（事故背景见 presets.js 里 nextController 的注释）。
      if (!btn.closest('[data-view="providers"]')) return;
      const tr = btn.closest('tr');
      if (!tr) return; // 不是列表里的按钮，交回给别处处理
      const id = Number(tr.dataset.id);
      // ★ 判断"还活着吗"认**当前**批次：刷新会换信号，用旧信号判断会让按钮"点了没反应"
      const isStale = () => renderState.signal?.aborted ?? true;
      const restore = buttonLoading(btn);

      try {
        const act = btn.dataset.act;

        if (act === 'test') {
          const res = await api.post(`/providers/${id}/test`);
          if (isStale()) return;
          // ★ 失败时不再只弹一句 toast：把「实际请求地址 / 上游 HTTP 状态 / 上游原话」
          //   全部摆出来，并给出排查顺序。接不通第三方模型时，光看一句
          //   "指定的模型不存在" 根本无从下手（用户实际反馈过这个问题）。
          openTestDialog(res);          await refreshProviderList(root);
        } else if (act === 'models') {
          const res = await api.get(`/providers/${id}/models`);
          if (isStale()) return;
          restore();
          openModelsDialog(res);
        } else if (act === 'edit') {
          const detail = await api.get(`/providers/${id}`);
          if (isStale()) return;
          restore();
          openForm(root, detail);
        } else if (act === 'del') {
          const detail = await api.get(`/providers/${id}`);
          if (isStale()) return;
          restore();
          const go = await confirmDialog({
            title: '删除模型配置',
            message:
              `确定删除「${detail.name}」？\n\n` +
              '用这个配置开过的叙事会话不会被删除，只是不再关联模型（外键置空）。',
            confirmText: '删除',
            danger: true,
          });
          if (!go || isStale()) return;
          await api.del(`/providers/${id}`);
          toastOk('配置已删除');
          await refreshProviderList(root);
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
   新增 / 编辑表单
   ================================================================== */
function openForm(root, existing) {
  const isEdit = Boolean(existing);
  const g = existing?.generation || {
    temperature: 0.8,
    max_tokens: 2048,
    top_p: null,
    reasoning_effort: 'auto',
  };

  const m = modal({
    title: isEdit ? `编辑配置 · ${existing.name}` : '新增模型配置',
    width: 'wide',
    bodyHTML: `
      <div class="grid" style="grid-template-columns:1fr 1fr;gap:22px">
        <div>
          <h3 style="margin-top:0;font-size:13px;color:var(--text-dim)">连接信息</h3>
          ${textField('配置别名', 'name', existing?.name || '', {
            placeholder: '如「我的 DeepSeek」',
            hint: '仅用于你自己区分；同一用户下不能重名',
          })}
          ${selectField(
            '协议类型',
            'provider_type',
            PROVIDER_TYPES,
            existing?.provider_type || 'openai_compatible',
            '决定用哪套请求格式。协议差异全部由适配层吸收，上层代码零感知。',
          )}
          ${textField('API 地址', 'base_url', existing?.base_url || 'https://api.deepseek.com', {
            hint:
              '★ 必须正好填到<b>版本目录</b>为止（本项目的请求地址 = 这个地址 + /chat/completions）。' +
              '<br>少填一段或把完整路径粘进来，都会得到「模型不存在」这类<b>误导性</b>报错。',
          })}
          <div class="field" style="margin-top:-6px">
            <div class="hint">
              常见写法（点一下直接填入）：
              <div class="row tight mt8" style="flex-wrap:wrap;gap:6px" id="url-presets">
                ${BASE_URL_PRESETS.map(
                  (p, i) =>
                    `<button type="button" class="btn sec sm" data-preset="${i}"
                       title="Base URL：${esc(p.base_url)}&#10;模型名：${esc(p.model)}">${esc(p.name)}</button>`,
                ).join('')}
              </div>
              <div class="mt8" id="preset-detail"></div>
            </div>
          </div>
          ${textField('API 密钥', 'api_key', '', {
            type: 'password',
            placeholder: isEdit ? '留空 = 不修改原密钥' : 'sk-...',
            hint: isEdit
              ? `当前：${esc(existing.api_key_masked || '未配置')}　·　留空保持不变`
              : '本地部署（Ollama / vLLM）可以留空',
          })}
          ${isEdit ? checkboxField('清空密钥（改用无鉴权的本地部署时才用）', 'clear_api_key', false) : ''}
          ${textField('模型名', 'model_name', existing?.model_name || 'deepseek-flash', {
            hint: '不确定就先用列表页的「模型」按钮拉取一遍',
          })}
        </div>

        <div>
          <h3 style="margin-top:0;font-size:13px;color:var(--text-dim)">生成参数</h3>
          ${rangeField('温度 temperature', 'temperature', g.temperature, {
            min: 0,
            max: 2,
            step: 0.05,
            hint: '越低越稳定保守，越高越有创造性。叙事场景建议 0.7 ~ 1.0',
          })}
          ${numberField('最大输出 token', 'max_tokens', g.max_tokens, {
            min: 1,
            max: 131072,
            hint:
              '<b style="color:var(--warn)">★ 这个数字包含思考过程的 token，不只是正文字数。</b>' +
              '<br>推理模型会先"想"很久，配额给小了会一个字正文都出不来（接口还返回成功）。建议 ≥ 2048。',
          })}
          ${selectField(
            '思考强度',
            'reasoning_effort',
            REASONING_OPTIONS,
            g.reasoning_effort,
            'auto 表示不发送该字段（最安全）。设了具体档位后，适配器会把它翻译成厂商字段；'
              + '若该模型不认识这个参数，服务端会拒绝，本应用会自动去掉它重试一次并在回复里说明。',
          )}
          ${textField('上下文窗口', 'context_window', existing?.context_window ?? 65536, {
            type: 'number',
            hint: '模型能容纳的输入+输出总 token 数，按厂商文档填',
          })}
          ${textField('top_p（可留空）', 'top_p', g.top_p ?? '', {
            hint: '与温度二选一调整即可，同时调容易互相干扰',
          })}
          <div class="row">
            ${checkboxField('设为默认模型', 'is_default', existing?.is_default ?? false)}
            ${checkboxField('启用', 'is_active', existing?.is_active ?? true)}
          </div>
        </div>
      </div>

      <div class="form-section">
        <h3>上下文预算预览（实时）</h3>
        <div id="budget-preview"></div>
      </div>

      <div class="form-section">
        <h3>可靠性与传输方式</h3>
        ${checkboxField('流式传输（推荐开启）', 'stream_enabled', existing?.stream_enabled ?? true)}
        <div class="hint">
          ★ 有些模型或中转**不支持流式**：关掉后后端改成「整段一次性返回」，界面不再逐字出现，
          但内容与结果完全一样（不是出错）。<br />
          反过来，如果你确认模型支持流式、却一直看不到逐字效果，那多半是**中转把流式转成了非流式** ——
          这时开关的显示效果相同，属于上游行为，不是本控制台的问题（我们没法替上游保证）。
        </div>
        <div class="mt8">
          <label class="field" style="display:block">
            <span>备用模型</span>
            <select name="fallback_provider_id" id="fallback-select">
              <option value="">（不自动切换）</option>
            </select>
            <span class="hint">
              主模型**失败且尚未输出任何内容**时，自动切换到这个模型重试一次；
              已经输出过内容就不切（否则你会看到两段来自不同模型的文字拼在一起）。
              切换会在回复上方如实告知，消息上也会记**实际回答**的模型名。
            </span>
          </label>
        </div>
      </div>

      <div class="form-section">
        <h3>先测再存</h3>
        <div class="row tight">
          <button class="btn sec" type="button" id="btn-draft-test">用当前填写的内容试连一次</button>
        </div>
        <div class="hint">试连不会写数据库，只发一次极短的对话请求。</div>
        <div id="draft-test-result" class="mt8"></div>
      </div>`,
    footHTML: `
      <button class="btn sec" data-close>取消</button>
      <button class="btn" id="btn-save">${isEdit ? '保存修改' : '创建配置'}</button>`,
  });

  /* ---- 备用模型下拉：异步拉一次自己的配置列表（模板里拿不到列表） ---- */
  (async () => {
    const select = $('#fallback-select', m.root);
    if (!select) return;
    try {
      const data = await api.get('/providers');
      const rows = (data.items || data || []).filter((p) => p.id !== existing?.id);
      for (const p of rows) {
        const opt = document.createElement('option');
        opt.value = String(p.id);
        opt.textContent = `${p.name} · ${p.model_name}`;
        if (existing?.fallback_provider_id === p.id) opt.selected = true;
        select.appendChild(opt);
      }
      if (!rows.length) {
        select.disabled = true;
        select.title = '你只有这一个模型配置，先再加一个才能设备用模型';
      }
    } catch {
      /* 拉不到就留空（不自动切换），不影响保存 */
    }
  })();

  /* ---- 预算实时预览 ---- */
  const preview = $('#budget-preview', m.root);
  function refreshBudget() {
    const cw = Number($('[name=context_window]', m.root).value) || 0;
    const mt = Number($('[name=max_tokens]', m.root).value) || 0;
    const b = calcBudget(cw, mt);
    const bad = b.input <= 0;
    preview.innerHTML = `
      <table class="list" style="background:transparent">
        <tr><td style="width:60%">上下文窗口总容量</td><td class="mono">${b.contextWindow.toLocaleString()}</td></tr>
        <tr><td>最大输出 <span class="warn-text">（★ 含思考过程）</span></td><td class="mono">− ${b.maxTokens.toLocaleString()}</td></tr>
        <tr><td>安全余量（窗口的 5%，最少 128）</td><td class="mono">− ${b.margin.toLocaleString()}</td></tr>
        <tr><td><b>可用于系统提示词与对话历史</b></td>
            <td class="mono"><b style="color:${bad ? 'var(--danger)' : 'var(--ok)'}">${b.input.toLocaleString()}</b></td></tr>
      </table>
      ${
        bad
          ? '<div class="alert danger">输出预留 + 安全余量已吃掉整个上下文窗口，没有空间放提示词了。请调大窗口或调小最大输出。</div>'
          : b.maxTokens < 2048
            ? `<div class="alert warn">最大输出偏小（${b.maxTokens}）。如果是推理模型，思考过程可能吃光配额导致正文为空，建议至少 2048。</div>`
            : ''
      }`;
  }
  $('[name=context_window]', m.root).addEventListener('input', refreshBudget);
  $('[name=max_tokens]', m.root).addEventListener('input', refreshBudget);
  refreshBudget();

  /* ---- Base URL 预设：点一下填地址，并显示该厂商的模型名怎么填 ---- */
  $('#url-presets', m.root)?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-preset]');
    if (!btn) return;
    const preset = BASE_URL_PRESETS[Number(btn.dataset.preset)];
    if (!preset) return;
    $('[name=base_url]', m.root).value = preset.base_url;
    const detail = $('#preset-detail', m.root);
    if (detail) {
      detail.innerHTML = `<div class="alert info">
        <b>${esc(preset.name)}</b><br>
        Base URL：<span class="mono">${esc(preset.base_url)}</span><br>
        模型名：${esc(preset.model)}
      </div>`;
    }
  });

  /* ---- 从表单收集数据 ---- */
  function collect() {
    const v = {};
    for (const node of $$('[name]', m.root)) {
      const n = node.getAttribute('name');
      if (node.type === 'checkbox') v[n] = node.checked;
      else if (node.type === 'number' || node.type === 'range')
        v[n] = node.value === '' ? null : Number(node.value);
      else v[n] = node.value;
    }
    const generation = {
      temperature: v.temperature,
      max_tokens: v.max_tokens,
      reasoning_effort: v.reasoning_effort,
    };
    if (v.top_p !== null && v.top_p !== '') generation.top_p = Number(v.top_p);

    // ★ 提交前削掉「完整的接口路径」：用户从厂商文档整条复制是常见操作，
    //   存下一个必然失效的地址只会换来一个指向错误的报错（见 normalizeBaseUrl 的说明）。
    const fixed = normalizeBaseUrl(v.base_url);
    if (fixed.changed) {
      const input = $('[name=base_url]', m.root);
      if (input) input.value = fixed.url;
      toastWarn(`API 地址末尾的 ${fixed.removed} 已自动去掉（本项目会自己拼接口路径）`);
    }

    const body = {
      name: v.name,
      provider_type: v.provider_type,
      base_url: fixed.url,
      model_name: v.model_name,
      context_window: Number(v.context_window),
      generation,
      is_default: v.is_default,
      is_active: v.is_active,
      // ★ 每次都提交当前值：空串 = 解绑备用模型（后端用 model_fields_set 区分
      //   "传了 null"与"没传"，所以这里必须显式给 null，否则永远取消不掉）
      stream_enabled: v.stream_enabled,
      fallback_provider_id: v.fallback_provider_id ? Number(v.fallback_provider_id) : null,
    };
    // 编辑时：留空表示"不修改密钥"，所以不把空串塞进去
    if (v.api_key) body.api_key = v.api_key;
    if (v.clear_api_key) body.clear_api_key = true;
    if (!isEdit) body.api_key = v.api_key || '';
    return body;
  }

  /* ---- 试连（不写库） ---- */
  $('#btn-draft-test', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '试连中');
    const box = $('#draft-test-result', m.root);
    try {
      const body = collect();
      const res = await api.post('/providers/test-draft', body);
      // 与列表页的「测试」用同一套渲染：请求地址 / 上游状态 / 上游原话 + 排查顺序
      box.innerHTML = testResultHTML(res);
    } catch (err) {
      box.innerHTML = `<div class="alert danger">${esc(err.toDisplay ? err.toDisplay() : String(err))}</div>`;
    } finally {
      restore();
    }
  });

  /* ---- 保存 ---- */
  $('#btn-save', m.root).addEventListener('click', async (e) => {
    const restore = buttonLoading(e.target, '保存中');
    try {
      const body = collect();
      if (isEdit) {
        await api.patch(`/providers/${existing.id}`, body);
        toastOk('配置已更新');
      } else {
        await api.post('/providers', body);
        toastOk('配置已创建');
      }
      m.close();
      await refreshProviderList(root);
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      restore();
    }
  });
}

/* ==================================================================
   小弹窗
   ================================================================== */
function openModelsDialog(res) {
  modal({
    title: `可用模型（${res.count} 个）`,
    bodyHTML: res.models?.length
      ? `<div class="hint mb8">能拉到列表说明 Base URL 与 API Key 都是对的；模型名请从这里复制。</div>
         <div class="grid" style="grid-template-columns:1fr 1fr;gap:6px">
           ${res.models
             .map(
               (x) =>
                 `<div class="mono small" style="padding:5px 8px;background:var(--surface-2);border-radius:5px">${esc(x)}</div>`,
             )
             .join('')}
         </div>`
      : '<div class="alert warn">该服务商没有返回任何模型（有些厂商不提供 /models 接口，这不代表配置有错）。</div>',
    footHTML: '<button class="btn sec" data-close>关闭</button>',
  });
}

/**
 * 连通性测试结果弹窗。
 *
 * ★ 为什么测试失败要专门弹一个窗，而不是弹一条 toast？
 *   用户接不通第三方模型时（真实反馈过这个问题），
 *   一句 "指定的模型不存在，或当前 API Key 无权访问该模型" 提供的信息量是 0 ——
 *   它同时指向三种完全不同的原因。这里把**实际请求地址、上游 HTTP 状态、
 *   上游原话**全部摊开，再按状态码给出排查顺序，
 *   这样用户能自己判断是 URL 少了版本段、模型名写错、还是账号没开通。
 */
function openTestDialog(res) {
  modal({
    title: res.ok ? '连通性测试：正常' : '连通性测试：失败',
    width: 'wide',
    bodyHTML: testResultHTML(res),
    footHTML: '<button class="btn sec" data-close>关闭</button>',
  });
}

/** 把任意 JSON 结果丢进弹窗展示（健康检查详情、调试数据都用它） */
function openJsonDialog(title, data) {
  modal({
    title,
    bodyHTML: `
      <pre class="small mono" style="background:var(--surface-2);padding:12px;border-radius:6px;
        overflow:auto;max-height:52vh;white-space:pre-wrap">${esc(JSON.stringify(data, null, 2))}</pre>`,
    footHTML: '<button class="btn sec" data-close>关闭</button>',
  });
}
