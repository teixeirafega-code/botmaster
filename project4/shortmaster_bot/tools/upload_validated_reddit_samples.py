from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import ContentScript, QueueStatus, TrendTopic
from app.services.config import ROOT_DIR, load_config
from app.services.pipeline import ShortsMasterPipeline
from app.utils.logger import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload validated Reddit Story PAPER_MODE artifacts as private YouTube videos."
    )
    parser.add_argument("artifact_dirs", nargs="+", type=Path)
    args = parser.parse_args()

    config = load_config(ROOT_DIR)
    config.setdefault("app", {})["paper_mode"] = False
    config.setdefault("publishing", {})["enable_real_upload"] = True
    config["publishing"]["live_upload_enabled"] = True
    config["publishing"]["privacy_status"] = "private"
    configure_logging(PROJECT_ROOT / "logs", config["app"].get("log_level", "INFO"))

    pipeline = ShortsMasterPipeline(config)
    channel = pipeline.publisher.detect_authenticated_channel()
    if not channel["matches_configured_channel"]:
        raise SystemExit(
            "Authenticated YouTube channel does not match the configured channel; no upload attempted."
        )

    results: list[dict[str, Any]] = []
    failed = False
    for artifact_dir in args.artifact_dirs:
        artifact_dir = artifact_dir.resolve()
        try:
            result = upload_artifact(pipeline, artifact_dir)
        except Exception as exc:
            failed = True
            result = {
                "artifact_dir": str(artifact_dir),
                "status": "failed",
                "error": str(exc),
            }
        results.append(result)
        print(json.dumps(result, indent=2, ensure_ascii=True, default=str), flush=True)

    summary = {
        "channel": channel,
        "privacy_status": "private",
        "persistent_upload_settings_changed": False,
        "results": results,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=True, default=str), flush=True)
    if failed:
        raise SystemExit(1)


