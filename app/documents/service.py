import asyncio
import json
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.agent.contracts import DocumentArguments
from app.agent.errors import UngroundedReference
from app.agent.security import CredentialGuard
from app.contracts.documents import Document, ProjectDocuments
from app.contracts.presentation import DocumentSource, PresentationModel
from app.conversation.context import check_context_text
from app.core.latency import count, measure
from app.documents.config import MAX_DOCUMENTS_PER_ANALYSIS, DocumentSettings
from app.documents.download import DownloadedDocument
from app.documents.errors import (
    DocumentTooLarge,
    DocumentUnavailable,
    InvalidDocumentResponse,
    UnsupportedDocument,
)
from app.documents.formats import MIME_TYPES, SPREADSHEETS, extension, validate_bytes
from app.documents.provider import DocumentAnalysisResult, DocumentModel, configured_document_model
from app.tools import ReadTools


def sources(project_id: UUID, metadata: ProjectDocuments) -> list[DocumentSource]:
    initial = set(metadata.initial_handoff.document_ids) if metadata.initial_handoff else set()
    return [
        DocumentSource(
            document_id=d.id,
            project_id=project_id,
            filename=d.original_file_name,
            size_bytes=d.size_bytes,
            uploaded_at=d.uploaded_at,
            is_current=d.is_current,
            in_initial_handoff=d.id in initial,
            replaced_by_document_id=d.replaced_by_document_id,
        )
        for d in metadata.documents
    ]


class DocumentOutcome(PresentationModel):
    status: Literal["analyzed", "clarification", "unavailable"]
    reason: str | None = None
    sources: list[DocumentSource]
    analysis: DocumentAnalysisResult | None = None
    # Server-owned provider-format limitations, separate from model-authored prose.
    limitations: list[str] = Field(default_factory=list)


