from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from gtts import gTTS

from app.models import ContentScript
from app.services.config import resolve_storage_path
from app.services.voice_quality import analyze_voice_quality
from app.utils.text import safe_filename

try:
    from moviepy import AudioClip, AudioFileClip, concatenate_audioclips
except ImportError:  # MoviePy 1.x
    from moviepy.editor import AudioClip, AudioFileClip, concatenate_audioclips


LOGGER = logging.getLogger(__name__)

VOICE_PROFILES: dict[str, dict[str, Any]] = {
    "male_storyteller": {
        "label": "Narrador masculino",
        "voice": "pt-BR-AntonioNeural",
        "gender": "male",
        "rate": "-4%",
        "pitch": "-3Hz",
        "hook_rate": "-2%",
        "hook_pitch": "-1Hz",
        "suspense_rate": "-9%",
        "suspense_pitch": "-5Hz",
        "ending_rate": "-6%",
        "pause_seconds": 0.18,
        "hook_pause_seconds": 0.30,
        "suspense_pause_seconds": 0.46,
        "ending_pause_seconds": 0.30,
    },
    "female_storyteller": {
        "label": "Narradora feminina",
        "voice": "pt-BR-FranciscaNeural",
        "gender": "female",
        "rate": "-1%",
        "pitch": "+0Hz",
        "hook_rate": "+0%",
        "hook_pitch": "+1Hz",
        "suspense_rate": "-7%",
        "suspense_pitch": "-2Hz",
        "ending_rate": "-4%",
        "pause_seconds": 0.16,
        "hook_pause_seconds": 0.28,
        "suspense_pause_seconds": 0.42,
        "ending_pause_seconds": 0.28,
    },
    "neutral_storyteller": {
        "label": "Narrador neutro",
        "voice": "pt-BR-ThalitaMultilingualNeural",
        "gender": "neutral",
        "rate": "-3%",
        "pitch": "+0Hz",
        "hook_rate": "-1%",
        "hook_pitch": "+0Hz",
        "suspense_rate": "-6%",
        "suspense_pitch": "-2Hz",
        "ending_rate": "-5%",
        "pause_seconds": 0.20,
        "hook_pause_seconds": 0.30,
        "suspense_pause_seconds": 0.40,
        "ending_pause_seconds": 0.30,
    },
}

SUSPENSE_MARKERS = (
    "até que",
    "quando",
    "mas ",
    "então",
    "de repente",
    "escuro",
    "ouvi",
    "percebi",
    "travou",
    "segurava",
    "perguntou",
    "correu",
    "sumiu",
    "desaparec",
    "assassin",
    "arma",
    "navalha",
    "gritou",
    "pancad",
    "quebrou",
    "avançou",
)


