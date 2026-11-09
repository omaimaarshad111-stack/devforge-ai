"""LangGraph orchestration for the end-to-end software development lifecycle."""

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.architect import ArchitectAgent
from app.agents.debugger import DEFAULT_MAX_DEBUGGING_ATTEMPTS, DebuggerAgent
from app.agents.developer import DeveloperAgent
from app.agents.project_manager import ProjectManagerAgent
from app.agents.reviewer import CodeReviewerAgent
from app.agents.tester import TesterAgent
from app.graph import nodes
from app.graph.nodes import WorkflowAgents
from app.graph.state import DevForgeState
from app.tools.code_analyzer import CodeAnalyzer
from app.tools.file_manager import FileManager


def build_workflow(
    *,
    agents: WorkflowAgents | None = None,
    file_manager: FileManager | None = None,
    analyzer: CodeAnalyzer | None = None,
    max_debugging_attempts: int = DEFAULT_MAX_DEBUGGING_ATTEMPTS,
    checkpointer: Any | None = None,
) -> CompiledStateGraph:
    """Build and compile the graph with an explicitly managed checkpointer.

    Production callers should supply a persistent SQLite checkpointer. Tests
    may inject LangGraph's in-memory saver when persistence is not under test.
    """
    if checkpointer is None:
        raise ValueError(
            "A checkpointer is required. Use WorkflowRunner for persistent "
            "SQLite checkpointing or inject a saver explicitly."
        )
    if max_debugging_attempts < 0:
        raise ValueError("max_debugging_attempts must be non-negative.")
    analyzer_instance = analyzer or CodeAnalyzer()
    agent_set = agents or _create_default_agents(
        analyzer_instance,
        max_debugging_attempts,
    )
    manager = file_manager or FileManager()
    graph_builder = StateGraph(DevForgeState)

    graph_builder.add_node(
        "planning",
        lambda state: nodes.plan_project(
            state,
            agents=agent_set,
            max_debugging_attempts=max_debugging_attempts,
        ),
    )
    graph_builder.add_node(
        "architecture",
        lambda state: nodes.design_architecture(state, agents=agent_set),
    )
    graph_builder.add_node("architecture_approval", nodes.approve_architecture)
    graph_builder.add_node(
        "development",
        lambda state: nodes.develop_next_task(
            state,
            agents=agent_set,
            file_manager=manager,
        ),
    )
    graph_builder.add_node(
        "testing",
        lambda state: nodes.run_tests(state, agents=agent_set),
    )
    graph_builder.add_node(
        "static_analysis",
        lambda state: nodes.analyze_code(state, analyzer=analyzer_instance),
    )
    graph_builder.add_node(
        "review",
        lambda state: nodes.review_code(state, agents=agent_set),
    )
    graph_builder.add_node(
        "debugging",
        lambda state: nodes.debug_project(
            state,
            agents=agent_set,
            file_manager=manager,
        ),
    )
    graph_builder.add_node("complete", nodes.complete_project)
    graph_builder.add_node("failure", nodes.fail_project)

    graph_builder.add_edge(START, "planning")
    graph_builder.add_conditional_edges(
        "planning",
        nodes.route_after_planning,
        {"design": "architecture", "failed": "failure"},
    )
    graph_builder.add_conditional_edges(
        "architecture",
        nodes.route_after_design,
        {"approval": "architecture_approval", "failed": "failure"},
    )
    graph_builder.add_conditional_edges(
        "architecture_approval",
        nodes.route_after_approval,
        {"development": "development", "failed": "failure"},
    )
    graph_builder.add_conditional_edges(
        "development",
        nodes.route_after_development,
        {
            "development": "development",
            "testing": "testing",
            "failed": "failure",
        },
    )
    graph_builder.add_conditional_edges(
        "testing",
        nodes.route_after_testing,
        {"analysis": "static_analysis", "failed": "failure"},
    )
    graph_builder.add_conditional_edges(
        "static_analysis",
        nodes.route_after_analysis,
        {"review": "review", "failed": "failure"},
    )
    graph_builder.add_conditional_edges(
        "review",
        nodes.route_after_review,
        {"complete": "complete", "debug": "debugging", "failed": "failure"},
    )
    graph_builder.add_conditional_edges(
        "debugging",
        nodes.route_after_debug,
        {"testing": "testing", "failed": "failure"},
    )
    graph_builder.add_edge("complete", END)
    graph_builder.add_edge("failure", END)

    return graph_builder.compile(checkpointer=checkpointer)


def _create_default_agents(
    analyzer: CodeAnalyzer,
    max_debugging_attempts: int,
) -> WorkflowAgents:
    """Construct production agents while sharing static-analysis configuration."""
    debugger_attempts = max(1, max_debugging_attempts)
    return WorkflowAgents(
        project_manager=ProjectManagerAgent(),
        architect=ArchitectAgent(),
        developer=DeveloperAgent(),
        tester=TesterAgent(),
        reviewer=CodeReviewerAgent(code_analyzer=analyzer),
        debugger=DebuggerAgent(max_attempts=debugger_attempts),
    )


def workflow_diagram() -> str:
    """Return a concise Mermaid diagram for documentation and user interfaces."""
    return """\
flowchart TD
    START --> PM[Project Manager: validate and topologically order tasks]
    PM -->|valid| ARCH[Architect: design architecture]
    PM -->|failure| FAIL[Failure summary]
    ARCH -->|design ready| APPROVAL{Human architecture approval}
    ARCH -->|failure| FAIL
    APPROVAL -->|approved| DEV[Developer: next ready task, save files]
    APPROVAL -->|rejected| FAIL
    DEV -->|more ready developer tasks| DEV
    DEV -->|all developer tasks complete| TEST[Tester: isolated test run]
    DEV -->|failure or blocked dependency| FAIL
    TEST --> ANALYSIS[Static analysis: AST, Ruff, Radon]
    TEST -->|node failure| FAIL
    ANALYSIS --> REVIEW[Reviewer: tests, findings, requirements]
    ANALYSIS -->|node failure| FAIL
    REVIEW -->|approved| DONE[Final project summary]
    REVIEW -->|not approved and retry budget remains| DEBUG[Debugger: targeted corrections]
    REVIEW -->|retry budget exhausted| FAIL
    DEBUG -->|corrections saved| TEST
    DEBUG -->|failure| FAIL
"""
