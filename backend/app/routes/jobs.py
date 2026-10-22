"""Review jobs: create (topic or PDFs), list, inspect, cancel, resume, review criteria, delete."""
from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import current_user, get_db, owned_job
from ..models import Job, User
from ..results import RunFiles
from ..runner import JobRunner, log_tail
from ..schemas import CriteriaIn, CriteriaOut, JobOut, JobSummary, Limits

router = APIRouter(prefix="/api", tags=["jobs"])
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
CHUNK = 1 << 20


def _runner(request: Request) -> JobRunner:
    return request.app.state.runner


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops the time zone; every stored time is UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def job_out(job: Job, runner: JobRunner, with_summary: bool = True) -> JobOut:
    files = RunFiles(runner.run_dir(job))
    stage = None
    summary = JobSummary()
    if with_summary and files.dir.exists():
        stage = files.stage(log_tail(runner.log_path(job)))["key"]
        summary = JobSummary(**files.summary())
    return JobOut(id=job.id, title=job.title, mode=job.mode, topic=job.topic, n_seeds=job.n_seeds,
                  seed_files=job.seed_files or [], review_criteria=job.review_criteria, max_cost_usd=job.max_cost_usd,
                  status=job.status, error=job.error, created_at=_utc(job.created_at), started_at=_utc(job.started_at),
                  finished_at=_utc(job.finished_at), stage=stage, summary=summary)


@router.get("/limits", response_model=Limits)
def limits(request: Request, _: User = Depends(current_user)) -> Limits:
    s = request.app.state.settings
    return Limits(max_cost_usd=s.max_cost_usd, max_pdfs=s.max_pdfs, max_pdf_mb=s.max_pdf_mb)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[JobOut]:
    jobs = db.scalars(select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc())).all()
    return [job_out(j, _runner(request)) for j in jobs]


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    request: Request,
    mode: str = Form(...),
    topic: Optional[str] = Form(None),
    n_seeds: int = Form(5),
    title: Optional[str] = Form(None),
    review_criteria: bool = Form(False),
    max_cost_usd: Optional[float] = Form(None),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> JobOut:
    s = request.app.state.settings
    runner = _runner(request)
    if mode not in ("topic", "pdf"):
        raise HTTPException(422, "mode must be 'topic' or 'pdf'")
    cost = s.max_cost_usd if max_cost_usd is None else max_cost_usd
    if not 0.1 <= cost <= s.max_cost_usd:
        raise HTTPException(422, f"Cost limit must be between $0.10 and ${s.max_cost_usd:.2f}")
    topic = " ".join((topic or "").split())
    if mode == "topic":
        if not 3 <= len(topic) <= 300:
            raise HTTPException(422, "Topic must be 3 to 300 characters")
        if not 1 <= n_seeds <= 20:
            raise HTTPException(422, "Number of seed papers must be between 1 and 20")
        if files:
            raise HTTPException(422, "Topic mode takes no files")
    else:
        files = [f for f in files if f.filename]
        if not files:
            raise HTTPException(422, "Upload at least one PDF")
        if len(files) > s.max_pdfs:
            raise HTTPException(422, f"At most {s.max_pdfs} PDFs per review")
    job_id = str(uuid.uuid4())
    job = Job(id=job_id, user_id=user.id, mode=mode, topic=topic or None,
              n_seeds=n_seeds if mode == "topic" else len(files), review_criteria=review_criteria,
              max_cost_usd=round(cost, 2), status="queued", run_name=f"web-{job_id[:8]}",
              title=" ".join((title or "").split())[:200] or (topic[:120] if mode == "topic" else f"{len(files)} seed PDFs"))
    db.add(job)
    if mode == "pdf":
        job.seed_files = await _save_pdfs(files, runner.upload_dir(job), s.max_pdf_mb)
    db.commit()
    return job_out(job, runner, with_summary=False)


async def _save_pdfs(files: list[UploadFile], folder, max_mb: int) -> list[str]:  # noqa: ANN001
    folder.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    try:
        for i, f in enumerate(files):
            name = SAFE_NAME.sub("_", (f.filename or "paper.pdf").rsplit("/", 1)[-1])[:120] or "paper.pdf"
            if not name.lower().endswith(".pdf"):
                raise HTTPException(422, f"{name}: only PDF files are accepted")
            path = folder / f"{i:02d}-{name}"
            size, head = 0, b""
            with path.open("wb") as out:
                while chunk := await f.read(CHUNK):
                    if not head:
                        head = chunk[:5]
                    size += len(chunk)
                    if size > max_mb * CHUNK:
                        raise HTTPException(413, f"{name} is larger than {max_mb} MB")
                    out.write(chunk)
            if head != b"%PDF-":
                raise HTTPException(422, f"{name} is not a PDF file")
            names.append(name)
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return names


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(request: Request, job: Job = Depends(owned_job)) -> JobOut:
    return job_out(job, _runner(request))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(request: Request, job: Job = Depends(owned_job), db: Session = Depends(get_db)) -> JobOut:
    runner = _runner(request)
    if job.status not in ("queued", "running"):
        raise HTTPException(409, "Only a queued or running review can be stopped")
    with runner.lock:
        runner.cancel(job)
        db.commit()
    return job_out(job, runner)


@router.post("/jobs/{job_id}/resume", response_model=JobOut)
def resume_job(request: Request, job: Job = Depends(owned_job), db: Session = Depends(get_db)) -> JobOut:
    """Continue a stopped, failed or paused review where it left off, or re-run a finished one after its
    criteria were edited (papers are judged again under the new criteria)."""
    if job.status not in ("failed", "cancelled", "awaiting_criteria", "completed"):
        raise HTTPException(409, "Only a stopped, failed, paused or finished review can be continued")
    if job.mode == "pdf" and job.uploads_deleted and not (_runner(request).run_dir(job) / "store.sqlite").exists():
        raise HTTPException(409, "The uploaded PDFs were already removed; start a new review")
    job.status, job.error = "queued", None
    db.commit()
    return job_out(job, _runner(request))


@router.get("/jobs/{job_id}/criteria", response_model=CriteriaOut)
def get_criteria(request: Request, job: Job = Depends(owned_job)) -> CriteriaOut:
    files = RunFiles(_runner(request).run_dir(job))
    return CriteriaOut(text=files.criteria(), editable=job.status not in ("queued", "running"))


@router.put("/jobs/{job_id}/criteria", response_model=CriteriaOut)
def put_criteria(body: CriteriaIn, request: Request, job: Job = Depends(owned_job)) -> CriteriaOut:
    """Edit the criteria of a paused or finished review. Continuing it re-judges papers under the new text."""
    if job.status in ("queued", "running"):
        raise HTTPException(409, "Criteria can be edited only while the review is paused or finished")
    files = RunFiles(_runner(request).run_dir(job))
    if not files.criteria_path.parent.exists():
        raise HTTPException(409, "This review has no criteria yet")
    files.write_criteria(body.text)
    return CriteriaOut(text=files.criteria(), editable=True)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(request: Request, job: Job = Depends(owned_job), db: Session = Depends(get_db)) -> Response:
    if job.status == "running":
        raise HTTPException(409, "Stop the review before deleting it")
    _runner(request).delete_files(job)
    db.delete(job)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
