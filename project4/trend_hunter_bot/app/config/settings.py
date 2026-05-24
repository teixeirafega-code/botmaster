from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """Raised when runtime configuration is invalid."""


@dataclass(frozen=True)
class AppSettings:
    name: str = "Trend Hunter Bot"
    environment: str = "production"
    paper_mode: bool = True
    cycle_interval_minutes: int = 30
    trend_score_threshold: float = 70.0
    state_db_path: Path = Path("trend_hunter.db")
    log_file: Path = Path("logs/trend_hunter.log")
    log_level: str = "INFO"


@dataclass(frozen=True)
class GoogleTrendsSettings:
    enabled: bool = True
    pn: str = "united_states"
    geo: str = "US"
    hl: str = "en-US"
    tz: int = 360
    timeframe: str = "now 7-d"
    max_results: int = 25
    timeout_seconds: int = 20


@dataclass(frozen=True)
class RedditSettings:
    enabled: bool = True
    subreddits: tuple[str, ...] = ("technology", "business", "startups", "marketing", "worldnews")
    max_posts_per_subreddit: int = 25
    min_score: int = 25
    user_agent: str = "TrendHunterBot/1.0"
    client_id: str | None = None
    client_secret: str | None = None
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class TwitterSettings:
    enabled: bool = True
    queries: tuple[str, ...] = (
        "launch OR launches",
        "breakthrough OR emerging",
        "AI startup OR new app",
        "viral product OR trending product",
    )
    max_results_per_query: int = 50
    min_engagement: int = 20
    since_hours: int = 12
    nitter_instances: tuple[str, ...] = (
        "https://nitter.net",
        "https://nitter.poast.org",
    )


@dataclass(frozen=True)
class TikTokSettings:
    enabled: bool = True
    hashtags: tuple[str, ...] = ("tech", "aitools", "smallbusiness", "sidehustle", "startups")
    max_videos_per_hashtag: int = 30
    max_trending_videos: int = 50
    request_timeout_seconds: int = 15
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )


@dataclass(frozen=True)
class MonitorSettings:
    google_trends: GoogleTrendsSettings = field(default_factory=GoogleTrendsSettings)
    reddit: RedditSettings = field(default_factory=RedditSettings)
    twitter: TwitterSettings = field(default_factory=TwitterSettings)
    tiktok: TikTokSettings = field(default_factory=TikTokSettings)


@dataclass(frozen=True)
class ScoringSettings:
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "growth_velocity": 0.35,
            "search_volume": 0.25,
            "social_engagement": 0.25,
            "commercial_potential": 0.15,
        }
    )
    max_growth_velocity: float = 250.0
    max_search_volume: float = 100.0
    max_social_engagement: float = 10000.0
    platform_boost_per_extra_platform: float = 5.0
    max_platform_boost: float = 12.0
    commercial_keywords: tuple[str, ...] = (
        "buy",
        "deal",
        "discount",
        "review",
        "software",
        "tool",
        "app",
        "course",
        "template",
        "agency",
        "ai",
        "crypto",
        "fitness",
        "finance",
        "travel",
        "business",
        "startup",
        "jobs",
        "shop",
    )
    emerging_keywords: tuple[str, ...] = (
        "new",
        "launch",
        "beta",
        "viral",
        "breaking",
        "trend",
        "fastest",
        "rising",
        "exploding",
    )


@dataclass(frozen=True)
class DomainSettings:
    enabled: bool = True
    tlds: tuple[str, ...] = ("com", "io", "ai", "co")
    max_candidates_per_trend: int = 6
    register_max_per_trend: int = 1
    godaddy_api_key: str | None = None
    godaddy_api_secret: str | None = None
    godaddy_base_url: str = "https://api.godaddy.com"
    shopper_id: str | None = None
    period_years: int = 1
    privacy: bool = True
    auto_renew: bool = False
    contact: dict[str, Any] = field(default_factory=dict)
    social_platforms: tuple[str, ...] = ("x", "tiktok", "instagram", "youtube")


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool = True
    bot_token: str | None = None
    chat_id: str | None = None
    timeout_seconds: int = 15


