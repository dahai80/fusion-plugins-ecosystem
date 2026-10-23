/* Shared shell: injects sidebar + topbar into the Extensions console page.
   Console variant of f-ecommerce shell.js — menus come from /api/whoami
   (local console: single admin, no login flow). */

// ── theme (#33): shell 嵌入跟随宿主, standalone 沿用本地偏好 ──
// shell 宿主经 postMessage 推 {type:"event", event:"theme", payload:"dark"|"light"}
// (iframe 加载时会重推一次); 类主题切换 html.light/html.dark 驱动 --fs-* 令牌。
const theme = {
  apply(mode) {
    const root = document.documentElement;
    root.classList.toggle("light", mode === "light");
    root.classList.toggle("dark", mode !== "light");
  },
  current() {
    return document.documentElement.classList.contains("light") ? "light" : "dark";
  },
  // 初始: shell localStorage > 旧 cookie > prefers-color-scheme > dark。
  // 同步执行, 避免首帧错误主题闪烁; 嵌入时 shell 加载后仍会重推覆盖。
  init() {
    let mode = "";
    try {
      mode = localStorage.getItem("fusion.shell.theme") || "";
    } catch (e) { /* 私密模式等 localStorage 不可用 — 忽略 */ }
    if (!mode && document.cookie.includes("fs_theme=light")) mode = "light";
    if (!mode && window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: light)").matches) mode = "light";
    this.apply(mode === "light" ? "light" : "dark");
    // 嵌入模式: 跟随宿主推送 (仅同源消息; 主题事件带合法 payload 时才生效)
    window.addEventListener("message", (e) => {
      if (e.origin !== location.origin) return;
      const msg = e.data;
      if (!msg || msg.type !== "event" || msg.event !== "theme") return;
      if (msg.payload === "light" || msg.payload === "dark") this.apply(msg.payload);
    });
    return this;
  },
  // 本地切换: cookie (legacy) + localStorage (shell 同键, standalone 继承宿主偏好)
  toggle() {
    const next = this.current() === "light" ? "dark" : "light";
    this.apply(next);
    document.cookie = "fs_theme=" + next + "; path=/";
    try {
      localStorage.setItem("fusion.shell.theme", next);
    } catch (e) { /* 忽略 */ }
    return next;
  },
};
theme.init();

const shell = {
  async init() {
    // shell 模块化 (#30): 经 /shell/<id>/ 代理时 API 走同前缀
    const BASE = location.pathname.replace(/[^/]*$/, "");
    const me = await fetch(BASE + "api/whoami").then(r => r.json());
    const role = me.role || "admin";
    const page = document.body.dataset.page || "";
    const menus = Array.isArray(me.menus) && me.menus.length ? me.menus : [];
    const aside = document.createElement("aside");
    aside.className = "sidebar";
    aside.innerHTML =
      `<div class="brand">Fusion Plugins</div>` +
      menus.map(n => `<a href="${n.href}" class="${n.id === page ? "active" : ""}">${n.label}</a>`)
        .join("") +
      `<div class="spacer"></div>` +
      `<div class="foot">Console: <b>${me.user || "local"}</b><br>role: ${role}<br>` +
      `<a href="#" id="theme-toggle">theme</a></div>`;
    const layout = document.createElement("div");
    layout.className = "layout";
    while (document.body.firstChild) layout.appendChild(document.body.firstChild);
    const main = document.createElement("div");
    main.className = "main";
    while (layout.firstChild && layout.firstChild !== aside) {
      main.appendChild(layout.firstChild);
    }
    layout.appendChild(aside);
    layout.appendChild(main);
    document.body.appendChild(layout);

    // topbar: title + role badge
    const h1 = main.querySelector("h1");
    if (h1) {
      const bar = document.createElement("div");
      bar.className = "topbar";
      bar.appendChild(h1);
      const badge = document.createElement("span");
      badge.className = "badge " + (role === "auditor" ? "info" : "success");
      badge.textContent = role;
      bar.appendChild(badge);
      main.insertBefore(bar, main.children[0].nextSibling || null);
    }
    const toggle = document.getElementById("theme-toggle");
    if (toggle) toggle.onclick = (e) => {
      e.preventDefault();
      theme.toggle();
    };
    this.role = role;
  },
};
