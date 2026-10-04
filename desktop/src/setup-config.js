'use strict';

/**
 * 首启向导的配置层：字段定义、`.env` 读写、校验、随机密钥生成。
 *
 * ==================== 为什么需要它 ====================
 * 打包版的后端不在仓库里跑，所以它读不到仓库根的 `.env`。用户装完应用，
 * 必须有个地方能填 MySQL 连接与密钥 —— 否则"安装包做得再漂亮也用不起来"。
 *
 * 设计上的三条硬要求：
 *   1. **配置写在 userData 下**（安装目录之外），卸载重装不丢；
 *   2. **绝不把 `.env` 打进安装包**（本项目既有的红线），所以只能首启问用户要；
 *   3. 用户填的东西**必须真的能用**才放行 —— 见 `validateConfig` 的说明。
 *
 * ★ 本文件不 import electron：路径由调用方注入，于是能被 `node --test` 直接测。
 */

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

/**
 * 向导要收集的字段。
 *
 * ★ `required: true` 的判据是"**没有它后端一定起不来**"，不是"最好填上"：
 *   · MySQL 主机/端口/用户/库名/口令 —— 少一样就连不上库；
 *   · `HNE_SECRET_KEY` —— 登录令牌签名用，后端没它会直接拒绝启动；
 *   · `HNE_API_KEY_ENCRYPTION_KEY` —— 没有它只是"用户自配的 LLM API Key 无法加密存储"
 *     （后端给告警、仍能跑），所以标成推荐、并**如实说明**后果，而不是假装必须。
 */
const SETUP_FIELDS = Object.freeze([
  {
    key: 'HNE_MYSQL_HOST',
    label: 'MySQL 主机',
    default: '127.0.0.1',
    placeholder: '127.0.0.1',
    required: true,
  },
  {
    key: 'HNE_MYSQL_PORT',
    label: 'MySQL 端口',
    default: '3306',
    placeholder: '3306',
    required: true,
    numeric: true,
  },
  {
    key: 'HNE_MYSQL_USER',
    label: 'MySQL 用户名',
    default: 'narrative_app',
    placeholder: 'narrative_app',
    required: true,
  },
  {
    key: 'HNE_MYSQL_PASSWORD',
    label: 'MySQL 口令',
    default: '',
    placeholder: '（安装 MySQL 时设置的密码）',
    required: true,
    secret: true,
  },
  {
    key: 'HNE_MYSQL_DB',
    label: '数据库名',
    default: 'narrative_engine',
    placeholder: 'narrative_engine',
    required: true,
  },
  {
    key: 'HNE_SECRET_KEY',
    label: '登录令牌签名密钥',
    // 生成命令与 .env.example 里一致
    hint: '随机生成即可，用来给登录令牌签名。换掉它会让已登录的用户需要重新登录。',
    required: true,
    secret: true,
    generate: () => crypto.randomBytes(48).toString('base64url'),
  },
  {
    key: 'HNE_API_KEY_ENCRYPTION_KEY',
    label: '密钥加密口令（Fernet）',
    hint: '用来加密你在「模型配置」里填的 API Key。不填也能跑，但那些 Key 无法加密存储。',
    required: false,
    secret: true,
    generate: () => crypto.randomBytes(32).toString('base64'),
  },
  {
    key: 'HNE_APP_ENV',
    label: '运行环境',
    default: 'production',
    required: false,
    advanced: true,
  },
  {
    key: 'HNE_DEBUG',
    label: '调试模式',
    default: 'false',
    required: false,
    advanced: true,
  },
  {
    key: 'HNE_LOG_LEVEL',
    label: '日志级别',
    default: 'INFO',
    required: false,
    advanced: true,
  },
]);

/** 向导不暴露、但写进 `.env` 的固定值（保证打包版行为可预期）。 */
const IMPLIED_VALUES = Object.freeze({
  HNE_HOST: '127.0.0.1',            // 只监听本机：这是个单机应用
  HNE_EMBEDDING_BACKEND: 'onnx_default', // 默认本地嵌入，不花用户的钱
});

/**
 * 拿到"默认值"这一套（用于表单预填）。
 *
 * ★ 高级项**不给默认值**：它们是"留空就用后端默认"的东西。
 *   如果这里预填一份默认值，渲染 `.env` 时就会把这些键统统写进文件 ——
 *   用户明明没设过，配置里却多出一堆值；哪天后端默认值改了，
 *   用户机器上那份**旧的**默认值还会盖住新默认值（静默不一致）。
 *   空着的项在渲染时会被跳过。
 */
