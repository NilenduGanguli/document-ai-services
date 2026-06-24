/*
 * Executive briefing deck — Document Intelligence platform.
 * Build:  cd presentation && npm install pptxgenjs && node build_deck.js
 * Diagrams: PNGs under presentation/assets/diagrams/ (rendered from the docs' Mermaid via mmdc).
 * Palette: "Midnight Executive" (navy / ice-blue / white) + a single gold accent.
 */
const path = require("path");
const pptxgen = require("pptxgenjs");

const DIA = (name) => path.join(__dirname, "assets", "diagrams", name + ".png");
// real pixel dimensions of each rendered diagram (for aspect-correct, centered placement)
const DIMS = {
  "value-chain": [2734, 236], architecture: [4400, 616], subtree: [3404, 1004],
  gate: [2090, 348], ocr: [1940, 612], extraction: [1468, 2220],
  search: [2122, 700], merge: [1524, 1096], erd: [2508, 6740], datamodel: [1544, 1064],
};

// ---- palette (no '#': pptxgenjs requirement) ----
const NAVY = "1E2761", NAVY2 = "2C3A78", ICE = "CADCFC", ICELT = "EEF3FF";
const WHITE = "FFFFFF", INK = "1A1F36", MUTED = "5A6480", GOLD = "C8860D";
const TITLE_FONT = "Cambria", BODY_FONT = "Arial";

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.33 x 7.5 in
pres.author = "Document Intelligence";
pres.title = "Document Intelligence — Executive Briefing";
const W = 13.33, H = 7.5;
const shadow = () => ({ type: "outer", color: "000000", blur: 7, offset: 3, angle: 45, opacity: 0.16 });

// place an image inside box (bx,by,bw,bh), preserving aspect ratio, centered
function placeContain(slide, name, bx, by, bw, bh) {
  const [iw, ih] = DIMS[name];
  const s = Math.min(bw / iw, bh / ih);
  const w = iw * s, h = ih * s;
  slide.addImage({ path: DIA(name), x: bx + (bw - w) / 2, y: by + (bh - h) / 2, w, h });
}

function footer(slide, n) {
  slide.addText("Document Intelligence  ·  Confidential", { x: 0.5, y: H - 0.42, w: 8, h: 0.3, fontFace: BODY_FONT, fontSize: 9, color: MUTED, margin: 0 });
  slide.addText(String(n), { x: W - 1.0, y: H - 0.42, w: 0.5, h: 0.3, fontFace: BODY_FONT, fontSize: 9, color: MUTED, align: "right", margin: 0 });
}
function title(slide, text) {
  slide.background = { color: WHITE };
  slide.addText(text, { x: 0.6, y: 0.42, w: W - 1.2, h: 0.8, fontFace: TITLE_FONT, fontSize: 30, bold: true, color: NAVY, margin: 0, valign: "middle" });
}
function bullets(slide, items, o) {
  slide.addText(items.map((t) => ({ text: t, options: { bullet: { code: "2022", indent: 14 }, color: INK, breakLine: true, paraSpaceAfter: 9 } })),
    { x: o.x, y: o.y, w: o.w, h: o.h, fontFace: BODY_FONT, fontSize: o.fontSize || 15, valign: "top", color: INK });
}

// diagram slide: layout = "full" | "rail" | "wide"
function diagramSlide(n, heading, name, points, notes, layout) {
  const s = pres.addSlide();
  title(s, heading);
  if (layout === "full") {
    placeContain(s, name, 0.5, 1.35, 12.3, 5.15);
    if (points && points[0]) s.addText(points[0], { x: 1.0, y: 6.55, w: 11.3, h: 0.45, fontFace: BODY_FONT, fontSize: 12.5, color: MUTED, align: "center", margin: 0 });
  } else if (layout === "wide") {
    placeContain(s, name, 0.6, 1.45, 12.1, 3.1);
    bullets(s, points, { x: 1.1, y: 4.85, w: 11.1, h: 2.1, fontSize: 15 });
  } else { // rail
    placeContain(s, name, 0.5, 1.5, 7.85, 5.2);
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 8.65, y: 1.5, w: 4.18, h: 5.2, fill: { color: ICELT }, line: { type: "none" }, rectRadius: 0.1, shadow: shadow() });
    bullets(s, points, { x: 8.95, y: 1.78, w: 3.6, h: 4.7, fontSize: 14 });
  }
  footer(s, n);
  if (notes) s.addNotes(notes);
}

