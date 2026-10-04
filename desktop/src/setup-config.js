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
 * ==================== ★ 这一版的变化：默认零配置 ====================
 * 云梦枢是**单机桌面应用**，所以默认存储方式是 **SQLite（一个文件）** ——
 * 用户装完直接能用，不需要先去装一个 MySQL 服务器。因此：
 *
 *   · `HNE_DB_BACKEND` 是**选择项**，默认 `sqlite`；选它时下面那组 MySQL 字段
 *     整组不显示、也不参与校验；
 *   · 两个密钥**不再必填**：后端首次运行会自己生成并保存在
 *     `<数据目录>/config/.secrets.env`（见 app/db/bootstrap.py）。
 *     用户想自己掌控就填，不填也完全没问题 —— 所以标成"可选 + 留空会自动生成"。
 *   · MySQL 字段仍然保留（`dbOnly`），选 MySQL 时才出现，判据与以前完全一样。
 *
 * ★ `required: true` 的判据仍然是"**没有它后端一定起不来**"：
 *   · 选 mysql 时：主机/端口/用户/库名/口令少一样就连不上库；
 *   · 选 sqlite 时：**一个都不需要**（表由后端建、密钥由后端生成）。
 */
const SETUP_FIELDS = Object.freeze([
  {
    key: 'HNE_DB_BACKEND',
    label: '存储方式',
    // 选择项：界面渲染成 <select>
    options: [
      { value: 'sqlite', label: '本机文件（SQLite）· 推荐，装完即用' },
      { value: 'mysql', label: 'MySQL 数据库（需要你已经有 MySQL 8）' },
    ],
    default: 'sqlite',
    hint: '本机文件不需要安装任何额外软件；想用 MySQL 就在下面填连接信息。',
    required: true,
  },
  {
    key: 'HNE_MYSQL_HOST',
    label: 'MySQL 主机',
    default: '127.0.0.1',
    placeholder: '127.0.0.1',
    required: true,
    dbOnly: true,
  },
  {
    key: 'HNE_MYSQL_PORT',
    label: 'MySQL 端口',
    default: '3306',
    placeholder: '3306',
    required: true,
    numeric: true,
    dbOnly: true,
  },
  {
    key: 'HNE_MYSQL_USER',
    label: 'MySQL 用户名',
    default: 'narrative_app',
    placeholder: 'narrative_app',
    required: true,
    dbOnly: true,
  },
  {
    key: 'HNE_MYSQL_PASSWORD',
    label: 'MySQL 口令',
    default: '',
    placeholder: '（安装 MySQL 时设置的密码）',
    required: true,
    secret: true,
    dbOnly: true,
  },
  {
    key: 'HNE_MYSQL_DB',
    label: '数据库名',
    default: 'narrative_engine',
    placeholder: 'narrative_engine',
    required: true,
    dbOnly: true,
  },
  {
    key: 'HNE_SECRET_KEY',
    label: '登录令牌签名密钥（可留空）',
    hint: '留空 = 后端首次运行时自动生成并保存在应用数据目录，重启后沿用。'
      + '自己填的话至少 32 个字符；换掉它会让已登录的用户需要重新登录。',
    // ★ 不再是必填：后端会自己生成（app/db/bootstrap.py），
    //   这里只给"想自己掌控密钥"的用户一个入口。
    required: false,
    secret: true,
    generate: () => crypto.randomBytes(48).toString('base64url'),
  },
  {
    key: 'HNE_API_KEY_ENCRYPTION_KEY',
    label: '密钥加密口令（Fernet，可留空）',
    hint: '留空 = 后端首次运行时自动生成。它用来加密你在「模型配置」里填的 API Key；'
      + '★ 一旦用它存过 Key，就别再改动这个值（改了那些 Key 就解不开了）。',
    required: false,
    secret: true,
    // ★ 必须是**带 padding 的** base64url（43 个字符 + 一个 `=`），
    //   与 Python 的 `Fernet.generate_key()` 逐字节同构：
    //       base64.urlsafe_b64encode(os.urandom(32))
    //   ★ 注意 Node 的 `toString('base64url')` **不带 padding**（只有 43 个字符），
    //     而 Fernet 的标准形状是 44 个字符。第一版就是这么写的，
    //     于是"向导自己生成的值自己校验不过" —— 被测试当场抓到。
    generate: () => `${crypto.randomBytes(32).toString('base64url')}=`,
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

/** 默认的存储方式（写在一处，别在代码里散落 'sqlite' 字面量）。 */
const DEFAULT_DB_BACKEND = 'sqlite';

/** 向导不暴露、但写进 `.env` 的固定值（保证打包版行为可预期）。 */
const IMPLIED_VALUES = Object.freeze({
  HNE_HOST: '127.0.0.1',            // 只监听本机：这是个单机应用
  HNE_EMBEDDING_BACKEND: 'onnx_default', // 默认本地嵌入，不花用户的钱、不联网
});

/**
 * 「零配置」在**向导/配置文本**这一侧需要的一组值。
 *
 * ★ 与 `backend-config.js` 的 `databaseEnvDefaults` 是同一件事的两面：
 *   那边负责"给子进程的进程环境注入什么"，这边负责"向导表单与 .env 文本长什么样"。
 *   刻意不互相 import —— 两边职责不同，互相引用会让任一边的单测都要拖进另一边。
 *   代价是这两处要一起改，所以有 `desktop/test/zero-config.test.js` 同时盯着两边。
 *
 * @param {string} dataDir userData 下的数据目录
 * @returns {Record<string,string>}
 */
function databaseDefaults(dataDir) {
  return {
    HNE_DATA_DIR: String(dataDir),
    HNE_SQLITE_PATH: path.join(String(dataDir), 'data', 'app.sqlite3'),
  };
}

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
 * ★ 关键：**选 sqlite 时，MySQL 那一组字段完全不参与校验**。
 *   否则默认的零配置会被"口令没填"拦住 —— 而那个用户压根没打算用 MySQL。
 *   这一条和 `configIsUsable` / 本地体检（setup-validate.js）必须用**同一套判据**，
 *   三处不一致就会变成"界面放过了、保存被拦下"这类难查的问题。
 *
 * @param {Record<string,string>} values
 * @returns {{ ok: boolean, errors: Record<string,string> }}
 */
function validateValues(values) {
  const errors = {};
  const get = (key) => String((values && values[key]) ?? '').trim();
  const backend = get('HNE_DB_BACKEND') || DEFAULT_DB_BACKEND;

  for (const field of SETUP_FIELDS) {
    if (field.dbOnly && backend !== 'mysql') continue;
    if (!field.required) continue;
    if (!get(field.key)) {
      errors[field.key] = '这一项是必填的';
    }
  }

  // 端口只在 mysql 模式下才有意义（sqlite 不看它）
  if (backend === 'mysql') {
    const port = get('HNE_MYSQL_PORT');
    if (port && !errors.HNE_MYSQL_PORT) {
      if (parsePortValue(port) === null) {
        errors.HNE_MYSQL_PORT = '端口必须是 1~65535 之间的整数';
      }
    }
    const host = get('HNE_MYSQL_HOST');
    if (host && /\s/.test(host)) {
      errors.HNE_MYSQL_HOST = '主机名里不能有空格';
    }
  }

  // ★ 密钥是**可选**的（留空由后端自动生成），但一旦填了就得合法 ——
  //   否则用户会以为"我设了密钥"，后端却因为格式不合法又生成了一把新的。
  const secret = get('HNE_SECRET_KEY');
  if (secret && secret.length < 32) {
    errors.HNE_SECRET_KEY = '太短了（至少 32 个字符）；留空则由后端自动生成';
  }
  const fernet = get('HNE_API_KEY_ENCRYPTION_KEY');
  if (fernet && !isFernetShaped(fernet)) {
    errors.HNE_API_KEY_ENCRYPTION_KEY =
      '这不是一个合法的 Fernet 密钥（应为 32 字节的 base64，共 44 个字符）；留空则由后端自动生成';
  }

  return { ok: Object.keys(errors).length === 0, errors };
}

/**
 * 「切换存储方式」时要**保留**的已存密钥。
 *
 * ==================== ★ 为什么必须有这一步（不然会毁数据）====================
 * 切换存储方式时，那个表单是**预填**出来的，但密钥字段**故意不回传**给页面
 * （少一份"把密钥读回渲染进程"的风险，见 preload.js 的说明）。
 * 于是用户点「保存并切换」时，页面上那两个密钥格是**空的** ——
 * 如果就这么写下去，`.env` 里的 `HNE_API_KEY_ENCRYPTION_KEY` 会被清掉，
 * 后端下次启动就会**重新生成一把**，而用户已经存进数据库的模型 API Key
 * 是拿旧密钥加密的 → **永远解不开**了。
 *
 * ★ 规则（与 app/db/bootstrap.py 的 `ensure_secrets` 保持同一套价值观）：
 *   **只补缺失的，绝不覆盖用户已有的值。**
 *   所以：提交值为空 → 沿用文件里已有的；提交值非空 → 以用户填的为准。
 *
 * ★ 为什么只对"密钥类"字段这么做（`secret: true`）：
 *   普通字段（主机/端口/口令…）空着就是"用户真的想清掉它"，
 *   而密钥类字段空着更可能是"我没动它" —— 两者语义不同，不能一刀切。
 *
 * @param {Record<string,string>} values   表单提交值
 * @param {Record<string,string>} existing 现有配置（已解析）
 * @returns {{ values: Record<string,string>, preserved: string[] }} preserved = 沿用了哪几个键
 */
function preserveSecrets(values, existing) {
  const out = { ...(values || {}) };
  const preserved = [];
  for (const field of SETUP_FIELDS) {
    if (!field.secret) continue;
    const submitted = String(out[field.key] ?? '').trim();
    const saved = String((existing || {})[field.key] ?? '').trim();
    if (!submitted && saved) {
      out[field.key] = saved;
      preserved.push(field.key);
    }
  }
  return { values: out, preserved };
}

/**
 * 判断"看起来是不是一个合法的 Fernet 密钥"（32 字节的 urlsafe base64）。
 *
 * ★ 为什么向导也要判：`API_KEY_ENCRYPTION_KEY` 格式不对时，后端要到**用户第一次
 *   保存模型 API Key** 才报错 —— 那时离"填表"已经过去很久了。在这里挡一下，
 *   用户当场就知道自己粘贴错了。
 * ★ 判据必须**先卡形状再卡长度**：`Buffer.from` 的 base64 解码是宽松的，
 *   `'K'.repeat(44)` 会被解成 **33** 字节（而不是报错），所以"44 个字符"这件事
 *   不能当成"合法"——真密钥的形状是 `43 个 base64url 字符 + 一个 '='`。
 * ★ 只判形态，**不判内容**：合法的自定义密钥必须原样放行。
 */
function isFernetShaped(value) {
  const text = String(value || '').trim();
  if (!/^[A-Za-z0-9_-]{43}=$/.test(text)) return false;
  return Buffer.from(text, 'base64').length === 32;
}

/** `.env` 里需要加引号的值（否则被 # 截断、或被前后空格弄脏）。 */function needsQuoting(value) {
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
function renderEnvFile(values, existingText = '', existing = null) {
  const known = new Set(SETUP_FIELDS.map((f) => f.key));
  const preserved = [];
  //: 现有配置的键值（用于"切走时把另一套存储的连接信息留下来"，
  //  以及密钥留空即沿用）。不传就现解析一份 —— 免得调用方忘了传时静默丢值。
  const current = existing || parseEnvText(String(existingText || ''));

  for (const rawLine of String(existingText).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    // 只保留"我们不认识"的键；认识的以表单为准
    if (!known.has(key)) preserved.push({ key, line: rawLine });
  }

  // ★ 选 sqlite 时：MySQL 那一组键**一个都不写**（不写空值、也不写默认值）。
  //   原因：把 `HNE_MYSQL_PASSWORD=` 这种空行写进 .env 毫无意义，
  //   更糟的是它会让人以为"这里配了 MySQL"。没写的键，后端就用它自己的默认值。
  const backend = String(values.HNE_DB_BACKEND || '').trim() || DEFAULT_DB_BACKEND;

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
    `# ---------------------- 存储方式：${backend === 'mysql' ? 'MySQL' : '本机文件（SQLite）'} ----------------------`,
  ];

  // 第一行永远是存储方式本身（让这份文件一眼看得出用的是哪种存储）
  lines.push('# 数据存哪里（sqlite = 一个文件，装完即用；mysql = 连你已有的 MySQL）');
  lines.push(`HNE_DB_BACKEND=${quoteIfNeeded(backend)}`);

  for (const field of SETUP_FIELDS) {
    if (field.advanced) continue;
    if (field.key === 'HNE_DB_BACKEND') continue; // 上面已经写过
    // ★ 非当前后端的字段整组跳过（选 sqlite 就不写 MySQL 的任何键）
    if (field.dbOnly && backend !== 'mysql') continue;
    const value = String(values[field.key] ?? '').trim();
    if (!value) continue; // 留空 = 不写这个键（让后端用它自己的默认值）
    lines.push(`# ${field.label}${field.hint ? `：${field.hint}` : ''}`);
    lines.push(`${field.key}=${quoteIfNeeded(value)}`);
  }

  // ★★ 切走时**把另一套存储的连接信息留下来（写成注释）**。
  //
  //   为什么必须这么做（用户实测的严重问题）：
  //     他原来是 MySQL 用户，为了试试 SQLite 切了过去 —— 而这里原本把
  //     `HNE_MYSQL_*` **整组删掉**了，包括那串**自动生成的 32 位随机口令**。
  //     等他反悔想切回 MySQL 时，口令格是空的、而他又不可能记得那串随机串，
  //     于是只能瞎填一个 → 连不上 → 自检等到超时 → 他只看到"自检超时"，
  //     完全不知道发生了什么。（这直接导致了他"我的账号是不是被人改了"那一晚。）
  //
  //   ★ 写成**注释行**是刻意的：
  //     · 后端读 `.env` 时会跳过注释 → **它对运行行为毫无影响**，
  //       选 sqlite 时绝不会因为这里留着 MySQL 键就去连 MySQL；
  //     · 但值还在文件里，切回来时能原样恢复（见 stashOf / main.js 的恢复逻辑）。
  //   ★ 权衡说明（必须写下来，别让下一个人以为是漏了）：注释里会有明文的
  //     MySQL 口令。该文件本来就已经存着 HNE_SECRET_KEY / Fernet 密钥，
  //     而且就在用户自己的 userData 目录里；但**留明文口令确实多了一份暴露面**。
  //     我选择留下，理由是"用户能自己切回来"比"少一份本地凭据"更重要 ——
  //     两者都是本机文件，能读到这个文件的攻击者本来也能读到别的密钥。
  const stashedGroups = [];
  if (backend !== 'mysql') {
    const stash = [];
    for (const field of SETUP_FIELDS) {
      if (!field.dbOnly) continue;
      let value = String(values[field.key] ?? '').trim();
      if (!value) {
        // 本次表单里没填（切换页不回传口令）→ 沿用现有配置里的值，
        // 否则"切走再切回来"照样会把口令弄丢。
        value = String(current[field.key] ?? '').trim();
      }
      if (!value) continue;
      stash.push(`${field.key}=${quoteIfNeeded(value)}`);
    }
    if (stash.length) {
      stashedGroups.push(
        '',
        '# ---------------------- 你之前用 MySQL 时的连接信息（现在没在用）----------------------',
        '# ★ 这些行是**注释**，对程序没有任何影响 —— 选「本机文件」时后端不会去连 MySQL。',
        '#   它们留在这里是为了让你随时能切回去：菜单「数据 → 切换存储方式…」选 MySQL 时会自动填回来。',
        ...stash.map((line) => `# ${line}`),
      );
    }
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

  lines.push(...stashedGroups);

  if (preserved.length) {
    lines.push('', '# ---------------------- 你手工加过的其它配置（向导原样保留）----------------------');
    for (const item of preserved) lines.push(item.line.trim());
  }

  return `${lines.join('\n')}\n`;
}

/**
 * 取出"之前用 MySQL 时的连接信息"（`renderEnvFile` 写的那些**注释行**）。
 *
 * ★ 为什么需要它：用户切到「本机文件」之后，`HNE_MYSQL_*` 就不再是生效的配置了
 *   （`parseEnvText` 会跳过注释，这是对的 —— 选 sqlite 时绝不能因为文件里
 *   留着 MySQL 键就去连 MySQL）。但切回来时要把它们**原样填回表单**，
 *   否则用户就得重新回忆那串 32 位随机口令（他不可能记得，实测就是这样卡住的）。
 *
 * @param {string} text `.env` 的原始文本
 * @returns {Record<string,string>} 只含 `dbOnly` 字段里被注释掉的那些键
 */
function stashOf(text) {
  const out = {};
  const dbOnlyKeys = new Set(SETUP_FIELDS.filter((f) => f.dbOnly).map((f) => f.key));
  const source = String(text || '').replace(/^\uFEFF/, '');
  for (const rawLine of source.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line.startsWith('#')) continue;
    // 去掉注释标记后按普通 KV 解析（值里可能带引号）
    const parsed = parseEnvText(line.replace(/^#+\s*/, ''));
    for (const [key, value] of Object.entries(parsed)) {
      if (dbOnlyKeys.has(key) && String(value).trim()) out[key] = value;
    }
  }
  return out;
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
 * ★ 与 `validateValues` 同一套判据（选 sqlite 时不要求 MySQL 字段）。
 *   这里多一层**向后兼容**：老版本向导写的配置里**没有** `HNE_DB_BACKEND`
 *   却有 `HNE_MYSQL_*` —— 那说明这份配置本来就是给 MySQL 用的，
 *   必须按 mysql 判据走，否则会被误判成"缺项"而被拦在向导外面。
 *
 * @param {Record<string,string>} parsed
 */
function configIsUsable(parsed) {
  if (!parsed) return false;
  // ★ 空对象 = "文件在、但一行有效配置都没有"（被清空/被截断/只有注释）。
  //   这必须判为**不可用** —— 否则应用会拿着空配置一路启动失败，
  //   而用户看不到任何"去设置"的入口（因为文件存在）。
  //   注意：一份合法的零配置**至少**有一行 `HNE_DB_BACKEND=sqlite`，不会被这条拦下。
  if (Object.keys(parsed).length === 0) return false;
  return SETUP_FIELDS
    .filter((f) => f.required && !f.dbOnly)
    // ★ 兼容老配置：老版本向导从不写 `HNE_DB_BACKEND`，所以这个键缺失**不算缺项**
    //   （它到底算 sqlite 还是 mysql 由 resolvedBackend 推断）。
    .filter((f) => f.key !== 'HNE_DB_BACKEND')
    .every((f) => String(parsed[f.key] || '').trim() !== '')
    && SETUP_FIELDS
      .filter((f) => f.dbOnly && f.required && resolvedBackend(parsed) === 'mysql')
      .every((f) => String(parsed[f.key] || '').trim() !== '');
}

/**
 * 这份配置到底该按哪个后端理解。
 *
 * ★ 默认值不是 `sqlite`，而是"**看有没有 MySQL 配置**"：
 *   老版本向导从不写 `HNE_DB_BACKEND`，但一定写 `HNE_MYSQL_HOST`。
 *   所以"缺 DB_BACKEND 但有 MYSQL_HOST"必须理解成 mysql，
 *   否则老用户的配置会被当成"缺项"，一升级就被拽回向导 —— 那是明确的回归。
 *   全新配置（向导默认 sqlite）会显式带上 `HNE_DB_BACKEND=sqlite`，不走这条推断。
 */
function resolvedBackend(parsed) {
  const explicit = String((parsed && parsed.HNE_DB_BACKEND) || '').trim();
  if (explicit) return explicit;
  const hasMysql = ['HNE_MYSQL_HOST', 'HNE_MYSQL_PASSWORD', 'HNE_MYSQL_USER']
    .some((key) => String((parsed && parsed[key]) || '').trim() !== '');
  return hasMysql ? 'mysql' : DEFAULT_DB_BACKEND;
}

/** 列出哪些必填项在已有配置里是缺的（给界面显示"还缺什么"）。 */
function missingRequired(parsed) {
  const backend = resolvedBackend(parsed || {});
  return SETUP_FIELDS
    .filter((f) => f.required && (!f.dbOnly || backend === 'mysql'))
    .filter((f) => !String((parsed && parsed[f.key]) || '').trim())
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
  DEFAULT_DB_BACKEND,
  databaseDefaults,
  defaultValues,
  suggestSecrets,
  preserveSecrets,
  validateValues,
  isFernetShaped,
  parsePortValue,
  renderEnvFile,
  parseEnvText,
  stashOf,
  readEnvFile,
  writeEnvFile,
  configIsUsable,
  resolvedBackend,
  missingRequired,
  quoteIfNeeded,
};
