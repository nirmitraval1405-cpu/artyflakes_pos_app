# Database backup and restore

The database contains bills, customer details and stock records. Keep backups
on a trusted drive. Consider turning on BitLocker for a removable backup drive.
This backup feature does not encrypt dump files.

## Where backups are kept

By default, verified backups are saved outside the project in:

`%USERPROFILE%\Documents\artyflakes-backups`

Set `BACKUP_DIR` in the project `.env` to choose another folder. It must not be
inside the project. Each `.dump` file has a `.sha256` checksum beside it.
`status.json` records the most recent attempt and `backup.log` records a brief
history. The scheduled task also appends its console output to
`scripts\backup-task.log`. Keep the backup folder on a drive with enough free
space.

## Set up PostgreSQL tools

The PostgreSQL installer includes `pg_dump.exe`, `pg_restore.exe` and
`psql.exe`, usually in a folder such as
`C:\Program Files\PostgreSQL\16\bin`. The application searches common
locations and PATH automatically. If it cannot find the tools, add the folder
containing all three programs to `.env`:

```dotenv
BACKUP_PG_BIN=C:\Program Files\PostgreSQL\16\bin
```

Save the file and run the backup again. The database connection still uses the
existing `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST` and `DB_PORT` settings.
Never put the database password in a command or script.

## Run a backup by hand

Open PowerShell in the project folder and run:

```powershell
.\venv\Scripts\python.exe .\manage.py backup_db
```

The command reports success only after the dump is large enough and PostgreSQL
can list its contents. It removes an incomplete file instead of leaving it
with a normal backup name. A successful backup is named with the local date
and time. The application keeps recent daily, weekly and monthly backups.

## Set up the daily scheduled task

In PowerShell, from the project folder, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_backup_task.ps1
```

The task runs every day at 10:30 PM when this Windows user is logged on.
Choose another 24-hour time if needed:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_backup_task.ps1 -Time 21:45
```

The task starts when the PC next becomes available after a missed run and
stops a run after 15 minutes. To start it now:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_backup_task.ps1 -RunNow
```

To remove the task:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_backup_task.ps1 -Unregister
```

## Check that it ran

Open **Task Scheduler > Task Scheduler Library**, select **Artyflakes DB
Backup**, then check **Last Run Time** and **Last Run Result**. A result of
`0x0` means the script completed successfully. Check `backup.log` in the
backup folder and confirm a recent `.dump` file and matching `.sha256` file
exist. The dashboard warns administrators when backups are missing, stale,
or a recent attempt or optional copy failed.

## Keep a second copy off the PC

Connect a trusted USB drive or use a synced folder, then set its folder in
`.env`, for example:

```dotenv
BACKUP_COPY_DIR=E:\Artyflakes-backups
```

After each verified main backup, the application copies the dump and checksum
and compares SHA-256 hashes. If the second drive is unavailable, the main
backup still succeeds, but the log, status file and dashboard report that the
copy failed. Reconnect the drive and run another backup.

## Test that a backup can be restored

Run:

```powershell
.\venv\Scripts\python.exe .\manage.py restore_check
```

This restores the newest verified backup into a separate temporary database,
compares counts for important tables, reports PASS or FAIL, and removes that
temporary database. To check a particular backup:

```powershell
.\venv\Scripts\python.exe .\manage.py restore_check --file "C:\path\to\artyflakes_pos_2026-11-08_2230.dump"
```

Do not use the live database name as the scratch database.

## Disaster recovery into a new database

These are deliberate, manual steps. First make sure the application is
stopped and preserve the original database and its files. Do **not** create a
database with the live database's name.

1. Install PostgreSQL and configure `BACKUP_PG_BIN` if needed. Open
   PowerShell in the project folder and create a **new, empty** database with
   a distinct name (replace the database user as needed):

   ```powershell
   & "C:\Program Files\PostgreSQL\16\bin\createdb.exe" -U postgres artyflakes_pos_recovered
   ```

   If `createdb.exe` is not present in your PostgreSQL installation, create
   the database with pgAdmin instead. Do not copy a password into the command.
2. Restore the selected dump into that new database. Replace the tool and
   backup paths as needed; PostgreSQL will request a password if required:

   ```powershell
   & "C:\Program Files\PostgreSQL\16\bin\pg_restore.exe" -U postgres --exit-on-error --dbname artyflakes_pos_recovered "C:\path\to\artyflakes_pos_2026-11-08_2230.dump"
   ```
3. Make a separate copy of `.env` for recovery. Change `DB_NAME` to
   `artyflakes_pos_recovered` and check the connection settings. Do not point
   the application back at the original database until recovery is verified.
4. With the recovery `.env` active, check for unapplied migrations:

   ```powershell
   .\venv\Scripts\python.exe .\manage.py migrate --check
   ```

5. Test the application and its records before resuming business.

Restoring over the live database is a manual, deliberate action. The backup
and restore-check commands in this project never drop, overwrite or restore
over the live database.
