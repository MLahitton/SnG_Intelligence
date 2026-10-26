"""Run-local presentation evidence. Model output can select keys, never supply facts."""

from app.agent.errors import UngroundedReference
from app.agent.prompt import MANAGEMENT_LABELS_ES
from app.contracts.analysis import ComparedProject, ManagementSnapshot
from app.contracts.management import AttentionItem, ManagementOverview, ManagementTask
from app.contracts.presentation import (
    AreaItem,
    BriefPresentation,
    ClarificationPresentation,
    ComparedProjectItem,
    ComparisonPresentation,
    Coverage,
    DirectPresentation,
    DocumentAnalysisPresentation,
    DocumentSource,
    ListPresentation,
    Metric,
    Presentation,
    ProjectItem,
    ProjectPresentation,
    ResponseType,
    TaskItem,
    UnavailablePresentation,
)
from app.contracts.projects import ProjectCandidate, ProjectDetails, ProjectTask

AREA_LABELS_ES = {"Budgets": "Presupuestos", "Commercial": "Comercial"}


class PresentationEvidence:
    def __init__(self) -> None:
        self.items: dict[str, ProjectItem | TaskItem | Metric | AreaItem | DocumentSource] = {}
        self.names: set[str] = set()
        self._details: dict[str, list[TaskItem]] = {}
        self._candidate_groups: list[set[str]] = []
        self._comparisons: list[list[str]] = []
        self._compared: dict[str, ComparedProjectItem] = {}
        self._attention_order: list[str] = []
        self._attention_coverage: Coverage | None = None
        self._tasks_coverage: Coverage | None = None
        self._document_choices: list[set[str]] = []
        self._analysis_sources: list[str] = []
        self._analysis_projects: list[str] = []

    def document_sources(
        self, sources: list[DocumentSource], *, clarification: bool = False
    ) -> None:
        keys = set()
        for source in sources:
            key = f"document:{source.document_id}"
            previous = self.items.get(key)
            if isinstance(previous, DocumentSource) and previous.project_id != source.project_id:
                raise UngroundedReference()
            # Later metadata reads must not silently relabel the bytes already analyzed.
            if key in self._analysis_sources and previous != source:
                raise UngroundedReference()
            self.items[key] = source
            self.names.add(source.filename)
            keys.add(key)
        if clarification:
            self._document_choices.append(keys)

    def analyzed_documents(self, sources: list[DocumentSource]) -> None:
        self.document_sources(sources)
        self._analysis_sources = [f"document:{source.document_id}" for source in sources]
        self._analysis_projects = list(dict.fromkeys(f"project:{s.project_id}" for s in sources))

    def project(self, item: ProjectItem) -> None:
        self.items[f"project:{item.project_id}"] = item
        self.names.update(n for n in (item.project_name, item.client_name, item.city) if n)

    def task(
        self, task: ManagementTask | AttentionItem | ProjectTask, project: ProjectItem
    ) -> None:
        if isinstance(task, AttentionItem):
            identifier, task_type, status = task.task_id, task.task_type, task.task_status
        else:
            identifier = task.id if isinstance(task, ProjectTask) else task.task_id
            task_type, status = task.type, task.status
        self.items[f"task:{identifier}"] = TaskItem(
            project_id=project.project_id,
            project_name=project.project_name,
            sg_code=project.sg_code,
            task_id=identifier,
            area=AREA_LABELS_ES.get(task.area),
            task=MANAGEMENT_LABELS_ES.get(task_type),
            status=MANAGEMENT_LABELS_ES.get(status),
            lead_time_status=MANAGEMENT_LABELS_ES.get(task.lead_time_status),
            deadline_at=task.deadline_at,
        )

    def management_task(self, task: ManagementTask | AttentionItem) -> None:
        project = ProjectItem(
            project_id=task.project_id,
            project_name=task.project_name,
            sg_code=task.sg_code,
            client_name=task.client_name,
            city=task.city if isinstance(task, ManagementTask) else None,
        )
        self.project(project)
        self.task(task, project)

    def overview(self, overview: ManagementOverview) -> None:
        self._attention_order = [f"task:{task.task_id}" for task in overview.attention.items]
        counts = {
            "total_projects": ("Proyectos", overview.total_projects),
            "tasks.pending": ("Tareas pendientes", overview.tasks.pending),
            "tasks.in_progress": ("Tareas en curso", overview.tasks.in_progress),
            "tasks.completed": ("Tareas completadas", overview.tasks.completed),
            "attention.total": ("Tareas que requieren atención", overview.attention.total),
        }
        for field, internal in (
            ("not_started", "NotStarted"),
            ("on_track", "OnTrack"),
            ("due_soon", "DueSoon"),
            ("due_today", "DueToday"),
            ("overdue", "Overdue"),
            ("completed_on_time", "CompletedOnTime"),
            ("completed_late", "CompletedLate"),
            ("untracked_legacy", "UntrackedLegacy"),
        ):
            counts[f"lead_time.{field}"] = (
                MANAGEMENT_LABELS_ES[internal],
                getattr(overview.lead_time, field),
            )
        for key, (label, value) in counts.items():
            self.items[f"metric:{key}"] = Metric(key=key, label=label, value=value)
        for index, area in enumerate(overview.areas):
            self.items[f"area:{index}"] = AreaItem(
                area=AREA_LABELS_ES.get(area.area),
                pending=area.pending,
                in_progress=area.in_progress,
                completed=area.completed,
                overdue=area.overdue,
                due_today=area.due_today,
                due_soon=area.due_soon,
            )
        for task in overview.attention.items:
            self.management_task(task)

    def snapshot(self, snapshot: ManagementSnapshot) -> None:
        for task in snapshot.tasks:
            self.management_task(task)
        # Prefer official attention projection if an item also appears in tasks.
        self.overview(snapshot.overview)
        self._attention_coverage = snapshot.attention_coverage
        self._tasks_coverage = snapshot.tasks_coverage

    def comparison(self, results: list[ComparedProject]) -> None:
        group = []
        for result in results:
            self.details(result.details)
            key = f"project:{result.details.project.id}"
            project = self.items[key]
            assert isinstance(project, ProjectItem)
            tasks = result.details.tasks
            counts = {"tasks.total": ("Tareas actuales", len(tasks))}
            for status in ("Pending", "InProgress", "Completed"):
                counts[f"tasks.{status}"] = (
                    MANAGEMENT_LABELS_ES[status],
                    sum(task.status == status for task in tasks),
                )
            for status in ("Overdue", "DueToday", "DueSoon", "NotStarted", "OnTrack"):
                counts[f"deadlines.{status}"] = (
                    MANAGEMENT_LABELS_ES[status],
                    sum(task.lead_time_status == status for task in tasks),
                )
            if result.documents is not None:
                counts["documents.returned"] = (
                    "Documentos devueltos",
                    len(result.documents.documents),
                )
                counts["documents.current"] = (
                    "Documentos vigentes devueltos",
                    sum(document.is_current for document in result.documents.documents),
                )
            if result.activity is not None:
                counts["activity.returned"] = ("Avisos devueltos", len(result.activity))
            self._compared[key] = ComparedProjectItem(
                project=project,
                metrics=[
                    Metric(key=k, label=label, value=value) for k, (label, value) in counts.items()
                ],
                tasks=self._details[key],
            )
            group.append(key)
        self._comparisons.append(group)

    def candidates(self, candidates: list[ProjectCandidate]) -> None:
        group = set()
        for candidate in candidates:
            self.project(ProjectItem(**candidate.model_dump()))
            group.add(f"project:{candidate.project_id}")
        if len(group) > 1:
            self._candidate_groups.append(group)

    def details(self, details: ProjectDetails) -> None:
        p = details.project
        project = ProjectItem(
            project_id=p.id,
            project_name=p.name,
            sg_code=p.sg_code,
            city=p.city,
            client_name=None,
        )
        self.project(project)
        tasks = []
        for task in details.tasks:
            # A mismatched association must never become presentation authority.
            if task.project_id != p.id:
                raise UngroundedReference()
            self.task(task, project)
            item = self.items[f"task:{task.id}"]
            assert isinstance(item, TaskItem)
            tasks.append(item)
        self._details[f"project:{p.id}"] = tasks

    def build(self, response_type: ResponseType, keys: list[str]) -> Presentation:
        keys = list(dict.fromkeys(keys))
        if any(key not in self.items for key in keys):
            raise UngroundedReference()
        items = [self.items[key] for key in keys]
        projects = [item for item in items if isinstance(item, ProjectItem)]
        entities = [
            item for item in items if isinstance(item, (ProjectItem, TaskItem, DocumentSource))
        ]
        documents = [item for item in items if isinstance(item, DocumentSource)]
        metrics = [item for item in items if isinstance(item, Metric)]
        areas = [item for item in items if isinstance(item, AreaItem)]
        match response_type:
            case "document_analysis" if self._analysis_sources and set(keys) == set(
                self._analysis_sources
            ):
                selected_projects = [self.items.get(key) for key in self._analysis_projects]
                if not all(isinstance(project, ProjectItem) for project in selected_projects):
                    raise UngroundedReference()
                verified = [p for p in selected_projects if isinstance(p, ProjectItem)]
                return DocumentAnalysisPresentation(
                    project=verified[0],
                    related_projects=verified[1:],
                    sources=[
                        s
                        for key in self._analysis_sources
                        if isinstance(s := self.items[key], DocumentSource)
                    ],
                )
            case "clarification" if documents and len(items) == len(documents):
                if set(keys) not in self._document_choices:
                    raise UngroundedReference()
                return ClarificationPresentation(candidates=[], documents=documents)
            case "direct" if len(items) == len(metrics) <= 1:
                return DirectPresentation(metric=metrics[0] if metrics else None)
            case "unavailable" if not items:
                return UnavailablePresentation()
            case "management_brief" if len(items) == len(metrics) + len(entities) + len(areas):
                attention = [self.items[key] for key in self._attention_order if key in keys]
                ordered = [item for item in attention if isinstance(item, TaskItem)]
                return BriefPresentation(
                    metrics=metrics,
                    items=[*ordered, *(item for item in entities if item not in ordered)],
                    areas=areas,
                    attention_coverage=self._attention_coverage,
                    tasks_coverage=self._tasks_coverage,
                )
            case "list" if len(items) == len(entities):
                return ListPresentation(items=entities)
            case "comparison" if items and len(items) == len(areas):
                return ComparisonPresentation(areas=areas)
            case "comparison" if len(items) == len(projects) and 2 <= len(projects) <= 4:
                group = next((g for g in reversed(self._comparisons) if set(g) == set(keys)), None)
                if group is None or any(
                    key in candidates for key in group for candidates in self._candidate_groups
                ):
                    raise UngroundedReference()
                return ComparisonPresentation(projects=[self._compared[key] for key in group])
            case "clarification" if len(items) == len(projects):
                if items and set(keys) not in self._candidate_groups:
                    raise UngroundedReference()
                return ClarificationPresentation(candidates=projects)
            case "project_status" if len(items) == len(projects) == 1:
                if keys[0] not in self._details or any(
                    keys[0] in group for group in self._candidate_groups
                ):
                    raise UngroundedReference()
                return ProjectPresentation(project=projects[0], tasks=self._details[keys[0]])
        raise UngroundedReference()
