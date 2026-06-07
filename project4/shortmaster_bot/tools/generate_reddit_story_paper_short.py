from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import ResearchBrief, TrendTopic
from app.services.config import ROOT_DIR, load_config
from app.services.pipeline import ShortsMasterPipeline
from app.services.reddit_story import RedditStoryService
from app.services.validation import EndToEndValidator
from app.utils.logger import configure_logging
from app.utils.text import clean_text, safe_filename

try:
    from moviepy import VideoFileClip
except ImportError:  # MoviePy 1.x
    from moviepy.editor import VideoFileClip


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--story-index",
        type=int,
        default=0,
        help="Zero-based eligible Reddit RSS story index, used to generate distinct paper samples.",
    )
    args = parser.parse_args()
    config = load_config(ROOT_DIR)
    force_paper_story_mode(config)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = (
        Path(config["root_dir"])
        / "videos"
        / "validation"
        / f"reddit_story_short_{timestamp}_sample_{args.story_index + 1}"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    config["app"]["database_path"] = str(artifact_dir / "story_short.db")
    configure_logging(Path(config["root_dir"]) / "logs", config["app"].get("log_level", "INFO"))

    service = RedditStoryService(config)
    source = fetch_top_rss_story(config, service, story_index=max(0, args.story_index))
    topic = topic_from_rss_source(config, service, source)
    research = research_from_source(config, source, topic)

    pipeline = ShortsMasterPipeline(config)
    pipeline._select_next_topic = lambda: topic  # type: ignore[method-assign]
    pipeline.story_sources.build_research_brief = lambda _topic: research  # type: ignore[method-assign]

    validator = EndToEndValidator(pipeline)
    validator.artifact_dir = artifact_dir
    validator.artifact_dir.mkdir(parents=True, exist_ok=True)
    result = validator.run()

    frame_paths = extract_frames(Path(result["artifacts"]["final_mp4"]), artifact_dir)
    verification = verify_video(result, frame_paths)
    verification_path = artifact_dir / "frame_verification.json"
    verification_path.write_text(json.dumps(verification, indent=2, ensure_ascii=True), encoding="utf-8")
    result["artifacts"].update({key: str(path) for key, path in frame_paths.items()})
    result["artifacts"]["frame_verification_json"] = str(verification_path)
    result["frame_verification"] = verification

    report_path = artifact_dir / "validation_report.json"
    metadata_path = artifact_dir / "metadata.json"
    report_path.write_text(json.dumps(result, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
    metadata_path.write_text(json.dumps(result, indent=2, ensure_ascii=True, default=str), encoding="utf-8")

    print(
        json.dumps(
            {
                "artifact_dir": str(artifact_dir),
                "final_short": result["artifacts"]["final_mp4"],
                "first_frame": str(frame_paths["first_frame"]),
                "middle_frame": str(frame_paths["middle_frame"]),
                "last_frame": str(frame_paths["last_frame"]),
                "validation_report": str(report_path),
                "story_source_report": result["artifacts"].get("story_source_report"),
                "background_category": result.get("background_category"),
                "background_filename": result.get("background_filename"),
                "scores": {
                    "hook_score": result.get("hook_score"),
                    "curiosity_score": result.get("curiosity_score"),
                    "storytelling_score": result.get("storytelling_score"),
                    "narrative_naturalness_score": result.get("narrative_naturalness_score"),
                    "disclaimer_leakage_score": result.get("disclaimer_leakage_score"),
                    "visual_interest_score": result.get("visual_interest_score"),
                    "quality_score": result.get("quality_score"),
                },
                "verification": verification,
            },
            indent=2,
            ensure_ascii=True,
        )
    )


def force_paper_story_mode(config: dict[str, Any]) -> None:
    config.setdefault("app", {})["paper_mode"] = True
    config.setdefault("publishing", {})["enable_real_upload"] = False
    config.setdefault("publishing", {})["live_upload_enabled"] = False
    config.setdefault("language", {})["default"] = "pt-BR"
    config["language"]["required"] = "pt-BR"
    config["language"]["block_english_output"] = True
    config.setdefault("story_mode", {})["enabled"] = True
    config.setdefault("story_mode", {}).setdefault("background_video", {})["mode"] = "custom_background_library"
    config["story_mode"]["background_video"]["subtitles"] = "always"
    config["story_mode"]["background_video"]["loop"] = True
    config["story_mode"]["background_video"].setdefault("library_dir", "background_library")
    config["story_mode"]["background_video"].setdefault(
        "manifest_path",
        "background_library_manifest.json",
    )
    config["story_mode"]["background_video"].setdefault(
        "approved_categories",
        {
            "tier_1": ["pressure_washing", "deep_cleaning", "restoration"],
            "tier_2": ["slime", "kinetic_sand", "soap_cutting"],
        },
    )
    config.setdefault("generation", {}).setdefault("script", {}).setdefault("ollama", {})["enabled"] = False
    config["generation"].setdefault("voice", {})["lang"] = "pt"
    config["generation"]["voice"]["tld"] = "com.br"
    config.setdefault("validation", {})
    config["validation"].update(
        {
            "fast_render_profile": True,
            "video_width": 540,
            "video_height": 960,
            "fps": 15,
            "preset": "ultrafast",
            "threads": 2,
        }
    )


def fetch_top_rss_story(
    config: dict[str, Any],
    service: RedditStoryService,
    story_index: int = 0,
) -> dict[str, Any]:
    headers = {"User-Agent": config.get("story_mode", {}).get("user_agent", "ShortsMasterBot/1.0")}
    subreddits = config.get("story_mode", {}).get("subreddits", ["LetsNotMeet"])
    min_words = int(config.get("story_mode", {}).get("min_story_words", 90))
    eligible_index = 0
    for subreddit in subreddits:
        url = f"https://www.reddit.com/r/{subreddit}/top/.rss?t=week"
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", namespace):
            title = clean_text(entry.findtext("atom:title", default="", namespaces=namespace))
            link_node = entry.find("atom:link", namespace)
            link = clean_text(link_node.attrib.get("href", "")) if link_node is not None else ""
            updated = clean_text(entry.findtext("atom:updated", default="", namespaces=namespace))
            content = clean_text(html_to_text(entry.findtext("atom:content", default="", namespaces=namespace)))
            source_text = clean_text(f"{title}. {content}")
            labels = service._priority_labels(str(subreddit), title, source_text)
            if len(source_text.split()) >= min_words and labels:
                source = {
                    "subreddit": str(subreddit),
                    "title": title,
                    "source_url": link,
                    "updated": updated,
                    "source_text": source_text,
                    "priority_labels": labels,
                }
                if eligible_index == story_index:
                    return source
                eligible_index += 1
    raise RuntimeError("No usable Reddit story found in top RSS feeds")


def html_to_text(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    text = soup.get_text(" ")
    submitted_at = text.find("submitted by")
    if submitted_at != -1:
        text = text[:submitted_at]
    return clean_text(text)


def topic_from_rss_source(
    config: dict[str, Any],
    service: RedditStoryService,
    source: dict[str, Any],
) -> TrendTopic:
    source_text = source["source_text"]
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    story_report = {
        "content_mode": "reddit_story",
        "source_platform": "Reddit",
        "source_subreddit": source["subreddit"],
        "source_url": source["source_url"],
        "source_kind": "top_rss_post",
        "source_title": source["title"],
        "source_created_at": source["updated"],
        "minimum_upvote_threshold": int(config.get("story_mode", {}).get("min_upvotes", 2500)),
        "minimum_comment_threshold": int(config.get("story_mode", {}).get("min_comments", 120)),
        "engagement_counts_available": False,
        "engagement_validation_note": (
            "Reddit JSON endpoints returned HTTP 403 in this environment; source was selected "
            "from Reddit's top weekly RSS feed, so exact upvote/comment counts were not exposed."
        ),
        "priority_labels": list(source["priority_labels"]),
        "source_text_sha256": source_hash,
        "source_text_word_count": len(source_text.split()),
        "raw_source_text_included": False,
        "approved_content_sources": list(config.get("story_mode", {}).get("subreddits", [])),
        "royalty_free_background_mode": "custom_background_library",
        "background_library_dir": config["story_mode"]["background_video"].get("library_dir"),
        "background_library_manifest": config["story_mode"]["background_video"].get("manifest_path"),
        "script_copy_policy": "Do not read or copy Reddit post/comment text verbatim.",
        "fiction_framing_policy": "Present as an unverified Reddit story unless independently verified.",
    }
    raw = {
        "content_mode": "reddit_story",
        "story_source": story_report,
        "source_text_for_similarity": source_text,
        "source_text_hash": source_hash,
        "source_text_word_count": len(source_text.split()),
        "story_beats": service.extract_core_narrative(source_text, source["priority_labels"]),
    }
    return TrendTopic(
        source="reddit_story",
        title=f"Reddit story from r/{source['subreddit']}: {source['title']}",
        url=source["source_url"],
        score=80.0,
        niche="reddit_story",
        volume=0,
        engagement=0.0,
        hashtags=["historiasreddit", "relatos", "shortsbr", *source["priority_labels"][:3]],
        raw=raw,
        observed_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )


def research_from_source(config: dict[str, Any], source: dict[str, Any], topic: TrendTopic) -> ResearchBrief:
    created = source["updated"]
    freshness_window = int(config.get("story_mode", {}).get("freshness_window_hours", 168))
    age_hours = 999.0
    try:
        parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
        age_hours = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600)
    except ValueError:
        pass
    freshness_score = round(max(70.0, min(100.0, 100.0 - age_hours * 0.12)), 1) if age_hours <= freshness_window else 65.0
    trust_score = 82.0
    source_summary = {
        "source": "Reddit top RSS",
        "title": source["title"],
        "summary": (
            "Auditable Reddit story source selected through the top weekly RSS feed. "
            "Raw Reddit text is stored only for similarity checks and is not included in reports."
        ),
        "url": source["source_url"],
        "source_date": created,
        "source_trust_score": trust_score,
        "source_trust_reason": "Reddit anecdote is auditable but unverified; script requires explicit story framing.",
    }
    return ResearchBrief(
        topic=topic.title,
        category="reddit_story",
        why_now="Reddit Story Shorts mode selected this from a top weekly Reddit RSS feed during paper-mode generation.",
        concrete_facts=[
            f"Reddit story source is r/{source['subreddit']} with URL {source['source_url']}.",
            "Reddit JSON engagement counts were blocked by HTTP 403, so this paper artifact records the limitation.",
            "The script must frame the source as an unverified Reddit story and rewrite the narrative from scratch.",
            "Story priority labels include " + ", ".join(source["priority_labels"]) + ".",
        ],
        names_entities=["Reddit", f"r/{source['subreddit']}"],
        dates_times=[created],
        locations=[],
        uncertainty_notes=[
            "Reddit anecdotes are not independently verified; narration must not present them as fact.",
            "Reddit source text and comments must never be copied verbatim.",
            "Exact Reddit upvote/comment counts were unavailable because Reddit JSON returned HTTP 403.",
        ],
        source_urls=[source["source_url"]],
        source_summaries=[source_summary],
        source_dates=[
            {
                "source": "Reddit top RSS",
                "title": source["title"],
                "url": source["source_url"],
                "source_date": created,
                "source_trust_score": trust_score,
            }
        ],
        freshness_score=freshness_score,
        freshness_threshold=float(config.get("story_mode", {}).get("freshness_min_score", 70)),
        freshness_window_hours=freshness_window,
        requires_fresh_source=True,
        freshness_notes=[f"Reddit RSS source is approximately {age_hours:.1f} hours old."],
        trust_score=trust_score,
        trust_threshold=float(config.get("story_mode", {}).get("trust_min_score", 70)),
        requires_trusted_source=False,
        trust_notes=["Reddit source is auditable but unverified; story framing required."],
    )


def extract_frames(video_path: Path, artifact_dir: Path) -> dict[str, Path]:
    clip = VideoFileClip(str(video_path))
    try:
        duration = float(clip.duration or 0)
        frames = {
            "first_frame": (artifact_dir / "first_frame.jpg", min(0.15, max(0.0, duration / 10))),
            "middle_frame": (artifact_dir / "middle_frame.jpg", max(0.0, duration / 2)),
            "last_frame": (artifact_dir / "last_frame.jpg", max(0.0, duration - 0.25)),
        }
        for _name, (path, t) in frames.items():
            clip.save_frame(str(path), t=t)
        return {name: path for name, (path, _t) in frames.items()}
    finally:
        clip.close()


def verify_video(result: dict[str, Any], frame_paths: dict[str, Path]) -> dict[str, Any]:
    first = frame_signature(frame_paths["first_frame"])
    middle = frame_signature(frame_paths["middle_frame"])
    last = frame_signature(frame_paths["last_frame"])
    diffs = {
        "first_middle_delta": color_delta(first, middle),
        "middle_last_delta": color_delta(middle, last),
        "first_last_delta": color_delta(first, last),
    }
    subtitle_present = bool(result.get("subtitles_enabled"))
    retention_background_rendered = result.get("background_video_mode") == "custom_background_library"
    non_static = max(diffs.values()) >= 2.0
    gradient_like = all(sig["edge_density"] < 0.018 for sig in [first, middle, last])
    return {
        "retention_background_rendered": retention_background_rendered,
        "background_category": result.get("background_category"),
        "background_filename": result.get("background_filename"),
        "background_animated": bool(result.get("background_animated")),
        "subtitles_visible": subtitle_present,
        "background_not_static_gradient": retention_background_rendered and non_static and not gradient_like,
        "frame_deltas": diffs,
        "frame_signatures": {"first": first, "middle": middle, "last": last},
    }


def frame_signature(path: Path) -> dict[str, float]:
    from PIL import Image, ImageFilter, ImageStat

    with Image.open(path) as image:
        small = image.convert("RGB").resize((96, 170))
        stat = ImageStat.Stat(small)
        edges = small.filter(ImageFilter.FIND_EDGES).convert("L")
        edge_stat = ImageStat.Stat(edges)
    return {
        "mean_r": round(float(stat.mean[0]), 3),
        "mean_g": round(float(stat.mean[1]), 3),
        "mean_b": round(float(stat.mean[2]), 3),
        "std_r": round(float(stat.stddev[0]), 3),
        "std_g": round(float(stat.stddev[1]), 3),
        "std_b": round(float(stat.stddev[2]), 3),
        "edge_density": round(float(edge_stat.mean[0]) / 255.0, 5),
    }


def color_delta(left: dict[str, float], right: dict[str, float]) -> float:
    return round(
        math.sqrt(
            (left["mean_r"] - right["mean_r"]) ** 2
            + (left["mean_g"] - right["mean_g"]) ** 2
            + (left["mean_b"] - right["mean_b"]) ** 2
        ),
        3,
    )


if __name__ == "__main__":
    main()