// ============================ SLIDE 1 — TITLE ============================
{
  const s = pres.addSlide();
  s.background = { color: NAVY };
  s.addShape(pres.shapes.OVAL, { x: W - 3.2, y: -1.6, w: 4.2, h: 4.2, fill: { color: NAVY2 }, line: { type: "none" } });
  s.addShape(pres.shapes.OVAL, { x: -1.4, y: H - 2.2, w: 3.4, h: 3.4, fill: { color: NAVY2 }, line: { type: "none" } });
  s.addText("DOCUMENT INTELLIGENCE", { x: 0.9, y: 2.45, w: 11.5, h: 1.0, fontFace: TITLE_FONT, fontSize: 44, bold: true, color: WHITE, charSpacing: 1, margin: 0 });
  s.addText("Turning KYC documents into a queryable, per-client knowledge platform", { x: 0.9, y: 3.55, w: 11.2, h: 0.7, fontFace: BODY_FONT, fontSize: 19, color: ICE, margin: 0 });
  s.addText([{ text: "Architecture & System Design", options: { color: WHITE, bold: true } }, { text: "      Executive Briefing   ·   June 2026", options: { color: ICE } }],
    { x: 0.9, y: 5.7, w: 11, h: 0.4, fontFace: BODY_FONT, fontSize: 13, margin: 0 });
  s.addNotes("One-line: we turn a client's raw KYC documents into structured, searchable knowledge that downstream systems can query by client. Agenda: problem, the platform, how each stage works, security, roadmap.");
}

// ============================ SLIDE 2 — THE PROBLEM ============================
{
  const s = pres.addSlide();
  title(s, "The problem: KYC knowledge is locked away");
  const cards = [
    ["Trapped in documents", "Identity, address, ownership and income data arrive as PDFs, scans and images. The knowledge is there, but not usable."],
    ["Downstream can't query it", "Risk, onboarding and review systems need to ask questions about a client. Today they re-read raw files by hand."],
    ["Slow, manual, and risky", "Manual extraction is costly and error-prone, and sending sensitive documents to AI raises real compliance concerns."],
  ];
  const cw = 3.95, gap = 0.4, x0 = (W - (cw * 3 + gap * 2)) / 2;
  cards.forEach((c, i) => {
    const x = x0 + i * (cw + gap);
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 1.95, w: cw, h: 3.8, fill: { color: ICELT }, line: { type: "none" }, rectRadius: 0.12, shadow: shadow() });
    s.addShape(pres.shapes.OVAL, { x: x + 0.35, y: 2.3, w: 0.6, h: 0.6, fill: { color: NAVY }, line: { type: "none" } });
    s.addText(String(i + 1), { x: x + 0.35, y: 2.3, w: 0.6, h: 0.6, align: "center", valign: "middle", color: WHITE, bold: true, fontFace: BODY_FONT, fontSize: 18, margin: 0 });
    s.addText(c[0], { x: x + 0.35, y: 3.1, w: cw - 0.7, h: 0.7, fontFace: TITLE_FONT, fontSize: 18, bold: true, color: NAVY, margin: 0, valign: "top" });
    s.addText(c[1], { x: x + 0.35, y: 3.85, w: cw - 0.7, h: 1.7, fontFace: BODY_FONT, fontSize: 13.5, color: INK, margin: 0, valign: "top" });
  });
  footer(s, 2);
  s.addNotes("Set up the pain: the bank already has the documents; the value is locked. Three angles — trapped data, no machine access, manual + compliance risk.");
}

