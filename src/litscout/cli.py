"""Command line.

  litscout run --topic "..." --seeds 5        find seeds for a topic, crawl, validate, export
  litscout run --seeds-dir ./seeds             same, with seed PDFs given
      [--criteria FILE]                            use these review criteria instead of an LLM draft
      [--review-criteria]                          stop after drafting criteria.md, to edit it, then resume
  litscout criteria RUN                        print the run's review criteria and where to edit them
  litscout resume RUN [--recrawl]              continue an interrupted run (--recrawl: also a finished crawl)
  litscout validate RUN | report RUN           re-run one stage
  litscout dashboard RUN [--host H] [--port P] [--password PW]   serve the live dashboard for a run
  litscout status RUN                          print thresholds, counts and decisions
"""
from __future__ import annotations

import argparse
import base64
import functools
import os
import http.server
import json
import logging
import re
import shutil
import time
from pathlib import Path

from . import criteria as crit
from .config import Settings, load_settings
from .crawl import Crawler
from .llm import LLM, Budget
from .report import export
from .seeds import find_from_topic, import_pdf_folder, topic_from_seeds
from .sources import Sources
from .store import Store
from .validate import validate

log = logging.getLogger("litscout")


def _open(s: Settings, run: str) -> tuple[Path, Store]:
    run_dir = s.runs_dir / run
    if not (run_dir / "store.sqlite").exists() and not run_dir.exists():
        run_dir.mkdir(parents=True)
    return run_dir, Store(run_dir / "store.sqlite", s.t("embedding", "store_dtype", default="float16"))


