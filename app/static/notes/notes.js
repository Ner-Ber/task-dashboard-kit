// Notes tab: projects → notes on the left, markdown editor + live preview, images by
// paste/drop, autosave to disk (~2 s) and batched git sync (~60 s idle, note switch,
// leaving the tab, Save / Ctrl+S). Talks only to this loopback server.
(() => {
  "use strict";
  const $ = s => document.querySelector(s);
  const ed = $("#ed"), pv = $("#pv"), main = $("#main"), stateEl = $("#state");
  const SAVE_MS = 2000, SYNC_MS = 60000, MAX_SIDE = 1600, QUALITY = 0.85;
  const MODE_KEY = "notes-mode", TASKS_KEY = "notes-tasks", OPEN_KEY = "notes-open-projects";

  let projects = [], cur = null;            // cur = {key, name, base}
  let dirty = false, unsynced = false, saving = null, saveT = 0, syncT = 0, renderT = 0;
  let openProjects = new Set(load(OPEN_KEY, []));

  function load(k, d) { try { const v = JSON.parse(localStorage.getItem(k)); return v ?? d; } catch (e) { return d; } }
  function store(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  function toast(msg) {
    const d = document.createElement("div"); d.className = "toast"; d.textContent = msg;
    $("#toasts").append(d); setTimeout(() => d.remove(), 6000);
  }
  async function api(path, body, opts = {}) {
    const r = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": opts.type || "application/json"},
      body: opts.raw ? body : JSON.stringify(body)});
    const j = await r.json().catch(() => ({error: "HTTP " + r.status}));
    if (!r.ok) throw new Error(j.error || r.status);
    return j;
  }
  const qs = o => Object.entries(o).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");

  // ---------------------------------------------------------------- state pill
  function setState(s, text) {
    stateEl.dataset.s = s;
    stateEl.textContent = text || {saved: "saved", unsynced: "unsynced", dirty: "editing…",
      saving: "saving…", conflict: "conflict", error: "not saved", idle: ""}[s];
    stateEl.title = {saved: "On disk and pushed to the vault", unsynced: "On disk, not yet committed/pushed",
      dirty: "Typing — saves to disk 2 s after you stop", conflict: "See the red banner",
      error: "Could not save — your text is still in the editor"}[s] || "";
  }
  function refreshState() {
    if (stateEl.dataset.s === "conflict" || stateEl.dataset.s === "error") return;
    setState(!cur ? "idle" : dirty ? "dirty" : unsynced ? "unsynced" : "saved");
  }

  // ---------------------------------------------------------------- tree
  async function loadList() {
    const j = await api("/notes/api/list");
    projects = j.projects;
    if (j.brand) { $("#brand").textContent = j.brand; if (!cur) document.title = "Notes · " + j.brand; }
    if (!j.repo_ok) toast("Notes repo not found at " + j.root + " — clone your notes repo there");
    if (j.dirty) { unsynced = true; scheduleSync(); }
    showConflicts(j.conflicts);
    drawTree();
    refreshState();
  }
  function drawTree() {
    const q = $("#find").value.trim().toLowerCase();
    const tree = $("#tree"); tree.textContent = "";
    const byDom = new Map();
    for (const p of projects) { if (!byDom.has(p.domain)) byDom.set(p.domain, []); byDom.get(p.domain).push(p); }
    for (const [dom, list] of byDom) {
      const sec = document.createElement("section"); sec.className = "dom"; sec.dataset.d = dom; sec.dataset.i = [...byDom.keys()].indexOf(dom) % 6;
      const h = document.createElement("h4"); h.textContent = dom; sec.append(h);
      // Category-level list first ("general"), then projects; with notes first, then empty ones.
      list.sort((a, b) => (a.kind === "category" ? -1 : b.kind === "category" ? 1 : 0)
        || (b.notes.length > 0) - (a.notes.length > 0) || a.title.localeCompare(b.title));
      let shown = 0;
      for (const p of list) {
        const notes = p.notes.filter(n => !q || n.name.toLowerCase().includes(q) || p.title.toLowerCase().includes(q));
        if (q && !notes.length) continue;
        shown++;
        const div = document.createElement("div");
        const isOpen = q || openProjects.has(p.key) || (cur && cur.key === p.key);
        div.className = "proj" + (p.notes.length ? "" : " empty") + (isOpen ? " open" : "");
        const ph = document.createElement("div"); ph.className = "ph";
        const t = document.createElement("span"); t.textContent = (p.kind === "category" ? dom + " (general)" : p.title);
        t.dir = "auto";
        const n = document.createElement("span"); n.className = "n"; n.textContent = p.notes.length || "";
        const add = document.createElement("button"); add.className = "add"; add.type = "button";
        add.textContent = "+"; add.title = "New note in " + p.title;
        add.onclick = e => { e.stopPropagation(); newNote(p); };
        ph.append(t, n, add);
        ph.onclick = () => { openProjects.has(p.key) ? openProjects.delete(p.key) : openProjects.add(p.key);
          store(OPEN_KEY, [...openProjects]); drawTree(); };
        div.append(ph);
        if (isOpen && notes.length) {
          const ul = document.createElement("ul");
          for (const nt of notes) {
            const li = document.createElement("li"), a = document.createElement("a");
            a.href = "#" + qs({key: p.key, note: nt.name}); a.textContent = nt.name; a.dir = "auto";
            if (cur && cur.key === p.key && cur.name === nt.name) a.className = "on";
            if (nt.conflict) { a.classList.add("cf"); a.title = "Conflict copy: click to compare with the note"; }
            li.append(a);
            if (nt.conflict) {
              li.className = "cfi";
              const acts = document.createElement("span"); acts.className = "cfacts";
              const mb = document.createElement("button"); mb.type = "button"; mb.textContent = "⇄ Merge";
              mb.title = "Merge per dated entry: add entries only the copy has; for shared entries the later-edited version wins";
              mb.onclick = e => { e.preventDefault(); e.stopPropagation(); resolveConflict(p.key, nt.name, "merge"); };
              const db = document.createElement("button"); db.type = "button"; db.textContent = "×";
              db.title = "Discard this copy (keep the note as it is; the copy stays in git history)";
              db.onclick = e => { e.preventDefault(); e.stopPropagation(); resolveConflict(p.key, nt.name, "discard"); };
              acts.append(mb, db); li.append(acts);
            }
            ul.append(li);
          }
          div.append(ul);
        }
        sec.append(div);
      }
      if (shown) tree.append(sec);
    }
  }
  $("#find").addEventListener("input", drawTree);

  // ---------------------------------------------------------------- open / new / rename
  async function openNote(key, name) {
    if (cur && cur.key === key && cur.name === name) return;
    await flush();
    let j;
    try { j = await api("/notes/api/note?" + qs({key, name})); }
    catch (e) { toast("Can't open: " + e.message); return; }
    cur = {key, name, base: j.hash, path: j.path};
    ed.value = j.text; dirty = false;
    $("#empty").hidden = true; ed.hidden = false; pv.hidden = false;
    main.classList.toggle("conflict", CONFLICT_RE.test(name));
    openProjects.add(key); store(OPEN_KEY, [...openProjects]);
    drawCrumb(); drawTree();
    if (CONFLICT_RE.test(name)) await showConflict(key, name); else render();
    refreshState(); loadTasks();
    if (location.hash.slice(1) !== qs({key, note: name})) history.replaceState(null, "", "#" + qs({key, note: name}));
  }
  function drawCrumb() {
    const c = $("#crumb"); c.textContent = "";
    if (!cur) return;
    const p = projects.find(p => p.key === cur.key) || {title: cur.key, domain: ""};
    const parts = [p.domain, p.kind === "category" ? "general" : p.title, cur.name];
    parts.forEach((x, i) => { if (i) { const s = document.createElement("span"); s.className = "sep"; s.textContent = "›"; c.append(s); }
      const sp = document.createElement("span"); sp.textContent = x; sp.dir = "auto"; c.append(sp); });
    const rn = document.createElement("button"); rn.className = "rn"; rn.type = "button"; rn.textContent = "✎";
    rn.title = "Rename note"; rn.onclick = renameNote; c.append(rn);
    document.title = cur.name + " · Notes";
  }
  function cleanName(s) { return (s || "").replace(/[\/\\<>:"|?*\x00-\x1f]/g, "-").replace(/\s+/g, " ").trim().replace(/\.md$/i, ""); }
  async function newNote(p) {
    const name = cleanName(prompt("New note in " + p.title + " — name:", "Progress"));
    if (!name) return;
    try { await flush(); await api("/notes/api/create", {key: p.key, name}); }
    catch (e) { toast("Not created: " + e.message); return; }
    unsynced = true; scheduleSync();
    await loadList(); location.hash = qs({key: p.key, note: name});
  }
  async function renameNote() {
    if (!cur) return;
    const name = cleanName(prompt("Rename note:", cur.name));
    if (!name || name === cur.name) return;
    try { await flush(false); await api("/notes/api/rename", {key: cur.key, name: cur.name, new: name}); }
    catch (e) { toast("Not renamed: " + e.message); return; }
    cur.name = name; unsynced = true; scheduleSync();
    history.replaceState(null, "", "#" + qs({key: cur.key, note: name}));
    await loadList(); drawCrumb();
  }

  // ---------------------------------------------------------------- save / sync
  function scheduleSync() { clearTimeout(syncT); syncT = setTimeout(() => sync().catch(() => {}), SYNC_MS); }
  async function save() {
    if (!cur || !dirty) return saving;
    if (saving) { await saving; if (!dirty) return; }
    const snap = {key: cur.key, name: cur.name, text: ed.value, base: cur.base};
    dirty = false; setState("saving");
    saving = (async () => {
      try {
        const j = await api("/notes/api/save", snap);
        if (cur && cur.key === snap.key && cur.name === snap.name) cur.base = j.hash;
        unsynced = true; scheduleSync();
        if (j.conflict) { showConflicts([j.conflict]); loadList(); }
        refreshState();
      } catch (e) {
        if (cur && cur.key === snap.key && cur.name === snap.name) dirty = true;
        setState("error", "not saved: " + e.message); throw e;
      } finally { saving = null; }
    })();
    return saving;
  }
  async function sync() {
    clearTimeout(syncT);
    await save();
    if (!unsynced) return;
    setState("saving", "syncing…");
    try {
      const j = await api("/notes/api/sync", {});
      unsynced = !!j.dirty;
      if (j.warn) toast(j.warn);
      if (j.conflicts && j.conflicts.length) { showConflicts(j.conflicts); await loadList(); }
      if (cur) await reloadIfChanged();
      refreshState();
    } catch (e) { setState("error", "sync failed: " + e.message); scheduleSync(); }
  }
  // After a pull the note on disk may have new text from elsewhere; take it if we have no edits.
  async function reloadIfChanged() {
    if (!cur || dirty) return;
    try {
      const j = await api("/notes/api/note?" + qs({key: cur.key, name: cur.name}));
      if (j.hash !== cur.base) {
        const pos = ed.selectionStart; ed.value = j.text; cur.base = j.hash;
        ed.selectionStart = ed.selectionEnd = Math.min(pos, ed.value.length); render();
      }
    } catch (e) {}
  }
  async function flush(doSync = true) { clearTimeout(saveT); await save(); if (doSync && unsynced) await sync(); }

  ed.addEventListener("input", () => {
    dirty = true; refreshState();
    clearTimeout(saveT); saveT = setTimeout(() => save().catch(() => {}), SAVE_MS);
    clearTimeout(syncT);
    clearTimeout(renderT); renderT = setTimeout(render, 120);
  });
  $("#saveBtn").onclick = () => flush().catch(() => {});
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); flush().catch(() => {}); }
  });
  // Leaving the tab: hand the last text and a sync to the server even if the page dies.
  function beacon() {
    if (cur && dirty) {
      navigator.sendBeacon("/notes/api/save", new Blob([JSON.stringify(
        {key: cur.key, name: cur.name, text: ed.value, base: cur.base})], {type: "text/plain"}));
      dirty = false; unsynced = true;
    }
    if (unsynced) navigator.sendBeacon("/notes/api/sync", new Blob(["{}"], {type: "text/plain"}));
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") beacon();
    else if (cur) reloadIfChanged();
  });
  window.addEventListener("pagehide", beacon);

  // ---------------------------------------------------------------- conflicts
  let conflicts = [];
  function showConflicts(list) {
    if (!list || !list.length) return;
    for (const c of list) if (!conflicts.some(x => x.copy === c.copy && x.key === c.key)) conflicts.push(c);
    const b = $("#banner"); b.textContent = ""; b.hidden = false;
    const t = document.createElement("span");
    t.textContent = "Conflict: your text is kept in the note; the other version was saved beside it — ";
    b.append(t);
    conflicts.forEach((c, i) => {
      if (i) b.append(", ");
      const a = document.createElement("a"); a.textContent = c.copy; a.title = c.why || "";
      a.onclick = () => { location.hash = qs({key: c.key, note: c.copy}); };
      b.append(a);
    });
    const ok = document.createElement("button"); ok.type = "button"; ok.textContent = "Got it";
    ok.onclick = async () => { conflicts = []; b.hidden = true; await api("/notes/api/conflicts/dismiss", {}).catch(() => {});
      setState("idle"); refreshState(); };
    b.append(" ", ok);
    setState("conflict");
  }

  // ---------------------------------------------------------------- conflict copies: diff, merge, discard
  const CONFLICT_RE = /^(.+)\.conflict-\d{8}-\d{6}$/;
  // One sentence on what ⇄ Merge will do (entry by entry, see notebook_merge in dashboard.py).
  function planText(p) {
    if (!p) return "";
    const parts = [];
    if (p.added.length) parts.push(`add ${p.added.length} entr${p.added.length === 1 ? "y" : "ies"} that only the copy has (${p.added.join("; ")})`);
    if (p.kept_note.length) parts.push(`keep your note's newer version of ${p.kept_note.length} entr${p.kept_note.length === 1 ? "y" : "ies"} (edited after the conflict)`);
    if (p.combined.length) parts.push(`combine both versions' lines in ${p.combined.length} entr${p.combined.length === 1 ? "y" : "ies"} (${p.combined.join("; ")})`);
    return parts.length ? "Merge will " + parts.join(", ") + "." : "Merge would change nothing: the note already has everything in the copy.";
  }
  async function showConflict(key, name) {
    let d;
    try { d = await api("/notes/api/conflict?" + qs({key, name})); }
    catch (e) { pv.textContent = "Can't compare: " + e.message; return; }
    const escH = t => t.replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
    let h = `<div class="cfhead"><b>Conflict copy</b> of <a href="#${qs({key, note: d.note})}">${escH(d.note)}</a>`
      + ` <span class="muted">· <span class="dminus">${d.only_in_note} line(s) only in the note</span>`
      + ` · <span class="dplus">${d.only_in_copy} line(s) only in this copy</span></span>`
      + `<div class="cfbtns"><button type="button" data-how="merge" title="Merge per dated entry, then delete the copy">⇄ Merge into ${escH(d.note)}</button>`
      + `<button type="button" data-how="discard" title="Delete this copy; the note stays as it is">× Discard copy</button></div>`
      + `<p class="plan">${escH(planText(d.plan))}</p>`
      + `<p class="muted">Red (−) = only in the note, green (+) = only in this copy. Merge works per dated entry`
      + ` (<code>## Mon D, YYYY</code>): new entries are added by date; for an entry both have, the version edited`
      + ` later wins.</p></div>`;
    if (!d.hunks.length) h += `<p>The two files are identical.</p>`;
    for (const hk of d.hunks) {
      h += `<div class="hunk"><div class="hh">@@ note line ${hk.a} · copy line ${hk.b} @@</div>`;
      for (const [t, x] of hk.lines)
        h += `<div class="dl ${t === "-" ? "dminus" : t === "+" ? "dplus" : ""}" dir="auto">`
          + `<span class="dg">${t === "=" ? " " : t === "-" ? "−" : "+"}</span>${escH(x) || "&nbsp;"}</div>`;
      h += `</div>`;
    }
    pv.innerHTML = h;
    pv.querySelectorAll(".cfbtns button").forEach(b => b.onclick = () => resolveConflict(key, name, b.dataset.how));
  }
  async function resolveConflict(key, name, how) {
    const base = (CONFLICT_RE.exec(name) || [])[1];
    if (how === "discard" && !confirm(`Delete the conflict copy "${name}"?\n\nThe note "${base}" stays as it is; the copy stays in git history.`)) return;
    if (how === "merge") {
      let d; try { d = await api("/notes/api/conflict?" + qs({key, name})); } catch (e) { toast("Can't merge: " + e.message); return; }
      if (!confirm(planText(d.plan) + `\n\nThen the copy is deleted (it stays in git history). Go ahead?`)) return;
    }
    try {
      await flush(false);
      const j = await api("/notes/api/conflict/resolve", {key, name, how});
      const pl = j.plan || {};
      toast(how === "merge" ? `Merged into ${j.note}: ${(pl.added || []).length} added, ${(pl.kept_note || []).length} kept newer, `
        + `${(pl.combined || []).length} combined; copy deleted` : `Conflict copy deleted`);
      unsynced = true; scheduleSync();
      if (cur && cur.key === key && cur.name === name) { cur = null; location.hash = qs({key, note: j.note}); }
      await loadList();
      if (cur && cur.key === key && cur.name === j.note) { cur.base = null; const n = cur.name; cur = null; await openNote(key, n); }
    } catch (e) { toast("Not done: " + e.message); }
  }

  // ---------------------------------------------------------------- preview
  marked.setOptions({gfm: true, breaks: true});
  // Math is cut out before markdown (so _ and \ inside it survive) and rendered by KaTeX after.
  const MATH_RE = /(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$(?![\s$])((?:\\\$|[^$\n`])+?)(?<![\s\\])\$(?!\d)/g;
  function render() {
    if (!cur) return;
    if (main.classList.contains("conflict")) return;   // the diff view owns the pane
    const math = [];
    const src = ed.value.replace(MATH_RE, (m, code, d1, d2, i1, i2) => {
      if (code) return m;
      const display = d1 !== undefined || d2 !== undefined;
      math.push({tex: d1 ?? d2 ?? i1 ?? i2, display});
      return (display ? "\n\nKXMATHD" : "KXMATHI") + (math.length - 1) + "X" + (display ? "\n\n" : "");
    });
    let out = DOMPurify.sanitize(marked.parse(src), {ADD_ATTR: ["target"]});
    out = out.replace(/<p>KXMATHD(\d+)X<\/p>|KXMATH[DI](\d+)X/g, (m, a, b) => {
      const k = math[+(a ?? b)];
      try { return katex.renderToString(k.tex, {displayMode: k.display, throwOnError: false, output: "html"}); }
      catch (e) { return '<code class="mathbad">' + k.tex.replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c])) + "</code>"; }
    });
    const top = pv.scrollTop;
    pv.innerHTML = out;
    pv.querySelectorAll("p, li, h1, h2, h3, h4, h5, h6, blockquote, td, th").forEach(el => el.setAttribute("dir", "auto"));
    pv.querySelectorAll("img").forEach(img => {
      const s = img.getAttribute("src") || "";
      if (!/^(?:[a-z]+:|\/|#)/i.test(s)) {
        let rel = s; try { rel = decodeURI(s); } catch (e) {}
        img.src = "/notes/file?" + qs({key: cur.key, path: rel.replace(/^\.\//, "")});
        img.loading = "lazy";
      }
    });
    pv.querySelectorAll("a[href]").forEach(a => {
      const h = a.getAttribute("href");
      if (/^https?:/i.test(h)) { a.target = "_blank"; a.rel = "noopener noreferrer"; return; }
      const m = /^(?:\.\/)?([^/#?]+)\.md(?:#.*)?$/i.exec(h || "");
      if (m) { let n = m[1]; try { n = decodeURIComponent(n); } catch (e) {}
        a.href = "#" + qs({key: cur.key, note: n}); }
    });
    pv.scrollTop = top;
  }
  // Keep the preview roughly level with the editor while scrolling.
  let syncing = false;
  function follow(from, to) {
    if (syncing || main.dataset.mode !== "split") return;
    syncing = true;
    const f = from.scrollTop / Math.max(1, from.scrollHeight - from.clientHeight);
    to.scrollTop = f * (to.scrollHeight - to.clientHeight);
    requestAnimationFrame(() => { syncing = false; });
  }
  ed.addEventListener("scroll", () => follow(ed, pv));
  pv.addEventListener("scroll", () => follow(pv, ed));

  // ---------------------------------------------------------------- images: paste / drop
  async function shrink(file) {
    if (file.type === "image/gif") return file;                       // keep animation
    let bmp;
    try { bmp = await createImageBitmap(file); } catch (e) { return file; }
    const scale = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
    const w = Math.round(bmp.width * scale), h = Math.round(bmp.height * scale);
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    c.getContext("2d").drawImage(bmp, 0, 0, w, h);
    const webp = await new Promise(res => c.toBlob(res, "image/webp", QUALITY));
    if (!webp || webp.type !== "image/webp") return file;
    // Already small enough and smaller than the WebP: keep the original bytes.
    if (scale === 1 && file.size <= webp.size && /^image\/(png|jpeg|webp)$/.test(file.type)) return file;
    return webp;
  }
  async function insertImages(files) {
    if (!cur) { toast("Open a note first"); return; }
    let at = ed.selectionStart, end = ed.selectionEnd;
    for (const f of files) {
      if (!/^image\//.test(f.type)) { toast("Only images can be pasted"); continue; }
      const mark = "![uploading " + (f.name || "image") + "…]()";
      ed.setRangeText(mark, at, end, "end"); end = at + mark.length;
      try {
        const blob = await shrink(f);
        const j = await api("/notes/api/upload?" + qs({key: cur.key}), blob, {raw: true, type: blob.type});
        const md = "![](" + j.path + ")";
        const i = ed.value.indexOf(mark);
        if (i >= 0) ed.setRangeText(md, i, i + mark.length, "end");
        else ed.setRangeText(md, ed.selectionStart, ed.selectionEnd, "end");
        at = end = ed.selectionStart;
        if (files.length > 1) { ed.setRangeText("\n", at, at, "end"); at = end = ed.selectionStart; }
      } catch (e) {
        const i = ed.value.indexOf(mark); if (i >= 0) ed.setRangeText("", i, i + mark.length, "end");
        toast("Image not saved: " + e.message);
      }
      ed.dispatchEvent(new Event("input"));
    }
  }
  ed.addEventListener("paste", e => {
    const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); insertImages(files); }
  });
  ed.addEventListener("dragover", e => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); ed.classList.add("drop"); } });
  ed.addEventListener("dragleave", () => ed.classList.remove("drop"));
  ed.addEventListener("drop", e => {
    ed.classList.remove("drop");
    const files = [...(e.dataTransfer?.files || [])].filter(f => f.type.startsWith("image/"));
    if (!files.length) return;
    e.preventDefault();
    const pos = caretFromPoint(e) ?? ed.selectionStart;
    ed.focus(); ed.setSelectionRange(pos, pos);
    insertImages(files);
  });
  function caretFromPoint(e) {   // textarea has no API for this; put it at the end of the dropped-on line
    const r = ed.getBoundingClientRect(), lh = parseFloat(getComputedStyle(ed).lineHeight) || 22;
    const line = Math.max(0, Math.floor((e.clientY - r.top - 18 + ed.scrollTop) / lh));
    const lines = ed.value.split("\n"); let pos = 0;
    for (let i = 0; i < Math.min(line, lines.length - 1); i++) pos += lines[i].length + 1;
    return line >= lines.length ? ed.value.length : pos + lines[Math.min(line, lines.length - 1)].length;
  }
  ed.addEventListener("keydown", e => {   // Tab indents instead of leaving the editor
    if (e.key === "Tab" && !e.ctrlKey && !e.altKey) { e.preventDefault(); ed.setRangeText("  ", ed.selectionStart, ed.selectionEnd, "end");
      ed.dispatchEvent(new Event("input")); }
  });

  // ---------------------------------------------------------------- mode / tasks panel
  function setMode(m) {
    main.dataset.mode = m; store(MODE_KEY, m);
    document.querySelectorAll("#mode button").forEach(b => b.classList.toggle("on", b.dataset.m === m));
    if (m !== "edit") render();
  }
  document.querySelectorAll("#mode button").forEach(b => b.onclick = () => setMode(b.dataset.m));
  setMode(load(MODE_KEY, innerWidth >= 1100 ? "split" : "edit"));
  async function loadTasks() {
    const panel = $("#tasks"), ul = $("#taskList");
    if (panel.hidden || !cur) return;
    ul.textContent = "";
    try {
      const j = await api("/notes/api/tasks?" + qs({key: cur.key}));
      if (!j.tasks.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "No open tasks"; ul.append(li); }
      for (const t of j.tasks) {
        const li = document.createElement("li"); li.dir = "auto";
        li.textContent = t.text.replace(/^\[[A-Za-z0-9]+\]\s*/, "");
        if (t.due) { const d = document.createElement("span"); d.className = "due"; d.textContent = "due " + t.due; li.append(d); }
        ul.append(li);
      }
    } catch (e) { toast("Tasks: " + e.message); }
  }
  function setTasks(on) {
    $("#tasks").hidden = !on; $("#tasksBtn").setAttribute("aria-pressed", on); store(TASKS_KEY, on);
    if (on) loadTasks();
  }
  $("#tasksBtn").onclick = () => setTasks($("#tasks").hidden);
  setTasks(load(TASKS_KEY, false));

  // ---------------------------------------------------------------- Pull / Update (rail)
  // Pull: save open edits, then pull + push the notes repo, and pick up what came in.
  // Update: the same, then the dashboard's full refresh (vault pull, projects, GitHub).
  const pullBtn = $("#pullBtn"), updBtn = $("#updBtn");
  async function pullNotes() {
    await flush(false);
    const j = await api("/notes/api/sync", {});
    unsynced = !!j.dirty;
    if (j.warn) toast(j.warn);
    if (j.conflicts && j.conflicts.length) showConflicts(j.conflicts);
    await loadList();
    if (cur) await reloadIfChanged();
    refreshState();
  }
  pullBtn.onclick = async () => {
    pullBtn.disabled = true; pullBtn.textContent = "Pulling…";
    try { await pullNotes(); toast("Notes pulled"); }
    catch (e) { toast("Pull failed: " + e.message); }
    finally { pullBtn.disabled = false; pullBtn.textContent = "↻ Pull"; }
  };
  updBtn.onclick = async () => {
    updBtn.disabled = true; updBtn.textContent = "Updating…";
    try {
      await pullNotes();
      await api("/update", {});
      let j = {};
      for (let i = 0; i < 150; i++) {          // up to ~5 min
        await new Promise(r => setTimeout(r, 2000));
        j = await api("/update");
        if (j.status === "done" || j.status === "error") break;
      }
      await loadList();
      if (cur) loadTasks();
      toast(j.status === "error" ? "Update failed: " + (j.summary || j.error || "see the Tasks page")
                                 : "Updated: vault, projects and GitHub activity");
    } catch (e) { toast("Update failed: " + e.message); }
    finally { updBtn.disabled = false; updBtn.textContent = "↻ Update"; }
  };

  // ---------------------------------------------------------------- routing
  function fromHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    if (p.get("key") && p.get("note")) openNote(p.get("key"), p.get("note"));
  }
  window.addEventListener("hashchange", fromHash);
  loadList().then(fromHash).catch(e => toast("Can't load notes: " + e.message));
})();
