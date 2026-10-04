'use strict';

/**
 * `desktop/src/setup-config.js` 的自测。
 *
 * ★ 这一层管的是"用户第一次打开应用时填的那点东西"：字段校验、`.env` 生成与解析。
 *   它的 bug 表现都很坏 —— 要么写出一份**后端读不了**的配置（用户以为配好了），
 *   要么把用户手改过的其它设置**悄悄抹掉**（静默改用户的配置）。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const cfg = require('../src/setup-config');

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hne-setup-'));
}

/**
 * 一份通过校验的最小配置 —— 走 **MySQL 模式**。
 *
 * ★ 为什么显式带 `HNE_DB_BACKEND=mysql`：默认存储方式是 **sqlite**（零配置），
 *   那样 MySQL 字段整组不参与校验，本文件里大量"MySQL 字段必填"的断言就失去意义了。
 *   它们是"选了 MySQL 之后必须拦住"的断言，所以这里必须把模式说清楚。
 */
function validValues(extra = {}) {
  return {
    ...cfg.defaultValues(),
    HNE_DB_BACKEND: 'mysql',
    HNE_MYSQL_PASSWORD: 'p@ss word#1',
    HNE_SECRET_KEY: 'a'.repeat(48),
    ...extra,
  };
}

/** 默认（SQLite / 零配置）的一组表单值。 */
function sqliteValues(extra = {}) {
  return { ...cfg.defaultValues(), ...extra };
}

// ------------------------------------------------------------------
//  字段定义
// ------------------------------------------------------------------
test('SETUP_FIELDS：键名都带 HNE_ 前缀（项目硬约定：否则会与系统变量撞车）', () => {
  for (const field of cfg.SETUP_FIELDS) {
    assert.match(field.key, /^HNE_[A-Z0-9_]+$/, `${field.key} 没有 HNE_ 前缀`);
  }
});

test('defaultValues：给出所有字段的默认值，且必填项默认能过校验的是连库那几项', () => {
  const values = cfg.defaultValues();
  for (const field of cfg.SETUP_FIELDS) {
    assert.ok(field.key in values, `${field.key} 没有默认值`);
  }
  // 口令与签名密钥没有合理默认值（不该替用户编一个能用的）
  assert.equal(values.HNE_MYSQL_PASSWORD, '');
  assert.equal(values.HNE_SECRET_KEY, '');
});

// ------------------------------------------------------------------
//  校验
// ------------------------------------------------------------------
test('validateValues：缺必填项时逐字段报错（不是只给一句"配置不对"）', () => {
  // MySQL 模式：连库那几项一个都不能少
  const result = cfg.validateValues({ HNE_DB_BACKEND: 'mysql' });
  assert.equal(result.ok, false);
  for (const key of ['HNE_MYSQL_HOST', 'HNE_MYSQL_PORT', 'HNE_MYSQL_USER', 'HNE_MYSQL_PASSWORD', 'HNE_MYSQL_DB']) {
    assert.ok(result.errors[key], `${key} 应该被判缺失`);
  }
  // 非必填的项不该报错
  assert.ok(!result.errors.HNE_API_KEY_ENCRYPTION_KEY);
});

// ------------------------------------------------------------------
//  ★ 零配置：默认（SQLite）什么都不用填
// ------------------------------------------------------------------
test('★★ validateValues：默认（本机文件）配置**一项都不用填**就通过', () => {
  const result = cfg.validateValues(sqliteValues());
  assert.equal(result.ok, true, JSON.stringify(result.errors));
});

test('★★ validateValues：默认模式下 MySQL 字段**完全不参与校验**', () => {
  // 故意把 MySQL 字段填成明显错误的形态；选 sqlite 时它们不该被看
  const result = cfg.validateValues(sqliteValues({
    HNE_MYSQL_HOST: '有 空格 的主机',
    HNE_MYSQL_PORT: 'not-a-port',
    HNE_MYSQL_PASSWORD: '',
  }));
  assert.equal(result.ok, true, '选本机文件时，MySQL 字段不该拦住保存');
  assert.deepEqual(result.errors, {});
});

