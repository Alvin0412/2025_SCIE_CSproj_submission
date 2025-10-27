import hashlib
from typing import Dict
import mimetypes
from django.core.files.uploadedfile import UploadedFile


def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def sniff_mime(uploaded: UploadedFile) -> str:
    return uploaded.content_type or mimetypes.guess_type(uploaded.name)[0] or "application/octet-stream"


def file_size(uploaded: UploadedFile) -> int:
    try:
        return uploaded.size
    except Exception:
        uploaded.seek(0, 2)
        size = uploaded.tell()
        uploaded.seek(0)
        return size


def compute_sha256_and_size(dj_file) -> tuple[str, int]:
    """
    计算文件 sha256 与 size；会复位文件指针。
    """
    hasher = hashlib.sha256()
    total = 0
    pos = dj_file.tell()
    dj_file.seek(0)
    for chunk in dj_file.chunks() if hasattr(dj_file, "chunks") else iter(lambda: dj_file.read(8192), b""):
        if not chunk:
            break
        hasher.update(chunk)
        total += len(chunk)
    dj_file.seek(pos)  # 复位
    return hasher.hexdigest(), total


def infer_pdf_pages(dj_file):
    try:
        pos = dj_file.tell()
        dj_file.seek(0)
        try:
            from PyPDF2 import PdfReader  # 可选依赖
            reader = PdfReader(dj_file)
            pages = len(reader.pages)
        finally:
            dj_file.seek(pos)
        return pages
    except Exception:
        return None
