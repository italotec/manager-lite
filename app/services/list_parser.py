"""Transient lead-list parsing — lists are never written to durable storage.

An uploaded .csv/.xlsx is saved to a temp file only for the duration of
parsing; the file is deleted immediately after and the rows only ever live
in the server's RAM (see list_cache.py) until the disparo consumes them.
"""
import os
import tempfile
from werkzeug.utils import secure_filename

from .disparar_service import iter_rows

ALLOWED_EXT = {"csv", "xlsx"}


def allowed_list_file(filename: str) -> bool:
    return bool(filename) and "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def parse_uploaded_list(file_storage, has_header: bool = True) -> list:
    filename = secure_filename(file_storage.filename or "list")
    suffix = "." + filename.rsplit(".", 1)[1].lower() if "." in filename else ".csv"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        file_storage.save(tmp_path)
        return list(iter_rows(tmp_path, has_header=has_header))
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def dedup_by_phone(rows: list, phone_col: str) -> list:
    """Keep the first occurrence of each non-empty phone value."""
    seen = set()
    out = []
    for row in rows:
        phone = str(row.get(phone_col, "")).strip()
        if not phone or phone in seen:
            continue
        seen.add(phone)
        out.append(row)
    return out
