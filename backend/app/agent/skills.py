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
}


def skill_names() -> list[str]:
    return sorted(SKILLS)


def skill_text(name: str) -> str:
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    if key in SKILLS:
        return SKILLS[key]
    return f"No skill sheet named '{name}'. Available: {', '.join(skill_names())}"
