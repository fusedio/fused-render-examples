// @ts-check
/// <reference path="./types/fused.d.ts" />
/*
 * Client logic for the Claude config editor. Loaded as a sibling module by the
 * bootstrap in index.html (architecture.md §3a) — NOT via a relative <script
 * src>, which 404s under fused-render's /render route.
 *
 * `// @ts-check` above + types/fused.d.ts make this file checkable by
 * `tsc --checkJs` and by editors live. Two typed DOM helpers (qa/gid) narrow
 * querySelector results — the runtime elements are inputs/selects/buttons, and
 * casting to HTMLInputElement (a superset of the props we touch) keeps tsc
 * honest about real bugs without drowning in Element-property false positives.
 */

/** All matching elements, typed for the input-ish props this app reads.
 * @param {ParentNode} root @param {string} sel @returns {HTMLInputElement[]} */
function qa(root, sel) { return Array.from(root.querySelectorAll(sel)); }
/** getElementById typed for property access (value/checked/onclick/…).
 * @param {string} id @returns {HTMLInputElement} */
function gid(id) { return /** @type {any} */ (document.getElementById(id)); }

const TABS = [
  { id: "preferences", label: "Preferences", py: "./preferences.py", file: "settings.json" },
  { id: "plugins",     label: "Plugins",     py: "./plugins.py",     file: "settings.json → enabledPlugins" },
  { id: "marketplaces",label: "Marketplaces",py: "./marketplaces.py", file: "settings.json → extraKnownMarketplaces" },
  { id: "memory",      label: "Memory",      py: "./memory.py",      file: "projects/*/memory/ (read-only viewer)" },
  { id: "skills",      label: "Skills",      py: "./skills.py",      file: "skills/*/SKILL.md (read-only viewer)" },
  { id: "statusline",  label: "Statusline",  py: "./statusline.py",  file: "settings.json → statusLine (read-only viewer)" },
  { id: "profiles",    label: "Profiles",    py: "./profiles.py",    file: "git branches over your Claude config" },
  { id: "mcp",         label: "MCP",         py: "./mcp.py",         file: "global MCP servers via the `claude mcp` CLI (not version-controlled)" },
  { id: "history",     label: "History",     py: "./git_ops.py",     file: "git log over your Claude config" },
];

const main = gid("main");
const navEl = gid("nav");
const toastEl = gid("toast");

/** @param {*} s @returns {string} */
function esc(s) {
  return (s == null ? "" : String(s)).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] || c));
}
function currentTab() { return fused.params.get("tab") || "preferences"; }

let toastTimer = 0;
/** @param {string} msg @param {boolean} [isErr] */
function toast(msg, isErr) {
  toastEl.textContent = msg;
  toastEl.classList.toggle("err", !!isErr);
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2600);
}
/** @param {string} py @param {Record<string,string>} [params] */
async function run(py, params) {
  try { return await fused.runPython(py, params || {}); }
  catch (/** @type {any} */ err) { toast(`${err.type || "error"}: ${err.message}`, true); throw err; }
}
/** @param {string} text @param {string} [label] */
async function copy(text, label) {
  try { await navigator.clipboard.writeText(text); toast(`Copied ${label || ""}`.trim()); }
  catch { toast("Copy failed", true); }
}

/* ---- modal (in-app, scrollable) ---- */
const overlay = gid("overlay");
/** @type {((v:any)=>void)|null} — resolver for the currently-open modal. */
let overlayResolve = null;
function closeModal() { overlay.classList.remove("open"); if (overlayResolve) overlayResolve(false); overlayResolve = null; }
gid("modalClose").onclick = closeModal;
overlay.onclick = (e) => { if (e.target === overlay) closeModal(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && overlay.classList.contains("open")) closeModal(); });

/** Render a change-preview {files, settings}; buttons = [{label, primary?, value}].
 * Returns a promise of the clicked button's value (or false on dismiss).
 * @param {string} title
 * @param {{files?:{status:string,path:string}[], settings?:{key:string,from:*,to:*}[], note?:string}} preview
 * @param {{label:string, primary?:boolean, value:*}[]} buttons */
function showPreview(title, preview, buttons) {
  gid("modalTitle").textContent = title;
  const files = (preview.files || []).map((f) =>
    `<div class="delta"><span class="fstat ${esc(f.status)}">${esc(f.status)}</span> <span class="mono">${esc(f.path)}</span></div>`).join("");
  const deltas = (preview.settings || []).map((d) =>
    `<div class="delta"><span class="k">${esc(d.key)}</span> <span class="fromto">${esc(JSON.stringify(d.from))} → ${esc(JSON.stringify(d.to))}</span></div>`).join("");
  const body = gid("modalBody");
  const note = preview.note ? `<p style="color:var(--text);font-size:13px;line-height:1.5;margin:0">${esc(preview.note)}</p>` : "";
  body.innerHTML = (files || deltas)
    ? (deltas ? `<h3 style="font-size:12px;color:var(--muted);margin:0 0 6px">Settings changes</h3>${deltas}` : "")
      + (files ? `<h3 style="font-size:12px;color:var(--muted);margin:14px 0 6px">Files</h3>${files}` : "")
    : (note || `<div class="empty">No changes.</div>`);
  const foot = gid("modalFoot");
  foot.innerHTML = "";
  return new Promise((resolve) => {
    overlayResolve = resolve;
    for (const b of buttons) {
      const btn = document.createElement("button");
      btn.className = "btn" + (b.primary ? " primary" : "");
      btn.textContent = b.label;
      btn.onclick = () => { overlay.classList.remove("open"); overlayResolve = null; resolve(b.value); };
      foot.appendChild(btn);
    }
    overlay.classList.add("open");
  });
}

