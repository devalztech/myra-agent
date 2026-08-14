"""Named workflow definitions (WORKFLOWS in the target architecture).

Each workflow is a short, model-facing procedure that tells Myra how to carry
out a recurring task end-to-end (debugging, testing, web preview, deployment,
recovery). They are injected into the agent's context by the workflow tool so
the model follows the right sequence instead of improvising.

Deliberately lightweight: a workflow is a prompt template + a list of the
tools it typically needs, not a separate execution engine. The agent loop
drives it.
"""

from __future__ import annotations

WORKFLOWS: dict[str, str] = {
    "debug": """DEBUG WORKFLOW
1. Reproduce: understand the error and get it to fail (read the traceback/log).
2. Locate: find the exact file/line/function responsible.
3. Diagnose: explain the root cause before changing anything.
4. Fix: make the smallest correct edit.
5. Verify: run the test/build to confirm the fix.
6. Regression: ensure the fix didn't break anything else.
Never report "fixed" until step 5 passed.""",
    "test": """TEST WORKFLOW
1. Discover: find the test runner and test files (pytest/vitest/jest).
2. Run: execute the relevant tests.
3. Analyze: on failure, read the traceback and isolate the failing assertion.
4. Fix: correct the code or the test (whichever is actually wrong).
5. Verify: re-run to green, then run the whole suite once.
Report the final pass/fail counts.""",
    "web": """WEB WORKFLOW
1. Build the project (npm install / pip install as needed).
2. Start the dev/preview server.
3. Find the port/URL it listens on.
4. Open it (browser or http_fetch) and test the key pages.
5. Fix issues found, reload, and re-verify.
6. Confirm the final state with a health check.""",
    "deploy": """DEPLOY WORKFLOW
1. Prepare: confirm what is being deployed and where.
2. Build: produce the artifact (frontend build, backend package).
3. Configure: set env/keys/config for the target.
4. Deploy: move the artifact to the target environment.
5. Health check: verify the app responds correctly after deploy.
6. Rollback plan: know how to revert if the health check fails.""",
    "recover": """RECOVERY WORKFLOW
1. Checkpoint: know the last known-good state (git, snapshot, backup).
2. Detect: identify what failed and how far it got.
3. Restore: return to the last known-good state.
4. Retry: re-apply the change more carefully.
5. Verify: confirm the system is healthy again.""",
    "api": """API WORKFLOW
1. Design: define routes, methods, request/response models.
2. Implement: write the endpoints (FastAPI recommended).
3. Test: hit each endpoint with valid + invalid inputs.
4. Document: OpenAPI/schemas for the API.
5. Verify: confirm auth, validation, and error handling behave correctly.""",
    "database": """DATABASE WORKFLOW
1. Inspect: understand the current schema and data.
2. Design: plan the schema/migration change.
3. Migrate: apply it as a proper migration (Alembic for SQLAlchemy).
4. Seed: add any test/seed data.
5. Test: query to confirm the change works.
6. Verify: check constraints and indexes are correct.
Destructive DB ops (drop/truncate/delete) require explicit confirmation.""",
    "git": """GIT WORKFLOW
1. Branch: start from a clean, current branch.
2. Implement: make the change.
3. Test: ensure it passes.
4. Commit: a focused commit with a clear message.
5. Push: to the remote.
6. PR: open a pull request describing the change.""",
}

WORKFLOW_NAMES = sorted(WORKFLOWS)


def workflow_text(name: str) -> str:
    return WORKFLOWS.get((name or "").strip().lower(), "")
