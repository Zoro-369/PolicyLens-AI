import json
import os
import re
import shutil
import tempfile
import logging
from typing import Dict, Any

from langchain_community.embeddings import FastEmbedEmbeddings
from langchain_openai import ChatOpenAI
from langchain_community.vectorstores import AzureSearch
from langchain_core.messages import SystemMessage, HumanMessage

from backend.src.config import get_settings, require
from backend.src.graph.state import (
    VideoAuditState, STATUS_PASS, STATUS_FAIL, STATUS_ERROR,
)
from backend.src.services.video_indexer import VideoIndexerService

from backend.src.local_mode import (
    MockAuditLLM, get_local_vector_store, load_sample,
)

# Configure Logger
logger = logging.getLogger("policy_lens_ai")
logging.basicConfig(level=logging.INFO)

ALLOWED_SEVERITIES = ("CRITICAL", "WARNING")


def _error_update(message: str, report: str) -> Dict[str, Any]:
    """Standard state update for any failure that prevents a verdict."""
    return {
        "errors": [message],
        "final_status": STATUS_ERROR,
        "final_report": report,
    }


def _normalize_issues(raw_issues) -> list:
    """Make LLM-produced findings safe for the API schema.

    The API only accepts severity CRITICAL | WARNING. An LLM may answer "High", "critical", etc.
    Unknown severities become WARNING; missing text fields become empty strings.
    """
    issues = []
    for item in raw_issues or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "")).strip().upper()
        issues.append({
            "category": str(item.get("category", "Uncategorised")),
            "severity": severity if severity in ALLOWED_SEVERITIES else "WARNING",
            "description": str(item.get("description", "")),
            "timestamp": item.get("timestamp"),
        })
    return issues


