from __future__ import annotations

import hashlib
import json
import re
import textwrap
from typing import Any


_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_SPACE_RE = re.compile(r"\s+")


def clean_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


def normalize_topic_key(title: str) -> str:
    normalized = clean_text(title).lower()
    normalized = _NON_WORD_RE.sub("-", normalized).strip("-")
    if not normalized:
        normalized = "untitled"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{normalized[:80]}-{digest}"


def safe_filename(value: str, max_length: int = 80) -> str:
    cleaned = _NON_WORD_RE.sub("-", clean_text(value).lower()).strip("-")
    return (cleaned or "asset")[:max_length]


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def clamp_words(text: str, min_words: int, max_words: int) -> str:
    words = clean_text(text).split()
    if len(words) <= max_words:
        return clean_text(text)
    return " ".join(words[:max_words]).rstrip(" ,;:") + "."


def split_sentences(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", clean_text(text))
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def wrap_for_display(text: str, width: int = 28, max_lines: int = 4) -> str:
    lines: list[str] = []
    for paragraph in clean_text(text).split("\n"):
        lines.extend(textwrap.wrap(paragraph, width=width))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .") + "..."
    return "\n".join(lines)
