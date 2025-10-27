# IOQueue

A lightweight async job runner that executes Django-based I/O tasks from either a persistent database queue or a transient in-memory queue. The system is optimized for bounded-concurrency workloads where tasks are declared in Python and dispatched from application code.

## Architecture Overview
- **Registry (`registry.py`)** – Holds the task registry and decorator used to register/submit jobs. Non-persistent tasks are serialized and pushed to a Redis list for cross-process consumption.
- **Model (`models.py`)** – Stores durable jobs in the `IOJob` table, tracking status, scheduling metadata, retry counters, and results.
- **Brokers (`broker.py`)** –
  - `DBBroker` moves jobs out of the database, applies visibility timeouts, and persists completion or retry decisions.
  - `MemoryBroker` bridges the in-memory queue to the runner.
- **Runner (`runner.py`)** – Maintains an asyncio event loop, pulls jobs from both brokers, and executes task callables with a concurrency semaphore.

## Defining Tasks
Use the `@io_task` decorator to register a callable.

```python
from backend.apps.service.ioqueue.registry import io_task

@io_task(max_retries=5, dedupe=True)
def fetch_pdf(paper_id: int):
    ...
```

### Decorator Options
- `name`: override the default `<module>.<function>` identifier.
- `max_retries` (default `3`): maximum retry attempts for persistent jobs.
- `dedupe` (`False`): when `True`, identical arg/kwarg payloads reuse the same queued job while it is `pending` or `running`.
- `persist` (`True`): set to `False` to skip the database and run the task as a fire-and-forget memory job.

> **Note:** Persistent jobs must accept JSON-serializable arguments. Serialization validates at submission time and raises `ValueError` when the payload cannot be encoded.

## Submitting Work
Calling the decorated function enqueues the task:

- **Persistent jobs** return the `IOJob.id`. The call writes the payload into the database and will be picked up by `DBBroker`.
- **Memory jobs** return `None`. The payload is pushed into the Redis queue configured by `IOQUEUE_REDIS_URL`/`IOQUEUE_REDIS_QUEUE_KEY` for best-effort execution.

If you enable deduplication, use bounded argument payloads so the computed dedupe key fits the `IOJob.dedupe_key` column limit (255 characters).

## Running the Worker
Start the service via the Django management command:

```bash
PYTHONPATH=. python3 backend/manage.py run_ioqueue
```

The runner:
1. Imports every module listed in the `IOQUEUE_TASK_MODULES` Django setting, forcing task registration.
2. Spawns two fetch loops (`DBBroker` for the database, `MemoryBroker` for Redis) and a worker loop that fans out execution up to `IOQUEUE_MAX_CONCURRENCY` tasks at a time.
3. Listens for `SIGINT`/`SIGTERM` and drains in-flight work before exiting.

Each process announces itself as `<hostname>:<pid>` and records that identifier in `IOJob.picked_by` for visibility.

## Configuration Settings
All values are optional; sensible defaults are provided.

| Setting                                    | Default | Description |
|--------------------------------------------| --- | --- |
| `IOQUEUE_TASK_MODULES(CURRENTLY DISABLED)` | `[]` | List of import strings that define decorated tasks. Loaded on worker start. |
| `IOQUEUE_MAX_CONCURRENCY`                  | `8` | Number of concurrent task executions guarded by an asyncio semaphore. |
| `IOQUEUE_VISIBILITY_TIMEOUT_SEC`           | `300` | Visibility window after a job is picked. Expired jobs return to the queue. |
| `IOQUEUE_POLL_INTERVAL_SEC`                | `0.5` | Sleep interval for the DB fetcher when no work is found. |
| `IOQUEUE_REDIS_URL`                        | `${REDIS_URL}/5` | Redis connection string used by the memory queue. |
| `IOQUEUE_REDIS_QUEUE_KEY`                  | `ioqueue:memory` | Redis list key that stores serialized memory tasks. |
| `IOQUEUE_MEMORY_BLPOP_TIMEOUT_SEC`         | `5` | Timeout (seconds) for Redis `BLPOP` before the worker rechecks shutdown signals. |

## Job Lifecycle (Persistent Tasks)
1. Jobs start in `pending` status with `scheduled_at` defaulting to `timezone.now()`.
2. `DBBroker` locks the row, flips the status to `running`, and sets a `visible_until` timestamp.
3. The runner executes the callable, respecting the concurrency semaphore.
4. `DBBroker.finalize` updates the row:
   - Success → `status="done"`, `result` populated, error cleared.
   - Failure → increments `attempts`. Exponential backoff (`2 ** (attempts - 1)` seconds, capped at 60) reschedules the job unless `attempts > max_retries`, which moves it to `error`.

## Memory Queue Behaviour
- Designed for transient tasks that should not be retried or persisted.
- Uses Redis for transport, so producers and consumers may live in separate processes or hosts as long as they share the same Redis instance/key.
- Task payloads are pickled; avoid untrusted producers and keep the worker code in sync with submitters.
- Failures are logged to stdout and discarded; extend `MemoryBroker` or the runner if you need monitoring hooks.

## Operational Notes
- Ensure the worker has database access and runs alongside your web processes or as a separate service.
- When deploying multiple workers, each process will fetch distinct jobs thanks to `select_for_update(skip_locked=True)`.
- Track queue health by inspecting `IOJob` rows (`status`, `attempts`, `last_error`, `picked_by`). Consider adding admin views or metrics if you rely heavily on background processing.
- Add tests around your task functions—persistent jobs will be retried automatically, but idempotence makes retries safer.

For implementation details or extension points, read the modules within this directory (`broker.py`, `registry.py`, `runner.py`, `models.py`).
