"""Central configuration for PolicyLens AI.

All environment variables are read in ONE place. Values are read on every call to
get_settings() (not cached at import time) so that:
  * load_dotenv() ordering never matters, and
  * tests can monkeypatch environment variables.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# backend/src/config.py -> repo root is two levels above "src"
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
RETIRED_GROQ_MODELS = {"llama-3.3-70b-versatile"}  # remapped to DEFAULT_GROQ_MODEL


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


@dataclass(frozen=True)
class Settings:
    # LLM (Groq, OpenAI-compatible API)
    groq_api_key: Optional[str]
    groq_model: str
    groq_base_url: str

    # Embeddings (local FastEmbed). MUST match the model used by scripts/index_documents.py
    embedding_model: str

    # Azure AI Search
    search_endpoint: Optional[str]
    search_api_key: Optional[str]
    search_index_name: Optional[str]

    # Azure Video Indexer
    vi_account_id: Optional[str]
    vi_location: Optional[str]
    vi_name: Optional[str]
    subscription_id: Optional[str]
    resource_group: Optional[str]

    # Demo / local mode: no Azure needed (sample transcripts + local vector store)
    demo_mode: bool
    samples_dir: str
    docs_dir: str
    cache_dir: str

    # Behaviour knobs
    retrieval_k: int
    request_timeout_s: float          # connect+read timeout for normal HTTP calls
    upload_timeout_s: float           # read timeout for the video upload
    vi_poll_initial_s: float          # first wait between status checks
    vi_poll_max_interval_s: float     # backoff cap
    vi_max_wait_s: float              # give up after this long

    def missing(self, names: List[str]) -> List[str]:
        """Return the attribute names that are unset."""
        return [n for n in names if not getattr(self, n)]


def get_settings() -> Settings:
    model = os.getenv("GROQ_MODEL") or DEFAULT_GROQ_MODEL
    if model in RETIRED_GROQ_MODELS:
        model = DEFAULT_GROQ_MODEL

    return Settings(
        groq_api_key=os.getenv("GROQ_API_KEY"),
        groq_model=model,
        groq_base_url=os.getenv("GROQ_BASE_URL") or "https://api.groq.com/openai/v1",
        embedding_model=os.getenv("EMBEDDING_MODEL") or "BAAI/bge-small-en-v1.5",
        search_endpoint=os.getenv("AZURE_SEARCH_ENDPOINT"),
        search_api_key=os.getenv("AZURE_SEARCH_API_KEY"),
        search_index_name=os.getenv("AZURE_SEARCH_INDEX_NAME"),
        vi_account_id=os.getenv("AZURE_VI_ACCOUNT_ID"),
        vi_location=os.getenv("AZURE_VI_LOCATION"),
        vi_name=os.getenv("AZURE_VI_NAME"),
        subscription_id=os.getenv("AZURE_SUBSCRIPTION_ID"),
        resource_group=os.getenv("AZURE_RESOURCE_GROUP"),
        demo_mode=_bool("DEMO_MODE", False),
        samples_dir=os.getenv("DEMO_SAMPLES_DIR") or str(REPO_ROOT / "backend" / "data" / "samples"),
        docs_dir=os.getenv("POLICY_DOCS_DIR") or str(REPO_ROOT / "backend" / "data"),
        cache_dir=os.getenv("LOCAL_CACHE_DIR") or str(REPO_ROOT / ".cache"),
        retrieval_k=_int("RETRIEVAL_K", 3),
        request_timeout_s=_float("HTTP_TIMEOUT_S", 30),
        upload_timeout_s=_float("UPLOAD_TIMEOUT_S", 600),
        vi_poll_initial_s=_float("VI_POLL_INITIAL_S", 10),
        vi_poll_max_interval_s=_float("VI_POLL_MAX_INTERVAL_S", 60),
        vi_max_wait_s=_float("VI_MAX_WAIT_S", 1800),
    )


# Maps Settings attribute -> the env var a human should set.
ENV_NAME = {
    "groq_api_key": "GROQ_API_KEY",
    "vi_account_id": "AZURE_VI_ACCOUNT_ID",
    "vi_location": "AZURE_VI_LOCATION",
    "vi_name": "AZURE_VI_NAME",
    "subscription_id": "AZURE_SUBSCRIPTION_ID",
    "resource_group": "AZURE_RESOURCE_GROUP",
    "search_endpoint": "AZURE_SEARCH_ENDPOINT",
    "search_api_key": "AZURE_SEARCH_API_KEY",
    "search_index_name": "AZURE_SEARCH_INDEX_NAME",
}


def require(settings: Settings, attrs: List[str]) -> None:
    """Raise ConfigError naming the missing environment variables."""
    missing = settings.missing(attrs)
    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(ENV_NAME.get(m, m) for m in missing)
        )