/** Read a File as base64 (strip the data: prefix) for passing to runPython.
 * @param {File} file @returns {Promise<string>} */
function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(new Error("could not read file"));
    r.readAsDataURL(file);
  });
}

/** profiles.md §7 import picker: reuse the modal chrome with a custom body — a
 * new-branch name field plus a checkbox per file, bucketed by top-level folder
 * with a parent checkbox per group (and a master toggle) so a whole group can be
 * (de)selected at once. Resolves {branch, paths} or false on dismiss.
 * @param {{path:string, isDir?:boolean}[]} entries */
function importPicker(entries) {
  gid("modalTitle").textContent = "Import into a new profile";
  const files = entries.filter((e) => !e.isDir);
  // Bucket by top-level folder, preserving first-seen order; root-level files
  // (no "/") share a "Root files" group keyed by "".
  const order = [], /** @type {Record<string, typeof files>} */ byKey = {};
  for (const f of files) {
    const key = f.path.includes("/") ? f.path.slice(0, f.path.indexOf("/")) : "";
    if (!byKey[key]) { byKey[key] = []; order.push(key); }
    byKey[key].push(f);
  }
  const groupsHtml = order.map((key) => {
    const label = key === "" ? "Root files" : key + "/";
    const rows = byKey[key].map((f) =>
      `<label class="delta" style="cursor:pointer;display:flex;gap:8px;align-items:center;padding-left:24px">
         <input type="checkbox" class="ipick" data-group="${esc(key)}" data-path="${esc(f.path)}" checked>
         <span class="mono">${esc(f.path)}</span></label>`).join("");
    return `<label class="delta" style="cursor:pointer;display:flex;gap:8px;align-items:center;margin-top:8px">
         <input type="checkbox" class="ipgroup" data-group="${esc(key)}" checked>
         <span style="font-weight:600">${esc(label)}</span></label>${rows}`;
  }).join("");
  const body = gid("modalBody");
  body.innerHTML =
    `<label style="display:block;margin-bottom:12px">
       <div style="font-size:12px;color:var(--muted);margin-bottom:4px">New profile name</div>
       <input type="text" id="ipBranch" placeholder="e.g. imported" style="width:100%"></label>
     <label class="delta" style="cursor:pointer;display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--border)">
       <input type="checkbox" id="ipAll" checked> <span style="color:var(--muted)">Select all</span></label>
     ${groupsHtml || '<div class="empty">Empty archive.</div>'}`;
  const all = gid("ipAll");
  const groupCbs = qa(body, ".ipgroup");
  const picks = () => qa(body, ".ipick");
  /** @param {string} key */
  const childrenOf = (key) => picks().filter((c) => c.dataset.group === key);
  /** @param {string} key */
  const syncGroup = (key) => {
    const kids = childrenOf(key), on = kids.filter((c) => c.checked).length;
    const g = groupCbs.find((c) => c.dataset.group === key);
    if (g) { g.checked = on === kids.length; g.indeterminate = on > 0 && on < kids.length; }
  };
  const syncAll = () => {
    const cs = picks(), on = cs.filter((c) => c.checked).length;
    all.checked = on === cs.length; all.indeterminate = on > 0 && on < cs.length;
  };
  all.onchange = () => { picks().forEach((c) => (c.checked = all.checked)); groupCbs.forEach((g) => { g.checked = all.checked; g.indeterminate = false; }); };
  groupCbs.forEach((g) => g.onchange = () => { childrenOf(g.dataset.group || "").forEach((c) => (c.checked = g.checked)); g.indeterminate = false; syncAll(); });
  picks().forEach((c) => c.onchange = () => { syncGroup(c.dataset.group || ""); syncAll(); });
  const foot = gid("modalFoot");
  foot.innerHTML = "";
  return new Promise((resolve) => {
    overlayResolve = resolve;
    /** @param {string} label @param {boolean} primary @param {()=>void} fn */
    const mk = (label, primary, fn) => {
      const b = document.createElement("button");
      b.className = "btn" + (primary ? " primary" : "");
      b.textContent = label; b.onclick = fn; foot.appendChild(b);
    };
    mk("Cancel", false, () => { overlay.classList.remove("open"); overlayResolve = null; resolve(false); });
    mk("Import", true, () => {
      const branch = gid("ipBranch").value.trim();
      const paths = picks().filter((c) => c.checked).map((c) => c.dataset.path);
      if (!branch) return toast("new profile name required", true);
      if (!paths.length) return toast("select at least one file", true);
      overlay.classList.remove("open"); overlayResolve = null;
      resolve({ branch, paths });
    });
    overlay.classList.add("open");
  });
}

