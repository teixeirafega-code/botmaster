from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from app.models import ContentScript, TrendTopic
from app.utils.text import clean_text, split_sentences


MANIPULATIVE_ENGAGEMENT_PATTERNS = {
    "fake_scarcity": re.compile(r"\b99\s*%|\b99\s*por\s*cento", re.IGNORECASE),
    "intelligence_bait": re.compile(
        r"\bs[oó]\s+(?:os\s+)?inteligentes|\bquem\s+[ée]\s+inteligente",
        re.IGNORECASE,
    ),
    "emotional_blackmail": re.compile(
        r"\bse\s+voc[êe].{0,40}\bama\s+(?:a\s+)?sua\s+m[ãa]e"
        r"|\bse\s+ama\s+sua\s+m[ãa]e",
        re.IGNORECASE,
    ),
    "bad_luck_threat": re.compile(
        r"\b(?:ter[aá]|vai\s+ter|acontece(?:r[aá])?)\s+\d+\s+(?:dia|semana|m[eê]s)"
        r".{0,24}\bazar|\balgo\s+ruim\s+(?:vai\s+)?acontece",
        re.IGNORECASE,
    ),
    "forced_speed_challenge": re.compile(
        r"\bn[ãa]o\s+consegue(?:m)?\s+(?:curtir|dar\s+like|se\s+inscrever)"
        r".{0,30}\bsegundo",
        re.IGNORECASE,
    ),
}


_SOCIAL_ACTION_RE = re.compile(
    r"\b(?:curte|curtir|like|inscreva|inscrever|compartilhe|compartilhar|comenta|comente)\b",
    re.IGNORECASE,
)
_AUDIENCE_QUESTION_RE = re.compile(
    r"\b(?:pra\s+voc[êe]|na\s+sua\s+opini[ãa]o|"
    r"voc[êe]\s+(?:teria|faria|acreditaria|contaria|abriria)|"
    r"j[aá]\s+aconteceu.{0,45}com\s+voc[êe]|"
    r"qual\s+(?:parte|situa[cç][ãa]o|decis[ãa]o|descoberta)|"
    r"qual.{0,60}com\s+voc[êe]|o\s+que\s+voc[êe]\s+faria)\b",
    re.IGNORECASE,
)


def _story_category(topic: TrendTopic) -> str:
    raw = topic.raw or {}
    source = dict(raw.get("story_source") or {})
    beats = dict(raw.get("story_beats") or {})
    labels = source.get("priority_labels") or beats.get("priority_labels") or []
    return str(labels[0]) if labels else "mystery"


def _variant_templates(category: str) -> list[tuple[str, str]]:
    if category == "funny_disaster":
        return [
            ("opinion", "Você contaria a verdade ou fingiria que nada aconteceu?"),
            ("disagreement", "Pra você, quem mais piorou esse desastre?"),
            ("personal_experience", "Qual foi o desastre mais engraçado que já aconteceu com você?"),
        ]
    if category == "shocking_discovery":
        return [
            ("opinion", "Você abriria a caixa ou deixaria o segredo onde estava?"),
            ("disagreement", "Pra você, contar essa descoberta foi a decisão certa?"),
            ("personal_experience", "Qual descoberta já mudou completamente a sua família?"),
        ]
    if category == "life_changing":
        return [
            ("opinion", "Você teria tomado a mesma decisão no lugar dele?"),
            ("disagreement", "Pra você, essa mudança valeu todo o risco?"),
            ("personal_experience", "Qual decisão já mudou a sua vida de uma vez?"),
        ]
    if category == "unbelievable":
        return [
            ("opinion", "Você acreditaria nessa história se tivesse visto tudo de perto?"),
            ("disagreement", "Pra você, qual detalhe parece mais impossível?"),
            ("personal_experience", "Qual foi a coisa mais inacreditável que já aconteceu com você?"),
        ]
    return [
        ("opinion", "Você teria investigado isso ou saído dali na mesma hora?"),
        ("disagreement", "Pra você, qual foi o primeiro sinal de que havia algo errado?"),
        ("personal_experience", "Qual situação estranha já fez você sair sem pensar duas vezes?"),
    ]