// ============================ SLIDE 3 — THE SOLUTION ============================
{
  const s = pres.addSlide();
  title(s, "The solution: one platform, document in → knowledge out");
  placeContain(s, "value-chain", 0.6, 1.75, 12.1, 2.4);
  s.addText("Every document is OCR'd, classified safely, extracted, organized into a per-client knowledge tree, and served to downstream systems through one API — with full provenance on every fact.", {
    x: 1.4, y: 4.7, w: W - 2.8, h: 1.4, fontFace: BODY_FONT, fontSize: 17, color: INK, align: "center", valign: "top", margin: 0,
  });
  footer(s, 3);
  s.addNotes("Elevator pitch with the value chain. Emphasize 'one API' and 'provenance'. Everything that follows is how each stage works.");
}

// ============================ SLIDE 4 — OUTCOMES ============================
{
  const s = pres.addSlide();
  title(s, "What it delivers");
  const stats = [
    ["0", "sensitive IDs sent to AI", "Passports, SSNs and CURPs are extracted locally and never leave the boundary."],
    ["Millions", "of clients, isolated", "Each client's data is partitioned and access-controlled in the database."],
    ["US·CA·MX", "EN + ES", "North-American KYC documents across two languages, out of the box."],
    ["100%", "facts carry provenance", "Every extracted value links to its source page and verification status."],
  ];
  const cw = 2.95, gap = 0.33, x0 = (W - (cw * 4 + gap * 3)) / 2;
  stats.forEach((st, i) => {
    const x = x0 + i * (cw + gap);
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 1.95, w: cw, h: 4.0, fill: { color: WHITE }, line: { color: ICE, width: 1 }, rectRadius: 0.1, shadow: shadow() });
    s.addText(st[0], { x: x + 0.15, y: 2.25, w: cw - 0.3, h: 1.05, fontFace: TITLE_FONT, fontSize: st[0].length > 4 ? 28 : 40, bold: true, color: GOLD, align: "center", margin: 0, valign: "middle" });
    s.addText(st[1], { x: x + 0.2, y: 3.4, w: cw - 0.4, h: 0.6, fontFace: BODY_FONT, fontSize: 14, bold: true, color: NAVY, align: "center", margin: 0 });
    s.addText(st[2], { x: x + 0.25, y: 4.05, w: cw - 0.5, h: 1.7, fontFace: BODY_FONT, fontSize: 12.5, color: MUTED, align: "center", valign: "top", margin: 0 });
  });
  footer(s, 4);
  s.addNotes("Headline outcomes management cares about: compliance (0 IDs to AI), scale, coverage, auditability. These map to the security and architecture slides.");
}

// ============================ DIAGRAM SLIDES ============================
diagramSlide(5, "System architecture", "architecture",
  ["Upload → OCR → PII-safe gate → extraction → knowledge tree → isolated store → serving API.",
   "The gate decides what is safe for AI; sensitive IDs are extracted on-box and never leave the boundary.",
   "Model access (embeddings, LLM, rerank) is delegated to a shared gateway — no AI credentials live in this platform."],
  "Walk left to right. Stress the gate in the middle: it decides what is safe to send to AI. Storage is isolated per client. Downstream consumers only touch the serving API.", "wide");

diagramSlide(6, "The differentiator: the knowledge subtree", "subtree",
  ["One structure per document — classification, extracted facts, and searchable representations.",
   "Organized per client: document type → version → facts, built to be traversed by any downstream service or agent.",
   "Every node is semantic, linked, and carries its source."],
  "This is the novel part and the moat. A document becomes a small tree of knowledge that is both human- and machine-navigable, with provenance on every leaf.", "wide");

diagramSlide(7, "PII-safe by design", "gate",
  ["Documents are classified locally before any AI sees them — a policy gate decides: extract on-box, or allow to the AI model.",
   "Sensitive IDs (passport, SSN, CURP) stay fully local. Fails safe: when unsure, it keeps data in-house."],
  "Compliance is the headline. The gate is the control that lets the bank use AI without exposing regulated PII. Default-deny posture.", "wide");