test('validateValues：显式选 MySQL 之后，那一组又重新变成必填', () => {
  // ★ 注意默认值已经给主机/端口/用户名/库名填了合理默认（127.0.0.1 / 3306 / …），
  //   所以"切到 MySQL 之后还缺什么"这件事上，**口令是唯一没有合理默认的一项**。
  //   第一版这里断言 HNE_MYSQL_HOST 会报错 —— 那是错的（它有默认值，本来就不缺）。
  const withDefaults = cfg.validateValues(sqliteValues({ HNE_DB_BACKEND: 'mysql' }));
  assert.equal(withDefaults.ok, false, '切到 MySQL 后，空口令必须被拦下');
  assert.ok(withDefaults.errors.HNE_MYSQL_PASSWORD);

  // 把 MySQL 那几项都清空，则必须**逐项**都报出来
  const empty = cfg.validateValues({
    HNE_DB_BACKEND: 'mysql',
    HNE_MYSQL_HOST: '',
    HNE_MYSQL_PORT: '',
    HNE_MYSQL_USER: '',
    HNE_MYSQL_PASSWORD: '',
    HNE_MYSQL_DB: '',
  });
  assert.equal(empty.ok, false);
  for (const key of ['HNE_MYSQL_HOST', 'HNE_MYSQL_PORT', 'HNE_MYSQL_USER', 'HNE_MYSQL_PASSWORD', 'HNE_MYSQL_DB']) {
    assert.ok(empty.errors[key], `${key} 应该被判缺失`);
  }
});

test('★★ validateValues：两个密钥**可以留空**（后端会自动生成）', () => {
  const result = cfg.validateValues(sqliteValues({ HNE_SECRET_KEY: '', HNE_API_KEY_ENCRYPTION_KEY: '' }));
  assert.equal(result.ok, true, '密钥留空不该被拦 —— 后端首次运行会自己生成');
});

test('validateValues：密钥填了就得合法（否则用户以为"我设了"，后端却另生成一把）', () => {
  assert.equal(cfg.validateValues(sqliteValues({ HNE_SECRET_KEY: 'too-short' })).ok, false);
  assert.equal(cfg.validateValues(sqliteValues({ HNE_API_KEY_ENCRYPTION_KEY: 'not-44-chars' })).ok, false);
  // 长度对但形状错（44 个 base64url 字符、没有 '='）也必须被拦：
  // 它解码出来是 33 字节，后端会拒
  assert.equal(cfg.validateValues(sqliteValues({ HNE_API_KEY_ENCRYPTION_KEY: 'K'.repeat(44) })).ok, false);
  // 合法的 Fernet 形状：43 个 base64url 字符 + 一个 '='
  assert.equal(cfg.validateValues(sqliteValues({ HNE_API_KEY_ENCRYPTION_KEY: 'KioqKioqKioqKioqKioqKioqKioqKioqKioqKioqKio=' })).ok, true);
});

test('validateValues：合法配置通过', () => {
  const result = cfg.validateValues(validValues());
  assert.equal(result.ok, true, JSON.stringify(result.errors));
});

test('validateValues：端口必须是 1~65535 的整数', () => {
  for (const bad of ['0', '70000', 'abc', '3.5', '-1']) {
    const result = cfg.validateValues(validValues({ HNE_MYSQL_PORT: bad }));
    assert.equal(result.ok, false, `端口 ${bad} 不该通过`);
    assert.ok(result.errors.HNE_MYSQL_PORT);
  }
  for (const good of ['1', '3306', '65535']) {
    assert.equal(cfg.validateValues(validValues({ HNE_MYSQL_PORT: good })).ok, true, `端口 ${good} 应该通过`);
  }
});

test('validateValues：签名密钥太短会被拦（后端会拒绝启动，不如在这里说）', () => {
  const result = cfg.validateValues(validValues({ HNE_SECRET_KEY: 'short' }));
  assert.equal(result.ok, false);
  assert.match(result.errors.HNE_SECRET_KEY, /32/);
});

test('validateValues：主机名里有空格会被拦（最常见的粘贴事故）', () => {
  const result = cfg.validateValues(validValues({ HNE_MYSQL_HOST: '127.0.0.1 ' }));
  // 两侧空格会被 trim 掉 → 通过；中间的空格才是问题
  assert.equal(result.ok, true);
  const withInner = cfg.validateValues(validValues({ HNE_MYSQL_HOST: '127.0. 0.1' }));
  assert.equal(withInner.ok, false);
  assert.ok(withInner.errors.HNE_MYSQL_HOST);
});

// ------------------------------------------------------------------
//  随机生成
// ------------------------------------------------------------------
test('suggestSecrets：只给空着的密钥字段生成值，不覆盖用户已填的', () => {
  const values = { ...cfg.defaultValues(), HNE_SECRET_KEY: 'user-provided-key-that-is-long-enough' };
  const suggested = cfg.suggestSecrets(values);
  assert.equal(suggested.HNE_SECRET_KEY, 'user-provided-key-that-is-long-enough');
  assert.ok(suggested.HNE_API_KEY_ENCRYPTION_KEY.length > 0, '空着的加密口令应该被生成');
});

