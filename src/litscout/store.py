"""Per-run SQLite store: papers, seeds, discoveries, edges, embeddings, scores, frontier, judgments, events."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from .sources.records import canonical_id, norm_title

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY, openalex TEXT, doi TEXT, arxiv TEXT, title TEXT, title_norm TEXT, abstract TEXT,
  year INTEGER, venue TEXT, cited_by INTEGER, subfield_id TEXT, subfield TEXT, oa_pdf TEXT, url TEXT,
  best REAL, best_seed TEXT, centroid REAL, topic REAL, posterior REAL, segment TEXT,
  expanded INTEGER DEFAULT 0, created REAL);
CREATE INDEX IF NOT EXISTS ix_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS ix_papers_arxiv ON papers(arxiv);
CREATE INDEX IF NOT EXISTS ix_papers_title ON papers(title_norm);
CREATE INDEX IF NOT EXISTS ix_papers_best ON papers(best);
CREATE TABLE IF NOT EXISTS seeds (id TEXT PRIMARY KEY, name TEXT, origin TEXT, reason TEXT, expandable INTEGER);
CREATE TABLE IF NOT EXISTS discoveries (
  paper_id TEXT, seed_id TEXT, direction TEXT, back INTEGER, fwd INTEGER, parent_id TEXT, first INTEGER, at REAL);
CREATE INDEX IF NOT EXISTS ix_disc_seed ON discoveries(seed_id, at);
CREATE TABLE IF NOT EXISTS edges (src TEXT, dst TEXT, kind TEXT, PRIMARY KEY (src, dst, kind));
CREATE TABLE IF NOT EXISTS embeddings (paper_id TEXT PRIMARY KEY, model TEXT, dim INTEGER, vec BLOB);
CREATE TABLE IF NOT EXISTS scores (paper_id TEXT, seed_id TEXT, cosine REAL, PRIMARY KEY (paper_id, seed_id));
CREATE TABLE IF NOT EXISTS frontier (
  seed_id TEXT, paper_id TEXT, priority REAL, back INTEGER, fwd INTEGER, state TEXT, retries INTEGER DEFAULT 0,
  PRIMARY KEY (seed_id, paper_id));
CREATE INDEX IF NOT EXISTS ix_frontier ON frontier(seed_id, state, priority);
CREATE TABLE IF NOT EXISTS null_scores (paper_id TEXT PRIMARY KEY, title TEXT, best REAL);
CREATE TABLE IF NOT EXISTS null_embeddings (paper_id TEXT PRIMARY KEY, vec BLOB);
CREATE TABLE IF NOT EXISTS judgments (
  paper_id TEXT PRIMARY KEY, stratum TEXT, score INTEGER, relevant INTEGER, reason TEXT, model TEXT);
CREATE TABLE IF NOT EXISTS judgments_archive (
  paper_id TEXT, stratum TEXT, score INTEGER, relevant INTEGER, reason TEXT, model TEXT, criteria_hash TEXT, archived REAL);
CREATE TABLE IF NOT EXISTS events (at REAL, kind TEXT, rule TEXT, payload TEXT);
"""
PAPER_COLS = ("id", "openalex", "doi", "arxiv", "title", "abstract", "year", "venue", "cited_by",
              "subfield_id", "subfield", "oa_pdf", "url")


