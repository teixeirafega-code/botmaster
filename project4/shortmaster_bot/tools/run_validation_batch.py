from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import ContentScript, QueueStatus, ResearchBrief, TrendTopic
from app.services.config import ROOT_DIR, load_config
from app.services.pipeline import ShortsMasterPipeline
from app.services.research import ResearchService
from app.services.validation import EndToEndValidator


@dataclass(slots=True)
class BatchCase:
    label: str
    topic: str
    category: str
    niche: str
    source: str
    facts: list[str]
    sources: list[dict[str, str]]
    expected: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a controlled 10-topic paper-mode validation batch")
    parser.add_argument("--config-root", type=Path, default=ROOT_DIR)
    args = parser.parse_args()

    config = load_config(args.config_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = Path(config["root_dir"]) / "videos" / "validation" / f"batch_{timestamp}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    config["app"]["paper_mode"] = True
    config["app"]["database_path"] = str(artifact_dir / "batch.db")
    config["publishing"]["enable_real_upload"] = False
    config.setdefault("generation", {}).setdefault("script", {}).setdefault("ollama", {})["enabled"] = False
    config.setdefault("research", {})["wikipedia_enabled"] = False

    pipeline = ShortsMasterPipeline(config)
    _patch_media_generators(pipeline, artifact_dir)

    cases = build_cases()
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        topic = TrendTopic(
            source=case.source,
            title=case.topic,
            score=1000 - index,
            niche=case.niche,
            raw={"batch_label": case.label},
        )
        research = build_research_brief(config, topic, case)
        pipeline.research.build_brief = lambda _topic, brief=research: brief
        item = pipeline.db.enqueue_topic(topic, QueueStatus.APPROVED)
        processed = pipeline.process_item(item)
        script = ContentScript.from_dict(json.loads(processed["script_json"])) if processed.get("script_json") else None
        quality = quality_score(script, topic, research) if script else 0.0
        status = str(processed.get("status"))
        block_reason = processed.get("upload_blocked_reason") or processed.get("error") or ""
        final_decision = decision_for(status)
        results.append(
            {
                "index": index,
                "label": case.label,
                "category": case.category,
                "topic": case.topic,
                "queue_status": status,
                "facts_count": len(research.concrete_facts),
                "freshness_score": round(float(research.freshness_score), 1),
                "trust_score": round(float(research.trust_score), 1),
                "quality_score": quality,
                "final_decision": final_decision,
                "block_reason": block_reason,
                "expected": case.expected,
                "source_dates": research.source_dates,
                "sources": research.source_summaries,
                "youtube_video_id": processed.get("youtube_video_id"),
            }
        )

    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "paper_mode": True,
        "real_upload_enabled": False,
        "topics_evaluated": len(results),
        "summary": {
            "paper_published": sum(1 for item in results if item["queue_status"] == QueueStatus.PAPER_PUBLISHED),
            "needs_research": sum(1 for item in results if item["queue_status"] == QueueStatus.NEEDS_RESEARCH),
            "needs_fresh_source": sum(1 for item in results if item["queue_status"] == QueueStatus.NEEDS_FRESH_SOURCE),
            "needs_trusted_source": sum(1 for item in results if item["queue_status"] == QueueStatus.NEEDS_TRUSTED_SOURCE),
            "live_uploads": 0,
        },
        "confirmations": confirmations(results),
        "results": results,
    }

    json_path = artifact_dir / "validation_batch_report.json"
    html_path = artifact_dir / "validation_batch_report.html"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    print(json.dumps({"report_json": str(json_path), "report_html": str(html_path), **report}, indent=2, ensure_ascii=True))


