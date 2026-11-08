import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .retention import BACKUP_NAME_RE


EXPECTED_TABLES = ("inventory_product", "pos_sale")


class BackupOperationError(RuntimeError):
    pass


def local_timestamp():
    return timezone.localtime(timezone.now()).strftime("%Y-%m-%d_%H%M")


def timestamp_iso():
    return timezone.localtime(timezone.now()).isoformat()


def database_environment(database):
    env = os.environ.copy()
    password = database.get("PASSWORD")
    if password is None:
        env.pop("PGPASSWORD", None)
    else:
        env["PGPASSWORD"] = str(password)
    return env


def connection_arguments(database):
    args = []
    for option, key in (("-h", "HOST"), ("-p", "PORT"), ("-U", "USER")):
        value = database.get(key)
        if value:
            args.extend((option, str(value)))
    return args


def backup_path():
    directory = Path(settings.BACKUP_DIR).expanduser().resolve()
    project = Path(settings.BASE_DIR).resolve()
    try:
        directory.relative_to(project)
    except ValueError:
        return directory
    raise BackupOperationError("The backup folder must be outside the project folder.")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as backup_file:
        for chunk in iter(lambda: backup_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def translate_process_error(result, operation):
    message = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    if "password authentication failed" in message:
        return "Database password authentication failed. Check DB_USER and DB_PASSWORD in `.env`."
    if any(text in message for text in (
        "no space left on device",
        "disk full",
        "not enough space",
        "permission denied",
        "could not open output file",
    )):
        return "The backup folder is not writable or the disk is full. Check available space and folder permissions."
    if "server version" in message and ("older" in message or "newer" in message):
        return "PostgreSQL backup tools are older than the server. Install matching or newer PostgreSQL tools."
    if "could not connect" in message or "connection refused" in message or "no connection" in message:
        return "Could not connect to PostgreSQL. Check that the server is running and DB_HOST/DB_PORT in `.env`."
    return f"{operation} failed. Check PostgreSQL is running and that the database settings are correct."


def verify_dump(path, pg_restore, env, database, minimum_bytes=None):
    minimum_bytes = settings.BACKUP_MIN_BYTES if minimum_bytes is None else minimum_bytes
    if Path(path).stat().st_size <= minimum_bytes:
        raise BackupOperationError(
            f"Backup verification failed: the dump must be larger than {minimum_bytes} bytes."
        )
    args = [str(pg_restore), "--list", str(path)]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, env=env, check=False, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupOperationError(
            "Backup verification failed. Check that the PostgreSQL restore tool can read the dump."
        ) from exc
    if result.returncode:
        raise BackupOperationError(translate_process_error(result, "Backup verification"))
    listing = result.stdout or ""
    if not any(table in listing for table in EXPECTED_TABLES):
        raise BackupOperationError(
            "Backup verification failed: the dump does not list an expected POS or inventory table."
        )
    return sha256_file(path)


def write_status(directory, status):
    target = Path(directory) / "status.json"
    temporary = target.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as status_file:
        json.dump(status, status_file, indent=2)
        status_file.write("\n")
    temporary.replace(target)


def read_status(directory):
    try:
        with (Path(directory) / "status.json").open(encoding="utf-8") as status_file:
            value = json.load(status_file)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def valid_backup_filename(path):
    return BACKUP_NAME_RE.fullmatch(Path(path).name) is not None


def backup_timestamp(path):
    match = BACKUP_NAME_RE.fullmatch(Path(path).name)
    if not match:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%Y-%m-%d%H%M")
    except ValueError:
        return None
