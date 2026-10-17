#!/usr/bin/env bash
# Start a routre gateway wired to the repo's own mock upstreams, so the
# dashboard and the ledger can be demonstrated with real traffic and no keys.
#
#   tools/capture/routre-demo.sh          # build, start, drive traffic, print the ledger
#   tools/capture/routre-demo.sh stop     # shut the demo down
#
# Layout: /tmp/routre-demo/{config.json,routre.env,*.log} and an isolated HOME,
# so the real ~/.routre is never touched.
set -euo pipefail

SRC="${ROUTRE_SRC:-/tmp/repo-clones/routre}"
D="${ROUTRE_DEMO_DIR:-/tmp/routre-demo}"
GO_BIN="${GO_BIN:-$HOME/.local/share/mise/installs/go/1.26.9/bin/go}"
CLI="$D/routre"
GW="http://127.0.0.1:20128"

if [[ "${1:-}" == "stop" ]]; then
  for p in $(pgrep -f "routre-demo/routr[e]" || true; pgrep -f "routre-demo/mock-upstrea[m]" || true); do
    kill "$p" 2>/dev/null || true
  done
  echo "routre demo stopped"
  exit 0
fi

# The mock upstream always binds its admin listener on 19998, so only one can
# run at a time. Tier 1 therefore points at a port nothing is listening on:
# the failover and the cooldown are real, they just come from a network error
# instead of a 503.
for p in $(pgrep -f "routre-demo/routr[e]" || true; pgrep -f "routre-demo/mock-upstrea[m]" || true); do
  kill "$p" 2>/dev/null || true
done
sleep 1

rm -rf "$D"
mkdir -p "$D/home/.routre" "$D/data"

# Tier 1 returns 503 and tier 2 answers, so every request shows a real failover
# and a real cooldown instead of an invented one.
cat > "$D/config.json" <<'JSON'
{
  "listen": "127.0.0.1:20128",
  "log_level": "info",
  "forward_unknown": true,
  "rtk": { "enabled": true, "level": "routre", "min_bytes": 500, "max_bytes": 10485760,
           "scan_bytes": 524288, "budget_ms": 150 },
  "cache": { "enabled": true, "max_entries": 512, "ttl_seconds": 3600 },
  "persist": { "enabled": true, "interval_seconds": 60 },
  "tiers": [
    { "name": "subscription", "providers": [
      { "name": "anthropic-primary", "kind": "anthropic",
        "base_url": "http://127.0.0.1:19997",
        "api_key_env": "ANTHROPIC_API_KEY",
        "models": ["claude-sonnet-4"],
        "price_in": 3.0, "price_out": 15.0 } ] },
    { "name": "cheap", "providers": [
      { "name": "openrouter-economy", "kind": "openai",
        "base_url": "http://127.0.0.1:19999",
        "api_key_env": "OPENROUTER_API_KEY",
        "models": ["deepseek/deepseek-chat"],
        "price_in": 0.3, "price_out": 1.2 } ] }
  ]
}
JSON
printf 'ANTHROPIC_API_KEY=sk-ant-demo-placeholder\nOPENROUTER_API_KEY=sk-or-demo-placeholder\n' > "$D/routre.env"
chmod 600 "$D/routre.env"

echo "building routre + mock-upstream from $SRC"
( cd "$SRC" && PATH="$(dirname "$GO_BIN"):$PATH" go build -trimpath -ldflags "-s -w -X main.version=dev" -o "$CLI" . )
( cd "$SRC" && PATH="$(dirname "$GO_BIN"):$PATH" go build -o "$D/mock-upstream" ./cmd/mock-upstream )

export HOME="$D/home"
export ROUTRE_CLI_DATA_DIR="$D/data"
"$D/mock-upstream" -port 19999 > "$D/mock-ok.log" 2>&1 &
sleep 1
"$CLI" serve --config "$D/config.json" > "$D/gateway.log" 2>&1 &
sleep 2

echo "healthz: $(curl -s "$GW/healthz")"

# A tool-heavy agent request: the shape RTK exists to shrink.
python3 - "$D/req.json" <<'PY'
import json, sys
diff = "\n".join(
    f"@@ -{i*13},7 +{i*13},9 @@ static int handler_{i}(struct ctx *c)\n"
    f"-    ret = legacy_call_{i}(c, opts);\n"
    f"+    ret = modern_call_{i}(c, opts, /* retry */ 3);\n"
    f"+    if (ret < 0) {{ log_warn(\"handler %d failed: %d\", ret); return ret; }}\n"
    "     return ret;"
    for i in range(120)
)
body = {
    "model": "deepseek/deepseek-chat",
    "messages": [
        {"role": "user", "content": "Refactor the handlers and run the full test suite."},
        {"role": "assistant", "content": "Running tests."},
        {"role": "tool", "tool_call_id": "call_1",
         "content": "BUILD OK\n" + "test_handler_ok ... PASSED\n" * 400 + "\n$ git diff\n" + diff},
    ],
    "stream": False,
}
json.dump(body, open(sys.argv[1], "w"))
PY

echo "— request 1 (cache miss: tier-1 fails over, RTK compresses) —"
curl -s -D "$D/h1.txt" -o /dev/null -X POST "$GW/v1/chat/completions" \
  -H 'Content-Type: application/json' -H 'User-Agent: opencode/2.0.31' --data-binary @"$D/req.json"
grep -iE '^(HTTP|x-llrouter)' "$D/h1.txt"

echo "— request 2 (identical: replayed from cache) —"
curl -s -D "$D/h2.txt" -o /dev/null -X POST "$GW/v1/chat/completions" \
  -H 'Content-Type: application/json' -H 'User-Agent: opencode/2.0.31' --data-binary @"$D/req.json"
grep -iE '^(HTTP|x-llrouter)' "$D/h2.txt"

echo
"$CLI" list --config "$D/config.json" --url "$GW"
echo
echo "dashboard: $GW/ui"
