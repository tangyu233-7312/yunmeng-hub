'use strict';

/**
 * `desktop/src/backend-config.js` 的自测。
 *
 * ★ 这一层的 bug 表现最"远"：读错一个 .env 值 → MySQL 连不上 →
 *   用户看到的是"后端没起来"，而真正的原因是解析。所以解析规则要逐条钉住。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  parseEnvText, readEnvFile, detectPython, isTruthy, buildBackendEnv, choosePort,
  preferredPort, truncate,
} = require('../src/backend-config');

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hne-desktop-cfg-'));
}

// ------------------------------------------------------------------
//  parseEnvText
// ------------------------------------------------------------------
test('parseEnvText：基本键值、跳过注释与空行', () => {
  const parsed = parseEnvText([
    '# 这是注释',
    '',
    'HNE_APP_NAME=HeteroNarrativeEngine',
    'HNE_DEBUG=true',
    '  HNE_MYSQL_PORT  =  3306  ',
  ].join('\n'));

  assert.deepEqual(parsed, {
    HNE_APP_NAME: 'HeteroNarrativeEngine',
    HNE_DEBUG: 'true',
    HNE_MYSQL_PORT: '3306',
  });
});

test('parseEnvText：成对引号被去掉，值里的等号保留', () => {
  const parsed = parseEnvText([
    'HNE_MYSQL_PASSWORD="p@ss=word with spaces"',
    "HNE_SECRET_KEY='single-quoted'",
    'HNE_CORS_ORIGINS=["http://localhost:5173","file://"]',
  ].join('\n'));

  assert.equal(parsed.HNE_MYSQL_PASSWORD, 'p@ss=word with spaces');
  assert.equal(parsed.HNE_SECRET_KEY, 'single-quoted');
  // ★ 值里的等号/引号不能被误吃：JSON 数组要原样交给 pydantic-settings
  assert.equal(parsed.HNE_CORS_ORIGINS, '["http://localhost:5173","file://"]');
});

test('parseEnvText：不成对的引号不当成引号（避免把值吃掉一半）', () => {
  const parsed = parseEnvText('HNE_X="only-open\nHNE_Y=only-close"');
  assert.equal(parsed.HNE_X, '"only-open');
  assert.equal(parsed.HNE_Y, 'only-close"');
});

test('parseEnvText：值里可以带 # 与中文', () => {
  const parsed = parseEnvText('HNE_NOTE=优先级 #1 中文值\nHNE_URL=http://127.0.0.1:8000/#frag');
  assert.equal(parsed.HNE_NOTE, '优先级 #1 中文值');
  assert.equal(parsed.HNE_URL, 'http://127.0.0.1:8000/#frag');
});

test('parseEnvText：非法键名被跳过，不会污染结果', () => {
  const parsed = parseEnvText('1BAD=x\nGOOD_OK=1\n=novalue\n没有等号\n');
  assert.deepEqual(parsed, { GOOD_OK: '1' });
});

// ------------------------------------------------------------------
//  readEnvFile
// ------------------------------------------------------------------
test('readEnvFile：文件不存在返回空对象（首次运行还没配 .env 是正常状态）', () => {
  const dir = tempDir();
  assert.deepEqual(readEnvFile(path.join(dir, 'nope.env')), {});
});

test('readEnvFile：真读一个文件（含中文与 BOM 也不会崩）', () => {
  const dir = tempDir();
  const file = path.join(dir, '.env');
  fs.writeFileSync(file, '\uFEFF# 注释\nHNE_MYSQL_DB=narrative_engine\n', 'utf8');

  const parsed = readEnvFile(file);
  // ★ BOM 的处理结论：带 BOM 时第一行是注释，不影响后续键值读取
  assert.equal(parsed.HNE_MYSQL_DB, 'narrative_engine');
});

// ------------------------------------------------------------------
//  buildBackendEnv
// ------------------------------------------------------------------
test('buildBackendEnv：进程环境优先级高于 .env，HNE_HOST/PORT 覆盖全部', () => {
  const env = buildBackendEnv({
    baseEnv: { PATH: '/usr/bin', HNE_LOG_LEVEL: 'DEBUG' },
    fileEnv: { HNE_LOG_LEVEL: 'INFO', HNE_MYSQL_PASSWORD: 'from-file' },
    overrides: { HNE_HOST: '127.0.0.1', HNE_PORT: '51234' },
  });

  assert.equal(env.PATH, '/usr/bin');
  // ★ 进程环境里已有的值赢过 .env（与 pydantic-settings 的优先级一致）：
  //   否则"临时用环境变量试一个值"会静默失效
  assert.equal(env.HNE_LOG_LEVEL, 'DEBUG');
  assert.equal(env.HNE_MYSQL_PASSWORD, 'from-file');
  assert.equal(env.HNE_PORT, '51234');
});

test('buildBackendEnv：显式覆盖（HNE_HOST/HNE_PORT）赢过 .env 与进程环境', () => {
  const env = buildBackendEnv({
    baseEnv: { HNE_PORT: '1111' },
    fileEnv: { HNE_PORT: '2222' },
    overrides: { HNE_PORT: '3333' },
  });
  assert.equal(env.HNE_PORT, '3333');
});

test('buildBackendEnv：强制 UTF-8（否则中文日志在 Windows 上会 UnicodeEncodeError）', () => {
  const env = buildBackendEnv({ baseEnv: {}, fileEnv: {}, overrides: {} });
  assert.equal(env.PYTHONUTF8, '1');
  assert.equal(env.PYTHONIOENCODING, 'utf-8');
});

test('buildBackendEnv：不会凭空造出 HNE_ 前缀之外的键', () => {
  const env = buildBackendEnv({ baseEnv: {}, fileEnv: { MYSQL_PASSWORD: 'oops' }, overrides: {} });
  assert.equal(env.MYSQL_PASSWORD, 'oops'); // 照实传，但后端只认 HNE_*
  assert.ok(!('HNE_MYSQL_PASSWORD' in env));
});

// ------------------------------------------------------------------
//  choosePort
// ------------------------------------------------------------------
test('choosePort：优先端口空闲就用它，并说明来源', () => {
  assert.deepEqual(
    choosePort({ preferredPort: 8000, freePort: 51234, preferredIsFree: true }),
    { port: 8000, source: 'preferred' },
  );
});

test('choosePort：优先端口被占 → 换系统分配的空闲端口，来源标成 preferred-busy', () => {
  assert.deepEqual(
    choosePort({ preferredPort: 8000, freePort: 51234, preferredIsFree: false }),
    { port: 51234, source: 'preferred-busy' },
  );
});

test('choosePort：没有优先端口 → 用系统分配的', () => {
  assert.deepEqual(
    choosePort({ preferredPort: null, freePort: 51234, preferredIsFree: false }),
    { port: 51234, source: 'ephemeral' },
  );
});

test('choosePort：优先端口非法（0 / 越界）不采用', () => {
  for (const bad of [0, -1, 70000, 1.5, NaN]) {
    const result = choosePort({ preferredPort: bad, freePort: 51234, preferredIsFree: true });
    assert.equal(result.port, 51234, `非法端口 ${bad} 不该被采用`);
  }
});

// ------------------------------------------------------------------
//  preferredPort —— ★ 这一组是因为一个真 bug 补的
//  背景：我把默认端口策略从"写死 8000"改成"默认让系统分配"（返回 null），
//  但 `startBackend()` 里有**两处**用到这个值，我只给其中一处加了 null 守卫，
//  另一处把 null 传给了 net.connect → 默认路径直接启动失败
//  （用户截图：`The "options.port" property must be one of type number or string`）。
//  这里把"什么时候是 null"钉死，免得再出现"只守一半"的情况。
// ------------------------------------------------------------------
test('preferredPort：默认不指定 → 返回 null（不是 8000）', () => {
  assert.equal(preferredPort({}, {}), null);
  assert.equal(preferredPort(undefined, {}), null);
});

test('preferredPort：HNE_DESKTOP_PORT 优先于 .env 里的 HNE_PORT', () => {
  assert.equal(preferredPort({ HNE_PORT: '8000' }, { HNE_DESKTOP_PORT: '9001' }), 9001);
  assert.equal(preferredPort({ HNE_PORT: '8000' }, {}), 8000);
});

test('preferredPort：空串 / 空白 / 非法值一律当"没指定"（而不是 0 或 NaN）', () => {
  for (const raw of ['', '   ', 'abc', '0', '-1', '70000', '3.5']) {
    assert.equal(preferredPort({ HNE_PORT: raw }, {}), null, `HNE_PORT=${raw} 应该当作没指定`);
    assert.equal(preferredPort({}, { HNE_DESKTOP_PORT: raw }), null, `HNE_DESKTOP_PORT=${raw} 应该当作没指定`);
  }
});

test('★ 回归：preferredPort 返回 null 时，choosePort 必须走"系统分配"而不是把 null 当端口', () => {
  // 这条对应真实事故：调用方拿 null 去 net.connect，抛
  // "The options.port property must be one of type number or string"。
  const preferred = preferredPort({}, {});          // null
  assert.equal(preferred, null);

  const chosen = choosePort({ preferredPort: preferred, freePort: 51234, preferredIsFree: false });
  assert.equal(chosen.port, 51234);
  assert.equal(chosen.source, 'ephemeral');
  assert.equal(typeof chosen.port, 'number', 'port 必须是数字，绝不能是 null');
});

// ------------------------------------------------------------------
//  detectPython
// ------------------------------------------------------------------
test('detectPython：候选列表包含项目虚拟环境、PATH 与显式指定的解释器', () => {
  const appRoot = tempDir();
  const seen = [];
  const fakeSpawn = (command, _args, _options) => {
    seen.push(command);
    return { status: 1, stderr: 'ModuleNotFoundError: No module named \'uvicorn\'', error: undefined };
  };

  const result = detectPython({
    appRoot,
    env: { HNE_DESKTOP_PYTHON: 'C:\\custom\\python.exe' },
    spawnSyncImpl: fakeSpawn,
  });

  assert.equal(result.python, null);
  assert.ok(seen.includes('C:\\custom\\python.exe'), '显式指定的解释器必须被优先尝试');
  assert.ok(seen.some((p) => p.includes('.venv')), '必须尝试项目自带虚拟环境');
  assert.ok(seen.includes('python'), '必须兜底尝试 PATH 上的 python');
  assert.equal(seen[0], 'C:\\custom\\python.exe', '显式指定的应该排在最前');
  assert.equal(result.candidates.length, seen.length);
  assert.match(result.candidates[0].reason, /uvicorn/);
});

test('detectPython：第一个可用的解释器胜出，后面的不再探测', () => {
  const appRoot = tempDir();
  const venvPython = path.join(appRoot, '.venv', 'Scripts', 'python.exe');
  const tried = [];
  const fakeSpawn = (command) => {
    tried.push(command);
    return { status: command === venvPython ? 0 : 1, stderr: 'nope', error: undefined };
  };

  const result = detectPython({ appRoot, env: {}, spawnSyncImpl: fakeSpawn });

  assert.equal(result.python, venvPython);
  assert.deepEqual(tried, [venvPython], '找到之后就不该再试别的');
  assert.equal(result.candidates.filter((c) => c.ok).length, 1);
});

test('detectPython：解释器无法执行（spawn error）时如实记录原因', () => {
  const appRoot = tempDir();
  const fakeSpawn = () => ({ error: { code: 'ENOENT', message: 'not found' }, status: null });

  const result = detectPython({ appRoot, env: {}, spawnSyncImpl: fakeSpawn });
  assert.equal(result.python, null);
  assert.match(result.candidates[0].reason, /无法执行/);
});

// ------------------------------------------------------------------
//  detectPython：显式指定 + 严格模式
//  ★ 为什么值得单独测：默认行为是"指定的没成功就悄悄换别的解释器"。
//    这在多数情况下更省事，但**用户明明指定了它**却用了另一个，界面上完全看不出来 ——
//    所以提供了严格模式，而"严格模式到底严不严"必须由测试钉住。
// ------------------------------------------------------------------
test('detectPython：默认（非严格）下，显式指定的不可用会继续回退给别的解释器', () => {
  const appRoot = tempDir();
  const venvPython = path.join(appRoot, '.venv', 'Scripts', 'python.exe');
  const tried = [];
  const fakeSpawn = (command) => {
    tried.push(command);
    return { status: command === venvPython ? 0 : 1, stderr: 'bad', error: undefined };
  };

  const result = detectPython({
    appRoot,
    env: { HNE_DESKTOP_PYTHON: 'C:\\bogus\\python.exe' },
    spawnSyncImpl: fakeSpawn,
  });

  assert.equal(result.python, venvPython, '非严格模式应该回退成功');
  assert.equal(result.strictExplicit, false);
  assert.deepEqual(tried, ['C:\\bogus\\python.exe', venvPython]);
});

test('detectPython：严格模式下，显式指定的不可用就立刻失败（不静默换解释器）', () => {
  const appRoot = tempDir();
  const venvPython = path.join(appRoot, '.venv', 'Scripts', 'python.exe');
  const tried = [];
  const fakeSpawn = (command) => {
    tried.push(command);
    return { status: command === venvPython ? 0 : 1, stderr: 'bad', reason: 'bad', error: undefined };
  };

  const result = detectPython({
    appRoot,
    env: { HNE_DESKTOP_PYTHON: 'C:\\bogus\\python.exe', HNE_DESKTOP_PYTHON_STRICT: '1' },
    spawnSyncImpl: fakeSpawn,
  });

  assert.equal(result.python, null, '严格模式下不该回退到 .venv');
  assert.equal(result.strictExplicit, true);
  assert.deepEqual(tried, ['C:\\bogus\\python.exe'], '严格模式下不该再试别的解释器');
  assert.equal(result.candidates.length, 1);
});

test('detectPython：严格模式 + 指定的解释器可用 → 正常用它的', () => {
  const appRoot = tempDir();
  const fakeSpawn = () => ({ status: 0, stderr: '', error: undefined });

  const result = detectPython({
    appRoot,
    env: { HNE_DESKTOP_PYTHON: 'C:\\good\\python.exe', HNE_DESKTOP_PYTHON_STRICT: 'true' },
    spawnSyncImpl: fakeSpawn,
  });

  assert.equal(result.python, 'C:\\good\\python.exe');
  assert.equal(result.strictExplicit, true);
});

test('detectPython：没设 HNE_DESKTOP_PYTHON 时，严格开关不会误触发', () => {
  const appRoot = tempDir();
  const fakeSpawn = () => ({ status: 1, stderr: 'bad', error: undefined });

  const result = detectPython({
    appRoot,
    env: { HNE_DESKTOP_PYTHON_STRICT: '1' },
    spawnSyncImpl: fakeSpawn,
  });

  assert.equal(result.strictExplicit, false, '没有指定解释器时，严格模式无意义，不该生效');
  assert.ok(result.candidates.length > 1, '应该照常把所有候选都试一遍');
});

test('detectPython：空的 HNE_DESKTOP_PYTHON 当作没设（避免空字符串被拿去 spawn）', () => {
  const appRoot = tempDir();
  const tried = [];
  const fakeSpawn = (command) => {
    tried.push(command);
    return { status: 1, stderr: 'bad', error: undefined };
  };

  detectPython({ appRoot, env: { HNE_DESKTOP_PYTHON: '   ' }, spawnSyncImpl: fakeSpawn });
  assert.ok(!tried.includes('   '), '空白值不该被当成解释器路径');
  assert.ok(!tried.includes(''), '空值不该被当成解释器路径');
});

test('isTruthy：认得 1/true/yes/on，其余为假', () => {
  for (const yes of ['1', 'true', 'TRUE', 'Yes', 'on', ' on ']) {
    assert.equal(isTruthy(yes), true, `${yes} 应该是真`);
  }
  for (const no of ['0', 'false', '', 'no', 'off', undefined, null, '2']) {
    assert.equal(isTruthy(no), false, `${no} 应该是假`);
  }
});

// ------------------------------------------------------------------
//  truncate
// ------------------------------------------------------------------
test('truncate：超长截断并加省略号，短的原样返回', () => {
  assert.equal(truncate('abc', 10), 'abc');
  assert.equal(truncate('abcdef', 3), 'abc…');
});
