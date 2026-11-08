import logging
import re
import shutil
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone as datetime_timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from backups.common import (
    BackupOperationError,
    backup_path,
    connection_arguments,
    database_environment,
    local_timestamp,
    read_status,
    sha256_file,
    timestamp_iso,
    translate_process_error,
    verify_dump,
    write_status,
)
from backups.pgtools import PostgreSQLToolsError, find_pg_tools
from backups.retention import matching_backup_names, select_backups_to_keep


def _logger(directory):
    logger = logging.getLogger("backups")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    expected = str(Path(directory) / "backup.log")
    for handler in logger.handlers[:]:
        if getattr(handler, "baseFilename", None) != expected:
            logger.removeHandler(handler)
            handler.close()
    if not any(getattr(handler, "baseFilename", None) == expected for handler in logger.handlers):
        handler = RotatingFileHandler(expected, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        formatter.converter = lambda timestamp: timezone.localtime(
            datetime.fromtimestamp(timestamp, datetime_timezone.utc)
        ).timetuple()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def _run(args, env, operation):
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, env=env, check=False, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupOperationError(f"{operation} could not be completed. Check the PostgreSQL tools and try again.") from exc
    if result.returncode:
        raise BackupOperationError(translate_process_error(result, operation))
    return result


def _check_server_version(psql, database, env):
    args = [str(psql), "-w", *connection_arguments(database), "-d", str(database["NAME"]),
            "-t", "-A", "-c", "show server_version_num"]
    result = _run(args, env, "Could not read the PostgreSQL server version")
    try:
        return int(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise BackupOperationError("Could not determine the PostgreSQL server version.") from exc


def _check_tool_version(pg_dump, server_version, env):
    result = _run([str(pg_dump), "--version"], env, "Could not check the pg_dump version")
    match = re.search(r"\b(\d+)(?:\.\d+)?", result.stdout or result.stderr or "")
    if not match:
        raise BackupOperationError("Could not determine the pg_dump version.")
    tool_major = int(match.group(1))
    server_major = server_version // 10000
    if tool_major < server_major:
        raise BackupOperationError(
            "PostgreSQL backup tools are older than the server. Install matching or newer PostgreSQL tools."
        )


def _verified_backups(directory, tools, env, database):
    verified = {}
    for name in matching_backup_names(path.name for path in Path(directory).iterdir()):
        path = Path(directory) / name
        sidecar = path.with_name(path.name + ".sha256")
        try:
            expected_hash = sidecar.read_text(encoding="ascii").split()[0]
            actual_hash = sha256_file(path)
            if expected_hash != actual_hash:
                continue
            verify_dump(path, tools["pg_restore"], env, database)
        except (OSError, ValueError, IndexError, BackupOperationError, subprocess.SubprocessError):
            continue
        verified[name] = path
    return verified


def apply_retention(directory, tools, env, database):
    verified = _verified_backups(directory, tools, env, database)
    keep = select_backups_to_keep(
        verified,
        keep_daily=settings.BACKUP_KEEP_DAILY,
        keep_weekly=settings.BACKUP_KEEP_WEEKLY,
        keep_monthly=settings.BACKUP_KEEP_MONTHLY,
    )
    for name, path in verified.items():
        if name not in keep and any(
            other_name > name for other_name in verified if other_name in keep
        ):
            path.unlink()
            path.with_name(path.name + ".sha256").unlink(missing_ok=True)
    return sorted(name for name in keep if name in verified and (Path(directory) / name).exists())


def _copy_verified(source, copy_dir):
    if not copy_dir:
        return "not_configured"
    destination_dir = Path(copy_dir).expanduser().resolve()
    project_dir = Path(settings.BASE_DIR).resolve()
    try:
        destination_dir.relative_to(project_dir)
    except ValueError:
        pass
    else:
        raise OSError("The copy folder must be outside the project folder.")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    shutil.copy2(source, destination)
    if sha256_file(source) != sha256_file(destination):
        destination.unlink(missing_ok=True)
        raise OSError("The copied backup did not pass its SHA-256 check.")
    shutil.copy2(source.with_name(source.name + ".sha256"),
                 destination.with_name(destination.name + ".sha256"))
    return "copied"


class Command(BaseCommand):
    help = "Create, verify, copy and retain a PostgreSQL database backup."

    def handle(self, *args, **options):
        attempt = timestamp_iso()
        directory = None
        status = {}
        logger = None
        partial_created = False
        try:
            directory = backup_path()
            directory.mkdir(parents=True, exist_ok=True)
            logger = _logger(directory)
            status = read_status(directory)
            database = settings.DATABASES["default"]
            env = database_environment(database)
            tools = find_pg_tools(database_host=database.get("HOST"))
            server_version = _check_server_version(tools["psql"], database, env)
            _check_tool_version(tools["pg_dump"], server_version, env)

            filename = f"artyflakes_pos_{local_timestamp()}.dump"
            final_path = directory / filename
            partial_path = directory / (filename + ".part")
            if final_path.exists() or partial_path.exists():
                raise BackupOperationError(
                    "A backup with this timestamp already exists. Wait until the next minute and try again."
                )
            partial_created = True
            dump_args = [
                str(tools["pg_dump"]), "-F", "c", "-w", "-Z", "6",
                *connection_arguments(database), "-f", str(partial_path), str(database["NAME"]),
            ]
            _run(dump_args, env, "Database backup")
            digest = verify_dump(partial_path, tools["pg_restore"], env, database)
            partial_path.replace(final_path)
            (directory / (filename + ".sha256")).write_text(
                f"{digest}  {filename}\n", encoding="ascii"
            )

            copy_status = "not_configured"
            copy_warning = ""
            try:
                copy_status = _copy_verified(final_path, settings.BACKUP_COPY_DIR)
            except OSError:
                copy_status = "failed"
                copy_warning = " The optional backup copy failed; check the backup drive."
                logger.warning("Verified backup created; optional off-site copy failed.")

            retained = apply_retention(directory, tools, env, database)
            status.update({
                "last_attempt": attempt,
                "last_success": timestamp_iso(),
                "file_name": filename,
                "size": final_path.stat().st_size,
                "sha256": digest,
                "copy_status": copy_status,
                "error": "",
                "retained_files": retained,
            })
            write_status(directory, status)
            logger.info("Verified database backup created: %s (%s bytes)", filename, status["size"])
            self.stdout.write(self.style.SUCCESS(f"Database backup verified: {filename}.{copy_warning}"))
        except (BackupOperationError, PostgreSQLToolsError) as exc:
            error = str(exc)
            if directory is not None and logger is not None:
                status.update({
                    "last_attempt": attempt,
                    "error": error,
                    "copy_status": status.get("copy_status", "not_configured"),
                    "retained_files": status.get("retained_files", []),
                })
                try:
                    write_status(directory, status)
                    logger.error("Database backup failed: %s", error)
                except OSError:
                    error = "Database backup failed and the status file could not be written. Check the backup drive."
            self.stderr.write(self.style.ERROR(error))
            raise CommandError(error) from exc
        except OSError as exc:
            error = "The backup folder is not writable or the disk is full. Check available space and folder permissions."
            if directory is not None and logger is not None:
                status.update({
                    "last_attempt": attempt,
                    "error": error,
                    "copy_status": status.get("copy_status", "not_configured"),
                    "retained_files": status.get("retained_files", []),
                })
                try:
                    write_status(directory, status)
                    logger.error("Database backup failed: %s", error)
                except OSError:
                    error = "The backup folder is not writable or the disk is full; the status file could not be written."
            self.stderr.write(self.style.ERROR(error))
            raise CommandError(error) from exc
        finally:
            partial = locals().get("partial_path")
            if partial_created and partial is not None and partial.exists():
                try:
                    partial.unlink()
                except OSError:
                    if logger is not None:
                        logger.error("Could not remove an incomplete backup file.")
