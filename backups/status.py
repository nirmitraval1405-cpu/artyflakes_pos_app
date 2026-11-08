import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone


def read_backup_status():
    path = Path(settings.BACKUP_DIR) / "status.json"
    try:
        with path.open(encoding="utf-8") as status_file:
            status = json.load(status_file)
        return status if isinstance(status, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def backup_warning(status, stale_hours, now=None):
    """Return a safe admin-facing warning, or an empty string when healthy."""
    status = status if isinstance(status, dict) else {}
    if status.get("error"):
        return "The last backup attempt failed. Run a backup now."
    if status.get("copy_status") == "failed":
        return "The backup copy could not be made. Check the backup drive."
    last_success = _parse_datetime(status.get("last_success"))
    if last_success is None:
        return "No successful database backup is recorded. Run a backup now."
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.get_current_timezone())
    age = now - last_success
    if age.total_seconds() > max(stale_hours, 0) * 3600:
        days = max(1, age.days)
        return f"The last successful backup was {days} day{'s' if days != 1 else ''} ago. Run a backup now."
    return ""
