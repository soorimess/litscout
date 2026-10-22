"""HTTP API tests with a fake pipeline command (no network, no LLM)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import WebSettings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User  # noqa: E402
from app.runner import JobRunner  # noqa: E402
from app.security import hash_password  # noqa: E402
from litscout.store import Store  # noqa: E402

H = {"X-Requested-With": "LitScout"}
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def fake_command(outcome: str):
    def cmd(job, resume):  # noqa: ANN001
        lines = {"done": "INFO litscout: done: output (cost $0.01)", "fail": "Traceback\\nRuntimeError: boom",
                 "pause": "Review criteria drafted: criteria.md"}[outcome]
        code = 1 if outcome == "fail" else 0
        return [sys.executable, "-c", f"import sys, pathlib; pathlib.Path('runs/{job.run_name}/criteria.md').write_text('# c'); "
                f"print('{lines}'); sys.exit({code})"]
    return cmd


@pytest.fixture()
def env(tmp_path, monkeypatch):
    s = WebSettings(repo_root=tmp_path, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs",
                    frontend_dist=tmp_path / "dist", max_cost_usd=5.0)
    for d in (s.data_dir, s.uploads_dir, s.runs_dir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    db = Database(s.db_url)
    db.create_all()
    runner = JobRunner(s, db, command=fake_command("done"))
    app = create_app(s, runner=runner, background=False)
    with db.sessions() as d:
        d.add_all([User(username="alice", password_hash=hash_password("alice-password-1")),
                   User(username="bob", password_hash=hash_password("bob-password-12"))])
        d.commit()
    return app, runner, s


def login(client: TestClient, user: str = "alice", pw: str = "alice-password-1"):
    return client.post("/api/auth/login", json={"username": user, "password": pw}, headers=H)


def wait_finished(runner: JobRunner, client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        runner.tick()
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_auth_flow_and_csrf(env):
    app, _, _ = env
    c = TestClient(app)
    assert c.get("/api/auth/me").status_code == 401
    assert c.post("/api/auth/login", json={"username": "alice", "password": "x"}).status_code == 403  # no header
    assert login(c, pw="wrong").status_code == 401
    assert login(c, user="nobody").status_code == 401
    r = login(c)
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert c.get("/api/auth/me").json() == {"username": "alice"}
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]
    assert c.post("/api/auth/logout", headers=H).status_code == 204
    assert c.get("/api/auth/me").status_code == 401


def test_login_rate_limit(env):
    app, _, _ = env
    c = TestClient(app)
    codes = [login(c, pw="wrong").status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[-1] == 429


def test_topic_job_validation_and_lifecycle(env):
    app, runner, _ = env
    c = TestClient(app)
    login(c)
    assert c.post("/api/jobs", data={"mode": "topic", "topic": "x"}, headers=H).status_code == 422
    assert c.post("/api/jobs", data={"mode": "topic", "topic": "weather", "max_cost_usd": "50"}, headers=H).status_code == 422
    r = c.post("/api/jobs", data={"mode": "topic", "topic": "machine learning for weather forecasting", "n_seeds": "3"}, headers=H)
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "queued" and job["max_cost_usd"] == 5.0
    j = wait_finished(runner, c, job["id"])
    assert j["status"] == "completed", j
    assert [x["id"] for x in c.get("/api/jobs").json()] == [job["id"]]
    # Bob cannot see or touch Alice's review.
    b = TestClient(app)
    login(b, "bob", "bob-password-12")
    assert b.get(f"/api/jobs/{job['id']}").status_code == 404
    assert b.delete(f"/api/jobs/{job['id']}", headers=H).status_code == 404
    assert c.delete(f"/api/jobs/{job['id']}", headers=H).status_code == 204
    assert c.get("/api/jobs").json() == []


def test_failed_and_paused_jobs(env):
    app, runner, _ = env
    c = TestClient(app)
    login(c)
    runner.command = fake_command("fail")
    jid = c.post("/api/jobs", data={"mode": "topic", "topic": "machine learning for weather forecasting"}, headers=H).json()["id"]
    j = wait_finished(runner, c, jid)
    assert j["status"] == "failed" and "boom" in j["error"]
    runner.command = fake_command("pause")
    jid = c.post("/api/jobs", data={"mode": "topic", "topic": "machine learning for weather forecasting", "review_criteria": "true"},
                 headers=H).json()["id"]
    assert wait_finished(runner, c, jid)["status"] == "awaiting_criteria"
    assert c.put(f"/api/jobs/{jid}/criteria", json={"text": "# Review criteria\n\nInclude forecasting models only."}, headers=H).status_code == 200
    assert c.get(f"/api/jobs/{jid}/criteria").json()["text"].startswith("# Review criteria")
    runner.command = fake_command("done")
    assert c.post(f"/api/jobs/{jid}/resume", headers=H).json()["status"] == "queued"
    assert wait_finished(runner, c, jid)["status"] == "completed"  # the earlier pause line is ignored


def test_pdf_upload_checks_and_cleanup(env):
    app, runner, s = env
    c = TestClient(app)
    login(c)
    bad = c.post("/api/jobs", data={"mode": "pdf"}, files=[("files", ("a.pdf", b"not a pdf", "application/pdf"))], headers=H)
    assert bad.status_code == 422
    assert c.post("/api/jobs", data={"mode": "pdf"}, files=[("files", ("a.txt", PDF, "text/plain"))], headers=H).status_code == 422
    r = c.post("/api/jobs", data={"mode": "pdf"}, headers=H,
               files=[("files", ("../../etc/seed one.pdf", PDF, "application/pdf")), ("files", ("two.pdf", PDF, "application/pdf"))])
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["seed_files"] == ["seed one.pdf", "two.pdf"] and job["n_seeds"] == 2
    folder = s.uploads_dir / job["id"]
    assert sorted(p.name for p in folder.iterdir()) == ["00-seed one.pdf", "01-two.pdf"]
    wait_finished(runner, c, job["id"])
    assert not folder.exists(), "uploaded PDFs are deleted once the seeds are imported"


def _fixture_store(run_dir: Path) -> None:
    st = Store(run_dir / "store.sqlite")
    papers = [("W1", "Seed paper", 1.0, "seed", None), ("W2", "Weather forecasting model A", 0.8, "candidate", (10, 1)),
              ("W3", "Unrelated thing", 0.3, "discard", (1, 0)), ("W4", "Forecasting model B", 0.75, "candidate", (8, 1))]
    for pid, title, best, seg, j in papers:
        st.upsert_paper({"id": pid, "openalex": pid, "title": title, "abstract": f"About {title}", "year": 2026})
        st.x("UPDATE papers SET best=?, segment=?, centroid=?, topic=? WHERE id=?", (best, seg, best, best, pid))
        if j:
            st.x("INSERT INTO judgments VALUES (?, 'confirm', ?, ?, 'reason', 'm')", (pid, j[0], j[1]))
    st.x("INSERT INTO seeds VALUES ('W1', 'Seed paper', 'test', 'given', 1)")
    st.x("CREATE TABLE final (paper_id TEXT PRIMARY KEY, score REAL, include INTEGER)")
    st.many("INSERT INTO final VALUES (?, ?, ?)", [("W2", 0.9, 1), ("W4", 0.8, 1), ("W3", 0.1, 0)])


def test_results_api(env):
    app, runner, s = env
    c = TestClient(app)
    login(c)
    jid = c.post("/api/jobs", data={"mode": "topic", "topic": "machine learning for weather forecasting"}, headers=H).json()["id"]
    wait_finished(runner, c, jid)
    run_name = f"web-{jid[:8]}"
    _fixture_store(s.runs_dir / run_name)
    final = c.get(f"/api/jobs/{jid}/papers").json()
    assert final["total"] == 3 and [p["id"] for p in final["items"]] == ["W1", "W2", "W4"]
    assert final["items"][1]["url"] == "https://openalex.org/W2" and final["items"][1]["llm_score"] == 10
    every = c.get(f"/api/jobs/{jid}/papers", params={"view": "all", "sort": "score", "page_size": 2}).json()
    assert every["total"] == 4 and len(every["items"]) == 2
    assert c.get(f"/api/jobs/{jid}/papers", params={"q": "unrelated", "view": "all"}).json()["total"] == 1
    assert c.get(f"/api/jobs/{jid}/papers", params={"sort": "drop table"}).status_code == 422
    d = c.get(f"/api/jobs/{jid}/paper", params={"pid": "W2"}).json()
    assert d["abstract"] == "About Weather forecasting model A" and d["llm_reason"] == "reason"
    assert c.get(f"/api/jobs/{jid}/paper", params={"pid": "nope"}).status_code == 404
    assert c.get(f"/api/jobs/{jid}/seeds").json()[0]["title"] == "Seed paper"
    assert c.get(f"/api/jobs/{jid}/progress").json()["stage"]["key"] == "done"
    assert c.get(f"/api/jobs/{jid}/export/xlsx").status_code == 404
