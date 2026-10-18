// Drives the live app through the demo sequence in docs/DEMO_RUNBOOK.md and captures one numbered
// 1440x900 screenshot per step into docs/demo/ (and, with VIDEO=1, a .webm per workspace into scripts/videos/).
//
//   cd scripts && npm i                                   # playwright only; no browser download needed with CHROME
//   CHROME=/usr/bin/google-chrome node record_demo.mjs    # BASE=http://localhost:7860 by default
//   VIDEO=1 CHROME=/usr/bin/google-chrome node record_demo.mjs   # also record video (needs Playwright's ffmpeg:
//                                                                #   npx playwright install ffmpeg)
//
// The core path (steps 1-7) uses no LLM: the tender is qualified with use_llm=false and the documents are the fixed
// DEMO-0 / DEMO-1 fixtures. Step 8 promotes a company rule, which needs a signed-in principal: the script reads
// BIDBRAIN_DEMO_SECRET_MANAGER from ../.env (written by scripts/make_demo_principals.py), types it into the
// Sign in form, and never prints or saves it. Afterwards it retires the rule it promoted (as the same principal),
// so the shared company memory is left as it was. Sessions are left to expire (SESSION_TTL).
import { chromium } from "playwright";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const BASE = (process.env.BASE || "http://localhost:7860").replace(/\/$/, "");
const OUT = process.env.OUT || join(HERE, "..", "docs", "demo");
const VIDEO = !!process.env.VIDEO;
const PAUSE = Number(process.env.PAUSE ?? (VIDEO ? 2500 : 600));   // narration room when filming
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
mkdirSync(OUT, { recursive: true });

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

let n = 0;
const t0 = Date.now();
async function caption(page, text) {
  await page.evaluate((text) => {
    let el = document.getElementById("demo-caption");
    if (!el) {
      el = document.createElement("div");
      el.id = "demo-caption";
      el.style.cssText = "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:9999;max-width:1100px;" +
        "padding:8px 14px;border-radius:8px;background:rgba(17,24,39,.92);color:#fff;font:500 14px/1.4 system-ui,sans-serif;" +
        "box-shadow:0 4px 16px rgba(0,0,0,.25);pointer-events:none";
      document.body.appendChild(el);
    }
    el.textContent = text;
  }, text);
}
async function shot(page, name, text) {
  if (text) await caption(page, text);
  await sleep(PAUSE);
  n += 1;
  const file = join(OUT, `${String(n).padStart(2, "0")}-${name}.png`);
  await page.screenshot({ path: file });
  console.log(`${((Date.now() - t0) / 1000).toFixed(1).padStart(6)}s  ${file.split("/").slice(-2).join("/")}`);
}
const sid = (page) => page.evaluate(() => localStorage.getItem("bidbrain.session"));

async function newWorkspace(browser, label) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: "light",
    ...(VIDEO ? { recordVideo: { dir: join(HERE, "videos", label), size: { width: 1440, height: 900 } } } : {}) });
  const page = await ctx.newPage();
  page.setDefaultTimeout(60000);
  await page.goto(`${BASE}/`);
  await page.waitForFunction(() => !!localStorage.getItem("bidbrain.session"));
  return { ctx, page };
}

async function signIn(page, name, secret) {
  await page.getByRole("button", { name: /^Sign in$/ }).first().click();
  const dlg = page.getByRole("dialog", { name: "Sign in" });
  await dlg.getByLabel("Principal name").fill(name);
  await dlg.getByLabel("Secret").fill(secret);
  await dlg.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: new RegExp(`^${name}$`) }).waitFor();
}

async function fireFromInbox(page, title) {
  const done = page.waitForResponse((r) => r.url().includes("/fire") && r.request().method() === "POST");
  await page.locator("li", { hasText: title }).getByRole("button", { name: "Send" }).first().click();
  await (await done).finished();
  await sleep(800);
}

