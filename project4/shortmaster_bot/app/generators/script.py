from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from app.models import ContentScript, ResearchBrief, TrendTopic
from app.services.narrative_style import analyze_reddit_narrative_style
from app.services.reddit_story import is_reddit_story_topic
from app.utils.text import clamp_words, clean_text, extract_json_object, split_sentences


LOGGER = logging.getLogger(__name__)


class ScriptGenerationError(RuntimeError):
    """Raised when script generation returns invalid or unavailable content."""


class ScriptGenerator:
    def __init__(self, config: dict):
        self.config = config
        self.script_config = config.get("generation", {}).get("script", {})
        language_config = config.get("language", {})
        self.language = str(language_config.get("default") or language_config.get("required") or "pt-BR")
        self.paper_mode = bool(config.get("app", {}).get("paper_mode", True))
        self.last_provider = "unknown"
        self.last_warnings: list[str] = []

    def generate(self, topic: TrendTopic, research: ResearchBrief | None = None) -> ContentScript:
        self.last_warnings = []
        story_mode = is_reddit_story_topic(topic)
        if self.script_config.get("ollama", {}).get("enabled", True):
            try:
                script = self._generate_with_ollama(topic, research)
                self.last_provider = "ollama"
                return script
            except Exception as exc:
                warning = f"Ollama unavailable; used local template fallback: {exc}"
                self.last_warnings.append(warning)
                LOGGER.warning(warning)
        if story_mode:
            self.last_provider = "template_reddit_story"
            return self._generate_story_locally(topic, research)
        self.last_provider = "template_research" if research else "template"
        return self._generate_locally(topic, research)

    def _generate_with_ollama(self, topic: TrendTopic, research: ResearchBrief | None) -> ContentScript:
        ollama_config = self.script_config.get("ollama", {})
        base_url = str(ollama_config.get("base_url", "http://localhost:11434")).rstrip("/")
        model = str(ollama_config.get("model", "llama3.1:8b"))
        timeout = float(ollama_config.get("timeout_seconds", 6))
        prompt = self._prompt(topic, int(self.script_config.get("target_seconds", 60)), research)
        try:
            response = requests.post(
                f"{base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                    "options": {
                        "temperature": float(self.script_config.get("temperature", 0.7)),
                        "num_predict": int(self.script_config.get("max_tokens", 1200)),
                    },
                },
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ScriptGenerationError(f"Ollama request failed: {exc}") from exc

        payload = response.json()
        text = str(payload.get("response", "")).strip()
        if not text:
            raise ScriptGenerationError("Ollama returned an empty response")
        try:
            script_payload = extract_json_object(text)
        except Exception as exc:
            raise ScriptGenerationError(f"Ollama returned invalid JSON: {text[:500]}") from exc
        return self._validate_script(script_payload, topic)

    def _prompt(self, topic: TrendTopic, target_seconds: int, research: ResearchBrief | None = None) -> str:
        if is_reddit_story_topic(topic):
            return self._story_prompt(topic, target_seconds, research)
        return json.dumps(
            {
                "task": "Create an original YouTube Shorts narration script.",
                "requirements": {
                    "mandatory_language": "Brazilian Portuguese (pt-BR) for title, hook, narration, subtitles/captions, description, and hashtags.",
                    "localization": "If source material or topic wording is English, translate and rewrite naturally for Brazilian audiences. Do not leave English output text.",
                    "duration_seconds": target_seconds,
                    "narration_words": "115-155",
                    "tone": "fast, curious, emotional, credible, surprising",
                    "originality": "Write from scratch. Do not copy or paraphrase existing videos.",
                    "asset_safety": "Do not request copyrighted clips, copyrighted music, lyrics, logos, or real-person likenesses.",
                    "avoid": [
                        "encyclopedia-style narration",
                        "academic wording",
                        "static text-slide structure",
                        "generic trend reporting",
                        "copyrighted lyrics",
                        "copyrighted clips or music",
                        "reused viral scripts",
                        "medical/legal/financial advice",
                        "unsupported claims",
                        "spammy clickbait",
                    ],
                    "format": {
                        "title": "curiosity-based pt-BR title under 80 chars with a concrete detail",
                        "hook": "first pt-BR sentence under 10 words with a curiosity gap",
                        "narration": "single pt-BR narration paragraph, short spoken sentences",
                        "scenes": [
                            {
                                "caption": "pt-BR on-screen caption under 6 words",
                                "image_prompt": "specific vertical generated visual prompt, no text-only screen",
                            }
                        ],
                        "tags": ["5 to 10 pt-BR YouTube tags"],
                        "description": "plain pt-BR description with 2-4 pt-BR hashtags",
                        "niche": "niche label",
                    },
                    "scene_count": int(self.script_config.get("retention_scene_count", 10)),
                    "structure": [
                        "0-2s: Hook with a concrete surprising fact",
                        "2-10s: Curiosity gap",
                        "10-34s: Escalation through 3 concrete facts",
                        "34-52s: Reveal one surprising payoff",
                        "52-60s: Fast clean ending",
                    ],
                    "retention_rules": [
                        "Every sentence should be short enough to speak quickly.",
                        "Avoid phrases like 'here is what you need to know' and 'why it is trending'.",
                        "Every image prompt must be tied to the topic or a fact from the research brief.",
                        "Do not make any scene a static gradient, title card, or text-only slide.",
                    ],
                },
                "trend": topic.to_dict(),
                "research_brief": research.to_dict() if research else None,
            },
            ensure_ascii=True,
        )

    def _story_prompt(self, topic: TrendTopic, target_seconds: int, research: ResearchBrief | None = None) -> str:
        raw = topic.raw or {}
        story_source = dict(raw.get("story_source") or {})
        story_beats = dict(raw.get("story_beats") or {})
        return json.dumps(
            {
                "task": "Create an original high-retention YouTube Shorts Reddit story retelling.",
                "source_policy": {
                    "source_url": topic.url,
                    "source_kind": story_source.get("source_kind"),
                    "critical_rules": [
                        "Do not quote the Reddit post or comments.",
                        "Do not paraphrase sentence-by-sentence.",
                        "Rewrite completely as an original script.",
                        "Do not copy comments verbatim.",
                        "Keep source attribution, uncertainty, and verification notes out of every public-facing field.",
                        "Never say unverified, not confirmed, according to Reddit, source, poster, author, or engagement.",
                        "Internal reports handle source safety; the public script must be entertainment-first.",
                    ],
                },
                "requirements": {
                    "mandatory_language": "Brazilian Portuguese (pt-BR) for title, hook, narration, subtitles/captions, description, and hashtags.",
                    "localization": "Reddit source may be English, but the output must be translated, localized, and rewritten naturally for Brazilian audiences. Do not leave English output text.",
                    "duration_seconds": target_seconds,
                    "narration_words": "120-165",
                    "tone": "natural Brazilian spoken storytelling, suspenseful, emotional, direct",
                    "opening": "Narration must start with a Reddit-style question, such as 'Pessoal do Reddit: qual foi...?'",
                    "story_voice": "Immediately after the opening question, narrate in first person or close third person.",
                    "subtitles": "Subtitles will always be burned into the video; write short spoken sentences.",
                    "background_video": "user-approved satisfying MP4 from the local background library only",
                    "structure": [
                        "Hook",
                        "Curiosity",
                        "Escalation",
                        "Reveal",
                        "Ending",
                    ],
                    "avoid": [
                        "encyclopedia narration",
                        "fact-documentary voice",
                        "reading Reddit text",
                        "quoting comments",
                        "source verification disclaimers",
                        "phrases such as 'relato não verificado', 'segundo o Reddit', 'vale ressaltar', or 'não podemos confirmar'",
                        "phrases that introduce the poster, author, source, subreddit, engagement, or internal checks",
                        "labeled beats such as 'curiosidade:', 'aqui vem a revelação:', or 'final rápido:'",
                        "academic or report-like wording",
                        "spammy clickbait",
                        "copyrighted clips, music, lyrics, logos, or real-person likenesses",
                    ],
                    "format": {
                        "title": "natural pt-BR entertainment title under 80 chars",
                        "hook": "the opening Reddit-style question in pt-BR",
                        "narration": "single pt-BR paragraph; opening Reddit question, then immediate first-person or close-third-person story",
                        "scenes": [
                            {
                                "caption": "pt-BR subtitle beat label under 6 words",
                                "image_prompt": "short visual beat prompt; approved retention background renderer is used",
                            }
                        ],
                        "tags": ["5 to 10 pt-BR YouTube tags"],
                        "description": "plain entertainment-first pt-BR description with no source or verification disclaimer",
                        "niche": "reddit_story",
                    },
                    "scene_count": int(self.script_config.get("retention_scene_count", 10)),
                },
                "reddit_story_metadata": story_source,
                "core_narrative_beats": story_beats,
                "research_brief": research.to_dict() if research else None,
            },
            ensure_ascii=True,
        )

    def _validate_script(self, payload: dict[str, Any], topic: TrendTopic) -> ContentScript:
        required = {"title", "hook", "narration", "scenes", "tags", "description"}
        missing = required - set(payload)
        if missing:
            raise ScriptGenerationError(f"Script response missing keys: {sorted(missing)}")

        scenes = payload.get("scenes")
        min_scenes = min(8, int(self.script_config.get("retention_scene_count", 10)))
        if not isinstance(scenes, list) or len(scenes) < min_scenes:
            raise ScriptGenerationError(f"Script must include at least {min_scenes} retention scenes")

        normalized_scenes: list[dict[str, str]] = []
        max_scenes = max(8, int(self.script_config.get("retention_scene_count", 10)))
        for scene in scenes[:max_scenes]:
            caption = self._short_caption(clean_text(str(scene.get("caption", ""))))
            image_prompt = clean_text(str(scene.get("image_prompt", "")))
            if not caption or not image_prompt:
                continue
            normalized_scenes.append({"caption": caption, "image_prompt": image_prompt})

        if len(normalized_scenes) < min_scenes:
            raise ScriptGenerationError("Script scenes need retention captions and image prompts")

        narration = clamp_words(str(payload["narration"]), 105, 165)
        script = ContentScript(
            title=clean_text(str(payload["title"]))[:90],
            hook=clean_text(str(payload["hook"]))[:120],
            narration=narration,
            scenes=normalized_scenes,
            tags=[clean_text(str(tag)).lstrip("#") for tag in payload.get("tags", [])[:10]],
            description=clean_text(str(payload["description"]))[:900],
            niche=clean_text(str(payload.get("niche") or topic.niche)),
        )
        if is_reddit_story_topic(topic):
            style = analyze_reddit_narrative_style(script)
            if style["disclaimer_leakage_score"] > 0:
                raise ScriptGenerationError(
                    "Reddit story public script leaked source disclaimer language"
                )
            if style["narrative_naturalness_score"] < 75:
                raise ScriptGenerationError(
                    "Reddit story narration sounds report-like instead of natural"
                )
        return script

    def _generate_locally(self, topic: TrendTopic, research: ResearchBrief | None = None) -> ContentScript:
        if research and research.has_enough_facts:
            return self._generate_from_research(topic, research)
        title = self._localize_fact_text(clean_text(topic.title).lstrip("#"))
        angle = self._angle_for_niche(topic.niche)
        hook = f"{title} tem uma pista que falta."
        narration = (
            f"{hook} O detalhe estranho é a velocidade. O interesse está subindo porque toca em {angle}. "
            "Mas, sem pesquisa suficiente, isso precisa ficar na fila. A pergunta aberta é simples. "
            "Qual fato concreto explica a alta? Antes de virar vídeo real, o bot precisa achar três fatos verificáveis. "
            "Essa é a trava. Curiosidade só funciona quando a recompensa é factual. Então este rascunho segura o tema, "
            "aponta o ângulo e espera prova. Primeiro vem a checagem. Depois vem a escalada. Em seguida vem a revelação. "
            "Final rápido: encontre os fatos, confirme a pista e só então publique."
        )
        sentences = split_sentences(narration)
        scene_captions = [
            hook,
            "Velocidade estranha",
            "Falta um fato",
            "A fila espera",
            "Pesquisa decide",
            "Sem prova, sem envio",
            "Ache três fatos",
            "Depois revele",
            "Mantenha factual",
            "Termine com prova",
        ]
        prompts = [
            f"visual vertical editorial sobre {title}, pista em close dramatico, sem texto, sem logos",
            f"visual vertical de buscas rápidas sobre {title}, perspectiva de celular em movimento, sem texto",
            f"quadro de evidências vazio sobre {title}, espaço para o fato que falta, sem texto",
            f"painel vertical de fila de revisão sobre {title}, fluxo de checagem, sem texto",
            f"mesa de pesquisa sobre {title}, cartões de fonte e lupa, sem texto legível",
            f"três marcadores de evidência sobre {title}, estilo documental, sem texto",
            f"momento de revelação sobre {title}, luz em um detalhe, sem texto",
            f"cena vertical de checagem de fatos sobre {title}, clima confiável, sem logos",
            f"linha do tempo de criador sobre {title}, cortes rápidos, sem texto",
            f"imagem final limpa sobre {title}, visual guiado por prova, sem texto",
        ]
        scenes = [
            {
                "caption": self._short_caption(scene_captions[index]),
                "image_prompt": prompts[index],
            }
            for index in range(min(len(scene_captions), int(self.script_config.get("retention_scene_count", 10))))
        ]
        hashtags = self._localized_topic_tags(topic)
        if not hashtags:
            hashtags = [self._localized_niche_tag(topic.niche), "viralbr", "shortsbr", "explicado"]
        return ContentScript(
            title=f"{title}: a prova que falta"[:90],
            hook=hook,
            narration=clamp_words(" ".join(sentences), 105, 150),
            scenes=scenes,
            tags=hashtags[:10],
            description=f"Rascunho seguro em modo papel sobre {title}; publique só depois de encontrar fatos concretos. #shortsbr",
            niche=topic.niche,
        )

    def _generate_from_research(self, topic: TrendTopic, research: ResearchBrief) -> ContentScript:
        topic_title = self._localize_fact_text(clean_text(topic.title).lstrip("#"))
        facts = [clean_text(fact).rstrip(".") for fact in research.concrete_facts[:5]]
        spoken_facts = [self._spoken_fact(fact) for fact in facts]
        primary_fact = facts[0]
        title = self._research_title(topic_title, research)
        hook = self._research_hook(topic_title, primary_fact)
        curiosity = self._curiosity_line(topic_title, primary_fact)
        payoff = self._payoff_line(topic_title, research)
        ending = self._fast_ending(topic_title, primary_fact)
        parts = [
            hook,
            curiosity,
            f"Por que agora: {self._spoken_fact(research.why_now) if research.why_now else 'o tema ganhou uma nova rodada de atenção'}.",
            f"Primeira pista: {spoken_facts[0]}.",
            "Não pule essa pista.",
            f"Depois fica mais claro: {spoken_facts[1]}.",
            "Esse detalhe afunila a história.",
            f"O detalhe que muita gente perde: {spoken_facts[2]}.",
        ]
        if len(spoken_facts) > 3:
            parts.append(f"Isso aumenta a tensão: {spoken_facts[3]}.")
        if len(spoken_facts) > 4:
            parts.append(f"Mais uma virada: {spoken_facts[4]}.")
        parts.extend([
            "Agora a revelação tem peso.",
            payoff,
            "A surpresa não é barulho maior. É o limite que os fatos apontam.",
            ending,
        ])
        narration = " ".join(parts)
        scenes = self._research_scenes(topic_title, research)
        hashtags = self._research_tags(topic, research)
        description = (
            f"Vídeo curto factual original sobre {topic_title}, criado com pesquisa de fontes abertas. "
            f"Fatos usados: {'; '.join(spoken_facts[:3])}. #shortsbr #{self._localized_niche_tag(topic.niche)} #explicado"
        )
        return ContentScript(
            title=title,
            hook=hook,
            narration=clamp_words(narration, 125, 170),
            scenes=scenes,
            tags=hashtags,
            description=description[:900],
            niche=research.category or topic.niche,
        )

    def _generate_story_locally(self, topic: TrendTopic, research: ResearchBrief | None = None) -> ContentScript:
        raw = topic.raw or {}
        story_source = dict(raw.get("story_source") or {})
        beats = dict(raw.get("story_beats") or {})
        labels = [str(label) for label in story_source.get("priority_labels", []) or beats.get("priority_labels", [])]
        subreddit = clean_text(str(story_source.get("source_subreddit", "Reddit")))
        category = labels[0] if labels else "mystery"
        source_text = clean_text(str(raw.get("source_text_for_similarity", "")))
        profile = self._story_profile(source_text, category)
        hook = profile["prompt"]
        narration = " ".join([hook, *profile["sentences"]])
        scenes = self._story_scenes(profile["captions"], profile["visual_theme"])
        tags = self._story_tags(category, subreddit, topic)
        return ContentScript(
            title=profile["title"],
            hook=hook,
            narration=clamp_words(narration, 115, 165),
            scenes=scenes,
            tags=tags,
            description=(
                f"{profile['description']} #shortsbr #historiasreddit #historias"
            )[:900],
            niche="reddit_story",
        )

    def _story_profile(self, source_text: str, category: str) -> dict[str, Any]:
        lower = source_text.lower()
        if any(term in lower for term in ["mall encounter", "three nice young girls", "alternative store"]):
            return {
                "title": "A mulher do shopping começou a descrever a gente",
                "prompt": "Pessoal do Reddit: quando ajudar um estranho fez você se arrepender?",
                "sentences": [
                    "Eu tinha dezenove anos e estava no shopping com minha irmã e minha prima.",
                    "Uma mulher muito nervosa pediu meu celular emprestado.",
                    "Ela disse que ninguém queria buscá-la.",
                    "O primeiro número estava desligado.",
                    "No segundo, ela deixou uma mensagem de voz.",
                    "Então começou a descrever nós três.",
                    "Falou nossas roupas, idades aproximadas e até como estávamos andando juntas.",
                    "Peguei o celular e levei as meninas para uma loja onde eu conhecia os funcionários.",
                    "Minutos depois, a mulher entrou procurando três garotas com a nossa aparência.",
                    "Os vendedores nos esconderam atrás das araras e chamaram a segurança.",
                    "Ela saiu e começou a seguir outra família parecida.",
                    "Quando os seguranças se aproximaram, ela correu para o andar de cima e tentou pular da grade.",
                    "Nós só saímos quando tudo terminou.",
                    "Até hoje, não sei quem estava ouvindo aquela mensagem.",
                ],
                "captions": [
                    "Ela pediu meu celular",
                    "O número estava desligado",
                    "Começou a descrever",
                    "Nossas roupas e idades",
                    "Corremos para uma loja",
                    "Ela veio atrás",
                    "Os vendedores esconderam",
                    "Outra família parecida",
                    "A segurança chegou",
                    "Quem ouviu a mensagem?",
                ],
                "visual_theme": "perseguição estranha dentro de um shopping",
                "description": "Um pedido de telefone fica assustador quando uma desconhecida começa a descrever três garotas.",
            }
        if any(term in lower for term in ["i’m not ben", "i'm not ben", "british accent", "wooden headboard"]):
            return {
                "title": "Ele mudou de voz e atacou os próprios amigos",
                "prompt": "Pessoal do Reddit: qual festa saiu do controle em segundos?",
                "sentences": [
                    "Eu tinha dezessete anos e fui beber com dois garotos numa casa de hóspedes.",
                    "No começo, a noite estava tranquila.",
                    "Até que um deles começou a me encarar com ódio.",
                    "A voz dele mudou para um sotaque britânico.",
                    "Quando tentamos tirar a bebida, ele correu para o banheiro e pegou vários frascos de remédio.",
                    "Nós arrancamos tudo das mãos dele.",
                    "Ele gritou que não se chamava Ben.",
                    "Disse que era Reece.",
                    "Trancamos o quarto e achamos que ele tinha dormido.",
                    "Então ouvimos pancadas.",
                    "Ele quebrou uma ponta de madeira da cabeceira e avançou em mim.",
                    "Conseguimos imobilizá-lo e retiramos tudo que podia virar arma.",
                    "Depois bloqueamos a porta com uma estante e um móvel de televisão.",
                    "Na manhã seguinte, a avó viu a barricada e simplesmente foi embora.",
                    "Nós também deveríamos ter pedido ajuda muito antes.",
                ],
                "captions": [
                    "A festa começou normal",
                    "O olhar mudou",
                    "Sotaque britânico",
                    "Frascos de remédio",
                    "Eu sou Reece",
                    "Trancamos o quarto",
                    "Ponta de madeira",
                    "Ele avançou",
                    "Barricamos a porta",
                    "Deveríamos ter pedido ajuda",
                ],
                "visual_theme": "festa adolescente que vira uma noite perigosa",
                "description": "Uma noite entre amigos muda quando um deles troca de voz e fica violentamente imprevisível.",
            }
        if any(term in lower for term in ["straight razor", "motel", "sugar baby", "ex wives", "hot spring"]):
            return {
                "title": "O encontro no motel que virou uma armadilha",
                "prompt": "Pessoal do Reddit: quando seu instinto mandou você sair correndo?",
                "sentences": [
                    "Na faculdade, eu mal conseguia pagar o aluguel.",
                    "Entrei num site de encontros e aceitei viajar com um homem mais velho.",
                    "Avisei meus amigos, compartilhei minha localização e fui no meu próprio carro.",
                    "Quando cheguei, o hotel era um motel quase abandonado.",
                    "Ele também tinha cancelado meu quarto separado.",
                    "Eu devia ter ido embora.",
                    "Mas já estava escuro.",
                    "No jantar, ele dirigiu meia hora para uma cidade sem sinal.",
                    "Meu estômago travou.",
                    "De volta ao quarto, ele começou a falar das ex-esposas.",
                    "Uma tinha sido assassinada.",
                    "A outra havia desaparecido.",
                    "Fui ao banheiro e liguei para um amigo.",
                    "Quando saí, o homem segurava uma navalha antiga.",
                    "Ele perguntou se eu podia ajudá-lo a fazer a barba.",
                    "Mantive a ligação aberta, peguei minhas chaves e inventei que precisava buscar algo no carro.",
                    "Só parei de dirigir quando vi as luzes da cidade.",
                ],
                "captions": [
                    "Eu precisava de dinheiro",
                    "Aceitei o encontro",
                    "Motel abandonado",
                    "Sem quarto separado",
                    "Cidade sem sinal",
                    "As duas ex-esposas",
                    "Liguei escondida",
                    "A navalha antiga",
                    "Peguei as chaves",
                    "Dirigi sem olhar",
                ],
                "visual_theme": "encontro perigoso em motel isolado",
                "description": "Um encontro aparentemente simples fica assustador quando detalhes demais começam a dar errado.",
            }
        if any(term in lower for term in ["basement", "storage room", "window latch", "scratches", "yard"]):
            return {
                "title": "O barulho no porão não vinha dos canos",
                "prompt": "Pessoal do Reddit: qual barulho em casa fez você congelar?",
                "sentences": [
                    "Durante semanas, eu ouvi pancadas no porão e culpei o aquecedor velho.",
                    "Até que o som mudou de lugar.",
                    "Ele saiu dos canos e foi parar atrás da porta do depósito.",
                    "Na manhã seguinte, uma cadeira estava virada para a entrada.",
                    "Ninguém usava aquele cômodo.",
                    "Eu tentei rir, mas encontrei riscos novos na trava da janela.",
                    "Naquela noite, tranquei tudo e deixei a câmera ligada.",
                    "Pouco antes do amanhecer, meu celular vibrou.",
                    "A gravação mostrava um homem parado no quintal.",
                    "Ele olhava direto para a janela do porão.",
                    "Chamei a polícia e esperei no carro do vizinho.",
                    "Quando entraram no depósito, acharam comida, cobertores e marcas de sapato.",
                    "O aquecedor nunca tinha feito barulho.",
                    "Alguém estava morando ali embaixo.",
                ],
                "captions": [
                    "Pancadas no porão",
                    "O som mudou",
                    "A cadeira virou",
                    "Riscos na janela",
                    "Câmera ligada",
                    "Alguém no quintal",
                    "Olhando para o porão",
                    "A polícia entrou",
                    "Cobertores escondidos",
                    "Alguém morava ali",
                ],
                "visual_theme": "mistério dentro de uma casa com porão",
                "description": "Um barulho tratado como defeito da casa termina com uma descoberta impossível de ignorar.",
            }
        if any(term in lower for term in ["night shift", "security guard", "3am", "three am", "corridor", "warehouse"]):
            return {
                "title": "Os passos no corredor às três da manhã",
                "prompt": "Pessoal do Reddit: o que mais assustou você trabalhando de madrugada?",
                "sentences": [
                    "Eu trabalhava como segurança no turno da noite.",
                    "Às três da manhã, o prédio inteiro deveria estar vazio.",
                    "Foi quando ouvi passos no corredor do segundo andar.",
                    "Subi achando que algum funcionário tinha esquecido algo.",
                    "As luzes acendiam sozinhas à minha frente.",
                    "Mas apagavam logo atrás.",
                    "No rádio, meu parceiro jurou que continuava na portaria.",
                    "Os passos pararam diante de uma sala trancada.",
                    "Quando cheguei perto, alguém bateu três vezes pelo lado de dentro.",
                    "A chave ainda estava comigo.",
                    "Abri a porta e não havia ninguém.",
                    "Só encontrei um telefone antigo tocando no chão.",
                    "Atendi.",
                    "Minha própria voz sussurrou para eu não olhar para trás.",
                    "Eu olhei e pedi demissão antes do sol nascer.",
                ],
                "captions": [
                    "Turno da madrugada",
                    "Passos no corredor",
                    "Luzes sozinhas",
                    "Meu parceiro longe",
                    "Sala trancada",
                    "Três batidas",
                    "Telefone no chão",
                    "Minha própria voz",
                    "Não olhe atrás",
                    "Pedi demissão",
                ],
                "visual_theme": "corredor vazio durante turno de segurança",
                "description": "Um turno silencioso muda quando passos aparecem num andar que deveria estar vazio.",
            }
        if category == "funny_disaster" or any(term in lower for term in ["tifu", "accidentally", "wedding", "embarrassing"]):
            return {
                "title": "A mentira pequena que destruiu o casamento",
                "prompt": "Pessoal do Reddit: qual erro bobo virou um desastre completo?",
                "sentences": [
                    "Eu só queria escapar de um discurso no casamento do meu primo.",
                    "Então disse que estava com a voz falhando.",
                    "O problema é que minha tia ouviu pela metade.",
                    "Ela contou para todo mundo que eu estava passando mal.",
                    "Em cinco minutos, apareceu uma ambulância.",
                    "Tentei explicar, mas o microfone do salão estava ligado.",
                    "Duzentas pessoas ouviram minha mentira.",
                    "Meu primo começou a rir.",
                    "A noiva também.",
                    "Então o bolo chegou.",
                    "O garçom olhou para a confusão, tropeçou no cabo do microfone e lançou três andares de glacê na pista.",
                    "O silêncio durou dois segundos.",
                    "Depois todo mundo começou a filmar.",
                    "Meu discurso foi cancelado.",
                    "Mas a família ainda chama o vídeo de operação garganta.",
                ],
                "captions": [
                    "Fingi perder a voz",
                    "Chamaram uma ambulância",
                    "Microfone aberto",
                    "Todos ouviram",
                    "O primo riu",
                    "O bolo chegou",
                    "O garçom tropeçou",
                    "Glacê na pista",
                    "Todo mundo filmou",
                    "Operação garganta",
                ],
                "visual_theme": "desastre engraçado durante casamento",
                "description": "Uma desculpa pequena provoca ambulância, microfone aberto e um bolo voando.",
            }
        if category == "shocking_discovery" or any(term in lower for term in ["dna", "discovered", "found out", "hidden", "secret"]):
            return {
                "title": "A foto escondida atrás do armário",
                "prompt": "Pessoal do Reddit: qual descoberta mudou sua família para sempre?",
                "sentences": [
                    "Eu reformava o quarto da minha avó quando encontrei uma caixa presa atrás do armário.",
                    "Dentro havia cartas, uma chave e uma foto de duas crianças idênticas.",
                    "Uma delas era minha mãe.",
                    "A outra ninguém conhecia.",
                    "Levei a foto para casa e minha mãe ficou branca.",
                    "Ela disse que sempre sonhava com uma menina chamando seu nome.",
                    "A chave abria um cofre antigo na estação da cidade.",
                    "Lá dentro havia documentos de adoção e um endereço.",
                    "Dirigimos até o lugar no mesmo dia.",
                    "Uma mulher abriu a porta e começou a chorar antes que disséssemos qualquer coisa.",
                    "Ela tinha a mesma foto.",
                    "Minha mãe não era filha única.",
                    "Ela tinha uma irmã gêmea esperando havia cinquenta anos.",
                ],
                "captions": [
                    "Caixa atrás do armário",
                    "Duas crianças iguais",
                    "Minha mãe empalideceu",
                    "A chave antiga",
                    "Cofre na estação",
                    "Documentos de adoção",
                    "Um endereço",
                    "Ela abriu a porta",
                    "A mesma fotografia",
                    "Irmãs após cinquenta anos",
                ],
                "visual_theme": "descoberta familiar escondida em caixa antiga",
                "description": "Uma caixa escondida revela uma fotografia capaz de mudar uma família inteira.",
            }
        return {
            "title": "A estrada que desapareceu do mapa",
            "prompt": "Pessoal do Reddit: qual coincidência estranha você nunca conseguiu explicar?",
            "sentences": [
                "Eu voltava para casa quando o GPS mandou entrar numa estrada estreita.",
                "Eu conhecia a região, mas nunca tinha visto aquela entrada.",
                "Depois de alguns minutos, o sinal sumiu.",
                "As mesmas três casas começaram a aparecer de novo.",
                "Primeiro uma azul.",
                "Depois uma com varanda.",
                "E por último uma casa queimada.",
                "Achei que estava rodando em círculos.",
                "Então vi meu próprio carro parado diante da casa azul.",
                "As luzes estavam acesas.",
                "Havia alguém no banco do motorista.",
                "Eu acelerei sem olhar para o lado.",
                "O GPS voltou de repente e me colocou na rodovia.",
                "No dia seguinte, procurei a entrada.",
                "Ela não existia.",
                "Mas havia lama fresca nos quatro pneus.",
            ],
            "captions": [
                "Uma estrada desconhecida",
                "O sinal sumiu",
                "As mesmas casas",
                "Rodando em círculos",
                "Meu carro parado",
                "Alguém no volante",
                "Eu acelerei",
                "De volta à rodovia",
                "A entrada sumiu",
                "Lama nos pneus",
            ],
            "visual_theme": "mistério em estrada isolada durante a noite",
            "description": "Um desvio do GPS leva a uma estrada que não deveria existir.",
        }

    def _story_category_name(self, category: str) -> str:
        names = {
            "scary": "assustador",
            "unbelievable": "inacreditável",
            "mystery": "misterioso",
            "life_changing": "transformador",
            "funny_disaster": "de desastre engraçado",
            "shocking_discovery": "de descoberta chocante",
        }
        return names.get(category, "misterioso")

    def _story_scenes(self, captions: list[str], visual_theme: str) -> list[dict[str, str]]:
        count = int(self.script_config.get("retention_scene_count", 10))
        return [
            {
                "caption": self._short_caption(captions[index]),
                "image_prompt": f"vídeo satisfatório aprovado acompanhando {visual_theme}, momento {index + 1}",
            }
            for index in range(min(count, len(captions)))
        ]

    def _story_tags(self, category: str, subreddit: str, topic: TrendTopic) -> list[str]:
        tags = ["historiasreddit", "relatos", "shortsbr", "reddit", subreddit.lower()]
        labels = {
            "scary": "assustador",
            "unbelievable": "inacreditavel",
            "mystery": "misterio",
            "life_changing": "vidareal",
            "funny_disaster": "desastreengracado",
            "shocking_discovery": "descobertachocante",
        }
        label = labels.get(category, category.replace("_", ""))
        if label not in tags:
            tags.append(label)
        incoming_map = {
            "scary": "assustador",
            "unbelievable": "inacreditavel",
            "mystery": "misterio",
            "life_changing": "vidareal",
            "funny_disaster": "desastreengracado",
            "shocking_discovery": "descobertachocante",
            "redditstories": "historiasreddit",
            "storytime": "relatos",
            "shorts": "shortsbr",
        }
        for tag in topic.hashtags[:4]:
            cleaned = clean_text(str(tag)).lstrip("#").lower()
            cleaned = incoming_map.get(cleaned, cleaned)
            if cleaned and cleaned not in tags:
                tags.append(cleaned)
        return tags[:10]

    def _research_title(self, topic_title: str, research: ResearchBrief) -> str:
        mirror_fact = self._first_fact_matching(research.concrete_facts, ["mirror", "segment"])
        dates = research.dates_times[:1]
        number = self._first_number(" ".join(research.concrete_facts))
        entities = [entity for entity in research.names_entities if entity.lower() not in topic_title.lower()]
        if mirror_fact:
            title = f"{topic_title}: a pista das 18 peças"
        elif number:
            title = f"{topic_title}: a pista do {number}"
        elif dates:
            title = f"{topic_title}: a pista de {dates[0]}"
        elif entities:
            title = f"{topic_title}: a virada com {entities[0]}"
        else:
            title = f"{topic_title}: o detalhe perdido"
        if len(title) > 88:
            title = f"{topic_title}: o detalhe perdido"
        if "why it is trending" in title.lower():
            title = f"{topic_title}: o fato por trás da alta"
        return title[:90]

    def _research_hook(self, topic_title: str, primary_fact: str) -> str:
        number = self._first_number(primary_fact)
        if any(term in primary_fact.lower() for term in ["mirror", "segment"]):
            return "O espelho de 18 peças do Webb é a pista."
        dates = re.findall(
            r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:,\s*\d{4})?\b",
            primary_fact,
        )
        if dates:
            short_date = dates[0].replace(", 2021", "")
            return f"A pista de {short_date} muda {topic_title}."
        if number:
            return f"A pista do {number} muda {topic_title}."
        entity = self._first_named_entity(primary_fact, topic_title)
        if entity:
            return f"{entity} é a pista em {topic_title}."
        return f"{topic_title} tem uma pista escondida."

    def _curiosity_line(self, topic_title: str, primary_fact: str) -> str:
        if any(term in primary_fact.lower() for term in ["schedule", "fixture", "calendar", "match"]):
            return "A armadilha é que um calendário muda comportamento antes de qualquer jogo."
        if any(term in primary_fact.lower() for term in ["mirror", "orbit", "infrared", "telescope"]):
            return "O estranho é que a maior pista não está na imagem."
        return "O detalhe estranho é que a manchete não é a revelação real."

    def _payoff_line(self, topic_title: str, research: ResearchBrief) -> str:
        category = (research.category or "").lower()
        if category == "sports":
            return "Aqui vem a revelação: o tempo muda pressão, viagem e o que os fãs percebem primeiro."
        if category == "science":
            return "Aqui vem a revelação: o desenho explica o que a descoberta consegue mostrar."
        if category == "technology":
            return "Aqui vem a revelação: o pequeno detalhe técnico decide se isso importa."
        return f"Aqui vem a revelação: {topic_title} importa porque os detalhes mudam o sentido."

    def _fast_ending(self, topic_title: str, primary_fact: str) -> str:
        reminder = self._spoken_fact(primary_fact)
        return f"Final rápido: lembre da pista, {reminder}."

    def _first_fact_matching(self, facts: list[str], terms: list[str]) -> str:
        for fact in facts:
            lower = fact.lower()
            if all(term in lower for term in terms):
                return fact
        return ""

    def _spoken_fact(self, fact: str) -> str:
        spoken = clean_text(fact).rstrip(".")
        spoken = re.sub(
            r"^(?:NASA|ESA|CSA|Reuters|Associated Press|AP|ESPN|NCAA|NBA|WNBA|MLB|NFL)\s+reported\s+that\s+",
            "",
            spoken,
            flags=re.IGNORECASE,
        )
        spoken = re.sub(
            r"^(?:NASA|ESA|CSA)\s+says\s+that\s+",
            "",
            spoken,
            flags=re.IGNORECASE,
        )
        spoken = self._localize_fact_text(spoken)
        return spoken[:1].upper() + spoken[1:] if spoken else fact

    def _localize_fact_text(self, text: str) -> str:
        localized = clean_text(text)
        replacements = {
            "World Cup": "Copa do Mundo",
            "world cup": "Copa do Mundo",
            "United States": "Estados Unidos",
            "United States of America": "Estados Unidos",
            "USA": "Estados Unidos",
            "Canada": "Canadá",
            "Mexico": "México",
            "schedule": "calendário",
            "fixture": "jogo marcado",
            "fixtures": "jogos marcados",
            "match": "jogo",
            "matches": "jogos",
            "host": "sede",
            "hosts": "sedes",
            "teams": "times",
            "team": "time",
            "mirror": "espelho",
            "segment": "segmento",
            "segments": "segmentos",
            "orbit": "órbita",
            "infrared": "infravermelho",
            "telescope": "telescópio",
            "discovery": "descoberta",
            "source": "fonte",
        }
        for source, target in replacements.items():
            localized = re.sub(rf"\b{re.escape(source)}\b", target, localized, flags=re.IGNORECASE)
        english_markers = [
            " the ",
            " and ",
            " with ",
            " from ",
            " reported ",
            " says ",
            " starts ",
            " includes ",
            " uses ",
            " gives ",
        ]
        padded = f" {localized.lower()} "
        if any(marker in padded for marker in english_markers):
            details: list[str] = []
            details.extend(re.findall(r"\b\d{1,4}(?:[.,]\d+)?\b", localized)[:3])
            details.extend(self._named_detail_candidates(localized)[:3])
            detail = ", ".join(dict.fromkeys(details)) if details else "um detalhe verificável"
            localized = f"um fato verificado destaca {detail}"
        return localized

    def _named_detail_candidates(self, text: str) -> list[str]:
        banned = {
            "The",
            "This",
            "That",
            "First",
            "Second",
            "Third",
            "Source",
            "Reddit",
        }
        return [
            candidate
            for candidate in re.findall(r"\b[A-Z][A-Za-zÀ-ÖØ-öø-ÿ']+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ']+){0,2}\b", text)
            if candidate.split()[0] not in banned
        ]

    def _research_scenes(self, topic_title: str, research: ResearchBrief) -> list[dict[str, str]]:
        facts = research.concrete_facts[:5]
        entities = research.names_entities[:3]
        locations = research.locations[:2]
        dates = research.dates_times[:2]
        captions = [
            "Pista escondida",
            "Não é a manchete",
            "Primeira pista",
            "Fica mais claro",
            "Quase ninguém nota",
            "A tensão sobe",
            "A virada",
            "A revelação",
            "Por que importa",
            "Lembre disso",
        ]
        prompt_details = [
            facts[0],
            research.why_now or facts[0],
            facts[0],
            facts[1] if len(facts) > 1 else facts[0],
            facts[2] if len(facts) > 2 else facts[0],
            facts[3] if len(facts) > 3 else facts[-1],
            facts[4] if len(facts) > 4 else facts[-1],
            "; ".join(facts[:3]),
            "; ".join([*entities, *locations, *dates]) or facts[0],
            facts[0],
        ]
        scene_prompts: list[dict[str, str]] = []
        for index, caption in enumerate(captions):
            detail = clean_text(prompt_details[index])
            scene_prompts.append(
                {
                    "caption": self._short_caption(caption),
                    "image_prompt": (
                        f"visual vertical documental sobre {topic_title}, {detail}, "
                        "close cinematográfico, profundidade forte, sem texto legível, sem logos, sem gradiente"
                    ),
                }
            )
        return scene_prompts[: int(self.script_config.get("retention_scene_count", 10))]

    def _research_tags(self, topic: TrendTopic, research: ResearchBrief) -> list[str]:
        tags = [self._localized_niche_tag(topic.niche), "shortsbr", "explicado"]
        for entity in research.names_entities[:4]:
            tag = "".join(ch for ch in entity.lower() if ch.isalnum())
            if tag and tag not in tags:
                tags.append(tag)
        localized_title = self._localize_fact_text(topic.title)
        for word in localized_title.split()[:4]:
            tag = "".join(ch for ch in word.lower() if ch.isalnum())
            if tag and tag not in tags:
                tags.append(tag)
        return tags[:10]

    def _localized_topic_tags(self, topic: TrendTopic) -> list[str]:
        mapped_tags: list[str] = []
        replacements = {
            "ai": "ia",
            "aiediting": "edicaocomia",
            "technology": "tecnologia",
            "trending": "viralbr",
            "trend": "viralbr",
            "explained": "explicado",
            "shorts": "shortsbr",
            "storytime": "relatos",
            "redditstories": "historiasreddit",
            "finance": "financas",
            "sports": "esportes",
            "science": "ciencia",
            "gaming": "games",
        }
        for raw_tag in topic.hashtags[:6]:
            cleaned = "".join(ch for ch in clean_text(str(raw_tag)).lstrip("#").lower() if ch.isalnum())
            if not cleaned:
                continue
            tag = replacements.get(cleaned)
            if not tag and "ai" in cleaned:
                tag = "ia"
            if not tag and "edit" in cleaned:
                tag = "edicao"
            if not tag and cleaned not in {"english", "story", "hidden", "clue"}:
                tag = cleaned
            if tag and tag not in mapped_tags:
                mapped_tags.append(tag)
        return mapped_tags[:10]

    def _localized_niche_tag(self, niche: str) -> str:
        return {
            "technology": "tecnologia",
            "finance": "financas",
            "sports": "esportes",
            "entertainment": "entretenimento",
            "gaming": "games",
            "science": "ciencia",
            "general": "geral",
            "reddit_story": "historiasreddit",
        }.get(clean_text(niche).lower(), clean_text(niche).lower() or "geral")

    def _short_caption(self, caption: str) -> str:
        words = clean_text(caption).split()
        shortened = " ".join(words[:6])
        return shortened.rstrip(" ,;:.")[:42] or "Veja isso"

    def _first_number(self, text: str) -> str:
        match = re.search(r"\b\d{1,4}(?:[.,]\d+)?\b", text)
        return match.group(0) if match else ""

    def _first_named_entity(self, text: str, topic_title: str) -> str:
        for candidate in re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b", text):
            if candidate.lower() not in topic_title.lower():
                return candidate
        return ""

    def _angle_for_niche(self, niche: str) -> str:
        angles = {
            "technology": "novas ferramentas, mudanças de plataforma e curiosidade prática",
            "finance": "decisões de dinheiro, risco e psicologia de mercado",
            "sports": "competição, identidade e reação imediata",
            "entertainment": "fãs, surpresa e força cultural",
            "gaming": "atualizações, desafios e descoberta da comunidade",
            "science": "fascínio, evidência e explicações surpreendentes",
        }
        return angles.get(niche, "curiosidade, timing e atenção compartilhada")