def upload_artifact(
    pipeline: ShortsMasterPipeline,
    artifact_dir: Path,
) -> dict[str, Any]:
    report = read_json(artifact_dir / "validation_report.json")
    script_payload = read_json(artifact_dir / "generated_script.json")
    research = read_json(artifact_dir / "research_brief.json")
    source_report = read_json(artifact_dir / "story_source_report.json")
    script = ContentScript.from_dict(script_payload)
    video_path = artifact_dir / "final_short.mp4"

    validate_artifact(report, video_path)
    topic = TrendTopic(
        source=str(report.get("trend_source") or "reddit"),
        title=str(report["selected_topic"]),
        score=float(report.get("trend_score") or 0.0),
        niche=str(report.get("niche") or "reddit_story"),
        url=str(source_report.get("source_url") or report.get("story_source_url") or ""),
        hashtags=list(script.tags),
        raw={
            "content_mode": "reddit_story",
            "source_url": source_report.get("source_url"),
            "source_subreddit": source_report.get("source_subreddit"),
            "source_title": source_report.get("source_title"),
        },
    )

    item = pipeline.db.enqueue_topic(topic, status=QueueStatus.READY)
    queue_id = int(item["id"])
    if (
        item.get("status") == QueueStatus.PUBLISHED
        and item.get("youtube_video_id")
        and not str(item["youtube_video_id"]).startswith("paper-")
    ):
        return {
            "artifact_dir": str(artifact_dir),
            "status": "already_uploaded",
            "queue_id": queue_id,
            "youtube_video_id": str(item["youtube_video_id"]),
            "youtube_url": f"https://youtu.be/{item['youtube_video_id']}",
            "privacy_status": "private",
        }

    content_decision = pipeline.safety.evaluate_content(queue_id, topic, script)
    if not content_decision["allowed"]:
        raise RuntimeError(
            "Content safety validation failed: " + "; ".join(content_decision["reasons"])
        )

    safety_payload = {
        **content_decision,
        "safety_score": float(report["safety_score"]),
        "artifact_validation_report": str(artifact_dir / "validation_report.json"),
    }
    pipeline.db.update_queue_item(
        queue_id,
        status=QueueStatus.READY,
        research_json=json.dumps(research, ensure_ascii=True, sort_keys=True),
        script_json=json.dumps(script.to_dict(), ensure_ascii=True, sort_keys=True),
        video_path=str(video_path),
        background_category=report.get("background_category"),
        background_filename=report.get("background_filename"),
        background_source_url=report.get("background_source_url"),
        background_license_type=report.get("background_license_type"),
        background_commercial_rights_verified=int(
            report.get("background_commercial_rights_verified") is True
        ),
        quality_score=float(report["quality_score"]),
        safety_json=json.dumps(safety_payload, ensure_ascii=True, sort_keys=True),
        upload_blocked_reason=None,
        error=None,
    )
    item = pipeline.db.approve_for_live_upload(queue_id)

    upload_decision = pipeline.safety.evaluate_upload(
        pipeline.publisher.real_upload_enabled,
        queue_id=queue_id,
        topic=topic,
        script=script,
        video_path=video_path,
        queue_item=item,
    )
    checklist = upload_decision["checklist"]
    checklist_path = artifact_dir / "youtube_private_upload_checklist.json"
    checklist_path.write_text(
        json.dumps(checklist, indent=2, ensure_ascii=True, default=str),
        encoding="utf-8",
    )
    if not upload_decision["allowed"]:
        reason = "; ".join(upload_decision["reasons"])
        pipeline.db.update_queue_item(
            queue_id,
            status=QueueStatus.READY,
            upload_blocked_reason=reason,
            error=reason,
        )
        raise RuntimeError(f"Private upload blocked by safety gates: {reason}")

    pipeline.db.increment_upload_attempt(queue_id)
    try:
        youtube_id = pipeline.publisher.publish(video_path, script, item)
    except Exception as exc:
        pipeline.db.mark_upload_failure(queue_id, str(exc))
        pipeline.db.record_scheduler_event(
            "upload",
            "failure",
            queue_id=queue_id,
            reason=str(exc),
            payload={"artifact_dir": str(artifact_dir), "privacy_status": "private"},
        )
        raise

    result = {
        "artifact_dir": str(artifact_dir),
        "status": "uploaded",
        "queue_id": queue_id,
        "youtube_video_id": youtube_id,
        "youtube_url": f"https://youtu.be/{youtube_id}",
        "privacy_status": "private",
        "title": script.title,
        "quality_score": float(report["quality_score"]),
        "safety_score": float(report["safety_score"]),
        "checklist": str(checklist_path),
    }
    result_path = artifact_dir / "youtube_upload_result.json"
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=True, default=str),
        encoding="utf-8",
    )

    pipeline.db.record_youtube_quota_usage(
        queue_id,
        "videos.insert",
        int(pipeline.config.get("publishing", {}).get("youtube_upload_quota_units", 1600)),
    )
    pipeline.db.mark_published(queue_id, youtube_id, paper_mode=False)
    pipeline.db.record_scheduler_event(
        "upload",
        "success",
        queue_id=queue_id,
        payload=result,
    )
    return result


def validate_artifact(report: dict[str, Any], video_path: Path) -> None:
    failures: list[str] = []
    if not video_path.exists() or video_path.stat().st_size <= 0:
        failures.append(f"video missing or empty: {video_path}")
    if float(report.get("quality_score") or 0.0) < 75:
        failures.append("quality_score is below 75")
    if float(report.get("safety_score") or 0.0) < 90:
        failures.append("safety_score is below 90")
    if float(report.get("narrative_naturalness_score") or 0.0) < 75:
        failures.append("narrative_naturalness_score is below 75")
    if float(report.get("disclaimer_leakage_score") or 0.0) != 0:
        failures.append("disclaimer leakage was detected")
    if not bool(report.get("background_animated")):
        failures.append("background animation was not confirmed")
    if not bool(report.get("subtitles_enabled")):
        failures.append("subtitles are not enabled")
    if report.get("background_commercial_rights_verified") is not True:
        failures.append("background commercial rights are not verified")
    for field in (
        "script_language",
        "narration_language",
        "subtitle_language",
        "title_language",
    ):
        if str(report.get(field, "")).lower() != "pt-br":
            failures.append(f"{field} is not pt-BR")
    if report.get("failures"):
        failures.append("validation report contains failures")
    if failures:
        raise RuntimeError("Artifact validation failed: " + "; ".join(failures))


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


if __name__ == "__main__":
    main()