class DocumentAnalysisService:
    def __init__(
        self,
        tools: ReadTools,
        guard: CredentialGuard,
        *,
        settings: DocumentSettings | None = None,
        model_factory: Callable[
            [DocumentSettings], AbstractAsyncContextManager[DocumentModel]
        ] = configured_document_model,
    ) -> None:
        self._tools, self._guard = tools, guard
        self._settings, self._model_factory = settings, model_factory

    async def analyze(self, arguments: DocumentArguments) -> DocumentOutcome:
        self._guard.check(arguments.question)
        check_context_text(arguments.question)
        with measure("document_analysis_total_ms"):
            return await self._analyze(arguments)

    async def _analyze(self, arguments: DocumentArguments) -> DocumentOutcome:
        selected: list[tuple[UUID, Document, DocumentSource]] = []
        for selection in arguments.selections:
            metadata = await self._tools.get_project_documents(selection.project_id)
            self._guard.check(metadata.model_dump_json())
            evidence = sources(selection.project_id, metadata)
            available = {d.id: d for d in metadata.documents}
            initial = (
                set(metadata.initial_handoff.document_ids) if metadata.initial_handoff else set()
            )
            if selection.scope == "current":
                chosen = [d for d in metadata.documents if d.is_current]
            elif selection.scope == "initial_handoff":
                if not initial.issubset(available):
                    return DocumentOutcome(
                        status="unavailable",
                        reason="document_unavailable_or_changed",
                        sources=evidence,
                    )
                chosen = [d for d in metadata.documents if d.id in initial]
            elif selection.scope == "all_versions":
                chosen = list(metadata.documents)
            elif selection.filename is not None:
                chosen = [
                    d
                    for d in metadata.documents
                    if d.original_file_name.casefold() == selection.filename.casefold()
                ]
                if len(chosen) > 1:
                    return DocumentOutcome(
                        status="clarification",
                        reason="ambiguous_document_name",
                        sources=[s for s in evidence if s.document_id in {d.id for d in chosen}],
                    )
            else:
                if any(identifier not in available for identifier in selection.document_ids):
                    raise UngroundedReference()
                chosen = [available[identifier] for identifier in selection.document_ids]
            if not chosen:
                return DocumentOutcome(
                    status="unavailable", reason="no_matching_documents", sources=evidence
                )
            by_id = {s.document_id: s for s in evidence}
            selected.extend((selection.project_id, d, by_id[d.id]) for d in chosen)
        evidence = [source for _, _, source in selected]
        if len(selected) > MAX_DOCUMENTS_PER_ANALYSIS:
            return DocumentOutcome(
                status="clarification", reason="too_many_documents_max_4", sources=evidence
            )
        if arguments.compare_versions:
            identifiers = {d.id for _, d, _ in selected}
            edges = [
                (d.id, d.replaced_by_document_id)
                for _, d, _ in selected
                if d.replaced_by_document_id in identifiers
            ]
            connected = {selected[0][1].id}
            for _ in selected:
                for old, new in edges:
                    if old in connected or new in connected:
                        connected.update((old, new))
            if (
                len({p for p, _, _ in selected}) != 1
                or len(selected) < 2
                or connected != identifiers
            ):
                return DocumentOutcome(
                    status="clarification",
                    reason="unverified_version_relationship",
                    sources=evidence,
                )
        settings = self._settings or DocumentSettings.from_env()
        files: list[DownloadedDocument] = []
        try:
            suffixes = [extension(d.original_file_name, d.content_type) for _, d, _ in selected]
            if (
                any(d.size_bytes > settings.max_file_bytes for _, d, _ in selected)
                or sum(d.size_bytes for _, d, _ in selected) > settings.max_total_bytes
            ):
                raise DocumentTooLarge()
            total = 0
            for (project_id, document, _), suffix in zip(selected, suffixes, strict=True):
                downloaded = await self._tools._download_document(
                    project_id,
                    document,
                    min(settings.max_file_bytes, settings.max_total_bytes - total),
                )
                files.append(downloaded)
                total += downloaded.size_bytes
                self._guard.check_bytes(downloaded.content)
                # Directory/signature checks run outside the event loop. No extraction.
                validation = asyncio.create_task(
                    asyncio.to_thread(validate_bytes, suffix, downloaded.content)
                )
                try:
                    await asyncio.shield(validation)
                finally:
                    # A cancelled request must wait for the bounded signature check to
                    # release its byte reference before closing the document buffer.
                    await validation
                downloaded.content_type = MIME_TYPES[suffix]
            # Detect removal/replacement/metadata changes during the download window.
            for project_id in dict.fromkeys(p for p, _, _ in selected):
                latest = sources(project_id, await self._tools.get_project_documents(project_id))
                if any(source not in latest for p, _, source in selected if p == project_id):
                    raise DocumentUnavailable()
            count("documents_count", len(files))
            count("total_bytes", total)
            catalog = json.dumps(
                [
                    {
                        "key": f"document:{source.document_id}",
                        "filename": f"document-{i + 1}{suffixes[i]}",
                        "isCurrent": source.is_current,
                        "inInitialHandoff": source.in_initial_handoff,
                        "uploadedAt": source.uploaded_at.isoformat(),
                        "replacedByDocumentId": str(source.replaced_by_document_id)
                        if source.replaced_by_document_id
                        else None,
                    }
                    for i, source in enumerate(evidence)
                ]
            )
            self._guard.check(catalog)
            async with self._model_factory(settings) as model:
                result = await model.analyze(arguments.question, files, catalog)
            self._guard.check(result.model_dump_json())
            check_context_text(result.model_dump_json())
            valid_keys = {f"document:{source.document_id}" for source in evidence}
            if not set(result.source_keys).issubset(valid_keys) or not result.answer.strip():
                raise InvalidDocumentResponse()
            if any(len(item) > 500 for item in result.limitations) or re.search(
                r"[A-Za-z0-9+/]{1024,}={0,2}", result.model_dump_json()
            ):
                raise InvalidDocumentResponse()
            limitations = []
            if any(suffix in SPREADSHEETS for suffix in suffixes):
                limitations.append(
                    "El proveedor procesa hasta las primeras 1000 filas por hoja; "
                    "no permite asegurar cálculos exhaustivos."
                )
            if any(
                suffix in {".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx"} for suffix in suffixes
            ):
                limitations.append(
                    "En estos documentos el proveedor extrae texto; las imágenes o gráficos "
                    "incrustados pueden no estar disponibles."
                )
            return DocumentOutcome(
                status="analyzed",
                sources=[s for s in evidence if f"document:{s.document_id}" in result.source_keys],
                analysis=result,
                limitations=limitations,
            )
        except (DocumentTooLarge, UnsupportedDocument, DocumentUnavailable) as exc:
            return DocumentOutcome(status="unavailable", reason=exc.code, sources=evidence)
        finally:
            for file in files:
                file.close()
            files.clear()
