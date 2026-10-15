// Records the demo footage (no narration) by driving the live app, following docs/DEMO_SCRIPT.md.
//
//   cd scripts && npm i playwright@1 && npx playwright install chromium
//   BASE=https://<space>.hf.space node record_demo.mjs        # -> scripts/videos/*.webm
//
// Pauses are generous on purpose: a narrator talks over them. Each beat is a
// separate page so retakes are cheap (BEAT=qualify node record_demo.mjs).
import { chromium } from "playwright";

const BASE = (process.env.BASE || "http://localhost:7860").replace(/\/$/, "");
const ONLY = process.env.BEAT;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const beats = {
  async overview(page) {
    await page.goto(`${BASE}/`);
    await sleep(6000);
    await page.mouse.wheel(0, 500); await sleep(3500);
  },
  async qualify(page) {
    await page.goto(`${BASE}/qualify/`);
    await sleep(2500);
    await page.getByText("4-laning of Greenfield Expressway").first().click(); await sleep(1500);
    await page.getByRole("button", { name: /Qualify this tender/ }).click();
    await sleep(6000);
    await page.getByText(/^2\.1$/).first().click().catch(() => {});
    await sleep(3000);
    await page.getByText("DOC-CC-113").first().click().catch(() => {}); await sleep(4000);
    await page.keyboard.press("Escape"); await page.mouse.click(10, 10); await sleep(800);
    await page.getByText("Institutional Buildings Complex").first().click(); await sleep(1000);
    await page.getByRole("button", { name: /Qualify this tender/ }).click();
    await sleep(7000);
  },
  async ask(page) {
    await page.goto(`${BASE}/ask/`);
    await sleep(2500);
    await page.getByText(/no client reference letter on file/).first().click();
    await page.waitForSelector("text=Answer", { timeout: 90000 }).catch(() => {});
    await sleep(5000);
    await page.getByText("How the agent got there").first().click().catch(() => {}); await sleep(3000);
    await page.getByRole("button", { name: /Compare with vector RAG/ }).first().click().catch(() => {});
    await sleep(9000);
  },
  async live(page) {
    // a case must exist for the ripple: qualify the expressway tender silently first
    await page.goto(`${BASE}/qualify/`); await sleep(1500);
    await page.getByRole("button", { name: /Qualify this tender/ }).click(); await sleep(4000);
    await page.goto(`${BASE}/live/`); await sleep(3000);
    await page.getByText(/^Revised Completion Certificate — Six-Lane/).first().click(); await sleep(5000);
    await page.getByText(/^Corrigendum — Ring Road — Uttar Pradesh Pkg-113/).first().click(); await sleep(7000);
    await page.getByText(/^JV Experience Certificate — /).first().click(); await sleep(4000);
    await page.getByRole("button", { name: /Choose \+ learn rule/ }).first().click().catch(() => {}); await sleep(4000);
    await page.getByText(/^JV Experience Certificate — /).nth(1).click().catch(() => {}); await sleep(6000);
  },
  async decisions(page) {
    await page.goto(`${BASE}/decisions/`); await sleep(2500);
    await page.locator("button:has-text('authority')").nth(1).click().catch(() => {}); await sleep(4000);
    await page.goto(`${BASE}/explore/`); await sleep(3000);
    await page.getByRole("button", { name: /Bus factor/ }).click(); await sleep(4500);
    await page.getByRole("button", { name: /Audit past bids/ }).click(); await sleep(4500);
  },
  async benchmark(page) {
    await page.goto(`${BASE}/benchmark/`); await sleep(5000);
    await page.mouse.wheel(0, 600); await sleep(5000);
  },
};

const browser = await chromium.launch();
for (const [name, fn] of Object.entries(beats)) {
  if (ONLY && ONLY !== name) continue;
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1,
    recordVideo: { dir: "videos", size: { width: 1440, height: 900 } }, colorScheme: "dark" });
  const page = await ctx.newPage();
  console.log("recording", name);
  try { await fn(page); } catch (e) { console.error(name, e.message); }
  await ctx.close();
}
await browser.close();
