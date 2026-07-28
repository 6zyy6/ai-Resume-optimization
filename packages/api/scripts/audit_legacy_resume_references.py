import argparse
import sqlite3
from pathlib import Path


def audit(database: Path) -> tuple[int, list[str]]:
    if not database.exists():
        return 0, []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'resumes'"
        ).fetchone()
        if table is None:
            return 0, []
        rows = connection.execute(
            """
            SELECT id FROM resumes
            WHERE kind = 'base'
              AND (base_resume_id IS NOT NULL OR job_description_id IS NOT NULL)
            ORDER BY id
            """
        ).fetchall()
        return len(rows), [row[0] for row in rows]
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    database = parser.parse_args().database.resolve()
    count, resource_ids = audit(database)
    print(f"legacy_base_reference_count={count}")
    print(f"legacy_base_reference_ids={','.join(resource_ids)}")
    raise SystemExit(1 if count else 0)


if __name__ == "__main__":
    main()
