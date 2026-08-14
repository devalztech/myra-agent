"""SQLAlchemy engine/session wiring (SQLite only)."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.database_url,
    future=True,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _enable_sqlite_pragmas(dbapi_connection, _record) -> None:
    """WAL + foreign keys on every new connection.

    WAL lets reads and writes overlap instead of locking the whole file,
    which matters once the scheduler and a live chat request hit the
    database at the same time. Foreign keys are off by default in SQLite,
    so without this the ON DELETE CASCADE in models.py is silently ignored.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _add_missing_columns() -> None:
    """Best-effort ALTER TABLE for columns added after a DB already exists.

    There's no migration framework here (create_all only ever adds whole
    tables, never columns to an existing one), so a column added to a
    model in code — like Memory.deleted_at below — would otherwise be
    silently missing on any database created before that change. This
    walks each mapped table, diffs its columns against what SQLite
    actually has via PRAGMA table_info, and ALTERs in whatever's missing.
    Safe to run on every boot: already-migrated tables are a no-op.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all will create it fresh, already correct
            have = {row[1] for row in conn.execute(text(f'PRAGMA table_info("{table.name}")'))}
            for column in table.columns:
                if column.name in have:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                conn.execute(text(ddl))


def init_db() -> None:
    """Create tables if they do not exist yet, then patch missing columns."""
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
