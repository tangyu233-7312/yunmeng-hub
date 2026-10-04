'use strict';

/**
 * 首次设置页（`setup.html`）与主进程（`main.js`）之间的**契约**，用静态方式钉住。
 *
 * ==================== 为什么需要这样一个测试 ====================
 * 这两边隔着一条 **IPC 边界**，而 IPC 只能传**数据**、传不了函数。
 * 本轮就因此出了一个用户实测抓到的真 bug：
 *
 *   · 主进程发的是 `generate: 'HNE_SECRET_KEY'`（字符串，或者 null）；
 *   · 渲染端判断的是 `typeof field.generate === 'function'` —— **永远为假**；
 *   · 于是「随机生成」按钮**从来没有被创建过**，可提示文字却写着
 *     "点右边的「随机生成」更省事"。用户照着找，界面上根本没有那个东西。
 *
 * ★ 为什么我的自动验收没抓到它：验收脚本是**直接调用**
 *   `window.yunmengSetup.generate(...)` 去填密钥的 —— **绕过了按钮**，
 *   而验收里也没有一条断言检查"这个按钮存在吗"。
 *   **测试替我做了用户要做的事，于是它替我把 bug 藏了起来。**
 *
 * 所以这里用"读源码 + 断言两侧契约"的方式兜底：任何一边单方面改名/改类型，
 * 这个测试立刻红。它不依赖 Electron、不依赖 MySQL，跑得飞快。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SRC = path.resolve(__dirname, '..', 'src');
const html = fs.readFileSync(path.join(SRC, 'setup.html'), 'utf8');
const main = fs.readFileSync(path.join(SRC, 'main.js'), 'utf8');

/** 只看代码行：注释里会出现旧写法（本轮就是这么写的注释），不能拿它当证据。 */
function codeLines(text) {
  return text
    .split(/\r?\n/)
    .filter((line) => {
      const t = line.trim();
      return !(t.startsWith('//') || t.startsWith('*') || t.startsWith('/*') || t.startsWith('<!--'));
    });
}

// ------------------------------------------------------------------
//  渲染端（setup.html）
// ------------------------------------------------------------------
test('setup.html：按钮的判据必须是布尔量 generatable（不能是 typeof === function）', () => {
  const lines = codeLines(html);
  assert.ok(
    lines.some((l) => /if \(field\.generatable\)/.test(l)),
    '渲染端必须用 `if (field.generatable)` 决定是否画「随机生成」按钮',
  );
  // 反面教材：函数过不了 IPC，typeof 判断永远为假 —— 这条断言就是本轮 bug 的墓碑
  assert.ok(
    !lines.some((l) => /typeof field\.generate === 'function'/.test(l)),
    '不许再用 `typeof field.generate === \'function\'`：跨 IPC 传过来的是数据，不是函数',
  );
});

test('setup.html：「随机生成」按钮存在，且调用的入口名与 preload 暴露的一致', () => {
  assert.match(html, /btn\.textContent = '随机生成'/, '按钮文案就是用户看到的那个词');
  assert.match(html, /api\.generate\(field\.key\)/, '按钮必须按 key 向主进程要新值');
  // 提示文字与按钮必须同时存在：本轮 bug 就是"有文字、没按钮"
  assert.match(html, /随机生成/, '提示里提到「随机生成」');
});

test('setup.html：提示文字里提到的按钮，代码里真的会创建它', () => {
  // 这条是"用户视角"的断言：文案说"点右边的「随机生成」"，那就必须真有按钮。
  const mentionsButton = /「随机生成」/.test(html);
  const createsButton = /btn\.textContent = '随机生成'/.test(html);
  assert.equal(mentionsButton, createsButton, '提到就必须真的有；否则等于骗用户',
    '文案提到了「随机生成」但代码没有创建它的分支（或反过来）');
});

// ------------------------------------------------------------------
//  主进程（main.js）
// ------------------------------------------------------------------
test('main.js：发给渲染端的字段里，generatable 必须是布尔量', () => {
  const match = main.match(/generatable:\s*([^,\n]+)/);
  assert.ok(match, 'main.js 必须提供 generatable 字段');
  const expr = match[1].trim();
  assert.equal(
    expr,
    "typeof f.generate === 'function'",
    '它应当是"字段定义里到底有没有生成函数"的计算结果（布尔量）',
  );
  // 关键：**不能**再把字段名当数据发过去（那正是本轮的 bug）。
  // ★ 只看**代码行**：我在这段代码上面写了注释解释这个 bug，
  //   注释里当然会出现旧写法 —— 第一版断言就是被自己的注释绊倒的。
  const stale = codeLines(main).some((l) => /generate:\s*typeof f\.generate/.test(l));
  assert.equal(stale, false, '不许再把 `generate: <字符串>` 发过 IPC —— 渲染端按函数判断，永远为假');
});

test('★ 契约两侧一致：一个发 generatable，一个读 generatable', () => {
  assert.ok(/generatable:/.test(main), '主进程要发 generatable');
  assert.ok(/field\.generatable/.test(html), '渲染端要读 generatable');
  // 两侧都不许残留旧的 `generate` 数据字段（只允许 IPC 频道名 setup:generate）
  const staleInHtml = codeLines(html).some((l) => /\bfield\.generate\b/.test(l));
  assert.equal(staleInHtml, false, '渲染端不该再读 field.generate');
});

// ------------------------------------------------------------------
//  字段定义侧：确实存在"可以随机生成"的字段（否则按钮没有出现的理由）
// ------------------------------------------------------------------
test('setup-config.js：至少有一个字段带 generate 函数（密钥类）', () => {
  const cfg = require('../src/setup-config');
  const generatable = cfg.SETUP_FIELDS.filter((f) => typeof f.generate === 'function');
  assert.ok(generatable.length >= 1, '至少要有一个可生成的字段（签名密钥 / Fernet 口令）');
  const keys = generatable.map((f) => f.key);
  assert.ok(keys.includes('HNE_SECRET_KEY'), `签名密钥必须可生成，实际：${keys.join(', ')}`);
  // 生成器要真的产出非空、够长的值
  for (const f of generatable) {
    const value = f.generate();
    assert.equal(typeof value, 'string', `${f.key} 的生成器要返回字符串`);
    assert.ok(value.length >= 32, `${f.key} 的生成值太短（${value.length}）`);
  }
});

test('setup-config.js：不可生成的字段不该带 generate（否则会画出没用的按钮）', () => {
  const cfg = require('../src/setup-config');
  for (const f of cfg.SETUP_FIELDS) {
    if (f.generate !== undefined) {
      assert.equal(typeof f.generate, 'function', `${f.key} 的 generate 必须是函数或干脆没有`);
    }
  }
});
