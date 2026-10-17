// Capture the real payment-system stack running under docker compose.
// Walks the whole payment lifecycle on the actual portal and screenshots each
// screen: login -> catalog -> order -> CREATED -> charge -> READY -> the
// partner's checkout endpoint -> signed callback -> SUCCESS -> REFUND.
//
//   docker compose -f project/docker-compose.yml up -d --build     # 5 containers
//   npm i playwright-core                                          # no browser download
//   node tools/capture/payment-system.mjs /tmp/shots/payment-system
//
// PLAYWRIGHT_CHROMIUM overrides the browser binary (default: system chromium).
import { chromium } from "playwright-core";
import { createHmac } from "node:crypto";

const OUT = process.argv[2] ?? "/tmp/shots/payment-system";
const BASE = process.env.PORTAL_URL ?? "http://localhost:3000";
const MONOLITH = process.env.MONOLITH_URL ?? "http://localhost:8085";
const SECRET = process.env.NOTIFY_SECRET ?? "change-me-notify-secret";

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM ?? "/usr/bin/chromium",
  args: ["--no-sandbox"],
});
const ctx = await browser.newContext({ viewport: { width: 1400, height: 760 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
page.on("pageerror", (e) => console.log("  [pageerror]", e.message.slice(0, 160)));

async function shot(name) {
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/${name}.png` });
  console.log("  shot", name);
}

const bodyText = async () =>
  (await page.locator("body").innerText()).replace(/\s+/g, " ").slice(0, 200);

// The partner signs timestamp||body with HMAC-SHA256, but its payload carries
// `reference_id` while the monolith's CallbackEvt reads `referenceId`, so the
// partner's own callback is rejected with 400. Replay a correctly-shaped signed
// callback to exercise the monolith's verification path instead.
async function replayCallback(transactionId, status = "SUCCEEDED") {
  const ts = String(Math.floor(Date.now() / 1000));
  const body = JSON.stringify({ referenceId: transactionId, status });
  const signature = createHmac("sha256", SECRET).update(ts + body).digest("hex");
  const res = await fetch(`${MONOLITH}/ms/api/v1/payment/notify`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-callback-timestamp": ts,
      "x-callback-signature": signature,
    },
    body,
  });
  console.log("  signed callback ->", res.status, (await res.text()).slice(0, 90));
}

// 1 · sign in
await page.goto(`${BASE}/login`, { waitUntil: "networkidle" });
await shot("01-login");
await page.fill("#username", "klhomme0");
await page.fill("#password", "user1Pass!");
await page.click('button[type="submit"]');
await page.waitForURL("**/catalog", { timeout: 20000 });
await page.waitForLoadState("networkidle");
await shot("02-catalog");

// 2 · place an order
await page.getByRole("button", { name: /^order$/i }).first().click();
await page.waitForTimeout(500);
await shot("03-order-dialog");
await page.getByRole("button", { name: /place order/i }).last().click();
await page.waitForURL(/\/pay\//, { timeout: 25000 });
await page.waitForLoadState("networkidle");
const trx = page.url().split("/").pop();
console.log("  transaction:", trx);
await shot("04-created");

// 3 · charge the partner: CREATED -> READY
await page.getByRole("button", { name: /create payment/i }).click();
await page.waitForTimeout(1500);
const checkoutUrl = await page.locator("p.break-all").first().innerText().catch(() => "");
console.log("  checkoutUrl:", checkoutUrl);
await shot("05-ready");

// 4 · the partner's own checkout endpoint, then the signed callback
if (checkoutUrl) {
  await page.goto(checkoutUrl, { waitUntil: "networkidle" });
  await shot("06-partner-checkout");
}
await replayCallback(trx);

// 5 · back on the portal the callback has landed
await page.goto(`${BASE}/pay/${trx}`, { waitUntil: "networkidle" });
await page.waitForTimeout(1200);
await shot("07-success");
console.log("  after callback:", await bodyText());

// 6 · refund: SUCCESS -> REFUND
const refund = page.getByRole("button", { name: /refund/i }).first();
if (await refund.count()) {
  await refund.click();
  for (let i = 0; i < 10; i++) {
    await page.waitForTimeout(1200);
    if ((await bodyText()).includes("REFUND")) break;
  }
  await shot("08-refund");
  console.log("  after refund:", await bodyText());
}

await browser.close();
