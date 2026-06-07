from __future__ import annotations

import html
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import TrendTopic
from app.services.config import ROOT_DIR, load_config
from app.services.pipeline import ShortsMasterPipeline
from app.services.validation import EndToEndValidator
from app.utils.logger import configure_logging


def main() -> None:
    config = load_config(ROOT_DIR)
    enforce_paper_mode(config)
    configure_for_repeatable_quality(config)
    configure_logging(Path(config["root_dir"]) / "logs", config["app"].get("log_level", "INFO"))

    topic = evergreen_topic()
    pipeline = ShortsMasterPipeline(config)
    pipeline.trends.collect = lambda: [topic]  # type: ignore[method-assign]
    pipeline.selector.select_best = lambda topics: topics[0] if topics else None  # type: ignore[method-assign]

    validator = EndToEndValidator(pipeline)
    package_dir = package_path(config, topic.title)
    validator.artifact_dir = package_dir
    validator.artifact_dir.mkdir(parents=True, exist_ok=True)
    result = validator.run()

    summary = build_package_summary(config, result, package_dir)
    write_package_files(package_dir, result, summary)

    print(json.dumps(summary, indent=2, ensure_ascii=True))


def enforce_paper_mode(config: dict[str, Any]) -> None:
    app = config.get("app", {})
    publishing = config.get("publishing", {})
    if not bool(app.get("paper_mode", True)):
        raise SystemExit("Refusing to run: PAPER_MODE must remain true for this package generation.")
    if bool(publishing.get("enable_real_upload", False)):
        raise SystemExit("Refusing to run: ENABLE_REAL_UPLOAD must remain false.")
    if bool(publishing.get("live_upload_enabled", False)):
        raise SystemExit("Refusing to run: LIVE_UPLOAD_ENABLED must remain false.")


def configure_for_repeatable_quality(config: dict[str, Any]) -> None:
    config.setdefault("generation", {}).setdefault("script", {}).setdefault("ollama", {})["enabled"] = False
    config.setdefault("validation", {})
    config["validation"].update(
        {
            "fast_render_profile": True,
            "image_width": 720,
            "image_height": 1280,
            "video_width": 720,
            "video_height": 1280,
            "fps": 15,
            "preset": "ultrafast",
            "threads": 2,
            "force_local_images": True,
        }
    )


def evergreen_topic() -> TrendTopic:
    return TrendTopic(
        source="curated_evergreen",
        title="James Webb Space Telescope",
        score=88.0,
        niche="science",
        url="https://science.nasa.gov/mission/webb/",
        volume=0,
        engagement=0.0,
        hashtags=["jwst", "space", "science", "nasa", "shorts"],
        raw={
            "link": "https://science.nasa.gov/mission/webb/",
            "news_items": [
                {
                    "news_item_source": "NASA official site",
                    "news_item_title": "James Webb Space Telescope mission overview",
                    "news_item_url": "https://science.nasa.gov/mission/webb/",
                    "news_item_publish_date": "2025-01-01T00:00:00+00:00",
                    "news_item_snippet": (
                        "NASA reported that Webb launched on December 25, 2021. "
                        "NASA reported that Webb's mirror is 6.5 meters wide and has 18 segments. "
                        "NASA reported that Webb works near L2, about 1 million miles from Earth."
                    ),
                },
                {
                    "news_item_source": "ESA official site",
                    "news_item_title": "Webb infrared observatory overview",
                    "news_item_url": "https://www.esa.int/Science_Exploration/Space_Science/Webb",
                    "news_item_publish_date": "2025-01-01T00:00:00+00:00",
                    "news_item_snippet": (
                        "ESA reported that Webb observes infrared light from the early universe. "
                        "ESA reported that NASA leads Webb with ESA and CSA as partners."
                    ),
                },
            ],
        },
    )