function defaultValues() {
  const values = {};
  for (const field of SETUP_FIELDS) {
    if (field.advanced) { values[field.key] = ''; continue; }
    values[field.key] = field.default !== undefined ? field.default : '';
  }
  return values;
}

/** 给需要随机值的字段生成建议值（只补空着的，不覆盖用户已填的）。 */
function suggestSecrets(values) {
  const out = { ...values };
  for (const field of SETUP_FIELDS) {
    if (typeof field.generate === 'function' && !String(out[field.key] || '').trim()) {
      out[field.key] = field.generate();
    }
  }
  return out;
}

/**
 * 解析端口字符串 → 数字，或 null（无效）。
 *
 * ★ 判据必须与 `backend-config.js` 的 `parsePortValue` **完全一致**
 *   （两边都在校验"端口"，判据不一致就会一边放过、一边拦下）。
 *   要点：先用正则卡纯数字（`parseInt('3.5')`=3、`parseInt('3306abc')`=3306 都不能算合法），
 *   再卡 1~65535 的**闭区间**（`0` 也必须被拦下 —— 第一版只卡了上限，被测试抓到）。
 */
function parsePortValue(raw) {
  const text = String(raw ?? '').trim();
  if (!/^\d+$/.test(text)) return null;
  const parsed = Number.parseInt(text, 10);
  return parsed >= 1 && parsed <= 65535 ? parsed : null;
}

/**
 * 校验用户填的值。返回**逐字段**的错误，好让界面把红字标在对应输入框下面
 * （只给一句"配置不对"等于让用户自己猜）。
 *
 * @param {Record<string,string>} values
 * @returns {{ ok: boolean, errors: Record<string,string> }}
 */
function validateValues(values) {
  const errors = {};
  const get = (key) => String((values && values[key]) ?? '').trim();

  for (const field of SETUP_FIELDS) {
    if (!field.required) continue;
    if (!get(field.key)) {
      errors[field.key] = '这一项是必填的';
    }
  }

  const port = get('HNE_MYSQL_PORT');
  if (port && !errors.HNE_MYSQL_PORT) {
    if (parsePortValue(port) === null) {
      errors.HNE_MYSQL_PORT = '端口必须是 1~65535 之间的整数';
    }
  }

  const secret = get('HNE_SECRET_KEY');
  if (secret && secret.length < 32) {
    errors.HNE_SECRET_KEY = '太短了（至少 32 个字符），点右边的「随机生成」更省事';
  }

  const host = get('HNE_MYSQL_HOST');
  if (host && /\s/.test(host)) {
    errors.HNE_MYSQL_HOST = '主机名里不能有空格';
  }

  return { ok: Object.keys(errors).length === 0, errors };
}

