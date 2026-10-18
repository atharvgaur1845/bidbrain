// Films the submission video: one continuous take of the real app, with title / chapter cards, a caption bar,
// a visible cursor and labelled highlight boxes around each feature. Everything shown is live backend output.
//
//   cd scripts && CHROME=/usr/bin/google-chrome node film_demo.mjs      # BASE=http://localhost:7860 by default
//   -> scripts/videos/film/*.webm, then converted to docs/demo/bidbrain_demo.mp4 (needs ffmpeg on PATH)
//
// Sign-in for the company-memory chapter reads BIDBRAIN_DEMO_SECRET_MANAGER from ../.env and types it into the
// Sign in form (masked field); it is never printed. The promoted rule is retired at the end.
import { chromium } from "playwright";
import { mkdirSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const BASE = (process.env.BASE || "http://localhost:7860").replace(/\/$/, "");
const VDIR = join(HERE, "videos", "film");
const OUT = process.env.OUT || join(HERE, "..", "docs", "demo", "bidbrain_demo.mp4");
const W = 1440, H = 900;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const t0 = Date.now();
const log = (m) => console.log(`${((Date.now() - t0) / 1000).toFixed(1).padStart(6)}s  ${m}`);

function envKey(name) {
  if (process.env[name]) return process.env[name];
  try {
    for (const line of readFileSync(join(HERE, "..", ".env"), "utf8").split("\n")) {
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m && m[1] === name) return m[2].replace(/^["']|["']$/g, "");
    }
  } catch {}
  return "";
}

// ---------------------------------------------------------------- overlay helpers (re-injected after navigation)
const CSS = `
#fx-cap{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:2147483000;max-width:1180px;
  display:flex;gap:12px;align-items:center;padding:12px 18px;border-radius:12px;background:rgba(15,23,42,.94);
  color:#fff;font:500 17px/1.45 Inter,system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.3);pointer-events:none;
  transition:opacity .3s}
#fx-cap b{flex:none;font:700 12px/1 Inter,system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;
  background:#3b5bdb;padding:6px 9px;border-radius:6px}
.fx-box{position:fixed;z-index:2147482999;border:3px solid #f59f00;border-radius:10px;pointer-events:none;
  box-shadow:0 0 0 4000px rgba(15,23,42,.18),0 0 18px rgba(245,159,0,.7);animation:fxp 1.4s ease-in-out infinite}
.fx-tag{position:fixed;z-index:2147483001;background:#f59f00;color:#111;font:700 14px/1.2 Inter,system-ui,sans-serif;
  padding:6px 10px;border-radius:7px;pointer-events:none;box-shadow:0 4px 12px rgba(0,0,0,.25);max-width:420px}
@keyframes fxp{0%,100%{box-shadow:0 0 0 4000px rgba(15,23,42,.18),0 0 10px rgba(245,159,0,.5)}
  50%{box-shadow:0 0 0 4000px rgba(15,23,42,.18),0 0 26px rgba(245,159,0,.95)}}
#fx-cur{position:fixed;z-index:2147483002;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;
  background:rgba(59,91,219,.35);border:2px solid #3b5bdb;pointer-events:none;transition:left .55s ease,top .55s ease}
#fx-cur.clk{animation:fxc .35s ease}
@keyframes fxc{50%{transform:scale(1.8);background:rgba(59,91,219,.6)}}
#fx-card{position:fixed;inset:0;z-index:2147483003;display:flex;flex-direction:column;align-items:center;
  justify-content:center;background:linear-gradient(135deg,#0f172a,#1e3a8a);color:#fff;font-family:Inter,system-ui,sans-serif;
  text-align:center;padding:60px}
#fx-card .k{font:700 15px/1 Inter,system-ui;letter-spacing:.18em;text-transform:uppercase;color:#93c5fd;margin-bottom:22px}
#fx-card h1{font:800 54px/1.1 Inter,system-ui;margin:0 0 18px;max-width:1100px}
#fx-card p{font:400 22px/1.5 Inter,system-ui;color:#cbd5e1;max-width:980px;margin:0}
#fx-card ul{margin:26px 0 0;padding:0;list-style:none;font:500 19px/1.9 Inter,system-ui;color:#e2e8f0}
`;
let cur = { x: W / 2, y: H / 2 };
async function ensure(page) {
  await page.evaluate(([css, x, y]) => {
    if (!document.getElementById("fx-css")) {
      const s = document.createElement("style"); s.id = "fx-css"; s.textContent = css; document.head.appendChild(s);
    }
    if (!document.getElementById("fx-cur")) {
      const c = document.createElement("div"); c.id = "fx-cur"; c.style.left = x + "px"; c.style.top = y + "px";
      document.body.appendChild(c);
    }
  }, [CSS, cur.x, cur.y]);
}
async function cap(page, chapter, text) {
  await ensure(page);
  await page.evaluate(([chapter, text]) => {
    let el = document.getElementById("fx-cap");
    if (!el) { el = document.createElement("div"); el.id = "fx-cap"; document.body.appendChild(el); }
    el.innerHTML = ""; const b = document.createElement("b"); b.textContent = chapter;
    const s = document.createElement("span"); s.textContent = text; el.append(b, s);
  }, [chapter, text]);
}
async function card(page, kicker, title, sub, bullets = [], ms = 4200) {
  await page.evaluate(([k, t, s, bl]) => {
    document.getElementById("fx-card")?.remove();
    const d = document.createElement("div"); d.id = "fx-card";
    const e = (tag, txt) => { const x = document.createElement(tag); x.textContent = txt; return x; };
    d.append(e("div", k), e("h1", t), e("p", s));
    d.firstChild.className = "k";
    if (bl.length) { const u = document.createElement("ul"); bl.forEach((b) => u.append(e("li", b))); d.append(u); }
    document.body.appendChild(d);
  }, [kicker, title, sub, bullets]);
  await ensure(page);
  await sleep(ms);
  await page.evaluate(() => document.getElementById("fx-card")?.remove());
}
async function unmark(page) {
  await page.evaluate(() => document.querySelectorAll(".fx-box,.fx-tag").forEach((e) => e.remove())).catch(() => {});
}
async function mark(page, loc, label, ms = 2600, { keep = false } = {}) {
  try {
    await loc.scrollIntoViewIfNeeded({ timeout: 8000 });
    await sleep(250);
    const b = await loc.boundingBox();
    if (!b) return;
    await ensure(page);
    await page.evaluate(([b, label, W, H]) => {
      const p = 6, x = Math.max(4, b.x - p), y = Math.max(4, b.y - p);
      const w = Math.min(W - x - 4, b.width + 2 * p), h = Math.min(H - y - 4, b.height + 2 * p);
      const box = document.createElement("div"); box.className = "fx-box";
      Object.assign(box.style, { left: x + "px", top: y + "px", width: w + "px", height: h + "px" });
      const t = document.createElement("div"); t.className = "fx-tag"; t.textContent = label;
      const above = y > 44;
      Object.assign(t.style, { left: Math.min(x, W - 440) + "px", top: (above ? y - 38 : Math.min(y + h + 8, H - 40)) + "px" });
      document.body.append(box, t);
    }, [b, label, W, H]);
    await sleep(ms);
    if (!keep) await unmark(page);
  } catch (e) { log(`mark skipped (${label}): ${String(e).split("\n")[0]}`); }
}
async function moveTo(page, loc) {
  await loc.scrollIntoViewIfNeeded({ timeout: 15000 });
  const b = await loc.boundingBox();
  if (!b) return;
  cur = { x: b.x + b.width / 2, y: b.y + b.height / 2 };
  await ensure(page);
  await page.evaluate(([x, y]) => { const c = document.getElementById("fx-cur"); c.style.left = x + "px"; c.style.top = y + "px"; }, [cur.x, cur.y]);
  await sleep(650);
}
async function click(page, loc) {
  await moveTo(page, loc);
  await page.evaluate(() => { const c = document.getElementById("fx-cur"); c.classList.remove("clk"); void c.offsetWidth; c.classList.add("clk"); });
  await sleep(200);
  await loc.click();
}
async function typeSlow(page, loc, text) {
  await click(page, loc);
  await loc.pressSequentially(text, { delay: 28 });
}
async function go(page, path) {
  await page.goto(`${BASE}${path}`);
  await page.waitForLoadState("networkidle").catch(() => {});
  await ensure(page);
}
async function chapter(name, fn) {
  try { await fn(); log(`chapter ok: ${name}`); }
  catch (e) { log(`chapter FAILED: ${name}: ${String(e).split("\n")[0]}`); }
}
async function fire(page, title) {
  const done = page.waitForResponse((r) => r.url().includes("/fire") && r.request().method() === "POST", { timeout: 60000 });
  const li = page.locator("li", { hasText: title }).first();
  await mark(page, li, "Incoming document", 1400);
  await click(page, li.getByRole("button", { name: "Send" }));
  await (await done).finished();
  await sleep(1500);
}

// ---------------------------------------------------------------- the film
rmSync(VDIR, { recursive: true, force: true });
mkdirSync(VDIR, { recursive: true });
const browser = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME } : {});
const ctx = await browser.newContext({ viewport: { width: W, height: H }, colorScheme: "light",
  recordVideo: { dir: VDIR, size: { width: W, height: H } } });
