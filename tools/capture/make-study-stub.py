#!/usr/bin/env python3
"""Build a minimal *honest* stand-in for the trading-suite study checkout.

The plan module reads a private study checkout (`TRADING_DOCS_DIR`) that carries
the bar manifest, the per-file hashes and the results CSVs. This script produces
only the bar half — real daily bars, fetched through the app's own
`app.market.fetch_history` — so the plan module can run and its own integrity
checks can judge the evidence.

It deliberately does NOT invent `results/*.csv`. Those are the study's measured
evidence, and fabricating them would turn a screenshot about honest evidence into
a fabricated one. Their absence is the point: the app should refuse to issue a
ticket.

    backend/.venv/bin/python make-study-stub.py <backend-dir> <output-dir>

`app` and `pandas` are imported dynamically because this script lives outside the
application package, where a static import cannot be resolved.
"""

import importlib
import json
import pathlib
import sys

TICKERS = [
    "^JKSE",
    "BBCA", "BBRI", "BMRI", "BBNI", "TLKM", "ASII", "UNVR", "ICBP",
    "INDF", "KLBF", "ANTM", "ADRO", "PTBA", "SMGR", "INTP", "JSMR",
    "MDKA", "TPIA", "GOTO", "ARTO", "BRPT",
]
PERIOD = "2y"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2

    backend = pathlib.Path(argv[1]).resolve()
    root = pathlib.Path(argv[2]).resolve()
    if not (backend / "app" / "market.py").exists():
        print(f"not the trading-suite backend: {backend}")
        return 2

    sys.path.insert(0, str(backend))
    fetch_history = importlib.import_module("app.market").fetch_history
    pandas = importlib.import_module("pandas")

    (root / "data" / "raw").mkdir(parents=True, exist_ok=True)
    (root / "data" / "meta").mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict] = {"tickers": {}}
    for code in TICKERS:
        try:
            rows = fetch_history(code, PERIOD, "1d")
        except Exception as exc:  # noqa: BLE001 - a feed miss must not sink the stub
            print(f"  skip {code}: {exc}")
            continue
        frame = pandas.DataFrame(rows)
        if frame.empty or "date" not in frame.columns:
            print(f"  skip {code}: unexpected shape {list(frame.columns)[:6]}")
            continue
        rel = f"data/raw/{code.replace('^', 'IDX')}.csv"
        frame.to_csv(root / rel, index=False)
        manifest["tickers"][code] = {"file": rel, "rows": int(len(frame))}
        print(f"  {code}: {len(frame)} rows -> {rel}")

    (root / "data" / "meta" / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nwrote {len(manifest['tickers'])} tickers under {root}")
    print("no results/*.csv on purpose - the evidence is genuinely absent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