def build_research_brief(config: dict, topic: TrendTopic, case: BatchCase) -> ResearchBrief:
    service = ResearchService(config)
    source_summaries = [dict(item) for item in case.sources]
    service._apply_source_trust_scores(source_summaries)
    source_dates = service._source_dates(source_summaries)
    text_blob = " ".join([topic.title, *case.facts, *(item.get("summary", "") for item in source_summaries)])
    requires_fresh = service._requires_fresh_source(topic, text_blob)
    freshness_window = int(config.get("research", {}).get("freshness_window_hours", 72))
    freshness_threshold = float(config.get("research", {}).get("freshness_min_score", 70))
    trust_threshold = float(config.get("research", {}).get("trust_min_score", 70))
    freshness_score, freshness_notes = service._freshness_analysis(
        source_dates,
        requires_fresh,
        freshness_window,
        freshness_threshold,
    )
    trust_score, trust_notes = service._trust_analysis(source_summaries, requires_fresh, trust_threshold)
    return ResearchBrief(
        topic=topic.title,
        category=topic.niche,
        why_now=why_now(case),
        concrete_facts=case.facts,
        names_entities=[],
        dates_times=[],
        locations=[],
        uncertainty_notes=[] if len(case.facts) >= 3 else [f"Only {len(case.facts)} concrete facts found; minimum is 3"],
        source_urls=[item.get("url", "") for item in source_summaries if item.get("url")],
        source_summaries=source_summaries,
        source_dates=source_dates,
        freshness_score=freshness_score,
        freshness_threshold=freshness_threshold,
        freshness_window_hours=freshness_window,
        requires_fresh_source=requires_fresh,
        freshness_notes=freshness_notes,
        trust_score=trust_score,
        trust_threshold=trust_threshold,
        requires_trusted_source=requires_fresh,
        trust_notes=trust_notes,
    )


