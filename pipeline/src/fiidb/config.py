import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parents[3]


@dataclass(frozen=True)
class Settings:
    database_url: str
    start_year: int
    cache_dir: Path
    seeds_dir: Path
    sheet_id: str | None
    sheet_tab: str
    google_credentials: Path


def _dotenv(path: Path) -> dict[str, str]:
    """KEY=value lines of the repository's .env (git-ignored); comments and blanks skipped."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def load() -> Settings:
    # Real environment variables win over .env.
    env = _dotenv(REPO_ROOT / ".env") | dict(os.environ)
    return Settings(
        database_url=env.get("DATABASE_URL", "postgresql://fiidb:fiidb@localhost:5432/fiidb"),
        start_year=int(env.get("FIIDB_START_YEAR", "2016")),
        cache_dir=Path(env.get("FIIDB_CACHE_DIR", Path.home() / ".cache" / "fiidb")).expanduser(),
        seeds_dir=Path(env.get("FIIDB_SEEDS_DIR", REPO_ROOT / "seeds")).expanduser(),
        sheet_id=env.get("FIIDB_SHEET_ID") or None,
        sheet_tab=env.get("FIIDB_SHEET_TAB", "fiidb"),
        google_credentials=Path(
            env.get("FIIDB_GOOGLE_CREDENTIALS", Path.home() / ".config" / "fiidb" / "google-service-account.json")
        ).expanduser(),
    )
