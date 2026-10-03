from datetime import datetime, timezone


def utcnow() -> datetime:
    """Текущее время UTC без tzinfo: колонки DateTime хранят naive-UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
