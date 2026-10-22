from litscout.sources.records import ARXIV_RE, canonical_id, dedupe, is_paper, titles_match


def test_dedupe_merges_by_doi_and_title():
    recs = [{"doi": "10.1/x", "title": "A Study of Weather Forecasting Models", "abstract": None},
            {"doi": "10.1/x", "title": "A study of weather forecasting models", "abstract": "text", "year": 2025},
            {"title": "A Study of Weather Forecasting Models", "arxiv": "2501.00001"}]
    out = dedupe(recs)
    assert len(out) == 1
    assert out[0]["abstract"] == "text" and out[0]["year"] == 2025 and out[0]["arxiv"] == "2501.00001"
    assert out[0]["id"] == "doi:10.1/x"


def test_arxiv_and_ids():
    assert ARXIV_RE.search("see arXiv:2603.26130v1").group(1) == "2603.26130"
    assert ARXIV_RE.search("https://arxiv.org/abs/2509.01494").group(1) == "2509.01494"
    assert canonical_id({"openalex": "W1", "doi": "10.1/x"}) == "W1"
    assert canonical_id({"title": "x"}).startswith("local:")


def test_filters_and_title_match():
    assert not is_paper({"title": "Replication package for X"})
    assert not is_paper({"title": "Data", "type": "dataset"})
    assert titles_match("WX-Bench: Assessing LLMs", "WX-Bench Assessing LLMs")


def test_github_repo_records():
    from litscout.sources.github import GitHub
    from litscout.sources.records import canonical_id
    gh = GitHub(None)
    gh.http.get_json = lambda url, params=None: {"items": [
        {"name": "ForecastBench", "full_name": "forecast-bench/ForecastBench", "description": "Open benchmark for weather forecasting models",
         "created_at": "2026-10-01T00:00:00Z", "stargazers_count": 69, "html_url": "https://github.com/forecast-bench/ForecastBench"},
        {"name": "wx-bench", "full_name": "example/wx-bench", "description": "Benchmark, paper: arxiv.org/abs/2601.19494",
         "created_at": "2026-01-01T00:00:00Z", "stargazers_count": 246, "html_url": "https://github.com/example/wx-bench"},
        {"name": "empty", "full_name": "x/empty", "description": None, "created_at": None, "html_url": "https://github.com/x/empty"}]}
    recs = gh.search("weather forecasting benchmark", 10)
    assert [r["linked_arxiv"] for r in recs] == [None, "2601.19494"]
    assert recs[0]["title"].startswith("ForecastBench:") and recs[0]["venue"] == "GitHub repository"
    assert canonical_id(recs[0]) == "github:forecast-bench/ForecastBench"
