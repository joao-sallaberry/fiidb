from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# B3 and CVM publish on Brasília time; "today" must not depend on the host's timezone.
MARKET_TZ = ZoneInfo("America/Sao_Paulo")


def today() -> date:
    return datetime.now(MARKET_TZ).date()


def now() -> datetime:
    return datetime.now(UTC)


def parse_date(value: str | None) -> date | None:
    """Parse YYYY-MM-DD, YYYYMMDD or DD/MM/YYYY; None for empty or invalid values."""
    value = (value or "").strip()
    try:
        if "/" in value:
            day, month, year = value.split("/")
            return date(int(year), int(month), int(day))
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
