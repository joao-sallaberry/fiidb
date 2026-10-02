import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    start_year: int
    cache_dir: Path
    seeds_dir: Path


def load() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "postgresql://fiidb:fiidb@localhost:5432/fiidb"),
        start_year=int(os.environ.get("FIIDB_START_YEAR", "2016")),
        cache_dir=Path(os.environ.get("FIIDB_CACHE_DIR", Path.home() / ".cache" / "fiidb")),
        seeds_dir=Path(os.environ.get("FIIDB_SEEDS_DIR", Path(__file__).parents[3] / "seeds")),
    )
