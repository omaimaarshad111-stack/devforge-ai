"""Agent that turns project requests into dependency-ordered development plans."""

import json
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from app.agents.base_agent import (
    AgentExecutionError,
    AgentInput,
    AgentOutput,
    BaseAgent,
    LLMClient,
)
from app.llm import create_groq_chat_model
from app.models.schemas import DevelopmentTask, ProjectRequest, TaskStatus

_VALID_AGENT_ASSIGNMENTS = frozenset(
    {"project_manager", "architect", "developer", "tester", "reviewer", "debugger"}
)

_PROJECT_MANAGER_SYSTEM_PROMPT = """\
You are DevForge AI's project manager. Convert the supplied project request into
an actionable development plan. Do not write source code and do not claim to
execute tools.

Return only a JSON object with a "tasks" array. Each task must contain:
"task_id" (unique, stable string), "title", "description", "assigned_agent",
"dependencies" (task IDs), "priority" (low, medium, high, or critical), and
"status" ("pending"). Assign work only to these agents: project_manager,
architect, developer, tester, reviewer, debugger. Create dependencies only
when a task genuinely relies on another task. For incomplete or ambiguous
requirements, make assumptions explicit in task descriptions and add a task to
clarify important unknowns rather than silently inventing requirements.
"""


class ProjectPlanError(AgentExecutionError):
    """Raised when an LLM plan is invalid or cannot be ordered safely."""


class ProjectManagerAgent(BaseAgent):
    """Decompose a validated project request into a safe, ordered task plan.

    ``plan`` returns the structured task list. The BaseAgent ``execute``
    interface accepts an ``AgentInput`` containing a serialized
    ``project_request`` in its context and returns the plan as JSON content.
    """

    def __init__(self, llm: LLMClient | None = None) -> None:
        super().__init__(
            agent_name="Project Manager",
            role="Analyze project requests and produce development plans",
            llm=llm if llm is not None else create_groq_chat_model(),
            system_prompt=_PROJECT_MANAGER_SYSTEM_PROMPT,
        )

    def execute(self, request: AgentInput) -> AgentOutput:
        """Implement BaseAgent's common interface for project planning."""
        raw_request = request.context.get("project_request")
        if not isinstance(raw_request, Mapping):
            raise ProjectPlanError(
                "Project Manager requires a 'project_request' object in the input context."
            )
        try:
            project_request = ProjectRequest.model_validate(raw_request)
        except ValidationError as error:
            raise ProjectPlanError(
                "Project Manager received an invalid project request."
            ) from error

        tasks = self.plan(project_request)
        serialized_tasks = json.dumps(
            [task.model_dump(mode="json") for task in tasks],
            indent=2,
        )
        return AgentOutput(agent_name=self.agent_name, content=serialized_tasks)

    def plan(self, request: ProjectRequest) -> list[DevelopmentTask]:
        """Ask the LLM for a plan, validate it, and return topologically ordered tasks."""
        agent_request = AgentInput(
            task=(
                "Create a structured development plan for this project. "
                "Use the project description even if its requirements list is empty."
            ),
            context={"project_request": request.model_dump(mode="json")},
        )
        response = self.run_llm(agent_request)
        return self._parse_and_order_tasks(response.content)

    @staticmethod
    def _parse_and_order_tasks(content: str) -> list[DevelopmentTask]:
        """Parse task JSON and reject malformed assignments or dependency graphs."""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise ProjectPlanError("Project Manager returned invalid JSON.") from error

        if isinstance(payload, Mapping):
            raw_tasks = payload.get("tasks")
        else:
            raw_tasks = payload
        if not isinstance(raw_tasks, list):
            raise ProjectPlanError(
                'Project Manager response must contain a "tasks" list.'
            )
        if not raw_tasks:
            raise ProjectPlanError(
                "Project Manager returned no tasks; provide a request with enough "
                "detail to plan, or clarify its requirements."
            )

        try:
            tasks = [DevelopmentTask.model_validate(item) for item in raw_tasks]
        except ValidationError as error:
            raise ProjectPlanError(
                "Project Manager returned a task with invalid or missing fields."
            ) from error

        task_ids = [task.task_id for task in tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ProjectPlanError("Project Manager returned duplicate task IDs.")

        task_id_set = set(task_ids)
        task_by_id = {task.task_id: task for task in tasks}
        for task in tasks:
            if task.assigned_agent not in _VALID_AGENT_ASSIGNMENTS:
                raise ProjectPlanError(
                    f"Task '{task.task_id}' has invalid agent assignment "
                    f"'{task.assigned_agent}'."
                )
            missing = set(task.dependencies) - task_id_set
            if missing:
                missing_ids = ", ".join(sorted(missing))
                raise ProjectPlanError(
                    f"Task '{task.task_id}' depends on missing task(s): {missing_ids}."
                )
            if task.task_id in task.dependencies:
                raise ProjectPlanError(
                    f"Task '{task.task_id}' cannot depend on itself."
                )

        return ProjectManagerAgent._topological_sort(tasks, task_by_id)

    @staticmethod
    def _topological_sort(
        tasks: Sequence[DevelopmentTask],
        task_by_id: Mapping[str, DevelopmentTask],
    ) -> list[DevelopmentTask]:
        """Use Kahn's algorithm to order dependencies before dependent tasks."""
        indegree = {task.task_id: len(task.dependencies) for task in tasks}
        dependents: dict[str, list[str]] = defaultdict(list)
        for task in tasks:
            for dependency in task.dependencies:
                dependents[dependency].append(task.task_id)

        ready = deque(task.task_id for task in tasks if indegree[task.task_id] == 0)
        ordered_ids: list[str] = []
        while ready:
            completed_id = ready.popleft()
            ordered_ids.append(completed_id)
            for dependent_id in dependents[completed_id]:
                indegree[dependent_id] -= 1
                if indegree[dependent_id] == 0:
                    ready.append(dependent_id)

        if len(ordered_ids) != len(tasks):
            cyclic_ids = sorted(
                task_id for task_id, remaining in indegree.items() if remaining > 0
            )
            raise ProjectPlanError(
                "Project Manager returned circular task dependencies involving: "
                f"{', '.join(cyclic_ids)}."
            )

        return [task_by_id[task_id] for task_id in ordered_ids]

    @staticmethod
    def get_ready_tasks(
        tasks: Sequence[DevelopmentTask],
    ) -> list[DevelopmentTask]:
        """Return pending tasks whose dependencies have all been completed."""
        task_by_id = {task.task_id: task for task in tasks}
        ready: list[DevelopmentTask] = []
        for task in tasks:
            if task.status is not TaskStatus.PENDING:
                continue
            missing = set(task.dependencies) - task_by_id.keys()
            if missing:
                raise ProjectPlanError(
                    f"Task '{task.task_id}' depends on missing task(s): "
                    f"{', '.join(sorted(missing))}."
                )
            if all(
                task_by_id[dependency].status is TaskStatus.COMPLETED
                for dependency in task.dependencies
            ):
                ready.append(task)
        return ready
