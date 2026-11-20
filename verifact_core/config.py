from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _parse_utc_timestamp(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        if not value.endswith("Z") and not value.endswith("+00:00"):
            raise ValueError(f"{field_name} must be an ISO-8601 UTC timestamp ending in Z")
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field_name} is not a valid ISO-8601 UTC timestamp: {value}") from exc
    else:
        raise ValueError(f"{field_name} must be a UTC timestamp string or datetime")

    if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise ValueError(f"{field_name} must be in UTC")
    return dt.astimezone(timezone.utc)


def _validate_slug(value: str, field_name: str) -> str:
    if not value or not SLUG_RE.match(value):
        raise ValueError(f"{field_name} must be a valid kebab-case slug, got {value!r}")
    return value


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class T0Config(StrictFrozenModel):
    utc: datetime
    source_url: str | None = None
    note: str | None = None

    @field_validator("utc", mode="before")
    @classmethod
    def validate_utc(cls, value: Any) -> datetime:
        return _parse_utc_timestamp(value, "t0.utc")


class WindowConfig(StrictFrozenModel):
    pre_hours: int = Field(ge=0)
    post_hours: int = Field(ge=0)
    live: bool = False


class OfficialConfig(StrictFrozenModel):
    accounts: list[str] = Field(default_factory=list)
    posts: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    spokespeople: list[str] = Field(default_factory=list)


class EntitiesConfig(StrictFrozenModel):
    org: dict[str, list[str]] = Field(default_factory=dict)
    product: dict[str, list[str]] = Field(default_factory=dict)
    related: list[str] = Field(default_factory=list)


class KeywordsConfig(StrictFrozenModel):
    core: list[str] = Field(min_length=1)
    context: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class XSourceConfig(StrictFrozenModel):
    enabled: bool
    max_posts: int | None = Field(default=None, gt=0)
    thread_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_max_posts_when_enabled(self) -> XSourceConfig:
        if self.enabled and self.max_posts is None:
            raise ValueError("sources.x.max_posts is required when sources.x.enabled is true")
        return self


class RedditSourceConfig(StrictFrozenModel):
    enabled: bool
    subreddits: list[str] = Field(default_factory=list)


class HNSourceConfig(StrictFrozenModel):
    enabled: bool


class YouTubeSourceConfig(StrictFrozenModel):
    enabled: bool
    max_videos: int | None = Field(default=None, gt=0)


class NewsSourceConfig(StrictFrozenModel):
    enabled: bool
    gdelt: bool = True
    google_news_rss: bool = True


class ManualSourceConfig(StrictFrozenModel):
    enabled: bool
    platforms: list[str] = Field(default_factory=list)


class SourcesConfig(StrictFrozenModel):
    x: XSourceConfig | None = None
    reddit: RedditSourceConfig | None = None
    hn: HNSourceConfig | None = None
    youtube: YouTubeSourceConfig | None = None
    news: NewsSourceConfig | None = None
    manual: ManualSourceConfig | None = None

    @model_validator(mode="after")
    def check_enabled_sources_present(self) -> SourcesConfig:
        blocks = [self.x, self.reddit, self.hn, self.youtube, self.news, self.manual]
        present = [b for b in blocks if b is not None]
        if not present or not any(b.enabled for b in present):
            raise ValueError("sources must define at least one enabled source block")
        return self


class ManualTurningPoint(StrictFrozenModel):
    id: str
    label: str
    at_utc: datetime | None = None
    confirmed: bool = False
    form: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_slug(value, "turning_points.manual[].id")

    @field_validator("at_utc", mode="before")
    @classmethod
    def validate_at_utc(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        return _parse_utc_timestamp(value, "turning_points.manual[].at_utc")


class TurningPointsConfig(StrictFrozenModel):
    detect_official_response: bool = True
    manual: list[ManualTurningPoint] = Field(default_factory=list)


class SeedStoryline(StrictFrozenModel):
    id: str
    name: str
    seeds: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_slug(value, "seed_storylines[].id")


class ClaimToCheck(StrictFrozenModel):
    id: str
    text: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_slug(value, "claims_to_check[].id")


class ConflictOfInterest(StrictFrozenModel):
    person: str
    relationship: str


class PrivacyConfig(StrictFrozenModel):
    min_followers_to_name: int | None = None


class EventConfig(StrictFrozenModel):
    event_id: str
    name: str
    t0: T0Config
    window: WindowConfig
    official: OfficialConfig
    entities: EntitiesConfig = Field(default_factory=EntitiesConfig)
    keywords: KeywordsConfig
    languages: list[str] = Field(default_factory=list)
    sources: SourcesConfig
    turning_points: TurningPointsConfig = Field(default_factory=TurningPointsConfig)
    seed_storylines: list[SeedStoryline] = Field(default_factory=list)
    claims_to_check: list[ClaimToCheck] = Field(default_factory=list)
    conflicts_of_interest: list[ConflictOfInterest] = Field(default_factory=list)
    known_coverage: list[str] = Field(default_factory=list)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)

    @field_validator("event_id")
    @classmethod
    def validate_event_id_slug(cls, value: str) -> str:
        return _validate_slug(value, "event_id")

    @model_validator(mode="after")
    def validate_unique_ids(self) -> EventConfig:
        seen_storylines: set[str] = set()
        for item in self.seed_storylines:
            if item.id in seen_storylines:
                raise ValueError(f"Duplicate id in seed_storylines: {item.id!r}")
            seen_storylines.add(item.id)

        seen_claims: set[str] = set()
        for claim in self.claims_to_check:
            if claim.id in seen_claims:
                raise ValueError(f"Duplicate id in claims_to_check: {claim.id!r}")
            seen_claims.add(claim.id)

        seen_turning_points: set[str] = set()
        for tp in self.turning_points.manual:
            if tp.id in seen_turning_points:
                raise ValueError(f"Duplicate id in turning_points.manual: {tp.id!r}")
            seen_turning_points.add(tp.id)

        return self


def load_event(path: str | Path) -> EventConfig:
    """Load and validate an event YAML config file."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Event config file not found: {file_path}")

    raw_data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict):
        raise ValueError(f"Event config must be a YAML mapping: {file_path}")

    config = EventConfig.model_validate(raw_data)
    expected_stem = file_path.stem
    if config.event_id != expected_stem:
        raise ValueError(
            f"event_id {config.event_id!r} does not match filename {expected_stem!r}"
        )
    return config
