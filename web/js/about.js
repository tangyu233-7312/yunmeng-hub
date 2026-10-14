/* ============================================================
   「关于」弹窗

   ==================== 放什么、不放什么（用户逐条定过的）====================
   放：① 名称 + 版本号 + 环境
       ② 技术栈（浏览器直开 / Electron+Chromium+Node）
       ③ **当前存储方式**（本机文件(SQLite) / MySQL 8.0.28）—— 顺便解决
          "我怎么知道自己用的是哪个库"
       ④ 数据目录 / 日志目录（**完整路径**，可点击/可复制）
       ⑤ GitHub 开源地址
       ⑥ 「复制诊断信息」按钮（不需要用户自己整理，也不暴露任何联系方式）

   不放（用户明确划掉的）：
       · 向量库信息 —— 对普通用户没意义，只会看不懂
       · 已知限制（未签名 / x64 / SQLite 单写者）——
         "能进系统就说明装好了"。那句"未签名"提示改放在**首次设置向导页**，
         因为用户看到 SmartScreen 警告的当下才需要它。

   ==================== 为什么路径要从后端要 ====================
   浏览器**拿不到**真实文件系统路径。而"数据/日志目录在哪"恰恰是排查问题时
   最需要的信息之一（要自己打开目录、要把日志发给别人看）。
   所以由后端 `/system/diagnostics` 如实报出（见 app/api/v1/system.py）。
   ============================================================ */

import { api } from 'hne/api';
import { esc, modal, toastErr, toastOk } from 'hne/ui';
import { buildDiagnosticText, clearErrors, errorCount } from 'hne/diagnostics';

/** 开源仓库地址（用户确认放进界面） */
export const REPO_URL = 'https://github.com/tangyu233-7312/yunmeng-hub';

/**
 * 复制到剪贴板。
 *
 * ★ 为什么不能只用 `navigator.clipboard`：它要求**安全上下文**，
 *   而 `file://` 在部分浏览器/旧 Electron 里不算安全上下文，会直接抛错。
 *   桌面版正是 file:// 加载的，所以必须带回退方案。
 */
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 落到下面的回退方案 */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

function row(label, valueHTML) {
  return `<div class="about-row"><span class="about-k">${esc(label)}</span>`
    + `<span class="about-v">${valueHTML}</span></div>`;
}

/**
 * 描述界面运行形态（浏览器直开 or Electron 壳里）
 *
 * ★ 为什么用 UA 而不是 `window.yunmengSetup`：后者只在**设置页**注入，
 *   控制台里拿不到；而 UA 里 Electron 会把自己的版本号拼进去，够用且不挑页面。
 */
function interfaceLine() {
  const ua = navigator.userAgent || '';
  const electron = /Electron\/([\d.]+)/.exec(ua);
  if (!electron) return '浏览器直接打开（未经过桌面壳）';
  const chrome = /Chrome\/([\d.]+)/.exec(ua);
  const node = /Node\.js\/([\d.]+)/.exec(ua);
  return `Electron ${electron[1]}`
    + (chrome ? ` · Chromium ${chrome[1]}` : '')
    + (node ? ` · Node ${node[1]}` : '');
}

/**
 * 展示「关于」弹窗。
 *
 * ★ 先渲染再取数据：环境快照要请求后端，不该让弹窗卡着不出来。
 *   取不到就如实写"读取失败"，而不是留个空白让人以为没这项。
 */
