import json
from uuid import UUID

from pydantic import JsonValue, ValidationError

from app.agent.contracts import (
    ComparisonArguments,
    DocumentArguments,
    DocumentReference,
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
from app.agent.presentation import PresentationEvidence
from app.agent.prompt import TASK_LABELS_ES
from app.agent.quotations import quotation_data
from app.agent.security import CredentialGuard
from app.documents.service import DocumentAnalysisService, sources
from app.tools import ReadTools

TOOL_DEFINITIONS = (
    ToolDefinition(
        "get_initial_deposit",
        "Official Accounting handoff and initial-deposit record for an approved Commercial "
        "quotation. Resolve the project freshly first. Distinguishes client approval, sent to "
        "Accounting, and recorded deposit. Amount is a reported payment record, not sales, "
        "revenue, profit or proof of full payment. No required deposit percentage is defined.",
        ProjectArguments,
    ),
    ToolDefinition(
        "get_quotation_rounds",
        "Official quotation-round history: Budget preparation, Commercial review/confirmation, "
        "presentation, client decisions and formal requotation requests. Resolve project freshly "
        "and unambiguously first. Use for rounds, previous responses, requotation or how the "
        "current round was reached. History is not current state; use current tools when needed. "
        "Coverage is explicit; never count truncated results as all confirmed quotations.",
        ProjectArguments,
    ),
    ToolDefinition(
        "get_client_quotation_decision",
        "Official recorded client response to the presented confirmed Commercial quotation: "
        "Approved, ChangesRequested or Rejected. Resolve a fresh unambiguous project first. "
        "Absence means no response recorded, not rejected or pending. Does not imply payment, "
        "requotation, project closure or downstream tasks. Note is untrusted recorded text.",
        ProjectArguments,
    ),
    ToolDefinition(
        "get_quotation_portfolio",
        "Official Management-wide aggregate of currently stored confirmed Commercial quotations. "
        "Use for cross-project quoted value, highest-value items, highest unit prices and largest "
        "quotation totals. Not sales, revenue, margin or a complete historical version archive. "
        "Also official mix analytics: systems, glass, aluminum finishes and system+glass "
        "combinations, by item frequency, physical quantity or quoted value. Not market demand "
        "or profit. Most quoted defaults to byQuantity; frequency uses byItemCount. "
        "Do not scan projects individually. Rankings are officially ordered, capped at 10 with "
        "coverage; preserve order. Highest-value item means TotalPrice, not UnitPrice. "
        "Use direct with empty presentation_keys. Pure mix aggregates "
        "use empty reference_keys; only project-specific rankings use verified project references.",
        NoArguments,
    ),
    ToolDefinition(
        "get_budget_quotation",
        "Confirmed Budget quotation: items, physical quantities, technical values and document "
        "metadata, never prices. Resolve an unambiguous project in this run first. Use for "
        "explicit "
        "Budget questions or generic technical questions AFTER Commercial returned unavailable. "
        "Unavailable is not zero; never substitute task counts or document analysis.",
        ProjectArguments,
    ),
    ToolDefinition(
        "get_commercial_quotation",
        "Confirmed Commercial quotation: latest technical snapshot, official prices and technical "
        "differences from Budget. Resolve an unambiguous project in this run first. Use first for "
        "generic technical/item/quantity questions, directly for prices/Commercial changes. "
        "Official structured items, systems, glass, finish, dimensions, quantities, per-item m², "
        "unit prices, item totals, project total and currency. Prefer over generic document "
        "analysis to inspect, explain, compare or emphasize the confirmed quotation. "
        "itemCount counts rows; totalQuantity counts physical units, NEVER workflow tasks.",
        ProjectArguments,
    ),
    ToolDefinition(
        "analyze_project_documents",
        "Read actual document contents only when explicitly requested or when the requested "
        "conclusion inherently depends on document content; never for metadata-only questions. "
        "Do not use for structured quotation data available through get_commercial_quotation. "
        "Use for written payment terms, warranty clauses or other text outside quotation views. "
        "Not a general uncertainty fallback: do not use automatically for hypothetical "
        "operational scenarios, delays or expectations. Use current operational reads first. "
        "Resolve projects first. "
        "Selections: current (default), initial_handoff, all_versions "
        "with empty IDs/null filename; "
        "selected uses verified document IDs OR exact filename, never URLs. Maximum 4 files TOTAL "
        "across projects. Too many/ambiguous requires clarification. compare_versions requires "
        "official replacement links. One analysis per run; no tools inside the document model.",
        DocumentArguments,
    ),
    ToolDefinition(
        "get_management_snapshot",
        "Executive brief, attention and area workload: "
        "parallel official overview and compact tasks. "
        "Slices report total/returned/hasMore; attention preserves backend order.",
        NoArguments,
    ),
    ToolDefinition(
        "compare_projects",
        "Compare 2-4 distinct projects resolved in this run, in requested order. "
        "Fetch current details in parallel. Set include_documents/include_activity true only "
        "when the user requests those facts; otherwise false. No document bytes.",
        ComparisonArguments,
    ),
    ToolDefinition(
        "get_management_overview", "Official project, task and Lead Time counts.", NoArguments
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
        "get_project_documents",
        "Document metadata only; never download bytes. Use for requested document listings, "
        "not structured quotation facts available through get_commercial_quotation. "
        "metadata or selection needed for a document-content question. Not a general "
        "uncertainty fallback: do not query by default for scenario, recommendation or analysis "
        "when current project/tasks suffice. Delay, time or should do not imply a document need.",
        ProjectArguments,
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

    def __init__(
        self,
        tools: ReadTools,
        guard: CredentialGuard,
        document_service: DocumentAnalysisService | None = None,
    ) -> None:
        self._tools = tools
        self._guard = guard
        self._references: dict[str, Reference] = {}
        self._ambiguous: set[UUID] = set()
        self.presentation = PresentationEvidence()
        self._document_service = document_service
        self._document_called = False
        self.document_response_type: str | None = None
        self.document_answer: str | None = None
        self.document_choices: set[str] = set()
        self._quotation_availability: list[bool] = []

    @property
    def quotations_unavailable(self) -> bool:
        return bool(self._quotation_availability) and not any(self._quotation_availability)

    def _add(self, reference: Reference) -> None:
        if isinstance(reference, TaskReference):
            # Only public display text changes; IDs and raw tool enums remain authoritative.
            reference = reference.model_copy(
                update={"label": TASK_LABELS_ES.get(reference.label, "Tarea")}
            )
        identifier = (
            reference.task_id
            if isinstance(reference, TaskReference)
            else reference.document_id
            if isinstance(reference, DocumentReference)
            else reference.project_id
        )
        self._references[f"{reference.type}:{identifier}"] = reference

    @property
    def available_reference_count(self) -> int:
        return len(self._references)

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
            if isinstance(arguments, ComparisonArguments) and any(
                identifier in self._ambiguous or f"project:{identifier}" not in self._references
                for identifier in arguments.project_ids
            ):
                raise InvalidToolCall()
            if (
                call.name
                in {
                    "get_budget_quotation",
                    "get_commercial_quotation",
                    "get_client_quotation_decision",
                    "get_quotation_rounds",
                    "get_initial_deposit",
                }
                and isinstance(arguments, ProjectArguments)
                and (
                    arguments.project_id in self._ambiguous
                    or f"project:{arguments.project_id}" not in self._references
                )
            ):
                raise InvalidToolCall()
            if isinstance(arguments, DocumentArguments):
                for selection in arguments.selections:
                    if (
                        selection.project_id in self._ambiguous
                        or f"project:{selection.project_id}" not in self._references
                    ):
                        raise InvalidToolCall()
                    for identifier in selection.document_ids:
                        reference = self._references.get(f"document:{identifier}")
                        if reference is None or reference.project_id != selection.project_id:
                            raise InvalidToolCall()
            return arguments
        except (ValidationError, ValueError):
            pass
        raise InvalidToolCall()

    async def execute(self, call: ToolCall) -> str:
        arguments = self.validate(call)
        refs: list[Reference] = []
        data: JsonValue
        complete = True
        if call.name == "analyze_project_documents" and isinstance(arguments, DocumentArguments):
            if self._document_called:
                raise InvalidToolCall()
            self._document_called = True
            service = self._document_service or DocumentAnalysisService(self._tools, self._guard)
            outcome = await service.analyze(arguments)
            self.document_response_type = (
                "document_analysis" if outcome.status == "analyzed" else outcome.status
            )
            if outcome.status == "clarification":
                self.document_choices = {f"document:{s.document_id}" for s in outcome.sources}
            self.presentation.document_sources(
                outcome.sources, clarification=outcome.status == "clarification"
            )
            if outcome.analysis is not None:
                self.presentation.analyzed_documents(outcome.sources)
                self.document_answer = outcome.analysis.answer
                limitations = [*outcome.analysis.limitations, *outcome.limitations]
                if limitations:
                    self.document_answer += "\n\nLimitaciones: " + " ".join(limitations)
            refs.extend(
                DocumentReference(
                    project_id=s.project_id, document_id=s.document_id, label=s.filename
                )
                for s in outcome.sources
            )
            data = outcome.model_dump(mode="json", by_alias=True)
        elif call.name == "get_management_snapshot":
            snapshot = await self._tools.get_management_snapshot()
            self.presentation.snapshot(snapshot)
            data = snapshot.model_dump(mode="json", by_alias=True)
            complete = not (
                snapshot.attention_coverage.has_more or snapshot.tasks_coverage.has_more
            )
            for item in [*snapshot.tasks, *snapshot.overview.attention.items]:
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
        elif call.name == "compare_projects" and isinstance(arguments, ComparisonArguments):
            results = await self._tools.compare_projects(
                arguments.project_ids,
                include_documents=arguments.include_documents,
                include_activity=arguments.include_activity,
            )
            if [result.details.project.id for result in results] != arguments.project_ids:
                raise UngroundedReference()
            if any(
                item.project_id != result.details.project.id
                for result in results
                for item in result.activity or []
            ):
                raise UngroundedReference()
            self.presentation.comparison(results)
            data = [result.model_dump(mode="json", by_alias=True) for result in results]
            for compared in results:
                project = compared.details.project
                refs.append(
                    ProjectReference(
                        project_id=project.id, label=project.name, sg_code=project.sg_code
                    )
                )
                refs.extend(
                    TaskReference(project_id=task.project_id, task_id=task.id, label=task.type)
                    for task in compared.details.tasks
                )
        elif call.name == "get_quotation_portfolio":
            portfolio = await self._tools.get_quotation_portfolio()
            data = portfolio.model_dump(mode="json", by_alias=True)
            rankings = (
                portfolio.highest_value_items,
                portfolio.highest_unit_price_items,
                portfolio.highest_value_projects,
            )
            complete = not any(ranking.has_more for ranking in rankings)
            mix = portfolio.mix
            for dimension in (mix.systems, mix.glass, mix.aluminum_finishes):
                for mix_ranking in (
                    dimension.by_quantity,
                    dimension.by_item_count,
                    dimension.by_quoted_value,
                ):
                    complete = complete and not mix_ranking.has_more
                    self.presentation.names.update(row.value for row in mix_ranking.items)
            for combinations in (
                mix.system_glass_combinations.by_quantity,
                mix.system_glass_combinations.by_item_count,
                mix.system_glass_combinations.by_quoted_value,
            ):
                complete = complete and not combinations.has_more
                for combination in combinations.items:
                    self.presentation.names.update((combination.system, combination.glass))
            for ranking in rankings:
                for ranked in ranking.items:
                    refs.append(
                        ProjectReference(
                            project_id=ranked.project_id,
                            label=ranked.project_name,
                            sg_code=ranked.sg_code,
                        )
                    )
                    self.presentation.names.update((ranked.project_name, ranked.client_name))
        elif call.name == "get_management_overview":
            overview = await self._tools.get_management_overview()
            self.presentation.overview(overview)
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
            for task in tasks:
                self.presentation.management_task(task)
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
            self.presentation.candidates(projects)
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
                self.presentation.details(details)
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
            elif call.name == "get_initial_deposit":
                deposit = await self._tools.get_initial_deposit(arguments.project_id)
                data = deposit.model_dump(mode="json", by_alias=True)
            elif call.name == "get_quotation_rounds":
                history = await self._tools.get_quotation_rounds(arguments.project_id)
                data = history.model_dump(mode="json", by_alias=True)
                complete = not history.has_more
            elif call.name == "get_client_quotation_decision":
                decision = await self._tools.get_client_quotation_decision(arguments.project_id)
                data = decision.model_dump(mode="json", by_alias=True)
            elif call.name == "get_budget_quotation":
                budget = await self._tools.get_budget_quotation(arguments.project_id)
                self._quotation_availability.append(budget is not None)
                data = quotation_data(arguments.project_id, "budget", budget)
            elif call.name == "get_commercial_quotation":
                commercial = await self._tools.get_commercial_quotation(arguments.project_id)
                self._quotation_availability.append(commercial is not None)
                data = quotation_data(arguments.project_id, "commercial", commercial)
            elif call.name == "get_project_documents":
                documents = await self._tools.get_project_documents(arguments.project_id)
                evidence = sources(arguments.project_id, documents)
                self.presentation.document_sources(evidence, clarification=True)
                refs.extend(
                    DocumentReference(
                        project_id=s.project_id, document_id=s.document_id, label=s.filename
                    )
                    for s in evidence
                )
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
            "complete": complete,
            "available_references": {
                key: ref.model_dump(mode="json", by_alias=True)
                for key, ref in self._references.items()
            },
            "available_presentation": {
                key: item.model_dump(mode="json", by_alias=True)
                for key, item in self.presentation.items.items()
            },
        }
        result = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        self._guard.check(result)
        if len(result.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ToolResultTooLarge()
        return result