const page = await ctx.newPage();
page.setDefaultTimeout(60000);
let promoted = null;
try {
  await go(page, "/");
  await page.waitForFunction(() => !!localStorage.getItem("bidbrain.session"));
  await card(page, "Graph Hacks · FalkorDB", "BidBrain: the bid desk's company brain",
    "Every certificate, CV, bond and corrigendum a construction firm owns, as one FalkorDB graph that agents qualify tenders on, prove absences with, and keep current as documents change.",
    ["Company Brain · Agents That Act · Agent Memory & Coordination"], 5500);

  // 1 ── Control room
  await chapter("control room", async () => {
    await card(page, "Chapter 1", "Control room", "What needs attention right now, and why: computed from the graph, not hand-written.", [], 2600);
    await go(page, "/");
    await cap(page, "Control room", "One screen for the bid desk: open tenders, what is at risk, pending approvals, live graph stats.");
    await sleep(2500);
    await mark(page, page.locator("main").first(), "Live counts straight from FalkorDB", 2600);
    await page.mouse.wheel(0, 500); await sleep(1800); await page.mouse.wheel(0, -500);
  });

  // 2 ── Multi-agent qualification
  await chapter("qualify", async () => {
    await card(page, "Chapter 2", "Six agents qualify a tender", "A planner routes ClauseParser, EvidenceHunter, AbsenceAuditor, ConflictSentinel, a no-LLM Verifier and the Assembler over one shared case graph.", [], 3600);
    await go(page, "/qualify/");
    await cap(page, "Qualify", "Pick a real-shaped tender: ₹250 Cr expressway package. Its eligibility text is on the left.");
    const t = page.getByText("4-laning of Greenfield Expressway").first();
    await click(page, t);
    await mark(page, page.getByText("Eligibility text").first().locator(".."), "Tender eligibility clauses", 2600);
    const run = page.getByRole("button", { name: /Run agents/ }).first();
    await cap(page, "Qualify", "Run the agents. Each one reads and writes the same case subgraph in FalkorDB: that is their hand-off.");
    await click(page, run);
    await page.getByText(/Agents running|Running/).first().waitFor({ timeout: 15000 }).catch(() => {});
    await sleep(4000);
    await mark(page, page.getByText("Agent pipeline").first().locator("..").locator(".."), "Live agent steps (streamed)", 3500);
    await page.getByRole("button", { name: /Run agents/ }).first().waitFor({ timeout: 180000 });
    await sleep(1500);
    await cap(page, "Qualify", "Clause-by-clause verdicts. Arithmetic is exact Python over Cypher results; the Verifier re-checks every PASS without an LLM.");
    await mark(page, page.getByText("Requirement", { exact: true }).first().locator("..").locator(".."), "Verdict per clause, with the credited works", 3800);
    const ev = page.getByRole("button", { name: "View evidence" }).first();
    if (await ev.count()) {
      await cap(page, "Qualify", "Every verdict links to its evidence: the works, the documents and the page each value was read from.");
      await click(page, ev); await sleep(3200);
      await page.keyboard.press("Escape").catch(() => {});
    }
    const memo = page.getByRole("tab", { name: "Memo" }).or(page.getByText("Memo", { exact: true })).first();
    if (await memo.count()) {
      await click(page, memo);
      await cap(page, "Qualify", "The Assembler writes the bid memo, and a checker rejects any figure it cannot trace to the graph.");
      await sleep(3500);
    }
  });

  // 3 ── Live documents
  await chapter("live documents", async () => {
    await card(page, "Chapter 3", "Documents change. The graph keeps up.", "A revised certificate and a client corrigendum arrive. Each becomes a claim, is checked against what we already know, and is settled by policy.", [], 3600);
    await go(page, "/live/");
    await cap(page, "Live documents", "These are the fixed demo documents (DEMO-0, DEMO-1), parsed by the same PDF pipeline as the 687-document corpus.");
    await sleep(1500);
    await fire(page, "Revised Completion Certificate — Six-Lane");
    await cap(page, "Live documents", "Corrigendum: the client revises Ring Road Pkg-113 from ₹200 Cr to ₹188 Cr.");
    await fire(page, "Corrigendum — Ring Road — Uttar Pradesh Pkg-113");
    await page.getByText(/Ripple from/).first().waitFor({ timeout: 30000 }).catch(() => {});
    await mark(page, page.getByText(/Ripple from/).first().locator("..").locator(".."), "Ripple: which open bids this change reaches", 3500);
  });

  // 4 ── Impact room
  await chapter("impact room", async () => {
    await card(page, "Chapter 4", "Change impact", "Source document → claim → decision → requirement → bid → action. One graph traversal, shown as it is stored.", [], 3400);
    await go(page, "/impact/");
    await page.getByText("Dependency graph").first().waitFor();
    await click(page, page.getByText("Corrigendum — Ring Road — Uttar Pradesh Pkg-113").first());
    await cap(page, "Change impact", "The timeline of received documents; select one to trace exactly what it changed.");
    await mark(page, page.getByText("Event timeline").first().locator("..").locator(".."), "Event timeline", 2600);
    await cap(page, "Change impact", "The dependency graph, read from FalkorDB: the new ₹188 Cr claim beats both ₹200 Cr claims (same issuer, newer).");
    await mark(page, page.getByText("Dependency graph").first().locator("..").locator(".."), "Document → claim → decision → clause → bid → action", 4200);
    await click(page, page.getByText("Claim · active").first());
    await cap(page, "Change impact", "Inspector: the selected claim, its authority, its policy and the PDF page it came from.");
    await sleep(3000);
    const vc = page.getByText("Verdict change").first();
    await vc.scrollIntoViewIfNeeded(); await page.mouse.wheel(0, -120);
    await cap(page, "Change impact", "Deterministic requalification: clause 2.1 PASS → FAIL, so the bid flips BID → NO-BID.");
    await mark(page, vc.locator("..").locator(".."), "Before / after: BID → NO-BID", 4000);
  });

  // 5 ── Accountable actions
  await chapter("actions", async () => {
    await card(page, "Chapter 5", "Agents act, people approve", "A verdict change raises an action. Nothing executes until a person approves it, and every step lands in the audit trail.", [], 3400);
    const row = page.locator("li").filter({ hasText: "Re-approve bid" }).last();
    await row.scrollIntoViewIfNeeded(); await page.mouse.wheel(0, 120);
    await cap(page, "Actions", "Pending: re-approve the bid at its new verdict.");
    await mark(page, row, "Pending action", 2200);
    await click(page, row.getByRole("button", { name: "Approve" }));
    await click(page, row.getByRole("button", { name: "Confirm approve" }));
    await row.getByRole("button", { name: /^Execute/ }).waitFor();
    await cap(page, "Actions", "Approved. Execution is deterministic code: it re-verifies every clause against the graph first.");
    await click(page, row.getByRole("button", { name: /^Execute/ }));
    await click(page, row.getByRole("button", { name: "Confirm execute" }));
    await page.getByText(/re-verified \d+ clause/).first().waitFor();
    await mark(page, page.getByText(/re-verified \d+ clause/).first(), "Executed, re-verified, audited", 3200);
  });

  // 6 ── Provenance
  await chapter("provenance", async () => {
    await page.evaluate(() => window.scrollTo(0, 0)); await sleep(600);
    await cap(page, "Provenance", "Every fact is traceable to the exact page of the source PDF.");
    await click(page, page.getByRole("button", { name: "Open PDF" }).first());
    await page.locator("iframe").first().waitFor();
    await sleep(4200);
    await page.keyboard.press("Escape");
  });

  // 7 ── Decisions
  await chapter("decisions", async () => {
    await card(page, "Chapter 6", "Why was this decided?", "Every settled fact keeps its policy, its decider and both source documents. Decisions supersede decisions, never overwrite them.", [], 3200);
    await go(page, "/decisions/");
    await cap(page, "Decisions", "The decision log: policy, settled value, decided by, and what it supersedes.");
    await sleep(1500);
    const first = page.getByText(/^DEC-/).first();
    if (await first.count()) await click(page, first);
    await mark(page, page.getByText("Trace").first().locator("..").locator(".."), "Decision trace down to documents", 3800);
  });

  // 8 ── Company memory
  await chapter("company memory", async () => {
    await card(page, "Chapter 7", "Teach it once. The company remembers.", "An ambiguous conflict goes to a human. Their choice becomes a rule, promoted to company memory by a signed-in manager, and other workspaces reuse it.", [], 3800);
    await go(page, "/live/");
    await fire(page, "JV Experience Certificate — Residential Quarters");
    const jv = page.locator(".well").filter({ hasText: "JV Partner" }).first();
    await jv.waitFor();
    await cap(page, "Company memory", "Similar authority on both sides (JV lead says 'JV Partner', our portfolio says 'Prime'), so it asks a person.");
    await mark(page, page.getByText("Needs your judgement").first().locator("..").locator(".."), "Resolve card: needs human judgement", 3400);
    await click(page, jv.getByRole("button", { name: "Choose and learn rule" }));
    await page.getByRole("button", { name: /Promote to company memory/ }).first().waitFor();
    const secret = envKey("BIDBRAIN_DEMO_SECRET_MANAGER");
    if (!secret) throw new Error("BIDBRAIN_DEMO_SECRET_MANAGER missing");
    await cap(page, "Company memory", "Sharing a rule company-wide needs a server-verified identity: sign in as the bid manager.");
    await click(page, page.getByRole("button", { name: /^Sign in$/ }).first());
    const dlg = page.getByRole("dialog", { name: "Sign in" });
    await typeSlow(page, dlg.getByLabel("Principal name"), "manager");
    await click(page, dlg.getByLabel("Secret"));
    await dlg.getByLabel("Secret").fill(secret);
    await click(page, dlg.getByRole("button", { name: "Sign in" }));
    await page.getByRole("button", { name: /^manager$/ }).waitFor();
    const prom = page.waitForResponse((r) => r.url().includes("/api/memory/promote"));
    await click(page, page.getByRole("button", { name: /Promote to company memory/ }).first());
    promoted = (await (await prom).json()).org_rule || null;
    await page.getByText("In company memory").first().waitFor();
    await cap(page, "Company memory", `Promoted as ${promoted}, with the manager recorded as its owner in the audit trail.`);
    await mark(page, page.getByText("In company memory").first().locator(".."), "Now in company memory", 3200);

    // a new teammate: fresh session, signed out
    await page.evaluate(() => { localStorage.removeItem("bidbrain.session"); });
    await page.getByRole("button", { name: /^manager$/ }).click().catch(() => {});
    const so = page.getByRole("menuitem", { name: /Sign out/ }).or(page.getByRole("button", { name: /Sign out/ })).first();
    if (await so.count()) await so.click().catch(() => {});
    await go(page, "/live/");
    await page.waitForFunction(() => !!localStorage.getItem("bidbrain.session"));
    await card(page, "Meanwhile…", "A teammate opens a fresh workspace", "Separate session graph (GRAPH.COPY), nobody signed in.", [], 2600);
    const sw = page.getByRole("switch").first();
    await page.getByText(/^Off: only this session/).first().waitFor();
    await cap(page, "Company memory", "Company memory is opt-in per workspace. Switch it on.");
    await click(page, sw);
    await page.getByText(/^On: company rules/).first().waitFor();
    await fire(page, "JV Experience Certificate — Institutional Building");
    await page.getByText("Auto-resolved by company memory").first().waitFor();
    await cap(page, "Company memory", `A similar conflict resolves itself, citing ${promoted}. No card, no human, full trace.`);
    await mark(page, page.getByText("Auto-resolved by company memory").first().locator("..").locator(".."), "Reused company rule", 4000);
  });

  // 9 ── Ask (grounded Q&A, absence proof vs vector RAG)
  await chapter("ask", async () => {
    await card(page, "Chapter 8", "Ask anything, get proof", "A tool-using agent answers multi-hop questions over the graph, and refuses to claim a negative it cannot prove.", [], 3400);
    await go(page, "/ask/");
    const box = page.getByLabel("Your question");
    await typeSlow(page, box, "Which of Trishakti Power Generation Corporation's completed works have no client reference letter on file?");
    await page.keyboard.press("Enter");
    await cap(page, "Ask", "Absence question: the agent proves what is missing over the client's full portfolio, not from an empty search.");
    await page.getByText("Answer").first().waitFor({ timeout: 120000 });
    await page.getByRole("button", { name: /Compare with vector RAG/ }).first().waitFor({ timeout: 120000 }).catch(() => {});
    await sleep(2000);
    await mark(page, page.getByText("Answer").last().locator("..").locator(".."), "Grounded answer with its tool trace and documents", 4500);
    const cmp = page.getByRole("button", { name: /Compare with vector RAG/ }).first();
    if (await cmp.count()) {
      await cap(page, "Ask", "Same model, same question, vector RAG over the same chunks: compare.");
      await click(page, cmp);
      await page.getByText(/Running vector RAG/).first().waitFor({ state: "detached", timeout: 120000 }).catch(() => {});
      await sleep(4500);
    }
  });

  // 10 ── Explore
  await chapter("explore", async () => {
    await card(page, "Chapter 9", "The graph itself", "Search any entity, expand neighbourhoods, and run FalkorDB's built-in algorithms.", [], 3000);
    await go(page, "/explore/");
    await cap(page, "Explore", "Informal names resolve: type 'phed odisha', get the Public Health Engineering Department, Odisha.");
    const s = page.getByLabel("Search entities");
    await typeSlow(page, s, "phed odisha");
    await sleep(2500);
    const hit = page.getByText(/Public Health Engineering.*Odisha|Odisha.*Public Health/i).first();
    if (await hit.count()) await click(page, hit).catch(() => {});
    else { await page.keyboard.press("ArrowDown").catch(() => {}); await page.keyboard.press("Enter").catch(() => {}); }
    await sleep(3000);
    await mark(page, page.locator("canvas").first(), "Neighbourhood, live from FalkorDB", 3000);
    const bf = page.getByRole("button", { name: /Bus factor/ }).first();
    if (await bf.count()) {
      await cap(page, "Explore", "Bus factor: algo.betweenness finds the people every client relationship runs through.");
      await click(page, bf); await sleep(4500);
    }
    const cc = page.getByRole("button", { name: /Components/ }).first();
    if (await cc.count()) {
      await cap(page, "Explore", "Components (algo.WCC) checks evidence integrity: every work connects to its proof.");
      await click(page, cc); await sleep(4000);
    }
  });

  // 11 ── Benchmark
  await chapter("benchmark", async () => {
    await card(page, "Chapter 10", "Does the graph help? Measured.", "Same model, same database, four context strategies, five question sets.", [], 3000);
    await go(page, "/benchmark/");
    await cap(page, "Benchmark", "BidBrain vs text-to-Cypher, vector RAG and GraphRAG-SDK. Two runs reported; caveats are on the page.");
    await sleep(2500);
    await mark(page, page.locator("main table").first(), "Accuracy per question set", 4500);
    await page.mouse.wheel(0, 700); await sleep(3500);
  });

  await card(page, "BidBrain", "The graph does the work.",
    "Claims with authority and time · proofs of absence · conflict policy · blast radius · accountable actions · company memory.",
    ["Live: https://atharvgaur18-bidbrain.hf.space", "Code: github.com/atharvgaur1845/bidbrain"], 6000);
} finally {
  if (promoted) {
    // leave company memory as it was: sign in again as the owner (secret passed in, never printed) and retire it
    const st = await page.evaluate(async ([id, secret]) => {
      const h = { "Content-Type": "application/json" };
      const l = await fetch("/api/auth/login", { method: "POST", headers: h, body: JSON.stringify({ name: "manager", secret }) });
      const tok = (await l.json()).token;
      const s = localStorage.getItem("bidbrain.session");
      const r = await fetch(`/api/memory/org/${id}/retire`, { method: "POST",
        headers: { ...h, ...(tok ? { Authorization: `Bearer ${tok}` } : {}), ...(s ? { "X-Session": s } : {}) }, body: "{}" });
      return r.status;
    }, [promoted, envKey("BIDBRAIN_DEMO_SECRET_MANAGER")]).catch((e) => String(e).split("\n")[0]);
    log(`retired ${promoted}: ${st}`);
  }
  await ctx.close();
  await browser.close();
}

const webm = readdirSync(VDIR).filter((f) => f.endsWith(".webm")).map((f) => join(VDIR, f))[0];
mkdirSync(dirname(OUT), { recursive: true });
execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", webm, "-c:v", "libx264", "-preset", "medium", "-crf", "22",
  "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUT]);
log(`video: ${OUT}`);
