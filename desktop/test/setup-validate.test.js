'use strict';

/**
 * 首次设置页「发请求前的体检」自测（`desktop/src/setup-validate.js`）。
 *
 * ★ 这一层存在的理由是一条真实体验缺陷（用户实测撞到）：
 *   他把 **MySQL 口令留空**就点了保存，应用于是起了一次真后端、拿空口令去连库，
 *   界面上滚出一大段 `1045 Access denied ... using password: YES` + Traceback。
 *   信息没错，但**对填表的人没有用** —— 真正的原因就是"那一格没填"。
 *   现在的规则：**本地一眼能看出来的问题，不许去起后端**；
 *   本地看不出来的（口令对不对、库存不存在）仍然交给真后端 —— 这一层不越界。
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const { precheck, isPortLike, isFernetLike } = require('../src/setup-validate');

/** 用真实的字段定义，避免"测试里的字段"和"界面上真正的字段"长得不一样。 */
const { SETUP_FIELDS } = require('../src/setup-config');

/**
 * 一份"走 MySQL"的完整表单值。
 *
 * ★ 为什么必须显式写 `HNE_DB_BACKEND: 'mysql'`：默认存储方式是 **sqlite**
 *   （零配置），那时 MySQL 字段整组不参与体检 —— 本文件里大量
 *   "MySQL 必填项要拦住"的断言就失去意义了。
 */
function mysqlValues(overrides) {
  const values = {};
  for (const f of SETUP_FIELDS) values[f.key] = f.default || '';
  values.HNE_DB_BACKEND = 'mysql';
  values.HNE_MYSQL_PASSWORD = 'p@ss word#1';
  values.HNE_SECRET_KEY = 'a'.repeat(48);
  return { ...values, ...(overrides || {}) };
}

/** 一份默认（本机文件 / 零配置）的表单值。 */
function sqliteValues(overrides) {
  const values = {};
  for (const f of SETUP_FIELDS) values[f.key] = f.default || '';
  return { ...values, ...(overrides || {}) };
}

test('全部填好 → 放行（不该拦）', () => {
  const r = precheck({ fields: SETUP_FIELDS, values: mysqlValues() });
  assert.equal(r.ok, true);
  assert.deepEqual(r.errors, {});
  assert.equal(r.message, '');
});

// ------------------------------------------------------------------
//  ★ 零配置：默认（本机文件）什么都不用填
// ------------------------------------------------------------------
test('★★ 零配置：默认表单（本机文件）直接放行 —— 一格都不用填', () => {
  const r = precheck({ fields: SETUP_FIELDS, values: sqliteValues() });
  assert.equal(r.ok, true, JSON.stringify(r.errors));
  assert.deepEqual(r.errors, {});
});

test('★★ 零配置：选本机文件时，MySQL 字段填成什么样都不拦', () => {
  const r = precheck({
    fields: SETUP_FIELDS,
    values: sqliteValues({ HNE_MYSQL_HOST: '有 空格', HNE_MYSQL_PORT: 'abc', HNE_MYSQL_PASSWORD: '' }),
  });
  assert.equal(r.ok, true, '那些格子在这个模式下根本不该显示，更不该拦住用户');
});

test('★★ 零配置：两个密钥留空也放行（后端会自己生成）', () => {
  const r = precheck({
    fields: SETUP_FIELDS,
    values: sqliteValues({ HNE_SECRET_KEY: '', HNE_API_KEY_ENCRYPTION_KEY: '' }),
  });
  assert.equal(r.ok, true, '密钥留空是合法状态：后端首次运行会自动生成');
});

test('★★ 回归：选了 MySQL 之后，口令留空 → 就地拦住，并且**不去起后端**', () => {
  // 这就是用户那次的情形。断言的重点是"返回 ok=false 且指名字段"，
  // 于是页面可以就地高亮、根本不会调用 selfCheckWithEnv（不需要真后端）。
  const r = precheck({ fields: SETUP_FIELDS, values: mysqlValues({ HNE_MYSQL_PASSWORD: '' }) });
  assert.equal(r.ok, false, '空口令必须被拦下');
  assert.ok(r.errors.HNE_MYSQL_PASSWORD, '必须指出是哪一格');
  assert.match(r.message, /MySQL 口令/, '提示里要说清缺哪一项（用户看得到标签）');
});