diagramSlide(8, "Any document, any format", "ocr",
  ["PDF, Word, PNG and JPEG — all supported.",
   "Production OCR uses Azure Computer Vision Read.",
   "A drop-in local mock lets us run and test fully offline.",
   "Never fails hard — always degrades gracefully."],
  "Operationally important: onboard whatever format clients send. The same code talks to real Azure or a local stand-in, so development and testing are not blocked on credentials.", "rail");

diagramSlide(9, "Extraction: deterministic + AI", "extraction",
  ["Fixed-format IDs are parsed and checksum-verified — no AI, no errors.",
   "Variable documents use AI for adaptive extraction.",
   "Each fact records how it was verified and how confident we are.",
   "Deterministic always runs; AI only when the gate allows."],
  "Two engines. Deterministic gives trustworthy, audited ID fields. AI handles the messy long tail. The combination is both safe and broad.", "rail");

diagramSlide(10, "Search & serving", "search",
  ["Downstream asks questions scoped to one client.",
   "Hybrid search: keyword + meaning + document structure.",
   "Answers come back grounded in the source document.",
   "Optional masking redacts sensitive values by caller."],
  "What downstream teams actually consume: one scoped, grounded, access-aware API. Masking can be toggled per caller clearance.", "rail");

diagramSlide(11, "Always current: merge & versioning", "merge",
  ["Facts from all of a client's documents are consolidated.",
   "Conflicts are flagged for review, not silently overwritten.",
   "Re-uploads create immutable versions; nothing is lost.",
   "A change feed surfaces what is new for periodic review."],
  "Re-KYC and periodic review get this for free. The client view stays current across many documents, and history is preserved for audit.", "rail");

diagramSlide(12, "Data model", "datamodel",
  ["Seven tables: documents, versions, knowledge nodes, search representations, merged facts, entities, audit.",
   "Isolated and access-controlled per client.",
   "Tuned for fast lookup by client and fast semantic search."],
  "Keep this brief for management — it shows the design is real and rigorous. Point at the per-client isolation and the audit table.", "rail");

// ============================ SLIDE 13 — SECURITY & COMPLIANCE ============================
{
  const s = pres.addSlide();
  s.background = { color: NAVY };
  s.addText("Security & compliance, built in", { x: 0.6, y: 0.5, w: 12, h: 0.8, fontFace: TITLE_FONT, fontSize: 30, bold: true, color: WHITE, margin: 0 });
  const items = [
    ["Tenant isolation", "Every record is scoped to one client and enforced in the database — a forgotten filter cannot leak another client's data."],
    ["PII stays local", "Sensitive documents are extracted on-box and never sent to external AI; default-deny when uncertain."],
    ["Masking on demand", "The same data can be served full or redacted, depending on the caller's clearance."],
    ["Provenance & audit", "Every fact links to its source; every gate decision is logged for compliance review."],
  ];
  const cw = 5.9, ch = 2.15, gx = 0.5, x0 = (W - (cw * 2 + gx)) / 2, y0 = 1.65;
  items.forEach((it, i) => {
    const x = x0 + (i % 2) * (cw + gx), y = y0 + Math.floor(i / 2) * (ch + 0.4);
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: cw, h: ch, fill: { color: NAVY2 }, line: { type: "none" }, rectRadius: 0.1, shadow: shadow() });
    s.addText(it[0], { x: x + 0.35, y: y + 0.25, w: cw - 0.7, h: 0.5, fontFace: TITLE_FONT, fontSize: 19, bold: true, color: ICE, margin: 0 });
    s.addText(it[1], { x: x + 0.35, y: y + 0.82, w: cw - 0.7, h: 1.2, fontFace: BODY_FONT, fontSize: 14, color: WHITE, margin: 0, valign: "top" });
  });
  s.addNotes("For the risk/compliance stakeholders. Four controls: isolation, no-egress for PII, masking, audit. Tie back to the gate slide.");
}

