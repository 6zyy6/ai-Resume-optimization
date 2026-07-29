# AI runtime dependency notes

| Dependency | Purpose | License |
| --- | --- | --- |
| `redis` 6.1.0 | Shared Pi run state, 24-hour TTL and cross-replica cancellation notifications | MIT |

The Redis client replaces process-local run ownership in production. Redis
contains only short-lived coordination state; PostgreSQL remains the source of
business and audit facts.