/* ---- status badge ---- */
const badge = gid("badge");
async function refreshBadge() {
  try {
    const st = await run("./git_ops.py", { action: "status" });
    badge.className = st.dirty ? "dirty" : "clean";
    badge.textContent = st.dirty ? `${st.files.length} uncommitted change(s)` : "✓ all changes committed";
  } catch { badge.textContent = "status unavailable"; }
}
badge.onclick = async () => {
  if (!badge.classList.contains("dirty")) return refreshBadge();
  const drift = await run("./git_ops.py", { action: "drift" });
  const choice = await showPreview("Uncommitted changes", drift,
    [{ label: "Close", value: false }, { label: "Commit", primary: true, value: "commit" }]);
  if (choice === "commit") {
    await run("./git_ops.py", { action: "commit" });
    toast("Committed"); refreshBadge();
    if (currentTab() === "history") render();
  }
};

/* ---- nav ---- */
function renderNav() {
  navEl.innerHTML = TABS.map((t) =>
    `<div class="nav-item ${t.id === currentTab() ? "active" : ""}" data-tab="${t.id}">${esc(t.label)}</div>`).join("");
  qa(navEl, ".nav-item").forEach((el) =>
    el.onclick = () => fused.params.set("tab", el.dataset.tab || ""));
}

/* ================= sections ================= */

async function renderPreferences() {
  const { schema, prefs } = await run("./preferences.py", { action: "get" });
  /** @type {Record<string, any[]>} */
  const groups = {};
  for (const d of schema) (groups[d.group] ??= []).push(d);
  let html = `<div style="display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-bottom:18px">`
    + `<span class="unset" style="margin-right:auto">${schema.length} settings from the checked-in catalog</span>`
    + `<button class="btn" id="refreshCatalog" title="Re-fetch defaults & docs from code.claude.com and rewrite settings_catalog.json">↻ Refresh catalog</button></div>`;
  for (const [group, items] of Object.entries(groups)) {
    html += `<div class="group"><h3>${esc(group)}</h3>`;
    for (const d of items) {
      const val = prefs[d.key];
      const isSet = val !== null && val !== undefined;
      const unsetLabel = d.unsetLabel || (d.default !== null && d.default !== undefined ? `default: ${JSON.stringify(d.default)}` : "Claude default");
      let control;
      if (d.control === "toggle") {
        // Unset renders as a third (indeterminate) state, not off — else Claude
        // defaults read as an explicit `false` (preferences.md §4). Set post-render.
        control = `<span class="switch"><input type="checkbox" data-key="${esc(d.key)}"${isSet ? "" : " data-unset=\"1\""} ${isSet && !!val ? "checked" : ""}><span class="slider"></span></span>`;
      } else if (d.control === "select") {
        control = `<select data-key="${esc(d.key)}"><option value="">— ${esc(unsetLabel)} —</option>` +
          (d.options || []).map((o) => `<option value="${esc(o)}" ${val === o ? "selected" : ""}>${esc(o)}</option>`).join("") + `</select>`;
      } else if (d.control === "number") {
        control = `<input type="number" data-key="${esc(d.key)}" value="${isSet ? esc(val) : ""}" placeholder="${esc(d.default ?? "")}">`;
      } else {
        control = `<input type="text" data-key="${esc(d.key)}" value="${isSet ? esc(val) : ""}" placeholder="${esc(unsetLabel)}">`;
      }
      const reset = isSet ? `<button class="reset" data-reset="${esc(d.key)}" title="Reset to default">reset</button>` : `<span class="unset">${esc(unsetLabel)}</span>`;
      html += `<div class="row"><div class="meta"><div class="label">${esc(d.label)}</div>${d.doc ? `<div class="doc">${esc(d.doc)}</div>` : ""}</div><div class="control">${reset}${control}</div></div>`;
    }
    html += `</div>`;
  }
  main.innerHTML = html;

  /** @param {string} key @param {*} value */
  async function patch(key, value) {
    try {
      await run("./preferences.py", { action: "patch", payload: JSON.stringify({ [key]: value }) });
      toast("Saved"); refreshBadge(); render();
    } catch { render(); }  // run() already toasted; re-render restores the true on-disk value
  }
  qa(main, 'input[data-unset]').forEach((el) => { el.indeterminate = true; });
  qa(main, "[data-key]").forEach((el) => {
    const key = el.dataset.key || "";
    if (el.type === "checkbox") el.onchange = () => patch(key, el.checked);
    else if (el.tagName === "SELECT") el.onchange = () => patch(key, el.value === "" ? null : el.value);
    else el.onchange = () => {
      const v = el.value.trim();
      if (v === "") return patch(key, null);
      patch(key, el.type === "number" ? Number(v) : v);
    };
  });
  qa(main, "[data-reset]").forEach((el) => el.onclick = () => patch(el.dataset.reset || "", null));

  const refreshBtn = gid("refreshCatalog");
  refreshBtn.onclick = async () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "Refreshing…";
    try {
      const res = await run("./refresh_catalog.py", {});
      if (!res.ok) { toast(res.error, true); return; }
      const extra = res.undocumented.length ? ` · ${res.undocumented.length} undocumented` : "";
      toast(`Catalog refreshed: ${res.updated}/${res.total} entries${extra}`);
      render();  // re-read the rewritten catalog
    } finally {
      // render() rebuilds this button on success; only reached if we bailed early
      refreshBtn.disabled = false;
      refreshBtn.textContent = "↻ Refresh catalog";
    }
  };
}