// ============================ SLIDE 14 — SCALE & TECH ============================
{
  const s = pres.addSlide();
  title(s, "Scale & technology");
  s.addText("Designed for millions of clients", { x: 0.7, y: 1.55, w: 11, h: 0.6, fontFace: BODY_FONT, fontSize: 18, bold: true, color: NAVY, margin: 0 });
  bullets(s, [
    "Per-client partitioning keeps every lookup fast as the corpus grows.",
    "Semantic search is delegated to a shared, proven model gateway.",
    "Runs as containers; one command brings the whole stack up locally.",
  ], { x: 0.7, y: 2.2, w: 11.5, h: 2.4, fontSize: 16 });
  s.addText("Built on", { x: 0.7, y: 4.95, w: 4, h: 0.4, fontFace: BODY_FONT, fontSize: 13, bold: true, color: MUTED, margin: 0 });
  const chips = ["FastAPI", "PostgreSQL", "pgvector", "ltree", "Azure Vision Read v3.2", "Docker", "Python"];
  let cx = 0.7; const cy = 5.45;
  chips.forEach((c) => {
    const cwid = 0.5 + c.length * 0.11;
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: cx, y: cy, w: cwid, h: 0.5, fill: { color: ICELT }, line: { color: ICE, width: 1 }, rectRadius: 0.25 });
    s.addText(c, { x: cx, y: cy, w: cwid, h: 0.5, align: "center", valign: "middle", fontFace: BODY_FONT, fontSize: 12, color: NAVY, margin: 0 });
    cx += cwid + 0.25;
  });
  footer(s, 14);
  s.addNotes("Reassure on scale and that the stack is standard, proven technology — low operational risk.");
}

// ============================ SLIDE 15 — ROADMAP ============================
{
  const s = pres.addSlide();
  title(s, "Status & what's next");
  const col = (x, head, color, rows) => {
    s.addText(head, { x, y: 1.6, w: 5.7, h: 0.5, fontFace: TITLE_FONT, fontSize: 20, bold: true, color, margin: 0 });
    bullets(s, rows, { x, y: 2.25, w: 5.7, h: 4.4, fontSize: 15 });
  };
  col(0.7, "Built today", NAVY, [
    "End-to-end pipeline running in containers",
    "Multi-format OCR with local + Azure paths",
    "PII-safe gate + deterministic extraction",
    "Knowledge tree, search, and serving API",
    "Full documentation suite (with diagrams)",
  ]);
  col(7.0, "Next", GOLD, [
    "Connect the production Azure OCR resource",
    "Deploy the shared model-gateway endpoints",
    "Train the classifier on real document samples",
    "Pilot with a live client document set",
  ]);
  footer(s, 15);
  s.addNotes("Honest about built vs pending. 'Next' items are mostly external enablement (credentials, gateway, sample data), not core engineering.");
}

// ============================ SLIDE 16 — CLOSING ============================
{
  const s = pres.addSlide();
  s.background = { color: NAVY };
  s.addShape(pres.shapes.OVAL, { x: -1.5, y: -1.5, w: 3.6, h: 3.6, fill: { color: NAVY2 }, line: { type: "none" } });
  s.addShape(pres.shapes.OVAL, { x: W - 2.2, y: H - 2.2, w: 3.6, h: 3.6, fill: { color: NAVY2 }, line: { type: "none" } });
  s.addText("From documents to decisions", { x: 1.0, y: 2.7, w: 11.3, h: 1.0, fontFace: TITLE_FONT, fontSize: 40, bold: true, color: WHITE, margin: 0 });
  s.addText("A unified, PII-safe document-intelligence platform that makes every client's KYC knowledge instantly queryable — with the provenance and isolation a bank requires.", {
    x: 1.0, y: 3.9, w: 11.0, h: 1.3, fontFace: BODY_FONT, fontSize: 18, color: ICE, margin: 0, valign: "top",
  });
  s.addNotes("Close on the value: queryable client knowledge + bank-grade safety. Invite questions; point to the roadmap for what unlocks production.");
}

pres.writeFile({ fileName: path.join(__dirname, "Document-Intelligence-Executive-Briefing.pptx") })
  .then((f) => console.log("WROTE", f))
  .catch((e) => { console.error("FAILED", e); process.exit(1); });
