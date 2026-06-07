from __future__ import annotations

import logging
import math
import random
import re
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.models import ContentScript
from app.services.background_library import BackgroundAsset, BackgroundLibraryManager
from app.services.config import resolve_storage_path
from app.utils.text import clean_text, safe_filename, wrap_for_display

try:
    from moviepy import AudioFileClip, CompositeVideoClip, ImageClip, VideoClip, VideoFileClip, concatenate_videoclips
except ImportError:  # MoviePy 1.x
    from moviepy.editor import AudioFileClip, CompositeVideoClip, ImageClip, VideoClip, VideoFileClip, concatenate_videoclips


LOGGER = logging.getLogger(__name__)


class VideoAssemblyError(RuntimeError):
    """Raised when the video renderer cannot assemble a complete Short."""


class VideoAssembler:
    def __init__(self, config: dict, background_library: BackgroundLibraryManager | None = None):
        self.config = config
        self.video_config = config.get("generation", {}).get("video", {})
        videos_dir = resolve_storage_path(config, config.get("storage", {}).get("videos_dir", "videos"))
        self.output_dir = videos_dir / "rendered"
        self.overlay_dir = videos_dir / "overlays"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.overlay_dir.mkdir(parents=True, exist_ok=True)
        self.width = int(self.video_config.get("width", 1080))
        self.height = int(self.video_config.get("height", 1920))
        self.fps = int(self.video_config.get("fps", 30))
        story_video = config.get("story_mode", {}).get("background_video", {})
        self.story_background_config = story_video
        self.last_background_mode = "image_sequence"
        self.last_background_category = ""
        self.last_background_tier = ""
        self.last_background_animated = False
        self.last_subtitles_enabled = False
        self.last_background_looped = False
        self.last_background_trimmed = False
        self.last_background_filename = ""
        self.last_background_source_url = ""
        self.last_background_license_type = ""
        self.last_background_commercial_rights_verified = False
        self.background_library = background_library or BackgroundLibraryManager(config)

    @property
    def uses_story_background(self) -> bool:
        mode = str(self.story_background_config.get("mode", "")).lower()
        return bool(self.config.get("story_mode", {}).get("enabled", False)) and mode in {
            "custom_background_library",
            "background_library",
            "retention_categories",
            "approved_categories",
            "satisfying_categories",
        }

    def assemble(self, script: ContentScript, image_paths: list[Path], voice_path: Path, queue_id: int) -> Path:
        if self.uses_story_background:
            mode = str(self.story_background_config.get("mode", "")).lower()
            if mode in {"custom_background_library", "background_library"}:
                return self._assemble_custom_background(script, voice_path, queue_id)
            return self._assemble_retention_background(script, voice_path, queue_id)
        if not image_paths:
            raise VideoAssemblyError("At least one image is required")

        output_path = self.output_dir / f"{queue_id:06d}-{safe_filename(script.title)}.mp4"
        audio = AudioFileClip(str(voice_path))
        duration = float(audio.duration or self.video_config.get("target_seconds", 60))
        scene_duration = max(1.0, duration / len(image_paths))
        max_beat_seconds = max(0.75, float(self.video_config.get("max_visual_beat_seconds", 2.0)))
        clips = []
        opened_clips = [audio]
        try:
            for index, image_path in enumerate(image_paths):
                caption = script.scenes[min(index, len(script.scenes) - 1)].get("caption", script.title)
                scene_beats = self._scene_visual_beats(
                    image_path=image_path,
                    caption=caption,
                    title=script.title,
                    queue_id=queue_id,
                    scene_index=index + 1,
                    scene_duration=scene_duration,
                    max_beat_seconds=max_beat_seconds,
                )
                clips.extend(scene_beats)
                opened_clips.extend(scene_beats)

            final = concatenate_videoclips(clips, method="compose")
            final = self._with_duration(final, duration)
            final = self._with_audio(final, audio)
            opened_clips.append(final)
            LOGGER.info("Rendering short video: %s", output_path.name)
            final.write_videofile(
                str(output_path),
                fps=self.fps,
                codec="libx264",
                audio_codec="aac",
                threads=int(self.video_config.get("threads", 2)),
                preset=self.video_config.get("preset", "medium"),
                logger=None,
            )
            return output_path
        finally:
            for clip in reversed(opened_clips):
                close = getattr(clip, "close", None)
                if callable(close):
                    close()

    def _assemble_custom_background(self, script: ContentScript, voice_path: Path, queue_id: int) -> Path:
        excluded: set[str] = set()
        while True:
            asset = self.background_library.select(excluded=excluded)
            try:
                output_path = self._render_custom_background(script, voice_path, queue_id, asset)
                self.background_library.record_selected(asset.filename)
                try:
                    self.background_library.write_report()
                except Exception:
                    LOGGER.exception("Could not refresh the background library report after rendering")
                return output_path
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                excluded.add(asset.filename)
                self.background_library.record_failure(asset.filename, error)
                LOGGER.exception("Custom background render failed for %s; trying another file", asset.filename)

    def _render_custom_background(
        self,
        script: ContentScript,
        voice_path: Path,
        queue_id: int,
        asset: BackgroundAsset,
    ) -> Path:
        output_path = self.output_dir / f"{queue_id:06d}-{safe_filename(script.title)}.mp4"
        audio = AudioFileClip(str(voice_path))
        duration = float(audio.duration or self.video_config.get("target_seconds", 60))
        source = VideoFileClip(str(asset.path), audio=False)
        opened_clips = [audio, source]
        try:
            background, looped, trimmed, fitted_clips = self._fit_background_clip(source, duration)
            opened_clips.extend(fitted_clips)
            subtitle_clips = self._subtitle_clips(script.narration, duration, queue_id)
            opened_clips.extend(subtitle_clips)
            final = CompositeVideoClip([background, *subtitle_clips], size=(self.width, self.height))
            final = self._with_duration(final, duration)
            final = self._with_audio(final, audio)
            opened_clips.append(final)

            self.last_background_mode = "custom_background_library"
            self.last_background_category = asset.category or "custom_library"
            self.last_background_tier = "commercially_cleared"
            self.last_background_animated = True
            self.last_subtitles_enabled = True
            self.last_background_looped = looped
            self.last_background_trimmed = trimmed
            self.last_background_filename = asset.filename
            self.last_background_source_url = asset.source_url
            self.last_background_license_type = asset.license_type
            self.last_background_commercial_rights_verified = asset.commercial_rights_verified

            LOGGER.info(
                "Rendering story-mode custom background short video (%s, looped=%s, trimmed=%s): %s",
                asset.filename,
                looped,
                trimmed,
                output_path.name,
            )
            final.write_videofile(
                str(output_path),
                fps=self.fps,
                codec="libx264",
                audio_codec="aac",
                threads=int(self.video_config.get("threads", 2)),
                preset=self.video_config.get("preset", "medium"),
                logger=None,
            )
            return output_path
        finally:
            for clip in reversed(opened_clips):
                close = getattr(clip, "close", None)
                if callable(close):
                    close()

    def _fit_background_clip(self, source, duration: float) -> tuple[Any, bool, bool, list]:
        source_duration = float(source.duration or 0.0)
        if source_duration <= 0:
            raise VideoAssemblyError("Custom background duration must be greater than zero")
        covered = self._cover_video_clip(source)
        opened = [covered]
        if source_duration < duration:
            segments = []
            remaining = duration
            while remaining > 0.001:
                segment_duration = min(source_duration, remaining)
                segment = self._subclip(covered, 0.0, segment_duration)
                segments.append(segment)
                opened.append(segment)
                remaining -= segment_duration
            background = concatenate_videoclips(segments, method="compose")
            background = self._with_duration(background, duration)
            opened.append(background)
            return background, True, False, opened
        if source_duration > duration:
            background = self._subclip(covered, 0.0, duration)
            background = self._with_duration(background, duration)
            opened.append(background)
            return background, False, True, opened
        background = self._with_duration(covered, duration)
        return background, False, False, opened

    def _cover_video_clip(self, clip):
        source_width = float(getattr(clip, "w", self.width) or self.width)
        source_height = float(getattr(clip, "h", self.height) or self.height)
        target_ratio = self.width / self.height
        source_ratio = source_width / source_height
        if source_ratio > target_ratio:
            covered = self._resize(clip, height=self.height)
        else:
            covered = self._resize(clip, width=self.width)
        return self._crop(
            covered,
            width=self.width,
            height=self.height,
            x_center=float(getattr(covered, "w", self.width)) / 2,
            y_center=float(getattr(covered, "h", self.height)) / 2,
        )

    def _assemble_retention_background(self, script: ContentScript, voice_path: Path, queue_id: int) -> Path:
        output_path = self.output_dir / f"{queue_id:06d}-{safe_filename(script.title)}.mp4"
        audio = AudioFileClip(str(voice_path))
        duration = float(audio.duration or self.video_config.get("target_seconds", 60))
        loop_seconds = max(4.0, float(self.story_background_config.get("loop_seconds", 8.0)))
        category = self._select_background_category()
        self.last_background_mode = "retention_categories"
        self.last_background_category = category
        self.last_background_tier = self._background_tier(category)
        self.last_background_animated = True
        self.last_subtitles_enabled = True
        self.last_background_looped = duration > loop_seconds
        self.last_background_trimmed = False
        self.last_background_filename = ""
        self.last_background_source_url = ""
        self.last_background_license_type = ""
        self.last_background_commercial_rights_verified = False
        opened_clips = [audio]
        try:
            background = VideoClip(
                lambda t: self._retention_background_frame(float(t), loop_seconds, category),
                duration=duration,
            )
            background = self._with_duration(background, duration)
            opened_clips.append(background)
            subtitle_clips = self._subtitle_clips(script.narration, duration, queue_id)
            opened_clips.extend(subtitle_clips)
            final = CompositeVideoClip([background, *subtitle_clips], size=(self.width, self.height))
            final = self._with_duration(final, duration)
            final = self._with_audio(final, audio)
            opened_clips.append(final)
            LOGGER.info(
                "Rendering story-mode retention background short video (%s): %s",
                category,
                output_path.name,
            )
            final.write_videofile(
                str(output_path),
                fps=self.fps,
                codec="libx264",
                audio_codec="aac",
                threads=int(self.video_config.get("threads", 2)),
                preset=self.video_config.get("preset", "medium"),
                logger=None,
            )
            return output_path
        finally:
            for clip in reversed(opened_clips):
                close = getattr(clip, "close", None)
                if callable(close):
                    close()

    def _approved_background_categories(self) -> dict[str, list[str]]:
        configured = self.story_background_config.get("approved_categories")
        if isinstance(configured, dict):
            tier_1 = [self._normalize_category(item) for item in configured.get("tier_1", [])]
            tier_2 = [self._normalize_category(item) for item in configured.get("tier_2", [])]
            if tier_1 or tier_2:
                return {"tier_1": tier_1, "tier_2": tier_2}
        return {
            "tier_1": ["pressure_washing", "deep_cleaning", "restoration"],
            "tier_2": ["slime", "kinetic_sand", "soap_cutting"],
        }

    def _select_background_category(self) -> str:
        categories = self._approved_background_categories()
        allowed = [category for items in categories.values() for category in items]
        forced = self._normalize_category(str(self.story_background_config.get("forced_category", "") or ""))
        if forced:
            if forced not in allowed:
                raise VideoAssemblyError(f"Unapproved background category: {forced}")
            return forced
        if not allowed:
            raise VideoAssemblyError("No approved retention background categories configured")
        return random.SystemRandom().choice(allowed)

    def _background_tier(self, category: str) -> str:
        categories = self._approved_background_categories()
        for tier, values in categories.items():
            if category in values:
                return tier
        return "unknown"

    def _normalize_category(self, value: str) -> str:
        normalized = clean_text(value).lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "restoration_videos": "restoration",
            "cleaning": "deep_cleaning",
            "pressure_wash": "pressure_washing",
        }
        return aliases.get(normalized, normalized)

    def _retention_background_frame(self, t: float, loop_seconds: float, category: str) -> np.ndarray:
        if category == "pressure_washing":
            return self._pressure_washing_frame(t, loop_seconds)
        if category == "deep_cleaning":
            return self._deep_cleaning_frame(t, loop_seconds)
        if category == "restoration":
            return self._restoration_frame(t, loop_seconds)
        if category == "slime":
            return self._slime_frame(t, loop_seconds)
        if category == "kinetic_sand":
            return self._kinetic_sand_frame(t, loop_seconds)
        if category == "soap_cutting":
            return self._soap_cutting_frame(t, loop_seconds)
        return self._deep_cleaning_frame(t, loop_seconds)

    def _phase(self, t: float, loop_seconds: float) -> float:
        return (t % loop_seconds) / loop_seconds

    def _pressure_washing_frame(self, t: float, loop_seconds: float) -> np.ndarray:
        phase = self._phase(t, loop_seconds)
        width, height = self.width, self.height
        image = Image.new("RGB", (width, height), (102, 96, 84))
        draw = ImageDraw.Draw(image, "RGBA")
        tile = max(64, width // 7)
        clean_x = int(width * (0.08 + 0.84 * phase))
        clean_y = int(height * (0.22 + 0.50 * (0.5 + 0.5 * math.sin(phase * math.tau))))
        for y in range(0, height, tile):
            for x in range(0, width, tile):
                shade = 82 + ((x // tile + y // tile) % 2) * 16
                draw.rectangle((x, y, x + tile, y + tile), fill=(shade, shade - 6, shade - 16, 255))
                draw.rectangle((x, y, x + tile, y + tile), outline=(36, 34, 31, 95), width=2)
                grime = 55 + int(18 * math.sin((x + y) * 0.01 + phase * math.tau))
                draw.rectangle((x, y, x + tile, y + tile), fill=(45, grime, 38, 95))
        sweep_width = int(width * 0.34)
        clean_rect = (max(0, clean_x - sweep_width), 0, min(width, clean_x + sweep_width // 2), height)
        draw.rectangle(clean_rect, fill=(174, 169, 153, 180))
        for offset in range(-4, 5):
            angle = -0.50 + offset * 0.08
            end_x = clean_x + int(math.cos(angle) * width * 0.45)
            end_y = clean_y + int(math.sin(angle) * height * 0.30)
            draw.line((int(width * 0.06), int(height * 0.78), end_x, end_y), fill=(210, 245, 255, 95), width=max(5, width // 95))
        nozzle = (int(width * 0.05), int(height * 0.76), int(width * 0.20), int(height * 0.82))
        draw.rounded_rectangle(nozzle, radius=18, fill=(30, 38, 45, 255), outline=(220, 230, 235, 160), width=3)
        for drop in range(80):
            dx = (drop * 47 + int(phase * 1000)) % width
            dy = (drop * 83 + int(phase * 1600)) % height
            if abs(dx - clean_x) < sweep_width:
                draw.ellipse((dx, dy, dx + 4, dy + 10), fill=(225, 250, 255, 110))
        return np.array(image)

    def _deep_cleaning_frame(self, t: float, loop_seconds: float) -> np.ndarray:
        phase = self._phase(t, loop_seconds)
        width, height = self.width, self.height
        image = Image.new("RGB", (width, height), (72, 96, 104))
        draw = ImageDraw.Draw(image, "RGBA")
        for y in range(0, height, max(80, height // 12)):
            draw.line((0, y, width, y), fill=(230, 245, 240, 75), width=2)
        for x in range(0, width, max(90, width // 6)):
            draw.line((x, 0, x, height), fill=(230, 245, 240, 65), width=2)
        for speck in range(240):
            x = (speck * 61) % width
            y = (speck * 127) % height
            alpha = 85 + (speck % 60)
            draw.ellipse((x, y, x + 7, y + 7), fill=(22, 44, 39, alpha))
        scrub_x = int(width * (0.16 + 0.68 * (0.5 + 0.5 * math.sin(phase * math.tau))))
        scrub_y = int(height * (0.32 + 0.28 * (0.5 + 0.5 * math.sin(phase * math.tau * 1.7))))
        for ring in range(7, 0, -1):
            radius = int(width * 0.07 * ring)
            alpha = 28 + ring * 10
            draw.ellipse((scrub_x - radius, scrub_y - radius, scrub_x + radius, scrub_y + radius), fill=(235, 255, 245, alpha))
        sponge_w = int(width * 0.22)
        sponge_h = int(height * 0.09)
        draw.rounded_rectangle(
            (scrub_x - sponge_w // 2, scrub_y - sponge_h // 2, scrub_x + sponge_w // 2, scrub_y + sponge_h // 2),
            radius=26,
            fill=(244, 214, 75, 255),
            outline=(255, 250, 180, 210),
            width=4,
        )
        for bubble in range(60):
            angle = bubble * 0.55 + phase * math.tau * 2
            radius = width * (0.06 + (bubble % 9) * 0.018)
            x = int(scrub_x + math.cos(angle) * radius)
            y = int(scrub_y + math.sin(angle) * radius * 0.55)
            size = 8 + bubble % 12
            draw.ellipse((x, y, x + size, y + size), outline=(245, 255, 255, 145), width=2)
        return np.array(image)

    def _restoration_frame(self, t: float, loop_seconds: float) -> np.ndarray:
        phase = self._phase(t, loop_seconds)
        width, height = self.width, self.height
        image = Image.new("RGB", (width, height), (76, 50, 34))
        draw = ImageDraw.Draw(image, "RGBA")
        center = (width // 2, int(height * 0.52))
        plate = (int(width * 0.16), int(height * 0.18), int(width * 0.84), int(height * 0.82))
        draw.rounded_rectangle(plate, radius=42, fill=(112, 70, 42, 255), outline=(48, 31, 22, 255), width=8)
        for streak in range(180):
            x = plate[0] + (streak * 41) % max(1, (plate[2] - plate[0]))
            y = plate[1] + (streak * 89) % max(1, (plate[3] - plate[1]))
            color = (150 + streak % 55, 72 + streak % 38, 32, 135)
            draw.line((x, y, x + 28, y + 8), fill=color, width=3)
        reveal_x = int(plate[0] + (plate[2] - plate[0]) * phase)
        draw.rounded_rectangle(
            (plate[0], plate[1], reveal_x, plate[3]),
            radius=42,
            fill=(178, 181, 170, 235),
        )
        for shine in range(8):
            sx = plate[0] + shine * int((plate[2] - plate[0]) / 7)
            draw.line((sx, plate[1] + 22, sx + int(width * 0.15), plate[3] - 22), fill=(250, 250, 235, 70), width=5)
        brush_x = max(plate[0], min(plate[2], reveal_x))
        brush_y = int(center[1] + math.sin(phase * math.tau * 2.0) * height * 0.18)
        draw.rounded_rectangle(
            (brush_x - int(width * 0.08), brush_y - int(height * 0.035), brush_x + int(width * 0.16), brush_y + int(height * 0.035)),
            radius=18,
            fill=(38, 45, 52, 255),
        )
        for bristle in range(10):
            y = brush_y - 24 + bristle * 5
            draw.line((brush_x - 12, y, brush_x - int(width * 0.14), y + 4), fill=(235, 220, 180, 180), width=3)
        return np.array(image)

    def _slime_frame(self, t: float, loop_seconds: float) -> np.ndarray:
        phase = self._phase(t, loop_seconds)
        width, height = self.width, self.height
        image = Image.new("RGB", (width, height), (38, 34, 52))
        draw = ImageDraw.Draw(image, "RGBA")
        base_y = int(height * 0.54)
        colors = [(80, 232, 185), (248, 103, 188), (255, 220, 88), (121, 153, 255)]
        for blob in range(14):
            angle = blob * 0.7 + phase * math.tau
            x = int(width * (0.5 + 0.36 * math.sin(angle * 0.9)))
            y = int(base_y + height * 0.23 * math.cos(angle * 1.2))
            rx = int(width * (0.20 + 0.05 * math.sin(angle)))
            ry = int(height * (0.08 + 0.025 * math.cos(angle * 1.4)))
            draw.ellipse((x - rx, y - ry, x + rx, y + ry), fill=(*colors[blob % len(colors)], 165))
        press = 0.5 + 0.5 * math.sin(phase * math.tau * 2)
        hand_y = int(height * (0.22 + 0.10 * press))
        draw.rounded_rectangle(
            (int(width * 0.25), hand_y, int(width * 0.75), hand_y + int(height * 0.10)),
            radius=38,
            fill=(246, 204, 174, 230),
        )
        for drip in range(36):
            x = int(width * (0.18 + 0.64 * ((drip * 37 + phase * 100) % 100) / 100))
            length = int(height * (0.03 + 0.08 * ((drip % 7) / 7)))
            y = int(height * 0.58 + math.sin(phase * math.tau + drip) * height * 0.08)
            draw.rounded_rectangle((x, y, x + 12, y + length), radius=6, fill=(100, 255, 210, 125))
        return np.array(image)

    def _kinetic_sand_frame(self, t: float, loop_seconds: float) -> np.ndarray:
        phase = self._phase(t, loop_seconds)
        width, height = self.width, self.height
        image = Image.new("RGB", (width, height), (202, 174, 116))
        draw = ImageDraw.Draw(image, "RGBA")
        for row in range(0, height, 28):
            wave = int(math.sin(row * 0.02 + phase * math.tau * 2) * width * 0.03)
            draw.line((0, row, width, row + wave), fill=(238, 216, 157, 120), width=3)
        rake_x = int(width * (0.12 + 0.76 * phase))
        draw.rectangle((0, int(height * 0.28), rake_x, int(height * 0.78)), fill=(228, 201, 137, 130))
        blade_y = int(height * (0.36 + 0.24 * math.sin(phase * math.tau)))
        draw.polygon(
            [
                (rake_x - int(width * 0.06), blade_y - int(height * 0.08)),
                (rake_x + int(width * 0.06), blade_y),
                (rake_x - int(width * 0.06), blade_y + int(height * 0.08)),
            ],
            fill=(70, 78, 84, 240),
        )
        for grain in range(260):
            x = (grain * 59 + int(phase * 500)) % width
            y = (grain * 97) % height
            alpha = 60 + grain % 70
            draw.point((x, y), fill=(118, 92, 49, alpha))
        return np.array(image)

    def _soap_cutting_frame(self, t: float, loop_seconds: float) -> np.ndarray:
        phase = self._phase(t, loop_seconds)
        width, height = self.width, self.height
        image = Image.new("RGB", (width, height), (52, 66, 78))
        draw = ImageDraw.Draw(image, "RGBA")
        block = (int(width * 0.15), int(height * 0.25), int(width * 0.85), int(height * 0.76))
        draw.rounded_rectangle(block, radius=38, fill=(126, 220, 232, 255), outline=(235, 255, 255, 180), width=6)
        cut_x = int(block[0] + (block[2] - block[0]) * phase)
        for index in range(12):
            x = block[0] + index * int((block[2] - block[0]) / 12)
            draw.line((x, block[1] + 16, x - int(width * 0.09), block[3] - 16), fill=(73, 165, 180, 115), width=4)
            if x < cut_x:
                slice_y = block[3] + int((index % 5) * height * 0.018 + phase * height * 0.06)
                draw.rounded_rectangle(
                    (x - 22, slice_y, x + 52, slice_y + int(height * 0.04)),
                    radius=10,
                    fill=(170, 245, 250, 190),
                )
        draw.line((cut_x, block[1] - int(height * 0.05), cut_x - int(width * 0.16), block[3] + int(height * 0.06)), fill=(245, 248, 250, 255), width=max(5, width // 90))
        draw.line((cut_x + 10, block[1] - int(height * 0.05), cut_x - int(width * 0.16) + 10, block[3] + int(height * 0.06)), fill=(30, 35, 40, 140), width=3)
        for fleck in range(70):
            x = (fleck * 67 + int(phase * 800)) % width
            y = block[1] + (fleck * 43) % max(1, block[3] - block[1])
            draw.ellipse((x, y, x + 8, y + 8), fill=(235, 255, 255, 110))
        return np.array(image)

    def _subtitle_clips(self, narration: str, duration: float, queue_id: int) -> list:
        chunks = self._subtitle_chunks(narration)
        if not chunks:
            return []
        clips = []
        segment = duration / len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            overlay_path = self._create_subtitle_overlay(chunk, queue_id, index)
            clip = ImageClip(str(overlay_path))
            clip = self._with_start(clip, (index - 1) * segment)
            clip = self._with_duration(clip, segment)
            clip = self._with_position(clip, ("center", "center"))
            clips.append(clip)
        return clips

    def _subtitle_chunks(self, narration: str) -> list[str]:
        words = clean_text(narration).split()
        if not words:
            return []
        chunk_size = int(self.story_background_config.get("subtitle_words_per_chunk", 7))
        chunk_size = max(4, min(10, chunk_size))
        chunks = [" ".join(words[index : index + chunk_size]) for index in range(0, len(words), chunk_size)]
        return chunks[:120]

    def _create_subtitle_overlay(self, text: str, queue_id: int, index: int) -> Path:
        overlay_path = self.overlay_dir / f"{queue_id:06d}-story-subtitle-{index:03d}.png"
        image = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")
        font = self._load_font(max(42, int(self.width * 0.078)), bold=True)
        lines = self._subtitle_lines(text, draw, font, max_width=int(self.width * 0.86), max_lines=2)
        line_height = max(52, int(self.width * 0.092))
        total_height = line_height * len(lines)
        start_y = int(self.height * 0.52 - total_height / 2)
        for line_index, line in enumerate(lines):
            self._draw_highlighted_subtitle_line(
                draw=draw,
                words=line,
                font=font,
                y=start_y + line_index * line_height,
            )
        image.save(overlay_path)
        return overlay_path

    def _subtitle_lines(
        self,
        text: str,
        draw: ImageDraw.ImageDraw,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
        max_lines: int,
    ) -> list[list[str]]:
        words = clean_text(text).split()
        lines: list[list[str]] = []
        current: list[str] = []
        for word in words:
            candidate = [*current, word]
            if self._words_width(draw, candidate, font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = [word]
                if len(lines) >= max_lines:
                    break
        if current and len(lines) < max_lines:
            lines.append(current)
        return lines or [[clean_text(text)]]

    def _words_width(
        self,
        draw: ImageDraw.ImageDraw,
        words: list[str],
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> int:
        spacing = max(12, self.width // 90)
        width = 0
        for index, word in enumerate(words):
            bbox = draw.textbbox((0, 0), word, font=font, stroke_width=0)
            width += bbox[2] - bbox[0]
            if index:
                width += spacing
        return width

    def _draw_highlighted_subtitle_line(
        self,
        draw: ImageDraw.ImageDraw,
        words: list[str],
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        y: int,
    ) -> None:
        spacing = max(12, self.width // 90)
        total_width = self._words_width(draw, words, font)
        x = int((self.width - total_width) / 2)
        stroke_width = max(4, self.width // 135)
        for word in words:
            fill = (255, 225, 64, 255) if self._is_important_subtitle_word(word) else (255, 255, 255, 255)
            draw.text(
                (x + max(3, stroke_width // 2), y + max(3, stroke_width // 2)),
                word,
                fill=(0, 0, 0, 135),
                font=font,
                stroke_width=stroke_width,
                stroke_fill=(0, 0, 0, 185),
            )
            draw.text(
                (x, y),
                word,
                fill=fill,
                font=font,
                stroke_width=stroke_width,
                stroke_fill=(0, 0, 0, 255),
            )
            bbox = draw.textbbox((0, 0), word, font=font, stroke_width=0)
            x += bbox[2] - bbox[0] + spacing

    def _is_important_subtitle_word(self, word: str) -> bool:
        normalized = unicodedata.normalize("NFKD", word.strip(".,!?;:").lower())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        markers = {
            "pista",
            "segredo",
            "revelacao",
            "virada",
            "assustador",
            "assustadora",
            "estranho",
            "estranha",
            "nunca",
            "nao",
            "cuidado",
            "perigo",
            "final",
            "mudou",
            "descoberta",
            "misterio",
            "reddit",
        }
        return normalized in markers or any(marker in normalized for marker in ["revela", "pista", "segred", "assust", "mister"])

    def _scene_visual_beats(
        self,
        image_path: Path,
        caption: str,
        title: str,
        queue_id: int,
        scene_index: int,
        scene_duration: float,
        max_beat_seconds: float,
    ) -> list:
        beats = max(1, int((scene_duration + max_beat_seconds - 0.01) // max_beat_seconds))
        remaining = scene_duration
        clips = []
        overlay_path = self._create_caption_overlay(caption, title, queue_id, scene_index)
        for beat_index in range(beats):
            beat_duration = min(max_beat_seconds, remaining)
            remaining -= beat_duration
            base = self._cover_image_clip(image_path, variant=scene_index + beat_index)
            base = self._with_duration(base, beat_duration)
            layers = [base]
            if beat_index == 0:
                overlay = ImageClip(str(overlay_path))
                overlay = self._with_duration(overlay, min(beat_duration, 1.45))
                overlay = self._with_position(overlay, ("center", "center"))
                layers.append(overlay)
            scene = CompositeVideoClip(layers, size=(self.width, self.height))
            scene = self._with_duration(scene, beat_duration)
            clips.append(scene)
        return clips

    def _cover_image_clip(self, image_path: Path, variant: int = 0):
        with Image.open(image_path) as image:
            image_width, image_height = image.size
        target_ratio = self.width / self.height
        image_ratio = image_width / image_height
        clip = ImageClip(str(image_path))
        if image_ratio > target_ratio:
            clip = self._resize(clip, height=self.height)
            clip = self._crop(
                clip,
                width=self.width,
                height=self.height,
                x_center=float(getattr(clip, "w", self.width)) / 2,
                y_center=self.height / 2,
            )
        else:
            clip = self._resize(clip, width=self.width)
            clip = self._crop(
                clip,
                width=self.width,
                height=self.height,
                x_center=self.width / 2,
                y_center=float(getattr(clip, "h", self.height)) / 2,
            )
        return self._motion_crop(clip, variant)

    def _motion_crop(self, clip, variant: int):
        zoom = 1.035 + (variant % 3) * 0.018
        enlarged = self._resize(clip, width=int(self.width * zoom))
        clip_width = float(getattr(enlarged, "w", self.width))
        clip_height = float(getattr(enlarged, "h", self.height))
        max_x = max(0.0, (clip_width - self.width) / 2)
        max_y = max(0.0, (clip_height - self.height) / 2)
        offsets = [(-0.7, -0.45), (0.55, -0.25), (-0.35, 0.45), (0.65, 0.35)]
        ox, oy = offsets[variant % len(offsets)]
        return self._crop(
            enlarged,
            width=self.width,
            height=self.height,
            x_center=clip_width / 2 + max_x * ox,
            y_center=clip_height / 2 + max_y * oy,
        )

    def _create_caption_overlay(self, caption: str, title: str, queue_id: int, index: int) -> Path:
        overlay_path = self.overlay_dir / f"{queue_id:06d}-{index:02d}-caption.png"
        image = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")
        caption_font = self._load_font(54)
        footer_font = self._load_font(24)

        wrapped = wrap_for_display(caption, width=18, max_lines=2)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=caption_font, spacing=10)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        panel_width = min(self.width - 150, text_width + 72)
        panel_height = text_height + 54
        left = 64
        top = int(self.height * 0.72)
        draw.rounded_rectangle(
            (left, top, left + panel_width, top + panel_height),
            radius=22,
            fill=(0, 0, 0, 150),
        )
        draw.multiline_text(
            (left + 36, top + 26),
            wrapped,
            fill=(255, 255, 255, 255),
            font=caption_font,
            spacing=10,
            align="left",
        )
        draw.text((64, self.height - 78), "ShortsMaster", fill=(255, 255, 255, 170), font=footer_font)
        image.save(overlay_path)
        return overlay_path

    def _load_font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            self.video_config.get("font_path"),
            "C:/Windows/Fonts/arialbd.ttf" if bold else None,
            "C:/Windows/Fonts/segoeuib.ttf" if bold else None,
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else None,
        ]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _with_duration(self, clip, duration: float):
        method = getattr(clip, "with_duration", None) or getattr(clip, "set_duration")
        return method(duration)

    def _with_audio(self, clip, audio):
        method = getattr(clip, "with_audio", None) or getattr(clip, "set_audio")
        return method(audio)

    def _with_position(self, clip, position):
        method = getattr(clip, "with_position", None) or getattr(clip, "set_position")
        return method(position)

    def _with_start(self, clip, start: float):
        method = getattr(clip, "with_start", None) or getattr(clip, "set_start")
        return method(start)

    def _resize(self, clip, **kwargs):
        method = getattr(clip, "resized", None) or getattr(clip, "resize")
        return method(**kwargs)

    def _crop(self, clip, **kwargs):
        method = getattr(clip, "cropped", None) or getattr(clip, "crop")
        return method(**kwargs)

    def _subclip(self, clip, start: float, end: float):
        method = getattr(clip, "subclipped", None) or getattr(clip, "subclip")
        return method(start, end)
