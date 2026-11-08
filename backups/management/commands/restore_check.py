import re
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backups.common import (
    BackupOperationError,
    backup_path,
    connection_arguments,
    database_environment,
    translate_process_error,
    verify_dump,
)
from backups.pgtools import PostgreSQLToolsError, find_pg_tools


TABLES = (
    "inventory_product",
    "inventory_stockmovement",
    "pos_sale",
    "pos_saleitem",
    "purchase_purchase",
    "people_contact",
    "expenses_expense",
)


def _run(args, env, operation):
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, env=env, check=False, timeout=600
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupOperationError(
            f"{operation} could not be completed. Check the PostgreSQL tools and try again."
        ) from exc
    if result.returncode:
        raise BackupOperationError(translate_process_error(result, operation))
    return result.stdout.strip()


def _database_exists(psql, database, env, dbname, table):
    sql = f"SELECT to_regclass('public.{table}')"
    output = _run(
        [str(psql), "-w", *connection_arguments(database), "-d", dbname, "-t", "-A", "-c", sql],
        env,
        "Could not inspect a database table",
    )
    return bool(output)


def _row_count(psql, database, env, dbname, table):
    output = _run(
        [str(psql), "-w", *connection_arguments(database), "-d", dbname, "-t", "-A", "-c",
         f'SELECT count(*) FROM public."{table}"'],
        env,
        "Could not count database rows",
    )
    try:
        return int(output)
    except ValueError as exc:
        raise BackupOperationError("Could not read a restored table row count.") from exc


def _checked_backup(options, tools, env, database):
    if options["file"]:
        path = Path(options["file"]).expanduser().resolve()
        if not path.is_file():
            raise BackupOperationError("The selected backup file was not found.")
        verify_dump(path, tools["pg_restore"], env, database)
        return path

    directory = backup_path()
    candidates = []
    for path in directory.glob("artyflakes_pos_????-??-??_????.dump"):
        sidecar = path.with_name(path.name + ".sha256")
        try:
            expected = sidecar.read_text(encoding="ascii").split()[0]
            from backups.common import sha256_file
            if expected != sha256_file(path):
                continue
            verify_dump(path, tools["pg_restore"], env, database)
        except (OSError, ValueError, IndexError, BackupOperationError):
            continue
        candidates.append(path)
    if not candidates:
        raise BackupOperationError("No verified database backup was found.")
    return max(candidates, key=lambda path: path.name)


class Command(BaseCommand):
    help = "Restore a verified backup to a scratch database and compare key row counts."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Backup dump to test instead of the newest verified backup.")
        parser.add_argument("--scratch-db", default="artyflakes_restore_test",
                            help="Scratch database name (never the live database).")

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        scratch = options["scratch_db"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", scratch):
            raise CommandError("The scratch database name may contain only letters, numbers and underscores.")
        if scratch == str(database["NAME"]):
            raise CommandError("Restore check refused: the scratch database name matches the live database.")

        created = False
        failures = []
        try:
            env = database_environment(database)
            tools = find_pg_tools(database_host=database.get("HOST"))
            dump = _checked_backup(options, tools, env, database)
            admin_args = [str(tools["psql"]), "-w", *connection_arguments(database), "-d", "postgres"]
            _run([*admin_args, "-c", f'CREATE DATABASE "{scratch}"'], env, "Could not create the scratch database")
            created = True

            restore_args = [
                str(tools["pg_restore"]), "-w", "--no-owner", "--exit-on-error",
                "--dbname", scratch, str(dump),
            ]
            _run(restore_args, env, "Restore into the scratch database")

            for table in TABLES:
                if not _database_exists(tools["psql"], database, env, str(database["NAME"]), table):
                    if table == "expenses_expense":
                        continue
                    failures.append(f"{table}: not found in the live database")
                    continue
                live_count = _row_count(tools["psql"], database, env, str(database["NAME"]), table)
                if not _database_exists(tools["psql"], database, env, scratch, table):
                    restored_count = 0
                    failures.append(f"{table}: live {live_count}, restored table missing")
                    continue
                restored_count = _row_count(tools["psql"], database, env, scratch, table)
                self.stdout.write(f"{table}: live {live_count}, restored {restored_count}")
                if live_count > 0 and restored_count == 0:
                    failures.append(f"{table}: live {live_count}, restored 0")
        except (BackupOperationError, PostgreSQLToolsError) as exc:
            failures.append(str(exc))
        finally:
            if created:
                try:
                    env = database_environment(database)
                    tools = find_pg_tools(database_host=database.get("HOST"))
                    _run(
                        [str(tools["psql"]), "-w", *connection_arguments(database), "-d", "postgres",
                         "-c", f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'],
                        env,
                        "Could not remove the scratch database",
                    )
                except (BackupOperationError, PostgreSQLToolsError) as exc:
                    failures.append(str(exc))

        if failures:
            self.stderr.write(self.style.ERROR("Restore check: FAIL"))
            for failure in failures:
                self.stderr.write(failure)
            raise CommandError("Restore check failed. Review the messages above.")
        self.stdout.write(self.style.SUCCESS("Restore check: PASS"))