# --- NODE 1: THE INDEXER ---
def index_video_node(state: VideoAuditState) -> Dict[str, Any]:
    """
    Downloads YouTube video, uploads to Azure VI, and extracts insights.
    The local video is always deleted, even if a step fails.
    """
    video_url = state.get("video_url")
    video_id_input = state.get("video_id", "vid_demo")

    logger.info(f"--- [Node: Indexer] Processing: {video_url} ---")

    cfg = get_settings()
    if cfg.demo_mode:
        # DEMO_MODE: read a sample transcript instead of calling YouTube + Azure Video Indexer.
        try:
            return load_sample(video_url, cfg.samples_dir)
        except Exception as e:
            logger.error(f"Demo sample failed: {e}")
            update = _error_update(str(e), f"Demo sample could not be loaded: {e}")
            update.update({"transcript": "", "ocr_text": []})
            return update


    # Unique per-request directory: concurrent requests can no longer overwrite each other.
    tmp_dir = tempfile.mkdtemp(prefix="policylens_")
    local_filename = os.path.join(tmp_dir, "video.mp4")

    try:
        vi_service = VideoIndexerService()

        # 1. DOWNLOAD
        if video_url and ("youtube.com" in video_url or "youtu.be" in video_url):
            local_path = vi_service.download_youtube_video(video_url, output_path=local_filename)
        else:
            raise ValueError("Please provide a valid YouTube URL for this test.")

        # 2. UPLOAD
        azure_video_id = vi_service.upload_video(local_path, video_name=video_id_input)
        logger.info(f"Upload Success. Azure ID: {azure_video_id}")

        # 3. WAIT (bounded: raises VideoIndexerTimeout)
        raw_insights = vi_service.wait_for_processing(azure_video_id)

        # 4. EXTRACT
        clean_data = vi_service.extract_data(raw_insights)

        logger.info("--- [Node: Indexer] Extraction Complete ---")
        return clean_data

    except Exception as e:
        logger.error(f"Video Indexer Failed: {e}")
        update = _error_update(
            str(e), "Video processing failed, so no compliance verdict was produced."
        )
        update.update({"transcript": "", "ocr_text": []})
        return update

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- NODE 2: THE COMPLIANCE AUDITOR ---
def audit_content_node(state: VideoAuditState) -> Dict[str, Any]:
    """
    Performs Retrieval-Augmented Generation (RAG) to audit the content.
    Returns PASS / FAIL on a completed audit, ERROR if no verdict could be produced.
    """
    logger.info("--- [Node: Auditor] querying Knowledge Base & LLM ---")

    transcript = state.get("transcript", "") or ""
    ocr_text = state.get("ocr_text", []) or []

    if not transcript and not ocr_text:
        logger.warning("No transcript or on-screen text available. Skipping audit.")
        return _error_update(
            "empty_content", "Audit skipped: no transcript or on-screen text was extracted."
        )

    cfg = get_settings()
    response = None
    try:
        if not cfg.demo_mode:
            require(cfg, ["groq_api_key", "search_endpoint", "search_api_key", "search_index_name"])

        # Initialize Clients
        if cfg.groq_api_key:
            llm = ChatOpenAI(
                model=cfg.groq_model,
                api_key=cfg.groq_api_key,
                base_url=cfg.groq_base_url,
                temperature=0.0,
            )
        else:
            # Only reachable in DEMO_MODE (require() above guarantees a key otherwise).
            logger.warning("DEMO_MODE without GROQ_API_KEY: using the keyword-rule mock auditor.")
            llm = MockAuditLLM()

        # Must be the same model that scripts/index_documents.py used to build the index.
        embeddings = FastEmbedEmbeddings(model_name=cfg.embedding_model)

        if cfg.demo_mode:
            # Local in-memory index over the policy PDFs: no Azure AI Search needed.
            vector_store = get_local_vector_store(
                embeddings, cfg.docs_dir, cfg.cache_dir, cache_tag=cfg.embedding_model
            )
        else:
            vector_store = AzureSearch(
                azure_search_endpoint=cfg.search_endpoint,
                azure_search_key=cfg.search_api_key,
                index_name=cfg.search_index_name,
                embedding_function=embeddings.embed_query,
            )

        # RAG Retrieval
        query_text = f"{transcript} {' '.join(ocr_text)}"
        docs = vector_store.similarity_search(query_text, k=cfg.retrieval_k)
        retrieved_rules = "\n\n".join([doc.page_content for doc in docs])

        # --- PROMPT WITH STRICT SCHEMA ---
        system_prompt = f"""
            You are a Senior Brand Compliance Auditor.

            OFFICIAL REGULATORY RULES:
            {retrieved_rules}

            INSTRUCTIONS:
            1. Analyze the Transcript and OCR text below.
            2. Identify ANY violations of the rules.
            3. Return strictly JSON in the following format:

            {{
                "compliance_results": [
                    {{
                        "category": "Claim Validation",
                        "severity": "CRITICAL",
                        "description": "Explanation of the violation..."
                    }}
                ],
                "status": "FAIL",
                "final_report": "Summary of findings..."
            }}

            If no violations are found, set "status" to "PASS" and "compliance_results" to [].
        """

        user_message = f"""
            VIDEO METADATA: {state.get('video_metadata', {})}
            TRANSCRIPT: {transcript}
            ON-SCREEN TEXT (OCR): {ocr_text}
        """

        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])

        # Clean Markdown if present (```json ... ```)
        content = response.content
        if "```" in content:
            match = re.search(r"```(?:json)?(.*?)```", content, re.DOTALL)
            if not match:
                raise ValueError("LLM returned an unterminated code block.")
            content = match.group(1)

        audit_data = json.loads(content.strip())

        status = audit_data.get("status")
        if status not in (STATUS_PASS, STATUS_FAIL):
            raise ValueError(f"LLM returned an invalid status: {status!r}")

        return {
            "compliance_results": _normalize_issues(audit_data.get("compliance_results")),
            "final_status": status,
            "final_report": audit_data.get("final_report", "No report generated."),
        }

    except Exception as e:
        logger.error(f"System Error in Auditor Node: {str(e)}")
        logger.error(f"Raw LLM Response: {response.content if response is not None else 'None'}")
        return _error_update(
            str(e), "The audit could not be completed, so no compliance verdict was produced."
        )