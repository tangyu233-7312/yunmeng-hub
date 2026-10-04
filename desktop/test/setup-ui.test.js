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
  // 默认必须仍然是隐藏：一打开页面就把密钥摊在屏幕上不合适。
  // ★ 断言的是"密码框的初始 type 由 field.secret 决定"这条**契约**，
  //   而不是某一行的具体写法 —— 本页后来加了 `<select>`（存储方式），
  //   input 的创建被挪进了 if/else，逐字匹配旧写法会让这条断言变成"改了就坏"的假警报。
  assert.match(html, /input\.type = field\.secret \? 'password' : 'text';/,
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

// ------------------------------------------------------------------
//  存储方式切换（菜单里进来的那条路）
// ------------------------------------------------------------------
test('★★ 接线：切换模式必须走 setup:switch，而不是当首启 save', () => {
  // 这两条路的收尾**不同**：首启是"落盘并进入"，切换是"停旧后端、用新配置重启"。
  // 如果切换页误调 save()，旧后端不会被停 —— 新配置起在另一个端口，
  // 用户看到的还是旧数据，而界面上一切正常（最坏的一类故障）。
  assert.match(html, /api\.switchBackend\(collect\(\)\)/, '切换页要调 switchBackend');
  assert.match(html, /result = switchMode \? await api\.switchBackend\(collect\(\)\) : await api\.save\(collect\(\)\)/,
    '要有明确的分支，别指望同一句调用兼容两种模式');
  assert.match(html, /api\.switchFields\(\)/, '切换模式要用 switchFields 读字段（预填当前配置）');
  assert.match(html, /URLSearchParams\(location\.search\)\.get\('mode'\) === 'switch'/,
    '模式判据来自 ?mode=switch');
});

test('★ 接线：preload 要暴露 switchFields 与 switchBackend 两个入口', () => {
  const preload = fs.readFileSync(path.join(SRC, 'preload.js'), 'utf8');
  assert.match(preload, /switchFields:\s*\(\)\s*=>\s*ipcRenderer\.invoke\('setup:fields',\s*\{\s*mode:\s*'switch'\s*\}\)/);
  assert.match(preload, /switchBackend:\s*\(values\)\s*=>\s*ipcRenderer\.invoke\('setup:switch',\s*values\)/);
  // 主进程侧必须有对应的 handler
  assert.match(main, /ipcMain\.handle\('setup:switch'/);
  assert.match(main, /options\.mode === 'switch'/);
});

test('★★ 回归：`switchMode` 必须在模块作用域声明（曾在 boot 里声明 → 提交必抛错）', () => {
  // 真实事故：第一版把它写成 `const switchMode = …` 放在 `boot()` 内部，
  // 而 `submit` 处理函数在**模块作用域**引用它 → 每次提交抛
  // `ReferenceError: switchMode is not defined`。
  // 更坏的是它发生在 setStatus('busy') 与 disabled=true **之后**、try **之前**：
  //   · catch 不执行 → 用户看到状态栏纹丝不动、没有任何提示；
  //   · 「保存并重启后端」按钮永久灰掉 → 页面变成死的。
  const lines = codeLines(html);
  assert.ok(lines.some((l) => /^\s*let switchMode = false;\s*$/.test(l)),
    '要在模块作用域声明 `let switchMode = false;`');
  // boot() 里只能**赋值**，不许再声明（声明就会遮蔽成局部的，处理函数照样取不到）
  assert.equal(lines.some((l) => /^\s*(const|let)\s+switchMode\s*=/.test(l) && !/let switchMode = false;/.test(l)),
    false, 'boot() 里不许再声明 switchMode，只能赋值');
  assert.match(html, /switchMode = switchModeFromUrl;/,
    'boot() 要用赋值把 URL 里的模式写进模块作用域那个变量');
});

test('★★ 回归：提交处理函数必须用 finally 兜底解开按钮（否则一次异常就永久禁用）', () => {
  // 与上一条配套：即使还有别的"进 try 之前抛异常"的情况，也不能把按钮留在禁用态。
  // 断言的是**结构**（try 后面有 finally），不是某一句文案。
  const submitIdx = html.indexOf("el.form.addEventListener('submit'");
  assert.ok(submitIdx > 0, '找不到 submit 处理函数');
  const body = html.slice(submitIdx, submitIdx + 3000);
  assert.match(body, /}\s*finally\s*\{/, 'submit 处理函数必须有 finally');
  assert.match(body, /el\.save\.disabled = false;[\s\S]{0,120}el\.test\.disabled = false;/,
    'finally 里要把两个按钮都解开');
});

test('★★ 接线：切到本机文件时不许有**生效**的 MySQL 键，但必须留注释版', () => {
  // 这条同时钉住两件事，而且第二件是**用户实测的严重事故**：
  //   ① 选了 sqlite 就不该有生效的 `HNE_MYSQL_*`（否则读配置的人会以为在连 MySQL）；
  //   ② 但必须把 MySQL 连接信息以**注释**形式留着 —— 尤其是那串自动生成的
  //      32 位随机口令。旧版把它们整组删掉，用户切回来时口令格是空的、
  //      他又不可能记得随机串 → 只能瞎填 → 连不上 → 只看到"自检超时"。
  const cfg = require('../src/setup-config');
  const before = [
    'HNE_DB_BACKEND=mysql',
    'HNE_MYSQL_HOST=127.0.0.1',
    'HNE_MYSQL_USER=narrative_app',
    'HNE_MYSQL_PASSWORD=SomeRandomGeneratedPassword1234',
    'HNE_MYSQL_DB=narrative_engine',
  ].join('\n');
  const after = cfg.renderEnvFile(
    { HNE_DB_BACKEND: 'sqlite' }, before, cfg.parseEnvText(before));
  assert.doesNotMatch(after, /^HNE_MYSQL_/m, '选 sqlite 时不许有生效的 MySQL 键');
  assert.match(after, /^# HNE_MYSQL_PASSWORD=/m, '★ 口令必须留成注释（否则用户切不回来）');
  assert.equal(cfg.stashOf(after).HNE_MYSQL_PASSWORD, 'SomeRandomGeneratedPassword1234');
});

test('★★ 接线：切换页要把注释里那份连接信息填回表单（含口令）', () => {
  // 光把值留在文件里还不够 —— 切换页得**把它填回输入框**，否则用户还是看不到、
  // 还是得自己回忆。这一段守的就是"填回去"这条接线。
  const body = main.slice(main.indexOf('function buildSetupFields'));
  assert.match(body, /setupConfig\.stashOf\(existingText\)/,
    '切换模式要用 stashOf 把注释里那份读回来');
  assert.match(body, /if \(switchMode\)/, '只在切换模式做（首启向导不该看到旧连接信息）');
  // 口令属于 secret 字段，主进程会把它的值放进 values 里由页面统一填充；
  // 这里断言"stash 的值确实进了 values"（而不是被 secret 过滤掉）。
  const stashIdx = body.indexOf('setupConfig.stashOf(existingText)');
  const block = body.slice(stashIdx, stashIdx + 600);
  assert.match(block, /values\[key\] = value/, 'stash 里的值要写进 values（页面据此填充）');
});

test('★★ 接线：切换必须先自检、成功后才停旧后端（失败不能把在跑的后端弄停）', () => {
  // 顺序反了会怎样：用户点一下切换、填错了 MySQL 口令 → 后端被停掉、
  // 新配置又起不来 → 他连"用回原来的存储"都做不到（界面全挂）。
  // 所以断言的是**语句顺序**，不是某一句存在。
  const start = main.indexOf("ipcMain.handle('setup:switch'");
  assert.ok(start > 0, '找不到 setup:switch handler');
  const body = main.slice(start, start + 4000);
  const checkAt = body.indexOf("selfCheckWithEnv(envFile, '切换前验证')");
  const stopAt = body.indexOf("stopBackend('切换存储方式')");
  assert.ok(checkAt > 0, '切换里必须先跑一次自检');
  assert.ok(stopAt > 0, '切换成功后要停掉旧后端');
  assert.ok(checkAt < stopAt, '自检必须在停旧后端**之前**（否则失败时用户就没有后端可用了）');
  // ★ 失败分支要说清"原设置被恢复"，而且**真的做回滚**（不只是嘴上说）。
  //   第一版只做到"不重启后端"，文件却已被写坏 —— 失败会被推迟到**下次启动**
  //   才发作，而那时用户早忘了自己"只是试了一下"。
  assert.match(body, /已把设置恢复成原来的样子/, '失败时要说清原设置被恢复');
  assert.match(body, /writeEnvFile\(paths\.userEnvFile, existingText\)/,
    '失败时必须把原文件内容写回去（原子回滚）');
  const rollbackAt = body.indexOf('writeEnvFile(paths.userEnvFile, existingText)');
  const failReturnAt = body.indexOf('已把设置恢复成原来的样子');
  assert.ok(rollbackAt > 0 && failReturnAt > 0 && rollbackAt < failReturnAt,
    '要**先回滚再返回**失败结论（顺序反了等于没回滚）');
});