@dataclass(frozen=True)
class ContentSettings:
    max_ideas: int = 8
    audience: str = "early adopters, niche publishers, affiliate marketers, and founders"
    channels: tuple[str, ...] = ("blog", "short_video", "newsletter", "social_thread")


@dataclass(frozen=True)
class Settings:
    project_root: Path
    app: AppSettings = field(default_factory=AppSettings)
    monitors: MonitorSettings = field(default_factory=MonitorSettings)
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    domains: DomainSettings = field(default_factory=DomainSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    content: ContentSettings = field(default_factory=ContentSettings)

    @property
    def paper_mode(self) -> bool:
        return self.app.paper_mode

    def validate_for_runtime(self) -> None:
        if self.app.cycle_interval_minutes < 1:
            raise ConfigError("app.cycle_interval_minutes must be at least 1")
        if not 0 <= self.app.trend_score_threshold <= 100:
            raise ConfigError("app.trend_score_threshold must be between 0 and 100")
        weight_sum = sum(self.scoring.weights.values())
        if weight_sum <= 0:
            raise ConfigError("scoring weights must sum to a positive value")
        if not self.paper_mode:
            if self.domains.enabled and not (self.domains.godaddy_api_key and self.domains.godaddy_api_secret):
                raise ConfigError("GODADDY_API_KEY and GODADDY_API_SECRET are required when paper mode is disabled")
            if self.domains.enabled:
                required_contact_fields = {
                    "first_name",
                    "last_name",
                    "email",
                    "phone",
                    "address1",
                    "city",
                    "state",
                    "postal_code",
                    "country",
                }
                missing = sorted(field for field in required_contact_fields if not self.domains.contact.get(field))
                if missing:
                    raise ConfigError(f"domains.contact is missing required production fields: {', '.join(missing)}")
            if self.telegram.enabled and not (self.telegram.bot_token and self.telegram.chat_id):
                raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required when paper mode is disabled")


def load_settings(config_path: Path | None = None, env_path: Path | None = None) -> Settings:
    project_root = Path(__file__).resolve().parents[2]
    env_path = env_path or project_root / ".env"
    config_path = config_path or project_root / "config.yaml"
    _load_env_file(env_path)

    raw_config = _load_yaml(config_path)
    default_app = AppSettings()
    default_google = GoogleTrendsSettings()
    default_reddit = RedditSettings()
    default_twitter = TwitterSettings()
    default_tiktok = TikTokSettings()
    default_scoring = ScoringSettings()
    default_domains = DomainSettings()
    default_telegram = TelegramSettings()
    default_content = ContentSettings()
    app_data = raw_config.get("app", {})
    monitor_data = raw_config.get("monitors", {})
    scoring_data = raw_config.get("scoring", {})
    domain_data = raw_config.get("domains", {})
    telegram_data = raw_config.get("telegram", {})
    content_data = raw_config.get("content", {})

    app = AppSettings(
        name=str(app_data.get("name", default_app.name)),
        environment=os.getenv("APP_ENV", str(app_data.get("environment", default_app.environment))),
        paper_mode=_env_bool("PAPER_MODE", bool(app_data.get("paper_mode", default_app.paper_mode))),
        cycle_interval_minutes=int(app_data.get("cycle_interval_minutes", default_app.cycle_interval_minutes)),
        trend_score_threshold=float(app_data.get("trend_score_threshold", default_app.trend_score_threshold)),
        state_db_path=_resolve_path(project_root, app_data.get("state_db_path", default_app.state_db_path)),
        log_file=_resolve_path(project_root, app_data.get("log_file", default_app.log_file)),
        log_level=os.getenv("LOG_LEVEL", str(app_data.get("log_level", default_app.log_level))),
    )

    reddit_data = monitor_data.get("reddit", {})
    reddit = RedditSettings(
        enabled=bool(reddit_data.get("enabled", default_reddit.enabled)),
        subreddits=_tuple(reddit_data.get("subreddits", default_reddit.subreddits)),
        max_posts_per_subreddit=int(reddit_data.get("max_posts_per_subreddit", default_reddit.max_posts_per_subreddit)),
        min_score=int(reddit_data.get("min_score", default_reddit.min_score)),
        user_agent=os.getenv("REDDIT_USER_AGENT", str(reddit_data.get("user_agent", default_reddit.user_agent))),
        client_id=_env_optional("REDDIT_CLIENT_ID", reddit_data.get("client_id")),
        client_secret=_env_optional("REDDIT_CLIENT_SECRET", reddit_data.get("client_secret")),
        username=_env_optional("REDDIT_USERNAME", reddit_data.get("username")),
        password=_env_optional("REDDIT_PASSWORD", reddit_data.get("password")),
    )

    google_data = monitor_data.get("google_trends", {})
    google = GoogleTrendsSettings(
        enabled=bool(google_data.get("enabled", default_google.enabled)),
        pn=str(google_data.get("pn", default_google.pn)),
        geo=str(google_data.get("geo", default_google.geo)),
        hl=str(google_data.get("hl", default_google.hl)),
        tz=int(google_data.get("tz", default_google.tz)),
        timeframe=str(google_data.get("timeframe", default_google.timeframe)),
        max_results=int(google_data.get("max_results", default_google.max_results)),
        timeout_seconds=int(google_data.get("timeout_seconds", default_google.timeout_seconds)),
    )

    twitter_data = monitor_data.get("twitter", {})
    twitter = TwitterSettings(
        enabled=bool(twitter_data.get("enabled", default_twitter.enabled)),
        queries=_tuple(twitter_data.get("queries", default_twitter.queries)),
        max_results_per_query=int(twitter_data.get("max_results_per_query", default_twitter.max_results_per_query)),
        min_engagement=int(twitter_data.get("min_engagement", default_twitter.min_engagement)),
        since_hours=int(twitter_data.get("since_hours", default_twitter.since_hours)),
        nitter_instances=_tuple(twitter_data.get("nitter_instances", default_twitter.nitter_instances)),
    )

    tiktok_data = monitor_data.get("tiktok", {})
    tiktok = TikTokSettings(
        enabled=bool(tiktok_data.get("enabled", default_tiktok.enabled)),
        hashtags=_tuple(tiktok_data.get("hashtags", default_tiktok.hashtags)),
        max_videos_per_hashtag=int(tiktok_data.get("max_videos_per_hashtag", default_tiktok.max_videos_per_hashtag)),
        max_trending_videos=int(tiktok_data.get("max_trending_videos", default_tiktok.max_trending_videos)),
        request_timeout_seconds=int(tiktok_data.get("request_timeout_seconds", default_tiktok.request_timeout_seconds)),
        user_agent=os.getenv("TIKTOK_USER_AGENT", str(tiktok_data.get("user_agent", default_tiktok.user_agent))),
    )

    scoring = ScoringSettings(
        weights=dict(scoring_data.get("weights", default_scoring.weights)),
        max_growth_velocity=float(scoring_data.get("max_growth_velocity", default_scoring.max_growth_velocity)),
        max_search_volume=float(scoring_data.get("max_search_volume", default_scoring.max_search_volume)),
        max_social_engagement=float(scoring_data.get("max_social_engagement", default_scoring.max_social_engagement)),
        platform_boost_per_extra_platform=float(
            scoring_data.get("platform_boost_per_extra_platform", default_scoring.platform_boost_per_extra_platform)
        ),
        max_platform_boost=float(scoring_data.get("max_platform_boost", default_scoring.max_platform_boost)),
        commercial_keywords=_tuple(scoring_data.get("commercial_keywords", default_scoring.commercial_keywords)),
        emerging_keywords=_tuple(scoring_data.get("emerging_keywords", default_scoring.emerging_keywords)),
    )

    domains = DomainSettings(
        enabled=bool(domain_data.get("enabled", default_domains.enabled)),
        tlds=_tuple(domain_data.get("tlds", default_domains.tlds)),
        max_candidates_per_trend=int(domain_data.get("max_candidates_per_trend", default_domains.max_candidates_per_trend)),
        register_max_per_trend=int(domain_data.get("register_max_per_trend", default_domains.register_max_per_trend)),
        godaddy_api_key=_env_optional("GODADDY_API_KEY", domain_data.get("godaddy_api_key")),
        godaddy_api_secret=_env_optional("GODADDY_API_SECRET", domain_data.get("godaddy_api_secret")),
        godaddy_base_url=os.getenv(
            "GODADDY_BASE_URL", str(domain_data.get("godaddy_base_url", default_domains.godaddy_base_url))
        ),
        shopper_id=_env_optional("GODADDY_SHOPPER_ID", domain_data.get("shopper_id")),
        period_years=int(domain_data.get("period_years", default_domains.period_years)),
        privacy=bool(domain_data.get("privacy", default_domains.privacy)),
        auto_renew=bool(domain_data.get("auto_renew", default_domains.auto_renew)),
        contact=dict(domain_data.get("contact", default_domains.contact)),
        social_platforms=_tuple(domain_data.get("social_platforms", default_domains.social_platforms)),
    )

    telegram = TelegramSettings(
        enabled=bool(telegram_data.get("enabled", default_telegram.enabled)),
        bot_token=_env_optional("TELEGRAM_BOT_TOKEN", telegram_data.get("bot_token")),
        chat_id=_env_optional("TELEGRAM_CHAT_ID", telegram_data.get("chat_id")),
        timeout_seconds=int(telegram_data.get("timeout_seconds", default_telegram.timeout_seconds)),
    )

    content = ContentSettings(
        max_ideas=int(content_data.get("max_ideas", default_content.max_ideas)),
        audience=str(content_data.get("audience", default_content.audience)),
        channels=_tuple(content_data.get("channels", default_content.channels)),
    )

    settings = Settings(
        project_root=project_root,
        app=app,
        monitors=MonitorSettings(google_trends=google, reddit=reddit, twitter=twitter, tiktok=tiktok),
        scoring=scoring,
        domains=domains,
        telegram=telegram,
        content=content,
    )
    settings.validate_for_runtime()
    return settings


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError as exc:
        loaded = _load_simple_yaml(text)
        if not isinstance(loaded, dict):
            raise ConfigError("config.yaml must contain a YAML mapping") from exc
        return loaded
    loaded = yaml.safe_load(text) or {}
    if not isinstance(loaded, dict):
        raise ConfigError("config.yaml must contain a YAML mapping")
    return loaded


def _load_simple_yaml(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for index, raw_line in enumerate(lines):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if line.startswith("- "):
            if not isinstance(parent, list):
                raise ConfigError("fallback YAML parser only supports scalar lists under mapping keys")
            parent.append(_parse_scalar(line[2:].strip()))
            continue

        if ":" not in line:
            raise ConfigError(f"invalid config line: {raw_line}")

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            if not isinstance(parent, dict):
                raise ConfigError("fallback YAML parser only supports mappings of scalar keys")
            parent[key] = _parse_scalar(value)
            continue

        child: Any = [] if _next_content_is_list(lines, index, indent) else {}
        if not isinstance(parent, dict):
            raise ConfigError("fallback YAML parser only supports nested mappings under mapping keys")
        parent[key] = child
        stack.append((indent, child))

    return root


def _next_content_is_list(lines: list[str], current_index: int, current_indent: int) -> bool:
    for raw_line in lines[current_index + 1 :]:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        return indent > current_indent and raw_line.strip().startswith("- ")
    return False


def _parse_scalar(value: str) -> Any:
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_optional(name: str, default: Any) -> str | None:
    value = os.getenv(name)
    if value is None:
        value = default
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return tuple(str(item) for item in value)


def _resolve_path(project_root: Path, value: Any) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = project_root / path
    return path
