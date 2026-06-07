from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.generators.video import VideoAssembler
from app.generators.voice import VOICE_PROFILES, VoiceGenerator
from app.models import ContentScript
from app.services.background_library import BackgroundAsset, BackgroundLibraryManager
from app.services.config import ROOT_DIR, load_config
from app.utils.logger import configure_logging

try:
    from moviepy import AudioFileClip, VideoFileClip
except ImportError:  # MoviePy 1.x
    from moviepy.editor import AudioFileClip, VideoFileClip


PROFILES = ["male_storyteller", "female_storyteller", "neutral_storyteller"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=_latest_reddit_artifact(),
        help="Validated Reddit Story artifact whose script will be reused for all voices.",
    )
    args = parser.parse_args()

    source_dir = args.artifact_dir.resolve()
    script = ContentScript.from_dict(read_json(source_dir / "generated_script.json"))
    source_report = read_json(source_dir / "validation_report.json")
    config = comparison_config(load_config(ROOT_DIR))
    configure_logging(PROJECT_ROOT / "logs", config["app"].get("log_level", "INFO"))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = PROJECT_ROOT / "videos" / "validation" / f"voice_comparison_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_dir / "generated_script.json", output_dir / "generated_script.json")
    if (source_dir / "story_source_report.json").exists():
        shutil.copy2(source_dir / "story_source_report.json", output_dir / "story_source_report.json")

    manager = BackgroundLibraryManager(config)
    background = select_shared_background(manager, source_report)
    results = []
    for index, profile_name in enumerate(PROFILES, start=1):
        results.append(
            render_variant(
                config=config,
                script=script,
                profile_name=profile_name,
                variant_number=index,
                output_dir=output_dir,
                background=background,
            )
        )

    ranked = sorted(results, key=lambda item: item["comparison_score"], reverse=True)
    best = ranked[0]
    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "paper_mode": True,
        "real_upload_enabled": False,
        "upload_attempted": False,
        "same_story_used_for_all_variants": True,
        "story_title": script.title,
        "story_word_count": len(script.narration.split()),
        "shared_background_filename": background.filename,
        "comparison_formula": {
            "voice_naturalness_score": 0.45,
            "voice_engagement_score": 0.35,
            "narration_pacing_score": 0.20,
        },
        "variants": results,
        "ranking": [
            {
                "position": index,
                "voice_profile": item["voice_profile"],
                "voice_label": item["voice_label"],
                "comparison_score": item["comparison_score"],
            }
            for index, item in enumerate(ranked, start=1)
        ],
        "recommended_profile": best["voice_profile"],
        "recommended_voice": best["voice_name"],
        "recommendation": (
            f"{best['voice_label']} obteve o melhor equilíbrio entre naturalidade, "
            "envolvimento e ritmo narrativo."
        ),
    }
    json_path = output_dir / "voice_comparison_report.json"
    html_path = output_dir / "voice_comparison_report.html"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    html_path.write_text(report_html(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "comparison_report_json": str(json_path),
                "comparison_report_html": str(html_path),
                "recommended_profile": best["voice_profile"],
                "recommended_voice": best["voice_name"],
                "variants": results,
            },
            indent=2,
            ensure_ascii=True,
        )
    )


