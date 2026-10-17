"""
Workflow Definition for the Policy Lens AI.

This module defines the Directed Acyclic Graph (DAG) that orchestrates the
video compliance audit process. It connects the nodes (functional units)
using the StateGraph primitive from LangGraph.

Architecture:
    [START] -> [index_video_node] -> [audit_content_node] -> [END]
"""

from langgraph.graph import StateGraph, END
from typing import Literal

# Import the State Schema
from backend.src.graph.state import VideoAuditState, STATUS_ERROR

# Import the Functional Nodes
from backend.src.graph.nodes import (
    index_video_node,
    audit_content_node
)

def route_after_indexer(state: VideoAuditState) -> str:
    """If indexing failed there is nothing to audit: end the run instead of calling the LLM."""
    return "end" if state.get("final_status") == STATUS_ERROR else "auditor"

def create_graph():
    """
    Constructs and compiles the LangGraph workflow.

    Returns:
        CompiledGraph: A runnable graph object ready for execution.
    """
    # 1. Initialize the Graph with the State Schema
    # This ensures all nodes adhere to the 'VideoAuditState' data structure.
    workflow = StateGraph(VideoAuditState)

    # 2. Add Nodes (The Workers)
    # The first argument is the unique name of the node in the graph.
    # The second argument is the function to execute.
    workflow.add_node("indexer", index_video_node)
    workflow.add_node("auditor", audit_content_node)

    # 3. Define Edges (The Logic Flow)
    # Define the entry point: When the graph starts, go to 'indexer'.
    workflow.set_entry_point("indexer")

    # Connect 'indexer' -> 'auditor'
    # Once the video is indexed (transcript extracted), move to compliance auditing.
    workflow.add_conditional_edges(
        "indexer",
        route_after_indexer,
        {"auditor": "auditor", "end": END},
    )

    # Connect 'auditor' -> END
    # Once the audit is complete, the workflow finishes.
    workflow.add_edge("auditor", END)

    # 4. Compile the Graph
    # This validates the connections and creates the executable runnable.
    app = workflow.compile()

    return app

# Expose the runnable app for import by the API or CLI
app = create_graph()