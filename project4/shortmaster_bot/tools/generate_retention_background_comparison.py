from __future__ import annotations

import json
import math
import shutil
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.generators.video import VideoAssembler
from app.models import ContentScript, QueueStatus, TrendTopic, utc_now_iso
from app.services.config import ROOT_DIR, load_config
from app.services.database import ShortsMasterDatabase
from app.services.validation import EndToEndValidator
from app.utils.text import safe_filename

try:
    from moviepy import AudioFileClip, VideoFileClip
except ImportError:  # MoviePy 1.x
    from moviepy.editor import AudioFileClip, VideoFileClip


TEST_CASES = [
    ("cleaning", "pressure_washing"),
    ("cleaning", "deep_cleaning"),
    ("restoration", "restoration"),
    ("restoration", "restoration"),
    ("slime", "slime"),
    ("slime", "slime"),
]


def main() -> None:
    config = load_config(ROOT_DIR)
    force_offline_paper_config(config)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = Path(config["root_dir"]) / "videos" / "validation" / f"retention_background_comparison_{timestamp}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    db = ShortsMasterDatabase(artifact_dir / "comparison.db")

    results = []
    for index, (group, category) in enumerate(TEST_CASES, start=1):
        case_dir = artifact_dir / f"{index:02d}_{group}_{category}"
        case_dir.mkdir(parents=True, exist_ok=True)
        case_config = json.loads(json.dumps(config))
        case_config["story_mode"]["background_video"]["forced_category"] = category
        case_config["storage"]["base_dir"] = str(case_dir)
        case_config["generation"]["video"].update({"width": 360, "height": 640, "fps": 12, "preset": "ultrafast", "threads": 2})

        script = comparison_script(group, category, index)
        topic = TrendTopic(
            source="offline_retention_test",
            title=f"Relato do Reddit teste {group} {index}",
            score=100 + index,
            niche="reddit_story",
            raw={"content_mode": "reddit_story", "background_category_test": category},
        )
        queued = db.enqueue_topic(topic, QueueStatus.APPROVED)
        queue_id = int(queued["id"])

        audio_path = case_dir / f"{queue_id:06d}-{safe_filename(script.title)}.wav"
        write_test_audio(audio_path, duration=14.0)
        assembler = VideoAssembler(case_config)
        video_path = assembler.assemble(script, [], audio_path, queue_id)
        copied_video = case_dir / "final_short.mp4"
        if video_path != copied_video:
            shutil.copy2(video_path, copied_video)

        audio_probe = probe_audio(audio_path)
        video_probe = probe_video(copied_video)
        video_probe.update(
            {
                "background_mode": assembler.last_background_mode,
                "background_category": assembler.last_background_category,
                "background_tier": assembler.last_background_tier,
                "background_animated": assembler.last_background_animated,
                "subtitles_enabled": assembler.last_subtitles_enabled,
                "background_looped": assembler.last_background_looped,
            }
        )
        image_probe = [
            {
                "source": "procedural_retention_background",
                "royalty_free": True,
                "background_category": assembler.last_background_category,
                "background_tier": assembler.last_background_tier,
                "animated": assembler.last_background_animated,
            }
        ]
        validator = object.__new__(EndToEndValidator)
        validator.config = case_config
        quality = validator._analyze_quality(script, topic, audio_probe, video_probe, image_probe)
        frames = extract_frames(copied_video, case_dir)
        verification = verify_frames(frames, video_probe)

        db.update_queue_item(
            queue_id,
            status=QueueStatus.READY,
            script_json=json.dumps(script.to_dict(), ensure_ascii=True, sort_keys=True),
            video_path=str(copied_video),
            background_category=assembler.last_background_category,
            quality_score=float(quality["final_quality_score"]),
            safety_json=json.dumps({"quality": quality, "verification": verification}, ensure_ascii=True, sort_keys=True),
        )
        db.mark_published(queue_id, f"paper-background-test-{queue_id}", paper_mode=True)
        db.record_metrics(queue_id, f"paper-background-test-{queue_id}", niche="reddit_story", views=0, likes=0, comments=0)

        item_report = {
            "queue_id": queue_id,
            "group": group,
            "category_used": assembler.last_background_category,
            "background_tier": assembler.last_background_tier,
            "final_short": str(copied_video),
            "visual_interest_score": quality["visual_interest_score"],
            "retention_score": quality["retention_score"],
            "quality_score": quality["final_quality_score"],
            "verification": verification,
        }
        (case_dir / "validation_report.json").write_text(
            json.dumps({**item_report, "quality_analysis": quality}, indent=2, ensure_ascii=True, default=str),
            encoding="utf-8",
        )
        results.append(item_report)

    comparison = {
        "generated_at": utc_now_iso(),
        "paper_mode": True,
        "real_upload_enabled": False,
        "test_matrix": {"cleaning": 2, "restoration": 2, "slime": 2},
        "results": results,
    }
    comparison_path = artifact_dir / "background_category_comparison_report.json"
    comparison_path.write_text(json.dumps(comparison, indent=2, ensure_ascii=True, default=str), encoding="utf-8")

    category_performance = {
        "generated_at": utc_now_iso(),
        "background_mode": "retention_categories",
        "approved_categories": config["story_mode"]["background_video"]["approved_categories"],
        "category_performance": db.background_category_performance(),
    }
    category_path = artifact_dir / "category_performance_report.json"
    category_path.write_text(json.dumps(category_performance, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
    reports_dir = Path(config["root_dir"]) / str(config.get("storage", {}).get("reports_dir", "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(category_path, reports_dir / "category_performance_report.json")

    print(
        json.dumps(
            {
                "artifact_dir": str(artifact_dir),
                "comparison_report": str(comparison_path),
                "category_performance_report": str(category_path),
                "results": results,
            },
            indent=2,
            ensure_ascii=True,
        )
    )


def force_offline_paper_config(config: dict[str, Any]) -> None:
    config.setdefault("app", {})["paper_mode"] = True
    config.setdefault("publishing", {})["enable_real_upload"] = False
    config.setdefault("publishing", {})["live_upload_enabled"] = False
    config.setdefault("story_mode", {})["enabled"] = True
    config.setdefault("story_mode", {}).setdefault("background_video", {})["mode"] = "retention_categories"
    config["story_mode"]["background_video"]["subtitles"] = "always"
    config["story_mode"]["background_video"]["loop"] = True
    config["story_mode"]["background_video"]["approved_categories"] = {
        "tier_1": ["pressure_washing", "deep_cleaning", "restoration"],
        "tier_2": ["slime", "kinetic_sand", "soap_cutting"],
    }
    config.setdefault("language", {})["default"] = "pt-BR"
    config["language"]["required"] = "pt-BR"
    config.setdefault("generation", {}).setdefault("script", {}).setdefault("ollama", {})["enabled"] = False


def comparison_script(group: str, category: str, index: int) -> ContentScript:
    label = {
        "pressure_washing": "limpeza com pressão",
        "deep_cleaning": "limpeza profunda",
        "restoration": "restauração",
        "slime": "slime",
    }.get(category, category)
    narration = (
        f"Este relato do Reddit tem uma pista escondida. O fundo de {label} segura a atenção enquanto a história avança. "
        "Curiosidade: a primeira pista parecia comum, mas voltou no pior momento. "
        "Primeiro, o autor achou que era coincidência. Depois fica mais tenso: o mesmo detalhe apareceu de novo. "
        "Em seguida, a sensação mudou e ninguém sabia se aquilo era alerta ou acaso. "
        "Aqui vem a revelação: a pista pequena explicava por que o relato parecia tão estranho. "
        "Final rápido: lembre da pista e trate este relato como uma releitura não verificada."
    )
    scenes = [
        {"caption": "Pista escondida", "image_prompt": f"fundo satisfatório de {label}"},
        {"caption": "Relato do Reddit", "image_prompt": f"fundo satisfatório de {label}"},
        {"caption": "Primeiro detalhe", "image_prompt": f"fundo satisfatório de {label}"},
        {"caption": "Fica mais tenso", "image_prompt": f"fundo satisfatório de {label}"},
        {"caption": "A pista volta", "image_prompt": f"fundo satisfatório de {label}"},
        {"caption": "Tudo muda", "image_prompt": f"fundo satisfatório de {label}"},
        {"caption": "A revelação", "image_prompt": f"fundo satisfatório de {label}"},
        {"caption": "Não verificado", "image_prompt": f"fundo satisfatório de {label}"},
        {"caption": "Final rápido", "image_prompt": f"fundo satisfatório de {label}"},
        {"caption": "Lembre da pista", "image_prompt": f"fundo satisfatório de {label}"},
    ]
    return ContentScript(
        title=f"Teste {index}: relato com {label}",
        hook="Este relato tem uma pista escondida.",
        narration=narration,
        scenes=scenes,
        tags=["historiasreddit", "relatos", "shortsbr", {"cleaning": "limpeza", "restoration": "restauracao", "slime": "slime"}.get(group, "teste")],
        description=f"Teste em papel com fundo de {label} para comparar retenção visual. #shortsbr",
        niche="reddit_story",
    )


def write_test_audio(path: Path, duration: float, sample_rate: int = 44100) -> None:
    total_frames = int(duration * sample_rate)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = bytearray()
        for index in range(total_frames):
            value = int(2600 * math.sin(2 * math.pi * 190 * index / sample_rate))
            frames.extend(int(value).to_bytes(2, byteorder="little", signed=True))
        handle.writeframes(bytes(frames))


def probe_audio(audio_path: Path) -> dict[str, Any]:
    clip = AudioFileClip(str(audio_path))
    try:
        return {"path": str(audio_path), "duration_seconds": round(float(clip.duration or 0), 2)}
    finally:
        clip.close()


def probe_video(video_path: Path) -> dict[str, Any]:
    clip = VideoFileClip(str(video_path))
    try:
        return {
            "path": str(video_path),
            "duration_seconds": round(float(clip.duration or 0), 2),
            "width": int(clip.w),
            "height": int(clip.h),
            "fps": round(float(clip.fps or 0), 2),
            "file_size_bytes": video_path.stat().st_size,
        }
    finally:
        clip.close()


def extract_frames(video_path: Path, case_dir: Path) -> dict[str, Path]:
    clip = VideoFileClip(str(video_path))
    try:
        duration = float(clip.duration or 0)
        frames = {
            "first_frame": (case_dir / "first_frame.jpg", min(0.2, max(0.0, duration / 10))),
            "middle_frame": (case_dir / "middle_frame.jpg", max(0.0, duration / 2)),
            "last_frame": (case_dir / "last_frame.jpg", max(0.0, duration - 0.25)),
        }
        for _name, (path, t) in frames.items():
            clip.save_frame(str(path), t=t)
        return {name: path for name, (path, _t) in frames.items()}
    finally:
        clip.close()


def verify_frames(frame_paths: dict[str, Path], video_probe: dict[str, Any]) -> dict[str, Any]:
    first = frame_signature(frame_paths["first_frame"])
    middle = frame_signature(frame_paths["middle_frame"])
    last = frame_signature(frame_paths["last_frame"])
    diffs = {
        "first_middle_delta": color_delta(first, middle),
        "middle_last_delta": color_delta(middle, last),
        "first_last_delta": color_delta(first, last),
    }
    gradient_like = all(sig["edge_density"] < 0.018 for sig in [first, middle, last])
    animated = max(diffs.values()) >= 2.0 and bool(video_probe.get("background_animated"))
    return {
        "background_animated": animated,
        "subtitles_visible": bool(video_probe.get("subtitles_enabled")),
        "background_not_static_gradient": animated and not gradient_like,
        "frame_deltas": diffs,
        "frame_signatures": {"first": first, "middle": middle, "last": last},
    }


def frame_signature(path: Path) -> dict[str, float]:
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