class _DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Serves only the dashboard page and its metrics file, never the store or the outputs."""

    ALLOWED = {"/": "/index.html", "/index.html": "/index.html", "/metrics.json": "/metrics.json"}
    password = ""

    def do_GET(self) -> None:
        if self.password:
            expected = "Basic " + base64.b64encode(f"review:{self.password}".encode()).decode()
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="LitScout"')
                self.end_headers()
                return
        path = self.ALLOWED.get(self.path.split("?", 1)[0])
        if path is None:
            self.send_error(404)
            return
        self.path = path
        super().do_GET()

    def do_HEAD(self) -> None:
        self.do_GET()

    def list_directory(self, path):  # noqa: ANN001
        self.send_error(404)
        return None

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass


def _install_dashboard(run_dir: Path) -> None:
    shutil.copy(Path(__file__).parent / "dashboard" / "index.html", run_dir / "index.html")


def _pipeline(s: Settings, run_dir: Path, store: Store, llm: LLM = None, src: Sources = None) -> None:  # type: ignore[assignment]
    budget = llm.budget if llm else Budget(s.max_cost_usd, s.t("pricing", default={}) or {}, spent=float(store.meta("cost_usd", 0.0)))
    llm, src = llm or LLM(s, budget), src or Sources(s)
    run = store.meta("run")
    if not store.q("SELECT 1 FROM seeds LIMIT 1"):
        if run["mode"] == "topic":
            find_from_topic(run["topic"], run["n_seeds"], s.seed_pool_max, store, src, llm, s.t("seed_finder", default={}) or {})
        else:
            import_pdf_folder(Path(run["seeds_dir"]), store, src)
        store.set_meta("cost_usd", budget.spent)
    if not store.meta("topic"):
        store.set_meta("topic", run.get("topic") or topic_from_seeds(store, llm))
        run["topic"] = store.meta("topic")
        store.set_meta("run", run)
    path = run_dir / "criteria.md"
    if not path.exists() and not store.meta("criteria"):
        given = run.get("criteria_file")
        path.write_text(Path(given).read_text() if given else crit.draft(store.meta("topic"), store, llm))
        log.info("review criteria written to %s", path)
        if run.get("review_criteria") and not given:
            store.set_meta("cost_usd", budget.spent)
            print(f"\nReview criteria drafted: {path}\nEdit them if needed, then continue with: litscout resume {run['name']}")
            return
    crit.load(store, run_dir)
    _install_dashboard(run_dir)
    passes = int(s.t("crawl", "max_passes", default=3))
    for n in range(passes):
        crawler = Crawler(s, store, src, llm, budget, run_dir)
        if store.meta("state") != "crawled":
            crawler.run()
        validate(s, store, llm, run_dir)
        left = crawler.unexpanded_relevant()
        if not left or n == passes - 1 or budget.spent >= budget.limit:
            break
        log.info("pass %d: %d LLM-confirmed papers not expanded yet, crawling again", n + 1, left)
        store.set_meta("state", "crawling")
    store.set_meta("cost_usd", budget.spent)
    _finish_metrics(run_dir, store, budget)
    out = export(store, run_dir)
    log.info("done: %s (cost $%.3f)", out, budget.spent)


def _finish_metrics(run_dir: Path, store: Store, budget: Budget) -> None:
    """After validation: the dashboard shows the total cost and the final check."""
    path = run_dir / "metrics.json"
    if not path.exists():
        return
    m = json.loads(path.read_text())
    m.update({"state": "done", "updated": time.time(), "cost_usd": round(budget.spent, 4), "tokens": budget.tokens,
              "validation": store.meta("validation")})
    path.write_text(json.dumps(m, default=float))


def main() -> None:
    ap = argparse.ArgumentParser(prog="litscout", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="tuning YAML (default: config/default.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--topic")
    g.add_argument("--seeds-dir")
    r.add_argument("--seeds", type=int, default=5, help="topic mode: number of seeds to pick")
    r.add_argument("--run", help="run name (default: date + topic)")
    r.add_argument("--criteria", help="review criteria file (Markdown) to use instead of an LLM draft")
    r.add_argument("--review-criteria", action="store_true", help="stop after drafting the criteria so you can edit them")
    for name in ("resume", "validate", "report", "status", "criteria"):
        sub.add_parser(name).add_argument("run")
    sub.choices["resume"].add_argument("--recrawl", action="store_true",
                                       help="continue crawling even if the crawl had stopped (e.g. after raising a cap)")
    d = sub.add_parser("dashboard")
    d.add_argument("run")
    d.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it from other machines")
    d.add_argument("--port", type=int, default=8765)
    d.add_argument("--password", default=os.environ.get("DASHBOARD_PASSWORD", ""),
                   help="ask for this password (user name: review); default: $DASHBOARD_PASSWORD")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    s = load_settings(a.config)

    if a.cmd == "run":
        label = a.topic or Path(a.seeds_dir).name
        name = a.run or time.strftime("%Y%m%d-%H%M-") + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
        run_dir, store = _open(s, name)
        if not store.meta("run"):
            store.set_meta("run", {"name": name, "mode": "topic" if a.topic else "pdf", "topic": a.topic,
                                   "n_seeds": a.seeds, "seeds_dir": str(Path(a.seeds_dir).resolve()) if a.seeds_dir else None,
                                   "criteria_file": str(Path(a.criteria).resolve()) if a.criteria else None,
                                   "review_criteria": a.review_criteria, "created": time.time()})
        log.info("run %s in %s", name, run_dir)
        _pipeline(s, run_dir, store)
    elif a.cmd == "resume":
        run_dir, store = _open(s, a.run)
        if a.recrawl:
            store.set_meta("state", "crawling")
        _pipeline(s, run_dir, store)
    elif a.cmd == "validate":
        run_dir, store = _open(s, a.run)
        budget = Budget(s.max_cost_usd, s.t("pricing", default={}) or {}, spent=float(store.meta("cost_usd", 0.0)))
        print(json.dumps(validate(s, store, LLM(s, budget), run_dir), indent=2))
        store.set_meta("cost_usd", budget.spent)
    elif a.cmd == "report":
        run_dir, store = _open(s, a.run)
        print(export(store, run_dir))
    elif a.cmd == "status":
        run_dir, store = _open(s, a.run)
        counts = {r["segment"] or "unscored": r["n"] for r in store.q("SELECT segment, COUNT(*) AS n FROM papers GROUP BY segment")}
        print(json.dumps({"run": store.meta("run"), "state": store.meta("state"), "thresholds": store.meta("thresholds"),
                          "counts": counts, "given_up": store.meta("given_up"), "cost_usd": store.meta("cost_usd"),
                          "judged": dict(store.q("SELECT COUNT(*) AS total, COALESCE(SUM(relevant), 0) AS relevant FROM judgments")[0]),
                          "promoted_seeds": [r["name"] for r in store.q("SELECT name FROM seeds WHERE origin LIKE 'promoted%'")],
                          "search_rounds": store.meta("search_rounds", 0),
                          "validation": store.meta("validation")}, indent=2, default=str))
    elif a.cmd == "criteria":
        run_dir, store = _open(s, a.run)
        path = run_dir / "criteria.md"
        print(path.read_text() if path.exists() else (store.meta("criteria") or "No criteria yet."))
        print(f"\nEdit {path}, then: litscout resume {a.run}  (judgments under the old criteria are archived)")
    elif a.cmd == "dashboard":
        run_dir = s.runs_dir / a.run
        _install_dashboard(run_dir)
        _DashboardHandler.password = a.password
        handler = functools.partial(_DashboardHandler, directory=str(run_dir))
        print(f"Dashboard: http://{a.host}:{a.port}/  (Ctrl+C to stop)")
        http.server.ThreadingHTTPServer((a.host, a.port), handler).serve_forever()


if __name__ == "__main__":
    main()
