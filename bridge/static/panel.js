/* Panel de TI · Dimabe Print Bridge. Sin dependencias: el panel funciona sin internet. */
(function () {
  "use strict";

  const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const PAGE = document.body.dataset.page;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // -- utilidades ---------------------------------------------------------
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (key === "dataset") Object.assign(el.dataset, value);
      else el.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function icon(name, cls = "i s") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", cls);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#" + name);
    svg.append(use);
    return svg;
  }

  function toast(message, kind = "info") {
    const box = h("div", { class: "toast" + (kind === "error" ? " error" : ""), role: kind === "error" ? "alert" : "status" }, message);
    $("#toasts").append(box);
    setTimeout(() => box.remove(), kind === "error" ? 7000 : 4000);
  }

  async function api(path, options = {}) {
    const init = { method: options.method || "GET", headers: { Accept: "application/json" } };
    if (init.method !== "GET") {
      init.headers["Content-Type"] = "application/json";
      init.headers["X-CSRF-Token"] = CSRF;
      init.body = JSON.stringify(options.body || {});
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      throw new Error("No hay conexión con el puente. ¿Sigue corriendo el servicio?");
    }
    if (response.status === 401) {
      location.href = "/panel/login";
      throw new Error("La sesión expiró.");
    }
    let data = {};
    try { data = await response.json(); } catch (err) { data = {}; }
    if (!response.ok || data.result === false) {
      const error = new Error(data.message || `El puente respondió ${response.status}.`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  async function busy(button, fn) {
    if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
    try { return await fn(); }
    catch (err) { toast(err.message, "error"); return null; }
    finally { if (button) { button.disabled = false; button.removeAttribute("aria-busy"); } }
  }

  function confirmDialog(title, text, okLabel = "Continuar") {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    $("#confirm-ok").textContent = okLabel;
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
      dialog.showModal();
    });
  }

  const nf = new Intl.NumberFormat("es-CL");
  const fmtTime = (iso) => (iso ? iso.slice(11, 19) : "—");
  const fmtBytes = (n) => (n == null ? "—" : n >= 1048576 ? `${(n / 1048576).toFixed(1).replace(".", ",")} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);
  const fmtMs = (ms) => (ms == null ? "—" : ms >= 1000 ? `${(ms / 1000).toFixed(2).replace(".", ",")} s` : `${ms} ms`);
  const today = () => { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); };

  const DOC = { receipt: "Boleta", kitchen: "Comanda", preparation: "Comanda", label: "Etiqueta", cashbox: "Cajón", test: "Prueba" };
  const KIND = { queue: "Cola", health: "Consulta de estado", printers: "Lista de impresoras", service: "Servicio", config: "Configuración", rejected: "Rechazado" };
  const RESULT = { ok: ["ok", "Correcto"], error: ["err", "Con error"], rejected: ["rejected", "Rechazado"], duplicate: ["off", "Repetido"], queued: ["warn", "En cola"] };

  function actionLabel(row) {
    if (row.kind === "print" || row.kind === "cashbox") return DOC[row.document_type] || row.document_type || "Impresión";
    return KIND[row.kind] || row.kind;
  }
  function resultPill(row) {
    const [cls, label] = RESULT[row.result] || ["off", row.result || "—"];
    return h("span", { class: "pill " + cls }, row.kind === "cashbox" && row.result === "ok" ? "Abierto" : label);
  }
  function originLabel(origin) {
    if (!origin) return "—";
    if (origin === "panel" || origin === "panel-remoto") return "Panel";
    try { const host = new URL(origin).hostname; return host.endsWith(".odoo.com") ? "POS · odoo.com" : host; }
    catch (err) { return origin; }
  }

  function poll(fn, ms) {
    let timer = null;
    const tick = async () => { await fn(); timer = setTimeout(loop, ms); };
    const loop = () => { if (document.visibilityState === "visible") tick(); else timer = setTimeout(loop, ms); };
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { clearTimeout(timer); tick(); } });
    tick();
    return () => { clearTimeout(timer); tick(); };
  }

  // -- tema ---------------------------------------------------------------
  $("#theme-toggle")?.addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("bridge-theme", root.dataset.theme); } catch (err) { /* sin almacenamiento */ }
  });

  function lastText(p) {
    const msg = p.last_message || "";
    if (/^(Raw bytes sent|Printed on|Cash drawer pulse)/.test(msg)) return "Impreso";
    return msg;
  }

  // -- impresoras: fila común ----------------------------------------------
  const STATUS_PILL = { ok: "ok", warn: "warn", error: "err", test: "test", off: "off" };
  function printerStatus(p) { return h("span", { class: "pill " + (STATUS_PILL[p.status] || "off") }, p.status_text || "—"); }
  function connection(p) {
    return p.kind === "ip"
      ? h("span", { class: "inline" }, icon("i-net"), "Red")
      : h("span", { class: "inline" }, icon("i-usb"), p.kind === "virtual" ? "Virtual" : "USB");
  }

  // -- Estado ---------------------------------------------------------------
  function initEstado() {
    const render = async () => {
      let data;
      try { data = await api("/panel/api/status"); }
      catch (err) { renderDown(err.message); return; }
      const s = data.service, t = data.today;
      const errorsNow = data.attention.filter((a) => a.level === "error");
      const beacon = $("#status-beacon");
      beacon.className = "beacon" + (s.test_mode ? " test" : errorsNow.length ? " err" : "");
      beacon.replaceChildren(icon(s.test_mode ? "i-flask" : errorsNow.length ? "i-alert" : "i-check", "i"));
      $("#status-title").textContent = s.test_mode ? "Modo prueba: el puente responde, pero no imprime en papel"
        : "El puente está atendiendo al POS";
      const failed = Object.entries(t.by_target).filter(([, v]) => v.error).map(([k]) => k);
      // replaceChildren no aplana listas anidadas: sin .flat(2) salía «[object HTMLElement]».
      $("#status-text").replaceChildren(...[
        "Escuchando en ", h("span", { class: "mono" }, s.listen_url.replace("http://", "").replace(/\/$/, "")),
        s.since ? ` desde las ${fmtTime(s.since).slice(0, 5)}` : "", ". Hoy imprimió ",
        h("b", { class: "num" }, nf.format(t.ok)), " trabajo(s)",
        t.error ? ["; ", h("b", { class: "num", style: "color:var(--err)" }, nf.format(t.error)), " fallaron",
          failed.length ? [", en ", h("b", {}, failed.join(", "))] : ""] : "",
        "."].flat(2));
      $("#status-pills").replaceChildren(...[
        h("span", { class: "pill " + (s.state === "running" ? "ok" : "err") }, s.state === "running" ? "Servicio en línea" : "Puerto ocupado"),
        h("span", { class: "pill " + (s.api_key_set ? "ok" : "off") }, s.api_key_set ? "Clave API activa" : "Sin clave API (opcional)"),
        h("span", { class: "pill " + (s.test_mode ? "test" : "off") }, s.test_mode ? "Modo prueba encendido" : "Modo prueba apagado"),
        s.remote_enabled ? h("span", { class: "pill warn" }, "Panel abierto a la red") : null].filter(Boolean));
      const last = Object.values(t.by_target).filter((v) => v.last_ts).sort((a, b) => (a.last_ts < b.last_ts ? 1 : -1))[0];
      const facts = $$("#status-facts dd");
      const lastLabel = Object.entries(t.by_target).find(([, v]) => v === last)?.[0];
      facts[0].textContent = last ? `${fmtTime(last.last_ts)} · ${lastLabel}` : "Sin trabajos hoy";
      facts[1].textContent = t.total ? `${t.ok + t.error} hoy` : "—";
      facts[1].previousElementSibling.textContent = "Trabajos de hoy";
      facts[2].textContent = data.last_rejected ? `${data.last_rejected.ts.slice(0, 10)} ${fmtTime(data.last_rejected.ts).slice(0, 5)} · ${originLabel(data.last_rejected.origin)}` : "Ninguna";
      facts[3].textContent = `${data.journal.days} día(s) · ${fmtBytes(data.journal.bytes)}`;
      $("#topbar-sub").textContent = `Actualizado ${new Date().toLocaleTimeString("es-CL")}`;
      renderPrinters(data.printers);
      renderHours(t);
      renderAttention(data.attention);
      const navCount = $("#nav-printer-errors");
      const bad = data.printers.filter((p) => p.status === "error" || p.status === "warn").length;
      if (navCount) { navCount.hidden = !bad; navCount.textContent = bad; }
    };
    const renderDown = (message) => {
      $("#status-beacon").className = "beacon err";
      $("#status-title").textContent = "No se pudo leer el estado";
      $("#status-text").textContent = message;
    };
    const renderPrinters = (printers) => {
      $("#printers-meta").textContent = printers.length ? `${printers.length} con actividad` : "";
      $("#printers-body").replaceChildren(...(printers.length ? printers.map((p) => h("tr", {},
        h("td", {}, h("div", { class: "target" }, h("b", {}, p.label), p.detail ? h("small", {}, p.detail) : null)),
        h("td", {}, connection(p)),
        h("td", {}, printerStatus(p)),
        h("td", { class: "r num" }, nf.format(p.today_ok + p.today_error)),
        h("td", { class: "num" }, p.last_ts ? `${fmtTime(p.last_ts).slice(0, 5)} · ${lastText(p)}` : "—"))) :
        [h("tr", {}, h("td", { colspan: 5, class: "empty" }, "Todavía no hay trabajos hoy. Cuando el POS imprima, las impresoras aparecerán aquí."))]));
    };
    const renderHours = (t) => {
      const max = Math.max(1, ...t.by_hour.map((v, i) => v + t.errors_by_hour[i]));
      $("#hours").replaceChildren(...t.by_hour.map((ok, hour) => {
        const err = t.errors_by_hour[hour];
        return h("div", { class: "bar", title: `${hour}:00 · ${ok} impresos, ${err} con error` },
          h("i", { style: `height:${(ok / max) * 100}%` }), h("i", { class: "e", style: `height:${(err / max) * 100}%` }));
      }));
      $("#hours").setAttribute("aria-label", `Hoy: ${t.ok} impresos y ${t.error} con error.`);
    };
    const renderAttention = (items) => {
      $("#attention").replaceChildren(...(items.length ? items.map((a) => h("div", { class: "note-box " + a.level },
        icon(a.level === "test" ? "i-flask" : "i-alert", "i"),
        h("div", {}, h("b", {}, a.title), h("br"), a.detail, " ", a.link ? h("a", { href: a.link }, "Revisar") : null))) :
        [h("div", { class: "note-box ok" }, icon("i-check", "i"), h("div", {}, h("b", {}, "Todo en orden"), h("br"), "No hay errores en los últimos 30 minutos."))]));
    };
    const renderQueue = async () => {
      let data;
      try { data = await api("/panel/api/queue"); } catch (err) { return; }
      const section = $("#cola");
      section.hidden = !data.pending.length;
      if (!data.pending.length) return;
      $("#queue-meta").textContent = `${data.counts.total} esperando que vuelva su impresora · se imprimen solas`;
      const action = (id, verb, label, confirmText) => h("button", {
        class: "btn" + (verb === "discard" ? " danger" : ""), type: "button", onclick: async (e) => {
          if (confirmText && !(await confirmDialog("¿Descartar este trabajo?", confirmText, "Descartar"))) return;
          const res = await busy(e.currentTarget, () => api(`/panel/api/queue/${id}/${verb}`, { method: "POST" }));
          if (res) { toast(res.message); renderQueue(); }
        },
      }, label);
      $("#queue-body").replaceChildren(...data.pending.map((job) => h("tr", {},
        h("td", { class: "num" }, new Date(job.created * 1000).toLocaleTimeString("es-CL")),
        h("td", {}, job.target_label), h("td", {}, DOC[job.document_type] || job.document_type),
        h("td", { class: "r num" }, job.attempts), h("td", {}, job.last_error || "—"),
        h("td", { class: "r" }, h("div", { class: "inline end" },
          action(job.id, "retry", "Reintentar ahora"),
          action(job.id, "discard", "Descartar", "La comanda no se imprimirá. Úsalo solo si cocina ya la tiene."))))));
    };
    const refresh = poll(async () => { await render(); await renderQueue(); }, 5000);
    $('[data-action="refresh"]')?.addEventListener("click", refresh);
  }

  // -- Impresoras -------------------------------------------------------------
  function initImpresoras() {
    const testPrinter = async (button, target, kind) => {
      if (kind !== "connection") {
        const paper = kind === "cashbox" ? "Se abrirá el cajón" : "Saldrá un ticket de prueba en papel";
        if (!(await confirmDialog("¿Probar la impresora?", `${paper} en ${target.printer_name || target.ip_address}.`, "Probar"))) return;
      }
      const data = await busy(button, () => api("/panel/api/printers/test", { method: "POST", body: { target, kind } }));
      if (data) toast(data.message);
      return data;
    };
    const load = async () => {
      let data;
      try { data = await api("/panel/api/printers"); } catch (err) { toast(err.message, "error"); return; }
      const byLabel = new Map(data.printers.map((p) => [p.label, p]));
      const rows = [];
      for (const w of data.windows) {
        const p = byLabel.get(w.Name);
        byLabel.delete(w.Name);
        const target = { printer_name: w.Name };
        rows.push(h("tr", {},
          h("td", {}, h("b", {}, w.Name), h("div", { class: "muted" }, w.DriverName)),
          h("td", {}, "USB / Windows"), h("td", { class: "mono" }, w.PortName),
          h("td", {}, p ? printerStatus(p) : h("span", { class: "pill off" }, "Sin uso hoy")),
          h("td", { class: "r num" }, p ? nf.format(p.today_ok) : "0"), h("td", { class: "r num" }, p ? nf.format(p.today_error) : "0"),
          h("td", { class: "r" }, h("div", { class: "inline end" },
            h("button", { class: "btn", type: "button", onclick: (e) => testPrinter(e.currentTarget, target, "connection") }, "Estado"),
            h("button", { class: "btn", type: "button", onclick: (e) => testPrinter(e.currentTarget, target, "text") }, "Probar texto"),
            h("button", { class: "btn", type: "button", onclick: (e) => testPrinter(e.currentTarget, target, "cashbox") }, "Abrir cajón")))));
      }
      for (const p of byLabel.values()) {
        const [ip, port] = p.label.split(":");
        const target = p.kind === "ip" ? { ip_address: ip, ip_port: Number(port) } : { printer_name: p.label };
        rows.push(h("tr", {},
          h("td", {}, h("b", {}, p.label), h("div", { class: "muted" }, p.kind === "ip" ? "Impresora de red usada por el POS" : "Usada por el POS")),
          h("td", {}, p.kind === "ip" ? "Red" : "USB"), h("td", { class: "mono" }, p.label),
          h("td", {}, printerStatus(p)), h("td", { class: "r num" }, nf.format(p.today_ok)), h("td", { class: "r num" }, nf.format(p.today_error)),
          h("td", { class: "r" }, h("div", { class: "inline end" },
            h("button", { class: "btn", type: "button", onclick: (e) => testPrinter(e.currentTarget, target, "connection") }, "Probar conexión"),
            h("button", { class: "btn", type: "button", onclick: (e) => testPrinter(e.currentTarget, target, "text") }, "Probar texto")))));
      }
      $("#printers-body").replaceChildren(...(rows.length ? rows : [h("tr", {}, h("td", { colspan: 7, class: "empty" }, "Windows no informa impresoras y el POS aún no ha usado ninguna de red."))]));
    };
    $("#ip-test-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const kind = event.submitter?.value || "connection";
      const target = { ip_address: $("#ip-test-ip").value.trim(), ip_port: Number($("#ip-test-port").value) || 9100 };
      const data = await testPrinter(event.submitter, target, kind);
      $("#ip-test-result").textContent = data ? data.message : "";
    });
    $('[data-action="refresh"]').addEventListener("click", load);
    load();
  }

  // -- Bitácora ----------------------------------------------------------------
  function initBitacora() {
    const params = new URLSearchParams(location.search);
    const state = { day: params.get("dia") || today(), result: "", target: params.get("target") || "", document_type: "", q: "", offset: 0, selected: null };
    $("#f-day").value = state.day;
    let debounce;

    const query = () => new URLSearchParams({ day: state.day, result: state.result, target: state.target, document_type: state.document_type, q: state.q, offset: state.offset, limit: 200 });
    const load = async (append = false) => {
      let data;
      try { data = await api("/panel/api/journal?" + query()); } catch (err) { toast(err.message, "error"); return; }
      const s = data.summary;
      $$("[data-count]").forEach((el) => { el.textContent = nf.format(s[el.dataset.count] ?? 0); });
      const dayLabel = new Date(state.day + "T12:00").toLocaleDateString("es-CL", { weekday: "long", day: "numeric", month: "long" });
      $("#topbar-sub").textContent = `${dayLabel} · ${nf.format(s.total)} registro(s)`;
      $("#csv-link").href = `/panel/api/journal/${state.day}.csv`;
      const select = $("#f-target");
      const targets = Object.keys(s.by_target).sort();
      if (state.target && !targets.includes(state.target)) targets.push(state.target);
      select.replaceChildren(h("option", { value: "" }, "Todas"), ...targets.map((t) => h("option", { value: t, selected: t === state.target }, t)));
      const rows = data.rows.map((row) => h("tr", {
        class: "clickable", tabindex: 0, "aria-selected": String(row.id === state.selected), dataset: { id: row.id },
        onclick: () => showDetail(row.id), onkeydown: (e) => { if (e.key === "Enter") showDetail(row.id); },
      },
        h("td", {}, fmtTime(row.ts)), h("td", {}, actionLabel(row)), h("td", {}, row.target_label || "—"),
        h("td", {}, originLabel(row.origin)), h("td", { class: "r" }, row.bytes ? nf.format(row.bytes) : "—"),
        h("td", { class: "r" }, fmtMs(row.duration_ms)), h("td", {}, resultPill(row))));
      const body = $("#journal-body");
      if (append) body.append(...rows);
      else body.replaceChildren(...(rows.length ? rows : [h("tr", {}, h("td", { colspan: 7, class: "empty" }, "No hay registros para este día con esos filtros."))]));
      $("#journal-more").hidden = data.rows.length < 200;
    };

    const showDetail = async (id) => {
      state.selected = id;
      $$("#journal-body tr").forEach((tr) => tr.setAttribute("aria-selected", String(Number(tr.dataset.id) === id)));
      let row;
      try { row = await api(`/panel/api/journal/${id}`); } catch (err) { toast(err.message, "error"); return; }
      const panel = $("#journal-detail");
      const [cls, label] = RESULT[row.result] || ["off", row.result];
      const children = [
        h("div", { class: "inline" }, h("h3", {}, `${actionLabel(row)} · ${fmtTime(row.ts)}`), h("span", { class: "pill " + cls }, label)),
        h("dl", { class: "kv" },
          h("dt", {}, "Destino"), h("dd", { class: "mono" }, row.target_label || "—"),
          h("dt", {}, "Origen"), h("dd", { class: "mono" }, row.origin || "—"),
          h("dt", {}, "Equipo"), h("dd", { class: "mono" }, row.client_ip || "—"),
          h("dt", {}, "Formato"), h("dd", {}, row.payload_kind === "image" ? "Imagen" : row.payload_kind === "raw" ? "ESC/POS directo" : "—", row.bytes ? ` · ${nf.format(row.bytes)} bytes` : ""),
          h("dt", {}, "Duración"), h("dd", {}, fmtMs(row.duration_ms)),
          row.job_id ? [h("dt", {}, "Id de trabajo"), h("dd", { class: "mono" }, row.job_id)] : null,
          row.test_mode ? [h("dt", {}, "Modo"), h("dd", {}, h("span", { class: "pill test" }, "Modo prueba"))] : null,
          h("dt", {}, "Mensaje"), h("dd", {}, row.message || "—")),
      ];
      if (row.payload) {
        children.push(h("div", {}, h("div", { class: "muted", style: "font-size:12px;margin-bottom:4px" }, "Bytes enviados"),
          h("pre", { class: "hexbox" }, row.payload.length > 1200 ? row.payload.slice(0, 1200).replace(/(..)/g, "$1 ") + "…" : row.payload.replace(/(..)/g, "$1 "))));
        if (row.preview_kind) children.push(h("details", {}, h("summary", {}, "Ver como ticket"), h("div", { class: "stage mt-s" }, renderPreview(row.preview_kind, row.preview))));
        children.push(h("div", { class: "inline" }, h("button", {
          class: "btn primary", type: "button", onclick: async (e) => {
            if (!(await confirmDialog("¿Reenviar a la impresora?", `Se imprimirá una copia en ${row.target_label}. Úsalo solo si confirmaron que no salió.`, "Reenviar"))) return;
            const data = await busy(e.currentTarget, () => api(`/panel/api/journal/${id}/reprint`, { method: "POST" }));
            if (data) { toast(data.message); load(); }
          },
        }, icon("i-play"), "Reenviar a la impresora")));
      } else if (row.kind === "print") {
        children.push(h("p", { class: "help" }, "No se guardaron los bytes de este trabajo (se cambia en Configuración › Bitácora)."));
      }
      panel.replaceChildren(...children);
    };

    $$("#f-result button").forEach((b) => b.addEventListener("click", () => {
      $$("#f-result button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      state.result = b.dataset.value; state.offset = 0; load();
    }));
    $("#f-day").addEventListener("change", (e) => { state.day = e.target.value || today(); state.offset = 0; load(); });
    $("#f-target").addEventListener("change", (e) => { state.target = e.target.value; state.offset = 0; load(); });
    $("#f-doc").addEventListener("change", (e) => { state.document_type = e.target.value; state.offset = 0; load(); });
    $("#f-q").addEventListener("input", (e) => { clearTimeout(debounce); debounce = setTimeout(() => { state.q = e.target.value.trim(); state.offset = 0; load(); }, 300); });
    $("#journal-filters").addEventListener("submit", (e) => e.preventDefault());
    $("#journal-more").addEventListener("click", () => { state.offset += 200; load(true); });
    poll(() => (state.day === today() && state.offset === 0 ? load() : Promise.resolve()), 5000);
  }

  // -- vista previa del ticket ---------------------------------------------------
  function renderPreview(kind, content) {
    if (kind === "image") return h("div", { class: "paper" }, h("img", { src: "data:image/jpeg;base64," + content, alt: "Imagen del recibo" }));
    if (kind === "zpl") return h("div", { class: "paper" }, h("pre", { class: "zpl" }, content));
    if (kind === "cashbox") return h("p", { class: "muted center" }, "Apertura de cajón: no imprime nada.");
    const paper = h("div", { class: "paper" });
    for (const line of content || []) {
      if (line.type === "text") {
        const el = h("div", { class: `ln ${line.align}${line.bold ? " b" : ""}` }, line.text || " ");
        if (line.w > 1 || line.h > 1) el.style.fontSize = `${12 * Math.max(line.w, line.h) * 0.9}px`;
        paper.append(el);
      } else if (line.type === "cut") {
        paper.append(h("div", { class: "cut" }, line.mode === "partial" ? "corte parcial" : "corte total"));
      } else if (line.type === "image") {
        paper.append(h("img", { src: "data:image/png;base64," + line.png_base64, alt: "Imagen del ticket" }));
      } else if (line.type === "unknown") {
        paper.append(h("div", { class: "unk", title: "Comando no interpretado" }, `[${line.hex}]`));
      }
    }
    return paper;
  }

  // -- Modo prueba ----------------------------------------------------------------
  function initPrueba() {
    const state = { target: null, job: null, settings: null };
    const loadSettings = async () => {
      state.settings = await api("/panel/api/settings");
      $("#sim-response").value = state.settings.test_response;
      $$("#sim-delay button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.value) === state.settings.test_delay_s)));
    };
    const saveSettings = async (body) => {
      try { await api("/panel/api/settings", { method: "POST", body }); toast("Guardado."); }
      catch (err) { toast(err.message, "error"); }
    };
    const loadJobs = async () => {
      let data;
      try { data = await api("/panel/api/test/jobs" + (state.target ? "?target=" + encodeURIComponent(state.target) : "")); }
      catch (err) { toast(err.message, "error"); return; }
      const targets = Object.entries(data.targets);
      const total = targets.reduce((n, [, c]) => n + c, 0);
      const vp = (label, count, value) => h("button", { class: "vp", type: "button", "aria-pressed": String(state.target === value),
        onclick: () => { state.target = value; loadJobs(); } },
        icon("i-printer", "i"), h("span", { class: "meta" }, h("b", {}, label), h("small", {}, `${count} trabajo(s)`)));
      $("#vprinters").replaceChildren(vp("Todas las impresoras", total, null), ...targets.map(([t, c]) => vp(t, c, t)));
      $("#sample-targets").replaceChildren(...targets.map(([t]) => h("option", { value: t })));
      $("#queue").replaceChildren(...(data.jobs.length ? data.jobs.map((j) => h("button", {
        type: "button", "aria-current": String(state.job === j.id), onclick: () => showJob(j.id),
      }, icon(j.kind === "cashbox" ? "i-cash" : j.document_type === "label" ? "i-tag" : "i-list"),
        h("span", {}, j.kind === "cashbox" ? "Apertura de cajón" : DOC[j.document_type] || j.document_type,
          h("small", { class: "num" }, `${fmtTime(j.ts)} · ${j.target_label}`)))) :
        [h("p", { class: "muted" }, "Aún no llega ningún trabajo. Imprime desde el POS o usa «Prueba sin POS».")]));
    };
    const showJob = async (id) => {
      state.job = id;
      let data;
      try { data = await api(`/panel/api/test/jobs/${id}/preview`); } catch (err) { toast(err.message, "error"); return; }
      $("#stage").replaceChildren(renderPreview(data.kind, data.lines || data.zpl_text || data.png_base64));
      $("#preview-meta").textContent = { escpos: "ESC/POS interpretado", zpl: "Etiqueta ZPL (texto)", image: "Imagen del recibo", cashbox: "Cajón" }[data.kind];
      $("#preview-hex").textContent = (data.hex || "").replace(/(..)/g, "$1 ");
      $("#preview-actions").hidden = false;
      $$("#queue button").forEach((b) => b.setAttribute("aria-current", "false"));
      loadJobs();
    };
    $("#sim-response").addEventListener("change", (e) => saveSettings({ test_response: e.target.value }));
    $$("#sim-delay button").forEach((b) => b.addEventListener("click", () => {
      $$("#sim-delay button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      saveSettings({ test_delay_s: Number(b.dataset.value) });
    }));
    $$("[data-sample]").forEach((b) => b.addEventListener("click", async () => {
      const value = $("#sample-target").value.trim() || "Impresora de prueba";
      const target = /^\d+\.\d+\.\d+\.\d+(:\d+)?$/.test(value)
        ? { ip_address: value.split(":")[0], ip_port: Number(value.split(":")[1] || 9100) } : { printer_name: value };
      const body = { sample: b.dataset.sample, target };
      if (!state.settings?.test_mode) {
        if (!(await confirmDialog("El modo prueba está apagado", `Esto imprimirá en papel en ${value}.`, "Imprimir"))) return;
        body.confirm = true;
      }
      const data = await busy(b, () => api("/panel/api/test/sample", { method: "POST", body }));
      if (data) { toast(data.message); loadJobs(); }
    }));
    $('[data-action="test-mode"]').addEventListener("click", async (e) => {
      const enable = e.currentTarget.dataset.enable === "true";
      if (!enable || await confirmDialog("¿Encender el modo prueba?", "Mientras esté encendido, nada de lo que mande el POS saldrá en papel y el cajón no se abrirá.", "Encender")) {
        await busy(e.currentTarget, () => api("/panel/api/settings", { method: "POST", body: { test_mode: enable } }));
        location.reload();
      }
    });
    $('[data-action="clear-jobs"]').addEventListener("click", async (e) => {
      await busy(e.currentTarget, () => api("/panel/api/test/clear", { method: "POST" }));
      state.job = null;
      $("#stage").replaceChildren(h("p", { class: "muted center" }, "Elige un trabajo para ver cómo habría salido."));
      $("#preview-actions").hidden = true;
      loadJobs();
    });
    $('[data-action="toggle-hex"]').addEventListener("click", () => { $("#preview-hex").hidden = !$("#preview-hex").hidden; });
    $('[data-action="forward"]').addEventListener("click", async (e) => {
      const value = prompt("Nombre de la impresora de Windows o IP de red a la que enviar este trabajo:");
      if (!value) return;
      const target = /^\d+\.\d+\.\d+\.\d+(:\d+)?$/.test(value.trim())
        ? { ip_address: value.trim().split(":")[0], ip_port: Number(value.trim().split(":")[1] || 9100) } : { printer_name: value.trim() };
      if (!(await confirmDialog("¿Enviar a impresora real?", `El trabajo saldrá en papel en ${value}.`, "Enviar"))) return;
      const data = await busy(e.currentTarget, () => api("/panel/api/test/forward", { method: "POST", body: { job_id: state.job, target } }));
      if (data) toast(data.message);
    });
    loadSettings().catch((err) => toast(err.message, "error"));
    poll(loadJobs, 5000);
  }

  // -- Configuración ---------------------------------------------------------------
  function initConfiguracion() {
    let current;
    let origins = [];
    const port = Number(document.body.dataset.port);
    const renderOrigins = () => $("#s-origins").replaceChildren(...origins.map((o, i) => h("span", { class: "chip" }, o,
      h("button", { type: "button", "aria-label": `Quitar ${o}`, onclick: () => { origins.splice(i, 1); renderOrigins(); } }, icon("i-x")))));
    const fill = (s) => {
      current = s;
      $("#s-port").value = s.port;
      $("#s-port-help").textContent = `El POS usa http://127.0.0.1:${s.port}`;
      $$("#s-paper button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.value) === s.default_paper_width_mm)));
      const key = $("#s-key-state");
      key.className = "pill " + (s.api_key_set ? "ok" : "off");
      key.textContent = s.api_key_set ? "Clave configurada" : "Sin clave (opcional)";
      origins = [...s.allowed_origins];
      renderOrigins();
      const p = s.allowed_printer_ports;
      const contiguous = p.length > 1 && p.every((v, i) => i === 0 || v === p[i - 1] + 1);
      $("#s-ports").value = contiguous ? `${p[0]}-${p[p.length - 1]}` : p.join(",");
      $("#s-remote").checked = s.remote_panel_enabled;
      $("#s-remote-text").textContent = s.remote_panel_enabled ? "Encendido" : "Apagado";
      $("#s-remote-help").textContent = s.remote_panel_enabled ? `Desde otra PC: http://<IP de esta caja>:${s.remote_panel_port}/panel` : "";
      $("#s-remote-port").value = s.remote_panel_port;
      $("#s-ruser").value = s.remote_user;
      $("#s-rpass-state").textContent = s.remote_password_set ? "Hay una clave guardada. Escribe una nueva solo si quieres cambiarla." : "Aún no hay clave.";
      $("#s-networks").value = s.remote_networks.join(", ");
      $("#s-retention").value = s.retention_days;
      $("#s-update").value = s.update_mode;
      $("#s-store").checked = s.store_payload;
      $("#s-store-img").checked = s.store_images;
    };
    const load = async () => {
      try {
        fill(await api("/panel/api/settings"));
        const status = await api("/panel/api/status");
        $("#s-journal-size").textContent = `${fmtBytes(status.journal.bytes)} · ${status.journal.days} día(s)`;
      } catch (err) { toast(err.message, "error"); }
    };
    $$("#s-paper button").forEach((b) => b.addEventListener("click", () => $$("#s-paper button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)))));
    $("#s-remote").addEventListener("change", (e) => { $("#s-remote-text").textContent = e.target.checked ? "Encendido" : "Apagado"; });
    $('[data-action="add-origin"]').addEventListener("click", () => {
      const value = $("#s-origin-new").value.trim();
      if (!/^https?:\/\/\S+$/.test(value)) { toast("Escribe el sitio completo, por ejemplo https://pos.midominio.cl", "error"); return; }
      if (!origins.includes(value)) origins.push(value);
      $("#s-origin-new").value = "";
      renderOrigins();
    });
    $("#settings-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const paper = $('#s-paper button[aria-pressed="true"]');
      const body = {
        port: Number($("#s-port").value), default_paper_width_mm: paper ? Number(paper.dataset.value) : current.default_paper_width_mm,
        allowed_origins: origins, allowed_printer_ports: $("#s-ports").value,
        remote_panel_enabled: $("#s-remote").checked, remote_panel_port: Number($("#s-remote-port").value),
        remote_networks: $("#s-networks").value.split(",").map((v) => v.trim()).filter(Boolean),
        retention_days: Number($("#s-retention").value), store_payload: $("#s-store").checked, store_images: $("#s-store-img").checked,
        update_mode: $("#s-update").value,
      };
      if (body.port !== current.port && !(await confirmDialog("¿Cambiar el puerto?", `El POS dejará de encontrar el puente hasta que actualices la URL en Odoo a http://127.0.0.1:${body.port}.`, "Cambiar"))) return;
      const data = await busy(document.querySelector('button[form="settings-form"]'), () => api("/panel/api/settings", { method: "POST", body }));
      if (!data) return;
      fill(data.settings);
      toast(data.restart_required ? "Guardado. El servicio se reinicia para aplicar el cambio." : "Cambios guardados.");
      if (data.firewall === "needs_admin") toast(`Para abrir el puerto en el firewall, ejecuta como administrador: ${data.firewall_command}`, "error");
      if (data.restart_required && data.settings.port !== port) setTimeout(() => { location.href = `http://127.0.0.1:${data.settings.port}/panel/configuracion`; }, 2500);
    });
    $('[data-action="generate-key"]').addEventListener("click", async (e) => {
      if (current?.api_key_set && !(await confirmDialog("¿Generar una clave nueva?", "El POS dejará de poder imprimir hasta que pongas la clave nueva en Odoo.", "Generar"))) return;
      const data = await busy(e.currentTarget, () => api("/panel/api/settings/api-key/generate", { method: "POST" }));
      if (!data) return;
      $("#s-key-new").replaceChildren("Clave nueva (cópiala ahora, no se vuelve a mostrar): ", h("b", { class: "mono" }, data.api_key));
      load();
    });
    $('[data-action="save-remote-password"]').addEventListener("click", async (e) => {
      const data = await busy(e.currentTarget, () => api("/panel/api/settings/remote-password", { method: "POST", body: { user: $("#s-ruser").value, password: $("#s-rpass").value } }));
      if (data) { $("#s-rpass").value = ""; toast("Usuario y clave del panel guardados."); load(); }
    });
    $('[data-action="purge-all"]').addEventListener("click", async (e) => {
      if (!(await confirmDialog("¿Borrar toda la bitácora?", "Se eliminan todos los registros guardados. No se puede deshacer.", "Borrar"))) return;
      const data = await busy(e.currentTarget, () => api("/panel/api/journal/purge-all", { method: "POST" }));
      if (data) { toast(data.message); load(); }
    });
    load();
  }

  const inits = { estado: initEstado, impresoras: initImpresoras, bitacora: initBitacora, prueba: initPrueba, configuracion: initConfiguracion };
  inits[PAGE]?.();
})();
