from __future__ import annotations

from app.models import ContentScript, TrendTopic
from app.services.validation import EndToEndValidator


def analyzer() -> EndToEndValidator:
    validator = object.__new__(EndToEndValidator)
    validator.config = {}
    return validator


def generic_world_cup_script() -> ContentScript:
    narration = (
        "O calendário da Copa do Mundo está em alta. Aqui está o sinal para observar. "
        "A atividade de busca está empurrando esse tema porque mistura curiosidade, timing e atenção compartilhada. "
        "Isso é importante não porque todo mundo está falando, mas porque a conversa vira perguntas e reações muito rápido. "
        "Quando um tema anda assim, criadores geralmente têm uma janela curta para explicar o contexto antes da tela lotar. "
        "A leitura esperta é focar no motivo da alta e mostrar um detalhe surpreendente que faça a tendência parecer útil. "
        "Se isso continuar se espalhando por busca, comentários e hashtags curtas, espere mais explicações amanhã. "
        "Final rápido: não é só uma tendência para notar, é uma tendência para entender antes de todo mundo."
    )
    scenes = [
        {"caption": "Calendário em alta", "image_prompt": "visual vertical abstrato de dados subindo como tendência"},
        {"caption": "Pico rápido", "image_prompt": "visual vertical de análise de tendência em rede social"},
        {"caption": "Perguntas surgem", "image_prompt": "visual vertical de telas de celular e comentários"},
        {"caption": "Contexto importa", "image_prompt": "visual vertical documental limpo e genérico"},
        {"caption": "Veja se espalhar", "image_prompt": "visual vertical de estúdio acompanhando tema viral"},
    ]
    return ContentScript(
        title="Calendário da Copa: por que está em alta",
        hook="Calendário da Copa está em alta.",
        narration=narration,
        scenes=scenes,
        tags=["geral", "viralbr", "shortsbr", "explicado"],
        description="Uma explicação rápida em português sobre a alta do calendário da Copa. #shortsbr #viralbr",
        niche="sports",
    )


def specific_world_cup_script() -> ContentScript:
    narration = (
        "O calendário da Copa de 2026 tem uma armadilha de viagem. O detalhe estranho é que os jogos começam a moldar planos "
        "antes do apito inicial. Primeira pista: o torneio cresce para 48 times. Depois fica mais claro: Estados Unidos, "
        "Canadá e México recebem partidas. O detalhe que muita gente perde: a fase de grupos começa em junho de 2026. "
        "Isso aumenta a tensão: as eliminatórias passam por oitavas, quartas, semifinais e final. "
        "Aqui vem a revelação: o calendário mostra quem descansa, quem viaja e quais jogos iniciais podem explodir. "
        "Final rápido: lembre da pista, viagem pode importar antes de a bola rolar."
    )
    scenes = [
        {"caption": "Armadilha de viagem", "image_prompt": "visual vertical mapa de viagem Copa do Mundo 2026 Estados Unidos Canadá México"},
        {"caption": "Planos começam cedo", "image_prompt": "visual vertical fãs planejando calendário da Copa em junho de 2026"},
        {"caption": "48 times", "image_prompt": "visual vertical torneio Copa do Mundo 2026 com 48 times sem texto"},
        {"caption": "Três sedes", "image_prompt": "visual vertical estádios de Estados Unidos Canadá México na Copa"},
        {"caption": "Grupos em junho", "image_prompt": "visual vertical calendário de futebol junho de 2026 fase de grupos sem texto"},
        {"caption": "Oitavas e final", "image_prompt": "visual vertical chave eliminatória da Copa com oitavas quartas semifinal final"},
        {"caption": "Viagem muda descanso", "image_prompt": "visual vertical rota de viagem de time por sedes da América do Norte"},
        {"caption": "A revelação", "image_prompt": "visual vertical revelação dramática do calendário da Copa de 2026"},
        {"caption": "Jogos explodem", "image_prompt": "visual vertical estádio lotado antes de clássico da Copa 2026"},
        {"caption": "Lembre da viagem", "image_prompt": "visual vertical fã com plano de viagem perto de estádio sem logos"},
    ]
    return ContentScript(
        title="Calendário da Copa 2026: a armadilha da viagem",
        hook="A Copa de 2026 tem uma armadilha de viagem.",
        narration=narration,
        scenes=scenes,
        tags=["copadomundo", "fifa", "futebol", "calendario"],
        description="Um olhar específico sobre calendário, sedes, fases e planejamento da Copa do Mundo de 2026.",
        niche="sports",
    )


def test_generic_fallback_quality_stays_below_75() -> None:
    result = analyzer()._analyze_quality(
        generic_world_cup_script(),
        TrendTopic(source="google_trends", title="calendário da copa do mundo", score=500),
        {"duration_seconds": 64.0},
        {"duration_seconds": 64.0},
        [{"source": "pollinations"} for _ in range(5)],
    )

    assert result["final_quality_score"] <= 75
    assert result["specificity_score"] < 70
    assert result["generic_language_penalty"] > 0


def test_specific_fact_rich_quality_can_score_above_85() -> None:
    result = analyzer()._analyze_quality(
        specific_world_cup_script(),
        TrendTopic(source="google_trends", title="calendário da copa do mundo", score=500),
        {"duration_seconds": 60.0},
        {"duration_seconds": 60.0},
        [{"source": "pollinations"} for _ in range(10)],
    )

    assert result["final_quality_score"] >= 85
    assert result["specificity_score"] >= 85
    assert result["hook_score"] >= 70
    assert result["retention_score"] >= 72
    assert result["visual_interest_score"] >= 70
    assert result["generic_language_penalty"] == 0


def test_static_slide_visuals_are_blocked() -> None:
    script = specific_world_cup_script()
    script.scenes = [
        {"caption": "Esta legenda longa parece relatório e lota a tela", "image_prompt": "slide de gradiente estático com texto"}
        for _ in range(5)
    ]
    result = analyzer()._analyze_quality(
        script,
        TrendTopic(source="google_trends", title="calendário da copa do mundo", score=500),
        {"duration_seconds": 60.0},
        {"duration_seconds": 60.0},
        [{"source": "pollinations"} for _ in range(5)],
    )

    assert result["blocked"] is True
    assert result["visual_interest_score"] < 70
    assert any("visual_interest_score" in reason for reason in result["blocking_failures"])
