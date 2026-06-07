from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "background_library" / "shortmaster-original-pressure-washing-v1.mp4"

try:
    from moviepy import VideoClip
except ImportError:  # MoviePy 1.x
    from moviepy.editor import VideoClip


def make_frame(time_seconds: float) -> np.ndarray:
    width, height = 360, 640
    duration = 6.0
    phase = (time_seconds % duration) / duration
    sweep = int(width * (0.5 + 0.42 * math.sin(phase * math.tau)))
    image = Image.new("RGB", (width, height), (72, 68, 61))
    draw = ImageDraw.Draw(image)

    for y in range(0, height, 80):
        draw.rectangle((0, y, width, y + 76), fill=(84, 79, 69))
        draw.line((0, y + 76, width, y + 76), fill=(42, 40, 37), width=4)
    for x in range(0, width, 90):
        draw.line((x, 0, x, height), fill=(48, 46, 42), width=3)

    clean_edge = max(0, min(width, sweep))
    for y in range(0, height, 80):
        draw.rectangle((0, y, clean_edge, y + 76), fill=(194, 198, 193))
        draw.line((0, y + 76, clean_edge, y + 76), fill=(112, 116, 112), width=4)
    for x in range(0, clean_edge, 90):
        draw.line((x, 0, x, height), fill=(126, 130, 126), width=3)

    rng = np.random.default_rng(7321)
    for x, y, radius in rng.integers([0, 0, 1], [width, height, 5], size=(260, 3)):
        if x > clean_edge:
            shade = int(rng.integers(35, 68))
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(shade, shade - 3, shade - 8))

    nozzle_y = int(height * (0.45 + 0.12 * math.sin(phase * math.tau * 2)))
    draw.polygon(
        [(sweep - 14, nozzle_y + 34), (sweep + 14, nozzle_y + 34), (sweep + 55, height)],
        fill=(190, 228, 238),
    )
    draw.line((sweep, nozzle_y, sweep, nozzle_y + 60), fill=(28, 31, 34), width=14)
    draw.rounded_rectangle(
        (sweep - 24, nozzle_y - 28, sweep + 24, nozzle_y + 18),
        radius=8,
        fill=(40, 44, 48),
        outline=(212, 218, 220),
        width=3,
    )
    for offset in range(-45, 46, 15):
        draw.line(
            (sweep, nozzle_y + 28, sweep + offset, nozzle_y + 150),
            fill=(220, 244, 250),
            width=3,
        )
    return np.asarray(image)


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    try:
        clip = VideoClip(frame_function=make_frame, duration=6.0)
    except TypeError:
        clip = VideoClip(make_frame=make_frame, duration=6.0)
    try:
        clip.write_videofile(
            str(OUTPUT),
            fps=24,
            codec="libx264",
            audio=False,
            preset="medium",
            logger=None,
        )
    finally:
        clip.close()
    print(OUTPUT)


if __name__ == "__main__":
    sys.exit(main())
