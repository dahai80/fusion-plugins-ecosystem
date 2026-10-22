/* Shared shell: injects sidebar + topbar into the Extensions console page.
   Console variant of f-ecommerce shell.js — menus come from /api/whoami
   (local console: single admin, no login flow). */

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
      document.documentElement.classList.toggle("light");
      document.cookie = "fs_theme=" +
        (document.documentElement.classList.contains("light") ? "light" : "dark") +
        "; path=/";
    };
    if (document.cookie.includes("fs_theme=light"))
      document.documentElement.classList.add("light");
    this.role = role;
  },
};
