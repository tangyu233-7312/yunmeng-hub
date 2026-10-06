/* ============================================================
   API 客户端 + 登录态管理

   所有对后端的调用都经过这里，好处：
     1. 自动带上 Authorization 头
     2. 统一拆解后端的响应壳 { code, message, data }
     3. 统一把错误转成 ApiError（带业务错误码与 detail）
     4. ★ 每一次调用都会广播出去，供「请求日志」面板展示
        —— 这是为了让「测试功能」看得见摸得着：能看到真实请求与原始响应
   ============================================================ */

const TOKEN_KEY = 'hne_access_token';
const REFRESH_KEY = 'hne_refresh_token';
const USER_KEY = 'hne_user';

// 前端与后端同源（都由 FastAPI 提供），所以直接用相对路径即可
const API_PREFIX = '/api/v1';

/* ---------------- 登录态 ---------------- */
export const session = {
  get token() {
    return localStorage.getItem(TOKEN_KEY) || '';
  },
  get refreshToken() {
    return localStorage.getItem(REFRESH_KEY) || '';
  },
  get user() {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY) || 'null');
    } catch {
      return null;
    }
  },
  get isLoggedIn() {
    return Boolean(this.token);
  },
  save(tokens, user) {
    localStorage.setItem(TOKEN_KEY, tokens.access_token || '');
    if (tokens.refresh_token) localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
    if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem(USER_KEY);
  },
};

