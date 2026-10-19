"""Current read projections; no derived workflow states or stored business truth."""

from app.contracts.documents import ProjectDocuments
from app.contracts.management import AttentionItem, ManagementOverview
from app.contracts.notifications import ProjectNotification
from app.contracts.presentation import Coverage, PresentationModel
from app.contracts.projects import ProjectDetails


class ManagementSnapshot(PresentationModel):
    overview: ManagementOverview
    attention_coverage: Coverage
    tasks: list[AttentionItem]
    tasks_coverage: Coverage


class ComparedProject(PresentationModel):
    details: ProjectDetails
    documents: ProjectDocuments | None
    activity: list[ProjectNotification] | None
