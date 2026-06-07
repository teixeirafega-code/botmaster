from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.models import ContentScript
from app.utils.text import clean_text, split_sentences


DISCLAIMER_PHRASES = [
    "nao podemos confirmar",
    "nao e possivel confirmar",
    "nao foi confirmado",
    "nao verificado",
    "nao verificada",
    "nao comprovado",
    "nao comprovada",
    "pode nao ser verdade",
    "o assunto nao e verdade",
    "segundo o reddit",
    "vale ressaltar",
    "fonte interna",
    "essa historia veio",
    "essa historia e de",
    "um autor do reddit",
    "o autor do reddit",
    "a fonte passou",
    "checagem de engajamento",
    "releitura original",
    "nao sao lidos literalmente",
    "must be treated as",
    "unverified story",
    "not verified",
    "not confirmed",
    "cannot confirm",
]

REPORT_PHRASES = [
    "curiosidade:",
    "primeira pista:",
    "aqui vem a revelacao",
    "a recompensa e",
    "final rapido:",
    "a fonte",
    "checagem",
    "engajamento",
    "entrou na fila",
    "deve ser tratado",
    "o autor achou",
    "o autor contou",
    "story priority",
    "source validation",
]


def normalize_style_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", clean_text(value).lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def public_script_text(script: ContentScript) -> str:
    captions = " ".join(scene.get("caption", "") for scene in script.scenes)
    return clean_text(
        f"{script.title} {script.hook} {script.narration} {script.description} {captions}"
    )


def analyze_reddit_narrative_style(script: ContentScript) -> dict[str, Any]:
    public_text = normalize_style_text(public_script_text(script))
    narration = clean_text(script.narration)
    normalized_narration = normalize_style_text(narration)
    sentences = split_sentences(narration)
    first_sentence = normalize_style_text(sentences[0] if sentences else "")

    disclaimer_hits = [phrase for phrase in DISCLAIMER_PHRASES if phrase in public_text]
    report_hits = [phrase for phrase in REPORT_PHRASES if phrase in normalized_narration]
    disclaimer_leakage_score = min(100.0, len(disclaimer_hits) * 35.0)

    score = 100.0
    findings: list[str] = []
    prompt_ok = (
        ("reddit" in first_sentence or "pessoal do reddit" in first_sentence)
        and "?" in (sentences[0] if sentences else "")
    )
    if not prompt_ok:
        score -= 28
        findings.append("narration must open with a Reddit-style question")

    body = " ".join(sentences[1:]) if len(sentences) > 1 else narration
    normalized_body = normalize_style_text(body)
    first_person_terms = [
        " eu ",
        " meu ",
        " minha ",
        " comigo ",
        " estava ",
        " fui ",
        " vi ",
        " ouvi ",
        " senti ",
        " percebi ",
        " peguei ",
    ]
    padded_body = f" {normalized_body} "
    first_person_count = sum(padded_body.count(term) for term in first_person_terms)
    close_third_person = bool(
        re.search(r"\b(ele|ela)\b", normalized_body)
        and re.search(r"\b(estava|foi|viu|ouviu|sentiu|percebeu)\b", normalized_body)
    )
    if first_person_count < 3 and not close_third_person:
        score -= 25
        findings.append("story body lacks a clear first-person or close-third-person voice")

    if sentences:
        average_words = sum(len(sentence.split()) for sentence in sentences) / len(sentences)
        if average_words > 16:
            score -= min(22.0, (average_words - 16) * 3)
            findings.append(f"average sentence length sounds report-like ({average_words:.1f} words)")
    else:
        average_words = 0.0
        score -= 30
        findings.append("narration has no spoken sentence structure")

    if report_hits:
        score -= min(45.0, len(report_hits) * 12.0)
        findings.append("report-like phrases leaked into narration: " + ", ".join(report_hits[:5]))
    if disclaimer_hits:
        score -= min(70.0, len(disclaimer_hits) * 35.0)
        findings.append("source disclaimer leaked into public script: " + ", ".join(disclaimer_hits[:5]))

    colon_labels = len(re.findall(r"\b[a-záàâãéêíóôõúç ]{3,24}:", normalized_narration))
    if colon_labels > 1:
        score -= min(20.0, (colon_labels - 1) * 7.0)
        findings.append("narration uses too many labeled report beats")

    return {
        "narrative_naturalness_score": round(max(0.0, min(100.0, score)), 1),
        "disclaimer_leakage_score": round(disclaimer_leakage_score, 1),
        "disclaimer_hits": disclaimer_hits,
        "report_phrase_hits": report_hits,
        "reddit_prompt_present": prompt_ok,
        "first_person_signal_count": first_person_count,
        "close_third_person_present": close_third_person,
        "average_sentence_words": round(average_words, 1),
        "findings": findings,
    }