async function renderPlugins() {
  const { plugins } = await run("./plugins.py", { action: "list" });
  if (!plugins.length) return void (main.innerHTML = `<div class="empty">No plugins enabled or installed.</div>`);
  /** @type {Record<string, any[]>} */
  const byMkt = {};
  for (const p of plugins) (byMkt[p.marketplace] ??= []).push(p);
  let html = "";
  for (const [mkt, list] of Object.entries(byMkt)) {
    html += `<div class="group"><h3>${esc(mkt)}</h3>`;
    for (const p of list) {
      html += `<div class="card"><div style="display:flex;align-items:center;gap:10px">
        <span class="switch"><input type="checkbox" data-toggle="${esc(p.id)}" ${p.enabled ? "checked" : ""}><span class="slider"></span></span>
        <div style="flex:1"><div class="title">${esc(p.name)} ${p.enabled ? '<span class="pill on">enabled</span>' : ""} ${p.installed ? "" : '<span class="pill">not installed</span>'}</div>
        <div class="sub mono">${esc(p.id)}${p.version ? " · v" + esc(p.version) : ""}</div></div></div>
        <div class="actions">
          ${p.installed ? `<button class="btn" data-update="${esc(p.id)}">Update</button>` : ""}
          <button class="btn" data-share="${esc(p.shareCommand)}" title="${esc(p.shareCommand)}">Copy install command</button>
        </div></div>`;
    }
    html += `</div>`;
  }
  main.innerHTML = html;
  qa(main, "[data-toggle]").forEach((el) => el.onchange = async () => {
    try {
      await run("./plugins.py", { action: "toggle", id: el.dataset.toggle || "", enabled: el.checked ? "true" : "false" });
      toast(el.checked ? "Enabled" : "Disabled"); refreshBadge();
    } catch { el.checked = !el.checked; }  // run() already toasted; revert the visual flip
  });
  qa(main, "[data-update]").forEach((el) => el.onclick = async () => {
    el.disabled = true; el.textContent = "Updating…";
    const res = await run("./plugins.py", { action: "update", id: el.dataset.update || "" });
    toast(res.ok ? "Updated" : (res.error || "Update failed"), !res.ok);
    el.disabled = false; el.textContent = "Update";
  });
  qa(main, "[data-share]").forEach((el) => el.onclick = () => copy(el.dataset.share || "", "install command"));
}

async function renderMarketplaces() {
  const { marketplaces } = await run("./marketplaces.py", { action: "list" });
  let html = `<div class="card"><div class="title">Add a marketplace</div>
    <div class="actions" style="margin-top:10px">
      <input type="text" id="mpName" placeholder="name">
      <select id="mpKind"><option value="github">github (owner/repo)</option><option value="git">git url</option></select>
      <input type="text" id="mpValue" placeholder="owner/repo or url">
      <button class="btn primary" id="mpAdd">Add</button>
    </div></div>`;
  for (const m of marketplaces) {
    const ref = m.source.repo || m.source.url || "";
    html += `<div class="card"><div class="title">${esc(m.name)} ${m.editable ? "" : '<span class="pill ro">read-only</span>'}</div>
      <div class="sub mono">${esc(ref)}</div>
      <div class="actions">
        ${m.shareCommand ? `<button class="btn" data-share="${esc(m.shareCommand)}" title="${esc(m.shareCommand)}">Copy add command</button>` : ""}
        ${m.editable ? `<button class="btn danger" data-remove="${esc(m.name)}">Remove</button>` : ""}
      </div></div>`;
  }
  main.innerHTML = html;
  gid("mpAdd").onclick = async () => {
    const name = gid("mpName").value.trim();
    const kind = gid("mpKind").value;
    const value = gid("mpValue").value.trim();
    if (!name || !value) return toast("name and value required", true);
    const res = await run("./marketplaces.py", { action: "add", name, kind, value });
    if (res.ok) { toast("Added"); refreshBadge(); render(); } else toast(res.error, true);
  };
  qa(main, "[data-share]").forEach((el) => el.onclick = () => copy(el.dataset.share || "", "add command"));
  qa(main, "[data-remove]").forEach((el) => el.onclick = async () => {
    const res = await run("./marketplaces.py", { action: "remove", name: el.dataset.remove || "" });
    if (res.ok) { toast("Removed"); refreshBadge(); render(); } else toast(res.error, true);
  });
}

