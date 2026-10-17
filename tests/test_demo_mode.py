"""Day 3 tests: the whole pipeline runs with NO Azure account (DEMO_MODE).

Embeddings are replaced by a tiny deterministic bag-of-words embedder so tests need no model
download. Azure clients and the Video Indexer service are forbidden: touching them fails the test.
"""
import glob
import hashlib
import json
import os

import pytest
from langchain_core.embeddings import Embeddings

from backend.src import local_mode
from backend.src.config import get_settings
from backend.src.graph import nodes, workflow
from backend.src.graph.state import STATUS_ERROR, STATUS_FAIL, STATUS_PASS


class BowEmbeddings(Embeddings):
    """Hashed bag-of-words: similar words -> similar vectors. Counts calls to embed_documents."""
    calls = 0

    def __init__(self, **kwargs): pass

    def _vec(self, text, dim=128):
        v = [0.0] * dim
        for w in text.lower().split():
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
        return v

    def embed_documents(self, texts):
        BowEmbeddings.calls += 1
        return [self._vec(t) for t in texts]

    def embed_query(self, text): return self._vec(text)


def _forbid(*a, **k):
    raise AssertionError("Azure / Video Indexer must NOT be touched in DEMO_MODE")


@pytest.fixture
def demo_env(monkeypatch, tmp_path):
    for k in ["GROQ_API_KEY", "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY", "AZURE_SEARCH_INDEX_NAME",
              "AZURE_VI_NAME", "AZURE_VI_ACCOUNT_ID"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("LOCAL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(nodes, "FastEmbedEmbeddings", BowEmbeddings)
    monkeypatch.setattr(nodes, "VideoIndexerService", _forbid)
    monkeypatch.setattr(nodes, "AzureSearch", _forbid)
    local_mode.clear_store_memo()
    BowEmbeddings.calls = 0


def run(url):
    return workflow.create_graph().invoke(
        {"video_url": url, "video_id": "vid_t", "compliance_results": [], "errors": []}
    )


# ---------- config ----------
@pytest.mark.parametrize("val,expected", [("true", True), ("1", True), ("YES", True), ("false", False), ("", False)])
def test_demo_flag_parsing(monkeypatch, val, expected):
    monkeypatch.setenv("DEMO_MODE", val)
    assert get_settings().demo_mode is expected


def test_demo_off_by_default(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert get_settings().demo_mode is False


# ---------- samples ----------
def test_samples_are_well_formed():
    files = glob.glob(get_settings().samples_dir + "/*.json")
    assert len(files) >= 4
    for f in files:
        d = json.load(open(f))
        assert d["id"] == os.path.basename(f)[:-5]
        assert d["transcript"].strip()
        assert d["expected"]["status"] in ("PASS", "FAIL")


def test_load_sample_ok_and_errors():
    sd = get_settings().samples_dir
    out = local_mode.load_sample("demo://clean_disclosed", sd)
    assert out["transcript"] and isinstance(out["ocr_text"], list)
    with pytest.raises(ValueError, match="Unknown demo sample"):
        local_mode.load_sample("demo://nope", sd)
    with pytest.raises(ValueError, match="demo://"):
        local_mode.load_sample("https://youtu.be/abc", sd)
    with pytest.raises(ValueError):
        local_mode.load_sample("demo://../../etc/passwd", sd)  # no path traversal


# ---------- local vector store ----------
def test_local_vector_store_builds_retrieves_and_caches(tmp_path):
    s = get_settings()
    local_mode.clear_store_memo()
    BowEmbeddings.calls = 0
    store = local_mode.get_local_vector_store(BowEmbeddings(), s.docs_dir, str(tmp_path), "tag")
    docs = store.similarity_search("disclose material connection endorsement influencer", k=3)
    assert len(docs) == 3 and all(d.page_content.strip() for d in docs)
    assert all(d.metadata["source"].endswith(".pdf") for d in docs)
    assert BowEmbeddings.calls >= 1
    assert list(tmp_path.glob("demo_index_*.json")), "index should be cached to disk"

    # new process simulation: memo cleared, disk cache must be used (no re-embedding of documents)
    local_mode.clear_store_memo()
    calls_before = BowEmbeddings.calls
    store2 = local_mode.get_local_vector_store(BowEmbeddings(), s.docs_dir, str(tmp_path), "tag")
    assert BowEmbeddings.calls == calls_before
    assert len(store2.similarity_search("disclosure", k=2)) == 2


def test_local_vector_store_without_pdfs_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        local_mode.get_local_vector_store(BowEmbeddings(), str(tmp_path), str(tmp_path), "t")


# ---------- mock auditor agrees with the sample labels ----------
@pytest.mark.parametrize("path", sorted(glob.glob(get_settings().samples_dir + "/*.json")))
def test_mock_auditor_matches_expected_labels(path):
    d = json.load(open(path))
    msg = type("M", (), {"content": f"TRANSCRIPT: {d['transcript']}\n ON-SCREEN TEXT (OCR): {d['ocr_text']}"})()
    out = json.loads(local_mode.MockAuditLLM().invoke([msg]).content)
    assert out["status"] == d["expected"]["status"]
    got = {i["category"] for i in out["compliance_results"]}
    assert got == set(d["expected"]["categories"])
    assert "MOCK" in out["final_report"]


# ---------- end to end, no Azure, no keys ----------
def test_end_to_end_fail_sample(demo_env):
    out = run("demo://undisclosed_sponsorship")
    assert out["final_status"] == STATUS_FAIL
    assert out["compliance_results"][0]["category"] == "Missing sponsorship disclosure"
    assert out["compliance_results"][0]["severity"] == "CRITICAL"
    assert "MOCK" in out["final_report"]


def test_end_to_end_pass_sample(demo_env):
    out = run("demo://clean_disclosed")
    assert out["final_status"] == STATUS_PASS and out["compliance_results"] == []


def test_unknown_sample_is_error_and_never_reaches_auditor(demo_env, monkeypatch):
    monkeypatch.setattr(nodes, "FastEmbedEmbeddings", _forbid)
    out = run("demo://does_not_exist")
    assert out["final_status"] == STATUS_ERROR
    assert "Available samples" in out["errors"][0]


def test_real_llm_is_used_when_key_present(demo_env, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    seen = {}

    class FakeLLM:
        def __init__(self, **kw): seen.update(kw)
        def invoke(self, msgs):
            return type("R", (), {"content": '{"status":"PASS","compliance_results":[],"final_report":"real"}'})()

    monkeypatch.setattr(nodes, "ChatOpenAI", FakeLLM)
    out = run("demo://undisclosed_sponsorship")
    assert seen["api_key"] == "k" and out["final_report"] == "real"


def test_non_demo_mode_still_requires_azure_config(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    for k in ["GROQ_API_KEY", "AZURE_SEARCH_ENDPOINT"]:
        monkeypatch.delenv(k, raising=False)
    out = nodes.audit_content_node({"transcript": "hi", "ocr_text": []})
    assert out["final_status"] == STATUS_ERROR and "GROQ_API_KEY" in out["errors"][0]