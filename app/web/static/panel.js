(function () {
  function debounce(fn, ms) {
    let t;
    return function () {
      const args = arguments;
      const ctx = this;
      clearTimeout(t);
      t = setTimeout(function () {
        fn.apply(ctx, args);
      }, ms);
    };
  }

  function qs(sel, root) {
    return (root || document).querySelector(sel);
  }

  function qsa(sel, root) {
    return Array.from((root || document).querySelectorAll(sel));
  }

  function selectedIds(root) {
    return qsa("input.row-pick:checked", root || document)
      .map(function (el) {
        return el.value;
      })
      .filter(Boolean);
  }

  function wireSelectAll(scope) {
    const all = qs("[data-select-all]", scope);
    if (!all) return;
    all.addEventListener("change", function () {
      qsa("input.row-pick", scope).forEach(function (el) {
        el.checked = all.checked;
      });
    });
  }

  function wireBulkForms(scope) {
    qsa("form[data-bulk]", scope || document).forEach(function (form) {
      form.addEventListener("submit", function (ev) {
        const field = form.getAttribute("data-bulk-field") || "ids";
        const idsInput =
          qs("input[name=" + field + "]", form) || qs("input[name=ids]", form);
        if (!idsInput) return;
        const ids = selectedIds(document);
        if (!ids.length) {
          ev.preventDefault();
          alert((window.__fxI18n && window.__fxI18n.selectOne) || "Select at least one row first.");
          return;
        }
        idsInput.value = ids.join(",");
        const action = (qs("input[name=action], select[name=action]", form) || {})
          .value;
        if (action === "delete" || action === "unlink" || action === "clear_url") {
          const tpl =
            (window.__fxI18n && window.__fxI18n.confirmBulk) ||
            "Confirm this action for {n} item(s)?";
          if (!confirm(tpl.replace("{n}", String(ids.length)))) {
            ev.preventDefault();
          }
        }
      });
    });
  }

  function wireLiveSearch(form) {
    if (!form || form.dataset.liveBound) return;
    form.dataset.liveBound = "1";
    const targetSel = form.getAttribute("data-live-target");
    const target = targetSel ? qs(targetSel) : null;
    if (!target) return;

    const refresh = debounce(async function () {
      const url = new URL(form.action || window.location.pathname, window.location.origin);
      const fd = new FormData(form);
      fd.forEach(function (v, k) {
        url.searchParams.set(k, String(v));
      });
      url.searchParams.set("partial", "1");
      url.searchParams.set("page", "0");
      try {
        const res = await fetch(url.toString(), { headers: { Accept: "text/html" } });
        if (!res.ok) return;
        const html = await res.text();
        target.innerHTML = html;
        wrapTables(target);
        wireSelectAll(target);
        wireCopyButtons(target);
        if (window.__fxApplyCols) window.__fxApplyCols();
        if (window.__fxApplyActs) window.__fxApplyActs();
        const count = qs("[data-result-count]");
        const meta = qs("[data-live-meta]", target);
        if (count && meta) count.textContent = meta.getAttribute("data-live-meta") || "";
        const cleaned = new URL(url.toString());
        cleaned.searchParams.delete("partial");
        history.replaceState(null, "", cleaned.pathname + (cleaned.search || ""));
      } catch (_e) {
        /* ignore transient fetch errors while typing */
      }
    }, 220);

    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);
  }

  const colAppliers = [];
  const actAppliers = [];

  function wireVisibilityPicker(box, attr, storageFallback, appliers, globalName) {
    if (!box || box.dataset.bound) return;
    box.dataset.bound = "1";
    const key = box.getAttribute("data-storage-key") || storageFallback;
    const rootSel =
      box.getAttribute("data-col-root") || box.getAttribute("data-act-root");
    const defaults = qsa("input[type=checkbox]", box).map(function (el) {
      return el.value;
    });
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem(key) || "null");
    } catch (_e) {
      saved = null;
    }
    if (!Array.isArray(saved) || !saved.length) saved = defaults;

    function apply() {
      const root = rootSel ? qs(rootSel) || document : document;
      const on = new Set(
        qsa("input[type=checkbox]:checked", box).map(function (el) {
          return el.value;
        })
      );
      qsa("[" + attr + "]", root).forEach(function (el) {
        const c = el.getAttribute(attr);
        el.style.display = on.has(c) ? "" : "none";
      });
      localStorage.setItem(
        key,
        JSON.stringify(
          qsa("input[type=checkbox]:checked", box).map(function (el) {
            return el.value;
          })
        )
      );
    }

    appliers.push(apply);
    window[globalName] = function () {
      appliers.forEach(function (fn) {
        fn();
      });
    };
    qsa("input[type=checkbox]", box).forEach(function (el) {
      el.checked = saved.indexOf(el.value) >= 0;
      el.addEventListener("change", apply);
    });
    apply();
  }

  function wireColumnPickers() {
    qsa("[data-col-picker]").forEach(function (box) {
      wireVisibilityPicker(
        box,
        "data-col",
        "fx_cols_default",
        colAppliers,
        "__fxApplyCols"
      );
    });
  }

  function wireActionPickers() {
    qsa("[data-act-picker]").forEach(function (box) {
      wireVisibilityPicker(
        box,
        "data-act",
        "fx_acts_default",
        actAppliers,
        "__fxApplyActs"
      );
    });
  }

  function wireCardNumber() {
    const input = qs("input[data-card-group]");
    if (!input) return;
    function group(v) {
      const digits = String(v || "").replace(/\D/g, "").slice(0, 19);
      return digits.replace(/(\d{4})(?=\d)/g, "$1 ").trim();
    }
    input.value = group(input.value);
    input.addEventListener("input", function () {
      const start = input.selectionStart;
      const before = input.value;
      input.value = group(input.value);
      try {
        const diff = input.value.length - before.length;
        input.setSelectionRange(start + diff, start + diff);
      } catch (_e) {}
    });
  }

  function currentTheme() {
    const t = document.documentElement.getAttribute("data-theme");
    if (t === "light" || t === "ocean") return t;
    return "dark";
  }

  function applyTheme(theme) {
    const next =
      theme === "light" ? "light" : theme === "ocean" ? "ocean" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("fx_theme", next);
    } catch (_e) {}
    qsa("[data-theme-toggle]").forEach(function (group) {
      qsa("button[data-theme-value]", group).forEach(function (btn) {
        btn.setAttribute(
          "aria-pressed",
          btn.getAttribute("data-theme-value") === next ? "true" : "false"
        );
      });
    });
  }

  function wireThemeToggle() {
    applyTheme(currentTheme());
    qsa("[data-theme-toggle]").forEach(function (group) {
      qsa("button[data-theme-value]", group).forEach(function (btn) {
        btn.addEventListener("click", function () {
          applyTheme(btn.getAttribute("data-theme-value"));
        });
      });
    });
  }

  function wireCtrlASelect() {
    document.addEventListener("keydown", function (ev) {
      if (!(ev.ctrlKey || ev.metaKey)) return;
      if ((ev.key || "").toLowerCase() !== "a") return;
      const picks = qsa("input.row-pick");
      if (!picks.length) return;
      const tag = (ev.target && ev.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
        return;
      }
      ev.preventDefault();
      picks.forEach(function (el) {
        el.checked = true;
      });
      qsa("[data-select-all]").forEach(function (el) {
        el.checked = true;
      });
    });
  }

  function toast(msg) {
    let el = qs("[data-toast]");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      el.setAttribute("data-toast", "1");
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(function () {
      el.classList.remove("show");
    }, 1600);
  }

  async function copyText(text) {
    const value = String(text || "");
    if (!value) return false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(value);
        return true;
      }
    } catch (_e) {}
    try {
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (_e2) {
      return false;
    }
  }

  function wireCopyButtons(scope) {
    qsa("[data-copy]", scope || document).forEach(function (btn) {
      if (btn.dataset.copyBound) return;
      btn.dataset.copyBound = "1";
      btn.addEventListener("click", async function () {
        const ok = await copyText(btn.getAttribute("data-copy"));
        const i18n = window.__fxI18n || {};
        toast(ok ? i18n.toastCopied || "📋 Copied" : i18n.toastCopyFail || "Copy failed");
      });
    });
  }

  function parseCatalog(el) {
    try {
      return JSON.parse(el.getAttribute("data-catalog") || "[]");
    } catch (_e) {
      return [];
    }
  }

  function usedActions(builder) {
    return qsa("[data-kb-btn]", builder).map(function (b) {
      return b.getAttribute("data-action");
    });
  }

  function syncKbLayout(builder) {
    const hidden = qs("[data-kb-layout]", builder);
    if (!hidden) return;
    const lines = [];
    qsa("[data-kb-row]", builder).forEach(function (row) {
      const ids = qsa("[data-kb-btn]", row).map(function (b) {
        return b.getAttribute("data-action");
      });
      if (ids.length) lines.push(ids.join(", "));
    });
    hidden.value = lines.join("\n");
  }

  function makeKbBtn(builder, item) {
    const prefix = builder.getAttribute("data-label-prefix") || "label_";
    const wrap = document.createElement("div");
    wrap.className = "kb-btn";
    wrap.setAttribute("data-kb-btn", "");
    wrap.setAttribute("data-action", item.id);
    wrap.innerHTML =
      '<span class="kb-btn-meta"></span>' +
      '<input class="kb-btn-label" dir="rtl" autocomplete="off" />' +
      '<div class="kb-btn-tools">' +
      '<button type="button" class="kb-ico" data-kb-move-left title="→">‹</button>' +
      '<button type="button" class="kb-ico" data-kb-move-right title="←">›</button>' +
      '<button type="button" class="kb-ico danger" data-kb-remove title="×">×</button>' +
      "</div>";
    qs(".kb-btn-meta", wrap).textContent = item.fa_name || item.id;
    const input = qs(".kb-btn-label", wrap);
    input.name = prefix + item.id;
    input.value = item.label || item.default_label || item.fa_name || item.id;
    return wrap;
  }

  function makeKbRow(builder) {
    const row = document.createElement("div");
    row.className = "kb-row";
    row.setAttribute("data-kb-row", "");
    const add = document.createElement("button");
    add.type = "button";
    add.className = "kb-add-in-row";
    add.setAttribute("data-kb-add-in-row", "");
    add.textContent = "+";
    const tools = document.createElement("div");
    tools.className = "kb-row-tools";
    tools.innerHTML =
      '<button type="button" class="kb-ico" data-kb-row-up title="↑">↑</button>' +
      '<button type="button" class="kb-ico" data-kb-row-down title="↓">↓</button>' +
      '<button type="button" class="kb-ico danger" data-kb-row-remove title="×">×</button>';
    row.appendChild(add);
    row.appendChild(tools);
    return row;
  }

  function closePickers() {
    qsa(".kb-pick").forEach(function (el) {
      el.remove();
    });
  }

  function openPicker(anchor, builder, onPick) {
    closePickers();
    const catalog = parseCatalog(builder);
    const used = new Set(usedActions(builder));
    const available = catalog.filter(function (c) {
      return !used.has(c.id);
    });
    if (!available.length) {
      toast((window.__fxI18n && window.__fxI18n.menuNoActions) || "No actions left");
      return;
    }
    const pick = document.createElement("div");
    pick.className = "kb-pick";
    available.forEach(function (item) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = item.fa_name + " · " + (item.label || item.default_label || "");
      b.addEventListener("click", function () {
        onPick(item);
        closePickers();
      });
      pick.appendChild(b);
    });
    document.body.appendChild(pick);
    const r = anchor.getBoundingClientRect();
    pick.style.left = Math.max(8, r.left + window.scrollX) + "px";
    pick.style.top = r.bottom + window.scrollY + 4 + "px";
  }

  function wireKbBuilder(builder) {
    const rowsHost = qs("[data-kb-rows]", builder);
    if (!rowsHost) return;

    builder.addEventListener("click", function (ev) {
      const t = ev.target;
      if (!(t instanceof Element)) return;

      if (t.closest("[data-kb-add-row]")) {
        rowsHost.appendChild(makeKbRow(builder));
        syncKbLayout(builder);
        return;
      }

      const addIn = t.closest("[data-kb-add-in-row]");
      if (addIn) {
        const row = addIn.closest("[data-kb-row]");
        openPicker(addIn, builder, function (item) {
          row.insertBefore(makeKbBtn(builder, item), addIn);
          syncKbLayout(builder);
        });
        return;
      }

      const btn = t.closest("[data-kb-btn]");
      const row = t.closest("[data-kb-row]");

      if (t.closest("[data-kb-remove]") && btn) {
        btn.remove();
        syncKbLayout(builder);
        return;
      }
      if (t.closest("[data-kb-move-left]") && btn && btn.nextElementSibling) {
        const next = btn.nextElementSibling;
        if (next && next.hasAttribute("data-kb-btn")) {
          btn.parentNode.insertBefore(next, btn);
          syncKbLayout(builder);
        }
        return;
      }
      if (t.closest("[data-kb-move-right]") && btn && btn.previousElementSibling) {
        const prev = btn.previousElementSibling;
        if (prev && prev.hasAttribute("data-kb-btn")) {
          btn.parentNode.insertBefore(btn, prev);
          syncKbLayout(builder);
        }
        return;
      }
      if (t.closest("[data-kb-row-remove]") && row) {
        row.remove();
        syncKbLayout(builder);
        return;
      }
      if (t.closest("[data-kb-row-up]") && row && row.previousElementSibling) {
        row.parentNode.insertBefore(row, row.previousElementSibling);
        syncKbLayout(builder);
        return;
      }
      if (t.closest("[data-kb-row-down]") && row && row.nextElementSibling) {
        row.parentNode.insertBefore(row.nextElementSibling, row);
        syncKbLayout(builder);
        return;
      }
    });

    builder.addEventListener("input", function (ev) {
      if (ev.target && ev.target.classList && ev.target.classList.contains("kb-btn-label")) {
        syncKbLayout(builder);
      }
    });

    syncKbLayout(builder);
  }

  function syncInlineOrder(list) {
    const hidden = qs("[data-inline-order]", list);
    if (!hidden) return;
    const ids = qsa("[data-inline-item]", list).map(function (el) {
      return el.getAttribute("data-item-id");
    });
    hidden.value = ids.join(",");
    qsa(".inline-idx", list).forEach(function (el, i) {
      el.textContent = String(i + 1);
    });
  }

  function wireInlineMenus(scope) {
    qsa("[data-inline-menu]", scope || document).forEach(function (list) {
      list.addEventListener("click", function (ev) {
        const t = ev.target;
        if (!(t instanceof Element)) return;
        const item = t.closest("[data-inline-item]");
        if (!item) return;
        if (t.closest("[data-inline-up]") && item.previousElementSibling) {
          const prev = item.previousElementSibling;
          if (prev && prev.hasAttribute("data-inline-item")) {
            list.insertBefore(item, prev);
            syncInlineOrder(list);
          }
          return;
        }
        if (t.closest("[data-inline-down]") && item.nextElementSibling) {
          const next = item.nextElementSibling;
          if (next && next.hasAttribute("data-inline-item")) {
            list.insertBefore(next, item);
            syncInlineOrder(list);
          }
        }
      });
      syncInlineOrder(list);
    });
  }

  function wireMenusForm() {
    const form = qs("form[data-menus-form]");
    if (!form) return;
    qsa("[data-kb-builder]", form).forEach(wireKbBuilder);
    wireInlineMenus(form);
    form.addEventListener("submit", function () {
      qsa("[data-kb-builder]", form).forEach(syncKbLayout);
      qsa("[data-inline-menu]", form).forEach(syncInlineOrder);
    });
    document.addEventListener("click", function (ev) {
      const t = ev.target;
      if (!(t instanceof Element)) return;
      if (!t.closest(".kb-pick") && !t.closest("[data-kb-add-in-row]")) {
        closePickers();
      }
    });
  }

  function applyNavBadges(badges) {
    const map = badges || {};
    qsa("[data-nav-badge]").forEach(function (el) {
      const key = el.getAttribute("data-nav-badge");
      const n = Number(map[key] || 0);
      if (n > 0) {
        el.textContent = n > 99 ? "99+" : String(n);
        el.classList.remove("is-zero");
      } else {
        el.textContent = "";
        el.classList.add("is-zero");
      }
    });
  }

  function notifyStatusEl() {
    return qs("[data-notify-status]");
  }

  function setNotifyStatus(text) {
    const el = notifyStatusEl();
    if (el) el.textContent = text || "";
  }

  function browserNotify(title, body) {
    try {
      if (!("Notification" in window)) return;
      if (Notification.permission !== "granted") return;
      const n = new Notification(title, {
        body: body,
        tag: "fx-panel-" + String(body || "").slice(0, 40),
      });
      setTimeout(function () {
        try {
          n.close();
        } catch (_e) {}
      }, 8000);
    } catch (_e) {}
  }

  function diffBadgeAlerts(prev, next) {
    const i18n = window.__fxI18n || {};
    const labels = {
      orders: i18n.notifyOrders || "Orders",
      queue: i18n.notifyQueue || "Queue",
      tickets: i18n.notifyTickets || "Tickets",
      almost_out: i18n.notifyAlmost || "Almost out",
      warnings: i18n.notifyWarnings || "Vendor warnings",
    };
    const parts = [];
    Object.keys(labels).forEach(function (key) {
      const a = Number((prev && prev[key]) || 0);
      const b = Number((next && next[key]) || 0);
      if (b > a) {
        parts.push(labels[key] + ": " + b);
      }
    });
    return parts;
  }

  function wirePanelNotifications() {
    if (!qs("[data-panel-nav]")) return;
    const i18n = window.__fxI18n || {};
    const storageKey = "fx_badges_prev";
    let prev = window.__fxBadges || {};
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) prev = JSON.parse(raw) || prev;
    } catch (_e) {}
    applyNavBadges(window.__fxBadges || prev);

    const enableBtn = qs("[data-notify-enable]");
    function refreshPermissionLabel() {
      if (!("Notification" in window)) {
        setNotifyStatus(i18n.notifyOff || "Off");
        return;
      }
      if (Notification.permission === "granted") {
        setNotifyStatus(i18n.notifyOn || "On");
      } else if (Notification.permission === "denied") {
        setNotifyStatus(i18n.notifyDenied || "Blocked");
      } else {
        setNotifyStatus(i18n.notifyOff || "Off");
      }
    }
    refreshPermissionLabel();
    if (enableBtn) {
      enableBtn.addEventListener("click", async function () {
        if (!("Notification" in window)) return;
        try {
          const perm = await Notification.requestPermission();
          refreshPermissionLabel();
          if (perm === "granted") {
            browserNotify(
              i18n.notifyTitle || "FX One",
              i18n.notifyOn || "Notifications on"
            );
          }
        } catch (_e) {
          refreshPermissionLabel();
        }
      });
    }

    async function poll() {
      try {
        const res = await fetch("/api/panel-status", {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!res.ok) return;
        const data = await res.json();
        const badges = (data && data.badges) || {};
        applyNavBadges(badges);
        const parts = diffBadgeAlerts(prev, badges);
        if (parts.length) {
          const body = parts.join(" · ");
          toast("🔔 " + body);
          browserNotify(i18n.notifyTitle || "FX One", body);
        }
        prev = badges;
        try {
          localStorage.setItem(storageKey, JSON.stringify(badges));
        } catch (_e2) {}
      } catch (_e3) {}
    }

    setInterval(poll, 20000);
    setTimeout(poll, 4000);
  }

  function wrapTables(scope) {
    qsa("table", scope || document).forEach(function (table) {
      const parent = table.parentElement;
      if (!parent) return;
      if (parent.classList.contains("table-scroll")) return;
      if (table.closest(".kb-builder") || table.closest(".login-card")) return;
      const wrap = document.createElement("div");
      wrap.className = "table-scroll";
      parent.insertBefore(wrap, table);
      wrap.appendChild(table);
    });
  }

  function setNavOpen(open) {
    document.body.classList.toggle("nav-open", !!open);
    qsa("[data-nav-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  function wireMobileNav() {
    const toggle = qs("[data-nav-toggle]");
    if (!toggle) return;
    toggle.addEventListener("click", function () {
      setNavOpen(!document.body.classList.contains("nav-open"));
    });
    qsa("[data-nav-close]").forEach(function (el) {
      el.addEventListener("click", function () {
        setNavOpen(false);
      });
    });
    qsa("[data-side-nav] a").forEach(function (a) {
      a.addEventListener("click", function () {
        if (window.matchMedia("(max-width: 980px)").matches) {
          setNavOpen(false);
        }
      });
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") setNavOpen(false);
    });
    window.addEventListener(
      "resize",
      debounce(function () {
        if (!window.matchMedia("(max-width: 980px)").matches) {
          setNavOpen(false);
        }
      }, 120)
    );
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireThemeToggle();
    wireSelectAll(document);
    wireBulkForms(document);
    qsa("form[data-live-search]").forEach(wireLiveSearch);
    wireColumnPickers();
    wireActionPickers();
    wireCardNumber();
    wireCtrlASelect();
    wireCopyButtons(document);
    wireMenusForm();
    wirePanelNotifications();
    wrapTables(document);
    wireMobileNav();
  });
})();
