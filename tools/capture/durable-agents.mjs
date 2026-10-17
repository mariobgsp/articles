// Capture the durable-agents app running locally (Fastify :8787 + Vite :5173).
//
//   npm run dev
//   node tools/capture/durable-agents.mjs /tmp/shots/durable-agents
//
// Read-only: this navigates and screenshots existing threads. It never sends a
// message, so no model call is made and no thread is modified.
//
// Hash routes: #/ · #/s/<id> · #/groups · #/agents · #/agents/<id> · #/providers
import { chromium } from "playwright-core";

const OUT = process.argv[2] ?? "/tmp/shots/durable-agents";
const BASE = process.env.AGENTS_URL ?? "http://localhost:5173";

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM ?? "/usr/bin/chromium",
  args: ["--no-sandbox"],
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
page.on("pageerror", (e) => console.log("  [pageerror]", e.message.slice(0, 160)));

async function shot(name) {
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${OUT}/${name}.png` });
  console.log("  shot", name, "->", await page.evaluate(() => location.hash));
}

const routes = [
  ["01-groups", "#/groups"],
  ["02-agents", "#/agents"],
  ["03-agent-editor", "#/agents/pirate"],
  ["04-providers", "#/providers"],
];

for (const [name, hash] of routes) {
  await page.goto(`${BASE}/${hash}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1600);
  await shot(name);
  const text = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  console.log(`  ${name}: ${text.slice(0, 150)}`);
}

// Open a real group thread: the Nyahut flow is the interesting screen.
const group = process.env.AGENTS_GROUP_ID ?? "502";
await page.goto(`${BASE}/#/s/${group}`, { waitUntil: "networkidle" });
await page.waitForTimeout(2500);
await shot("05-group-thread");
console.log("  group:", (await page.locator("body").innerText()).replace(/\s+/g, " ").slice(0, 220));

// And a one-to-one agent thread.
const solo = process.env.AGENTS_SOLO_ID ?? "550";
await page.goto(`${BASE}/#/s/${solo}`, { waitUntil: "networkidle" });
await page.waitForTimeout(2500);
await shot("06-agent-thread");
console.log("  solo:", (await page.locator("body").innerText()).replace(/\s+/g, " ").slice(0, 200));

await browser.close();
