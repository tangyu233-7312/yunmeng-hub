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
//  "小眼睛"与复制（用户提的需求：随机生成的长串看不见，想核对/抄走）
// ------------------------------------------------------------------
test('setup.html：密码框有「小眼睛」，可切换明文/圆点', () => {
  const lines = codeLines(html);
  assert.ok(lines.some((l) => /if \(field\.secret\)/.test(l)), '只有密码框才该有眼睛');
  assert.match(html, /eye\.textContent = '👁'/, '默认图标是"眼睛"= 点击后显示');
  assert.match(html, /eye\.textContent = visible \? '🙈' : '👁'/,
    '切换后图标要跟着换（🙈 = 当前已显示，点它藏起来），否则用户不知道现在是哪种状态');
  assert.match(html, /input\.type = visible \? 'text' : 'password'/, '眼睛的本质就是切 input.type');
  // 默认必须仍然是隐藏：一打开页面就把密钥摊在屏幕上不合适
  assert.match(html, /const input = document\.createElement\('input'\);\s*\n\s*input\.type = field\.secret \? 'password' : 'text';/,
    '初始必须是 password（默认隐藏）');
});

test('★★ 回归：显示/隐藏的布尔参数必须按"用户视角"命名（本轮踩到语义反了）', () => {
  // 背景：输入框的 type 用的是"是否**隐藏**"（masked），而用户想的是"是否**可见**"。
  //   第一版函数叫 setMasked(key, masked)，两处调用各按自己的理解传值 ——
  //   结果默认就隐藏，点第一下等于"再隐藏一次"，表现成"点了没反应"。
  //   实测排查了四轮（图标在变、输入框不动）才定位到是**语义**反了，不是引用失效。
  //   现在参数叫 visible：`setVisible(key, true)` = 让用户看见。念得出来，就不会错。
  const lines = codeLines(html);
  assert.ok(lines.some((l) => /function setVisible\(key, visible\)/.test(l)),
    '参数要叫 visible（用户视角），不要叫 masked');
  assert.equal(lines.some((l) => /\bsetMasked\b/.test(l)), false,
    '不许再留 setMasked 这种"要翻一层"的命名');
  // 点击判断也要按用户视角：当前隐藏 → 这次就是要显示
  assert.match(html, /const nowHidden = live \? live\.type === 'password' : true/,
    '点击时的判断要读 DOM 里的真实状态');
  assert.match(html, /setVisible\(field\.key, nowHidden\)/,
    '当前隐藏 → setVisible(true)：点一下就该看见');
});

test('setup.html：「随机生成」之后顺手把内容显示出来', () => {
  // 用户刚点了"随机生成"，第一反应是"生成了什么、要不要存起来" —— 这时还蒙着圆点很反直觉
  assert.match(html, /clearError\(field\.key\);[\s\S]{0,300}setVisible\(field\.key, true\);/,
    '生成后应调用 setVisible(key, true) 把它显示出来');
});

test('setup.html：可生成的字段带「复制」按钮（抄进密码管理器用）', () => {
  assert.match(html, /copy\.textContent = '复制'/, '复制按钮存在');
  assert.match(html, /navigator\.clipboard\.writeText\(text\)/, '优先用剪贴板 API');
  assert.match(html, /document\.execCommand\('copy'\)/, '被拒时要退到 execCommand（否则等于按钮失灵）');
  // 空值不许静默成功
  assert.match(html, /这一格还是空的/, '空值要明确告诉用户，不能假装复制成功');
});

test('★ 接线：眼睛按钮被存进 inputs，否则 setMasked 改不动图标', () => {
  // 这个坑我真踩了：eye 用 const 声明在 if 块里，块外取不到 → 图标永远不变。
  assert.match(html, /let eyeBtn = null;/, 'eyeBtn 要在块外先声明（块级 const 出不了块）');
  assert.match(html, /inputs\[field\.key\] = \{ input, err, eye: eyeBtn \};/, '要把它存进 inputs');
});

test('★ 没有 eval：CSP 里没有 unsafe-eval，用了就会当场被拦', () => {
  const lines = codeLines(html).join('\n');
  assert.equal(/\beval\s*\(/.test(lines), false, '不许用 eval');
  assert.equal(/new Function\s*\(/.test(lines), false, '不许用 new Function');
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
