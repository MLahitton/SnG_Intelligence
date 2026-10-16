import json
from uuid import UUID

from pydantic import JsonValue, ValidationError

from app.agent.contracts import (
    NoArguments,
    ProjectArguments,
    ProjectReference,
    Reference,
    SearchArguments,
    StrictModel,
    TaskReference,
    ToolCall,
    ToolDefinition,
)
from app.agent.errors import InvalidToolCall, ToolResultTooLarge, UngroundedReference
from app.agent.security import CredentialGuard
from app.tools import ReadTools

TOOL_DEFINITIONS = (
    ToolDefinition(
        "get_management_overview", "Official project, task and Leak Time counts.", NoArguments
    ),
    ToolDefinition(
        "list_management_tasks", "All official management tasks, in backend order.", NoArguments
    ),
    ToolDefinition(
        "find_projects",
        "Find all matching projects by name, SG, city or available client name.",
        SearchArguments,
    ),
    ToolDefinition(
        "get_project_details", "Official project and tasks for a UUID.", ProjectArguments
    ),
    ToolDefinition(
        "get_project_documents", "Document metadata only; never download bytes.", ProjectArguments
    ),
    ToolDefinition(
        "get_project_activity",
        "Official notifications/history for a project UUID.",
        ProjectArguments,
    ),
)
MAX_RESULT_BYTES = 48_000
# Identity/permission metadata is unnecessary for model reasoning in 1B.
_PRIVATE_FIELDS = {
    "createdBy",
    "assignedToGoogleSubject",
    "recipientGoogleSubject",
    "uploadedBy",
    "sentBy",
    "canModify",
    "canRemove",
    "canSendToBudgets",
    "clientId",
}


def _project_data(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {k: _project_data(v) for k, v in value.items() if k not in _PRIVATE_FIELDS}
    if isinstance(value, list):
        return [_project_data(v) for v in value]
    return value


class ToolBox:
    """Closed dispatcher and run-local evidence ledger. Never supplied to a provider."""

    def __init__(self, tools: ReadTools, guard: CredentialGuard) -> None:
        self._tools = tools
        self._guard = guard
        self._references: dict[str, Reference] = {}
        self._ambiguous: set[UUID] = set()

    def _add(self, reference: Reference) -> None:
        identifier = (
            reference.task_id if isinstance(reference, TaskReference) else reference.project_id
        )
        self._references[f"{reference.type}:{identifier}"] = reference

    def references(self, keys: list[str]) -> list[Reference]:
        if any(key not in self._references for key in keys):
            raise UngroundedReference()
        return [self._references[key] for key in dict.fromkeys(keys)]

    def validate(self, call: ToolCall) -> StrictModel:
        definition = next((tool for tool in TOOL_DEFINITIONS if tool.name == call.name), None)
        if definition is None or len(call.arguments) > 4096:
            raise InvalidToolCall()
        self._guard.check(call.arguments)
        try:
            arguments = definition.arguments.model_validate_json(call.arguments)
            if isinstance(arguments, SearchArguments) and not arguments.query.strip():
                raise ValueError("Blank search")
            return arguments
        except (ValidationError, ValueError):
            pass
        raise InvalidToolCall()

    async def execute(self, call: ToolCall) -> str:
        arguments = self.validate(call)
        refs: list[Reference] = []
        data: JsonValue
        if call.name == "get_management_overview":
            overview = await self._tools.get_management_overview()
            data = overview.model_dump(mode="json", by_alias=True)
            for item in overview.attention.items:
                refs.extend(
                    [
                        ProjectReference(
                            project_id=item.project_id,
                            label=item.project_name,
                            sg_code=item.sg_code,
                        ),
                        TaskReference(
                            project_id=item.project_id, task_id=item.task_id, label=item.task_type
                        ),
                    ]
                )
        elif call.name == "list_management_tasks":
            tasks = await self._tools.list_management_tasks()
            data = [task.model_dump(mode="json", by_alias=True) for task in tasks]
            for task in tasks:
                refs.extend(
                    [
                        ProjectReference(
                            project_id=task.project_id,
                            label=task.project_name,
                            sg_code=task.sg_code,
                        ),
                        TaskReference(
                            project_id=task.project_id, task_id=task.task_id, label=task.type
                        ),
                    ]
                )
        elif call.name == "find_projects" and isinstance(arguments, SearchArguments):
            projects = await self._tools.find_projects(arguments.query)
            data = {
                "candidates": [p.model_dump(mode="json", by_alias=True) for p in projects],
                "ambiguous": len(projects) > 1,
            }
            if len(projects) > 1:
                self._ambiguous.update(p.project_id for p in projects)
            refs.extend(
                ProjectReference(project_id=p.project_id, label=p.project_name, sg_code=p.sg_code)
                for p in projects
            )
        elif isinstance(arguments, ProjectArguments):
            if arguments.project_id in self._ambiguous:
                raise InvalidToolCall()  # No arbitrary selection from an ambiguous lookup.
            if call.name == "get_project_details":
                details = await self._tools.get_project_details(arguments.project_id)
                data = details.model_dump(mode="json", by_alias=True)
                refs.append(
                    ProjectReference(
                        project_id=details.project.id,
                        label=details.project.name,
                        sg_code=details.project.sg_code,
                    )
                )
                refs.extend(
                    TaskReference(project_id=t.project_id, task_id=t.id, label=t.type)
                    for t in details.tasks
                )
            elif call.name == "get_project_documents":
                documents = await self._tools.get_project_documents(arguments.project_id)
                data = documents.model_dump(mode="json", by_alias=True)
            elif call.name == "get_project_activity":
                activity = await self._tools.get_project_activity(arguments.project_id)
                data = [item.model_dump(mode="json", by_alias=True) for item in activity]
            else:
                raise InvalidToolCall()
        else:
            raise InvalidToolCall()
        for reference in refs:
            self._add(reference)
        envelope = {
            "data": _project_data(data),
            "complete": True,
            "available_references": {
                key: ref.model_dump(mode="json", by_alias=True)
                for key, ref in self._references.items()
            },
        }
        result = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        self._guard.check(result)
        if len(result.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ToolResultTooLarge()
        return result