/* ---------------- 业务异常 ---------------- */
export class ApiError extends Error {
  constructor({ status, code, message, detail, requestId }) {
    super(message || `请求失败（HTTP ${status}）`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.requestId = requestId;
  }

  /** 把错误整理成一句能直接显示给用户的话 */
  toDisplay() {
    let text = this.message;
    if (this.detail?.hint) text += `\n提示：${this.detail.hint}`;

    // ★ 后端的 detail 有两种形状，必须都认：
    //   · 对象（业务异常）：{ hint, suggestion, ... }
    //   · 数组（422 参数校验）：[{ field, msg, type }, …]
    //   以前只处理了对象里的 detail.errors，所以 422 在界面上只显示一句
    //   "请求参数校验失败"，**到底是哪个字段错了完全看不出来** ——
    //   用户只能来问，而我们也只能猜。
    const list = Array.isArray(this.detail)
      ? this.detail
      : Array.isArray(this.detail?.errors)
        ? this.detail.errors
        : null;
    if (list?.length) {
      text +=
        '\n具体字段：' +
        list
          .map((e) => `· ${e.field || '(body)'}：${e.msg || JSON.stringify(e)}`)
          .join('\n');
    }
    if (this.detail?.suggestion) text += `\n建议：${this.detail.suggestion}`;
    if (this.requestId) text += `\nrequest_id: ${this.requestId}`;
    return text;
  }
}

/* ---------------- 请求日志广播 ---------------- */
const logListeners = [];
export function onRequestLogged(fn) {
  logListeners.push(fn);
}
function emitLog(entry) {
  for (const fn of logListeners) {
    try {
      fn(entry);
    } catch {
      /* 日志面板自身出错不能影响主流程 */
    }
  }
}

/* ---------------- 核心请求方法 ---------------- */
/**
 * @param {string} method  HTTP 方法
 * @param {string} path    以 / 开头的接口路径（不含 /api/v1 前缀）
 * @param {object} [opts]
 * @param {object} [opts.query]  查询参数（值为 undefined/null/'' 的会被忽略）
 * @param {object} [opts.body]   JSON 请求体
 * @param {FormData} [opts.form] 表单请求体（文件上传用，此时不能手动设 Content-Type）
 * @param {string} [opts.token]  覆盖本次请求用的令牌。
 *   ★ 为什么需要它：自动化验收要能**故意用一个坏令牌**，才能验证
 *     「401 会被诊断收集器记下来」这类行为。没有它，验收就只能拿真令牌，
 *     永远造不出 401（第一版就因此写了一条永远测不到东西的断言）。
 *     常规调用方**不要**传它 —— 默认就用当前登录态。
 * @returns {Promise<{code:string,message:string,data:any}>} 后端完整响应壳
 */
export async function request(method, path, opts = {}) {
  const { query, body, form, timeoutMs, token } = opts;

  const url = new URL(API_PREFIX + path, window.location.origin);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
    }
  }

  const headers = {};
  const bearer = token !== undefined ? token : session.token;
  if (bearer) headers['Authorization'] = `Bearer ${bearer}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const init = { method, headers };
  if (body !== undefined) init.body = JSON.stringify(body);
  if (form) init.body = form;
  // ★ 可选超时：只有"列表/详情"这类**应当很快返回**的请求才传 timeoutMs。
  //   不传就维持原来的行为（对话生成可能要几十秒，绝不能给它加超时）。
  //   没有超时的话，后端一旦卡住，页面会永远停在"加载中…"，用户连重试都没有。
  if (timeoutMs) init.signal = AbortSignal.timeout(timeoutMs);

  const started = performance.now();
  let response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    // 超时也走这里（AbortSignal.timeout 抛的是 TimeoutError），
    // 但要说清楚是"超时"还是"连不上"，否则用户会被误导去查服务有没有起。
    const timedOut = String(err?.name || '') === 'TimeoutError';
    emitLog({
      method,
      url: url.pathname + url.search,
      status: 0,
      durationMs: Math.round(performance.now() - started),
      error: String(err),
    });
    throw new ApiError({
      status: 0,
      code: timedOut ? 'TIMEOUT' : 'NETWORK_ERROR',
      message: timedOut
        ? `请求超时（超过 ${Math.round(timeoutMs / 1000)} 秒没有响应），后端可能卡住了，请重试`
        : '无法连接后端服务，请确认服务是否已启动（uvicorn）',
    });
  }
  const durationMs = Math.round(performance.now() - started);

  const rawText = await response.text();
  let payload = null;
  try {
    payload = rawText ? JSON.parse(rawText) : null;
  } catch {
    payload = { code: 'BAD_JSON', message: rawText.slice(0, 500) };
  }

  emitLog({
    method,
    url: url.pathname + url.search,
    status: response.status,
    durationMs,
    requestBody: body,
    responseBody: payload,
    requestId: response.headers.get('X-Request-ID') || payload?.request_id,
  });

  if (!response.ok) {
    throw new ApiError({
      status: response.status,
      code: payload?.code,
      message: payload?.message,
      detail: payload?.detail,
      requestId: payload?.request_id || response.headers.get('X-Request-ID'),
    });
  }

  return payload ?? { code: 'OK', message: '', data: null };
}

/* ---------------- 便捷方法：直接返回 data ---------------- */
export const api = {
  async get(path, query, opts) {
    return (await request('GET', path, { query, ...opts })).data;
  },
  async getRaw(path, query, opts) {
    return request('GET', path, { query, ...opts });
  },
  /**
   * 取一段**纯文本**（不是 JSON 壳）。
   *
   * ★ 为什么需要它：插件的 CSS 主题接口返回的是 text/css，
   *   走 get() 会在 JSON.parse 那里炸掉（本项目统一响应壳是 JSON，
   *   所以只有这里破例）。鉴权照旧。
   */
  async getText(path) {
    const url = new URL(API_PREFIX + path, window.location.origin);
    const headers = {};
    if (session.token) headers['Authorization'] = `Bearer ${session.token}`;
    const response = await fetch(url, { headers });
    if (!response.ok) {
      throw new ApiError({
        status: response.status,
        message: `读取失败（HTTP ${response.status}）`,
      });
    }
    return response.text();
  },
  async post(path, body, query) {
    return (await request('POST', path, { body, query })).data;
  },
  async postRaw(path, body, query) {
    return request('POST', path, { body, query });
  },
  async patch(path, body) {
    return (await request('PATCH', path, { body })).data;
  },
  async put(path, body) {
    // PUT = "整份替换"语义（例如记忆锚点清单：提交什么就是什么，不做增删改三套接口）
    return (await request('PUT', path, { body })).data;
  },
  async patchRaw(path, body) {
    return request('PATCH', path, { body });
  },
  async del(path, query) {
    return (await request('DELETE', path, { query })).data;
  },
  async delRaw(path, query) {
    return request('DELETE', path, { query });
  },
  async postForm(path, form, query) {
    return (await request('POST', path, { form, query })).data;
  },
};

/* ---------------- 认证相关 ---------------- */
export const auth = {
  async register({ username, email, password }) {
    return api.post('/auth/register', { username, email, password });
  },
  async login({ username, password }) {
    const tokens = await api.post('/auth/login', { username, password });
    session.save(tokens, tokens.user || null);
    return tokens;
  },
  async loadMe() {
    const me = await api.get('/auth/me');
    session.save({ access_token: session.token }, me);
    return me;
  },
  logout() {
    session.clear();
  },
};
