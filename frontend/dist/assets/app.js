"use strict";
// Document Intelligence — console SPA. Built with a safe hyperscript helper (no innerHTML):
// all text becomes text nodes, so user/API content can never be interpreted as HTML.

// ---- safe DOM builder ----
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  if (attrs)
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v;
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v);
    }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
const set = (n, ...kids) => { clear(n); for (const k of kids.flat()) if (k != null && k !== false) n.append(k.nodeType ? k : document.createTextNode(String(k))); return n; };
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const clientId = () => $("#clientId").value.trim() || "acme-bank-001";
const masked = () => $("#maskToggle").checked;
const main = $("#main");
let DOCS = [];
let SEL_DOC = null; // remembered across mask/client re-renders (tree + manifest views)

const SAMPLES = {
  "passport_specimen.txt":
    "PASSPORT\nREPUBLIC OF UTOPIA\nType: P   Code: UTO\nSurname: ERIKSSON\nGiven names: ANNA MARIA\nP<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\nL898902C36UTO7408122F1204159ZE184226B<<<<<10\n",
  "us_ssn_card.txt":
    "SOCIAL SECURITY ADMINISTRATION\nTHIS NUMBER HAS BEEN ESTABLISHED FOR\nJANE A DOE\n536-90-4399\nSignature: Jane A Doe\n",
  "mx_ine_credencial.txt":
    "INSTITUTO NACIONAL ELECTORAL\nCREDENCIAL PARA VOTAR\nNOMBRE GUILLERMINA HERNANDEZ GUZMAN\nCLAVE DE ELECTOR HRGZGL56042709M400\nCURP HEGG560427MVZRRL04\nFECHA DE NACIMIENTO 27/04/1956\nSEXO M\nVIGENCIA 2030\n",
  "us_utility_bill.txt":
    "PACIFIC ELECTRIC UTILITY\nSTATEMENT OF ACCOUNT\nService Address: 742 Evergreen Terrace, Springfield, OR 97403\nAccount Number: 4471-2098-33\nBilling Period: 2026-05-01 to 2026-05-31\nCustomer: Jane A Doe\nAmount Due: $128.44\n",
};

