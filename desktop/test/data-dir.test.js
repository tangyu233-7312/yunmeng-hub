'use strict';

/**
 * 「数据目录（HNE_DATA_DIR）」这项可选项的自测。
 *
 * ==================== 为什么要专门测它 ====================
 * 这一项的作用是"改变**全部**数据的落点"（库文件 / 长期记忆 / 密钥 / 日志），
 * 而它踩坑的方式全是**静默**的：
 *   · 值没写进 `.env` → 用户以为搬了，其实没搬；
 *   · 被壳的 `--data-dir` 盖掉 → 界面保存成功、实际没生效（本轮实测确认过）；
 *   · 只有库文件跟着走、向量库没跟 → 搬家只搬走一半，且不报错。
 * 所以这里把"值真的落地"和"交给后端按新数据根推导"都变成可执行断言。
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const cfg = require('../src/setup-config');

test('★ 字段表里有 HNE_DATA_DIR，且不是必填、不跟存储方式绑定', () => {
  const field = cfg.SETUP_FIELDS.find((f) => f.key === 'HNE_DATA_DIR');
  assert.ok(field, '字段表里必须有「数据目录」这一项，否则界面根本渲染不出来');
  assert.equal(field.required, false, '留空 = 用默认位置，所以不能必填');
  // ★ 关键：不能标 dbOnly —— 那一组在选「本机文件」时整组隐藏，
  //   而这个字段两种后端都要能填（它就是"数据放哪"）。
  assert.ok(!field.dbOnly, '数据目录与存储方式无关，不能进 dbOnly 组');
  assert.ok(field.dirOnly, '要标 dirOnly，好让页面把它放进独立分组');
});

test('★ 用户填了数据目录 → 必须写进 .env（不能只停在界面上）', () => {
  const text = cfg.renderEnvFile({
    ...cfg.defaultValues(),
    HNE_DATA_DIR: 'D:\\云梦枢数据',
  });
  // ★ 正则里 `\\` 匹配**一个**字面反斜杠（写四个就变成匹配两个了 —— 这里是实测踩到的）。
  assert.match(text, /^HNE_DATA_DIR=D:\\云梦枢数据$/m,
    '填了就要写进配置文件，否则用户"改了但没生效"且毫无提示');
});

test('★ 留空 → **不写**这一行（让后端用默认位置，别写个空值进去）', () => {
  const text = cfg.renderEnvFile({ ...cfg.defaultValues(), HNE_DATA_DIR: '' });
  assert.doesNotMatch(text, /^HNE_DATA_DIR=/m,
    '留空时写 `HNE_DATA_DIR=` 会让后端拿到空串，属于"替用户做了决定"');
});

test('★ 数据目录那段要带上"不会自动搬家"的警告', () => {
  const text = cfg.renderEnvFile({
    ...cfg.defaultValues(),
    HNE_DATA_DIR: 'E:\\data',
  });
  assert.match(text, /不会自动搬家/,
    '换成新目录后旧数据不会自己过去 —— 这句必须写进文件里（用户以后只打开这个文件看）');
});

test('★★ 数据目录与存储方式正交：选 mysql / sqlite 都要写得出来', () => {
  for (const backend of ['sqlite', 'mysql']) {
    const values = {
      ...cfg.defaultValues(),
      HNE_DB_BACKEND: backend,
      HNE_DATA_DIR: 'E:\\shared',
      HNE_MYSQL_PASSWORD: 'pw',
    };
    const text = cfg.renderEnvFile(values);
    assert.match(text, /^HNE_DATA_DIR=E:\\shared$/m,
      `后端为 ${backend} 时也必须写出数据目录（两种后端都用它）`);
  }
});

test('★★ 数据目录里的值会被解析回来（写-读闭环，切换页才能预填）', () => {
  const text = cfg.renderEnvFile({
    ...cfg.defaultValues(),
    HNE_DATA_DIR: 'D:\\带 空格 的目录',
  });
  const parsed = cfg.parseEnvText(text);
  assert.equal(parsed.HNE_DATA_DIR, 'D:\\带 空格 的目录',
    '带空格的值必须能原样读回来（否则切换页预填的值是坏的）');
});

test('★ 手改过数据目录的老配置：以表单为准，但不会被当成"未知键"重复写两遍', () => {
  const before = [
    'HNE_DB_BACKEND=sqlite',
    'HNE_DATA_DIR=E:\\old',
    '',
  ].join('\n');
  const after = cfg.renderEnvFile(
    { ...cfg.defaultValues(), HNE_DATA_DIR: 'E:\\new' }, before, cfg.parseEnvText(before));
  const occurrences = after.split('\n').filter((l) => /^HNE_DATA_DIR=/.test(l.trim()));
  assert.equal(occurrences.length, 1, `HNE_DATA_DIR 只能出现一次，实际 ${occurrences.length} 次`);
  assert.match(after, /^HNE_DATA_DIR=E:\\new$/m, '以表单里的新值为准');
});
