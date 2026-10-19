"""
core/job_manager.py, Thread-safe asynchronous job and task state manager.
"""

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Job:
    id: str
    job_type: str
    status: str = "pending"  # "pending", "running", "completed", "failed", "cancelled"
    progress: float = 0.0
    progress_text: str = ""
    logs: List[str] = field(default_factory=list)
    result: Optional[Any] = None
    error: Optional[str] = None
    stop_requested: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    output_filename: Optional[str] = None
    output_data: Optional[bytes] = None

    def add_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {message}")
        self.updated_at = time.time()

    def update_progress(self, progress: float, text: str = "") -> None:
        self.progress = max(0.0, min(1.0, progress))
        if text:
            self.progress_text = text
        self.updated_at = time.time()

    def request_stop(self) -> None:
        self.stop_requested = True
        self.add_log("Stop requested by user.")
        self.updated_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "job_type": self.job_type,
            "status": self.status,
            "progress": round(self.progress * 100, 1),
            "progress_text": self.progress_text,
            "logs": self.logs,
            "error": self.error,
            "stop_requested": self.stop_requested,
            "has_result": self.result is not None,
            "has_download": self.output_data is not None,
            "output_filename": self.output_filename,
            "updated_at": self.updated_at,
        }


class JobManager:
    """In-memory thread-safe registry of background jobs."""

    def __init__(self, ttl_seconds: int = 3600):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._ttl_seconds = ttl_seconds

    def create_job(self, job_type: str) -> Job:
        job_id = uuid.uuid4().hex
        job = Job(id=job_id, job_type=job_type)
        with self._lock:
            self._cleanup_old_jobs()
            self._jobs[job_id] = job
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            self._cleanup_old_jobs()
            sorted_jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.to_dict() for j in sorted_jobs[:limit]]

    def cancel_job(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if job and job.status in ("pending", "running"):
            job.request_stop()
            return True
        return False

    def start_job_thread(self, job: Job, target_fn: Callable[[Job], None]) -> threading.Thread:
        def _wrapper():
            job.status = "running"
            job.updated_at = time.time()
            try:
                target_fn(job)
                if job.stop_requested:
                    job.status = "cancelled"
                elif job.status == "running":
                    job.status = "completed"
                    job.progress = 1.0
            except Exception as exc:
                # str(exc) alone loses *where* the failure happened, which
                # made failed jobs hard to diagnose from /api/status/<id>.
                # Log the full traceback into the job's own log stream too.
                tb = traceback.format_exc()
                job.status = "failed"
                job.error = str(exc)
                job.add_log(f"Error: {exc}")
                job.add_log(tb)
            finally:
                job.updated_at = time.time()

        thread = threading.Thread(target=_wrapper, daemon=True)
        thread.start()
        return thread

    def _cleanup_old_jobs(self) -> None:
        now = time.time()
        expired_ids = [
            jid for jid, j in self._jobs.items()
            if now - j.created_at > self._ttl_seconds
        ]
        for jid in expired_ids:
            del self._jobs[jid]


# Global instance
job_manager = JobManager()
