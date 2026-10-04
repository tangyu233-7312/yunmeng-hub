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

/** 一份通过校验的最小配置。 */
function validValues(extra = {}) {
  return {
    ...cfg.defaultValues(),
    HNE_MYSQL_PASSWORD: 'p@ss word#1',
    HNE_SECRET_KEY: 'a'.repeat(48),
    ...extra,
  };
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
  const result = cfg.validateValues({});
  assert.equal(result.ok, false);
  for (const key of ['HNE_MYSQL_HOST', 'HNE_MYSQL_PORT', 'HNE_MYSQL_USER', 'HNE_MYSQL_PASSWORD', 'HNE_MYSQL_DB', 'HNE_SECRET_KEY']) {
    assert.ok(result.errors[key], `${key} 应该被判缺失`);
  }
  // 非必填的项不该报错
  assert.ok(!result.errors.HNE_API_KEY_ENCRYPTION_KEY);
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
  assert.equal(cfg.validateValues({ ...a, HNE_MYSQL_PASSWORD: 'x' }).ok, true);
  assert.notEqual(a.HNE_SECRET_KEY, b.HNE_SECRET_KEY);
  assert.notEqual(a.HNE_API_KEY_ENCRYPTION_KEY, b.HNE_API_KEY_ENCRYPTION_KEY);
  // Fernet 密钥必须是 44 位 base64
  assert.match(a.HNE_API_KEY_ENCRYPTION_KEY, /^[A-Za-z0-9+/]{43}=$/);
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
  assert.equal(cfg.configIsUsable({}), false);
  assert.equal(cfg.configIsUsable({ HNE_MYSQL_HOST: '127.0.0.1' }), false);
  assert.equal(cfg.configIsUsable(cfg.parseEnvText(cfg.renderEnvFile(validValues()))), true);
});

test('configIsUsable：被手改坏（必填项被清空）的配置判为不可用', () => {
  const broken = cfg.parseEnvText(cfg.renderEnvFile(validValues()));
  broken.HNE_MYSQL_PASSWORD = '';
  assert.equal(cfg.configIsUsable(broken), false);
});

test('missingRequired：列出缺哪些必填项（给界面显示"还缺什么"）', () => {
  assert.deepEqual(cfg.missingRequired(null).sort(), [
    'HNE_MYSQL_DB', 'HNE_MYSQL_HOST', 'HNE_MYSQL_PASSWORD', 'HNE_MYSQL_PORT', 'HNE_MYSQL_USER', 'HNE_SECRET_KEY',
  ].sort());

  const full = cfg.parseEnvText(cfg.renderEnvFile(validValues()));
  assert.deepEqual(cfg.missingRequired(full), []);

  delete full.HNE_MYSQL_USER;
  assert.deepEqual(cfg.missingRequired(full), ['HNE_MYSQL_USER']);
});

test('端到端：表单值 → .env 文本 → 解析 → 通过可用性判定', () => {
  const values = cfg.suggestSecrets({ ...cfg.defaultValues(), HNE_MYSQL_PASSWORD: '真实口令 with 空格' });
  const text = cfg.renderEnvFile(values);
  const parsed = cfg.parseEnvText(text);

  assert.equal(cfg.configIsUsable(parsed), true);
  assert.equal(parsed.HNE_MYSQL_PASSWORD, '真实口令 with 空格');
  assert.equal(parsed.HNE_MYSQL_HOST, '127.0.0.1');
  assert.equal(parsed.HNE_MYSQL_PORT, '3306');
});
