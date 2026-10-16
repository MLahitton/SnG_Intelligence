"""Run-local presentation evidence. Model output can select keys, never supply facts."""

from app.agent.errors import UngroundedReference
from app.agent.prompt import MANAGEMENT_LABELS_ES
from app.contracts.management import AttentionItem, ManagementOverview, ManagementTask
from app.contracts.presentation import (
    AreaItem,
    BriefPresentation,
    ClarificationPresentation,
    ComparisonPresentation,
    DirectPresentation,
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
        self.items: dict[str, ProjectItem | TaskItem | Metric | AreaItem] = {}
        self.names: set[str] = set()
        self._details: dict[str, list[TaskItem]] = {}
        self._candidate_groups: list[set[str]] = []

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
            leak_time_status=MANAGEMENT_LABELS_ES.get(task.leak_time_status),
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
            counts[f"leak_time.{field}"] = (
                MANAGEMENT_LABELS_ES[internal],
                getattr(overview.leak_time, field),
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
        entities = [item for item in items if isinstance(item, (ProjectItem, TaskItem))]
        metrics = [item for item in items if isinstance(item, Metric)]
        areas = [item for item in items if isinstance(item, AreaItem)]
        match response_type:
            case "direct" if len(items) == len(metrics) <= 1:
                return DirectPresentation(metric=metrics[0] if metrics else None)
            case "unavailable" if not items:
                return UnavailablePresentation()
            case "management_brief" if len(items) == len(metrics) + len(entities):
                return BriefPresentation(metrics=metrics, items=entities)
            case "list" if len(items) == len(entities):
                return ListPresentation(items=entities)
            case "comparison" if items and len(items) == len(areas):
                return ComparisonPresentation(areas=areas)
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
