// Capture The Gateway running under docker compose: login -> dashboard -> API
// detail, all through the Next.js BFF with an httpOnly session cookie.
//
//   docker compose -f project/docker-compose.yml up -d --build
//   node tools/capture/the-gateway.mjs /tmp/shots/the-gateway
//
// Seeded credentials come from project/pg-init-scripts/gateway.sql.
import { chromium } from "playwright-core";

const OUT = process.argv[2] ?? "/tmp/shots/the-gateway";
const BASE = process.env.GATEWAY_URL ?? "http://localhost:3000";
const USER = process.env.GATEWAY_USER ?? "ario_test";
const PASS = process.env.GATEWAY_PASS ?? "password123";

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM ?? "/usr/bin/chromium",
  args: ["--no-sandbox"],
});
const ctx = await browser.newContext({ viewport: { width: 1400, height: 820 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
page.on("pageerror", (e) => console.log("  [pageerror]", e.message.slice(0, 160)));
page.on("response", (r) => {
  if (r.url().includes("/gw/")) console.log("  BFF", r.status(), r.url().replace(BASE, ""));
});

async function shot(name) {
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/${name}.png` });
  console.log("  shot", name, "->", page.url());
}

// 1 · the login screen is the site root; the session is minted server-side
await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await shot("01-login");

await page.fill("#username", USER);
await page.fill("#password", PASS);
await page.click('button[type="submit"]');
await page.waitForURL("**/home", { timeout: 25000 });
await page.waitForLoadState("networkidle");
await shot("02-dashboard-apilist");

// 2 · cookies are httpOnly, so the browser cannot read the session token
const jsVisible = await page.evaluate(() => document.cookie);
const cookies = await ctx.cookies();
console.log("  document.cookie =>", JSON.stringify(jsVisible));
console.log("  session cookie  =>", cookies.filter((c) => c.name.includes("session") || c.name.includes("gw")).map((c) => `${c.name} httpOnly=${c.httpOnly} sameSite=${c.sameSite}`).join(", "));

// 3 · the Store tab
const storeTab = page.getByRole("button", { name: /^store$/i }).first();
if (await storeTab.count()) {
  await storeTab.click();
  await page.waitForTimeout(800);
  await shot("03-store-tab");
  await page.getByRole("button", { name: /^api$/i }).first().click();
  await page.waitForTimeout(600);
}

// 4 · open an API row -> the configuration detail screen (allowlists, gates)
const rows = page.locator(".table-row");
const rowCount = await rows.count();
console.log("  api rows:", rowCount);
if (rowCount > 0) {
  await rows.first().click();
  await page.waitForURL(/\/api\//, { timeout: 20000 });
  await page.waitForLoadState("networkidle");
  await shot("04-api-detail");
}

// 5 · "Try it Now!" executes the configured upstream through GatewayForward.
// The demo API points at the gateway's own /health so the forward pipeline can
// be shown completing without depending on an external host's TLS chain.
await page.goto(`${BASE}/api/${process.env.TRY_IT_API ?? "gateway-self-health"}`, { waitUntil: "networkidle" });
await page.waitForTimeout(600);
const tryIt = page.getByRole("button", { name: /try it/i }).first();
if (await tryIt.count()) {
  await tryIt.click();
  await page.waitForTimeout(900);
  await shot("05-try-it");
  const send = page.getByRole("button", { name: /send request|send|execute/i }).first();
  if (await send.count()) {
    await send.click();
    await page.waitForTimeout(2500);
    await shot("06-try-it-response");
  }
}

console.log("  done");
await browser.close();
