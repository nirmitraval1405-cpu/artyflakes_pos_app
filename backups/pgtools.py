import os
import re
import shutil
from pathlib import Path


class PostgreSQLToolsError(RuntimeError):
    pass


def _version_key(path):
    match = re.search(r"(\d+(?:\.\d+)*)$", path.parent.name)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def find_pg_tools(environment=None, database_host=None, search_roots=None):
    """Find pg_dump, pg_restore and psql without relying on process-global PATH."""
    env = dict(os.environ if environment is None else environment)
    executable_names = ("pg_dump.exe", "pg_restore.exe", "psql.exe")
    if os.name != "nt":
        executable_names = tuple(name[:-4] for name in executable_names)

    candidates = []
    configured = env.get("BACKUP_PG_BIN", "").strip()
    if configured:
        configured_dir = Path(configured)
        configured_tools = {
            name: configured_dir / exe
            for name, exe in zip(("pg_dump", "pg_restore", "psql"), executable_names)
        }
        if all(path.is_file() for path in configured_tools.values()):
            return configured_tools

    path_tools = tuple(shutil.which(name, path=env.get("PATH")) for name in executable_names)
    if all(path_tools):
        return dict(zip(("pg_dump", "pg_restore", "psql"), map(Path, path_tools)))

    if configured:
        candidates.append(Path(configured))
    roots = list(search_roots or ())
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        if env.get(key):
            roots.append(Path(env[key]))

    host = (database_host or "").strip().lower()
    if host in ("", "localhost", "127.0.0.1", "::1"):
        if env.get("SystemDrive"):
            roots.append(Path(env["SystemDrive"] + "\\"))
        if search_roots is None and os.name == "nt":
            roots.extend(Path(f"{letter}:\\") for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                         if Path(f"{letter}:\\").is_dir())
        roots.extend(Path(root) for root in env.get("BACKUP_PG_DRIVE_ROOTS", "").split(os.pathsep) if root)

    version_bins = []
    seen = set()
    for root in roots:
        postgres_dir = root / "PostgreSQL"
        if postgres_dir.is_dir():
            for version_dir in postgres_dir.iterdir():
                bin_dir = version_dir / "bin"
                if bin_dir.is_dir():
                    key = str(bin_dir).casefold()
                    if key not in seen:
                        seen.add(key)
                        version_bins.append(bin_dir)
    version_bins.sort(key=_version_key, reverse=True)
    candidates.extend(version_bins)

    for directory in candidates:
        tools = {
            name: directory / exe
            for name, exe in zip(("pg_dump", "pg_restore", "psql"), executable_names)
        }
        if all(path.is_file() for path in tools.values()):
            return tools

    raise PostgreSQLToolsError(
        "PostgreSQL tools were not found. Set `BACKUP_PG_BIN` in `.env` to the "
        "folder that contains `pg_dump.exe`, `pg_restore.exe`, and `psql.exe`."
    )