/** `.env` 里需要加引号的值（否则被 # 截断、或被前后空格弄脏）。 */
function needsQuoting(value) {
  return /[\s#"'`]/.test(value) || value === '';
}

function quoteIfNeeded(value) {
  if (!needsQuoting(value)) return value;
  // 双引号包裹 + 转义内部的双引号与反斜杠（与 python-dotenv 的解析兼容）
  return `"${String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

/**
 * 生成 `.env` 文本。
 *
 * ★ 采用**读-改-写**：把已有文件里我们不认识的键原样保留下来。
 *   否则用户手改过的设置（比如改了嵌入后端、加了 CORS 源）会被向导悄悄抹掉 ——
 *   那属于"替用户做决定还不告诉他"。
 *
 * @param {Record<string,string>} values   表单值
 * @param {string} [existingText]          已有的 .env 内容
 * @returns {string}
 */
function renderEnvFile(values, existingText = '') {
  const known = new Set(SETUP_FIELDS.map((f) => f.key));
  const preserved = [];

  for (const rawLine of String(existingText).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    // 只保留"我们不认识"的键；认识的以表单为准
    if (!known.has(key)) preserved.push({ key, line: rawLine });
  }

  const lines = [
    '# ============================================================',
    '#  云梦枢 —— 本机配置（由桌面版「首次设置」生成）',
    '# ------------------------------------------------------------',
    '#  ★ 这个文件在 Electron 的 userData 目录下，**不在安装目录里**，',
    '#    所以卸载重装不会丢配置；换台机器则需要重新填一次。',
    '#  ★ 里面可能有数据库口令，别把它提交到任何代码仓库。',
    '#    所有键名都必须带 HNE_ 前缀（原因见项目里 .env.example 的说明）。',
    '# ============================================================',
    '',
    '# ---------------------- 应用基础 ----------------------',
  ];

  for (const field of SETUP_FIELDS) {
    if (field.advanced) continue;
    const value = String(values[field.key] ?? '').trim();
    if (!value) continue; // 留空 = 不写这个键（让后端用它自己的默认值）
    lines.push(`# ${field.label}${field.hint ? `：${field.hint}` : ''}`);
    lines.push(`${field.key}=${quoteIfNeeded(value)}`);
  }

  lines.push('', '# ---------------------- 固定值（按本机应用的最保守选择）----------------------');
  for (const [key, value] of Object.entries(IMPLIED_VALUES)) {
    lines.push(`# ${key === 'HNE_HOST' ? '只监听本机（单机应用）' : '默认用本地嵌入模型，不调用外部服务、不花钱'}`);
    lines.push(`${key}=${quoteIfNeeded(value)}`);
  }

  const advanced = SETUP_FIELDS.filter((f) => f.advanced && String(values[f.key] ?? '').trim());
  if (advanced.length) {
    lines.push('', '# ---------------------- 高级（可留默认）----------------------');
    for (const field of advanced) {
      lines.push(`${field.key}=${quoteIfNeeded(String(values[field.key]).trim())}`);
    }
  }

  if (preserved.length) {
    lines.push('', '# ---------------------- 你手工加过的其它配置（向导原样保留）----------------------');
    for (const item of preserved) lines.push(item.line.trim());
  }

  return `${lines.join('\n')}\n`;
}

/**
 * 解析 `.env` 文本（与后端一致的朴素规则：KEY=VALUE、跳过注释、去成对引号）。
 * @param {string} text
 * @returns {Record<string,string>}
 */
function parseEnvText(text) {
  const out = {};
  const source = String(text).replace(/^\uFEFF/, '');
  for (const rawLine of source.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    let value = line.slice(eq + 1).trim();
    if (value.length >= 2 && (
      (value.startsWith('"') && value.endsWith('"'))
      || (value.startsWith("'") && value.endsWith("'"))
    )) {
      value = value.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, '\\');
    }
    out[key] = value;
  }
  return out;
}

function readEnvFile(envFile) {
  try {
    return fs.readFileSync(envFile, 'utf8');
  } catch (error) {
    if (error && error.code === 'ENOENT') return null;
    throw error;
  }
}

/**
 * 判断"这个 .env 是否已经是可用配置"。
 *
 * ★ 为什么要判：如果只是"文件存在就算配好"，一个被截断/手改坏的文件
 *   会让应用每次启动都失败，而用户看不到"去向导"的入口（因为文件在）。
 *
 * @param {Record<string,string>} parsed
 */
function configIsUsable(parsed) {
  if (!parsed) return false;
  return SETUP_FIELDS.filter((f) => f.required).every((f) => String(parsed[f.key] || '').trim() !== '');
}

/** 列出哪些必填项在已有配置里是缺的（给界面显示"还缺什么"）。 */
function missingRequired(parsed) {
  if (!parsed) return SETUP_FIELDS.filter((f) => f.required).map((f) => f.key);
  return SETUP_FIELDS
    .filter((f) => f.required && !String(parsed[f.key] || '').trim())
    .map((f) => f.key);
}

/**
 * 写 `.env`（先写临时文件再改名，避免写到一半断电留下半个文件）。
 * @returns {string} 实际写入的路径
 */
function writeEnvFile(envFile, text) {
  fs.mkdirSync(path.dirname(envFile), { recursive: true });
  const tmp = `${envFile}.tmp`;
  fs.writeFileSync(tmp, text, { encoding: 'utf8', mode: 0o600 });
  fs.renameSync(tmp, envFile);
  return envFile;
}

module.exports = {
  SETUP_FIELDS,
  IMPLIED_VALUES,
  defaultValues,
  suggestSecrets,
  validateValues,
  parsePortValue,
  renderEnvFile,
  parseEnvText,
  readEnvFile,
  writeEnvFile,
  configIsUsable,
  missingRequired,
  quoteIfNeeded,
};
