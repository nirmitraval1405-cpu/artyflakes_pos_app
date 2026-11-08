from datetime import date, timedelta
import re


BACKUP_NAME_RE = re.compile(r"^artyflakes_pos_(\d{4}-\d{2}-\d{2})_(\d{4})\.dump$")


def select_backups_to_keep(
    verified_names,
    today=None,
    keep_daily=14,
    keep_weekly=8,
    keep_monthly=12,
):
    """Return verified backup names selected by daily, weekly and monthly rules."""
    today = today or date.today()
    dated = []
    for name in verified_names:
        match = BACKUP_NAME_RE.fullmatch(name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match.group(1))
            hour, minute = int(match.group(2)[:2]), int(match.group(2)[2:])
            if hour > 23 or minute > 59 or day > today:
                continue
        except ValueError:
            continue
        dated.append((day, hour, minute, name))
    dated.sort(reverse=True)

    keep = {dated[0][3]} if dated else set()
    daily_dates = []
    weekly_keys = set()
    monthly_keys = set()
    current_month = today.year * 12 + today.month - 1
    current_week_start = today - timedelta(days=today.weekday())
    for day, _, _, name in dated:
        if day not in daily_dates and len(daily_dates) < max(keep_daily, 0):
            keep.add(name)
            daily_dates.append(day)

        week_key = day.isocalendar()[:2]
        backup_week_start = day - timedelta(days=day.weekday())
        age_weeks = (current_week_start - backup_week_start).days // 7
        if 0 <= age_weeks < max(keep_weekly, 0) and week_key not in weekly_keys:
            keep.add(name)
            weekly_keys.add(week_key)

        age_months = current_month - (day.year * 12 + day.month - 1)
        month_key = day.year, day.month
        if age_months < max(keep_monthly, 0) and month_key not in monthly_keys:
            keep.add(name)
            monthly_keys.add(month_key)
    return keep


def matching_backup_names(names):
    """Filter directory entries to names this application is allowed to prune."""
    return {name for name in names if BACKUP_NAME_RE.fullmatch(name)}
