import argparse
import asyncio
import sqlite3
from pathlib import Path
from typing import Any


_LEGACY_QUERY = """
SELECT id FROM resumes
WHERE kind = 'base'
  AND (base_resume_id IS NOT NULL OR job_description_id IS NOT NULL)
ORDER BY id
"""


class _AsyncpgResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchall(self) -> list[Any]:
        return self.rows


class _AsyncpgConnection:
    def __init__(self, url: str) -> None:
        import asyncpg

        self.loop = asyncio.new_event_loop()
        dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        self.connection = self.loop.run_until_complete(asyncpg.connect(dsn))

    def execute(self, statement: str) -> _AsyncpgResult:
        if statement.lstrip().upper().startswith("SELECT"):
            rows = self.loop.run_until_complete(self.connection.fetch(statement))
            return _AsyncpgResult(rows)
        self.loop.run_until_complete(self.connection.execute(statement))
        return _AsyncpgResult([])

    def close(self) -> None:
        self.loop.run_until_complete(self.connection.close())
        self.loop.close()


def _connect_read_only(url: str) -> _AsyncpgConnection:
    return _AsyncpgConnection(url)


def audit(database: str | Path) -> tuple[int, list[str]]:
    if str(database).startswith(("postgresql://", "postgresql+")):
        connection = _connect_read_only(str(database))
        try:
            connection.execute("BEGIN READ ONLY")
            rows = connection.execute(_LEGACY_QUERY).fetchall()
            return len(rows), [row[0] for row in rows]
        finally:
            connection.close()
    database = Path(database)
    if not database.exists():
        return 0, []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'resumes'"
        ).fetchone()
        if table is None:
            return 0, []
        rows = connection.execute(_LEGACY_QUERY).fetchall()
        return len(rows), [row[0] for row in rows]
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    database = parser.parse_args().database
    if not database.startswith(("postgresql://", "postgresql+")):
        database = str(Path(database).resolve())
    count, resource_ids = audit(database)
    print(f"legacy_base_reference_count={count}")
    print(f"legacy_base_reference_ids={','.join(resource_ids)}")
    raise SystemExit(1 if count else 0)


if __name__ == "__main__":
    main()
