// Capture the trading-suite desk running locally (FastAPI :8000 + Vite :5199).
//
//   TRADING_DOCS_DIR=<study> FRONTEND_PORT=5199 ./trading-suite run
//   node tools/capture/trading-suite.mjs /tmp/shots/trading-suite
//
// The study checkout the plan module reads is private, so a stub built by
// tools/capture/make-study-stub.py supplies real daily bars and no evidence
// files at all. The app then computes its own integrity verdict from real
// numbers and withholds every ticket - which is the behaviour worth showing.
import { chromium } from "playwright-core";

const OUT = process.argv[2] ?? "/tmp/shots/trading-suite";
const BASE = process.env.SUITE_URL ?? "http://localhost:5199";

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM ?? "/usr/bin/chromium",
  args: ["--no-sandbox"],
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
page.on("pageerror", (e) => console.log("  [pageerror]", e.message.slice(0, 160)));

async function shot(name) {
  await page.waitForTimeout(1400);
  await page.screenshot({ path: `${OUT}/${name}.png` });
  console.log("  shot", name, "->", page.url());
}

const routes = [
  ["01-tonight", "/"],
  ["02-screener", "/screener"],
  ["03-method", "/method"],
  ["04-name", "/name/BBCA"],
];

for (const [name, path] of routes) {
  await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
  // the plan is fetched client-side and SWR-cached; give the first paint time
  await page.waitForTimeout(2500);
  await shot(name);
  const text = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  console.log(`  ${name}: ${text.slice(0, 170)}`);
}

await browser.close();
