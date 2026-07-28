from dataclasses import dataclass


TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
TRANSITIONS = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"waiting_for_user", "succeeded", "failed", "cancelled"}),
    "waiting_for_user": frozenset({"queued", "running", "failed", "cancelled"}),
}


@dataclass
class TaskStateError(Exception):
    code: str
    message: str


def require_transition(current: str, target: str) -> None:
    if current in TERMINAL_STATUSES:
        raise TaskStateError("TASK_TERMINAL", "Terminal task state cannot change")
    if target not in TRANSITIONS.get(current, frozenset()):
        raise TaskStateError(
            "TASK_STATE_INVALID",
            f"Task cannot transition from {current} to {target}",
        )
