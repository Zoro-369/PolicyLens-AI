"""Day 2 regression tests: error routing, cleanup, polling limits, config.

No network or Azure access is used: every external client is replaced by a fake.
"""
import os
import pytest

from backend.src.graph import nodes, workflow
from backend.src.graph.state import STATUS_ERROR, STATUS_PASS
from backend.src.services import video_indexer as vi_mod
from backend.src.config import ConfigError, get_settings, require


# ---------- fakes ----------
class FailingIndexer:
    def __init__(self): pass
    def download_youtube_video(self, url, output_path): raise Exception("boom: download failed")


class OkIndexer:
    created_paths = []
    def __init__(self): pass
    def download_youtube_video(self, url, output_path):
        with open(output_path, "wb") as f:
            f.write(b"fake video")
        OkIndexer.created_paths.append(output_path)
        return output_path
    def upload_video(self, path, video_name): return "azure-id"
    def wait_for_processing(self, vid): return {}
    def extract_data(self, raw):
        return {"transcript": "This cream cures acne guaranteed", "ocr_text": ["#ad"],
                "video_metadata": {"duration": 12, "platform": "youtube"}}


def _forbid_llm(*a, **k):
    raise AssertionError("LLM/embeddings/search must NOT be constructed when indexing failed")


def _env(monkeypatch):
    for k, v in {
        "GROQ_API_KEY": "test-key",
        "AZURE_SEARCH_ENDPOINT": "https://x", "AZURE_SEARCH_API_KEY": "k",
        "AZURE_SEARCH_INDEX_NAME": "idx",
    }.items():
        monkeypatch.setenv(k, v)


# ---------- routing / error handling ----------
def test_indexer_failure_ends_graph_with_error_and_never_calls_llm(monkeypatch):
    monkeypatch.setattr(nodes, "VideoIndexerService", FailingIndexer)
    monkeypatch.setattr(nodes, "ChatOpenAI", _forbid_llm)
    monkeypatch.setattr(nodes, "FastEmbedEmbeddings", _forbid_llm)
    monkeypatch.setattr(nodes, "AzureSearch", _forbid_llm)

    out = workflow.create_graph().invoke(
        {"video_url": "https://youtu.be/abc", "video_id": "vid_1", "compliance_results": [], "errors": []}
    )
    assert out["final_status"] == STATUS_ERROR
    assert out["errors"] and "download failed" in out["errors"][0]
    assert "no compliance verdict" in out["final_report"]


def test_non_youtube_url_is_error(monkeypatch):
    monkeypatch.setattr(nodes, "VideoIndexerService", OkIndexer)
    out = nodes.index_video_node({"video_url": "https://evil.example/x.mp4", "video_id": "v"})
    assert out["final_status"] == STATUS_ERROR


def test_temp_video_deleted_even_when_later_step_fails(monkeypatch):
    class UploadFails(OkIndexer):
        def upload_video(self, path, video_name): raise Exception("upload failed")
    OkIndexer.created_paths.clear()
    monkeypatch.setattr(nodes, "VideoIndexerService", UploadFails)
    out = nodes.index_video_node({"video_url": "https://youtu.be/abc", "video_id": "v"})
    assert out["final_status"] == STATUS_ERROR
    assert OkIndexer.created_paths, "download should have happened"
    assert not os.path.exists(OkIndexer.created_paths[0])
    assert not os.path.exists(os.path.dirname(OkIndexer.created_paths[0]))


def test_each_request_gets_its_own_temp_path(monkeypatch):
    OkIndexer.created_paths.clear()
    monkeypatch.setattr(nodes, "VideoIndexerService", OkIndexer)
    for _ in range(2):
        nodes.index_video_node({"video_url": "https://youtu.be/abc", "video_id": "v"})
    a, b = OkIndexer.created_paths
    assert a != b


def test_empty_content_is_error_not_pass():
    out = nodes.audit_content_node({"transcript": "", "ocr_text": []})
    assert out["final_status"] == STATUS_ERROR


def test_missing_config_is_error_with_clear_message(monkeypatch):
    for k in ["GROQ_API_KEY", "AZURE_SEARCH_ENDPOINT"]:
        monkeypatch.delenv(k, raising=False)
    out = nodes.audit_content_node({"transcript": "hello", "ocr_text": []})
    assert out["final_status"] == STATUS_ERROR
    assert "GROQ_API_KEY" in out["errors"][0]


def _patch_clients(monkeypatch, llm_reply):
    class Doc:  page_content = "Rule: disclose paid endorsements."
    class FakeLLM:
        def __init__(self, **k): FakeLLM.last = k
        def invoke(self, msgs):
            class R: content = llm_reply
            return R()
    class FakeEmb:
        def __init__(self, **k): FakeEmb.last = k
        def embed_query(self, q): return [0.0]
    class FakeSearch:
        def __init__(self, **k): pass
        def similarity_search(self, q, k): return [Doc()]
    monkeypatch.setattr(nodes, "ChatOpenAI", FakeLLM)
    monkeypatch.setattr(nodes, "FastEmbedEmbeddings", FakeEmb)
    monkeypatch.setattr(nodes, "AzureSearch", FakeSearch)
    return FakeEmb, FakeLLM