def package_path(config: dict[str, Any], topic_title: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(char.lower() if char.isalnum() else "-" for char in topic_title).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return Path(config["root_dir"]) / "videos" / "validation" / f"paper_high_quality_{slug}_{stamp}"


def build_package_summary(config: dict[str, Any], result: dict[str, Any], package_dir: Path) -> dict[str, Any]:
    quality = float(result.get("quality_score", 0.0) or 0.0)
    facts_count = int(result.get("research_fact_count", 0) or 0)
    trust_score = float(result.get("trust_score", 0.0) or 0.0)
    trust_threshold = float(result.get("trust_threshold", 70.0) or 70.0)
    freshness_score = float(result.get("freshness_score", 0.0) or 0.0)
    freshness_threshold = float(result.get("freshness_threshold", 70.0) or 70.0)
    freshness_required = bool(result.get("freshness_required", False))
    upload_id = str(result.get("upload_simulation_result", ""))
    summary = {
        "package_dir": str(package_dir),
        "paper_mode": bool(config.get("app", {}).get("paper_mode", True)),
        "enable_real_upload": bool(config.get("publishing", {}).get("enable_real_upload", False)),
        "live_upload_enabled": bool(config.get("publishing", {}).get("live_upload_enabled", False)),
        "upload_attempted": False,
        "selected_topic": result.get("selected_topic"),
        "title": result.get("generated_title"),
        "description": result.get("generated_description"),
        "tags": result.get("generated_hashtags"),
        "queue_status": result.get("queue_status"),
        "upload_simulation_result": upload_id,
        "facts_count": facts_count,
        "facts_count_pass": facts_count >= 3,
        "quality_score": quality,
        "quality_score_pass": quality >= 75.0,
        "trust_score": trust_score,
        "trust_threshold": trust_threshold,
        "trust_score_pass": trust_score >= trust_threshold,
        "freshness_score": freshness_score,
        "freshness_required": freshness_required,
        "freshness_threshold": freshness_threshold,
        "freshness_pass": (not freshness_required) or freshness_score >= freshness_threshold,
        "final_mp4": result.get("artifacts", {}).get("final_mp4"),
        "validation_report_json": result.get("artifacts", {}).get("validation_report_json"),
        "validation_report_html": result.get("artifacts", {}).get("validation_report_html"),
        "research_report": str(package_dir / "research_report.json"),
        "source_list": str(package_dir / "source_list.json"),
        "pre_upload_checklist_json": str(package_dir / "youtube_pre_upload_checklist.json"),
        "pre_upload_checklist_html": str(package_dir / "youtube_pre_upload_checklist.html"),
    }
    summary["success"] = all(
        [
            summary["paper_mode"],
            not summary["enable_real_upload"],
            not summary["live_upload_enabled"],
            summary["upload_attempted"] is False,
            summary["facts_count_pass"],
            summary["quality_score_pass"],
            summary["trust_score_pass"],
            summary["freshness_pass"],
            upload_id.startswith("paper-"),
        ]
    )
    return summary


def write_package_files(package_dir: Path, result: dict[str, Any], summary: dict[str, Any]) -> None:
    (package_dir / "title.txt").write_text(str(result.get("generated_title", "")), encoding="utf-8")
    (package_dir / "description.txt").write_text(str(result.get("generated_description", "")), encoding="utf-8")
    (package_dir / "tags.json").write_text(
        json.dumps(result.get("generated_hashtags", []), indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    (package_dir / "source_list.json").write_text(
        json.dumps(source_list(result), indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    research_report = {
        "research_brief": result.get("research_brief", {}),
        "facts_count": result.get("research_fact_count"),
        "trust_score": result.get("trust_score"),
        "freshness_score": result.get("freshness_score"),
        "sources": source_list(result),
    }
    (package_dir / "research_report.json").write_text(
        json.dumps(research_report, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    (package_dir / "package_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    (package_dir / "package_summary.html").write_text(render_summary_html(summary), encoding="utf-8")
    copy_pre_upload_checklist(package_dir)


def source_list(result: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = result.get("research_brief", {}).get("source_summaries", [])
    output: list[dict[str, Any]] = []
    for item in summaries:
        output.append(
            {
                "source": item.get("source"),
                "title": item.get("title"),
                "url": item.get("url"),
                "source_date": item.get("source_date"),
                "source_trust_score": item.get("source_trust_score"),
                "source_trust_reason": item.get("source_trust_reason"),
                "summary": item.get("summary"),
            }
        )
    return output


def copy_pre_upload_checklist(package_dir: Path) -> None:
    reports = PROJECT_ROOT / "reports"
    for name in ["youtube_pre_upload_checklist.json", "youtube_pre_upload_checklist.html"]:
        source = reports / name
        if source.exists():
            shutil.copy2(source, package_dir / name)


def render_summary_html(summary: dict[str, Any]) -> str:
    rows = []
    for key, value in summary.items():
        rows.append(
            "<tr>"
            f"<th>{html.escape(str(key))}</th>"
            f"<td>{html.escape(json.dumps(value, ensure_ascii=True) if isinstance(value, (list, dict)) else str(value))}</td>"
            "</tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>ShortsMaster Paper Video Package Summary</title>"
        "<style>body{font-family:Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left}"
        "th{background:#f4f4f4;width:280px}</style></head><body>"
        "<h1>ShortsMaster Paper Video Package Summary</h1>"
        f"<table>{''.join(rows)}</table></body></html>"
    )


if __name__ == "__main__":
    main()
