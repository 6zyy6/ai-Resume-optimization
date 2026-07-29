from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for trigger, table in (
        ("trg_terminal_task_result_immutable", "tasks"),
        ("trg_suggestion_accept_from_pending", "suggestions"),
        ("trg_export_version_immutable", "exports"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS enforce_immutable_state()")

    op.execute(
        """
        CREATE FUNCTION enforce_terminal_task_result_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF OLD.status IN ('succeeded', 'failed', 'cancelled')
             AND NEW.result_ref IS DISTINCT FROM OLD.result_ref THEN
            RAISE EXCEPTION 'terminal task result is immutable'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_suggestion_accept_from_pending()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF NEW.status = 'accepted' AND OLD.status <> 'pending' THEN
            RAISE EXCEPTION 'suggestion must be pending before acceptance'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_export_version_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF NEW.resume_version_id IS DISTINCT FROM OLD.resume_version_id THEN
            RAISE EXCEPTION 'export version is immutable'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_terminal_task_result_immutable
        BEFORE UPDATE ON tasks
        FOR EACH ROW
        EXECUTE FUNCTION enforce_terminal_task_result_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_suggestion_accept_from_pending
        BEFORE UPDATE ON suggestions
        FOR EACH ROW
        EXECUTE FUNCTION enforce_suggestion_accept_from_pending()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_export_version_immutable
        BEFORE UPDATE ON exports
        FOR EACH ROW
        EXECUTE FUNCTION enforce_export_version_immutable()
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for trigger, table in (
        ("trg_terminal_task_result_immutable", "tasks"),
        ("trg_suggestion_accept_from_pending", "suggestions"),
        ("trg_export_version_immutable", "exports"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for function in (
        "enforce_terminal_task_result_immutable",
        "enforce_suggestion_accept_from_pending",
        "enforce_export_version_immutable",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")

    op.execute(
        """
        CREATE FUNCTION enforce_immutable_state()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_TABLE_NAME = 'tasks'
             AND OLD.status IN ('succeeded', 'failed', 'cancelled')
             AND NEW.result_ref IS DISTINCT FROM OLD.result_ref THEN
            RAISE EXCEPTION 'terminal task result is immutable'
              USING ERRCODE = '23514';
          END IF;
          IF TG_TABLE_NAME = 'suggestions'
             AND NEW.status = 'accepted'
             AND OLD.status <> 'pending' THEN
            RAISE EXCEPTION 'suggestion must be pending before acceptance'
              USING ERRCODE = '23514';
          END IF;
          IF TG_TABLE_NAME = 'exports'
             AND NEW.resume_version_id IS DISTINCT FROM OLD.resume_version_id THEN
            RAISE EXCEPTION 'export version is immutable'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    for trigger, table in (
        ("trg_terminal_task_result_immutable", "tasks"),
        ("trg_suggestion_accept_from_pending", "suggestions"),
        ("trg_export_version_immutable", "exports"),
    ):
        op.execute(
            f"""
            CREATE TRIGGER {trigger}
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION enforce_immutable_state()
            """
        )