def test_happy_path_pass_and_models_come_from_config(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL", "my-embed")
    monkeypatch.setenv("GROQ_MODEL", "my-llm")
    emb, llm = _patch_clients(monkeypatch, '```json\n{"status":"PASS","compliance_results":[],"final_report":"ok"}\n```')
    out = nodes.audit_content_node({"transcript": "hello", "ocr_text": ["x"]})
    assert out["final_status"] == STATUS_PASS
    assert emb.last["model_name"] == "my-embed"
    assert llm.last["model"] == "my-llm"


def test_severity_is_normalised_so_the_api_schema_never_breaks(monkeypatch):
    _env(monkeypatch)
    reply = ('{"status":"FAIL","final_report":"r","compliance_results":['
             '{"category":"A","severity":"critical","description":"d1"},'
             '{"category":"B","severity":"High","description":"d2"},'
             '{"description":"d3"}]}')
    _patch_clients(monkeypatch, reply)
    out = nodes.audit_content_node({"transcript": "hello", "ocr_text": []})
    assert [i["severity"] for i in out["compliance_results"]] == ["CRITICAL", "WARNING", "WARNING"]
    assert all(set(i) >= {"category", "severity", "description"} for i in out["compliance_results"])


@pytest.mark.parametrize("reply", ["not json at all", '{"status":"MAYBE"}', "```json broken"])
def test_bad_llm_output_is_error_not_fail(monkeypatch, reply):
    _env(monkeypatch)
    _patch_clients(monkeypatch, reply)
    out = nodes.audit_content_node({"transcript": "hello", "ocr_text": []})
    assert out["final_status"] == STATUS_ERROR


# ---------- polling ----------
class _Resp:
    def __init__(self, code, data=None): self.status_code, self._d, self.text = code, data or {}, "x"
    def json(self): return self._d


def _service(monkeypatch):
    for k, v in {"AZURE_VI_ACCOUNT_ID": "a", "AZURE_VI_LOCATION": "l", "AZURE_VI_NAME": "n",
                 "AZURE_SUBSCRIPTION_ID": "s", "AZURE_RESOURCE_GROUP": "r"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(vi_mod, "DefaultAzureCredential", lambda: object())
    svc = vi_mod.VideoIndexerService()
    monkeypatch.setattr(svc, "_fresh_account_token", lambda: "tok")
    return svc


class FakeClock:
    def __init__(self): self.t = 0.0
    def monotonic(self): return self.t
    def sleep(self, s): self.t += s


def test_polling_times_out(monkeypatch):
    svc = _service(monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(vi_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(vi_mod.time, "sleep", clock.sleep)
    monkeypatch.setattr(vi_mod.requests, "get", lambda *a, **k: _Resp(200, {"state": "Processing"}))
    with pytest.raises(vi_mod.VideoIndexerTimeout):
        svc.wait_for_processing("vid", max_wait_s=100)
    assert 100 <= clock.t <= 100.001  # never sleeps past the deadline


def test_polling_backs_off_and_survives_transient_errors(monkeypatch):
    svc = _service(monkeypatch)
    clock = FakeClock()
    sleeps = []
    monkeypatch.setattr(vi_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(vi_mod.time, "sleep", lambda s: (sleeps.append(s), clock.sleep(s)))
    replies = iter([_Resp(503), _Resp(200, {"state": "Processing"}), _Resp(429),
                    _Resp(200, {"state": "Processed", "ok": True})])
    monkeypatch.setattr(vi_mod.requests, "get", lambda *a, **k: next(replies))
    data = svc.wait_for_processing("vid", max_wait_s=1000, initial_interval_s=10, max_interval_s=60)
    assert data["ok"] is True
    assert sleeps == [10, 15.0, 22.5]  # exponential backoff


@pytest.mark.parametrize("state", ["Failed", "Quarantined"])
def test_polling_terminal_failure_states_raise(monkeypatch, state):
    svc = _service(monkeypatch)
    monkeypatch.setattr(vi_mod.requests, "get", lambda *a, **k: _Resp(200, {"state": state}))
    with pytest.raises(Exception, match=state if state == "Quarantined" else "Failed"):
        svc.wait_for_processing("vid", max_wait_s=100)


# ---------- config ----------
def test_config_require_names_missing_vars(monkeypatch):
    monkeypatch.delenv("AZURE_VI_NAME", raising=False)
    with pytest.raises(ConfigError, match="AZURE_VI_NAME"):
        require(get_settings(), ["vi_name"])


def test_embedding_default(monkeypatch):
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert get_settings().embedding_model == "BAAI/bge-small-en-v1.5"


def test_retired_groq_model_is_remapped(monkeypatch):
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    assert get_settings().groq_model == "openai/gpt-oss-120b"