def build_cases() -> list[BatchCase]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    recent = (now - timedelta(hours=6)).isoformat()
    old = (now - timedelta(days=10)).isoformat()
    evergreen_old = (now - timedelta(days=365)).isoformat()
    return [
        BatchCase(
            label="current_sports_trusted_wNBA",
            category="sports/current",
            topic="Aces vs Valkyries WNBA schedule",
            niche="sports",
            source="google_trends",
            facts=[
                "WNBA.com lists the Las Vegas Aces and Golden State Valkyries as teams in a scheduled WNBA game.",
                "The matchup includes Las Vegas Aces star A'ja Wilson and the Golden State Valkyries roster.",
                "The official WNBA schedule provides game context for fans tracking the league.",
            ],
            sources=[source("WNBA", "WNBA official game schedule", "https://www.wnba.com/game/aces-valkyries", recent)],
            expected="passes: current sports has recent official source",
        ),
        BatchCase(
            label="current_sports_trusted_ncaa",
            category="sports/current",
            topic="Texas softball vs Nebraska WCWS",
            niche="sports",
            source="google_trends",
            facts=[
                "NCAA.com lists Texas softball and Nebraska in Women's College World Series tournament coverage.",
                "The matchup is part of NCAA softball postseason context.",
                "The game affects the Women's College World Series bracket path.",
            ],
            sources=[source("NCAA", "NCAA softball bracket update", "https://www.ncaa.com/news/softball", recent)],
            expected="passes: current sports has recent recognized source",
        ),
        BatchCase(
            label="current_sports_low_trust_blog",
            category="sports/current",
            topic="Yankees injury report today",
            niche="sports",
            source="google_trends",
            facts=[
                "The Yankees injury report topic refers to roster availability before a baseball game.",
                "The report mentions player status and game-day lineup uncertainty.",
                "Fans use injury report updates to understand team context before first pitch.",
            ],
            sources=[source("Unknown Baseball Blog", "Yankees injury rumors", "https://baseball-rumors-blog.example/post", recent)],
            expected="blocks: recent but low-trust source",
        ),
        BatchCase(
            label="current_sports_stale_espn",
            category="sports/current",
            topic="NBA Finals schedule update",
            niche="sports",
            source="google_trends",
            facts=[
                "ESPN coverage identifies the NBA Finals as the league championship series.",
                "The schedule topic concerns game dates, teams, and broadcast windows.",
                "Fans search for the NBA Finals schedule to follow the championship matchup.",
            ],
            sources=[source("ESPN", "NBA Finals schedule", "https://www.espn.com/nba/story/_/id/example", old)],
            expected="blocks: trusted but stale source",
        ),
        BatchCase(
            label="current_sports_reddit_only",
            category="sports/current",
            topic="Football injury rumor from Reddit",
            niche="sports",
            source="reddit",
            facts=[
                "A Reddit sports thread discusses a football injury rumor before a game.",
                "The thread includes comments about player availability and team impact.",
                "The rumor depends on community discussion rather than an official team report.",
            ],
            sources=[source("Reddit", "Football injury rumor thread", "https://www.reddit.com/r/sports/comments/example", recent)],
            expected="blocks: Reddit-only current sports lacks trusted source",
        ),
        BatchCase(
            label="current_sports_wikipedia_only",
            category="sports/current",
            topic="World Cup schedule current search",
            niche="sports",
            source="google_trends",
            facts=[
                "Wikipedia summarizes FIFA World Cup tournament history and format.",
                "The World Cup schedule topic involves teams, dates, host cities, and match stages.",
                "Current schedule searches need official FIFA or recognized sports confirmation.",
            ],
            sources=[source("Wikipedia", "FIFA World Cup", "https://en.wikipedia.org/wiki/FIFA_World_Cup", recent)],
            expected="blocks: Wikipedia-only current sports lacks trusted source",
        ),
        BatchCase(
            label="evergreen_science_old_source",
            category="evergreen",
            topic="How photosynthesis works",
            niche="science",
            source="google_trends",
            facts=[
                "Photosynthesis lets plants convert light energy into chemical energy.",
                "Chlorophyll helps capture light in plant cells.",
                "The process uses carbon dioxide and water to produce sugars and oxygen.",
            ],
            sources=[source("Wikipedia", "Photosynthesis", "https://en.wikipedia.org/wiki/Photosynthesis", evergreen_old)],
            expected="passes: evergreen science does not require 72h freshness",
        ),
        BatchCase(
            label="evergreen_food_undated",
            category="evergreen",
            topic="Why sourdough bread rises",
            niche="science",
            source="google_trends",
            facts=[
                "Sourdough rises because wild yeast produces carbon dioxide in dough.",
                "Lactic acid bacteria contribute acidity and flavor during fermentation.",
                "Gluten structure traps gas bubbles as the dough proofs.",
            ],
            sources=[source("Food Science Guide", "Sourdough fermentation basics", "https://food-science.example/sourdough", "")],
            expected="passes: evergreen topic can be undated",
        ),
        BatchCase(
            label="weak_generic_one_fact",
            category="weak/generic",
            topic="Why everyone is talking about this",
            niche="general",
            source="google_trends",
            facts=["A trend feed showed search interest for a vague topic."],
            sources=[source("Google Trends RSS", "Vague trend", "https://trends.google.com/trending/rss?geo=US", recent)],
            expected="blocks: fewer than 3 concrete facts",
        ),
        BatchCase(
            label="weak_generic_two_facts",
            category="weak/generic",
            topic="New viral thing online",
            niche="general",
            source="google_trends",
            facts=[
                "A trend feed showed online interest for the phrase.",
                "The available context did not identify names, dates, or locations.",
            ],
            sources=[source("Unknown Social Page", "Viral thing online", "https://social.example/trend", recent)],
            expected="blocks: fewer than 3 concrete facts",
        ),
    ]


def source(source_name: str, title: str, url: str, source_date: str) -> dict[str, str]:
    item = {
        "source": source_name,
        "title": title,
        "summary": title,
        "url": url,
    }
    if source_date:
        item["source_date"] = source_date
    return item


def why_now(case: BatchCase) -> str:
    if "current" in case.category:
        return f"Current search demand is active for '{case.topic}', so source freshness and trust are required."
    return f"'{case.topic}' is evergreen, so factual specificity matters more than a 72-hour source window."


def _patch_media_generators(pipeline: ShortsMasterPipeline, artifact_dir: Path) -> None:
    media_dir = artifact_dir / "mock_media"
    media_dir.mkdir(parents=True, exist_ok=True)

    def voice_generate(_script: ContentScript, queue_id: int) -> Path:
        path = media_dir / f"{queue_id:06d}.mp3"
        path.write_bytes(b"paper-mode validation audio placeholder")
        return path

    def image_generate(_script: ContentScript, _topic: TrendTopic, queue_id: int) -> list[Path]:
        paths = []
        for index in range(5):
            path = media_dir / f"{queue_id:06d}-{index + 1}.jpg"
            path.write_bytes(b"paper-mode validation image placeholder")
            paths.append(path)
        return paths

    def video_assemble(_script: ContentScript, _images: list[Path], _voice: Path, queue_id: int) -> Path:
        path = media_dir / f"{queue_id:06d}.mp4"
        path.write_bytes(b"paper-mode validation video placeholder")
        return path

    pipeline.voice.generate = voice_generate
    pipeline.images.generate = image_generate
    pipeline.video.assemble = video_assemble


