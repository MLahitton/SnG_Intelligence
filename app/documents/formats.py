"""Minimal type checks, never content extraction or execution. Not an antivirus."""

import io
import zipfile
from pathlib import PurePath

from app.documents.errors import UnsupportedDocument

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".xml": "application/xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
SPREADSHEETS = {".csv", ".tsv", ".xls", ".xlsx"}


def extension(filename: str, content_type: str | None) -> str:
    suffix = PurePath(filename).suffix.lower()
    if suffix not in MIME_TYPES or any(ord(char) < 32 for char in filename):
        raise UnsupportedDocument()
    # Metadata is a consistency check, not proof of the byte format.
    mime = (content_type or "").split(";", 1)[0].lower().strip()
    aliases = {"text/plain", "application/octet-stream", "binary/octet-stream", ""}
    aliases.update({"text/rtf"} if suffix == ".rtf" else set())
    aliases.update({"text/xml"} if suffix == ".xml" else set())
    aliases.update({"application/csv"} if suffix == ".csv" else set())
    aliases.update({"text/tsv"} if suffix == ".tsv" else set())
    if mime not in {MIME_TYPES[suffix], *aliases}:
        raise UnsupportedDocument()
    return suffix


def validate_bytes(suffix: str, data: bytes) -> None:
    valid = False
    if not data or data.startswith((b"MZ", b"\x7fELF")):
        raise UnsupportedDocument()
    if suffix == ".pdf":
        valid = data.startswith(b"%PDF-")
    elif suffix == ".png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif suffix in {".jpg", ".jpeg"}:
        valid = data.startswith(b"\xff\xd8\xff")
    elif suffix == ".webp":
        valid = data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    elif suffix in {".doc", ".xls", ".ppt"}:
        valid = data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    elif suffix == ".rtf":
        valid = data.lstrip().startswith(b"{\\rtf")
    elif suffix in {".docx", ".xlsx", ".pptx", ".odt"}:
        # Inspect the bounded ZIP directory only; never extract/decompress document entries.
        # Reject excessive directories before ZipFile allocates per-entry objects.
        if data.count(b"PK\x01\x02") > 10000:
            raise UnsupportedDocument()
        marker = {
            ".docx": "word/document.xml",
            ".xlsx": "xl/workbook.xml",
            ".pptx": "ppt/presentation.xml",
            ".odt": "content.xml",
        }[suffix]
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                entries = package.infolist()
                names = {entry.filename for entry in entries}
                valid = (
                    len(entries) <= 10000
                    and marker in names
                    and (
                        "[Content_Types].xml" in names
                        if suffix != ".odt"
                        else "META-INF/manifest.xml" in names
                    )
                    and not any(entry.flag_bits & 1 for entry in entries)
                )
        except (zipfile.BadZipFile, ValueError, OSError):
            valid = False
    else:
        try:
            text = data.decode("utf-8-sig")
            valid = "\0" not in text and not any(ord(c) < 32 and c not in "\r\n\t\f" for c in text)
        except UnicodeDecodeError:
            # UTF-16 with BOM is common for exported business text.
            try:
                valid = data.startswith((b"\xff\xfe", b"\xfe\xff")) and "\0" not in data.decode(
                    "utf-16"
                )
            except UnicodeDecodeError:
                valid = False
    if not valid:
        raise UnsupportedDocument()
