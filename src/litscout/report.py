"""Exports: papers table (CSV, XLSX with final list, all papers, seeds, validation) and a short report."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .store import Store

COLUMNS = ["Rank", "In final list", "Segment", "Title", "Year", "URL", "Abstract", "Score", "Similarity to seeds",
           "Similarity to topic", "Nearest seed", "P(relevant)", "Final score", "LLM score", "LLM reason", "Found via",
           "Venue", "Citations"]


def _url(p: dict[str, Any]) -> str:
    if p.get("arxiv"):
        return f"https://arxiv.org/abs/{p['arxiv']}"
    if p.get("doi"):
        return f"https://doi.org/{p['doi']}"
    if p.get("openalex"):
        return f"https://openalex.org/{p['openalex']}"
    return p.get("url") or ""


def _rows(store: Store) -> list[dict[str, Any]]:
    has_final = bool(store.q("SELECT name FROM sqlite_master WHERE name='final'"))
    final_join = "LEFT JOIN final f ON f.paper_id = p.id" if has_final else ""
    final_cols = "f.score AS fscore, f.include AS finc" if has_final else "NULL AS fscore, NULL AS finc"
    rows = [dict(r) for r in store.q(
        f"SELECT p.*, s.name AS seed_name, j.score AS jscore, j.reason AS jreason, {final_cols} "
        f"FROM papers p LEFT JOIN seeds s ON s.id = p.best_seed LEFT JOIN judgments j ON j.paper_id = p.id {final_join} "
        "WHERE p.best IS NOT NULL")]
    via = {r["paper_id"]: r for r in store.q(
        "SELECT d.paper_id, s.name, d.direction, d.back, d.fwd FROM discoveries d JOIN seeds s ON s.id = d.seed_id WHERE d.first=1")}
    out = []
    for p in rows:
        v = via.get(p["id"])
        out.append({
            "In final list": "seed" if p["segment"] == "seed" else ("yes" if p["finc"] else ("no" if p["finc"] is not None else "")),
            "Segment": p["segment"], "Title": p["title"], "Year": p["year"], "URL": _url(p), "Abstract": p["abstract"] or "",
            "Score": round(p["best"], 4),
            "Similarity to seeds": round(p["centroid"], 4) if p["centroid"] is not None else "",
            "Similarity to topic": round(p["topic"], 4) if p.get("topic") is not None else "",
            "Nearest seed": p["seed_name"],
            "P(relevant)": round(p["posterior"], 4) if p["posterior"] is not None else "",
            "Final score": round(p["fscore"], 4) if p["fscore"] is not None else "",
            "LLM score": p["jscore"] if p["jscore"] is not None else "", "LLM reason": p["jreason"] or "",
            "Found via": ("keyword search" if v["direction"] == "search" else f"{v['name']}: {v['back']} back, {v['fwd']} forward")
                         if v else "seed",
            "Venue": p["venue"] or "", "Citations": p["cited_by"] if p["cited_by"] is not None else "",
            "_sort": (p["segment"] != "seed", -(p["fscore"] if p["fscore"] is not None else p["best"])),
        })
    out.sort(key=lambda r: r["_sort"])
    for i, r in enumerate(out, 1):
        r["Rank"] = i
    return out


def export(store: Store, run_dir: Path) -> Path:
    out_dir = run_dir / "output"
    out_dir.mkdir(exist_ok=True)
    rows = _rows(store)
    with (out_dir / "papers.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    wb.remove(wb.active)
    widths = {"Title": 50, "Abstract": 80, "LLM reason": 50, "URL": 34, "Found via": 30, "Nearest seed": 28, "Venue": 25}
    for name, subset in (("Final list", [r for r in rows if r["In final list"] in ("yes", "seed")]),
                         ("All scored papers", rows)):
        ws = wb.create_sheet(name)
        ws.append(COLUMNS)
        for r in subset:
            ws.append([r.get(c, "") for c in COLUMNS])
        for i, c in enumerate(COLUMNS, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = widths.get(c, 13)
            ws.cell(1, i).font = Font(bold=True)
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    ws = wb.create_sheet("Seeds")
    ws.append(["Name", "Title", "Origin", "Reason", "Expandable", "Given up"])
    # promoted seeds are listed here and in the final list
    given_up = store.meta("given_up", {})
    for r in store.q("SELECT s.*, p.title FROM seeds s JOIN papers p ON p.id = s.id"):
        ws.append([r["name"], r["title"], r["origin"], r["reason"], bool(r["expandable"]), given_up.get(r["id"], "")])
    ws = wb.create_sheet("Run summary")
    for k in ("run", "topic", "criteria", "null", "thresholds", "validation", "cost_usd", "state", "queries", "crawl_queries",
              "repo_queries"):
        ws.append([k, json.dumps(store.meta(k), ensure_ascii=False)])
    wb.save(out_dir / "papers.xlsx")
    summary = {k: store.meta(k) for k in ("run", "topic", "criteria", "null", "thresholds", "validation", "cost_usd", "state",
                                          "queries", "crawl_queries", "repo_queries")}
    summary["counts"] = {r["segment"] or "unscored": r["n"] for r in store.q("SELECT segment, COUNT(*) AS n FROM papers GROUP BY segment")}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return out_dir
