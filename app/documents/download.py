from typing import Never
from uuid import UUID


class DownloadedDocument:
    """Owned in-memory bytes; not a Pydantic/dataclass/wire contract.

    close releases references, not physical RAM zeroization (Python cannot guarantee it).
    """

    __slots__ = ("project_id", "document_id", "filename", "content_type", "__content")

    def __init__(
        self, project_id: UUID, document_id: UUID, filename: str, content_type: str, content: bytes
    ) -> None:
        self.project_id = project_id
        self.document_id = document_id
        self.filename = filename
        self.content_type = content_type
        self.__content = content

    @property
    def content(self) -> bytes:
        return self.__content

    @property
    def size_bytes(self) -> int:
        return len(self.__content)

    def close(self) -> None:
        self.__content = b""

    def __repr__(self) -> str:
        return "<DownloadedDocument>"

    def __reduce__(self) -> Never:
        raise TypeError("Document bytes cannot be serialized")
