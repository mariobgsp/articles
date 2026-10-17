// Screenshot the routre dashboard (the loopback /ui settings page).
//
//   tools/capture/routre-demo.sh                              # start gateway + traffic
//   node tools/capture/routre.mjs /tmp/shots/routre
//
// PLAYWRIGHT_CHROMIUM overrides the browser binary (default: system chromium).
import { chromium } from "playwright-core";

const OUT = process.argv[2] ?? "/tmp/shots/routre";
const URL = process.env.ROUTRE_UI ?? "http://127.0.0.1:20128/ui";

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM ?? "/usr/bin/chromium",
  args: ["--no-sandbox"],
});
const page = await (
  await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
).newPage();

await page.goto(URL, { waitUntil: "networkidle" });
await page.waitForTimeout(600);
await page.screenshot({ path: `${OUT}/routre-dashboard.png` });
await page.screenshot({ path: `${OUT}/routre-dashboard-full.png`, fullPage: true });
console.log("shot routre-dashboard");

// The ledger is a CLI surface, so render it as text next to the dashboard.
await browser.close();
