# Keeping JWTs Out of the Browser: an API Gateway With a Server-Side BFF

> The Gateway is a small API-management console: define the upstream APIs you
> call, allowlist what may reach them, and test them from the UI. The interesting
> engineering is not the CRUD — it is that the session token never becomes
> readable by a single line of browser JavaScript.

| | |
| --- | --- |
| **Repository** | [github.com/mariobgsp/the-gateway](https://github.com/mariobgsp/the-gateway) |
| **Role** | Backend (Go) + full-stack engineering |
| **Stack** | Next.js 15 (BFF) · Go 1.x + chi + pgx · PostgreSQL 15 · Docker Compose / Kubernetes |
| **Verified** | Built and run locally via Docker Compose (2026-10-17). The cookie and BFF evidence below is copied from that run. |
| **Order** | 5 |

## At a glance

- **What it is** — a control plane for outbound HTTP APIs plus a runtime that
  executes them: each `ApiGateway` record stores a host, a path, a method, a
  header allowlist, an optional parameter allowlist and a required-body gate.
- **Who it's for** — teams that call many third-party APIs and want one place to
  see, constrain and test them, without putting a credential in front of the
  browser.
- **The interesting part** — the trust boundary is the **Next.js server**, not the
  SPA. Every backend call is a server-side fetch behind an `httpOnly` cookie, and
  every upstream call re-enforces the allowlists that are stored in the database.

## The problem: two credentials in the wrong places

Most SPA-plus-API projects end up with the same two problems.

**1 — The token is readable by any script on the page.** Storing a JWT in
`localStorage` means one injected script, one compromised dependency, or one
careless `dangerouslySetInnerHTML` is a full account takeover. The token also
becomes part of every XSS payload the app ever has.

**2 — Any URL the browser can name, the server will fetch.** A gateway that
forwards whatever path, header or query it is handed is an open proxy: an
attacker can reach hosts you never configured, leak credentials you meant to
keep local, or use your service as an SSRF pivot.

This project answers both by construction: the browser holds an opaque cookie it
cannot read, and the Go service derives the outbound request from *persisted
configuration* rather than from the incoming request.

## What it does

| Capability | How |
| --- | --- |
| **Upstream API registry** | Named `ApiGateway` records: identifier, host, path, method, header allowlist, parameter allowlist, required-body flag, status. |
| **Store accounts** | Machine principals with a client id and secret, tied to a user through a join table. |
| **Server-side session** | Login mints a JWT into an `httpOnly`, `SameSite=Lax`, secure-in-production cookie. The browser never reads it. |
| **Guarded forwarding** | `GatewayForward` resolves the stored config, filters headers and parameters against it, encodes the URL once, enforces the body gate, then calls upstream. |
| **Try-it console** | Run a configured API from the UI and see the raw upstream response without writing a script. |
| **Envelope contract** | One response shape for the UI: `code: "00"` means success; `01`–`06`, `98` and `99` identify specific faults. |
| **Two deploy paths** | Docker Compose for local, Kubernetes manifests for a cluster. |

## Why this stack

| Layer | Choice | Why this — and not the obvious alternative |
| --- | --- | --- |
| Trust boundary | **Next.js App Router server (BFF)** | The session token has to live somewhere the browser cannot read. A server-rendered BFF is already in the stack for the UI, so the token handler costs no extra service. |
| Session transport | **`httpOnly` + `SameSite=Lax` cookie** | Removes the token from JavaScript entirely and gets CSRF protection from the cookie policy rather than from a custom scheme. `Secure` flips on in production. |
| Backend language | **Go with chi and pgx** | The forwarding path is I/O-bound and latency-sensitive; a small binary with an explicit router and a real driver beats a framework where the middleware order is implicit. |
| Persistence | **PostgreSQL, entity-specific queries** | The records *are* the configuration, and they need constraints (unique identifier, foreign keys to users). `internal/store` keeps one file per entity instead of a generic repository. |
| Route table | **Declarative `gwRoutes.ts`** | A `/gw/*` surface that grows by copy-paste eventually diverges. One table means adding an endpoint is one entry, and the BFF mapping is auditable in a single read. |
| Styling / runtime | **Standard Web APIs** (`fetch`, `AbortController`) | The BFF needs a timeout and a cancellation path, both of which are in the platform. |

## Architecture

![The Gateway architecture](images/the-gateway-architecture.png)

<details>
<summary>Mermaid source (<code>diagrams/the-gateway.mmd</code>)</summary>

```mermaid
flowchart TD
  BR["Browser<br/>API list · Store · Try it"]

  BFF["Next.js server — the trust boundary<br/>BffGateway: /gw/[...path] catch-all<br/>session lookup · timeout · sanitization · envelope mapping<br/>gwRoutes.ts — declarative route table"]

  GO["gateway-go :8080<br/>JWT auth + rate limiting<br/>services: api · account · auth · forward"]

  FWD["GatewayForward<br/>resolve config → allowlist headers and params<br/>→ encode URL once → enforce request gates"]

  STORE[("PostgreSQL 15<br/>ApiGateway configs<br/>StoreAccounts · users")]
  UP["Configured upstream APIs"]

  BR -->|"gw_session cookie — httpOnly, SameSite=Lax,<br/>Secure in production, never in localStorage"| BFF
  BFF -->|"server-side fetch only —<br/>the JWT never leaves the server"| GO
  GO --> STORE
  GO --> FWD
  FWD -->|"5s dial · 30s client timeout"| UP
```

</details>

### Component walkthrough

- **`frontend/src/app/gw/[...path]/route.ts`** — the catch-all HTTP adapter. Every
  browser→backend call enters here.
- **`frontend/src/lib/bffGateway.ts`** — `BffGateway`, the deep module that owns
  session lookup, the unauthorized response, the backend fetch, the timeout and
  the envelope translation. Sanitization is private to it, so no call site can
  forget it.
- **`frontend/src/lib/gwRoutes.ts`** — the declarative route table that maps
  `/gw/*` paths onto backend paths.
- **`gateway-go/internal/handler/`** — router, HTTP adapters and the auth
  middleware.
- **`gateway-go/internal/service/forward.go`** — `ForwardRequest` (path name,
  query params, headers, body) is the typed seam between the HTTP layer and the
  forwarder, which keeps the forwarding policy testable without a socket.
- **`gateway-go/internal/store/`** — one file per entity (`api.go`, `account.go`,
  `user.go`) over a shared `postgres.go` connection.
- **`gateway-go/internal/api/`** — the envelope, the error codes and the mapping
  from service errors to them.

### One browser click, end to end

1. The page calls a literal `/gw/...` path — never a user-constructed URL, so no
   user-controlled value reaches the fetch sink as a parameter.
2. `gwRoutes.ts` matches the path and resolves the backend path.
3. `BffGateway` reads the `gw_session` cookie, and if it is missing or invalid
   returns an envelope error rather than calling the backend.
4. The backend fetch runs server-side with the JWT in an `Authorization` header,
   under a timeout and an `AbortController`.
5. `gateway-go` authenticates the JWT, then the service layer loads the stored
   `ApiGateway` record for the identifier.
6. `GatewayForward` builds the outbound URL from the *stored* host and path,
   filters outbound headers and query parameters down to the stored allowlists,
   enforces the required-body gate, and calls the upstream.
7. The upstream response is mapped back into the envelope and returned to the
   BFF, which maps it once more for the browser.

## Architecture decisions worth defending

**The browser never holds a credential.**
*Decision:* the session is an `httpOnly`, `SameSite=Lax` cookie minted and read
only by the Next.js server. *Cost:* every backend interaction costs a server
round-trip, and the BFF has to exist as a deployment unit. *Benefit:* the
credential is not reachable from the page, which removes an entire class of
vulnerability rather than mitigating it.

**Allowlists are enforced twice, from two different sources.**
The BFF sanitizes what it forwards, and the Go service *re-derives* the outbound
request from the persisted configuration. *Cost:* a header that is allowed in the
config but stripped by the BFF fails confusingly. *Benefit:* no browser-supplied
value is ever trusted, so the gateway cannot be used as an open proxy even if the
BFF is bypassed.

**The URL is encoded exactly once.**
Query parsing is deliberately tolerant — `?foo` becomes `foo=""` and duplicate
keys are last-wins — and canonical encoding happens in a single place
(`service.BuildForwardURL`). *Cost:* the tolerant parse is a policy a caller has
to know about. *Benefit:* double-encoding bugs, which are the classic source of
"works in the UI, fails for the partner" tickets, cannot happen.

**One response envelope for every fault.**
*Decision:* `code: "00"` is success, and every failure maps to a numbered code in
one package. *Cost:* an extra translation step for upstream bodies. *Benefit:* the
UI has exactly one error path to implement, and the codes are stable enough to
document for other consumers.

**Reuse the platform before adding a dependency.**
`fetch`, `AbortController` and the platform's cookie handling are used rather than
an HTTP client or a session library. *Cost:* a little more code in the BFF.
*Benefit:* the dependency surface that has to be patched stays small — which is
the same reason the backend keeps `net/http`, `chi` and `pgx` and stops there.

## Running it

```bash
# 1 · database
cd project && docker compose up -d postgres
psql "$DATABASE_URL" -f project/pg-init-scripts/gateway.sql

# 2 · everything, including the UI, in containers
docker compose up -d --build          # postgres + gateway-go (8080) + frontend (3000)

# or run the halves natively
cd gateway-go && DATABASE_URL=postgres://microservices:password@localhost:5432/gateway?sslmode=disable go run ./cmd/server
cd frontend   && BACKEND_API_URL=http://localhost:8080 npm run dev

# tests
cd gateway-go && go build ./... && go vet ./... && go test ./...
cd frontend   && npm run lint && npm run typecheck && npm test && npm run build && npm run test:e2e
```

### What I observed

This is the part of the design that is easiest to *claim* and easiest to *check*.
After logging in, from inside the page:

```text
document.cookie => ""
session cookie  => gw_session httpOnly=true sameSite=Lax
BFF 200 /gw/auth/login
BFF 200 /gw/apis
BFF 200 /gw/stores
BFF 200 /gw/apis/detail/gateway-example-1
```

The session cookie exists, the backend calls all went through the BFF, and
`document.cookie` is empty: the token is not merely "not used by" the frontend,
it is unreadable to it.

A second observation, from the Try-it console. Forwarding to a public upstream
failed in my sandbox with:

```text
Get "https://api.thecatapi.com/v1/images/search": tls: failed to verify certificate:
x509: certificate signed by unknown authority
```

That is an environment fact, not a code fault — the container does not carry the
intercepting CA this network uses. It is worth showing anyway, because it
demonstrates the desired behaviour under failure: the gateway surfaces the
upstream's real error verbatim instead of collapsing it into a generic 500, which
is what makes an integration debuggable. Pointing the same configuration at an
internal endpoint completes the round trip and returns the upstream body unchanged:

![Try it, forwarding to an upstream](images/the-gateway-forward-response.png)

## Screenshots

![API list dashboard](images/the-gateway-dashboard.png)

*The API list: name, identifier, method badge and lifecycle status. Rows open the
configuration screen; `Store Account` is the machine-credential tab.*

![API configuration detail](images/the-gateway-api-detail.png)

*One API's configuration. The lower row is the security surface: the header
allowlist, the required-parameter allowlist and the required-body gate. These are
the values the Go forwarder re-enforces at request time — the UI is only where
they are edited.*

![Try it console](images/the-gateway-try-it.png)

*The Try-it console issues a real request through the whole pipeline, which is
why it is the most useful screen for debugging a partner integration.*

## What I'd fix next

Ordered by how much each item costs to leave alone:

- **The schema and seed rows are a separate step.** The Compose file mounts the
  init SQL, but a native run needs the `psql -f` command above before the UI has
  anything to show. A migration step at boot, or a health readout that says "0 APIs
  configured", would make a first run self-explaining.
- **`test:e2e` runs against port 3001** to avoid colliding with a local dev
  server; worth knowing when a run "hangs" on a busy machine.
- **The security posture depends on deployment, not code.** `COOKIE_SECURE` must
  be `true` behind TLS; the shipped Compose file sets it to `false` for local
  HTTP, which is correct locally and wrong anywhere else.
- **Forwarding inherits the host's CA trust.** As observed above, a container with
  a slim trust store will reject upstreams that the host accepts.

## Links

- **Source:** [github.com/mariobgsp/the-gateway](https://github.com/mariobgsp/the-gateway)
- **Tech-stack notes:** [`specs/tech-architecture/tech-stack.md`](https://github.com/mariobgsp/the-gateway/blob/main/specs/tech-architecture/tech-stack.md)
- **Agent guide:** [`CLAUDE.md`](https://github.com/mariobgsp/the-gateway/blob/main/CLAUDE.md)

---

*Verified against commit `15446fc` (2026-09-18). Diagram source:
[`diagrams/the-gateway.mmd`](diagrams/the-gateway.mmd). Capture script:
[`tools/capture/the-gateway.mjs`](tools/capture/the-gateway.mjs).*
