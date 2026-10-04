'use strict';

/**
 * 首次设置页的**发请求前**校验（在渲染进程里跑）。
 *
 * ==================== 为什么要有这一层 ====================
 * 原来点「测试连接」/「保存并开始」会把整个表单**直接交给主进程**，主进程再用
 * 真后端去连一次 MySQL。于是出现了一个很糟的体验（用户实测撞到）：
 *
 *   他**口令那一格是空的**，点了保存 → 应用起了一次真后端 → 后端报
 *   `1045 Access denied for user 'narrative_app'@'localhost' (using password: YES)`，
 *   界面上滚出一大段 Traceback + 自检提示。
 *
 * 那段输出对排查有用，但**对填表的人毫无意义**：真正的原因就是"那一格没填"。
 * 所以规则很简单：**能在本地一眼看出来的问题，就不要去起后端**。
 * 反过来，凡是本地看不出来的（口令对不对、库存不存在、向量库目录能不能写），
 * 一律照旧交给真后端去验 —— 这一层**不假装**自己能判断那些事。
 *
 * ★ 本文件同时被两处使用：
 *   · 浏览器 / Electron 渲染进程：`<script src="setup-validate.js"></script>`
 *     之后用 `HneSetupValidate.precheck(...)`；
 *   · `node --test`：`require('../src/setup-validate.js')`。
 *   所以它**不引用任何 DOM 与 Electron API**，纯函数、可直接测。
 */

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api; // Node
  else root.HneSetupValidate = api; // 浏览器
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  /** 端口字段的合法值：纯数字且 1..65535（与 setup-config.js 的判据保持一致）。 */
  function isPortLike(value) {
    const text = String(value == null ? '' : value).trim();
    if (!/^\d+$/.test(text)) return false;
    const port = Number(text);
    return port >= 1 && port <= 65535;
  }

  /**
   * 填表前的体检。
   *
   * @param {object} args
   * @param {object[]} args.fields  字段定义（来自主进程，含 key/label/required/numeric/dbOnly）
   * @param {object} args.values    用户当前填的值 { key: string }
   * @returns {{ ok: boolean, errors: object, message: string }}
   *          errors 形如 { HNE_MYSQL_PASSWORD: '这一格是必填的' }，交给页面就地高亮
   */
  function precheck(args) {
    const fields = (args && args.fields) || [];
    const values = (args && args.values) || {};
    const errors = {};
    const labels = [];

    // ★ 默认存储方式是 SQLite（不需要任何连接信息）。选它的时候，
    //   标了 `dbOnly` 的 MySQL 字段**整组不参与体检** —— 否则默认的零配置
    //   会被"MySQL 口令没填"拦住，而那个用户压根没打算用 MySQL。
    //   ★ 这个判据必须与 setup-config.js 的 validateValues 一致：
    //     本地放过了、保存被拦下（或反过来）是最难查的一类不一致。
    const backend = String(values.HNE_DB_BACKEND || '').trim() || 'sqlite';
    const mysqlMode = backend === 'mysql';

    for (const field of fields) {
      const key = field.key;
      if (field.dbOnly && !mysqlMode) continue;
      const raw = values[key];
      const text = String(raw == null ? '' : raw).trim();

      if (field.required && !text) {
        // ★ 这就是用户那次撞到的情形：别去起后端，直接告诉他这一格要填
        errors[key] = field.numeric
          ? '这一格是必填的（填数字）'
          : '这一格是必填的';
        labels.push(field.label || key);
        continue;
      }
      if (field.numeric && text && !isPortLike(text)) {
        errors[key] = '要填 1~65535 之间的数字';
        labels.push(field.label || key);
        continue;
      }
      // 提前给出"短了"的提示（后端也会拒，但在这里说更清楚）
      if (key === 'HNE_SECRET_KEY' && text && text.length < 32) {
        errors[key] = '太短了（至少 32 个字符）；留空则由后端自动生成';
        labels.push(field.label || key);
      }
      // Fernet 密钥：长度必须正好 44（32 字节的 base64）。填错了后端要到
      // "用户第一次保存模型 API Key"才报错，那时离填表已经很久了。
      if (key === 'HNE_API_KEY_ENCRYPTION_KEY' && text && !isFernetLike(text)) {
        errors[key] = '应是 44 个字符的 Fernet 密钥；留空则由后端自动生成';
        labels.push(field.label || key);
      }
    }

    const ok = labels.length === 0;
    return {
      ok,
      errors,
      message: ok ? '' : `先补上这几项再试：${labels.join('、')}`,
    };
  }

  /**
   * 看起来像不像一个合法的 Fernet 密钥（32 字节 urlsafe base64 → 43 字符 + 一个 '='）。
   *
   * ★ 只判形态，**不判正确性** —— 本地没有也不该有那把真密钥的权威。
   * ★ 形状里已经隐含长度：`43 + 1 = 44`。用形状而不是"长度等于 44"，
   *   是因为 `'K'.repeat(44)` 这种长度对、形状错的值必须被拦下
   *   （它解码出来是 33 字节，后端会拒）。
   */
  function isFernetLike(value) {
    const text = String(value == null ? '' : value).trim();
    return /^[A-Za-z0-9_-]{43}=$/.test(text);
  }

  return { precheck, isPortLike, isFernetLike };
}));
