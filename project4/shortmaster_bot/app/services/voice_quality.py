from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.models import ContentScript

try:
    from moviepy import AudioFileClip
except ImportError:  # MoviePy 1.x
    from moviepy.editor import AudioFileClip


def analyze_voice_quality(
    script: ContentScript,
    audio_path: Path,
    generation_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = dict(generation_report or {})
    acoustic = _acoustic_metrics(audio_path)
    return score_voice_quality(
        script=script,
        duration_seconds=acoustic["duration_seconds"],
        pause_ratio=acoustic["pause_ratio"],
        dynamic_variation=acoustic["dynamic_variation"],
        generation_report=report,
    ) | {"acoustic_metrics": acoustic}


def score_voice_quality(
    script: ContentScript,
    duration_seconds: float,
    pause_ratio: float,
    dynamic_variation: float,
    generation_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = dict(generation_report or {})
    word_count = max(1, len(script.narration.split()))
    duration_minutes = max(1.0 / 60.0, float(duration_seconds) / 60.0)
    words_per_minute = word_count / duration_minutes

    pacing_speed_score = _target_score(words_per_minute, low=132.0, ideal_low=142.0, ideal_high=168.0, high=182.0)
    pause_score = _target_score(pause_ratio, low=0.035, ideal_low=0.075, ideal_high=0.19, high=0.28)
    narration_pacing_score = pacing_speed_score * 0.65 + pause_score * 0.35

    segment_count = max(1, int(report.get("segment_count") or 1))
    variation_count = max(0, int(report.get("emotional_variation_count") or 0))
    suspense_segments = max(0, int(report.get("suspense_segment_count") or 0))
    suspense_pauses = max(0, int(report.get("suspense_pause_count") or 0))
    variation_coverage = min(1.0, variation_count / segment_count)
    suspense_coverage = 1.0 if suspense_segments == 0 else min(1.0, suspense_pauses / suspense_segments)

    provider = str(report.get("provider") or "unknown").lower()
    provider_score = 97.0 if provider == "edge_tts" else 58.0 if provider == "gtts" else 72.0
    dynamic_score = _target_score(dynamic_variation, low=0.05, ideal_low=0.16, ideal_high=0.48, high=0.72)
    variation_score = min(100.0, 62.0 + variation_coverage * 38.0)

    voice_naturalness_score = (
        provider_score * 0.42
        + dynamic_score * 0.23
        + narration_pacing_score * 0.23
        + variation_score * 0.12
    )
    voice_engagement_score = (
        narration_pacing_score * 0.34
        + dynamic_score * 0.28
        + variation_score * 0.20
        + suspense_coverage * 100.0 * 0.18
    )

    findings: list[str] = []
    if provider != "edge_tts":
        findings.append("neural storyteller provider was not used")
    if words_per_minute < 132:
        findings.append("narration is slower than the conversational storytelling range")
    elif words_per_minute > 182:
        findings.append("narration is faster than the conversational storytelling range")
    if pause_ratio < 0.035:
        findings.append("narration has too little breathing room")
    elif pause_ratio > 0.28:
        findings.append("narration contains too much silence")
    if dynamic_variation < 0.05:
        findings.append("voice energy is too uniform")
    if suspense_coverage < 0.75:
        findings.append("some suspense moments lack an intentional pause")

    return {
        "voice_profile": report.get("voice_profile"),
        "voice_name": report.get("voice_name"),
        "voice_provider": provider,
        "voice_naturalness_score": round(max(0.0, min(100.0, voice_naturalness_score)), 1),
        "voice_engagement_score": round(max(0.0, min(100.0, voice_engagement_score)), 1),
        "narration_pacing_score": round(max(0.0, min(100.0, narration_pacing_score)), 1),
        "words_per_minute": round(words_per_minute, 1),
        "pause_ratio": round(float(pause_ratio), 4),
        "dynamic_variation": round(float(dynamic_variation), 4),
        "findings": findings,
    }


def _acoustic_metrics(audio_path: Path) -> dict[str, Any]:
    clip = AudioFileClip(str(audio_path))
    try:
        duration = float(clip.duration or 0.0)
        chunks = [
            np.asarray(chunk, dtype=np.float32)
            for chunk in clip.iter_chunks(
                chunk_duration=0.25,
                fps=16000,
                quantize=False,
                logger=None,
            )
            if len(chunk)
        ]
    finally:
        clip.close()

    if not chunks:
        return {
            "duration_seconds": round(duration, 3),
            "pause_ratio": 0.0,
            "dynamic_variation": 0.0,
            "window_count": 0,
        }

    rms_values: list[float] = []
    for chunk in chunks:
        mono = chunk.mean(axis=1) if chunk.ndim > 1 else chunk
        rms_values.append(float(np.sqrt(np.mean(np.square(mono)) + 1e-12)))
    rms = np.asarray(rms_values, dtype=np.float32)
    reference = float(np.percentile(rms, 75)) if len(rms) else 0.0
    silence_threshold = max(0.0025, reference * 0.12)
    pause_ratio = float(np.mean(rms <= silence_threshold))
    active = rms[rms > silence_threshold]
    if len(active) > 1 and float(np.mean(active)) > 0:
        dynamic_variation = float(np.std(active) / np.mean(active))
    else:
        dynamic_variation = 0.0
    return {
        "duration_seconds": round(duration, 3),
        "pause_ratio": round(pause_ratio, 4),
        "dynamic_variation": round(dynamic_variation, 4),
        "window_count": len(rms_values),
        "silence_threshold": round(silence_threshold, 6),
    }


def _target_score(
    value: float,
    *,
    low: float,
    ideal_low: float,
    ideal_high: float,
    high: float,
) -> float:
    value = float(value)
    if ideal_low <= value <= ideal_high:
        return 100.0
    if value < ideal_low:
        if value <= low:
            return 45.0
        return 45.0 + ((value - low) / (ideal_low - low)) * 55.0
    if value >= high:
        return 45.0
    return 100.0 - ((value - ideal_high) / (high - ideal_high)) * 55.0