class Store:
    def __init__(self, path: Path, dtype: str = "float16"):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(papers)")}
        for col, kind in (("topic", "REAL"), ("url", "TEXT")):  # columns added after the first stores
            if col not in cols:
                self.db.execute(f"ALTER TABLE papers ADD COLUMN {col} {kind}")
        self.lock = threading.RLock()
        self.dtype = np.dtype(dtype)

    # -- generic ------------------------------------------------------------------------
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, tuple(args)).fetchall()

    def x(self, sql: str, args: Iterable[Any] = ()) -> None:
        with self.lock:
            self.db.execute(sql, tuple(args))

    def many(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany(sql, [tuple(r) for r in rows])
            self.db.execute("COMMIT")

    def meta(self, key: str, default: Any = None) -> Any:
        r = self.q("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set_meta(self, key: str, value: Any) -> None:
        self.x("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    def event(self, kind: str, rule: str, payload: dict[str, Any]) -> None:
        self.x("INSERT INTO events VALUES (?, ?, ?, ?)", (time.time(), kind, rule, json.dumps(payload)))

    # -- papers -------------------------------------------------------------------------
    def find(self, rec: dict[str, Any]) -> Optional[str]:
        for col in ("openalex", "doi", "arxiv"):
            if rec.get(col):
                r = self.q(f"SELECT id FROM papers WHERE {col}=? LIMIT 1", (rec[col],))
                if r:
                    return r[0]["id"]
        t = norm_title(rec.get("title"))
        if len(t) > 20:
            r = self.q("SELECT id FROM papers WHERE title_norm=? LIMIT 1", (t,))
            if r:
                return r[0]["id"]
        return None

    def upsert_paper(self, rec: dict[str, Any]) -> tuple[str, bool]:
        """Insert a paper or fill empty fields of the existing one. Returns (id, is_new)."""
        with self.lock:
            pid = self.find(rec)
            if pid is None:
                pid = rec.get("id") or canonical_id(rec)
                vals = {c: rec.get(c) for c in PAPER_COLS}
                vals["id"] = pid
                self.x(f"INSERT INTO papers ({','.join(PAPER_COLS)}, title_norm, created) VALUES "
                       f"({','.join('?' * len(PAPER_COLS))}, ?, ?)",
                       [vals[c] for c in PAPER_COLS] + [norm_title(rec.get("title")), time.time()])
                return pid, True
            cur = dict(self.q("SELECT * FROM papers WHERE id=?", (pid,))[0])
            fill = {c: rec[c] for c in PAPER_COLS[1:] if rec.get(c) not in (None, "") and cur.get(c) in (None, "")}
            if fill:
                self.x(f"UPDATE papers SET {', '.join(f'{c}=?' for c in fill)} WHERE id=?", list(fill.values()) + [pid])
            return pid, False

    def paper(self, pid: str) -> Optional[dict[str, Any]]:
        r = self.q("SELECT * FROM papers WHERE id=?", (pid,))
        return dict(r[0]) if r else None

    # -- embeddings and scores -------------------------------------------------------------
    def put_embeddings(self, ids: list[str], model: str, mat: np.ndarray) -> None:
        self.many("INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?)",
                  [(i, model, mat.shape[1], v.astype(self.dtype).tobytes()) for i, v in zip(ids, mat)])

    def embeddings(self, ids: list[str]) -> np.ndarray:
        rows = {r["paper_id"]: r for r in self.q(
            f"SELECT paper_id, vec FROM embeddings WHERE paper_id IN ({','.join('?' * len(ids))})", ids)}
        return np.stack([np.frombuffer(rows[i]["vec"], dtype=self.dtype).astype(np.float32) for i in ids])

    def has_embedding(self, pid: str) -> bool:
        return bool(self.q("SELECT 1 FROM embeddings WHERE paper_id=?", (pid,)))

    def put_null_embeddings(self, ids: list[str], mat: np.ndarray) -> None:
        self.many("INSERT OR REPLACE INTO null_embeddings VALUES (?, ?)",
                  [(i, v.astype(self.dtype).tobytes()) for i, v in zip(ids, mat)])

    def null_embeddings(self) -> tuple[list[str], np.ndarray]:
        rows = self.q("SELECT paper_id, vec FROM null_embeddings ORDER BY paper_id")
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32)
        return [r["paper_id"] for r in rows], np.stack(
            [np.frombuffer(r["vec"], dtype=self.dtype).astype(np.float32) for r in rows])

    def archive_judgments(self, criteria_hash: str) -> int:
        """Move judgments made under other review criteria out of the active table."""
        with self.lock:
            n = self.q("SELECT COUNT(*) AS n FROM judgments")[0]["n"]
            if n:
                self.x("INSERT INTO judgments_archive SELECT *, ?, ? FROM judgments", (criteria_hash, time.time()))
                self.x("DELETE FROM judgments")
            return n