test('生成的密钥能通过校验，且两次生成不同（真的是随机的）', () => {
  const a = cfg.suggestSecrets(cfg.defaultValues());
  const b = cfg.suggestSecrets(cfg.defaultValues());
  // ★ 必须在**同一个模式下**校验：默认是 SQLite，而 SQLite 下 MySQL 口令不参与校验
  //   （所以这里显式走 MySQL，才真正验证了"生成出来的密钥能过校验"）。
  assert.equal(cfg.validateValues({ ...a, HNE_DB_BACKEND: 'mysql', HNE_MYSQL_PASSWORD: 'x' }).ok, true);
  assert.notEqual(a.HNE_SECRET_KEY, b.HNE_SECRET_KEY);
  assert.notEqual(a.HNE_API_KEY_ENCRYPTION_KEY, b.HNE_API_KEY_ENCRYPTION_KEY);
  // ★ Fernet 密钥是 base64**url** 变体（字母表含 `-` `_`，不含 `+` `/`），
  //   形状固定为 43 个字符 + 一个 '='。这一条同时守着"生成器与校验器讲同一种语言"。
  assert.match(a.HNE_API_KEY_ENCRYPTION_KEY, /^[A-Za-z0-9_-]{43}=$/);
  assert.equal(cfg.isFernetShaped(a.HNE_API_KEY_ENCRYPTION_KEY), true);
});

// ------------------------------------------------------------------
//  .env 生成
// ------------------------------------------------------------------
test('renderEnvFile：口令里有空格与 # 时会被引号包住（否则会被截断/弄脏）', () => {
  const text = cfg.renderEnvFile(validValues({ HNE_MYSQL_PASSWORD: 'p@ss word#1' }));
  assert.match(text, /HNE_MYSQL_PASSWORD="p@ss word#1"/);
  // 反向确认：解析回来必须一模一样
  assert.equal(cfg.parseEnvText(text).HNE_MYSQL_PASSWORD, 'p@ss word#1');
});

test('renderEnvFile：解析回来的值必须与填进去的**逐字节一致**（含各种刁钻口令）', () => {
  const nasty = [
    'simple',
    'with space',
    'with#hash',
    'with"quote',
    "with'quote",
    'with\\backslash',
    'A1!@#$%^&*()_+-=[]{}|;:,.<>?/~`',
    '中文口令也可以',
  ];
  for (const password of nasty) {
    const text = cfg.renderEnvFile(validValues({ HNE_MYSQL_PASSWORD: password }));
    const parsed = cfg.parseEnvText(text);
    assert.equal(parsed.HNE_MYSQL_PASSWORD, password, `口令 ${JSON.stringify(password)} 往返不一致`);
  }
});

test('renderEnvFile：写进固定值（只监听本机、用本地嵌入），不写会被误读的项', () => {
  const parsed = cfg.parseEnvText(cfg.renderEnvFile(validValues()));
  assert.equal(parsed.HNE_HOST, '127.0.0.1');
  assert.equal(parsed.HNE_EMBEDDING_BACKEND, 'onnx_default');
});

test('★ renderEnvFile：从 MySQL 改回「本机文件」时，MySQL 的键必须被清掉', () => {
  // 用户先填了 MySQL，又改回本机文件 —— 这是切换存储方式的真实路径
  const initial = cfg.parseEnvText(cfg.renderEnvFile(validValues()));
  const rewritten = cfg.parseEnvText(cfg.renderEnvFile(sqliteValues(), cfg.renderEnvFile(validValues())));

  assert.equal(rewritten.HNE_DB_BACKEND, 'sqlite');
  for (const key of Object.keys(initial)) {
    if (!key.startsWith('HNE_MYSQL_')) continue;
    assert.ok(!(key in rewritten), `${key} 应该被清掉（否则配置里留着会让人以为还在用 MySQL）`);
  }
});

test('★ renderEnvFile：用户手工加过的其它配置必须原样保留（不能悄悄抹掉）', () => {
  const existing = [
    '# 我自己加的',
    'HNE_CORS_ORIGINS=["http://localhost:5173"]',
    'HNE_BCRYPT_ROUNDS=10',
    '',
    'HNE_MYSQL_HOST=old-host',   // 这个我们认识 → 应该以表单为准
  ].join('\n');

  const text = cfg.renderEnvFile(validValues({ HNE_MYSQL_HOST: 'new-host' }), existing);
  const parsed = cfg.parseEnvText(text);

  assert.equal(parsed.HNE_MYSQL_HOST, 'new-host', '认识的键要以表单为准');
  assert.equal(parsed.HNE_CORS_ORIGINS, '["http://localhost:5173"]', '不认识的键必须保留');
  assert.equal(parsed.HNE_BCRYPT_ROUNDS, '10', '不认识的键必须保留');
  assert.match(text, /你手工加过的其它配置/);
});

