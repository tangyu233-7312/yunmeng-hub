/* ============================================================
   登录 / 注册页
   ============================================================ */

import { auth } from 'hne/api';
import { $, mount, toastErr, toastOk } from 'hne/ui';

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
  (async () => {
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
      const where = which === 'sqlite' ? '应用数据目录下的 <code>data/app.sqlite3</code>'
        : '你配置的 MySQL 实例';
      box.innerHTML = `
        <span class="auth-storage-tag">当前存储：${label}</span>
        <div class="auth-storage-hint">
          ${where}。★ 两种存储是<b>两个互相独立的库</b>，数据与<b>账号都不互通</b> ——
          如果你在用 MySQL 时的账号在这里登录不上，多半是因为现在连的是另一个库：
          菜单「数据 → 切换存储方式…」可以切回去；也可以在这里直接<b>注册</b>一个新账号。
        </div>`;
      box.classList.remove('hidden');
    } catch {
      /* 探不到就什么都不显示：这段只是提示，不该影响登录 */
    }
  })();

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
