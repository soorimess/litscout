"""Request and response models of the HTTP API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    username: str


class JobSummary(BaseModel):
    papers: Optional[int] = None
    final: Optional[int] = None
    judged: Optional[int] = None
    seeds: Optional[int] = None
    cost_usd: Optional[float] = None


class JobOut(BaseModel):
    id: str
    title: str
    mode: str
    topic: Optional[str]
    n_seeds: int
    seed_files: list[str]
    review_criteria: bool
    max_cost_usd: float
    status: str
    error: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    stage: Optional[str] = None
    summary: JobSummary = JobSummary()


class CriteriaIn(BaseModel):
    text: str = Field(min_length=20, max_length=20000)


class CriteriaOut(BaseModel):
    text: Optional[str]
    editable: bool


class Limits(BaseModel):
    max_cost_usd: float
    max_pdfs: int
    max_pdf_mb: int
    max_seeds: int = 20


class PaperPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[dict[str, Any]]
