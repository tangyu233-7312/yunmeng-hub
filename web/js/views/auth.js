/* ============================================================
   登录 / 注册页
   ============================================================ */

import { auth } from 'hne/api';
import { $, mount, toastErr, toastOk } from 'hne/ui';
// ★ 登录页也要能改存储（"登录不上是因为连错了库"这条必须能当场自救），
//   所以直接复用「存储设置」页那一份实现，不另写一套表单。
import { openStorageDialog } from 'hne/storage';

export function renderAuth(root, { onLoggedIn }) {
  mount(
    root,
    `
    <div class="auth-wrap">
      <div class="auth-box">
        <div class="auth-brand">
          <img class="auth-logo" src="/console/img/logo-256.png" alt="云梦枢" width="52" height="52" />
          <div>
            <h1>云梦枢</h1>
            <div class="auth-brand-sub">交互式叙事引擎</div>
          </div>
        </div>
        <div class="sub">支持自配任意大模型 API 的交互式叙事引擎</div>

        <!-- ★★ 存储方式提示（用于"登录不上"时自查）
             用户实测反馈过一件事：他切换存储方式之后拿原账号登录，界面只回
             "用户名或密码错误"，于是第一反应是"我的账号被人改了吗"。
             而真相是：两种存储（本机文件 / MySQL）是**两个互相独立的库** ——
             账号都不互通，切过去就得在新库里重新注册。
             这一块把"你现在连的是哪个库"直接写在登录页上，让那种误会不再发生。 -->
        <div id="auth-storage-note" class="auth-storage-note hidden"></div>

        <!-- ★★ 「存储设置」入口 —— **登录前**必须也能打开。
             用户的真实困境是"登录不上，因为连错了库"：那时他还没有令牌，
             进不去顶栏那个入口。所以按钮放在这里，且它能直接改存储。 -->
        <div class="mt14 center">
          <button type="button" class="ghost-btn" id="btn-storage">存储设置…</button>
        </div>

        <div class="auth-tabs">
          <button data-tab="login" class="active">登录</button>
          <button data-tab="register">注册</button>
        </div>

        <form id="form-login">
          <div class="field">
            <label>用户名</label>
            <input type="text" name="username" autocomplete="username" />
          </div>
          <div class="field">
            <label>密码</label>
            <input type="password" name="password" autocomplete="current-password" />
          </div>
          <button class="btn" type="submit" style="width:100%">登录</button>
        </form>

        <form id="form-register" class="hidden">
          <div class="field">
            <label>用户名</label>
            <input type="text" name="username" autocomplete="username"
                   placeholder="3~50 个字符" />
          </div>
          <div class="field">
            <label>邮箱</label>
            <input type="email" name="email" autocomplete="email" />
          </div>
          <div class="field">
            <label>密码</label>
            <input type="password" name="password" autocomplete="new-password" />
            <div class="hint">
              至少 8 位，且需包含字母与数字。注意：上限按 <b>UTF-8 字节数</b> 算 72 字节
              （bcrypt 的限制），中文密码约 24 个字就会超。
            </div>
          </div>
          <button class="btn" type="submit" style="width:100%">注册并登录</button>
        </form>

        <div class="mt14 small faint center">
          后端接口文档：<a href="/docs" target="_blank">/docs</a>
        </div>
      </div>
    </div>`,
  );

  // 切换登录 / 注册
  const tabs = root.querySelectorAll('.auth-tabs button');
  const formLogin = $('#form-login', root);
  const formRegister = $('#form-register', root);

  // ★★ 把"当前连的是哪个库"写在登录页上。
  //
  //   为什么要做：用户切换存储方式后拿原账号登录，界面只回"用户名或密码错误"，
  //   他会以为账号被人改了 —— 而真相是**两种存储是两个互相独立的库**
  //   （连账号都不互通）。这件事必须在用户**看得到的地方**说清楚，
  //   而不是只写在一屏之外的"切换存储方式"页上。
  //
  //   ★ 失败就**静默隐藏**：这段提示只是"锦上添花"，绝不能让 /health 探不通
  //     影响到登录本身（登录用的是另一个请求）。
  // ★ 抽成一个具名函数（而不是立即执行的匿名 IIFE）：因为**保存存储之后要重刷它** ——
  //   否则用户刚把库从 MySQL 切到本机文件，这句提示还写着"当前存储：MySQL"。
  async function refreshStorageNote() {
    const box = $('#auth-storage-note', root);
    if (!box) return;
    try {
      const resp = await fetch('/health', { headers: { Accept: 'application/json' } });
      if (!resp.ok) return;
      const health = await resp.json();
      const detail = ((health.components || {}).database || {}).detail || {};
      const which = String(detail.backend || '');
      const label = which === 'mysql' ? 'MySQL 数据库'
        : which === 'sqlite' ? '本机文件（SQLite）' : '';
      if (!label) return;
      // ★★ 同一份前端会跑在**两种环境**里，而"怎么切回去"不一样，
      //    所以必须分开说 —— 说错任何一边，用户都会按着找却找不到：
      //    · **Electron 桌面壳**：窗口顶部的「数据」菜单里就有
      //      「切换存储方式（本机文件 ↔ MySQL）…」（见 desktop/src/main.js 的 buildMenu）；
      //    · **浏览器直接打开 /console/**：那个菜单**不存在**（它属于桌面壳），
      //      只能改 .env 再重启后端。
      //
      //    ★ 我在这里犯过一次错（记在 docs/pitfalls.md §48）：
      //      只在 `web/` 与 `app/` 里搜索、**漏了 `desktop/`**，于是把那个
      //      真实存在的菜单判成"不存在的入口"，还顺手删掉了桌面端的正确指引。
      //      教训：搜索范围要覆盖所有相关目录，宁可多搜一遍。
      //
      //    判据沿用 about.js / diagnostics.js 的既有做法（UA 里的 Electron），
      //    不依赖 preload —— `window.yunmengSetup` 只在设置页可用。
      const inDesktop = /Electron\/[\d.]+/.test(navigator.userAgent || '');
      const where = which === 'sqlite'
        ? (inDesktop
          ? '桌面版的应用数据目录 <code>%APPDATA%\\云梦枢\\data\\</code>'
          : '后端数据目录下的 <code>data/app.sqlite3</code>')
        : '你配置的 MySQL 实例';

      // ★★ 卡片正文必须按**当前后端**分两种说法。
      //
      //   ==================== 这条守着一个我写错过的恒真断言 ====================
      //   早先这里一律写"**桌面版与浏览器是两套数据**、账号不互通"。
      //   那句话只在"两端没指向同一份库"时才成立：
      //     · MySQL 是**服务端** —— 桌面版与浏览器只要连同一个实例、同一个库名，
      //       看到的**就是同一份数据**，账号与会话都互通；
      //       说成"两套"会把用户误导去重新注册一个账号。
      //     · 本机文件（SQLite）才是**各端一份**，天然不互通。
      //   ⇒ 判据就是 `which`（已经从 /health 拿到），不要写成环境无关的一句话。
      const sharing = which === 'mysql'
        ? '桌面版连的是<b>同一份数据</b>，账号通用。'
        : '它是<b>本地文件</b>：桌面版与浏览器<b>各有一份</b>，账号不互通。';

      // ★★ 这一段是**压缩过的**，别再往里堆技术细节。
      //
      //   教训：第一版把三件事（共享机制 / 向量库路径 / 怎么切）全塞进登录页，
      //   用户看到的是"一大段技术说明，还带着 HNE_CHROMA_PERSIST_DIR 这种键名"，
      //   既看不懂、也不知道该点哪里。登录页只该回答一件事：**我现在用的是哪种存储**。
      //   细节与操作入口都移到「关于」与「存储设置」页（那里有上下文、能放表格）。
      //
      //   ★ 浏览器端**指向真实存在的入口**（导航里的「存储设置」），
      //     桌面壳里则指向它自己的菜单 —— 两边入口不同，说错就是指向不存在的地方
      //     （这个错本项目犯过，见 docs/pitfalls.md §48）。
      const howToSwitch = inDesktop
        ? '想换存储方式？顶部菜单「数据 → 切换存储方式…」。'
        : '想换存储方式？点下面的<b>「存储设置…」</b>按钮。';

      box.innerHTML = `
        <span class="auth-storage-tag">当前存储：${label}</span>
        <div class="auth-storage-hint">
          ${where}。${sharing}
          <br>${howToSwitch}
        </div>`;
      box.classList.remove('hidden');
    } catch {
      /* 探不到就什么都不显示：这段只是提示，不该影响登录 */
    }
  }

  refreshStorageNote();

  // ★★ 登录页上的「存储设置…」按钮（**登录前**可用）。
  //
  //   为什么放在这里而不是只放顶栏：用户的真实困境是
  //   "**登录不上**，因为连错了库" —— 那时他还没登录、顶栏是隐藏的
  //   （见 app.js 的 showChrome：`topbar.hidden = !loggedIn`），
  //   只放顶栏等于"要登得上才能修登不上"。
  //
  //   ★ 保存成功后要**重刷那段提示**：否则用户刚切完库，
  //     上面还写着"当前存储：MySQL 数据库"，自相矛盾。
  const storageBtn = $('#btn-storage', root);
  if (storageBtn) {
    storageBtn.addEventListener('click', () => {
      openStorageDialog({
        onSaved: () => {
          refreshStorageNote();
        },
      });
    });
  }

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      tabs.forEach((t) => t.classList.toggle('active', t === tab));
      const isLogin = tab.dataset.tab === 'login';
      formLogin.classList.toggle('hidden', !isLogin);
      formRegister.classList.toggle('hidden', isLogin);
      $(isLogin ? 'input' : 'input', isLogin ? formLogin : formRegister)?.focus();
    });
  });

  // ---- 登录 ----
  formLogin.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = formLogin.querySelector('button[type=submit]');
    btn.disabled = true;
    try {
      const values = {
        username: formLogin.username.value.trim(),
        password: formLogin.password.value,
      };
      await auth.login(values);
      await auth.loadMe();
      toastOk('登录成功');
      onLoggedIn();
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      btn.disabled = false;
    }
  });

  // ---- 注册（成功后自动登录）----
  formRegister.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = formRegister.querySelector('button[type=submit]');
    btn.disabled = true;
    try {
      const values = {
        username: formRegister.username.value.trim(),
        email: formRegister.email.value.trim(),
        password: formRegister.password.value,
      };
      await auth.register(values);
      // 注册接口只创建账号，不返回令牌，所以紧接着调一次登录
      await auth.login({ username: values.username, password: values.password });
      await auth.loadMe();
      toastOk('注册成功，已自动登录');
      onLoggedIn();
    } catch (err) {
      toastErr(err.toDisplay ? err.toDisplay() : String(err));
    } finally {
      btn.disabled = false;
    }
  });
}
