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
