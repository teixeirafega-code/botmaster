from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.models import ContentScript
from app.utils.text import clean_text


class LanguageGuard:
    """Checks generated public-facing text before audio/video creation."""

    REQUIRED_LANGUAGE = "pt-BR"

    PORTUGUESE_WORDS = {
        "a",
        "as",
        "ao",
        "aos",
        "ate",
        "baixa",
        "brasileiro",
        "brasileira",
        "cada",
        "com",
        "como",
        "curiosidade",
        "ciencia",
        "de",
        "depois",
        "detalhe",
        "da",
        "das",
        "do",
        "dos",
        "e",
        "ela",
        "ele",
        "em",
        "era",
        "essa",
        "esse",
        "esta",
        "este",
        "foi",
        "historia",
        "historiasreddit",
        "ia",
        "isso",
        "mas",
        "mesmo",
        "nao",
        "no",
        "nos",
        "o",
        "os",
        "para",
        "permanente",
        "porque",
        "por",
        "quando",
        "que",
        "relato",
        "relatos",
        "sem",
        "semifinal",
        "slime",
        "sua",
        "seu",
        "sobre",
        "titulo",
        "uma",
        "um",
        "video",
        "voce",
        "viralbr",
        "shortsbr",
        "tecnologia",
        "financas",
        "esportes",
        "geral",
        "explicado",
        "edicao",
        "futebol",
        "calendario",
        "copadomundo",
        "envio",
        "qualidade",
    }
    PORTUGUESE_SUBSTRINGS = {
        "assust",
        "brasil",
        "curios",
        "descob",
        "desastre",
        "engrac",
        "edicao",
        "detalh",
        "escond",
        "histori",
        "inacredit",
        "misteri",
        "mudou",
        "pista",
        "relat",
        "reviravolta",
        "segredo",
        "viral",
    }
    ENGLISH_WORDS = {
        "about",
        "after",
        "attention",
        "before",
        "changed",
        "changes",
        "clue",
        "comments",
        "cup",
        "described",
        "ending",
        "explained",
        "fast",
        "first",
        "from",
        "gaining",
        "has",
        "here",
        "hidden",
        "how",
        "important",
        "know",
        "missing",
        "mode",
        "next",
        "not",
        "odd",
        "payoff",
        "paper",
        "people",
        "poster",
        "reveal",
        "schedule",
        "shared",
        "short",
        "source",
        "story",
        "storytime",
        "strange",
        "suddenly",
        "talking",
        "that",
        "the",
        "then",
        "this",
        "topic",
        "trap",
        "trend",
        "trending",
        "twist",
        "unverified",
        "upload",
        "verified",
        "watch",
        "what",
        "why",
        "world",
        "with",
        "you",
    }
    ENGLISH_PHRASES = {
        "fast ending",
        "gaining attention",
        "here is the reveal",
        "here is what you need to know",
        "hidden clue",
        "not verified",
        "people are talking",
        "reddit story",
        "suddenly everywhere",
        "the payoff",
        "the reveal",
        "this reddit story",
        "why it is trending",
    }
    ALLOWED_TERMS = {
        "askreddit",
        "fifa",
        "google",
        "instagram",
        "interestingasfuck",
        "letsnotmeet",
        "lifeprotips",
        "nasa",
        "ncaa",
        "nebraska",
        "nostupidquestions",
        "openai",
        "reddit",
        "shorts",
        "softball",
        "texas",
        "tifu",
        "tiktok",
        "todayilearned",
        "youtube",
    }

    def __init__(self, config: dict[str, Any]):
        self.config = config
        language_config = config.get("language", {})
        self.required_language = str(
            language_config.get("required") or language_config.get("default") or self.REQUIRED_LANGUAGE
        )
        self.block_english = bool(language_config.get("block_english_output", True))

    def evaluate_script(self, script: ContentScript) -> dict[str, Any]:
        fields = self._script_fields(script)
        languages = {
            "script_language": self.detect_language(fields["script"]),
            "narration_language": self.detect_language(fields["narration"]),
            "subtitle_language": self.detect_language(fields["subtitles"]),
            "title_language": self.detect_language(fields["title"]),
            "description_language": self.detect_language(fields["description"]),
            "hashtag_language": self.detect_language(fields["hashtags"]),
        }
        required = self._normalize_language(self.required_language)
        reasons: list[str] = []
        warnings: list[str] = []

        for key in ["script_language", "narration_language", "subtitle_language", "title_language"]:
            if self._normalize_language(languages[key]) != required:
                reasons.append(f"{key} must be {self.required_language}; detected {languages[key]}")

        for key in ["description_language", "hashtag_language"]:
            if self._normalize_language(languages[key]) != required:
                reasons.append(f"{key} must be {self.required_language}; detected {languages[key]}")

        residue = self.english_residue(fields)
        if self.block_english and residue:
            preview = ", ".join(item["term"] for item in residue[:8])
            reasons.append(f"English text remains in final output: {preview}")

        if not residue:
            warnings.append("pt-BR language gate found no English residue in final output")

        return {
            "allowed": not reasons,
            "required_language": self.required_language,
            **languages,
            "english_residue": residue,
            "reasons": reasons,
            "warnings": warnings,
        }

    def detect_language(self, text: str) -> str:
        normalized_text = self._normalize(text)
        tokens = [token for token in self._tokens(text) if token not in self.ALLOWED_TERMS]
        if not tokens:
            return self.required_language

        portuguese_hits = sum(1 for token in tokens if token in self.PORTUGUESE_WORDS)
        portuguese_hits += sum(
            1 for token in tokens for marker in self.PORTUGUESE_SUBSTRINGS if marker in token
        )
        english_hits = sum(1 for token in tokens if token in self.ENGLISH_WORDS)
        english_hits += sum(2 for phrase in self.ENGLISH_PHRASES if phrase in normalized_text)

        if english_hits >= 2 and english_hits >= portuguese_hits:
            return "en"
        if portuguese_hits >= 2 and english_hits <= max(1, portuguese_hits // 3):
            return self.required_language
        if portuguese_hits >= 1 and english_hits == 0:
            return self.required_language
        if english_hits > 0 and portuguese_hits == 0:
            return "en"
        return "unknown"

    def english_residue(self, fields: dict[str, str]) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        for field, text in fields.items():
            if field == "script":
                continue
            normalized_text = self._normalize(text)
            for phrase in sorted(self.ENGLISH_PHRASES):
                if phrase in normalized_text:
                    findings.append({"field": field, "term": phrase})
            for token in self._tokens(text):
                if token in self.ALLOWED_TERMS:
                    continue
                if token in self.ENGLISH_WORDS:
                    findings.append({"field": field, "term": token})
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in findings:
            key = (item["field"], item["term"])
            if key not in seen:
                deduped.append(item)
                seen.add(key)
        return deduped

    def _script_fields(self, script: ContentScript) -> dict[str, str]:
        captions = " ".join(clean_text(scene.get("caption", "")) for scene in script.scenes)
        hashtags = " ".join(str(tag).lstrip("#") for tag in script.tags)
        script_text = " ".join(
            [
                script.title,
                script.hook,
                script.narration,
                captions,
                script.description,
                hashtags,
            ]
        )
        return {
            "script": clean_text(script_text),
            "title": clean_text(script.title),
            "narration": clean_text(script.narration),
            "subtitles": clean_text(f"{script.narration} {captions}"),
            "description": clean_text(script.description),
            "hashtags": clean_text(hashtags),
        }

    def _tokens(self, text: str) -> list[str]:
        return [
            token
            for token in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9']+", self._normalize(text))
            if len(token) > 1
        ]

    def _normalize(self, text: str) -> str:
        value = unicodedata.normalize("NFKD", clean_text(text))
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        return value.lower()

    def _normalize_language(self, value: str) -> str:
        return value.strip().lower().replace("_", "-")
