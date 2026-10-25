# Project articles

Five written-up engineering projects, each one a repository I built and ran, then
documented from the running system and re-checked against the code.

Every article follows the same shape, so the set reads as one portfolio instead of
five unrelated blog posts:

1. **At a glance** — what it is, who it is for, and the one thing that is
   interesting about it.
2. **The problem** — the concrete failure the project exists to prevent.
3. **What it does** — the capability table.
4. **How it works** — a diagram, the components, and the path one request, session
   or message takes.
5. **Key choices** — the decisions that matter, with their price.
6. **Running it** — the shortest verified commands, plus what I actually observed.
7. **Screenshots** — captured from the running application.
8. **What I'd fix next** — including defects I found by running the code.
9. **Links** — the repository, the design docs, and the diagram source.

## The five

| Article | What it is | Stack | The idea worth reading for |
| --- | --- | --- | --- |
| [**routre**](routre.md) — *A Local LLM Gateway* | A local LLM gateway for coding agents: compression, cache and provider failover in one static binary. | Go (standard library only) | A 7-step request pipeline where every step is bounded, so a gateway can never be slower than the provider it fronts. |
| [**payment-system**](payment-system.md) — *One Go Binary, No Kafka* | A payment lifecycle (order → charge → signed callback → invoice → refund) as one Go monolith. | Go · PostgreSQL · Redis · Next.js | A Postgres **outbox** replacing Kafka without losing atomicity, and a measured trigger for splitting again. |
| [**the-gateway**](the-gateway.md) — *JWTs Stay on the Server* | API-management console for outbound third-party APIs, with a Next.js BFF in front. | Next.js 15 · Go · PostgreSQL | A trust boundary where the session token is unreachable by page JavaScript, and allowlists enforced from persisted configuration. |
| [**trading-suite**](trading-suite.md) — *The Screener That Refuses to Buy* | A self-hosted IDX market desk with six screens and a CLI, whose verdict can be "no trade" or "the data is not sound". | Python · FastAPI · SQLite · Vite + React 19 | **Refusal as a product feature**: `DATA_UNSAFE` outranks every signal, and the evidence behind each number is traceable to the study. |
| [**durable-agents**](durable-agents.md) — *Chats That Survive a Crash* | Local agent chat where every message and generation is a durable SQLite task, plus an agent network, schedules and generated files. | Svelte 5 · Fastify · SQLite (`pi-durable`) | Durability owned by the harness rather than the UI, and a group/network protocol where agents decide who answers. |

## Layout

```text
articles/
  README.md                  this file
  routre.md                  the five articles
  payment-system.md
  the-gateway.md
  trading-suite.md
  durable-agents.md
  diagrams/<slug>.mmd        Mermaid source, one file per article
  images/                    diagrams (light + dark) and application screenshots
  tools/render-diagrams.py   diagrams/*.mmd -> images/<slug>-architecture[-dark].png
  tools/capture/*.mjs        screenshot scripts, one per application
  tools/capture/*.sh|py      local run helpers (routre demo, study stub)
```

## Regenerating

Prerequisites: Python 3, ImageMagick (`magick`), Node, Chromium, and
`npm install` in this directory (for `playwright-core`, which drives the system
Chromium — no browser download).

```bash
# diagrams: every .mmd -> a light and a dark PNG, trimmed and framed
python3 tools/render-diagrams.py                # or: npm run diagrams
python3 tools/render-diagrams.py routre         # one diagram
python3 tools/render-diagrams.py --theme=light  # one theme only

# screenshots: each script walks a real, running instance
npm run capture:payment-system   # needs docker compose up (5 containers)
npm run capture:the-gateway      # needs docker compose up (3 containers)
npm run capture:trading-suite    # needs ./trading-suite run
npm run capture:durable-agents   # needs npm run dev in that repo
npm run capture:routre           # needs tools/capture/routre-demo.sh first
```

Two of the captures need a local fixture rather than the real dependency:

- **routre** — `tools/capture/routre-demo.sh` builds the binary and the repo's own
  `cmd/mock-upstream`, drives a tool-heavy request twice, and prints the token
  ledger. No provider keys are used.
- **trading-suite** — `tools/capture/make-study-stub.py` builds a stand-in for the
  private study checkout containing **real daily bars** and **no evidence files**,
  which is what makes the app produce its `DATA_UNSAFE` verdict on its own logic.

## CI

Two workflows, in two files, because the two halves have different permissions.
`.github/workflows/articles-validate.yml` runs on every push and on every pull request and holds
`contents: read` and nothing else; `.github/workflows/articles-drift.yml` runs daily and on demand
and holds `issues: write`. Neither one lists an article, a repository or a branch: the inventory is
derived from the articles themselves.

```bash
python3 -m tools.articles_ci validate   # the checks; the validate workflow runs exactly this
python3 -m tools.articles_ci drift      # read the drift report here first; writes nothing
python3 -m tools.articles_ci selftest   # proves validate, drift and publish offline
```

The drift workflow runs `python3 -m tools.articles_ci drift --publish --out drift-report.json` and
keeps one issue, rewritten in place: it names the sections that went stale, the upstream paths that
prove it, and the handoff. Nothing in CI edits prose, and the pin moving is the human's step.

## Defects found by running the code

Documented in the relevant articles' *What I'd fix next* sections, and worth
separating here because they are the best evidence that the articles describe real
software rather than screenshots of a README:

| Project | Defect |
| --- | --- |
| payment-system | The new checkout page reads `camelCase` while the legacy compat endpoint answers `snake_case`, so `formatIdr(trx.priceCharge)` throws and the page unmounts into an error boundary. |
| payment-system | `ms-paymentagr` sends `reference_id`; the monolith's `CallbackEvt` reads `referenceId`. Verified: `snake_case` → HTTP 400, `camelCase` → HTTP 202. The payment can never reach `SUCCESS` through the normal partner flow. |
| trading-suite | With no study checkout, `LiveBars.load()` receives `None` and raises `TypeError` → HTTP 500, instead of the intended `DATA_UNSAFE` verdict. |
| trading-suite | The Method screen has no error state, so a failed load shows "Loading the method…" forever and looks like a slow one. |

## Notes

- Diagram PNGs come in a **light** version (used by the articles, readable on
  GitHub, dev.to and Hashnode alike) and a **`-dark`** version matching the
  palette of [mariobgsp.github.io](https://mariobgsp.github.io) for site use.
- Screenshots are captured at 2× device scale and 2000 px wide; diagrams are
  rendered at 2× and trimmed to content, which keeps text legible at article width.
- Each article's closing note records the commit it was verified against and links
  its diagram source and capture script. The prose is re-read against that commit;
  screenshots come from the dated capture run named in the article's header.
