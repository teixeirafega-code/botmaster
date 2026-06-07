from __future__ import annotations

from pathlib import Path

from app.models import ContentScript
from app.services.language import LanguageGuard
from tests.conftest import make_test_config


def portuguese_script() -> ContentScript:
    narration = (
        "Este relato do Reddit tem uma pista escondida. Um autor contou a história como relato não verificado. "
        "Curiosidade: a primeira pista parecia pequena demais para importar. Primeiro, tudo parecia comum. "
        "Depois fica mais tenso: o detalhe volta no pior momento. Em seguida, as coincidências se acumulam. "
        "Aqui vem a revelação: a pista mudava o sentido da história inteira. Final rápido: lembre da pista."
    )
    return ContentScript(
        title="A pista escondida no relato do Reddit",
        hook="Este relato tem uma pista escondida.",
        narration=narration,
        scenes=[
            {"caption": "Pista escondida", "image_prompt": "fundo satisfatório"},
            {"caption": "Relato do Reddit", "image_prompt": "fundo satisfatório"},
            {"caption": "Primeiro detalhe", "image_prompt": "fundo satisfatório"},
            {"caption": "Fica tenso", "image_prompt": "fundo satisfatório"},
            {"caption": "A revelação", "image_prompt": "fundo satisfatório"},
            {"caption": "Final rápido", "image_prompt": "fundo satisfatório"},
            {"caption": "Não verificado", "image_prompt": "fundo satisfatório"},
            {"caption": "Lembre da pista", "image_prompt": "fundo satisfatório"},
        ],
        tags=["historiasreddit", "relatos", "shortsbr"],
        description="Releitura original em português de um relato não verificado do Reddit. #shortsbr",
        niche="reddit_story",
    )


def test_language_guard_accepts_pt_br_output(tmp_path: Path) -> None:
    decision = LanguageGuard(make_test_config(tmp_path)).evaluate_script(portuguese_script())

    assert decision["allowed"] is True
    assert decision["script_language"] == "pt-BR"
    assert decision["narration_language"] == "pt-BR"
    assert decision["subtitle_language"] == "pt-BR"
    assert decision["title_language"] == "pt-BR"


def test_language_guard_blocks_english_output(tmp_path: Path) -> None:
    script = portuguese_script()
    script.title = "The hidden clue in this Reddit story"
    script.hook = "This Reddit story has one hidden clue."
    script.narration = "This Reddit story has one hidden clue. Here is the reveal. Fast ending: remember the clue."

    decision = LanguageGuard(make_test_config(tmp_path)).evaluate_script(script)

    assert decision["allowed"] is False
    assert decision["title_language"] == "en"
    assert any("English text remains" in reason for reason in decision["reasons"])
