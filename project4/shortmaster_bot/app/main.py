from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from app.models import ContentScript, QueueStatus
from app.scheduler import ShortsMasterScheduler
from app.services.config import ROOT_DIR, load_config, resolve_storage_path
from app.services.pipeline import ShortsMasterPipeline
from app.services.validation import EndToEndValidator
from app.services.youtube_publish_scheduler import YouTubePublishScheduler
from app.utils.logger import configure_logging


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shortsmaster", description="Automated YouTube Shorts bot")
    parser.add_argument("--config-root", type=Path, default=ROOT_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Run 24/7 scheduler")
    sub.add_parser("once", help="Run one discovery/processing cycle")
    sub.add_parser("collect", help="Collect trends and queue the best unseen topic")

    queue_parser = sub.add_parser("queue", help="List queued content")
    queue_parser.add_argument("--status", choices=[
        QueueStatus.PENDING_APPROVAL,
        QueueStatus.APPROVED,
        QueueStatus.GENERATING,
        QueueStatus.READY,
        QueueStatus.NEEDS_RESEARCH,
        QueueStatus.NEEDS_FRESH_SOURCE,
        QueueStatus.NEEDS_TRUSTED_SOURCE,
        QueueStatus.SAFETY_BLOCKED,
        QueueStatus.PUBLISHED,
        QueueStatus.PAPER_PUBLISHED,
        QueueStatus.FAILED,
    ])
    queue_parser.add_argument("--limit", type=int, default=20)

    approve_parser = sub.add_parser("approve", help="Approve a queued topic")
    approve_parser.add_argument("queue_id", type=int)
    approve_parser.add_argument("--process", action="store_true", help="Generate and publish immediately")

    approve_live_parser = sub.add_parser("approve-live", help="Mark a generated video approved for live upload")
    approve_live_parser.add_argument("queue_id", type=int)

    process_parser = sub.add_parser("process", help="Process approved queue items")
    process_parser.add_argument("--limit", type=int, default=3)

    metrics_parser = sub.add_parser("metrics", help="Refresh YouTube metrics")
    metrics_parser.add_argument("--limit", type=int, default=50)

    checklist_parser = sub.add_parser("pre-upload-checklist", help="Generate YouTube pre-upload checklist without publishing")
    checklist_parser.add_argument("--queue-id", type=int)

    sub.add_parser("youtube-scheduler-report", help="Generate YouTube scheduler dashboard report without publishing")
    sub.add_parser("background-library-report", help="Scan approved custom backgrounds and write JSON/HTML reports")

    sub.add_parser("youtube-auth-check", help="Validate YouTube OAuth and detect channel without uploading")

    validate_parser = sub.add_parser("validate", help="Run a real paper-mode end-to-end validation")
    validate_parser.add_argument("--runs", type=int, default=1, help="Number of validation runs to execute")
    validate_parser.add_argument(
        "--force-local-images",
        action="store_true",
        help="Use local generated validation images instead of Pollinations for faster batch scoring",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config_root)
    configure_logging(
        resolve_storage_path(config, config.get("storage", {}).get("logs_dir", "logs")),
        config["app"].get("log_level", "INFO"),
    )
    pipeline = ShortsMasterPipeline(config)

    if args.command == "run":
        ShortsMasterScheduler(pipeline, config).start()
    elif args.command == "once":
        print_json(pipeline.run_cycle())
    elif args.command == "collect":
        print_json(pipeline.discover_and_queue())
    elif args.command == "queue":
        print_json(pipeline.db.list_queue(status=args.status, limit=args.limit))
    elif args.command == "approve":
        item = pipeline.approve(args.queue_id)
        if args.process:
            item = pipeline.process_item(item)
        print_json(item)
    elif args.command == "approve-live":
        print_json(pipeline.db.approve_for_live_upload(args.queue_id))
    elif args.command == "process":
        print_json({"processed": pipeline.process_approved(limit=args.limit)})
    elif args.command == "metrics":
        print_json({"updated": pipeline.metrics.refresh(limit=args.limit)})
    elif args.command == "pre-upload-checklist":
        item = pipeline.db.get_queue_item(args.queue_id) if args.queue_id else None
        topic = pipeline._topic_from_queue_item(item) if item else None
        script = ContentScript.from_dict(json.loads(item["script_json"])) if item and item.get("script_json") else None
        video_path = Path(item["video_path"]) if item and item.get("video_path") else None
        decision = pipeline.safety.evaluate_upload(
            pipeline.publisher.real_upload_enabled,
            queue_id=args.queue_id,
            topic=topic,
            script=script,
            video_path=video_path,
            queue_item=item,
        )
        print_json(decision["checklist"])
    elif args.command == "youtube-scheduler-report":
        service = YouTubePublishScheduler(pipeline, config)
        report = service.build_report()
        paths = service.write_report(report)
        print_json({"paths": paths, **report})
    elif args.command == "background-library-report":
        print_json(pipeline.background_library.write_report())
    elif args.command == "youtube-auth-check":
        print_json(pipeline.publisher.detect_authenticated_channel())
    elif args.command == "validate":
        if args.force_local_images:
            config.setdefault("validation", {})["force_local_images"] = True
        results = [EndToEndValidator(pipeline).run() for _ in range(max(1, args.runs))]
        print_json(results[0] if args.runs == 1 else results)
    else:
        raise SystemExit(f"Unknown command: {args.command}")


def print_json(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=True, default=str))


if __name__ == "__main__":
    main()