test('renderEnvFile：高级项只有填了才写进去（不塞一堆用户没设过的东西）', () => {
  const withoutAdvanced = cfg.parseEnvText(cfg.renderEnvFile(validValues()));
  assert.ok(!('HNE_DEBUG' in withoutAdvanced), '没填的高级项不该出现');

  const withAdvanced = cfg.parseEnvText(cfg.renderEnvFile(validValues({ HNE_DEBUG: 'true' })));
  assert.equal(withAdvanced.HNE_DEBUG, 'true');
});

// ------------------------------------------------------------------
//  解析
// ------------------------------------------------------------------
test('parseEnvText：去 BOM、跳过注释、去成对引号、值里保留 =', () => {
  const parsed = cfg.parseEnvText('\uFEFF# 注释\nA=1\nB="x=y z"\nC=\'q\'\n');
  assert.deepEqual(parsed, { A: '1', B: 'x=y z', C: 'q' });
});

test('quoteIfNeeded：只有需要时才加引号', () => {
  assert.equal(cfg.quoteIfNeeded('plain'), 'plain');
  assert.equal(cfg.quoteIfNeeded('has space'), '"has space"');
  assert.equal(cfg.quoteIfNeeded('has#hash'), '"has#hash"');
  assert.equal(cfg.quoteIfNeeded(''), '""');
});

// ------------------------------------------------------------------
//  读写与"可用性"判定
// ------------------------------------------------------------------
test('readEnvFile：文件不存在返回 null（而不是抛异常）', () => {
  assert.equal(cfg.readEnvFile(path.join(tempDir(), 'nope.env')), null);
});

test('writeEnvFile：能写出并读回，且不会留下 .tmp 残file', () => {
  const dir = tempDir();
  const target = path.join(dir, 'config', '.env');
  const text = cfg.renderEnvFile(validValues());

  cfg.writeEnvFile(target, text);

  assert.equal(fs.readFileSync(target, 'utf8'), text);
  assert.equal(fs.existsSync(`${target}.tmp`), false, '不该留下临时文件');
  assert.equal(fs.existsSync(path.dirname(target)), true, '父目录应该被自动创建');
});

test('configIsUsable：缺必填项的配置判为不可用（否则应用会反复启动失败却没有入口去改）', () => {
  assert.equal(cfg.configIsUsable(null), false);
  // ★ 空对象/只有一半 MySQL 信息：这些值**不是向导表单值**（表单有默认值），
  //   而是"一个被手改坏、或缺项的 .env" —— 所以必须判为不可用。
  //   ★ 注意不能拿 `cfg.defaultValues()` 来断言这一点：那份默认值里
  //     `HNE_DB_BACKEND=sqlite` 而 MySQL 字段只有一个默认主机名，
  //     属于"向导还没保存过"的中间状态，本来就不该按 .env 的判据去衡量。
  assert.equal(cfg.configIsUsable({}), false);
  assert.equal(cfg.configIsUsable({ HNE_DB_BACKEND: 'mysql' }), false, '选了 MySQL 却什么都没填');
  assert.equal(cfg.configIsUsable({ HNE_DB_BACKEND: 'mysql', HNE_MYSQL_HOST: 'h' }), false, '缺口令');
  // ★ 反过来：选了本机文件时，**连空口令都不算缺项** ——
  //   这正是"默认零配置一项都不用填"在可用性判据上的体现。
  //   （`defaultValues()` 就是这种情况：存储方式=sqlite，MySQL 口令留空。）
  assert.equal(cfg.configIsUsable(cfg.defaultValues()), true);
  // ★ 反过来：**只有一个存储方式**就是一份完全可用的零配置。
  //   连"残留了一个 MySQL 主机名"也仍然可用 —— 选 sqlite 时那些字段根本不参与判断，
  //   这正是"改回本机文件之后老字段不会拦住用户"的保证。
  assert.equal(cfg.configIsUsable({ HNE_DB_BACKEND: 'sqlite' }), true);
  assert.equal(cfg.configIsUsable({ HNE_DB_BACKEND: 'sqlite', HNE_MYSQL_HOST: '127.0.0.1' }), true);
  // ★ 老版本向导写的配置（没有 HNE_DB_BACKEND，但有完整 MySQL 信息）必须判为可用 ——
  //   否则老用户一升级就被拽回向导，那是最典型的回归。
  assert.equal(cfg.configIsUsable({
    HNE_MYSQL_HOST: '127.0.0.1',
    HNE_MYSQL_PORT: '3306',
    HNE_MYSQL_USER: 'u',
    HNE_MYSQL_PASSWORD: 'p',
    HNE_MYSQL_DB: 'd',
  }), true);
  assert.equal(cfg.configIsUsable(cfg.parseEnvText(cfg.renderEnvFile(validValues()))), true);
  // ★ 零配置：向导只写出「本机文件」那一行，也是可用配置（不需要任何别的东西）
  assert.equal(cfg.configIsUsable(cfg.parseEnvText(cfg.renderEnvFile(sqliteValues()))), true);
});