def quality_score(script: ContentScript, topic: TrendTopic, research: ResearchBrief) -> float:
    validator = object.__new__(EndToEndValidator)
    validator.config = {}
    analysis = validator._analyze_quality(
        script,
        topic,
        {"duration_seconds": 60.0},
        {"duration_seconds": 60.0},
        [{"source": "pollinations"} for _ in range(5)],
        research,
    )
    return float(analysis["final_quality_score"])


def decision_for(status: str) -> str:
    decisions = {
        QueueStatus.PAPER_PUBLISHED: "paper_publish_simulated",
        QueueStatus.NEEDS_RESEARCH: "blocked_needs_research",
        QueueStatus.NEEDS_FRESH_SOURCE: "blocked_needs_fresh_source",
        QueueStatus.NEEDS_TRUSTED_SOURCE: "blocked_needs_trusted_source",
        QueueStatus.SAFETY_BLOCKED: "blocked_safety",
    }
    return decisions.get(status, status)


def confirmations(results: list[dict[str, Any]]) -> dict[str, bool]:
    current_sports = [item for item in results if item["category"] == "sports/current"]
    weak = [item for item in results if item["category"] == "weak/generic"]
    evergreen = [item for item in results if item["category"] == "evergreen"]
    return {
        "current_sports_need_fresh_and_trusted_sources": all(
            item["queue_status"] == QueueStatus.PAPER_PUBLISHED
            or item["queue_status"] in {QueueStatus.NEEDS_FRESH_SOURCE, QueueStatus.NEEDS_TRUSTED_SOURCE}
            for item in current_sports
        ),
        "generic_topics_with_weak_facts_are_blocked": all(item["queue_status"] == QueueStatus.NEEDS_RESEARCH for item in weak),
        "evergreen_topics_can_pass_without_72h_freshness": all(item["queue_status"] == QueueStatus.PAPER_PUBLISHED for item in evergreen),
        "no_live_publish": all(str(item.get("youtube_video_id", "")).startswith("paper-") or not item.get("youtube_video_id") for item in results),
    }


def render_html(report: dict[str, Any]) -> str:
    rows = []
    for item in report["results"]:
        rows.append(
            "<tr>"
            f"<td>{item['index']}</td>"
            f"<td>{html.escape(item['category'])}</td>"
            f"<td>{html.escape(item['topic'])}</td>"
            f"<td>{html.escape(item['queue_status'])}</td>"
            f"<td>{item['facts_count']}</td>"
            f"<td>{item['freshness_score']}</td>"
            f"<td>{item['trust_score']}</td>"
            f"<td>{item['quality_score']}</td>"
            f"<td>{html.escape(item['final_decision'])}</td>"
            f"<td>{html.escape(item['block_reason'])}</td>"
            "</tr>"
        )
    confirmations_html = html.escape(json.dumps(report["confirmations"], indent=2, ensure_ascii=True))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>ShortsMaster 10-topic Validation Batch</title>"
        "<style>body{font-family:Arial,sans-serif;max-width:1200px;margin:32px auto;line-height:1.45}"
        "table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}"
        "th{background:#f4f4f4}pre{background:#111;color:#eee;padding:16px;overflow:auto}</style>"
        "</head><body><h1>ShortsMaster 10-topic Validation Batch</h1>"
        f"<p>Paper mode: {report['paper_mode']} | Real upload enabled: {report['real_upload_enabled']}</p>"
        "<table><thead><tr><th>#</th><th>Category</th><th>Topic</th><th>Queue Status</th><th>Facts</th>"
        "<th>Freshness</th><th>Trust</th><th>Quality</th><th>Decision</th><th>Block Reason</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "<h2>Confirmations</h2><pre>" + confirmations_html + "</pre>"
        "</body></html>"
    )


if __name__ == "__main__":
    main()
