import importlib.util
from pathlib import Path
from types import SimpleNamespace


def load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0008_split_immutable_state_triggers.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0008", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_postgresql_upgrade_uses_table_specific_trigger_functions(monkeypatch):
    migration = load_migration()
    statements = []
    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    sql = "\n".join(statements)
    assert "enforce_terminal_task_result_immutable()" in sql
    assert "enforce_suggestion_accept_from_pending()" in sql
    assert "enforce_export_version_immutable()" in sql
    assert "EXECUTE FUNCTION enforce_immutable_state()" not in sql
