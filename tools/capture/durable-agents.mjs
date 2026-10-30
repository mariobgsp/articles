// Capture durable-agents' command surface, from a checkout of that repository.
//
//   node tools/capture/durable-agents.mjs [outDir] [--repo <path>] [--sha <sha>]
//
// There is no browser in this project any more: it is a daemon, a CLI, a
// read-only fleet TUI and one Telegram bot per agent. So the honest capture is
// the command surface itself — the command list and the two catalogs — read out
// of the checkout that the article's closing pin names.
//
// Read-only, and offline: no daemon is started, no model call is made, no
// socket is opened, and DURA_HOME points at a throwaway directory so nothing on
// this machine is read or written.
//
// Output: <outDir>/in-use.md — the blocks quoted in the article's "In use"
// section — plus a one-line summary of the counts that were observed.
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const argv = process.argv.slice(2);
const flag = (name) => {
  const i = argv.indexOf(name);
  if (i === -1) return undefined;
  const value = argv[i + 1];
  argv.splice(i, 2);
  return value;
};

const OUT = resolve(argv[0] ?? "/tmp/shots/durable-agents");
const REPO = resolve(flag("--repo") ?? "../durable-agents");
const EXPECTED = flag("--sha");

function git(...args) {
  return execFileSync("git", ["-C", REPO, ...args], { encoding: "utf8" }).trim();
}

const sha = git("rev-parse", "--short", "HEAD");
const subject = git("log", "-1", "--format=%s");
const dirty = git("status", "--porcelain");
if (EXPECTED && !git("rev-parse", "HEAD").startsWith(EXPECTED)) {
  console.error(`warning: HEAD is ${sha}, the article pins ${EXPECTED}`);
}
if (dirty) {
  console.error("warning: the checkout has uncommitted changes, so this is not the pinned tree");
}

// A throwaway home: `provider list` reads config, never a daemon.
const home = mkdtempSync(join(tmpdir(), "dura-capture-"));

function dura(...args) {
  return execFileSync("node", ["packages/cli/bin/dura.ts", ...args], {
    cwd: REPO,
    env: { ...process.env, DURA_HOME: home, NO_COLOR: "1" },
    encoding: "utf8",
    timeout: 120_000,
  }).trim();
}

// The `--help` output is a fixed layout; drop the "Global:" trailer, which is
// repeated in the article's prose.
const help = dura("--help");
const version = dura("--version");
const providers = dura("provider", "list", "--all");
// The provider table carries this machine's credential state, which belongs in
// no article; the catalog size is the fact, and the model table is public.
const models = dura("provider", "models", "opencode-go");

const count = (text) => text.split("\n").filter((line) => line.trim() !== "").length - 1;
const providerCount = count(providers);
const modelCount = count(models);

mkdirSync(OUT, { recursive: true });
const out = join(OUT, "in-use.md");
writeFileSync(
  out,
  `<!-- captured from ${sha} (${subject}) on ${new Date().toISOString().slice(0, 10)} -->\n` +
    `\`${version}\` — ${providerCount} providers, ${modelCount} models on opencode-go.\n\n` +
    "```text\n" +
    help +
    "\n```\n\n" +
    "```text\n" +
    models.split("\n").slice(0, 9).join("\n") +
    "\n…\n```\n",
);

rmSync(home, { recursive: true, force: true });
console.log(`captured ${version} at ${sha}: ${providerCount} providers, ${modelCount} models -> ${out}`);
console.log(`  ${subject}`);
