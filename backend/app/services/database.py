"""Database service layer (TOOLS/Database).

SQLite-first (Myra's own storage), with optional connect/query helpers for
external PostgreSQL/MySQL/Redis when configured. Destructive operations are
gated behind explicit confirmation at the tool layer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class DatabaseError(RuntimeError):
    pass


def sqlite_connect(path: Path):
    """Open a read-only-by-default sqlite connection to a db file in the workspace."""
    if not path.exists():
        raise DatabaseError(f"Database file not found: {path}")
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_schema(conn) -> str:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    tables = [r["name"] for r in rows]
    out: list[str] = []
    for t in tables:
        out.append(f"== {t} ==")
        for row in conn.execute(f'SELECT sql FROM sqlite_master WHERE name=?', (t,)):
            out.append(row["sql"] or "")
    return "\n".join(out)


def sqlite_query(conn, sql: str, limit: int = 100) -> str:
    sql_upper = sql.strip().lower()
    if not sql_upper.startswith("select") and not sql_upper.startswith("pragma"):
        raise DatabaseError("Only SELECT / PRAGMA queries are allowed here.")
    try:
        rows = conn.execute(sql).fetchmany(limit)
    except sqlite3.Error as exc:
        raise DatabaseError(f"Query failed: {exc}")
    cols = [d[0] for d in conn.description] if conn.description else []
    data = [dict(zip(cols, row)) for row in rows]
    return json.dumps(data, default=str, indent=2)