test('只有空格也算没填（不许拿空白糊过去）', () => {
  const r = precheck({ fields: SETUP_FIELDS, values: mysqlValues({ HNE_MYSQL_USER: '   ' }) });
  assert.equal(r.ok, false);
  assert.ok(r.errors.HNE_MYSQL_USER);
});

test('签名密钥：留空放行，但填了太短要被拦（后端会拒绝启动，不如在这里说）', () => {
  const empty = precheck({ fields: SETUP_FIELDS, values: mysqlValues({ HNE_SECRET_KEY: '' }) });
  assert.equal(empty.ok, true, '留空 = 后端自动生成，是合法的');

  const r = precheck({ fields: SETUP_FIELDS, values: mysqlValues({ HNE_SECRET_KEY: 'short' }) });
  assert.equal(r.ok, false);
  assert.match(r.errors.HNE_SECRET_KEY, /32/);
});

test('Fernet 口令：留空放行；形状不对要拦（后端要到"第一次存 API Key"才报错）', () => {
  assert.equal(precheck({ fields: SETUP_FIELDS, values: mysqlValues({ HNE_API_KEY_ENCRYPTION_KEY: '' }) }).ok, true);
  const bad = precheck({ fields: SETUP_FIELDS, values: mysqlValues({ HNE_API_KEY_ENCRYPTION_KEY: 'K'.repeat(44) }) });
  assert.equal(bad.ok, false);
  assert.ok(bad.errors.HNE_API_KEY_ENCRYPTION_KEY);
  assert.equal(isFernetLike('KioqKioqKioqKioqKioqKioqKioqKioqKioqKioqKio='), true);
});

test('端口必须是 1~65535 的数字', () => {
  assert.equal(isPortLike('3306'), true);
  assert.equal(isPortLike('1'), true);
  assert.equal(isPortLike('65535'), true);
  assert.equal(isPortLike('0'), false);
  assert.equal(isPortLike('65536'), false);
  assert.equal(isPortLike('3.5'), false, '第一版用 parseInt，3.5 会被当成 3 放过去');
  assert.equal(isPortLike('33 06'), false);
  assert.equal(isPortLike('abc'), false);

  const r = precheck({ fields: SETUP_FIELDS, values: mysqlValues({ HNE_MYSQL_PORT: '70000' }) });
  assert.equal(r.ok, false);
  assert.ok(r.errors.HNE_MYSQL_PORT);
});

test('非必填项留空不该被拦（比如 Fernet 口令、高级项）', () => {
  const r = precheck({
    fields: SETUP_FIELDS,
    values: mysqlValues({ HNE_API_KEY_ENCRYPTION_KEY: '', HNE_LOG_LEVEL: '', HNE_DEBUG: '' }),
  });
  assert.equal(r.ok, true, '非必填留空是合法的：后端会用自己的默认值');
});

test('一次报多项：提示里把缺的都列出来（不让用户挤牙膏）', () => {
  const r = precheck({
    fields: SETUP_FIELDS,
    values: mysqlValues({ HNE_MYSQL_PASSWORD: '', HNE_MYSQL_DB: '', HNE_MYSQL_USER: '' }),
  });
  assert.equal(r.ok, false);
  assert.equal(Object.keys(r.errors).length, 3);
  assert.match(r.message, /MySQL 口令/);
  assert.match(r.message, /数据库名/);
  assert.match(r.message, /MySQL 用户名/);
});

test('★ 不越界：它**不**假装能判断"口令对不对"', () => {
  // 这些只有真后端能回答（连库、建集合、写目录）。填了格式合法的值就必须放行，
  // 否则会把"口令错"这类真正该去试的情况误拦在外 —— 那就成了另一种骗人。
  const r = precheck({
    fields: SETUP_FIELDS,
    values: mysqlValues({ HNE_MYSQL_PASSWORD: 'definitely-the-wrong-password' }),
  });
  assert.equal(r.ok, true, '格式没问题就该放行，让真后端去判对错');
});

test('字段定义来自主进程：缺字段时安全返回（不抛异常）', () => {
  assert.deepEqual(precheck({}).ok, true);
  assert.deepEqual(precheck({ fields: [], values: {} }).ok, true);
  assert.deepEqual(precheck({ fields: SETUP_FIELDS, values: {} }).ok, false, '必填项一个都没填，当然不放过');
});
