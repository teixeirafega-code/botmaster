from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont

from app.models import ContentScript, TrendTopic
from app.services.config import resolve_storage_path
from app.utils.retry import retry
from app.utils.text import safe_filename, wrap_for_display


LOGGER = logging.getLogger(__name__)


class ImageGenerationError(RuntimeError):
    """Raised when image generation cannot produce a usable background asset."""


class ImageGenerator:
    def __init__(self, config: dict):
        self.config = config
        self.image_config = config.get("generation", {}).get("images", {})
        self.paper_mode = bool(config.get("app", {}).get("paper_mode", True))
        videos_dir = resolve_storage_path(config, config.get("storage", {}).get("videos_dir", "videos"))
        self.output_dir = videos_dir / "images"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ShortsMasterBot/1.0"})
        self.last_sources: list[str] = []
        self.last_warnings: list[str] = []

    def generate(self, script: ContentScript, topic: TrendTopic, queue_id: int) -> list[Path]:
        paths: list[Path] = []
        self.last_sources = []
        self.last_warnings = []
        for index, scene in enumerate(script.scenes, start=1):
            prompt = self._build_prompt(scene.get("image_prompt", ""), topic)
            output_path = self.output_dir / f"{queue_id:06d}-{index:02d}-{safe_filename(script.title)}.jpg"
            if self.image_config.get("force_local_fallback", False):
                warning = f"Image {index} generated with forced local validation fallback"
                self.last_warnings.append(warning)
                LOGGER.warning(warning)
                self._create_local_fallback(output_path, scene.get("caption", script.title), index)
                self.last_sources.append("local_forced")
                paths.append(output_path)
                continue
            try:
                self._download_pollinations(prompt, output_path, seed=queue_id * 100 + index)
                self.last_sources.append("pollinations")
            except Exception as exc:
                if self.paper_mode and self.image_config.get("offline_fallback", True):
                    warning = f"Pollinations image {index} failed; used local paper-mode fallback: {exc}"
                    self.last_warnings.append(warning)
                    LOGGER.warning(warning)
                    self._create_local_fallback(output_path, scene.get("caption", script.title), index)
                    self.last_sources.append("local_fallback")
                else:
                    raise ImageGenerationError(f"Pollinations image generation failed: {exc}") from exc
            paths.append(output_path)
        return paths

    def _build_prompt(self, scene_prompt: str, topic: TrendTopic) -> str:
        style = self.image_config.get(
            "style",
            "vertical 9:16 cinematic editorial, sharp focus, premium YouTube Shorts background",
        )
        safety = "no logos, no text, no watermark, no real person likeness"
        return f"{scene_prompt}, topic: {topic.title}, {style}, {safety}"

    @retry(attempts=3, delay_seconds=1.0, exceptions=(requests.RequestException,))
    def _download_pollinations(self, prompt: str, output_path: Path, seed: int) -> None:
        width = int(self.image_config.get("width", 1080))
        height = int(self.image_config.get("height", 1920))
        model = self.image_config.get("model", "flux")
        encoded = quote(prompt[:1800], safe="")
        url = f"https://image.pollinations.ai/prompt/{encoded}"
        response = self.session.get(
            url,
            params={
                "width": width,
                "height": height,
                "seed": seed,
                "model": model,
                "nologo": "true",
                "private": "true",
                "enhance": "true",
            },
            timeout=90,
        )
        response.raise_for_status()
        output_path.write_bytes(response.content)
        with Image.open(output_path) as image:
            image.verify()

    def _create_local_fallback(self, output_path: Path, caption: str, index: int) -> None:
        width = int(self.image_config.get("width", 1080))
        height = int(self.image_config.get("height", 1920))
        palettes = [
            ((18, 28, 34), (236, 196, 96), (74, 155, 185)),
            ((24, 37, 64), (93, 214, 200), (219, 108, 91)),
            ((45, 35, 63), (240, 124, 94), (132, 211, 151)),
            ((24, 62, 52), (219, 231, 148), (90, 144, 220)),
            ((72, 31, 50), (107, 218, 177), (238, 191, 93)),
        ]
        base, accent, secondary = palettes[(index - 1) % len(palettes)]
        image = Image.new("RGB", (width, height), base)
        draw = ImageDraw.Draw(image)
        horizon = int(height * 0.58)
        draw.rectangle((0, horizon, width, height), fill=tuple(max(0, c - 10) for c in base))
        for layer in range(7):
            offset = layer * 140 + (index % 3) * 35
            color = tuple(int(base[ch] * 0.65 + accent[ch] * 0.35) for ch in range(3))
            draw.polygon(
                [
                    (offset - 260, height),
                    (offset + 60, int(height * 0.42)),
                    (offset + 370, height),
                ],
                fill=color,
            )
        for ring in range(5):
            pad = 100 + ring * 72
            color = tuple(int(secondary[ch] * (0.55 + ring * 0.06)) for ch in range(3))
            draw.ellipse((width - pad - 360, 140 + ring * 22, width - pad, 500 + ring * 22), outline=color, width=9)
        card_w = int(width * 0.46)
        card_h = int(height * 0.14)
        for card in range(3):
            x = 82 + card * 52
            y = int(height * (0.23 + card * 0.13))
            draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=28, fill=(245, 245, 238), outline=accent, width=5)
            draw.line((x + 38, y + 48, x + card_w - 40, y + 48), fill=base, width=8)
            draw.line((x + 38, y + 86, x + card_w - 130, y + 86), fill=secondary, width=7)
        font = self._load_font(44)
        small_font = self._load_font(26)
        text = wrap_for_display(caption, width=18, max_lines=2)
        panel_top = int(height * 0.76)
        draw.rounded_rectangle((72, panel_top, width - 72, panel_top + 150), radius=30, fill=(0, 0, 0))
        draw.multiline_text((112, panel_top + 42), text, fill=(255, 255, 255), font=font, spacing=10, align="left")
        draw.text((82, height - 96), "ShortsMaster paper visual", fill=(255, 255, 255), font=small_font)
        image.save(output_path, quality=92)

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
        return ImageFont.load_default()
