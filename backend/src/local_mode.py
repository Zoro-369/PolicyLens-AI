"""Local / demo mode helpers: run the whole pipeline with NO Azure account.

Three building blocks, all used only when DEMO_MODE=true:

1. load_sample()            sample transcripts instead of Azure Video Indexer
2. get_local_vector_store() in-memory vector store over the policy PDFs instead of Azure AI Search
3. MockAuditLLM             tiny rule-based auditor used ONLY when no GROQ_API_KEY is set,
                            so tests / CI / a keyless demo still run end to end.
                            It is NOT an LLM and its output says so.
"""
import glob
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("policy_lens_ai.local_mode")

DEMO_URL_PREFIX = "demo://"


# ----------------------------------------------------------------------------
# 1. Sample transcripts
# ----------------------------------------------------------------------------
def list_samples(samples_dir: str) -> List[str]:
    return sorted(Path(p).stem for p in glob.glob(os.path.join(samples_dir, "*.json")))


def load_sample(video_url: str, samples_dir: str) -> Dict[str, Any]:
    """Resolve 'demo://<sample_id>' to the same state update the Video Indexer path returns."""
    available = list_samples(samples_dir)
    if not video_url or not video_url.startswith(DEMO_URL_PREFIX):
        raise ValueError(
            f"DEMO_MODE is on: use a demo URL such as '{DEMO_URL_PREFIX}{available[0] if available else 'sample_id'}'. "
            f"Available samples: {available}"
        )
    sample_id = video_url[len(DEMO_URL_PREFIX):].strip()
    # Only plain ids are allowed: no path separators, so a URL cannot escape samples_dir.
    if sample_id not in available:
        raise ValueError(f"Unknown demo sample '{sample_id}'. Available samples: {available}")

    with open(os.path.join(samples_dir, f"{sample_id}.json"), encoding="utf-8") as f:
        data = json.load(f)
    return {
        "transcript": data["transcript"],
        "ocr_text": data.get("ocr_text", []),
        "video_metadata": data.get("video_metadata", {}),
    }


# ----------------------------------------------------------------------------
# 2. Local vector store (no Azure AI Search)
# ----------------------------------------------------------------------------
_STORE_MEMO: Dict[str, Any] = {}


def clear_store_memo() -> None:
    _STORE_MEMO.clear()


def get_local_vector_store(embeddings, docs_dir: str, cache_dir: str, cache_tag: str):
    """Build (or load from disk) an in-memory vector store from the PDFs in docs_dir.

    Same chunking as scripts/index_documents.py (1000 chars, 200 overlap) so local and Azure
    retrieval see the same chunks. The embedded index is cached on disk, keyed by the embedding
    model name and the PDF names/sizes, so it is only computed once.
    """
    from langchain_core.vectorstores import InMemoryVectorStore

    pdfs = sorted(Path(docs_dir).glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No policy PDFs found in {docs_dir}")

    signature = hashlib.sha1(
        (cache_tag + "|" + "|".join(f"{p.name}:{p.stat().st_size}" for p in pdfs)).encode()
    ).hexdigest()[:16]

    if signature in _STORE_MEMO:
        return _STORE_MEMO[signature]

    cache_file = Path(cache_dir) / f"demo_index_{signature}.json"
    if cache_file.exists():
        logger.info(f"Loading cached local index: {cache_file}")
        store = InMemoryVectorStore.load(str(cache_file), embeddings)
    else:
        from langchain_community.document_loaders import PyPDFLoader
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        logger.info(f"Building local index from {len(pdfs)} PDFs (first run only)...")
        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        splits = []
        for pdf in pdfs:
            chunks = splitter.split_documents(PyPDFLoader(str(pdf)).load())
            for c in chunks:
                c.metadata["source"] = pdf.name
            splits.extend(chunks)

        store = InMemoryVectorStore(embeddings)
        store.add_documents(splits)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        store.dump(str(cache_file))
        logger.info(f"Local index built: {len(splits)} chunks -> {cache_file}")

    _STORE_MEMO[signature] = store
    return store


# ----------------------------------------------------------------------------
# 3. Mock auditor (keyless fallback; NOT an LLM)
# ----------------------------------------------------------------------------
MOCK_BANNER = "[OFFLINE MOCK AUDITOR: simple keyword rules, not an LLM]"

_DISCLOSURE = [
    "sponsored by", "this video is sponsored", "this is an ad", "paid partnership", "they paid me",
    "i was paid", "paid to make", "#ad", "#sponsored", "affiliate link", "gifted to me", "gifted me",
    "in partnership with",
]
_CONNECTION_CUES = [
    "sent it to me", "sent me", "working together", "use my code", "use code", "discount code",
    "affiliate", "gifted", "sponsor", "paid", "partner", "link below", "link in the description",
]
_CLAIMS = [
    "cures", "guarantee", "100%", "no side effects", "clinically proven", "works for everyone",
    "doctors do not want",
]


class _Reply:
    def __init__(self, content: str):
        self.content = content


class MockAuditLLM:
    """Drop-in for the chat model: .invoke(messages) -> object with .content (JSON string)."""

    def invoke(self, messages) -> _Reply:
        text = messages[-1].content if messages else ""
        m = re.search(r"TRANSCRIPT:\s*(.*?)\s*ON-SCREEN TEXT \(OCR\):\s*(.*)", text, re.DOTALL)
        transcript = (m.group(1) if m else text).strip()
        ocr = (m.group(2) if m else "").strip()
        low, full = transcript.lower(), (transcript + " " + ocr).lower()

        issues = []
        positions = [low.find(p) for p in _DISCLOSURE if p in low]
        has_cue = any(c in full for c in _CONNECTION_CUES)

        if has_cue and not positions:
            issues.append({
                "category": "Missing sponsorship disclosure", "severity": "CRITICAL",
                "description": "The video suggests a material connection with the brand "
                               "(free product, code or link) but never discloses it.",
            })
        elif positions and min(positions) / max(len(low), 1) > 0.30:
            issues.append({
                "category": "Disclosure not clear or conspicuous", "severity": "WARNING",
                "description": "A disclosure exists but only appears late in the video, "
                               "after the endorsement.",
            })

        hits = sorted({c for c in _CLAIMS if c in full})
        if hits:
            issues.append({
                "category": "Unsupported claim", "severity": "CRITICAL",
                "description": "Absolute or health claims need substantiation: " + ", ".join(hits) + ".",
            })

        status = "FAIL" if issues else "PASS"
        report = (f"{MOCK_BANNER} Found {len(issues)} potential issue(s)."
                  if issues else f"{MOCK_BANNER} No issues found by the keyword rules.")
        return _Reply(json.dumps({"compliance_results": issues, "status": status, "final_report": report}))