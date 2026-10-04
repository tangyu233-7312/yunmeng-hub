'use strict';

/**
 * 「零配置」这一版的核心不变量：**用户什么都不填也能跑起来，且不会被静默改配置**。
 *
 * ==================== 为什么单独开一个文件 ====================
 * 这个特性跨越**两个模块**（一个管进程环境怎么拼、一个管向导表单与 .env 文本），
 * 它们刻意不互相 import，所以只有把两边**同时**断言，才能保证"它们讲的是同一件事"。
 *
 * 这里盯的三件事，任何一件坏掉都会造成"用户看着一切正常、数据却去了别处"：
 *
 *   1. **默认后端是 sqlite** —— 否则用户装完还得去装 MySQL，整个零配置就是假的；
 *   2. **数据目录必须落在壳指定的目录**（userData）—— 否则"卸载重装不丢数据"
 *      与"程序可以装在只读位置"这两条承诺同时失效，而且**不报错**；
 *   3. **用户显式配过的东西一个都不许被覆盖** —— 想用 MySQL 的用户被静默拉回
 *      SQLite 是最坏的一类故障：界面一模一样，只是数据进了另一个文件。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const cfg = require('../src/setup-config');
const backend = require('../src/backend-config');

// ------------------------------------------------------------------
//  1. 默认后端
// ------------------------------------------------------------------
test('★ 默认存储方式是 sqlite（"安装即用"的前提）', () => {
  assert.equal(cfg.DEFAULT_DB_BACKEND, 'sqlite');
  assert.equal(cfg.defaultValues().HNE_DB_BACKEND, 'sqlite');
});

test('★ 默认表单里，MySQL 字段不该被当成必填拦住用户', () => {
  const result = cfg.validateValues(cfg.defaultValues());
  assert.equal(result.ok, true, JSON.stringify(result.errors));
});

// ------------------------------------------------------------------
//  2. 数据目录（两处必须一致）
// ------------------------------------------------------------------
test('★★ databaseDefaults 与 databaseEnvDefaults 讲的是同一件事', () => {
  const dataDir = path.join('C:', 'Users', 'someone', 'AppData', 'Roaming', '云梦枢');
  const formSide = cfg.databaseDefaults(dataDir);
  const envSide = backend.databaseEnvDefaults({ dataDir, fileEnv: {}, baseEnv: {} });

  assert.equal(envSide.HNE_DATA_DIR, formSide.HNE_DATA_DIR);
  assert.equal(envSide.HNE_SQLITE_PATH, formSide.HNE_SQLITE_PATH);
  assert.equal(path.isAbsolute(envSide.HNE_SQLITE_PATH), true);
});

test('★ 注入的 SQLite 文件必须在数据目录之下（而不是相对当前工作目录）', () => {
  const dataDir = path.join('D:', 'appdata', 'yunmeng');
  const env = backend.databaseEnvDefaults({ dataDir, fileEnv: {}, baseEnv: {} });
  const relative = path.relative(dataDir, env.HNE_SQLITE_PATH);
  assert.ok(!relative.startsWith('..'), `库文件跑到数据目录之外了：${env.HNE_SQLITE_PATH}`);
  assert.equal(env.HNE_DB_BACKEND, 'sqlite');
});

// ------------------------------------------------------------------
//  3. 绝不覆盖用户的显式选择
// ------------------------------------------------------------------
test('★★ 用户选了 MySQL：注入里绝不能出现 sqlite', () => {
  const env = backend.databaseEnvDefaults({
    dataDir: 'C:/data',
    fileEnv: { HNE_DB_BACKEND: 'mysql', HNE_MYSQL_HOST: '127.0.0.1' },
    baseEnv: {},
  });
  assert.equal(env.HNE_DB_BACKEND, undefined, '绝不能把用户选的 mysql 改回 sqlite');
  // 但数据目录仍然要给他（它只决定相对路径的基准，与后端选择无关）
  assert.equal(env.HNE_DATA_DIR, 'C:/data');
  // ★ SQLite 的库文件路径也不该注入：那个用户根本不用 SQLite，
  //   注进去只会在日志里造出一个"看起来在用 SQLite"的假象。
  assert.equal(env.HNE_SQLITE_PATH, undefined);
});

test('★★ 老配置（只有 HNE_MYSQL_*、没有 DB_BACKEND）绝不能被改成 sqlite', () => {
  const env = backend.databaseEnvDefaults({
    dataDir: 'C:/data',
    fileEnv: { HNE_MYSQL_HOST: '10.0.0.5', HNE_MYSQL_PASSWORD: 'secret' },
    baseEnv: {},
  });
  assert.equal(env.HNE_DB_BACKEND, undefined,
    '老版本向导写的配置必须继续按 MySQL 理解，否则老用户升级后数据会进另一个文件');
});

test('★ 向导写出的 sqlite 配置：仍要注入数据目录（否则数据落到安装目录附近）', () => {
  // 这是最容易漏的一条：向导**总是**会写 HNE_DB_BACKEND，
  // 如果"看到 DB_BACKEND 就整体不注入"，默认用户就拿不到 HNE_DATA_DIR。
  const env = backend.databaseEnvDefaults({
    dataDir: 'C:/data',
    fileEnv: { HNE_DB_BACKEND: 'sqlite' },
    baseEnv: {},
  });
  assert.equal(env.HNE_DATA_DIR, 'C:/data', '默认用户也必须拿到数据目录');
  assert.equal(typeof env.HNE_SQLITE_PATH, 'string');
});

test('★ 用户自己设过 HNE_DATA_DIR 时不覆盖它（逐键判断）', () => {
  const env = backend.databaseEnvDefaults({
    dataDir: 'C:/userData',
    fileEnv: { HNE_DATA_DIR: 'E:/my-own-data' },
    baseEnv: {},
  });
  assert.equal(env.HNE_DATA_DIR, undefined);
  // 数据目录由用户定，但库文件路径我们仍可以给（在他自己的目录下）
  assert.equal(env.HNE_SQLITE_PATH, path.join('C:/userData', 'data', 'app.sqlite3'));
});

test('★ 真实进程环境里的显式设置同样优先（临时用环境变量试一个值不会被悄悄忽略）', () => {
  const env = backend.databaseEnvDefaults({
    dataDir: 'C:/userData',
    fileEnv: {},
    baseEnv: { HNE_DB_BACKEND: 'mysql', HNE_MYSQL_HOST: 'h' },
  });
  assert.equal(env.HNE_DB_BACKEND, undefined);
});

test('★ 空字符串不算"用户设过"（向导会把没填的项写成空值）', () => {
  const env = backend.databaseEnvDefaults({
    dataDir: 'C:/userData',
    fileEnv: { HNE_DB_BACKEND: '', HNE_MYSQL_HOST: '' },
    baseEnv: {},
  });
  assert.equal(env.HNE_DB_BACKEND, 'sqlite', '空值应被当作没设，走默认 sqlite');
});

// ------------------------------------------------------------------
//  4b. ★★ 数据根 vs 数据目录（本轮打包验收抓到的"多一层 data"）
// ------------------------------------------------------------------
test('★★ paths.dataRoot 必须是 dataDir 的父级（否则会建出 data\\data）', () => {
  // 这一条是打包产物实测抓到的：零配置跑完，库文件落在
  //   <userData>\data\data\app.sqlite3
  // 因为壳把 `HNE_DATA_DIR`（数据**根**）传成了 `<userData>\data`，
  // 而后端会在数据根下面再拼一层 `data/app.sqlite3`。
  // 于是这里把"两者差一层"变成可执行的断言 —— 名字太像了，光靠注释守不住。
  const path = require('node:path');
  const { resolvePaths } = require('../src/paths');

  for (const packaged of [false, true]) {
    const p = resolvePaths({
      baseDir: path.resolve(__dirname, '..', 'src'),
      appRoot: path.join('E:', 'repo'),
      userDataDir: path.join('C:', 'userData'),
      packaged,
    });
    assert.equal(path.dirname(p.dataDir), p.dataRoot,
      `packaged=${packaged} 时 dataRoot 应当是 dataDir 的父级`);
    // 打包态：数据根是 userData（安装目录之外）；开发态：数据根是仓库根
    assert.equal(p.dataRoot, packaged ? p.userDataDir : p.appRoot);
  }
});

test('★★ 把 dataRoot 喂给注入逻辑，SQLite 文件正好落在 dataDir 下', () => {
  const path = require('node:path');
  const { resolvePaths } = require('../src/paths');

  for (const packaged of [false, true]) {
    const p = resolvePaths({
      baseDir: path.resolve(__dirname, '..', 'src'),
      appRoot: path.join('E:', 'repo'),
      userDataDir: path.join('C:', 'userData'),
      packaged,
    });
    const env = backend.databaseEnvDefaults({ dataDir: p.dataRoot, fileEnv: {}, baseEnv: {} });
    // ★ 关键不变式：注入出来的库文件路径必须**恰好**是 <dataDir>/app.sqlite3，
    //   而不是 <dataDir>/data/app.sqlite3（那就是本轮那个多一层的 bug）。
    assert.equal(env.HNE_SQLITE_PATH, path.join(p.dataDir, 'app.sqlite3'),
      `packaged=${packaged} 时库文件位置不对`);
    assert.equal(env.HNE_DATA_DIR, p.dataRoot);
  }
});

// ------------------------------------------------------------------
//  5. 切换存储方式：★ 密钥必须被保住
// ------------------------------------------------------------------
test('★★ preserveSecrets：密钥留空 → 沿用已存值（否则已保存的 API Key 会解不开）', () => {
  // 这条守着一个**会毁数据**的场景：
  //   切换存储方式时页面**不回传密钥**（安全考虑，见 preload.js），
  //   如果就这么把空值写下去，后端下次启动会**重新生成加密密钥** ——
  //   而用户已经存进数据库的模型 API Key 是拿旧密钥加密的，从此永远解不开。
  const saved = {
    HNE_SECRET_KEY: 'saved-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz',
    HNE_API_KEY_ENCRYPTION_KEY: 'KioqKioqKioqKioqKioqKioqKioqKioqKioqKioqKio=',
    HNE_MYSQL_HOST: '127.0.0.1',
  };
  const submitted = {
    HNE_DB_BACKEND: 'sqlite',
    HNE_SECRET_KEY: '',            // 页面拿不到，所以是空的
    HNE_API_KEY_ENCRYPTION_KEY: '', // 同上
    HNE_MYSQL_HOST: '',            // 普通字段：空就是用户想清掉它
  };

  const { values, preserved } = cfg.preserveSecrets(submitted, saved);
  assert.equal(values.HNE_SECRET_KEY, saved.HNE_SECRET_KEY, '密钥必须沿用已存的');
  assert.equal(values.HNE_API_KEY_ENCRYPTION_KEY, saved.HNE_API_KEY_ENCRYPTION_KEY);
  assert.deepEqual(preserved.sort(), ['HNE_API_KEY_ENCRYPTION_KEY', 'HNE_SECRET_KEY']);
  // ★ 普通字段**不**被"保住"：用户清空就是清空（否则他永远删不掉一个错的主机名）
  assert.equal(values.HNE_MYSQL_HOST, '');
  assert.equal(values.HNE_DB_BACKEND, 'sqlite');
});

test('★ preserveSecrets：用户重新填了密钥 → 以他填的为准（不能反过来覆盖用户）', () => {
  const saved = { HNE_SECRET_KEY: 'old', HNE_API_KEY_ENCRYPTION_KEY: 'old-fernet' };
  const mine = 'my-own-secret-key-0123456789-abcdefghijklmnop';
  const { values, preserved } = cfg.preserveSecrets(
    { HNE_SECRET_KEY: mine, HNE_API_KEY_ENCRYPTION_KEY: '' }, saved,
  );
  assert.equal(values.HNE_SECRET_KEY, mine);
  assert.equal(values.HNE_API_KEY_ENCRYPTION_KEY, 'old-fernet');
  assert.deepEqual(preserved, ['HNE_API_KEY_ENCRYPTION_KEY']);
});

test('★ preserveSecrets：两边都空 → 还是空（交给后端自动生成，不要凭空造一个）', () => {
  const { values, preserved } = cfg.preserveSecrets({ HNE_SECRET_KEY: '', HNE_API_KEY_ENCRYPTION_KEY: '' }, {});
  assert.equal(values.HNE_SECRET_KEY, '');
  assert.equal(values.HNE_API_KEY_ENCRYPTION_KEY, '');
  assert.deepEqual(preserved, []);
});

// ------------------------------------------------------------------
//  5b. ★★ 切到「本机文件」时，MySQL 连接信息必须留存下来（实测事故的墓碑）
// ------------------------------------------------------------------
test('★★★ 切到 SQLite：MySQL 连接信息写成注释留存（否则切回来口令就丢了）', () => {
  // ==================== 这条守着一个**真实且严重**的事故 ====================
  // 用户原来是 MySQL 用户，为了试试 SQLite 切了过去。而 `renderEnvFile` 原本
  // 把 `HNE_MYSQL_*` **整组删掉** —— 包括那串**自动生成的 32 位随机口令**。
  // 等他反悔想切回 MySQL 时：口令格是空的，而他不可能记得那串随机串，
  // 于是只能自己编一个 → 连不上 → 自检等到超时（180 秒）→ 他只看到"自检超时"。
  // 那一晚他因此以为"我的账号是不是被人改了"。
  const mysqlConfig = [
    'HNE_DB_BACKEND=mysql',
    'HNE_MYSQL_HOST=127.0.0.1',
    'HNE_MYSQL_PORT=3306',
    'HNE_MYSQL_USER=narrative_app',
    'HNE_MYSQL_PASSWORD=TheRandom32CharPasswordAbcdefgh',
    'HNE_MYSQL_DB=narrative_engine',
    'HNE_SECRET_KEY=secret-key-0123456789-abcdefghijklmnopqrstuvwxyz',
    'HNE_API_KEY_ENCRYPTION_KEY=KioqKioqKioqKioqKioqKioqKioqKioqKioqKioqKio=',
  ].join('\n');

  // 用户点「切换存储方式」→ 选「本机文件（SQLite）」→ 页面不回传口令，所以口令格是空的
  // ★ 真实链路是：主进程先用 `preserveSecrets` 把空密钥补回已存值，再渲染。
  //   第一版测试漏了这一步，于是断言"密钥必须沿用"失败 —— 那是**测试写错了**，
  //   不是实现错了（实现本来就在 main.js 的 setup:switch 里做这件事）。
  const { values: merged } = cfg.preserveSecrets(
    {
      HNE_DB_BACKEND: 'sqlite',
      HNE_MYSQL_HOST: '',
      HNE_MYSQL_PASSWORD: '',
      HNE_SECRET_KEY: '',
      HNE_API_KEY_ENCRYPTION_KEY: '',
    },
    cfg.parseEnvText(mysqlConfig),
  );
  const written = cfg.renderEnvFile(merged, mysqlConfig, cfg.parseEnvText(mysqlConfig));

  // ① 生效的配置里**不能**有 MySQL 键（选了 sqlite 就不该去连 MySQL）
  const effective = cfg.parseEnvText(written);
  assert.equal(effective.HNE_DB_BACKEND, 'sqlite');
  assert.equal(effective.HNE_MYSQL_HOST, undefined, 'MySQL 键不能还生效 —— 否则选 sqlite 却去连 MySQL');
  assert.equal(effective.HNE_MYSQL_PASSWORD, undefined);
  // 密钥要被保住
  assert.match(effective.HNE_SECRET_KEY, /^secret-key-/, '密钥必须沿用');

  // ② 但值必须**留在文件里**（注释形式），否则用户永远切不回去
  assert.match(written, /#\s*HNE_MYSQL_HOST=127\.0\.0\.1/, '要把 MySQL 主机写成注释留存');
  assert.match(written, /#\s*HNE_MYSQL_PASSWORD=TheRandom32CharPasswordAbcdefgh/,
    '★ 口令也必须留存 —— 它是随机串，用户不可能记得');
  assert.match(written, /#\s*HNE_MYSQL_DB=narrative_engine/);

  // ③ `stashOf` 要能把它们读回来（切回 MySQL 时靠它预填）
  const stash = cfg.stashOf(written);
  assert.equal(stash.HNE_MYSQL_HOST, '127.0.0.1');
  assert.equal(stash.HNE_MYSQL_USER, 'narrative_app');
  assert.equal(stash.HNE_MYSQL_PASSWORD, 'TheRandom32CharPasswordAbcdefgh');
  assert.equal(stash.HNE_MYSQL_DB, 'narrative_engine');
  // 不能把非 dbOnly 的键也当成 stash（那会让预填逻辑把范围放宽）
  assert.equal(stash.HNE_SECRET_KEY, undefined);
});

test('★★ 切回 MySQL：口令从注释里恢复，且**不再**留注释', () => {
  const sqliteWithStash = [
    'HNE_DB_BACKEND=sqlite',
    'HNE_SECRET_KEY=secret-key-0123456789-abcdefghijklmnopqrstuvwxyz',
    'HNE_API_KEY_ENCRYPTION_KEY=KioqKioqKioqKioqKioqKioqKioqKioqKioqKioqKio=',
    '# ---------------------- 你之前用 MySQL 时的连接信息（现在没在用）----------------------',
    '# HNE_MYSQL_HOST=127.0.0.1',
    '# HNE_MYSQL_PORT=3306',
    '# HNE_MYSQL_USER=narrative_app',
    '# HNE_MYSQL_PASSWORD=TheRandom32CharPasswordAbcdefgh',
    '# HNE_MYSQL_DB=narrative_engine',
  ].join('\n');

  const existing = cfg.parseEnvText(sqliteWithStash);
  const stash = cfg.stashOf(sqliteWithStash);
  // 主进程在切换时会这样合并：注释里那份在前，生效键在后覆盖
  const forSecrets = { ...stash, ...existing };
  const submitted = {
    HNE_DB_BACKEND: 'mysql',
    HNE_MYSQL_HOST: stash.HNE_MYSQL_HOST,
    HNE_MYSQL_PORT: stash.HNE_MYSQL_PORT,
    HNE_MYSQL_USER: stash.HNE_MYSQL_USER,
    HNE_MYSQL_DB: stash.HNE_MYSQL_DB,
    // ★ 页面预填了口令，但用户没动它时值就是这一份；即便为空也要能沿用
    HNE_MYSQL_PASSWORD: '',
    HNE_SECRET_KEY: '',
    HNE_API_KEY_ENCRYPTION_KEY: '',
  };
  const { values: merged } = cfg.preserveSecrets(submitted, forSecrets);
  assert.equal(merged.HNE_MYSQL_PASSWORD, 'TheRandom32CharPasswordAbcdefgh',
    '★ 切回 MySQL 时口令必须自动沿用，用户不该被迫回忆随机串');

  const written = cfg.renderEnvFile(merged, sqliteWithStash, forSecrets);
  const effective = cfg.parseEnvText(written);
  assert.equal(effective.HNE_DB_BACKEND, 'mysql');
  assert.equal(effective.HNE_MYSQL_PASSWORD, 'TheRandom32CharPasswordAbcdefgh', '切回后口令要生效');
  assert.equal(effective.HNE_MYSQL_USER, 'narrative_app');
  assert.equal(cfg.resolvedBackend(effective), 'mysql');
  assert.equal(cfg.missingRequired(effective).length, 0, '切回后不该还缺必填项');
  // 切回 MySQL 之后就没必要再留那份注释了（它已经变成生效配置）
  assert.doesNotMatch(written, /# HNE_MYSQL_PASSWORD=/, '生效之后不该再留一份注释副本');
});

// ------------------------------------------------------------------
//  5. 源码验收的开关（本轮真踩的坑：验收测的不是当前代码）
// ------------------------------------------------------------------
test('★★ preferPythonFromSource：必须显式开启，默认关（避免静默降低验收形态）', () => {
  const sidecar = require('../src/sidecar');

  // 默认（没设）：false —— 打包形态照旧优先用 backend.exe
  assert.equal(sidecar.preferPythonFromSource({}), false);
  assert.equal(sidecar.preferPythonFromSource(undefined), false);

  // 显式开启的几种写法
  for (const raw of ['1', 'true', 'TRUE', 'yes', 'on', ' on ']) {
    assert.equal(sidecar.preferPythonFromSource({ HNE_DESKTOP_PREFER_PYTHON: raw }), true,
      `${raw} 应该被认成开启`);
  }
  // 明确关闭 / 乱填都算关（"乱填"不该被当成开启 —— 宁可维持打包形态）
  for (const raw of ['0', 'false', '', 'no', 'off', 'maybe']) {
    assert.equal(sidecar.preferPythonFromSource({ HNE_DESKTOP_PREFER_PYTHON: raw }), false,
      `${raw} 应该被认成关闭`);
  }
});

test('★ 接线：main.js 必须真的用这个开关把打包后端跳过', () => {
  // 这个坑的形状是"开关写了但没接线" —— 本项目已经踩过一次同类问题
  // （「随机生成」按钮：契约写了、按钮从没被创建）。
  // 所以这里断言的是**接线**，而不只是那个纯函数存在。
  const fs = require('node:fs');
  const path = require('node:path');
  const main = fs.readFileSync(path.resolve(__dirname, '..', 'src', 'main.js'), 'utf8');
  assert.match(main, /preferPythonFromSource/, 'main.js 要引用开关');
  assert.match(main, /const forceSourcePython = preferPythonFromSource\(process\.env\)/,
    '要把它算成一个布尔量');
  assert.match(main, /forceSourcePython\s*\?[\s\S]{0,200}?chooseBackend/,
    '为真时必须**跳过** chooseBackend（否则 backend.exe 照样会赢）');
});

test('★★ 接线：两条后端启动路径都必须注入数据库默认值', () => {
  // ★ 这条守着一个真实事故（打包验收抓到）：
  //   `startBackend()` 注入了数据库默认值，而 `selfCheckWithEnv()` **没有** ——
  //   于是自检退回读仓库根的 .env，把 SQLite 库建到了**仓库里**，
  //   正式启动又按注入值建到 userData：一次启动在两个地方各建一个库。
  //   两边都不报错，只有"数据该在哪"被当契约来验的时候才会发现。
  const fs = require('node:fs');
  const path = require('node:path');
  const main = fs.readFileSync(path.resolve(__dirname, '..', 'src', 'main.js'), 'utf8');
  const calls = main.match(/databaseEnvDefaults\(\{/g) || [];
  assert.ok(calls.length >= 2,
    `databaseEnvDefaults 至少要被调用两次（正式启动 + 保存前自检），实际 ${calls.length} 次`);
  // 而且必须传**数据根**（dataRoot），不能传 dataDir / userDataDataDir
  assert.equal(/dataDir:\s*paths\.dataRoot/.test(main), true,
    '必须传 paths.dataRoot（数据根）—— 传 dataDir/userDataDataDir 会建出 data\\data');
  // ★ 判据要限定在 `databaseEnvDefaults({...})` 的**调用**里：
  //   `paths.userDataDataDir` 在别处是**合法**的（比如 sidecar 自检的
  //   `--data-dir` 参数，那个参数的语义本来就是 `<userData>/data`）。
  //   第一版直接全文匹配 `dataDir: paths.userDataDataDir`，于是把
  //   `'--data-dir', paths.userDataDataDir` 那条**正确**的用法也判成违规。
  //   ★ 教训：静态断言要盯"哪个函数的参数"，不要全文扫一个字符串。
  const callBlocks = main.split('databaseEnvDefaults({').slice(1).map((s) => s.slice(0, 200));
  assert.ok(callBlocks.length >= 2, '应当至少有两处 databaseEnvDefaults 调用');
  for (const block of callBlocks) {
    assert.equal(/dataDir:\s*paths\.userDataDataDir/.test(block), false,
      '不许把 userDataDataDir 当数据根传进 databaseEnvDefaults（那就是"多一层 data"的成因）');
    assert.equal(/dataDir:\s*paths\.dataRoot/.test(block), true,
      `每处 databaseEnvDefaults 都必须传 paths.dataRoot，实际：${block.split('\n')[0]}`);
  }
});

// ------------------------------------------------------------------
//  6. 端到端：向导的产物 → 后端读到的就是 sqlite + 数据目录
// ------------------------------------------------------------------
test('★★ 端到端：默认向导产物喂给注入逻辑，两条路径指向同一个库文件', () => {
  const dataDir = path.join('C:', 'Users', 'someone', 'AppData', 'Roaming', '云梦枢');
  const text = cfg.renderEnvFile(cfg.defaultValues());
  const fileEnv = cfg.parseEnvText(text);

  const env = backend.databaseEnvDefaults({ dataDir, fileEnv, baseEnv: {} });

  assert.equal(fileEnv.HNE_DB_BACKEND, 'sqlite');
  assert.equal(env.HNE_DATA_DIR, dataDir);
  assert.equal(env.HNE_SQLITE_PATH, path.join(dataDir, 'data', 'app.sqlite3'));
  // 用户全程没填任何数据库信息，配置却已经是"可用"的
  assert.equal(cfg.configIsUsable(fileEnv), true);
});