const browser = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME } : {});
let promoted = null, A = null;
try {
  // ------------------------------------------------------------------ workspace A
  A = await newWorkspace(browser, "workspace-a");
  const page = A.page;
  // qualify T-NHX-2026-014 with rules only (use_llm=false), as the Change impact demo runner does
  const verdict = await page.evaluate(async (s) => {
    const r = await fetch("/api/qualify", { method: "POST", headers: { "Content-Type": "application/json", "X-Session": s },
      body: JSON.stringify({ tender_id: "T-NHX-2026-014", use_llm: false }) });
    await r.text();
    const c = await (await fetch("/api/cases", { headers: { "X-Session": s } })).json();
    return c.map((x) => x.verdict).join(",");
  }, await sid(page));
  console.log("qualified T-NHX-2026-014 (no LLM):", verdict);

  // 1. Control room with the tender at risk
  await page.goto(`${BASE}/`);
  await page.getByText("4-laning of Greenfield Expressway").first().waitFor();
  await shot(page, "control-room", "1 · Control room: T-NHX-2026-014 qualified BID (rules only, no LLM). Clause 2.1 rests on one work: Ring Road Pkg-113, ₹200 Cr.");

  // 2. Amendments arrive: the DEMO-0 / DEMO-1 fixtures
  await page.goto(`${BASE}/live/`);
  await page.locator("li", { hasText: "Revised Completion Certificate — Six-Lane" }).first().waitFor();
  await shot(page, "fixtures-inbox", "2 · Live documents → Scenarios: DEMO-0 and DEMO-1 are fixed demo fixtures (synthetic documents), sent in order.");
  await fireFromInbox(page, "Revised Completion Certificate — Six-Lane");
  await fireFromInbox(page, "Corrigendum — Ring Road — Uttar Pradesh Pkg-113");
  await page.getByText(/Ripple from/).first().waitFor().catch(() => {});
  await shot(page, "fixtures-sent", "2 · Fixture DEMO-0 (revised certificate) then fixture DEMO-1 (corrigendum: Pkg-113 ₹200 Cr → ₹188 Cr) received.");

  // 3. Conflict detection and the evidence behind the selected claim
  await page.goto(`${BASE}/impact/`);
  await page.getByText("Dependency graph").first().waitFor();
  await page.getByText("Corrigendum — Ring Road — Uttar Pradesh Pkg-113").first().click();
  await page.getByText("Claim · active").first().waitFor();
  await page.getByText("Claim · active").first().click();
  await sleep(500);
  await shot(page, "claim-evidence", "3 · Conflict: the corrigendum's ₹188 Cr claim contradicts two ₹200 Cr claims. Inspector: the selected claim and its source page.");

  // 4. Graph traversal to dependent decisions (Impact room)
  await page.getByText("Affected requirements").first().scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy(0, -260));
  await shot(page, "graph-traversal", "4 · Recency (same issuer) settles it, no LLM. Graph traversal finds the dependent requirement: clause 2.1, Pass → Fail.");

  // 5. Verdict change BID → NO-BID
  await page.getByText("Verdict change").first().scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy(0, -80));
  await shot(page, "verdict-change", "5 · Deterministic requalification: 4-laning of Greenfield Expressway, BID → NO-BID.");

  // 6. The resulting action and its approval state
  const actions = page.getByText("Required actions").first();
  await actions.scrollIntoViewIfNeeded();
  const row = page.locator("li").filter({ hasText: "Re-approve bid" }).last();   // the action row in step 6
  await shot(page, "action-pending", "6 · The verdict change raised an action: Re-approve bid, Pending. Nothing executes without approval.");
  await row.getByRole("button", { name: "Approve" }).click();
  await shot(page, "action-approve-confirm", "6 · Approval is explicit and recorded in the audit trail.");
  await row.getByRole("button", { name: "Confirm approve" }).click();
  const done = row;
  await done.getByRole("button", { name: /^Execute/ }).waitFor();
  await shot(page, "action-approved", "6 · Approved. Execute is now enabled.");
  await done.getByRole("button", { name: /^Execute/ }).click();
  await done.getByRole("button", { name: "Confirm execute" }).click();
  await page.getByText(/re-verified \d+ clause/).first().waitFor();
  await shot(page, "action-executed", "6 · Executed: every clause re-verified against the graph; recommendation NO-BID recorded with an audit trail.");

  // 7. Provenance: open the PDF page
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.getByRole("button", { name: "Open PDF" }).first().click();
  await page.locator("iframe").first().waitFor();
  await sleep(2500);
  await shot(page, "provenance-pdf", "7 · Provenance: the corrigendum PDF (fixture DOC-DEMO-1), page 1, the page the ₹188 Cr claim was read from.");
  await page.keyboard.press("Escape");

  // 8a. Teach a rule: DEMO-3 lands as a conflict card; choose the JV lead's certificate and learn a rule
  await page.goto(`${BASE}/live/`);
  await fireFromInbox(page, "JV Experience Certificate — Residential Quarters");
  const card = page.getByText("Needs your judgement").first();
  await card.scrollIntoViewIfNeeded();
  const jv = page.locator(".well").filter({ hasText: "JV Partner" }).first();
  await jv.waitFor();
  await shot(page, "rule-card", "8 · Fixture DEMO-3: the JV lead certifies our role as 'JV Partner'; our portfolio says 'Prime'. Similar authority, so it asks a human.");
  await jv.getByRole("button", { name: "Choose and learn rule" }).click();
  await page.getByRole("button", { name: /Promote to company memory/ }).first().waitFor();

  // 8b. Promote it with a signed-in principal (secret read from .env, never printed)
  const secret = envKey("BIDBRAIN_DEMO_SECRET_MANAGER");
  if (!secret) throw new Error("BIDBRAIN_DEMO_SECRET_MANAGER is not set in .env (run scripts/make_demo_principals.py)");
  await signIn(page, "manager", secret);
  const prom = page.waitForResponse((r) => r.url().includes("/api/memory/promote"));
  await page.getByRole("button", { name: /Promote to company memory/ }).first().click();
  const pr = await (await prom).json();
  promoted = pr.org_rule || null;
  console.log("promoted company rule:", promoted);
  await page.getByText("In company memory").first().waitFor();
  await page.getByText("Learned rules").first().scrollIntoViewIfNeeded();
  await shot(page, "rule-promoted", `8 · Rule learned and promoted to company memory by the signed-in bid manager (${promoted}).`);

  // ------------------------------------------------------------------ workspace B (a separate session)
  const B = await newWorkspace(browser, "workspace-b");
  const pb = B.page;
  await pb.goto(`${BASE}/live/`);
  const sw = pb.getByRole("switch").first();
  await sw.waitFor();
  await shot(pb, "second-session", "8 · A separate session (new workspace, nobody signed in). Company memory is off by default.");
  await pb.getByText(/^Off: only this session/).first().waitFor();     // setting loaded
  await sw.click();                                                    // controlled switch: wait for the server's answer
  await pb.getByText(/^On: company rules/).first().waitFor();
  await fireFromInbox(pb, "JV Experience Certificate — Institutional Building");
  await pb.getByText("Auto-resolved by company memory").first().waitFor();
  await pb.getByText("Auto-resolved by company memory").first().scrollIntoViewIfNeeded();
  await shot(pb, "rule-reused", `8 · Opted in to company memory; fixture DEMO-4 resolves itself, citing company rule ${promoted}. No card, no human.`);
  await B.ctx.close();
} finally {
  // leave company memory as it was: retire the rule this run promoted, as the principal who promoted it
  if (promoted && A) {
    const s = await sid(A.page).catch(() => null);
    const r = await A.page.evaluate(async ([id, s]) => {
      const x = await fetch(`/api/memory/org/${id}/retire`, { method: "POST", headers: { "Content-Type": "application/json", ...(s ? { "X-Session": s } : {}) }, body: "{}" });
      return x.status;
    }, [promoted, s]).catch((e) => String(e));
    console.log(`retired ${promoted}: HTTP ${r}`);
  }
  if (A) await A.ctx.close();
  await browser.close();
}