def render_variant(
    *,
    config: dict[str, Any],
    script: ContentScript,
    profile_name: str,
    variant_number: int,
    output_dir: Path,
    background: BackgroundAsset,
) -> dict[str, Any]:
    variant_dir = output_dir / profile_name
    variant_dir.mkdir(parents=True, exist_ok=True)

    voice = VoiceGenerator(config)
    voice.output_dir = variant_dir
    audio_source = voice.generate(script, 900 + variant_number, profile=profile_name)
    audio_path = variant_dir / "narration.mp3"
    shutil.copy2(audio_source, audio_path)
    voice_report_source = audio_source.with_suffix(".voice.json")
    voice_report_path = variant_dir / "voice_report.json"
    shutil.copy2(voice_report_source, voice_report_path)

    manager = BackgroundLibraryManager(config)
    manager.select = lambda excluded=None: background  # type: ignore[method-assign]
    manager.write_report = lambda: {}  # type: ignore[method-assign]
    video = VideoAssembler(config, background_library=manager)
    video.output_dir = variant_dir
    video.overlay_dir = variant_dir / "overlays"
    video.overlay_dir.mkdir(parents=True, exist_ok=True)
    rendered_path = video.assemble(script, [], audio_path, 900 + variant_number)
    final_path = variant_dir / "final_short.mp4"
    shutil.copy2(rendered_path, final_path)

    frame_paths = extract_frames(final_path, variant_dir)
    frame_deltas = calculate_frame_deltas(frame_paths)
    audio_duration = media_duration(audio_path, audio=True)
    video_duration = media_duration(final_path, audio=False)
    scores = {
        key: float(voice.last_generation_report[key])
        for key in (
            "voice_naturalness_score",
            "voice_engagement_score",
            "narration_pacing_score",
        )
    }
    comparison_score = round(
        scores["voice_naturalness_score"] * 0.45
        + scores["voice_engagement_score"] * 0.35
        + scores["narration_pacing_score"] * 0.20,
        1,
    )
    profile = VOICE_PROFILES[profile_name]
    result = {
        "voice_profile": profile_name,
        "voice_label": profile["label"],
        "voice_name": profile["voice"],
        "voice_gender": profile["gender"],
        "voice_provider": voice.last_generation_report["provider"],
        **scores,
        "comparison_score": comparison_score,
        "words_per_minute": voice.last_generation_report["words_per_minute"],
        "pause_ratio": voice.last_generation_report["pause_ratio"],
        "dynamic_variation": voice.last_generation_report["dynamic_variation"],
        "inserted_pause_seconds": voice.last_generation_report["inserted_pause_seconds"],
        "audio_duration_seconds": audio_duration,
        "video_duration_seconds": video_duration,
        "audio_video_delta_seconds": round(abs(audio_duration - video_duration), 3),
        "background_filename": background.filename,
        "background_animated": max(frame_deltas.values()) > 2.0,
        "subtitles_visible": bool(video.last_subtitles_enabled),
        "frame_deltas": frame_deltas,
        "final_short": str(final_path),
        "narration_audio": str(audio_path),
        "voice_report": str(voice_report_path),
        "first_frame": str(frame_paths["first_frame"]),
        "middle_frame": str(frame_paths["middle_frame"]),
        "last_frame": str(frame_paths["last_frame"]),
    }
    (variant_dir / "validation_report.json").write_text(
        json.dumps(
            {
                "paper_mode": True,
                "real_upload_enabled": False,
                "upload_attempted": False,
                "story_title": script.title,
                **result,
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    return result


def comparison_config(config: dict[str, Any]) -> dict[str, Any]:
    config.setdefault("app", {})["paper_mode"] = True
    config.setdefault("publishing", {})["enable_real_upload"] = False
    config["publishing"]["live_upload_enabled"] = False
    config.setdefault("generation", {}).setdefault("voice", {})["provider"] = "edge_tts"
    config["generation"]["voice"]["allow_gtts_fallback"] = False
    config.setdefault("story_mode", {})["enabled"] = True
    background = config["story_mode"].setdefault("background_video", {})
    background["mode"] = "custom_background_library"
    background["subtitles"] = "always"
    background["loop"] = True
    video = config["generation"].setdefault("video", {})
    video.update(
        {
            "width": 540,
            "height": 960,
            "fps": 15,
            "preset": "ultrafast",
            "threads": 2,
        }
    )
    return config


def select_shared_background(
    manager: BackgroundLibraryManager,
    source_report: dict[str, Any],
) -> BackgroundAsset:
    preferred = str(source_report.get("background_filename") or "")
    assets = manager.scan()["approved_assets"]
    if preferred:
        for asset in assets:
            if asset.filename == preferred:
                return asset
    if not assets:
        raise RuntimeError("No approved custom background is available")
    return assets[0]


def extract_frames(video_path: Path, output_dir: Path) -> dict[str, Path]:
    clip = VideoFileClip(str(video_path))
    try:
        duration = float(clip.duration or 0.0)
        times = {
            "first_frame": min(0.5, duration * 0.05),
            "middle_frame": duration * 0.5,
            "last_frame": max(0.0, duration - 0.5),
        }
        paths = {}
        for key, time_value in times.items():
            path = output_dir / f"{key}.jpg"
            clip.save_frame(str(path), t=time_value)
            paths[key] = path
        return paths
    finally:
        clip.close()


def calculate_frame_deltas(frame_paths: dict[str, Path]) -> dict[str, float]:
    frames = {
        key: np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
        for key, path in frame_paths.items()
    }

    def delta(left: str, right: str) -> float:
        return round(float(np.mean(np.abs(frames[left] - frames[right]))), 3)

    return {
        "first_middle_delta": delta("first_frame", "middle_frame"),
        "middle_last_delta": delta("middle_frame", "last_frame"),
        "first_last_delta": delta("first_frame", "last_frame"),
    }


def media_duration(path: Path, *, audio: bool) -> float:
    clip = AudioFileClip(str(path)) if audio else VideoFileClip(str(path))
    try:
        return round(float(clip.duration or 0.0), 3)
    finally:
        clip.close()


def report_html(report: dict[str, Any]) -> str:
    rows = []
    for variant in report["variants"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(variant['voice_label'])}</td>"
            f"<td>{html.escape(variant['voice_name'])}</td>"
            f"<td>{variant['voice_naturalness_score']:.1f}</td>"
            f"<td>{variant['voice_engagement_score']:.1f}</td>"
            f"<td>{variant['narration_pacing_score']:.1f}</td>"
            f"<td>{variant['comparison_score']:.1f}</td>"
            f"<td><a href='{html.escape(variant['voice_profile'] + '/final_short.mp4')}'>MP4</a></td>"
            "</tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Voice Variant Comparison</title>"
        "<style>body{font-family:Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:9px;text-align:left}"
        "th{background:#f4f4f4}</style></head><body>"
        "<h1>Comparação de vozes narrativas</h1>"
        f"<p><strong>História:</strong> {html.escape(report['story_title'])}</p>"
        f"<p><strong>Recomendação:</strong> {html.escape(report['recommendation'])}</p>"
        "<table><thead><tr><th>Perfil</th><th>Voz</th><th>Naturalidade</th>"
        "<th>Envolvimento</th><th>Ritmo</th><th>Total</th><th>Vídeo</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected an object in {path}")
    return payload


def _latest_reddit_artifact() -> Path:
    validation_dir = PROJECT_ROOT / "videos" / "validation"
    candidates = sorted(
        (
            path
            for path in validation_dir.glob("reddit_story_short_*")
            if (path / "generated_script.json").exists()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return validation_dir / "reddit_story_short_missing"
    return candidates[0]


if __name__ == "__main__":
    main()
