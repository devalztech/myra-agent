"""Built-in skill sheets.

Skills keep the system prompt small: instead of stuffing every convention into
every request, the model asks for the sheet it needs with the ``get_skill``
tool. Each sheet is short, opinionated and practical.
"""

from __future__ import annotations

SKILLS: dict[str, str] = {
    "javascript": """JavaScript (ES2022+)
- Modules: ESM (`import`/`export`), no CommonJS in new code.
- `const` by default, `let` when reassigned, never `var`.
- Async: `async/await` + `try/catch`; never floating promises.
- Prefer array methods (`map`/`filter`/`reduce`) over index loops.
- Optional chaining `?.` and nullish `??` instead of `&&` chains.
- Tests: `node --test` or vitest.""",
    "typescript": """TypeScript
- `strict: true`; never `any` — use `unknown` + narrowing.
- Model data with `type` aliases; `interface` for extendable object contracts.
- Discriminated unions over boolean flags.
- `satisfies` to check object literals without widening.
- Validate external input at the boundary (zod) and infer types from schemas.""",
    "react": """React 18/19
- Function components + hooks only.
- Derive state; don't duplicate it. `useEffect` is for synchronising with
  external systems, not for computing values.
- Keys must be stable IDs, never array indexes.
- Split components when a file passes ~200 lines.
- Data fetching via a query library or the router's loader, not ad-hoc effects.""",
    "node": """Node.js
- Read config from `process.env` at startup and validate it once.
- Use `node:` prefixed core imports.
- Streams for large payloads; never buffer whole files in memory.
- Graceful shutdown on SIGTERM/SIGINT.
- Never block the event loop: heavy CPU work goes to worker threads.""",
    "express": """Express
- Order: security headers -> body parsers -> routes -> 404 -> error handler.
- Every async handler wrapped so rejections reach the error middleware.
- Validate params/body before touching the database.
- Return JSON errors as `{ error: { message, code } }` with correct status.""",
    "postgresql": """PostgreSQL
- Explicit schemas + migrations; never mutate production by hand.
- Parameterised queries only ($1, $2) — no string interpolation.
- Index the columns you filter/join on; verify with EXPLAIN ANALYZE.
- Use transactions for multi-statement writes.
- Prefer `timestamptz`, `numeric` for money, `uuid` for keys.""",
    "sqlite": """SQLite
- Enable `PRAGMA foreign_keys = ON` and WAL mode for concurrency.
- One writer at a time — wrap writes in short transactions.
- `INTEGER PRIMARY KEY` is the rowid alias; use TEXT for uuids.
- Myra's own database (users/sessions/memories) is SQLite-only — don't
  suggest Postgres for it unless the user explicitly asks to change that.""",
    "python": """Python 3.11+
- Type hints everywhere; `from __future__ import annotations`.
- Prefer dataclasses/pydantic over dicts for structured data.
- Context managers for files, locks and connections.
- Raise specific exceptions; never bare `except:`.
- Tests with pytest; format with ruff/black conventions (88-100 cols).""",
    "cpp": """C++17/20
- RAII always; no raw `new`/`delete`. `unique_ptr`/`shared_ptr` for ownership.
- Pass by `const&` for large types; move for transfers.
- `constexpr` and `enum class` over macros.
- Build with CMake; compile with -Wall -Wextra -Werror.
- Prefer `std::` containers/algorithms over hand-rolled loops.""",
    "html": """HTML
- Semantic landmarks: header/nav/main/section/footer; exactly one h1.
- Labels tied to inputs; alt text on every meaningful image.
- Meta viewport + descriptive title/description.
- Buttons for actions, anchors for navigation.""",
    "css": """CSS
- Design tokens as custom properties; no hard-coded colours in components.
- Flexbox/grid for layout; avoid absolute positioning for structure.
- Mobile-first media queries; respect `prefers-reduced-motion`.
- Keep specificity flat; no `!important`.""",
    "nextjs": """Next.js (App Router)
- Use server components by default; add 'use client' only for interactivity.
- Route handlers and server actions for mutations; keep data fetching server-side.
- Read params/searchParams as promises; validate with zod at the boundary.
- Static by default; revalidate/dynamic where freshness matters.
- Prefer the built-in image/font components over raw tags.""",
    "vue": """Vue 3 (Composition API)
- <script setup> single-file components; no Options API in new code.
- Use computed for derived state, ref/reactive for state.
- emit/defineProps for component contracts; provide/inject for shared deps.
- Keep components focused; split at ~200 lines.
- Pinia for app state when a store is needed.""",
    "svelte": """Svelte 5 (runes)
- $state/$derived/$props runes over legacy reactive declarations.
- SvelteKit for routing/SSR; load() functions for data.
- Let the framework own reactivity — no manual subscribe/unsubscribe.
- Type props and events; keep components small and single-purpose.""",
    "tailwind": """Tailwind CSS
- Utility classes in markup; extract to components, not CSS.
- Use the design-token scale (spacing, colors) — no arbitrary values unless needed.
- Responsive: mobile-first breakpoints (sm: md: lg:).
- Group related utilities; avoid deep nesting and @apply for simple cases.
- Dark mode via class strategy when required.""",
    "fastapi": """FastAPI
- Define request/response with Pydantic models; never accept raw dicts.
- Path/query/body params typed; validation happens at the boundary.
- Dependency injection for auth, db sessions, and shared services.
- async def for I/O-bound endpoints; def for CPU-bound.
- Document with OpenAPI; version endpoints; add health and error handlers.""",
    "flask": """Flask
- Application factory pattern; blueprints per resource.
- SQLAlchemy (or similar) for models; migrate with Alembic/Flask-Migrate.
- Blueprint error handlers; return JSON as {error: {message, code}}.
- Never trust client input; validate and sanitize.
- Keep config in a class hierarchy loaded from env.""",
    "django": """Django
- Apps for bounded contexts; keep models thin, logic in services/managers.
- Use the ORM (no raw SQL unless required) and migrations for schema.
- Class-based views or DRF serializers for APIs.
- Settings via env-based config; never commit secrets.
- Tests with pytest-django; factories over fixtures where convenient.""",
    "nestjs": """NestJS
- Modules per feature; controllers for HTTP, services for logic.
- DTOs with class-validator; enable global ValidationPipe.
- Guards for auth, interceptors for logging/transform, pipes for validation.
- Use the built-in DI container; keep services decoupled from controllers.
- Jest + supertest for testing; OpenAPI/Swagger for docs.""",
    "graphql": """GraphQL
- Schema-first or code-first consistently; never mix.
- Resolvers stay thin — delegate to services.
- N+1: use a DataLoader for batched relations.
- Always provide pagination for list fields; validate args.
- Persisted/masked errors: don't leak internal messages to clients.""",
    "websockets": """WebSockets
- Client reconnects with backoff and jitter; server heartbeats/ping.
- Handle partial messages; validate and size-limit payloads.
- Auth on connect (token/cookie); re-check on each message.
- Graceful close on shutdown; clean up per-connection state.
- Backpressure: don't buffer unboundedly when the client is slow.""",
    "pytest": """pytest
- Arrange-Act-Assert; one behaviour per test.
- Fixtures for setup/teardown; scope them (function/class/module/session).
- Parametrize for table-driven cases; name tests descriptively.
- Mock external calls at the boundary; assert on behaviour not implementation.
- Aim for fast, deterministic, isolated tests.""",
    "jest": """Jest
- __tests__ colocated or in a tests dir — be consistent.
- useFakeTimers for time-dependent code; restore after each test.
- Mock modules with jest.mock; mock fetch/axios at the boundary.
- Prefer testing behaviour over implementation details.
- Coverage as a signal, not a gate.""",
    "vitest": """Vitest
- Native ESM + TS; fast watch mode.
- Test files *.test.ts / *.spec.ts colocated near source.
- vi.mock for modules; vi.fn for spies.
- jsdom for DOM, happy-dom as a lighter alternative.
- Use describe/it blocks with clear naming.""",
    "playwright": """Playwright (E2E)
- Use role-based locators (getByRole) over CSS/text where possible.
- Prefer user-visible flows; avoid implementation-coupled selectors.
- Handle waits with expect auto-retry, not fixed sleeps.
- One browser context per test; parallel by default.
- Assert on user outcomes (visible state), not internal state.""",
    "docker": """Docker
- Prefer slim/runtime base images; multi-stage builds for compiled apps.
- Run as non-root; least privilege for packages/user.
- One concern per container; healthchecks for services.
- .dockerignore to keep context small; reproducible versions (no latest).
- Compose for local multi-service; named volumes for data.""",
    "github_actions": """GitHub Actions
- Trigger on the events that matter (push, PR, schedule); avoid duplicate runs.
- Pin action versions by tag/commit for supply-chain safety.
- Keep jobs fast: cache deps, run in parallel when independent.
- Secrets via env from the runner context, never inline.
- Fail loudly; upload artifacts/logs on failure for debugging.""",
    "oauth": """OAuth 2.0 / OpenID Connect
- Authorization Code + PKCE for web/native; never the implicit flow.
- Store tokens server-side; refresh tokens rotated and protected.
- Validate id_token signature, issuer, audience, and expiry.
- Scope least privilege; never put secrets in the frontend.
- Redirect URI allow-list; state/CSRF protection on auth flows.""",
    "jwt": """JWT
- Sign with RS256 (or HS256 only with a strong, secret key).
- Short expiry; refresh tokens for rotation; revoke on logout.
- Validate signature, issuer, audience, expiry, and not-before.
- Never put sensitive data in the token payload; keep it small.
- Reject unknown 'alg' values to avoid algorithm-confusion attacks.""",
    "openapi": """OpenAPI
- Define schemas once; share between request/response and examples.
- Version the API; mark breaking changes clearly.
- Document auth, errors, and pagination consistently.
- Generate clients/docs from the spec to avoid drift.
- Keep operationIds stable for tooling.""",
    "rest": """REST
- Nouns for resources, HTTP verbs for actions; stateless servers.
- Consistent error shape: {error: {message, code, field?}}.
- Paginate list endpoints; version the API.
- Validate input; return correct status codes (201/204/400/401/404/409/422).
- HATEOAS/link relations when clients need to discover flows.""",
    "sqlalchemy": """SQLAlchemy
- Declarative models; explicit column types and constraints.
- Use session-per-request; commit/rollback in a context manager.
- Relationships with clear cascade rules; avoid N+1 (joinedload/selectinload).
- Write migrations with Alembic; never auto-create schema in prod.
- Parameterised queries; prefer the ORM, drop to text() when needed.""",
    "git": """Git
- Small, focused commits with imperative subject lines.
- Branch per feature/fix; keep history linear where possible (rebase).
- Write a meaningful commit body: what + why.
- Never commit secrets, build artifacts, or large binaries.
- Review your own diff before pushing.""",
    "bash": """Bash
- set -euo pipefail at the top of scripts.
- Quote variables; use arrays for lists of words.
- Prefer built-ins over external commands when equivalent.
- Handle failures explicitly; provide clear exit codes.
- Keep scripts idempotent and re-runnable.""",
    "sql": """SQL
- Write SELECTs explicitly (no SELECT * in code).
- Use indexes for filtered/joined columns; explain-slow queries.
- Transactions for multi-statement writes.
- Prefer parameterised queries everywhere.
- Name constraints and indexes descriptively.""",
    "security": """Security basics
- Validate and sanitize ALL external input (injection, XSS).
- Never log or store secrets/keys in plaintext; redact them.
- Use parameterised queries; escape output; set security headers.
- AuthN/Z: least privilege, verify on every request, don't roll your own crypto.
- Keep dependencies patched; review for known CVEs.
- Rate-limit auth and abuse-prone endpoints; fail closed.""",
}

ALIASES = {
    "js": "javascript",
    "ts": "typescript",
    "reactjs": "react",
    "nodejs": "node",
    "postgres": "postgresql",
    "pg": "postgresql",
    "c++": "cpp",
    "cplusplus": "cpp",
    "next": "nextjs",
    "tailwindcss": "tailwind",
    "actions": "github_actions",
    "gh-actions": "github_actions",
    "security": "security",
    "oauth2": "oauth",
    "openid": "oauth",
    "shell": "bash",
}


def skill_names() -> list[str]:
    return sorted(SKILLS)


def skill_text(name: str) -> str:
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    if key in SKILLS:
        return SKILLS[key]
    return f"No skill sheet named '{name}'. Available: {', '.join(skill_names())}"
