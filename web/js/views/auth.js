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