// ---- API ----
async function api(path, { method = "GET", body } = {}) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${method} ${path} → ${r.status}`);
  return data;
}
async function streamIngest(file, onEvent) {
  const fd = new FormData();
  fd.append("client_id", clientId());
  fd.append("file", file);
  const resp = await fetch("/api/v1/ingest", { method: "POST", body: fd });
  if (!resp.ok || !resp.body) throw new Error("ingest failed: " + resp.status);
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf = (buf + dec.decode(value, { stream: true })).replace(/\r\n/g, "\n");
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of block.split("\n"))
        if (line.startsWith("data:")) { try { onEvent(JSON.parse(line.slice(5).trim())); } catch {} }
    }
  }
}

// ---- ui atoms ----
const pill = (txt, cls) => h("span", { class: "pill " + (cls || txt) }, txt);
const spinner = () => h("span", { class: "spinner" });
const card = (...kids) => h("div", { class: "card" }, ...kids);
const warn = (m) => h("div", { class: "card warn" }, m);
const empty = (m) => h("div", { class: "empty" }, m);
const toast = (msg, err = false) => {
  const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
  setTimeout(() => (t.className = "toast"), 3200);
};
function showModal(...kids) { set($("#modalBody"), ...kids); $("#modal").hidden = false; }
$("#modalClose").onclick = () => ($("#modal").hidden = true);
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").hidden = true; };
const headOf = (title, sub) => [h("h2", { class: "view-head" }, title), h("p", { class: "view-sub" }, sub)];

// ============================ VIEWS ============================
const views = {};

// ---- Ingest ----
views.ingest = () => {
  const drop = h("div", { class: "drop" },
    h("div", { class: "big" }, "⇪"),
    h("div", {}, "Drop a file here or ", h("b", {}, "click to browse")),
    h("div", { class: "muted", style: "margin-top:6px" }, "PDF / image / .txt — text is read directly"));
  const file = h("input", { type: "file", hidden: "" });
  const samples = h("div", { class: "samples" },
    Object.keys(SAMPLES).map((n) => h("span", { class: "chip", onclick: () => runIngest(new File([SAMPLES[n]], n, { type: "text/plain" })) }, "＋ " + n)));
  const out = h("div", {});
  drop.onclick = () => file.click();
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) runIngest(e.dataTransfer.files[0]); };
  file.onchange = () => file.files[0] && runIngest(file.files[0]);
  window._ingestOut = out;
  set(main, ...headOf("Ingest a document",
    "Upload a file (or use a sample). It is OCR'd, classified by the PII-safe gate, routed to deterministic or LLM extraction, and assembled into the client's knowledge subtree."),
    card(drop, file, samples), out);
};
async function runIngest(file) {
  const tl = h("ul", { class: "timeline" });
  const title = h("h3", {}, file.name + " ", spinner());
  set(window._ingestOut, card(title, tl));
  const icons = { start: "▸", done: "✓", skip: "↷", error: "✕" };
  const seen = {};
  try {
    await streamIngest(file, (ev) => {
      const d = ev.detail || {};
      const dtxt = Object.keys(d).length ? JSON.stringify(d) : "";
      const li = seen[ev.stage] || (seen[ev.stage] = tl.appendChild(h("li", {})));
      li.className = "stage " + ev.status;
      set(li, h("span", { class: "ic" }, icons[ev.status] || "•"),
        h("div", {}, h("span", { class: "name" }, ev.stage), ev.status === "skip" ? " (skipped)" : "",
          h("div", { class: "detail" }, dtxt)));
    });
    set(title, file.name + "  ", pill("done", "ok"));
    toast("Ingested " + file.name); DOCS = [];
  } catch (e) { set(title, file.name + " — failed"); toast(e.message, true); window._ingestOut.append(warn(e.message)); }
}

// ---- Documents ----
views.documents = async () => {
  const box = h("div", {}, spinner());
  set(main, ...headOf("Documents", "All documents ingested for " + clientId() + "."), box);
  try {
    const r = await api(`/api/v1/clients/${clientId()}/documents`);
    DOCS = r.documents || [];
    if (!DOCS.length) return set(box, empty("No documents yet — ingest one first."));
    const rows = DOCS.map((d) => {
      const tr = h("tr", { class: "click", onclick: () => { SEL_DOC = d.id; go("tree"); setTimeout(loadTree, 60); } },
        h("td", {}, d.document_name), h("td", {}, pill(d.doc_type || "?", "type")),
        h("td", {}, pill(d.sensitivity_bucket)), h("td", {}, pill(d.gate_decision || "-")),
        h("td", {}, d.page_count ?? "-"), h("td", { class: "muted" }, (d.created_at || "").slice(0, 19).replace("T", " ")));
      return tr;
    });
    set(box, card(h("div", { class: "count" }, r.count + " document(s)"),
      table(["Document", "Type", "Sensitivity", "Gate", "Pages", "Created"], rows)));
  } catch (e) { set(box, warn(e.message)); }
};
const table = (cols, rows) => h("table", {},
  h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, c)))), h("tbody", {}, rows));

// ---- Tree ----
views.tree = async () => {
  const sel = h("select", { class: "text", id: "docSel", style: "max-width:380px" });
  const count = h("span", { class: "count", id: "treeCount" });
  const box = h("div", { id: "tree" });
  set(main, ...headOf("Knowledge tree",
    "The per-document knowledge subtree (document → sections → chunks → facts). Toggle Mask PII in the top bar for the access-aware projection."),
    h("div", { class: "toolbar" }, sel, h("button", { class: "btn", onclick: loadTree }, "Load"), count), box);
  await fillDocSelect(sel);
  if (sel.value) loadTree();
};
async function loadTree() {
  const did = $("#docSel").value; if (!did) return;
  SEL_DOC = did;
  const box = set($("#tree"), spinner());
  try {
    const r = await api(`/api/v1/clients/${clientId()}/tree?doc_id=${did}&mask=${masked()}`);
    $("#treeCount").textContent = r.count + " nodes";
    set(box, card(h("div", { class: "tree" }, (r.tree || []).map(renderNode))));
  } catch (e) { set(box, warn(e.message)); }
}
function renderNode(n) {
  const kids = n.children || [];
  const childWrap = kids.length ? h("div", { class: "tchildren" }, kids.map(renderNode)) : null;
  const caret = h("span", { class: "caret" }, kids.length ? "▾" : "");
  if (kids.length) caret.onclick = () => {
    const hidden = childWrap.style.display === "none";
    childWrap.style.display = hidden ? "" : "none"; caret.textContent = hidden ? "▾" : "▸";
  };
  const label = [h("span", { class: "ntype " + n.node_type }, n.node_type)];
  if (n.node_type === "fact") {
    label.push(" ", h("span", { class: "kv" }, n.attribute_key || ""), " = ", h("span", { class: "nval" }, n.value_text ?? ""));
    if (n.verification_status === "checksum_verified") label.push(" ", pill("✓ checksum", "ok"));
    if (n.masked) label.push(" ", pill("masked", "warn"));
  } else {
    label.push(" ", h("span", { class: "ntitle" }, n.title || (n.content || "").slice(0, 60)));
  }
  const wrap = h("div", { class: "twrap" }, caret, ...label);
  wrap.onclick = (e) => { if (e.target !== caret) provenance(n.id); };
  return h("div", { class: "tnode" }, wrap, childWrap);
}
async function provenance(nodeId) {
  try {
    const p = await api(`/api/v1/nodes/${nodeId}/provenance?client_id=${clientId()}`);
    showModal(h("h3", {}, "Node provenance"),
      table([], [
        h("tr", {}, h("th", {}, "node_type"), h("td", {}, p.node_type)),
        h("tr", {}, h("th", {}, "attribute"), h("td", { class: "val" }, p.attribute_key || "-")),
        h("tr", {}, h("th", {}, "verification"), h("td", {}, pill(p.verification_status || "unverified", p.verification_status === "checksum_verified" ? "ok" : ""))),
        h("tr", {}, h("th", {}, "confidence"), h("td", {}, (p.confidence ?? 0).toFixed(2))),
        h("tr", {}, h("th", {}, "document"), h("td", { class: "val" }, p.doc_id)),
      ]),
      h("h3", { style: "margin-top:14px" }, "Source"),
      h("pre", { class: "json" }, JSON.stringify(p.provenance, null, 2)));
  } catch (e) { toast(e.message, true); }
}

// ---- Merged facts ----
views.facts = async () => {
  const vOnly = h("input", { type: "checkbox" });
  const count = h("span", { class: "count" });
  const box = h("div", {});
  const load = async () => {
    set(box, spinner());
    try {
      const r = await api(`/api/v1/clients/${clientId()}/facts?verified_only=${vOnly.checked}&mask=${masked()}`);
      count.textContent = r.count + " fact(s)";
      if (!r.facts.length) return set(box, empty("No merged facts yet."));
      const rows = r.facts.map((f) => h("tr", {},
        h("td", { class: "val" }, f.attribute_key), h("td", { class: "val" }, f.resolved_value),
        h("td", {}, h("div", { class: "conf-bar" }, h("span", { style: "width:" + Math.round((f.confidence || 0) * 100) + "%" }))),
        h("td", {}, f.verified ? pill("✓", "ok") : h("span", { class: "muted" }, "—")),
        h("td", {}, pill(f.sensitivity)),
        h("td", {}, f.conflict ? pill("conflict", "warn") : h("span", { class: "muted" }, "—"))));
      set(box, card(table(["Attribute", "Value", "Confidence", "Verified", "Sensitivity", "Conflict"], rows)));
    } catch (e) { set(box, warn(e.message)); }
  };
  vOnly.onchange = load;
  set(main, ...headOf("Merged client facts",
    "Cross-document consolidation for " + clientId() + " (confidence-weighted; conflicts flagged)."),
    h("div", { class: "toolbar" }, h("label", { class: "toggle" }, vOnly, h("span", {}, "Verified only")), count), box);
  load();
};

// ---- Search ----
views.search = () => {
  const q = h("input", { class: "text", placeholder: "e.g. passport number, curp date of birth, electric account" });
  const res = h("div", {});
  const run = async () => {
    if (!q.value.trim()) return;
    set(res, spinner());
    try {
      const r = await api(`/api/v1/clients/${clientId()}/search`, { method: "POST", body: { query: q.value.trim(), top_k: 8, mask: masked() } });
      if (!r.hits.length) return set(res, empty("No matches."));
      set(res, h("div", { class: "count", style: "margin-bottom:10px" }, r.count + " hit(s)"),
        r.hits.map((hh) => h("div", { class: "hit" },
          h("div", { class: "meta" }, h("span", { class: "rank" }, "#" + hh._rank), pill(hh.node_type, "type"),
            h("span", {}, "score " + (hh._score || 0).toFixed(4)), hh.masked ? pill("masked", "warn") : null),
          h("div", { class: "snippet" }, hh.content || (hh.attribute_key ? hh.attribute_key + " = " + (hh.value_text ?? "") : "")),
          h("div", { class: "path" }, hh.path))));
    } catch (e) { set(res, warn(e.message)); }
  };
  q.addEventListener("keydown", (e) => e.key === "Enter" && run());
  set(main, ...headOf("Hybrid search",
    "Dense + lexical + structural retrieval scoped to " + clientId() + "; returns ranked nodes with grounding."),
    h("div", { class: "toolbar grow" }, q, h("button", { class: "btn", onclick: run }, "Search")), res);
};

// ---- Manifest ----
views.manifest = async () => {
  const sel = h("select", { class: "text", id: "mSel", style: "max-width:380px" });
  const box = h("div", {});
  const load = async () => {
    const did = sel.value; if (!did) return;
    SEL_DOC = did;
    set(box, spinner());
    try {
      const cid = clientId();
      const [man, ans] = await Promise.all([
        api(`/api/v1/clients/${cid}/docs/${did}/manifest`),
        api(`/api/v1/clients/${cid}/docs/${did}/answerable`),
      ]);
      const reps = man.accessibility_rep_counts || {};
      const meta = (lbl, val) => h("div", {}, h("div", { class: "muted" }, lbl), val);
      set(box,
        card(h("h3", {}, (man.document_name || "") + "  ", pill(man.doc_type || "?", "type")),
          h("div", { class: "row", style: "gap:24px;margin-bottom:10px" },
            meta("Jurisdiction", man.jurisdiction || "-"), meta("Language", man.languages || "-"),
            meta("Sensitivity", pill(man.sensitivity)), meta("Gate", pill(man.gate_decision || "-")),
            meta("Answerable", man.answerable ? pill("yes", "ok") : pill("no", ""))),
          h("div", { class: "muted", style: "margin:8px 0 4px" }, "Attribute keys"),
          h("div", {}, (man.attribute_keys || []).length ? (man.attribute_keys).map((k) => [pill(k, "type"), " "]) : h("span", { class: "muted" }, "none")),
          h("div", { class: "muted", style: "margin:12px 0 4px" }, "Accessibility representations"),
          h("div", {}, Object.keys(reps).length ? Object.entries(reps).map(([k, v]) => [pill(k, ""), " " + v + "  "]) : h("span", { class: "muted" }, "none (deterministic-only docs skip LLM aids)"))),
        card(h("h3", {}, "Answerable questions (" + (ans.answerable || []).length + ")"),
          (ans.answerable || []).length ? (ans.answerable).map((a) => h("div", { class: "hit" },
            h("div", { class: "snippet" }, a.question), h("div", { class: "path" }, a.path + " · " + a.lang)))
            : h("div", { class: "muted" }, "No accessibility reps for this document.")));
    } catch (e) { set(box, warn(e.message)); }
  };
  set(main, ...headOf("Capabilities manifest",
    "What a document knows + the questions it can answer (self-describing surface)."),
    h("div", { class: "toolbar" }, sel, h("button", { class: "btn", onclick: load }, "Load")), box);
  await fillDocSelect(sel);
  if (sel.value) load();
};

// ---- shared ----
async function fillDocSelect(sel) {
  if (!DOCS.length) { try { DOCS = (await api(`/api/v1/clients/${clientId()}/documents`)).documents || []; } catch {} }
  set(sel, DOCS.length
    ? DOCS.map((d) => h("option", { value: d.id }, d.document_name + " — " + (d.doc_type || "?")))
    : h("option", { value: "" }, "(no documents — ingest first)"));
  if (SEL_DOC && DOCS.some((d) => d.id === SEL_DOC)) sel.value = SEL_DOC; // restore selection
}

// ============================ ROUTER ============================
function go(view) {
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  (views[view] || views.ingest)();
}
$$(".nav-item").forEach((b) => (b.onclick = () => go(b.dataset.view)));
$("#clientId").addEventListener("change", () => { DOCS = []; const a = $(".nav-item.active"); if (a) go(a.dataset.view); });
$("#maskToggle").addEventListener("change", () => { const a = $(".nav-item.active"); if (a) go(a.dataset.view); });

(async () => { try { await api("/health"); $("#health").className = "health ok"; } catch { $("#health").className = "health bad"; } })();
go("ingest");
