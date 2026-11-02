"""Background job runner.

Design note: the brief suggests Redis/BullMQ. To keep the project runnable with zero infrastructure we use a
bounded in-process worker pool whose *state lives in the database* (status/stage/attempts), so jobs survive
restarts via `recover_pending()`. `enqueue()` is the single seam: swap its body for `celery_app.send_task(...)`
or an RQ enqueue to move workers to separate machines (see docs/ARCHITECTURE.md)."""
import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import Document

log = logging.getLogger(__name__)
_pool: ThreadPoolExecutor | None = None


def start():
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=settings.worker_threads, thread_name_prefix="docworker")


def shutdown():
    global _pool
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


def enqueue(doc_id: str):
    from .services import process_document

    start()
    _pool.submit(process_document, doc_id)


def recover_pending() -> int:
    """After a crash/restart: re-queue everything that was queued or mid-processing."""
    with session_scope() as s:
        ids = s.scalars(select(Document.id).where(Document.status.in_(("queued", "processing")))).all()
    for i in ids:
        enqueue(i)
    return len(ids)
