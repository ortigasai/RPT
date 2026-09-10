"""PostgreSQL persistence for extraction runs.

Mirrors the cwt-tax-portal setup: a single Postgres database (``RPT`` on the
tax server), connection string from ``DATABASE_URL`` in the environment / .env.

The app works fine without a database - if ``DATABASE_URL`` is unset or the
server is unreachable, every function here degrades to a no-op and logs a
warning, so extraction + Excel download keep working.

    python -m rpt.db check      # test connectivity
    python -m rpt.db upgrade    # create / update the schema  (deploy step)
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
from datetime import datetime, timezone

try:  # so `python -m rpt.db upgrade` sees .env without app.py importing first
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

log = logging.getLogger("rpt.db")

_engine = None
_checked = False
_ok = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_run (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    location     TEXT        NOT NULL,
    source       TEXT        NOT NULL DEFAULT '',
    files        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    row_count    INTEGER     NOT NULL DEFAULT 0,
    rows_flagged INTEGER     NOT NULL DEFAULT 0,
    seconds      NUMERIC     NOT NULL DEFAULT 0,
    columns      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    rows         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    notes        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    xlsx_name    TEXT        NOT NULL DEFAULT '',
    xlsx         BYTEA
);
CREATE INDEX IF NOT EXISTS ix_extraction_run_created ON extraction_run (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_extraction_run_location ON extraction_run (location);
"""


def _database_url() -> str | None:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        return None
    # SQLAlchemy needs the psycopg (v3) driver spelled out
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def engine():
    global _engine
    if _engine is None:
        url = _database_url()
        if not url:
            return None
        import sqlalchemy as sa
        _engine = sa.create_engine(
            url, pool_pre_ping=True, pool_size=5, max_overflow=5,
            connect_args={"connect_timeout": 8},
        )
    return _engine


def enabled() -> bool:
    return _database_url() is not None


def check() -> tuple[bool, str]:
    eng = engine()
    if eng is None:
        return False, "DATABASE_URL not set"
    try:
        import sqlalchemy as sa
        with eng.connect() as c:
            v = c.execute(sa.text("select version()")).scalar()
        return True, str(v)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def init() -> bool:
    """Create the schema if needed. Called at app startup and by 'upgrade'."""
    global _checked, _ok
    eng = engine()
    if eng is None:
        _checked, _ok = True, False
        log.warning("DATABASE_URL not set - runs will not be persisted")
        return False
    try:
        import sqlalchemy as sa
        with eng.begin() as c:
            for stmt in filter(str.strip, _SCHEMA.split(";")):
                c.execute(sa.text(stmt))
        _checked, _ok = True, True
        log.info("database ready")
        return True
    except Exception as e:  # noqa: BLE001
        _checked, _ok = True, False
        log.warning("database unavailable (%s) - runs will not be persisted", e)
        return False


def _ready() -> bool:
    global _checked
    if not _checked:
        init()
    return _ok


def record_run(*, location: str, source: str, files: list[str], row_count: int,
               rows_flagged: int, seconds: float, columns: list[str],
               rows: list[dict], notes: list[str], xlsx: bytes,
               xlsx_name: str) -> int | None:
    if not _ready():
        return None
    try:
        import sqlalchemy as sa
        with engine().begin() as c:
            rid = c.execute(sa.text("""
                INSERT INTO extraction_run
                  (location, source, files, row_count, rows_flagged, seconds,
                   columns, rows, notes, xlsx_name, xlsx)
                VALUES
                  (:loc, :src, :files, :rc, :rf, :sec,
                   :cols, :rows, :notes, :xn, :xb)
                RETURNING id
            """), {
                "loc": location, "src": source,
                "files": json.dumps(files), "rc": row_count, "rf": rows_flagged,
                "sec": seconds, "cols": json.dumps(columns),
                "rows": json.dumps(rows, default=str),
                "notes": json.dumps(notes),
                "xn": xlsx_name, "xb": xlsx,
            }).scalar()
        return int(rid)
    except Exception as e:  # noqa: BLE001
        log.warning("could not persist run: %s", e)
        return None


def list_runs(limit: int = 100) -> list[dict]:
    if not _ready():
        return []
    try:
        import sqlalchemy as sa
        with engine().connect() as c:
            rs = c.execute(sa.text("""
                SELECT id, created_at, location, source, files,
                       row_count, rows_flagged, seconds, xlsx_name
                FROM extraction_run ORDER BY created_at DESC LIMIT :n
            """), {"n": limit}).mappings().all()
        return [dict(r) | {"created_at": _iso(r["created_at"])} for r in rs]
    except Exception as e:  # noqa: BLE001
        log.warning("list_runs failed: %s", e)
        return []


def get_run(run_id: int) -> dict | None:
    if not _ready():
        return None
    try:
        import sqlalchemy as sa
        with engine().connect() as c:
            r = c.execute(sa.text("""
                SELECT id, created_at, location, source, files, row_count,
                       rows_flagged, seconds, columns, rows, notes, xlsx_name
                FROM extraction_run WHERE id = :id
            """), {"id": run_id}).mappings().first()
        if not r:
            return None
        return dict(r) | {"created_at": _iso(r["created_at"])}
    except Exception as e:  # noqa: BLE001
        log.warning("get_run failed: %s", e)
        return None


def get_xlsx(run_id: int) -> tuple[str, bytes] | None:
    if not _ready():
        return None
    try:
        import sqlalchemy as sa
        with engine().connect() as c:
            r = c.execute(sa.text(
                "SELECT xlsx_name, xlsx FROM extraction_run WHERE id = :id"
            ), {"id": run_id}).first()
        if not r or r[1] is None:
            return None
        return (r[0] or f"RPT_run_{run_id}.xlsx"), bytes(r[1])
    except Exception as e:  # noqa: BLE001
        log.warning("get_xlsx failed: %s", e)
        return None


def _iso(v) -> str:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    return str(v)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        ok, msg = check()
        print(("OK  " if ok else "FAIL ") + msg)
        sys.exit(0 if ok else 1)
    if cmd == "upgrade":
        sys.exit(0 if init() else 1)
    print(f"unknown command: {cmd!r} (use 'check' or 'upgrade')")
    sys.exit(2)