test('resolvedBackend：缺 HNE_DB_BACKEND 时按"有没有 MySQL 配置"推断（向后兼容的关键）', () => {
  assert.equal(cfg.resolvedBackend({}), 'sqlite');
  assert.equal(cfg.resolvedBackend({ HNE_DB_BACKEND: 'mysql' }), 'mysql');
  assert.equal(cfg.resolvedBackend({ HNE_DB_BACKEND: 'sqlite', HNE_MYSQL_HOST: 'h' }), 'sqlite');
  // 老配置：没有 DB_BACKEND，但有 MySQL 信息 → 必须理解成 mysql
  assert.equal(cfg.resolvedBackend({ HNE_MYSQL_HOST: 'h', HNE_MYSQL_PASSWORD: 'p' }), 'mysql');
});

test('configIsUsable：被手改坏（必填项被清空）的配置判为不可用', () => {
  const broken = cfg.parseEnvText(cfg.renderEnvFile(validValues()));
  broken.HNE_MYSQL_PASSWORD = '';
  assert.equal(cfg.configIsUsable(broken), false);
});

test('missingRequired：列出缺哪些必填项（给界面显示"还缺什么"）', () => {
  // 完全空的配置：连"存储方式"都还没有，而它默认是 sqlite → 只差它自己
  assert.deepEqual(cfg.missingRequired(null), ['HNE_DB_BACKEND']);
  // 显式 sqlite：什么也不缺（其余字段都不是必填）
  assert.deepEqual(cfg.missingRequired({ HNE_DB_BACKEND: 'sqlite' }), []);
  // 显式 mysql：连库那几项全都要
  assert.deepEqual(cfg.missingRequired({ HNE_DB_BACKEND: 'mysql' }).sort(), [
    'HNE_MYSQL_DB', 'HNE_MYSQL_HOST', 'HNE_MYSQL_PASSWORD', 'HNE_MYSQL_PORT', 'HNE_MYSQL_USER',
  ].sort());

  const full = cfg.parseEnvText(cfg.renderEnvFile(validValues()));
  assert.deepEqual(cfg.missingRequired(full), []);

  delete full.HNE_MYSQL_USER;
  assert.deepEqual(cfg.missingRequired(full), ['HNE_MYSQL_USER']);
});

test('端到端（MySQL 模式）：表单值 → .env 文本 → 解析 → 通过可用性判定', () => {
  const values = cfg.suggestSecrets({ ...validValues() });
  const text = cfg.renderEnvFile(values);
  const parsed = cfg.parseEnvText(text);

  assert.equal(cfg.configIsUsable(parsed), true);
  assert.equal(parsed.HNE_DB_BACKEND, 'mysql');
  assert.equal(parsed.HNE_MYSQL_PASSWORD, 'p@ss word#1');
  assert.equal(parsed.HNE_MYSQL_HOST, '127.0.0.1');
  assert.equal(parsed.HNE_MYSQL_PORT, '3306');
});

test('★★ 端到端（零配置 / SQLite）：默认表单直接产出可用配置，且**不含任何 MySQL 键**', () => {
  const text = cfg.renderEnvFile(sqliteValues());
  const parsed = cfg.parseEnvText(text);

  assert.equal(cfg.configIsUsable(parsed), true);
  assert.equal(parsed.HNE_DB_BACKEND, 'sqlite');
  // ★ 关键：选本机文件时，一个 MySQL 键都不该被写进配置 ——
  //   否则用户会以为"这里配了 MySQL"，而实际上我们只想让他什么都不用管。
  for (const key of Object.keys(parsed)) {
    assert.ok(!key.startsWith('HNE_MYSQL_'), `${key} 不该出现在零配置里`);
  }
});