async function renderMemory() {
  const { projects } = await run("./memory.py", { action: "list" });
  if (!projects.length) return void (main.innerHTML = `<div class="empty">No persistent memory found under projects/*/memory/.</div>`);
  let html = "";
  for (const p of projects) {
    const dirty = p.changes.length;
    const n = p.files.length;
    html += `<div class="card"><div class="title mono">${esc(p.project)} <span class="count-badge">${n} file${n === 1 ? "" : "s"}</span> ${dirty ? `<span class="change-badge">${dirty} uncommitted</span>` : ""}</div>
      <div class="sub">${p.files.map(esc).join(" · ")}</div>
      <div class="actions">
        <button class="btn" data-open="${esc(p.project)}">Reveal in explorer</button>
        <button class="btn" data-commit="${esc(p.project)}" ${dirty ? "" : "disabled"}>Commit</button>
        <button class="btn danger" data-clear="${esc(p.project)}">Clear</button>
      </div></div>`;
  }
  main.innerHTML = html;
  qa(main, "[data-open]").forEach((el) => el.onclick = () => run("./memory.py", { action: "open", project: el.dataset.open || "" }));
  qa(main, "[data-commit]").forEach((el) => el.onclick = async () => {
    await run("./memory.py", { action: "commit", project: el.dataset.commit || "" }); toast("Committed"); refreshBadge(); render();
  });
  qa(main, "[data-clear]").forEach((el) => el.onclick = async () => {
    const ok = await showPreview(`Clear memory for ${el.dataset.clear}?`,
      { files: [], settings: [] },
      [{ label: "Cancel", value: false }, { label: "Clear", primary: true, value: true }]);
    if (!ok) return;
    await run("./memory.py", { action: "clear", project: el.dataset.clear || "" }); toast("Cleared"); refreshBadge(); render();
  });
}

async function renderSkills() {
  const { skills } = await run("./skills.py", { action: "list" });
  if (!skills.length) return void (main.innerHTML = `<div class="empty">No local skills under skills/*/SKILL.md.</div>`);
  main.innerHTML = skills.map((s) =>
    `<div class="card"><div class="title">${esc(s.name)} ${s.linked ? '<span class="pill">linked</span>' : ""}</div>
      <div class="sub">${esc(s.description)}</div>
      <div class="actions">
        <button class="btn" data-open="${esc(s.slug)}">Reveal in explorer</button>
        ${s.shareCommand ? `<button class="btn" data-share="${esc(s.shareCommand)}" title="${esc(s.shareCommand)}">Copy install command</button>` : '<span class="unset">not shareable (no recorded source)</span>'}
      </div></div>`).join("");
  qa(main, "[data-open]").forEach((el) => el.onclick = () => run("./skills.py", { action: "open", slug: el.dataset.open || "" }));
  qa(main, "[data-share]").forEach((el) => el.onclick = () => copy(el.dataset.share || "", "install command"));
}

/* Minimal ANSI SGR renderer (statusline.md §6): reset/bold/dim + basic foreground
   colors, so a preview matches the terminal. Unknown codes are ignored. Returns
   an escaped HTML string. */
