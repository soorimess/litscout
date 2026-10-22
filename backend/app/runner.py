"""Runs review jobs as separate `litscout` processes, at most N at a time.

Each job gets its own run folder (runs/<run_name>) with the pipeline's store and a job.log. The runner
polls its processes, deletes uploaded PDFs once their seeds are imported, and classifies a finished
process from its exit code and log. Processes run in their own session, so they survive a restart of
the web server; after a restart they are tracked by pid."""
from __future__ import annotations

import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select

from .config import WebSettings
from .db import Database
from .models import Job

log = logging.getLogger(__name__)
ACTIVE = ("running",)
DONE_MARK = " done: "
PAUSE_MARK = "Review criteria drafted"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie child of this process still answers kill(0); reap it if it is ours.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def log_tail(path: Path, max_bytes: int = 64_000, start: int = 0) -> str:
    """The last max_bytes of the log, never reading before byte `start`."""
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(start, f.tell() - max_bytes))
        return f.read().decode("utf-8", "replace")


class JobRunner:
    def __init__(self, settings: WebSettings, db: Database,
                 command: Optional[Callable[[Job, bool], list[str]]] = None):
        self.s, self.db = settings, db
        self.command = command or self.default_command
        self.procs: dict[str, subprocess.Popen] = {}
        self.cancelled: set[str] = set()
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- paths ------------------------------------------------------------------------------
    def run_dir(self, job: Job) -> Path:
        return self.s.runs_dir / job.run_name

    def log_path(self, job: Job) -> Path:
        return self.run_dir(job) / "job.log"

    def upload_dir(self, job: Job) -> Path:
        return self.s.uploads_dir / job.id

    def default_command(self, job: Job, resume: bool) -> list[str]:
        base = [self.s.python, "-m", "litscout"]
        if resume:  # --recrawl: also continue a finished crawl, e.g. after the criteria were edited
            return base + ["resume", job.run_name, "--recrawl"]
        cmd = base + ["run", "--run", job.run_name]
        if job.mode == "topic":
            cmd += ["--topic", job.topic or "", "--seeds", str(job.n_seeds)]
        else:
            cmd += ["--seeds-dir", str(self.upload_dir(job))]
        if job.review_criteria:
            cmd.append("--review-criteria")
        return cmd

    # -- lifecycle ----------------------------------------------------------------------------
    def start_background(self, interval: float = 2.0) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:  # keep the loop alive; the next tick retries
                    log.exception("job runner tick failed")
        self._thread = threading.Thread(target=loop, name="job-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        with self.lock, self.db.sessions() as db:
            for job in db.scalars(select(Job).where(Job.status.in_(ACTIVE))):
                self._check(job)
            n_running = len(db.scalars(select(Job.id).where(Job.status == "running")).all())
            for job in db.scalars(select(Job).where(Job.status == "queued").order_by(Job.created_at)):
                if n_running >= self.s.max_concurrent_jobs:
                    break
                self._start(job)
                n_running += 1
            db.commit()

    def _start(self, job: Job) -> None:
        run_dir = self.run_dir(job)
        resume = (run_dir / "store.sqlite").exists()
        run_dir.mkdir(parents=True, exist_ok=True)
        job.log_offset = self.log_path(job).stat().st_size if self.log_path(job).exists() else 0
        env = {**os.environ, "MAX_COST_USD": str(job.max_cost_usd), "MAX_PAPERS": str(self.s.max_papers),
               "PYTHONUNBUFFERED": "1"}
        with self.log_path(job).open("ab") as out:
            p = subprocess.Popen(self.command(job, resume), cwd=self.s.repo_root, stdout=out,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        self.procs[job.id] = p
        job.status, job.pid, job.error = "running", p.pid, None
        job.started_at = job.started_at or _now()
        job.finished_at = None
        log.info("job %s started (pid %d, %s)", job.id, p.pid, "resume" if resume else "new run")

    def _check(self, job: Job) -> None:
        p = self.procs.get(job.id)
        rc = p.poll() if p else None
        alive = (rc is None) if p else _alive(job.pid)
        if job.mode == "pdf" and not job.uploads_deleted and (not alive or self._seeds_imported(job)):
            self._delete_uploads(job)
        if alive:
            return
        self.procs.pop(job.id, None)
        tail = log_tail(self.log_path(job), start=job.log_offset or 0)
        if job.id in self.cancelled:
            job.status = "cancelled"
            self.cancelled.discard(job.id)
        elif DONE_MARK in tail and (rc in (0, None)):
            job.status = "completed"
        elif PAUSE_MARK in tail and rc in (0, None) and DONE_MARK not in tail:
            job.status = "awaiting_criteria"
        else:
            job.status = "failed"
            job.error = _error_summary(tail, rc)
        job.pid = None
        job.finished_at = _now()
        log.info("job %s %s", job.id, job.status)

    def _seeds_imported(self, job: Job) -> bool:
        """The seed step is over once the criteria (written right after it) exist."""
        return (self.run_dir(job) / "criteria.md").exists()

    def _delete_uploads(self, job: Job) -> None:
        shutil.rmtree(self.upload_dir(job), ignore_errors=True)
        job.uploads_deleted = True
        log.info("job %s: uploaded PDFs deleted (seeds imported)", job.id)

    # -- actions ------------------------------------------------------------------------------
    def cancel(self, job: Job) -> None:
        if job.status == "running" and job.pid:
            self.cancelled.add(job.id)
            try:
                os.killpg(job.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif job.status == "queued":
            job.status = "cancelled"
            job.finished_at = _now()

    def delete_files(self, job: Job) -> None:
        shutil.rmtree(self.run_dir(job), ignore_errors=True)
        shutil.rmtree(self.upload_dir(job), ignore_errors=True)


def _error_summary(tail: str, rc: Optional[int]) -> str:
    lines = [ln for ln in tail.strip().splitlines() if ln.strip()]
    if "Traceback" in tail:
        last = lines[-1] if lines else ""
        return f"{last[:400]} (exit code {rc})" if rc is not None else last[:400]
    msg = next((ln for ln in reversed(lines) if "SystemExit" in ln or "error" in ln.lower()), lines[-1] if lines else "")
    return (msg[:400] or "the process stopped") + (f" (exit code {rc})" if rc not in (None, 0) else "")


def store_ready(run_dir: Path) -> bool:
    path = run_dir / "store.sqlite"
    if not path.exists():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
            return c.execute("SELECT 1 FROM sqlite_master WHERE name='papers'").fetchone() is not None
    except sqlite3.Error:
        return False
