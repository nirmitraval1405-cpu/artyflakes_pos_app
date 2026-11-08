import hashlib
import json
import logging
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from backups.management.commands.backup_db import apply_retention
from backups.pgtools import PostgreSQLToolsError, find_pg_tools
from backups.retention import matching_backup_names, select_backups_to_keep


def _touch_tools(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    names = ("pg_dump.exe", "pg_restore.exe", "psql.exe")
    for name in names:
        (directory / name).write_text("", encoding="ascii")
    return dict(zip(("pg_dump", "pg_restore", "psql"), (directory / name for name in names)))


class PostgreSQLToolDiscoveryTests(SimpleTestCase):
    def test_backup_pg_bin_wins_over_path(self):
        with tempfile.TemporaryDirectory() as root:
            configured = _touch_tools(Path(root) / "configured")
            path = _touch_tools(Path(root) / "path")
            path_tools = {item.name: item for item in path.values()}
            with patch("backups.pgtools.shutil.which",
                       side_effect=lambda name, path=None: str(path_tools[name])
                       if name in path_tools else None):
                found = find_pg_tools(
                    {"BACKUP_PG_BIN": str(Path(root) / "configured"), "PATH": ""},
                    database_host="remote-db",
                )
            self.assertEqual(found, configured)

    def test_path_is_used_after_configured_folder(self):
        with tempfile.TemporaryDirectory() as root:
            path_tools = _touch_tools(Path(root) / "path")
            lookup = {item.name: str(item) for item in path_tools.values()}
            with patch("backups.pgtools.shutil.which",
                       side_effect=lambda name, path=None: lookup.get(name)):
                found = find_pg_tools({"PATH": ""}, database_host="remote-db")
        self.assertEqual(found, path_tools)

    def test_highest_version_folder_is_chosen(self):
        with tempfile.TemporaryDirectory() as root:
            program_files = Path(root) / "Program Files"
            older = _touch_tools(program_files / "PostgreSQL" / "15" / "bin")
            newer = _touch_tools(program_files / "PostgreSQL" / "16" / "bin")
            with patch("backups.pgtools.shutil.which", return_value=None):
                found = find_pg_tools(
                    {"ProgramFiles": str(program_files)},
                    database_host="localhost",
                    search_roots=[program_files],
                )
        self.assertEqual(found, newer)
        self.assertNotEqual(found, older)

    def test_missing_install_has_friendly_error(self):
        with patch("backups.pgtools.shutil.which", return_value=None):
            with self.assertRaisesRegex(PostgreSQLToolsError, "BACKUP_PG_BIN"):
                find_pg_tools({"PATH": ""}, database_host="remote-db", search_roots=[])


class RetentionTests(SimpleTestCase):
    def _name(self, day, time="2200"):
        return f"artyflakes_pos_{day:%Y-%m-%d}_{time}.dump"

    def test_keeps_latest_backup_for_each_daily_slot(self):
        today = date(2026, 11, 8)
        names = [self._name(today - timedelta(days=day)) for day in range(20)]
        keep = select_backups_to_keep(
            names, today=today, keep_daily=3, keep_weekly=0, keep_monthly=0
        )
        self.assertEqual(len(keep), 3)
        self.assertEqual(keep, set(names[:3]))

    def test_daily_retention_counts_backup_days_not_calendar_gaps(self):
        names = [
            self._name(date(2026, 11, 8)),
            self._name(date(2026, 10, 1)),
            self._name(date(2026, 8, 1)),
        ]
        keep = select_backups_to_keep(
            names, today=date(2026, 11, 8), keep_daily=2, keep_weekly=0, keep_monthly=0
        )
        self.assertEqual(keep, set(names[:2]))

    def test_keeps_newest_backup_in_each_week(self):
        today = date(2026, 11, 8)
        names = [self._name(today - timedelta(weeks=week)) for week in range(8)]
        keep = select_backups_to_keep(
            names, today=today, keep_daily=0, keep_weekly=4, keep_monthly=0
        )
        self.assertEqual(len(keep), 4)

    def test_keeps_newest_backup_in_each_month(self):
        today = date(2026, 11, 8)
        names = [
            self._name(date(2026, month, 5))
            for month in range(1, 12)
        ]
        keep = select_backups_to_keep(
            names, today=today, keep_daily=0, keep_weekly=0, keep_monthly=4
        )
        self.assertEqual(len(keep), 4)

    def test_no_backup_is_deleted_when_there_is_no_newer_verified_backup(self):
        only = self._name(date(2020, 1, 1))
        keep = select_backups_to_keep(
            [only], today=date(2026, 11, 8), keep_daily=0, keep_weekly=0, keep_monthly=0
        )
        self.assertEqual(keep, {only})

    def test_unrelated_files_are_not_backup_candidates(self):
        names = {
            self._name(date(2026, 11, 8)),
            "notes.txt",
            "artyflakes_pos_2026-11-08_2200.dump.part",
            "otherapp_2026-11-08.dump",
        }
        self.assertEqual(
            matching_backup_names(names), {self._name(date(2026, 11, 8))}
        )

    @override_settings(
        BACKUP_MIN_BYTES=1,
        BACKUP_KEEP_DAILY=1,
        BACKUP_KEEP_WEEKLY=0,
        BACKUP_KEEP_MONTHLY=0,
    )
    def test_retention_does_not_touch_unrelated_files(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            today = date.today()
            recent = self._name(today)
            old = self._name(today - timedelta(days=4))
            unrelated = directory / "keep-me.txt"
            unrelated.write_text("unchanged", encoding="utf-8")
            for name in (recent, old):
                path = directory / name
                path.write_bytes(b"verified dump")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                path.with_name(name + ".sha256").write_text(digest, encoding="ascii")

            listing = type("Result", (), {"returncode": 0, "stdout": "TABLE public inventory_product",
                                          "stderr": ""})()
            with patch("backups.common.subprocess.run", return_value=listing):
                retained = apply_retention(
                    directory, {"pg_restore": "pg_restore.exe"}, {}, {}
                )
            self.assertEqual(retained, [recent])
            self.assertTrue((directory / recent).exists())
            self.assertFalse((directory / old).exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "unchanged")


class BackupCommandTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "backups"
        self.tools = {
            "pg_dump": Path(self.temp.name) / "pg_dump.exe",
            "pg_restore": Path(self.temp.name) / "pg_restore.exe",
            "psql": Path(self.temp.name) / "psql.exe",
        }
        self.db = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "pos_db",
            "USER": "pos_user",
            "PASSWORD": "do-not-leak-this-password",
            "HOST": "localhost",
            "PORT": "5432",
        }
        self.calls = []
        self.dump_mode = "success"
        self.dump_error = "pg_dump failed"
        self.version = "16.2"
        self._settings = override_settings(
            BACKUP_DIR=self.directory,
            BACKUP_COPY_DIR="",
            BACKUP_MIN_BYTES=20,
            BACKUP_KEEP_DAILY=14,
            BACKUP_KEEP_WEEKLY=8,
            BACKUP_KEEP_MONTHLY=12,
            DATABASES={"default": self.db},
        )
        self._settings.enable()
        self.addCleanup(self._settings.disable)

    def tearDown(self):
        logger = logging.getLogger("backups")
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        super().tearDown()

    def fake_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        executable = Path(args[0]).name.lower()
        if executable.startswith("psql"):
            return self.result(0, "160000")
        if executable.startswith("pg_dump") and "--version" in args:
            return self.result(0, f"pg_dump (PostgreSQL) {self.version}")
        if executable.startswith("pg_dump"):
            if self.dump_mode == "failure":
                return self.result(1, "", self.dump_error)
            output = Path(args[args.index("-f") + 1])
            size = 0 if self.dump_mode == "empty" else 20 if self.dump_mode == "minimum" else 128
            output.write_bytes(b"x" * size)
            return self.result(0)
        if executable.startswith("pg_restore"):
            if self.dump_mode == "restore-failure":
                return self.result(1, "", "invalid archive")
            return self.result(0, "TABLE public inventory_product\nTABLE public pos_sale")
        return self.result(1, "", "unexpected command")

    @staticmethod
    def result(returncode=0, stdout="", stderr=""):
        return type("Result", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()

    def run_command(self):
        with patch("backups.management.commands.backup_db.find_pg_tools", return_value=self.tools), \
             patch("backups.management.commands.backup_db.subprocess.run", side_effect=self.fake_run):
            call_command("backup_db")

    def test_success_creates_dump_checksum_status_and_log(self):
        self.run_command()
        dumps = list(self.directory.glob("artyflakes_pos_*.dump"))
        self.assertEqual(len(dumps), 1)
        dump_call = next(args for args, _ in self.calls if "-F" in args)
        self.assertEqual(dump_call[dump_call.index("-F") + 1], "c")
        self.assertIn("-w", dump_call)
        self.assertEqual(dump_call[dump_call.index("-Z") + 1], "6")
        self.assertNotIn("--no-owner", dump_call)
        self.assertTrue(Path(str(dumps[0]) + ".sha256").is_file())
        self.assertTrue((self.directory / "status.json").is_file())
        self.assertIn("Verified database backup created", (self.directory / "backup.log").read_text())

    def test_dump_failure_leaves_no_partial_or_final_dump(self):
        self.dump_mode = "failure"
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(list(self.directory.glob("*.part")), [])
        self.assertEqual(list(self.directory.glob("*.dump")), [])

    def test_disk_full_error_is_actionable(self):
        self.dump_mode = "failure"
        self.dump_error = "could not write output: No space left on device"
        with self.assertRaisesRegex(CommandError, "disk is full"):
            self.run_command()

    def test_empty_or_too_small_dump_is_removed(self):
        self.dump_mode = "empty"
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(list(self.directory.glob("*.part")), [])
        self.assertEqual(list(self.directory.glob("*.dump")), [])

    def test_dump_at_minimum_size_is_rejected(self):
        self.dump_mode = "minimum"
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(list(self.directory.glob("*.part")), [])
        self.assertEqual(list(self.directory.glob("*.dump")), [])

    def test_restore_list_failure_rejects_and_removes_dump(self):
        self.dump_mode = "restore-failure"
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(list(self.directory.glob("*.part")), [])
        self.assertEqual(list(self.directory.glob("*.dump")), [])

    def test_password_is_not_in_arguments_log_or_status(self):
        self.run_command()
        self.assertNotIn(
            self.db["PASSWORD"],
            repr([args for args, _ in self.calls]),
        )
        self.assertNotIn(self.db["PASSWORD"], (self.directory / "backup.log").read_text())
        self.assertNotIn(self.db["PASSWORD"], (self.directory / "status.json").read_text())

    def test_backup_directory_inside_project_is_refused(self):
        project = Path(self.temp.name) / "project"
        inside = project / "backups"
        with override_settings(BASE_DIR=project, BACKUP_DIR=inside):
            with self.assertRaisesRegex(CommandError, "outside the project"):
                call_command("backup_db")
        self.assertFalse(inside.exists())

    def test_missing_copy_drive_does_not_fail_main_backup(self):
        blocked_parent = Path(self.temp.name) / "not-a-directory"
        blocked_parent.write_text("file", encoding="utf-8")
        with override_settings(BACKUP_COPY_DIR=str(blocked_parent / "backup")):
            self.run_command()
        status = json.loads((self.directory / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["copy_status"], "failed")
        self.assertTrue(list(self.directory.glob("*.dump")))

    def test_older_tool_version_is_translated(self):
        self.version = "15.8"
        with self.assertRaisesRegex(CommandError, "older than the server"):
            self.run_command()
        status = json.loads((self.directory / "status.json").read_text(encoding="utf-8"))
        self.assertIn("older than the server", status["error"])

    def test_authentication_failure_is_translated(self):
        def auth_failure(args, **kwargs):
            if Path(args[0]).name.lower().startswith("psql"):
                return self.result(2, "", "FATAL: password authentication failed")
            return self.fake_run(args, **kwargs)

        with patch("backups.management.commands.backup_db.find_pg_tools", return_value=self.tools), \
             patch("backups.management.commands.backup_db.subprocess.run", side_effect=auth_failure):
            with self.assertRaisesRegex(CommandError, "Check DB_USER and DB_PASSWORD"):
                call_command("backup_db")
        status = json.loads((self.directory / "status.json").read_text(encoding="utf-8"))
        self.assertIn("password authentication failed", status["error"])
        self.assertNotIn(self.db["PASSWORD"], (self.directory / "status.json").read_text())


class RestoreCheckTests(SimpleTestCase):
    @override_settings(DATABASES={"default": {"NAME": "artyflakes_restore_test"}})
    def test_refuses_scratch_name_matching_live_database(self):
        with self.assertRaisesRegex(CommandError, "matches the live database"):
            call_command("restore_check")

    def test_scratch_database_is_dropped_when_restore_fails(self):
        database = {
            "NAME": "live_pos",
            "USER": "pos_user",
            "PASSWORD": "secret-password",
            "HOST": "localhost",
            "PORT": "5432",
        }
        tools = {"pg_dump": Path("pg_dump.exe"), "pg_restore": Path("pg_restore.exe"),
                 "psql": Path("psql.exe")}
        with tempfile.TemporaryDirectory() as root:
            dump = Path(root) / "test.dump"
            dump.write_bytes(b"dump")
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if "CREATE DATABASE" in args:
                    return BackupCommandTests.result()
                if Path(args[0]).name.lower().startswith("pg_restore"):
                    return BackupCommandTests.result(1, "", "restore failed")
                if "DROP DATABASE" in args:
                    return BackupCommandTests.result()
                return BackupCommandTests.result(0, "TABLE public inventory_product")

            with override_settings(DATABASES={"default": database}, BACKUP_MIN_BYTES=1), \
                 patch("backups.management.commands.restore_check.find_pg_tools", return_value=tools), \
                 patch("backups.management.commands.restore_check._checked_backup", return_value=dump), \
                 patch("backups.management.commands.restore_check.subprocess.run", side_effect=fake_run):
                with self.assertRaises(CommandError):
                    call_command("restore_check")
        self.assertTrue(any("DROP DATABASE" in arg for args in calls for arg in args))


class DashboardBackupBannerTests(TestCase):
    def setUp(self):
        from accounts.models import User

        self.admin = User.objects.create_user("backup-admin", password="test-pass", role="admin")
        self.cashier = User.objects.create_user("backup-cashier", password="test-pass", role="cashier")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backup_dir = Path(self.temp.name)

    def _write_status(self, contents):
        (self.backup_dir / "status.json").write_text(
            json.dumps(contents), encoding="utf-8"
        )

    def _dashboard(self, user):
        self.client.force_login(user)
        with override_settings(BACKUP_DIR=self.backup_dir):
            return self.client.get("/dashbord/")

    def test_absent_status_warns_admin(self):
        response = self._dashboard(self.admin)
        self.assertContains(response, "No successful database backup is recorded")

    def test_fresh_success_has_no_warning(self):
        self._write_status({"last_success": timezone.now().isoformat(), "error": "", "copy_status": "copied"})
        response = self._dashboard(self.admin)
        self.assertNotContains(response, "backup is recorded")
        self.assertNotContains(response, "last successful backup was")

    def test_three_day_old_success_warns_admin(self):
        self._write_status({
            "last_success": (timezone.now() - timedelta(days=3)).isoformat(),
            "error": "",
            "copy_status": "copied",
        })
        response = self._dashboard(self.admin)
        self.assertContains(response, "3 days ago")

    def test_failed_attempt_warns_admin(self):
        self._write_status({
            "last_success": timezone.now().isoformat(),
            "last_attempt": timezone.now().isoformat(),
            "error": "safe failure description",
            "copy_status": "copied",
        })
        response = self._dashboard(self.admin)
        self.assertContains(response, "last backup attempt failed")
        self.assertNotContains(response, "safe failure description")

    def test_failed_copy_warns_admin(self):
        self._write_status({
            "last_success": timezone.now().isoformat(),
            "error": "",
            "copy_status": "failed",
        })
        response = self._dashboard(self.admin)
        self.assertContains(response, "backup copy could not be made")

    def test_cashier_never_sees_backup_warning(self):
        response = self._dashboard(self.cashier)
        self.assertRedirects(response, "/")

    def test_corrupt_status_does_not_crash_dashboard(self):
        (self.backup_dir / "status.json").write_text("{broken", encoding="utf-8")
        response = self._dashboard(self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No successful database backup is recorded")