/** @type {Record<number, string>} */
const ANSI_FG = {
  30: "#666", 31: "#e5534b", 32: "#3fb950", 33: "#c9a227",
  34: "#4a90d9", 35: "#c678dd", 36: "#2aa198", 37: "#bbb",
  90: "#888", 91: "#f87171", 92: "#4ade80", 93: "#eab308",
  94: "#60a5fa", 95: "#c084fc", 96: "#22d3ee", 97: "#eee",
};
/** @param {string} input @returns {string} */
function renderAnsi(input) {
  /** @type {{color?:string, bold?:boolean, dim?:boolean}} */
  let style = {}; let out = "";
  /** @param {typeof style} s */
  const css = (s) => [
    s.color ? `color:${s.color}` : "",
    s.bold ? "font-weight:700" : "",
    s.dim ? "opacity:0.6" : "",
  ].filter(Boolean).join(";");
  for (const part of input.split(/(\x1b\[[0-9;]*m)/)) {
    if (part === "") continue;
    const m = part.match(/^\x1b\[([0-9;]*)m$/);
    if (m) {
      const codes = m[1] === "" ? [0] : m[1].split(";").map(Number);
      for (const code of codes) {
        if (code === 0) style = {};
        else if (code === 1) style = { ...style, bold: true };
        else if (code === 2) style = { ...style, dim: true };
        else if (code === 22) style = { ...style, bold: false, dim: false };
        else if (code === 39) style = { ...style, color: undefined };
        else if (ANSI_FG[code]) style = { ...style, color: ANSI_FG[code] };
      }
      continue;
    }
    out += `<span style="${css(style)}">${esc(part)}</span>`;
  }
  return out;
}

async function renderStatusline() {
  const data = await run("./statusline.py", { action: "get" });
  if (!data.configured) return void (main.innerHTML = `<div class="empty">No status line configured.</div>`);
  const sc = data.script;
  let html = `<div class="card"><div class="title">Status line</div>
    <div class="sub mono">${esc(data.command)}</div>`;
  if (sc) {
    if (sc.description) html += `<div class="sub">${esc(sc.description)}</div>`;
    html += `<div class="sub">${sc.tracked ? "tracked ✓" : "not tracked"} · ${sc.size} bytes · modified ${new Date(sc.modified).toLocaleString()}</div>`;
    if (sc.fields.length) html += `<div class="sub"><strong>Shows:</strong> ${sc.fields.map(esc).join(" · ")}</div>`;
    else html += `<div class="sub">Couldn't introspect this command's fields.</div>`;
    if (sc.otherFields.length) html += `<div class="sub">Also reads: ${sc.otherFields.map(esc).join(", ")}</div>`;
  } else {
    html += `<div class="sub">This command doesn't point at a local script we can read — showing the command only.</div>`;
  }
  html += `<div class="actions"><button class="btn" id="slRun">Re-run preview</button></div>
    <pre id="slPreview" class="mono" style="margin:12px 0 0;padding:10px 12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;white-space:pre-wrap;word-break:break-word;overflow-x:auto">Running…</pre>`;
  if (sc) html += `<div class="sub" style="margin-top:10px">Script: <span class="mono">${esc(sc.path)}</span></div>`;
  html += `</div>`;
  main.innerHTML = html;

  const previewEl = gid("slPreview");
  const runBtn = gid("slRun");
  async function preview() {
    runBtn.disabled = true; previewEl.textContent = "Running…";
    try {
      const res = await run("./statusline.py", { action: "preview" });
      if (res.ok) previewEl.innerHTML = renderAnsi(res.output) || "(empty output)";
      else previewEl.textContent = `Preview failed: ${res.error}`;
    } finally { runBtn.disabled = false; }
  }
  runBtn.onclick = preview;
  preview();
}

async function renderProfiles() {
  const { profiles, current } = await run("./profiles.py", { action: "list" });
  let html = `<div class="card"><div class="title">New profile</div>
    <div class="sub">Forks the current profile (<span class="mono">${esc(current)}</span>) and switches into it.</div>
    <div class="actions" style="margin-top:10px">
      <input type="text" id="pfName" placeholder="e.g. work, experiment">
      <button class="btn primary" id="pfCreate">Create &amp; switch</button>
    </div></div>
    <div class="card"><div class="title">Import profile</div>
    <div class="sub">Pick files/folders from an exported <span class="mono">.zip</span> to overlay onto a new profile. Your current profile is untouched.</div>
    <div class="actions" style="margin-top:10px">
      <input type="file" id="pfZip" accept=".zip">
    </div></div>`;
  for (const p of profiles) {
    html += `<div class="card"><div class="title">${esc(p.name)}
      ${p.current ? '<span class="pill on">current</span>' : ""}
      ${p.isDefault ? '<span class="pill">default</span>' : ""}</div>
      <div class="actions">
        ${!p.current ? `<button class="btn" data-switch="${esc(p.name)}">Switch</button>` : ""}
        <button class="btn" data-export="${esc(p.name)}">Export .zip</button>
        ${!p.current && !p.isDefault ? `<button class="btn danger" data-delete="${esc(p.name)}">Delete</button>` : ""}
      </div></div>`;
  }
  main.innerHTML = html;

  // Switch into `target`, first previewing the change and — if the tree is dirty —
  // offering to commit drift to the current profile first (profiles.md §4). On
  // success reload so every tab re-reads the swapped-in config.
  /** @param {string} target @returns {Promise<boolean>} */
  async function switchInto(target) {
    const preview = await run("./git_ops.py", { action: "diff", target });
    if (preview.error) { toast(preview.error, true); return false; }
    const ok = await showPreview(`Switch to "${target}"?`, preview,
      [{ label: "Cancel", value: false }, { label: "Switch", primary: true, value: true }]);
    if (!ok) return false;
    let res = await run("./profiles.py", { action: "switch", name: target });
    if (!res.ok && res.dirty) {
      const commitFirst = await showPreview(
        "Uncommitted changes",
        { files: res.files.map((/** @type {string} */ f) => ({ status: "M", path: f })), settings: [] },
        [{ label: "Cancel", value: false }, { label: "Commit & switch", primary: true, value: true }]);
      if (!commitFirst) return false;
      res = await run("./profiles.py", { action: "switch", name: target, message: `Save before switching to ${target}` });
    }
    if (!res.ok) { toast(res.error || "Switch failed", true); return false; }
    location.reload();
    return true;
  }

  gid("pfCreate").onclick = async () => {
    const name = gid("pfName").value.trim();
    if (!name) return toast("name required", true);
    const res = await run("./profiles.py", { action: "create", name });
    if (!res.ok) return toast(res.error, true);
    // Branch exists now; if the user cancels the switch it still shows in the list.
    const switched = await switchInto(name);
    if (!switched) { refreshBadge(); render(); }
  };
  // profiles.md §7: import a .zip. Read it to base64, list its members, let the
  // user pick files + a new branch name, then overlay onto that new branch —
  // dirty-guarded like a switch (commit current drift first if needed).
  gid("pfZip").onchange = async (e) => {
    const input = /** @type {HTMLInputElement} */ (e.target);
    const file = input.files && input.files[0];
    input.value = "";  // let re-selecting the same file fire change again
    if (!file) return;
    const b64 = await fileToB64(file);
    const info = await run("./profiles.py", { action: "inspect", b64 });
    if (!info.ok) return toast(info.error, true);
    const choice = await importPicker(info.entries);
    if (!choice) return;
    /** @param {string} [message] */
    const call = (message) => run("./profiles.py",
      { action: "import", b64, branch: choice.branch, paths: JSON.stringify(choice.paths), ...(message ? { message } : {}) });
    let res = await call();
    if (!res.ok && res.dirty) {
      const commitFirst = await showPreview(
        "Uncommitted changes",
        { files: res.files.map((/** @type {string} */ f) => ({ status: "M", path: f })), settings: [] },
        [{ label: "Cancel", value: false }, { label: "Commit & import", primary: true, value: true }]);
      if (!commitFirst) return;
      res = await call(`Save before importing into ${choice.branch}`);
    }
    if (!res.ok) return toast(res.error || "Import failed", true);
    toast(`Imported ${res.imported.length} file(s) into ${res.branch}`);
    location.reload();
  };
  qa(main, "[data-switch]").forEach((el) => el.onclick = () => switchInto(el.dataset.switch || ""));
  // profiles.md §6: export the branch as a .zip. Python base64's the archive; we
  // decode to a Blob and download via a transient object-URL anchor — the page
  // stamps the date (main() has no wall clock).
  qa(main, "[data-export]").forEach((el) => el.onclick = async () => {
    const res = await run("./profiles.py", { action: "export", name: el.dataset.export || "" });
    if (!res.ok) return toast(res.error || "Export failed", true);
    const bytes = Uint8Array.from(atob(res.b64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/zip" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${res.filename}-${new Date().toISOString().slice(0, 10)}.zip`;
    a.click();
    URL.revokeObjectURL(url);
    toast(`Exported ${res.filename}`);
  });
  qa(main, "[data-delete]").forEach((el) => el.onclick = async () => {
    const ok = await showPreview(`Delete profile "${el.dataset.delete}"?`,
      { files: [], settings: [] },
      [{ label: "Cancel", value: false }, { label: "Delete", primary: true, value: true }]);
    if (!ok) return;
    const res = await run("./profiles.py", { action: "delete", name: el.dataset.delete || "" });
    if (res.ok) { toast(`Deleted ${res.name}`); render(); } else toast(res.error, true);
  });
}

async function renderHistory() {
  const { log } = await run("./git_ops.py", { action: "log" });
  if (!log.length) return void (main.innerHTML = `<div class="empty">No history yet.</div>`);
  main.innerHTML = log.map((/** @type {*} */ e, /** @type {number} */ i) =>
    `<div class="log-entry"><span class="msg">${esc(e.message)}</span>
      <span class="date">${new Date(e.date).toLocaleString()}</span>
      <span class="sha mono">${esc(e.sha.slice(0, 8))}</span>
      ${i === 0 ? '<span class="pill on">current</span>' : `<button class="btn" data-restore="${esc(e.sha)}">Restore</button>`}
    </div>`).join("");
  qa(main, "[data-restore]").forEach((el) => el.onclick = async () => {
    const sha = el.dataset.restore || "";
    const preview = await run("./git_ops.py", { action: "diff", target: sha });
    if (preview.error) return toast(preview.error, true);
    const ok = await showPreview(`Restore to ${sha.slice(0, 8)}?`, preview,
      [{ label: "Cancel", value: false }, { label: "Restore", primary: true, value: true }]);
    if (!ok) return;
    const res = await run("./git_ops.py", { action: "restore", target: sha });
    if (res.ok) { toast("Restored"); refreshBadge(); render(); } else toast(res.error, true);
  });
}

// mcp.md §5: list global MCP servers (with health + auth status) and act on them by
// delegating to `claude mcp` — never touching ~/.claude.json. Auth is fire-and-forget
// (mcp.md §3): launch a detached `mcp login`, tell the user to finish in the browser,
// then Refresh to see the status flip.
/** @type {Record<string, {label:string, cls:string}>} */
const MCP_STATUS = {
  connected:    { label: "connected",     cls: "on" },
  "needs-auth": { label: "needs auth",     cls: "ro" },
  failed:       { label: "failed",         cls: "err" },
  pending:      { label: "pending approval", cls: "" },
  unknown:      { label: "unknown",        cls: "" },
};
const MCP_GROUPS = [
  ["user", "Your servers"],
  ["connector", "claude.ai connectors"],
  ["plugin", "Plugin-provided (read-only)"],
];
async function renderMcp() {
  const res = await run("./mcp.py", { action: "list" });
  if (!res.ok) return void (main.innerHTML = `<div class="empty">${esc(res.error || "Could not list MCP servers.")}</div>`);
  const servers = res.servers || [];
  /** @type {Record<string, any[]>} */
  const byKind = {};
  for (const s of servers) (byKind[s.kind] ??= []).push(s);

  let html = `<div class="card"><div class="title">Add a server</div>
    <div class="sub">Registers a user-scoped MCP server via <span class="mono">claude mcp add-json</span>.</div>
    <div class="actions" style="margin-top:10px">
      <input type="text" id="mcpName" placeholder="name">
      <input type="text" id="mcpJson" placeholder='{"type":"stdio","command":"my-mcp","args":[]}' style="flex:1;min-width:260px">
      <button class="btn primary" id="mcpAdd">Add</button>
      <button class="btn" id="mcpRefresh">Refresh</button>
    </div></div>`;

  if (!servers.length) html += `<div class="empty">No MCP servers configured.</div>`;
  for (const [kind, heading] of MCP_GROUPS) {
    const list = byKind[kind];
    if (!list || !list.length) continue;
    html += `<div class="group"><h3>${esc(heading)}</h3>`;
    for (const s of list) {
      const st = MCP_STATUS[s.status] || MCP_STATUS.unknown;
      const actions = [];
      if (s.canAuth && s.needsAuth) actions.push(`<button class="btn" data-login="${esc(s.name)}">Authenticate</button>`);
      if (s.canAuth && s.connected) actions.push(`<button class="btn" data-logout="${esc(s.name)}">Log out</button>`);
      if (s.removable) actions.push(`<button class="btn danger" data-remove="${esc(s.name)}">Remove</button>`);
      html += `<div class="card"><div class="title">${esc(s.name)}
        <span class="pill ${st.cls}">${esc(st.label)}</span>
        <span class="pill">${esc(s.transport)}</span></div>
        <div class="sub mono">${esc(s.endpoint)}</div>
        ${actions.length ? `<div class="actions">${actions.join("")}</div>` : ""}</div>`;
    }
    html += `</div>`;
  }
  main.innerHTML = html;

  gid("mcpRefresh").onclick = () => render();
  gid("mcpAdd").onclick = async () => {
    const name = gid("mcpName").value.trim();
    const json = gid("mcpJson").value.trim();
    if (!name || !json) return toast("name and JSON definition required", true);
    const r = await run("./mcp.py", { action: "add", name, json });
    if (r.ok) { toast(`Added ${name}`); render(); } else toast(r.stderr || r.error || "Add failed", true);
  };
  qa(main, "[data-login]").forEach((el) => el.onclick = async () => {
    const r = await run("./mcp.py", { action: "login", name: el.dataset.login || "" });
    if (!r.ok) return toast(r.error || "Could not launch login", true);
    await showPreview(`Authenticating "${el.dataset.login}"`,
      { files: [], settings: [], note: "A browser window should open to complete OAuth. Once you've approved access, click Refresh to update the status." },
      [{ label: "Done — Refresh", primary: true, value: true }]);
    render();
  });
  qa(main, "[data-logout]").forEach((el) => el.onclick = async () => {
    const r = await run("./mcp.py", { action: "logout", name: el.dataset.logout || "" });
    if (r.ok) { toast(`Logged out ${el.dataset.logout}`); render(); } else toast(r.stderr || "Logout failed", true);
  });
  qa(main, "[data-remove]").forEach((el) => el.onclick = async () => {
    const ok = await showPreview(`Remove MCP server "${el.dataset.remove}"?`,
      { files: [], settings: [] },
      [{ label: "Cancel", value: false }, { label: "Remove", primary: true, value: true }]);
    if (!ok) return;
    const r = await run("./mcp.py", { action: "remove", name: el.dataset.remove || "" });
    if (r.ok) { toast(`Removed ${el.dataset.remove}`); render(); } else toast(r.stderr || "Remove failed", true);
  });
}

/** @type {Record<string, () => Promise<void>>} */
const RENDERERS = {
  preferences: renderPreferences, plugins: renderPlugins, marketplaces: renderMarketplaces,
  memory: renderMemory, skills: renderSkills, statusline: renderStatusline,
  profiles: renderProfiles, mcp: renderMcp, history: renderHistory,
};

let renderSeq = 0;
async function render() {
  const tab = TABS.find((t) => t.id === currentTab()) || TABS[0];
  renderNav();
  const seq = ++renderSeq;
  main.innerHTML = `<h2>${esc(tab.label)}</h2><div class="caption">${esc(tab.file)}</div><div class="empty">Loading…</div>`;
  try {
    // renderers replace #main wholesale; re-add the header afterward
    await RENDERERS[tab.id]();
    if (seq !== renderSeq) return; // a newer render started; discard
    main.insertAdjacentHTML("afterbegin", `<h2>${esc(tab.label)}</h2><div class="caption">${esc(tab.file)}</div>`);
  } catch (e) {
    if (seq === renderSeq) main.innerHTML = `<h2>${esc(tab.label)}</h2><div class="caption">${esc(tab.file)}</div><div class="empty">Failed to load. See console.</div>`;
    console.error(e);
  }
}

fused.params.onChange(render);
render();
refreshBadge();
