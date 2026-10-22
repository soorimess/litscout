"""Live progress and results of one review: stage, metrics, log, papers, one paper, seeds, exports."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..deps import owned_job
from ..models import Job
from ..results import SORTS, VIEWS, RunFiles
from ..schemas import PaperPage

router = APIRouter(prefix="/api/jobs/{job_id}", tags=["results"])


def _files(request: Request, job: Job) -> RunFiles:
    return RunFiles(request.app.state.runner.run_dir(job))


@router.get("/progress")
def progress(request: Request, job: Job = Depends(owned_job)) -> dict[str, Any]:
    return _files(request, job).progress()


@router.get("/papers", response_model=PaperPage)
def papers(request: Request, job: Job = Depends(owned_job),
           view: str = Query("final", pattern="^(" + "|".join(VIEWS) + ")$"),
           q: str = Query("", max_length=200),
           segment: Optional[str] = Query(None, pattern="^(core|candidate|boundary|discard|seed)$"),
           min_llm: Optional[int] = Query(None, ge=0, le=10),
           sort: str = Query("rank", pattern="^(" + "|".join(SORTS) + ")$"),
           page: int = Query(1, ge=1, le=10000),
           page_size: int = Query(25, ge=1, le=100)) -> PaperPage:
    return PaperPage(**_files(request, job).papers(view, q.strip(), segment, min_llm, sort, page, page_size))


@router.get("/paper")
def paper(request: Request, pid: str = Query(..., min_length=1, max_length=300),
          job: Job = Depends(owned_job)) -> dict[str, Any]:
    p = _files(request, job).paper(pid)
    if not p:
        raise HTTPException(404, "Paper not found")
    return p


@router.get("/seeds")
def seeds(request: Request, job: Job = Depends(owned_job)) -> list[dict[str, Any]]:
    return _files(request, job).seeds()


@router.get("/export/{fmt}")
def export(fmt: str, request: Request, job: Job = Depends(owned_job)) -> FileResponse:
    if fmt not in ("xlsx", "csv"):
        raise HTTPException(404, "Unknown format")
    path = _files(request, job).export_path(fmt)
    if not path:
        raise HTTPException(404, "The tables are written when the review finishes")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in job.title.lower())[:60].strip("-") or "review"
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "xlsx" else "text/csv"
    return FileResponse(path, media_type=media, filename=f"{safe}.{fmt}")
