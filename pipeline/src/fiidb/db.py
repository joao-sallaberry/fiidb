from collections.abc import Iterable, Sequence
from datetime import datetime

import psycopg


def connect(url: str) -> psycopg.Connection:
    # Autocommit so each `conn.transaction()` block is a real, independent transaction.
    return psycopg.connect(url, autocommit=True)


def upsert(
    conn: psycopg.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence],
    key: Sequence[str],
    *,
    update: Sequence[str] | None = None,
    where: str | None = None,
) -> int:
    """Bulk upsert through COPY into a staging table. `rows` must not repeat a key.
    `update` defaults to every non-key column; `where` guards the DO UPDATE (may reference
    `excluded` and the target table)."""
    update = [c for c in columns if c not in key] if update is None else update
    cols = ", ".join(columns)
    stage = f"_stage_{table}"
    with conn.cursor() as cur:
        cur.execute(f"create temp table {stage} as select {cols} from {table} with no data")
        with cur.copy(f"copy {stage} ({cols}) from stdin") as copy:
            for row in rows:
                copy.write_row(row)
        action = "do update set " + ", ".join(f"{c} = excluded.{c}" for c in update) if update else "do nothing"
        if update and where:
            action += f" where {where}"
        cur.execute(f"insert into {table} ({cols}) select {cols} from {stage} on conflict ({', '.join(key)}) {action}")
        affected = cur.rowcount
        cur.execute(f"drop table {stage}")
    return affected


def last_run(conn: psycopg.Connection, source: str, period: str) -> tuple[str, str | None] | None:
    """(status, sha256) of the latest run for a source/period, or None."""
    return conn.execute(
        "select status, sha256 from ingestion_run where source = %s and period = %s order by started_at desc limit 1",
        (source, period),
    ).fetchone()


def record_run(
    conn: psycopg.Connection,
    *,
    source: str,
    period: str,
    status: str,
    started_at: datetime,
    url: str | None = None,
    sha256: str | None = None,
    rows: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "insert into ingestion_run (source, period, url, sha256, rows, status, error, started_at, finished_at) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())",
        (source, period, url, sha256, rows, status, error, started_at),
    )