export function showAbout() {
  const m = modal({
    title: '关于 云梦枢',
    // ★ 宽度类要写 `wide`（styles.css 里的类名是 `.modal.wide`）——
    //   第一版写成 `modal-wide`，那是个不存在的类，弹窗会退回默认窄宽度。
    width: 'wide',
    bodyHTML: `
      <div class="about-brand">
        <img src="/console/img/logo-64.png" alt="" width="40" height="40" />
        <div>
          <div class="about-name">云梦枢（YunMeng Hub）</div>
          <div class="small faint" id="about-sub">异构大模型交互式叙事引擎 · 本地单机部署</div>
        </div>
      </div>
      <div id="about-rows" class="about-rows">
        <div class="small faint">正在读取环境信息…</div>
      </div>
      <!-- 存储的技术细节放这里（登录页只留一行"当前存储"） -->
      <div id="about-storage-notes"></div>
      <div class="small faint mt8" style="line-height:1.7">
        ★ 「复制诊断信息」只会复制<b>版本 / 路径 / 出错的那几条请求</b>，
        <b>不包含</b>你的对话内容、角色卡或任何密钥 —— 可以直接贴给别人看。
      </div>`,
    footHTML: `
      <button class="btn sec" id="about-copy">复制诊断信息</button>
      <button class="btn sec" id="about-clear">清空错误记录</button>
      <button class="btn" data-close>关闭</button>`,
  });

  const rows = m.root.querySelector('#about-rows');

  function render(env, healthError) {
    const version = env?.version ? `v${env.version}` : '（读取失败）';
    const envName = env?.env ? ` · ${env.env}` : '';
    const sub = m.root.querySelector('#about-sub');
    if (sub && env?.version) {
      sub.textContent = `异构大模型交互式叙事引擎 · 本地单机部署 · v${env.version}${envName}`;
    }

    const storage = env
      ? (env.backend === 'mysql' ? `MySQL 数据库（${env.database || ''}）` : '本机文件（SQLite）')
      : (healthError ? '（读取失败）' : '…');

    const paths = env
      ? row('数据目录', `<code class="about-path" data-copy="${esc(env.data_dir)}">${esc(env.data_dir)}</code>`)
        + row('日志目录', `<code class="about-path" data-copy="${esc(env.log_dir)}">${esc(env.log_dir)}</code>`)
      : row('数据目录', '<span class="faint">读取失败（见下）</span>')
        + row('日志目录', '<span class="faint">读取失败（见下）</span>');

    rows.innerHTML = [
      row('版本', `${esc(version)}${env?.python ? ` · 后端 Python ${esc(env.python)}` : ''}`),
      row('技术栈', esc(interfaceLine())),
      row('当前存储', esc(storage)),
      paths,
      row('长期记忆', env?.chroma_dir
        ? `<code class="about-path" data-copy="${esc(env.chroma_dir)}">${esc(env.chroma_dir)}</code>`
        : '<span class="faint">读取失败</span>'),
      row('开源地址', `<a href="${esc(REPO_URL)}" target="_blank" rel="noreferrer">${esc(REPO_URL)}</a>`),
      healthError ? row('提示', `<span class="faint">${esc(healthError)}</span>`) : '',
    ].join('');

    // ★★ 存储的**细节**放在这里（登录页只留一行"当前存储"）。
    //
    //   为什么挪过来：登录页那张卡片原本塞了共享机制 + 向量库路径 + 切换方法，
    //   用户看完的反馈是"门槛是不是有点高，能不能说简单点"。
    //   而这一页有充足空间摆表格、也能带上"两种环境各自怎么改"，是它该待的地方。
    renderStorageNotes(env);
  }

  /**
   * 把"两种存储的关系 + 怎么改"写清楚。
   *
   * ★ 这里必须**按运行环境分支**（浏览器 / 桌面壳）：
   *   桌面壳有「数据」菜单能一键切，浏览器没有 —— 说错任何一边，
   *   用户都会按着找却找不到（本项目犯过，见 docs/pitfalls.md §48）。
   */
  function renderStorageNotes(env) {
    const box = m.root.querySelector('#about-storage-notes');
    if (!box) return;
    if (!env) {
      box.innerHTML = '';
      return;
    }
    const inDesktop = /Electron\/[\d.]+/.test(navigator.userAgent || '');
    const isMysql = env.backend === 'mysql';

    const sharing = isMysql
      ? '它跑在<b>服务端</b>：桌面版与浏览器只要连<b>同一个实例与库名</b>，'
        + '看到的就是<b>同一份数据</b>（账号、会话互通）；连的实例或库名不同就不互通。'
      : '它是<b>本地文件</b>：桌面版与浏览器<b>各有一份</b>，天然不互通。';

    const how = inDesktop
      ? '想换存储方式：顶部菜单「<b>数据 → 切换存储方式…</b>」，改完会自动重启后端。'
      : '想换存储方式：点顶部导航的「<b>存储设置</b>」。'
        + '★ 浏览器无法替你重启后端，保存后要手动重启一次才生效。';

    box.innerHTML = `
      <div class="small faint mt8" style="line-height:1.8">
        <b>关于存储</b><br>
        ${sharing}<br>
        <b>长期记忆（向量库）是本地文件</b>，与数据库<b>分开存放</b> ——
        所以会出现"账号通了、记忆却各记各的"。想共用就让两端指向同一个
        <code>HNE_CHROMA_PERSIST_DIR</code>。<br>
        ${how}
      </div>`;
  }

  let cachedEnv = null;

  function refresh() {
    api.get('/system/diagnostics')
      .then((env) => {
        cachedEnv = env;
        render(env, '');
      })
      .catch((err) => {
        // ★ 环境快照失败要说清原因：多半是没登录或后端没起来
        render(null, `环境信息读取失败：${err?.message || err}`);
      });
  }

  refresh();

  // 点路径 = 复制它（用户最常做的一件事：复制路径去文件管理器里打开）
  rows.addEventListener('click', async (e) => {
    const node = e.target.closest('[data-copy]');
    if (!node) return;
    const text = node.dataset.copy;
    if (await copyText(text)) toastOk('路径已复制');
    else toastErr('复制失败，请手动选中复制');
  });

  m.root.querySelector('#about-copy').addEventListener('click', async () => {
    const btn = m.root.querySelector('#about-copy');
    btn.disabled = true;
    try {
      // 尽量带上最新的组件状态；失败也不影响复制环境事实
      // ★ 注意：`/health` 在**根路径**（不带 /api/v1），所以不能用 `api.get`
      //   —— 那会给它拼上 `/api/v1` 前缀，变成 404。
      let health = null;
      try {
        const resp = await fetch('/health', { headers: { Accept: 'application/json' } });
        if (resp.ok) health = await resp.json();
      } catch {
        /* /health 拿不到就算了，诊断文本里仍会带"最近错误" */
      }
      const text = buildDiagnosticText(cachedEnv, health);
      if (await copyText(text)) {
        toastOk(`诊断信息已复制（含最近 ${Math.min(errorCount(), 5)} 条错误）`);
      } else {
        toastErr('复制失败，请手动选中复制');
      }
    } finally {
      btn.disabled = false;
    }
  });

  m.root.querySelector('#about-clear').addEventListener('click', () => {
    clearErrors();
    toastOk('已清空错误记录（下次复现问题时会重新收集）');
  });
}
