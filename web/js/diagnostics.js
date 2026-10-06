/* ============================================================
   诊断信息收集（「关于」页的「复制诊断信息」按钮用）

   ==================== 它解决什么问题 ====================
   用户报"它坏了"时，最能帮上忙的三样东西是：
     ① 版本 / 存储方式 / 端口 这类环境事实；
     ② **出错的那几条请求**（状态码 + request_id + 后端的一句话）；
     ③ 数据与日志目录在哪（好让人去翻日志）。

   但**绝不能**把请求体/响应体一起带上 —— 那里有用户的对话原文、
   角色卡内容、世界书设定。所以这个模块只记**错误摘要**，
   与「请求日志」面板（那个故意摊开原始报文给人自己看）职责不同：

     请求日志面板  = 给**本机用户自己**排查用，含原始报文
     本模块        = 给**贴给别人看**用，只有环境事实 + 错误摘要

   ==================== 与探测过的那条教训一致 ====================
   "看起来有用的信息"要能被验证才有意义：
   所以诊断文本里每条错误都带 request_id —— 它能直接对上
   后端的 logs/backend.log，而不是一句没法追查的"失败了"。
   ============================================================ */

import { onRequestLogged } from 'hne/api';

/** 最多保留多少条错误（够贴一次问题，又不会让文本太长） */
const MAX_ERRORS = 12;

/** 贴出去时默认只带最近几条（太长没人看） */
const DEFAULT_TAIL = 5;

const errors = [];

/**
 * 这条请求记录算不算"值得报给别人的故障"。
 *
 * ★ 关键是**排除正常业务流程里的失败**：
 *   例如"会话不存在（404）"、"重复提交（409）"—— 那些是预期结果，
 *   把它们塞进诊断信息只会让看的人误以为系统到处在坏。
 */
function isReportableFailure(entry) {
  const status = Number(entry.status) || 0;

  // ① 连不上后端 / 超时：最需要报的一类
  if (status === 0) return true;
  // ② 服务器内部错误
  if (status >= 500) return true;
  // ③ 鉴权与参数类：几乎总是配置问题，值得看
  if ([400, 401, 403, 422, 429].includes(status)) return true;

  // ④ 其它 4xx（404 / 409 …）基本是"这个资源不在"之类的正常业务结果，
  //    不报 —— 否则用户一粘贴，满屏都是"某卡片不存在"。
  return false;
}

function remember(entry) {
  if (!isReportableFailure(entry)) return;
  errors.unshift({
    at: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
    method: String(entry.method || 'GET'),
    url: String(entry.url || ''),
    status: Number(entry.status) || 0,
    requestId: entry.requestId || '',
    // ★★ 这里**只允许**取响应体里的 `message` 一个字段，而且必须截断。
    //
    //   为什么这条注释写得这么啰嗦：诊断文本会被用户**贴给别人**，
    //   而响应体里同时躺着别的东西（`detail` 里有厂商返回的原始报文预览）。
    //   只取 `message` 是安全的，因为后端保证它是**写死的短句**
    //   （如"登录成功"、"未找到该角色卡"），从不包含对话或提示词内容 ——
    //   这一点在 tests/test_frontend_contracts.py 里有静态断言钉着，
    //   在 scripts/ui_probe.py 的「13.关于」一节还有一条动态断言
    //   （真的造一次错误，检查生成的文本里不含密钥/对话正文）。
    //   想在这里加别的字段之前，请先想清楚"它会不会带出用户内容"。
    message: String(entry.responseBody?.message || entry.error || '').slice(0, 120),
  });
  if (errors.length > MAX_ERRORS) errors.length = MAX_ERRORS;
}

/** 让收集器开始工作（在应用启动时调用一次即可，重复调用无害）。 */
export function initDiagnostics() {
  if (initDiagnostics._done) return;
  initDiagnostics._done = true;
  onRequestLogged(remember);
}

/** 供界面显示"有几条错误"（没有就不显示）。 */
export function errorCount() {
  return errors.length;
}

/** 清空错误记录（「关于」页提供一个「清空」入口，方便下次重新复现再抓）。 */
export function clearErrors() {
  errors.length = 0;
}

/**
 * 拼出可直接粘贴的诊断文本。
 *
 * @param {object} env 来自 `/system/diagnostics` 的环境快照（可为 null）
 * @param {object} health 来自 `/health` 的组件状态（可为 null）
 * @param {object} [opts]
 * @param {number} [opts.tail] 带最近几条错误（默认 5）
 * @returns {string}
 */
export function buildDiagnosticText(env, health, opts = {}) {
  const tail = Number.isFinite(opts.tail) ? opts.tail : DEFAULT_TAIL;
  const lines = ['【云梦枢诊断信息】'];

  // ---- 环境 ----
  if (env) {
    lines.push(`版本 ${env.version || '?'}（${env.env || '?'}）`);
    lines.push(`存储 ${env.database || env.backend || '?'}`);
    lines.push(`数据目录 ${env.data_dir || '?'}`);
    lines.push(`日志目录 ${env.log_dir || '?'}`);
    if (env.python) lines.push(`后端 Python ${env.python} · ${env.platform || ''}`.trim());
  } else {
    lines.push('（环境快照读取失败 —— 见下方最近错误）');
  }

  // ---- 界面侧 ----
  const ua = navigator.userAgent || '';
  const electron = /Electron\/([\d.]+)/.exec(ua);
  if (electron) {
    const chrome = /Chrome\/([\d.]+)/.exec(ua);
    const node = /Node\.js\/([\d.]+)/.exec(ua);
    lines.push(
      `界面 Electron ${electron[1]}`
        + (chrome ? ` · Chromium ${chrome[1]}` : '')
        + (node ? ` · Node ${node[1]}` : ''),
    );
  } else {
    lines.push(`界面 浏览器直开（${ua.slice(0, 80)}）`);
  }
  lines.push(`地址 ${location.origin}`);

  // ---- 组件状态（只带状态词，不带细节） ----
  const components = health?.components || {};
  const parts = [];
  for (const name of ['database', 'vector_store']) {
    const status = components[name]?.status;
    if (status) parts.push(`${name}=${status}`);
  }
  if (parts.length) lines.push(`组件 ${parts.join(' ')}`);

  // ---- 最近错误 ----
  if (!errors.length) {
    lines.push('最近错误 无');
  } else {
    const shown = errors.slice(0, Math.max(1, tail));
    lines.push(`最近错误（共 ${errors.length} 条，这里列最近 ${shown.length} 条，新的在前）：`);
    shown.forEach((e, i) => {
      const status = e.status === 0 ? 'ERR' : e.status;
      const rid = e.requestId ? ` request_id=${e.requestId}` : '';
      const msg = e.message ? ` “${e.message}”` : '';
      lines.push(` ${i + 1}) ${e.at} ${e.method} ${e.url} → ${status}${rid}${msg}`);
    });
  }

  lines.push('（本段只含版本/路径/错误摘要，不含对话内容与任何密钥）');
  return lines.join('\n');
}