def score_engagement_prompt(prompt: str, prompt_type: str = "") -> float:
    text = clean_text(prompt)
    lower = text.lower()
    words = text.split()
    score = 66.0
    if text.endswith("?"):
        score += 9
    if 8 <= len(words) <= 14:
        score += 8
    elif len(words) < 6 or len(words) > 18:
        score -= 10
    if "você" in lower or "pra você" in lower:
        score += 7
    if " ou " in lower:
        score += 7
    if lower.startswith(("pra você", "qual", "você")):
        score += 4
    if prompt_type in {"opinion", "disagreement", "personal_experience"}:
        score += 3
    if prompt_type == "personal_experience":
        score += 2
    if any(pattern.search(text) for pattern in MANIPULATIVE_ENGAGEMENT_PATTERNS.values()):
        score = 0.0
    return round(max(0.0, min(98.0, score)), 1)


def is_engagement_prompt(sentence: str, hook: str = "") -> bool:
    text = clean_text(sentence)
    if not text or text == clean_text(hook):
        return False
    if text.lower().startswith("pessoal do reddit:"):
        return False
    return bool(_SOCIAL_ACTION_RE.search(text) or ("?" in text and _AUDIENCE_QUESTION_RE.search(text)))


def analyze_engagement_prompt(script: ContentScript) -> dict[str, Any]:
    sentences = split_sentences(script.narration)
    detected = [
        sentence
        for sentence in sentences
        if is_engagement_prompt(sentence, script.hook)
    ]
    public_text = clean_text(
        f"{script.title} {script.hook} {script.narration} {script.description}"
    )
    manipulative_hits = [
        label
        for label, pattern in MANIPULATIVE_ENGAGEMENT_PATTERNS.items()
        if pattern.search(public_text)
    ]
    selected = clean_text(script.engagement_prompt)
    selected_occurrences = script.narration.count(selected) if selected else 0
    near_end = bool(selected and sentences and clean_text(sentences[-1]) == selected)
    variants = [dict(item) for item in script.engagement_prompt_variants]
    selected_score = score_engagement_prompt(selected, script.engagement_prompt_type) if selected else 0.0
    recorded_score = float(script.engagement_score or 0.0)
    score = min(selected_score, recorded_score) if recorded_score else selected_score
    findings: list[str] = []
    if len(detected) != 1:
        findings.append(f"public narration has {len(detected)} engagement prompts; exactly one is required")
    if selected_occurrences != 1:
        findings.append("selected engagement prompt must appear exactly once in narration")
    if not near_end:
        findings.append("engagement prompt must be the final narration sentence")
    if len(variants) != 3:
        findings.append("engagement optimizer must retain exactly three internal variants")
    if manipulative_hits:
        findings.append("manipulative or false engagement language detected")
    if variants:
        highest = max(float(item.get("score", 0.0) or 0.0) for item in variants)
        if score + 0.01 < highest:
            findings.append("selected engagement prompt is not the highest-scoring variant")
    return {
        "engagement_score": round(score, 1),
        "engagement_prompt": selected,
        "engagement_prompt_type": script.engagement_prompt_type,
        "engagement_prompt_count": len(detected),
        "engagement_prompt_near_end": near_end,
        "engagement_prompt_variants": variants,
        "comments_oriented": script.engagement_prompt_type
        in {"opinion", "disagreement", "personal_experience"},
        "manipulative_engagement_hits": manipulative_hits,
        "findings": findings,
        "allowed": not findings,
    }


class EngagementPromptOptimizer:
    def __init__(self, config: dict[str, Any]):
        settings = config.get("story_mode", {}).get("engagement_prompt", {})
        self.enabled = bool(settings.get("enabled", True))

    def apply(self, script: ContentScript, topic: TrendTopic) -> ContentScript:
        if not self.enabled:
            return script

        narration_sentences = [
            sentence
            for sentence in split_sentences(script.narration)
            if not is_engagement_prompt(sentence, script.hook)
        ]
        variants = [
            {
                "type": prompt_type,
                "text": prompt,
                "score": score_engagement_prompt(prompt, prompt_type),
            }
            for prompt_type, prompt in _variant_templates(_story_category(topic))
        ]
        selected = max(variants, key=lambda item: float(item["score"]))
        narration = clean_text(" ".join([*narration_sentences, str(selected["text"])]))
        return replace(
            script,
            narration=narration,
            engagement_prompt=str(selected["text"]),
            engagement_prompt_type=str(selected["type"]),
            engagement_score=float(selected["score"]),
            engagement_prompt_variants=variants,
        )