class VoiceGenerator:
    def __init__(self, config: dict):
        self.config = config
        self.voice_config = config.get("generation", {}).get("voice", {})
        videos_dir = resolve_storage_path(config, config.get("storage", {}).get("videos_dir", "videos"))
        self.output_dir = videos_dir / "voice"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.last_generation_report: dict[str, Any] = {}

    def generate(
        self,
        script: ContentScript,
        queue_id: int,
        profile: str | None = None,
    ) -> Path:
        profile_name = str(
            profile
            or self.voice_config.get("default_profile")
            or "neutral_storyteller"
        )
        profile_config = self._profile(profile_name)
        filename = (
            f"{queue_id:06d}-{safe_filename(script.title)}-"
            f"{safe_filename(profile_name)}.mp3"
        )
        output_path = self.output_dir / filename
        provider = str(self.voice_config.get("provider", "edge_tts")).lower()
        if provider == "edge_tts":
            try:
                self._generate_edge_story(script, output_path, profile_name, profile_config)
                return output_path
            except Exception:
                if not bool(self.voice_config.get("allow_gtts_fallback", True)):
                    raise
                LOGGER.exception(
                    "Neural storyteller voice failed; falling back to gTTS for %s",
                    profile_name,
                )
        return self._generate_gtts_fallback(script, output_path, profile_name, profile_config)

    def available_profiles(self) -> dict[str, dict[str, Any]]:
        return {
            name: dict(values)
            for name, values in VOICE_PROFILES.items()
        }

    def _profile(self, profile_name: str) -> dict[str, Any]:
        if profile_name not in VOICE_PROFILES:
            raise ValueError(
                f"Unknown voice profile '{profile_name}'. "
                f"Available profiles: {', '.join(sorted(VOICE_PROFILES))}"
            )
        merged = dict(VOICE_PROFILES[profile_name])
        overrides = self.voice_config.get("profiles", {}).get(profile_name, {})
        if isinstance(overrides, dict):
            merged.update(overrides)
        return merged

    def _generate_edge_story(
        self,
        script: ContentScript,
        output_path: Path,
        profile_name: str,
        profile: dict[str, Any],
    ) -> None:
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError("edge-tts is required for neural storyteller voices") from exc

        segments = self._story_segments(script.narration, profile)
        LOGGER.info(
            "Generating neural pt-BR narration: profile=%s voice=%s segments=%s",
            profile_name,
            profile["voice"],
            len(segments),
        )
        with tempfile.TemporaryDirectory(prefix="shortsmaster-voice-", dir=self.output_dir) as temp_name:
            temp_dir = Path(temp_name)
            segment_paths: list[Path] = []
            for index, segment in enumerate(segments, start=1):
                segment_path = temp_dir / f"segment-{index:03d}.mp3"
                communicator = edge_tts.Communicate(
                    text=segment["text"],
                    voice=str(profile["voice"]),
                    rate=str(segment["rate"]),
                    pitch=str(segment["pitch"]),
                    volume="+0%",
                )
                asyncio.run(communicator.save(str(segment_path)))
                if not segment_path.exists() or segment_path.stat().st_size <= 0:
                    raise RuntimeError(f"Neural voice segment {index} was not generated")
                segment_paths.append(segment_path)
            self._join_segments(segment_paths, segments, output_path)

        report = self._generation_report(
            profile_name=profile_name,
            profile=profile,
            provider="edge_tts",
            segments=segments,
        )
        report.update(analyze_voice_quality(script, output_path, report))
        self.last_generation_report = report
        self._write_report(output_path, report)

    def _generate_gtts_fallback(
        self,
        script: ContentScript,
        output_path: Path,
        profile_name: str,
        profile: dict[str, Any],
    ) -> Path:
        lang = self.voice_config.get("lang", "pt")
        tld = self.voice_config.get("tld", "com.br")
        slow = bool(self.voice_config.get("slow", False))
        segments = self._story_segments(script.narration, profile)
        prepared_text = " ... ".join(segment["text"] for segment in segments)
        LOGGER.info("Generating fallback narration with gTTS: %s", output_path.name)
        tts = gTTS(text=prepared_text, lang=lang, tld=tld, slow=slow)
        tts.save(str(output_path))
        report = self._generation_report(
            profile_name=profile_name,
            profile=profile,
            provider="gtts",
            segments=segments,
        )
        report.update(analyze_voice_quality(script, output_path, report))
        self.last_generation_report = report
        self._write_report(output_path, report)
        return output_path

    def _story_segments(
        self,
        narration: str,
        profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", narration.strip())
            if sentence.strip()
        ]
        if not sentences:
            sentences = [narration.strip()]
        segments: list[dict[str, Any]] = []
        for index, sentence in enumerate(sentences):
            lower = sentence.lower()
            is_hook = index == 0
            is_ending = index == len(sentences) - 1
            is_suspense = any(marker in lower for marker in SUSPENSE_MARKERS)
            rate = str(profile["rate"])
            pitch = str(profile["pitch"])
            pause = float(profile["pause_seconds"])
            role = "story"
            if is_hook:
                rate = str(profile["hook_rate"])
                pitch = str(profile["hook_pitch"])
                pause = float(profile["hook_pause_seconds"])
                role = "hook"
            elif is_ending:
                rate = str(profile["ending_rate"])
                pause = float(profile["ending_pause_seconds"])
                role = "ending"
            elif is_suspense:
                rate = str(profile["suspense_rate"])
                pitch = str(profile["suspense_pitch"])
                pause = float(profile["suspense_pause_seconds"])
                role = "suspense"
            elif index % 3 == 1:
                rate = self._adjust_rate(rate, -1)
                role = "reflective"
            segments.append(
                {
                    "text": sentence,
                    "role": role,
                    "rate": rate,
                    "pitch": pitch,
                    "pause_after_seconds": pause if not is_ending else 0.0,
                }
            )
        return segments

    def _join_segments(
        self,
        segment_paths: list[Path],
        segments: list[dict[str, Any]],
        output_path: Path,
    ) -> None:
        clips: list[Any] = []
        opened: list[Any] = []
        try:
            for path, segment in zip(segment_paths, segments):
                clip = AudioFileClip(str(path))
                clips.append(clip)
                opened.append(clip)
                pause = float(segment["pause_after_seconds"])
                if pause > 0:
                    silence = AudioClip(
                        lambda t: (
                            np.array([0.0, 0.0], dtype=np.float32)
                            if np.isscalar(t)
                            else np.zeros((len(t), 2), dtype=np.float32)
                        ),
                        duration=pause,
                        fps=44100,
                    )
                    clips.append(silence)
                    opened.append(silence)
            final = concatenate_audioclips(clips)
            opened.append(final)
            final.write_audiofile(
                str(output_path),
                fps=44100,
                codec="libmp3lame",
                bitrate="192k",
                logger=None,
            )
        finally:
            for clip in reversed(opened):
                close = getattr(clip, "close", None)
                if callable(close):
                    close()

    def _generation_report(
        self,
        *,
        profile_name: str,
        profile: dict[str, Any],
        provider: str,
        segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        suspense_segments = [segment for segment in segments if segment["role"] == "suspense"]
        varied_segments = [
            segment
            for segment in segments
            if segment["rate"] != profile["rate"]
            or segment["pitch"] != profile["pitch"]
        ]
        return {
            "provider": provider,
            "voice_profile": profile_name,
            "voice_label": profile["label"],
            "voice_name": profile["voice"],
            "voice_gender": profile["gender"],
            "language": "pt-BR",
            "segment_count": len(segments),
            "suspense_segment_count": len(suspense_segments),
            "suspense_pause_count": sum(
                1
                for segment in suspense_segments
                if float(segment["pause_after_seconds"]) >= 0.35
            ),
            "emotional_variation_count": len(varied_segments),
            "inserted_pause_seconds": round(
                sum(float(segment["pause_after_seconds"]) for segment in segments),
                3,
            ),
            "segments": segments,
        }

    def _write_report(self, output_path: Path, report: dict[str, Any]) -> None:
        report_path = output_path.with_suffix(".voice.json")
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=True, default=str),
            encoding="utf-8",
        )

    def _adjust_rate(self, rate: str, delta: int) -> str:
        match = re.fullmatch(r"([+-]?)(\d+)%", rate.strip())
        if not match:
            return rate
        value = int(match.group(2))
        if match.group(1) == "-":
            value *= -1
        value += int(delta)
        return f"{value:+d}%"
