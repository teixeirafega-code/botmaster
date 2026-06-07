from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse

import requests

from app.models import ResearchBrief, TrendTopic
from app.utils.text import clean_text, split_sentences


LOGGER = logging.getLogger(__name__)


class ResearchService:
    def __init__(self, config: dict):
        self.config = config
        self.research_config = config.get("research", {})
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ShortsMasterBot/1.0 factual research"})

    def build_brief(self, topic: TrendTopic) -> ResearchBrief:
        source_summaries: list[dict[str, str]] = []
        source_urls: list[str] = []
        fact_candidates: list[tuple[str, str]] = []
        uncertainty_notes: list[str] = []

        self._add_trend_context(topic, source_summaries, source_urls, fact_candidates)
        self._add_reddit_context(topic, source_summaries, source_urls, fact_candidates)
        self._add_wikipedia_context(topic, source_summaries, source_urls, fact_candidates, uncertainty_notes)

        facts = self._dedupe_facts([fact for fact, _source in fact_candidates])[:7]
        text_blob = " ".join([topic.title, *facts, *(item.get("summary", "") for item in source_summaries)])
        names = self._extract_entities(text_blob)
        dates = self._extract_dates(text_blob)
        locations = self._extract_locations(text_blob)
        why_now = self._why_now(topic, facts, source_summaries)

        if len(facts) < 3:
            uncertainty_notes.append(f"Only {len(facts)} concrete facts found; minimum is 3")

        requires_fresh = self._requires_fresh_source(topic, text_blob)
        requires_trusted = requires_fresh
        self._apply_source_trust_scores(source_summaries)
        source_dates = self._source_dates(source_summaries)
        freshness_window = int(self.research_config.get("freshness_window_hours", 72))
        freshness_threshold = float(self.research_config.get("freshness_min_score", 70))
        freshness_score, freshness_notes = self._freshness_analysis(
            source_dates,
            requires_fresh,
            freshness_window,
            freshness_threshold,
        )
        trust_threshold = float(self.research_config.get("trust_min_score", 70))
        trust_score, trust_notes = self._trust_analysis(source_summaries, requires_trusted, trust_threshold)

        return ResearchBrief(
            topic=topic.title,
            category=topic.niche,
            why_now=why_now,
            concrete_facts=facts,
            names_entities=names,
            dates_times=dates,
            locations=locations,
            uncertainty_notes=self._dedupe_text(uncertainty_notes),
            source_urls=self._dedupe_text(source_urls),
            source_summaries=source_summaries,
            source_dates=source_dates,
            freshness_score=freshness_score,
            freshness_threshold=freshness_threshold,
            freshness_window_hours=freshness_window,
            requires_fresh_source=requires_fresh,
            freshness_notes=freshness_notes,
            trust_score=trust_score,
            trust_threshold=trust_threshold,
            requires_trusted_source=requires_trusted,
            trust_notes=trust_notes,
        )

    def _add_trend_context(
        self,
        topic: TrendTopic,
        summaries: list[dict[str, str]],
        urls: list[str],
        facts: list[tuple[str, str]],
    ) -> None:
        raw = topic.raw or {}
        if topic.url:
            urls.append(topic.url)
        if raw.get("link"):
            urls.append(str(raw["link"]))
        source_date = self._coerce_source_date(raw.get("published_at") or raw.get("pubDate"))
        traffic = clean_text(str(raw.get("traffic", "")))
        rank = raw.get("rank")
        if traffic:
            facts.append((f"Google Trends reported about {traffic} searches for this topic.", "google_trends"))
        if rank:
            facts.append((f"Google Trends RSS ranked this topic at position {rank} in the current feed.", "google_trends"))
        if raw.get("summary") or traffic or rank:
            summary_item = {
                "source": "Google Trends RSS",
                "title": topic.title,
                "summary": clean_text(str(raw.get("summary") or self._why_now(topic, [], []))),
                "url": str(raw.get("link", topic.url or "")),
            }
            if source_date:
                summary_item["source_date"] = source_date
            summaries.append(summary_item)
        if raw.get("summary"):
            self._sentences_to_facts(str(raw["summary"]), facts, "google_trends_summary")
        for news in raw.get("news_items", [])[:4]:
            title = clean_text(str(news.get("news_item_title") or news.get("title") or ""))
            source = clean_text(str(news.get("news_item_source") or news.get("source") or "Google Trends related news"))
            url = clean_text(str(news.get("news_item_url") or news.get("url") or ""))
            snippet = clean_text(str(news.get("news_item_snippet") or news.get("snippet") or ""))
            news_date = self._coerce_source_date(
                news.get("news_item_publish_date")
                or news.get("news_item_published_at")
                or news.get("published_at")
                or news.get("pubDate")
                or news.get("date")
            )
            if url:
                urls.append(url)
            summary = ". ".join(part for part in [title, snippet] if part)
            if summary:
                item = {"source": source, "title": title or topic.title, "summary": summary, "url": url}
                if news_date:
                    item["source_date"] = news_date
                summaries.append(item)
                self._sentences_to_facts(summary, facts, source)

    def _add_reddit_context(
        self,
        topic: TrendTopic,
        summaries: list[dict[str, str]],
        urls: list[str],
        facts: list[tuple[str, str]],
    ) -> None:
        if topic.source != "reddit":
            return
        raw = topic.raw or {}
        url = topic.url or str(raw.get("url", ""))
        if url:
            urls.append(url)
        source_date = self._coerce_source_date(raw.get("created_utc") or raw.get("created_at"))
        pieces = [
            f"Reddit post title: {topic.title}",
            f"Subreddit: r/{raw.get('subreddit')}" if raw.get("subreddit") else "",
            f"Upvotes: {raw.get('ups')}" if raw.get("ups") is not None else "",
            f"Comments: {raw.get('comments')}" if raw.get("comments") is not None else "",
        ]
        summary = ". ".join(part for part in pieces if part)
        if summary:
            item = {"source": "Reddit", "title": topic.title, "summary": summary, "url": url}
            if source_date:
                item["source_date"] = source_date
            summaries.append(item)
            self._sentences_to_facts(summary, facts, "reddit")

    def _add_wikipedia_context(
        self,
        topic: TrendTopic,
        summaries: list[dict[str, str]],
        urls: list[str],
        facts: list[tuple[str, str]],
        uncertainty_notes: list[str],
    ) -> None:
        if not self.research_config.get("wikipedia_enabled", True):
            return
        query = self._wikipedia_query(topic.title)
        if not query:
            return
        try:
            title = self._wikipedia_search(query)
            if not title:
                uncertainty_notes.append(f"Wikipedia search found no page for '{query}'")
                return
            response = self.session.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}",
                timeout=float(self.research_config.get("timeout_seconds", 12)),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            LOGGER.warning("Wikipedia research failed for %s: %s", topic.title, exc)
            uncertainty_notes.append(f"Wikipedia lookup failed: {exc}")
            return
        extract = clean_text(str(payload.get("extract", "")))
        page_title = str(payload.get("title", title))
        if not self._is_relevant_wikipedia_result(topic.title, page_title, extract):
            uncertainty_notes.append(
                f"Wikipedia result '{page_title}' was rejected as not specific enough for '{topic.title}'"
            )
            return
        page_url = payload.get("content_urls", {}).get("desktop", {}).get("page", "")
        if page_url:
            urls.append(page_url)
        if extract:
            item = {"source": "Wikipedia", "title": page_title, "summary": extract, "url": page_url}
            source_date = self._coerce_source_date(payload.get("timestamp"))
            if source_date:
                item["source_date"] = source_date
            summaries.append(item)
            self._sentences_to_facts(extract, facts, "wikipedia")

    def _wikipedia_query(self, topic: str) -> str:
        cleaned = clean_text(topic)
        replacements = {
            "world cup schedule": "2026 FIFA World Cup",
            "world cup": "FIFA World Cup",
        }
        lower = cleaned.lower()
        for key, value in replacements.items():
            if key in lower:
                return value
        return cleaned

    def _wikipedia_search(self, query: str) -> str | None:
        response = self.session.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "opensearch",
                "search": query,
                "limit": 1,
                "namespace": 0,
                "format": "json",
            },
            timeout=float(self.research_config.get("timeout_seconds", 12)),
        )
        response.raise_for_status()
        payload = response.json()
        if len(payload) >= 2 and payload[1]:
            return str(payload[1][0])
        return None

    def _is_relevant_wikipedia_result(self, topic: str, page_title: str, extract: str) -> bool:
        topic_tokens = self._meaningful_tokens(topic)
        if not topic_tokens:
            return True
        title_tokens = self._meaningful_tokens(page_title)
        page_tokens = title_tokens | self._meaningful_tokens(extract)
        overlap = topic_tokens & page_tokens
        required_overlap = 2 if len(topic_tokens) > 1 else 1
        if len(overlap) < required_overlap:
            return False

        sports = {
            "softball",
            "football",
            "basketball",
            "baseball",
            "soccer",
            "cricket",
            "tennis",
            "hockey",
            "volleyball",
        }
        topic_sports = topic_tokens & sports
        page_sports = page_tokens & sports
        return not (topic_sports and page_sports and topic_sports.isdisjoint(page_sports))

    def _meaningful_tokens(self, text: str) -> set[str]:
        stopwords = {
            "about",
            "after",
            "before",
            "best",
            "current",
            "from",
            "game",
            "games",
            "into",
            "latest",
            "league",
            "match",
            "news",
            "schedule",
            "score",
            "scores",
            "season",
            "team",
            "teams",
            "today",
            "versus",
            "with",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9]+", clean_text(text).lower())
            if len(token) > 2 and token not in stopwords
        }

    def _sentences_to_facts(self, text: str, facts: list[tuple[str, str]], source: str) -> None:
        for sentence in split_sentences(text):
            fact = clean_text(sentence)
            if self._is_concrete_fact(fact):
                facts.append((fact, source))

    def _is_concrete_fact(self, sentence: str) -> bool:
        lower = sentence.lower()
        if len(sentence.split()) < 5:
            return False
        vague = ["gaining attention", "people are talking", "suddenly everywhere", "worth watching"]
        if any(phrase in lower for phrase in vague):
            return False
        has_number = bool(re.search(r"\b\d{1,4}(?:,\d{3})*(?:\.\d+)?\b", sentence))
        has_date = bool(re.search(self._date_pattern(), sentence, flags=re.IGNORECASE))
        has_entity = bool(re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b", sentence))
        has_context = any(
            word in lower
            for word in [
                "born",
                "plays",
                "team",
                "league",
                "tournament",
                "season",
                "championship",
                "university",
                "state",
                "country",
                "host",
                "match",
                "game",
                "searches",
                "ranked",
                "reported",
            ]
        )
        return (has_number or has_date or has_entity) and has_context

    def _why_now(
        self,
        topic: TrendTopic,
        facts: list[str],
        summaries: list[dict[str, str]],
    ) -> str:
        traffic = clean_text(str((topic.raw or {}).get("traffic", "")))
        if topic.source == "google_trends" and traffic:
            return f"Google Trends RSS shows current search demand for '{topic.title}' at about {traffic} searches."
        if summaries:
            source = summaries[0].get("source", "a free source")
            return f"{source} surfaced current context for '{topic.title}'."
        if facts:
            return f"Free research found factual context for '{topic.title}'."
        return f"Free research could not find enough current context for '{topic.title}'."

    def _source_dates(self, summaries: list[dict[str, str]]) -> list[dict[str, str]]:
        dated: list[dict[str, str]] = []
        for item in summaries:
            source_date = self._coerce_source_date(item.get("source_date"))
            if not source_date:
                continue
            dated.append(
                {
                    "source": clean_text(str(item.get("source", ""))),
                    "title": clean_text(str(item.get("title", ""))),
                    "url": clean_text(str(item.get("url", ""))),
                    "source_date": source_date,
                    "source_trust_score": item.get("source_trust_score", 0.0),
                }
            )
        return dated

    def _apply_source_trust_scores(self, summaries: list[dict[str, str]]) -> None:
        for item in summaries:
            score, reason = self._source_trust_score(item)
            item["source_trust_score"] = round(score, 1)
            item["source_trust_reason"] = reason

    def _trust_analysis(
        self,
        summaries: list[dict[str, str]],
        requires_trusted_source: bool,
        threshold: float,
    ) -> tuple[float, list[str]]:
        scores = []
        for item in summaries:
            try:
                scores.append(float(item.get("source_trust_score", 0.0)))
            except (TypeError, ValueError):
                continue
        if not scores:
            note = "topic has no research sources to trust-score"
            return 0.0, [note] if requires_trusted_source else [note + "; trusted source gate not required"]
        best = max(scores)
        average = sum(scores) / len(scores)
        aggregate = round(min(100.0, best * 0.75 + average * 0.25), 1)
        if not requires_trusted_source:
            return aggregate, [f"trusted source gate not required; aggregate source trust is {aggregate:.1f}"]
        if aggregate >= threshold:
            return aggregate, [f"trusted source requirement passed; best source score {best:.1f}"]
        low_sources = [
            f"{item.get('source', 'unknown source')} ({item.get('source_trust_score', '0')})"
            for item in summaries
            if float(item.get("source_trust_score", 0.0)) < threshold
        ][:3]
        return aggregate, [
            f"trusted source requirement failed; best source score {best:.1f}, aggregate {aggregate:.1f}",
            "low-trust sources: " + ", ".join(low_sources),
        ]

    def _source_trust_score(self, item: dict[str, str]) -> tuple[float, str]:
        source = clean_text(str(item.get("source", "")))
        title = clean_text(str(item.get("title", "")))
        url = clean_text(str(item.get("url", "")))
        source_date = clean_text(str(item.get("source_date", "")))
        domain = self._domain(url)
        text = f"{source} {title} {url} {domain}".lower()

        score = 52.0
        reason = "unknown or general source"
        official_domains = {
            "apnews.com": 95,
            "reuters.com": 95,
            "ncaa.com": 96,
            "wnba.com": 96,
            "nba.com": 96,
            "mlb.com": 96,
            "nfl.com": 96,
            "espn.com": 92,
        }
        for known_domain, known_score in official_domains.items():
            if domain == known_domain or domain.endswith("." + known_domain):
                score = float(known_score)
                reason = f"recognized sports source: {known_domain}"
                break

        if score == 52.0 and any(name in text for name in ["associated press", "ap news"]):
            score, reason = 95.0, "recognized wire source: Associated Press"
        elif score == 52.0 and "reuters" in text:
            score, reason = 95.0, "recognized wire source: Reuters"
        elif score == 52.0 and "espn" in text:
            score, reason = 92.0, "recognized sports source: ESPN"
        elif score == 52.0 and any(term in text for term in ["official athletics website", "official team", "official site"]):
            score, reason = 88.0, "official team or school page"
        elif score == 52.0 and (
            domain.endswith(".edu")
            or ("university" in text and "athletics" in text)
            or ("college" in text and "athletics" in text)
        ):
            score, reason = 84.0, "school athletics source"
        elif any(term in text for term in ["reddit", "tiktok", "instagram", "facebook", "twitter", "x.com"]):
            score, reason = 25.0, "social or community source"
        elif "wikipedia" in text:
            score, reason = 45.0, "Wikipedia is context only, not a primary trusted source"
        elif any(term in text for term in ["blog", "substack", "medium.com", "wordpress", "blogspot"]):
            score, reason = 38.0, "unknown blog source"
        elif "google trends rss" in text:
            score, reason = 64.0, "trend feed confirms demand but is not a primary factual source"

        if not source_date:
            score -= 15.0
            reason += "; undated source penalty"
        return max(0.0, min(100.0, score)), reason

    def _domain(self, url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url if "://" in url else f"https://{url}")
        domain = parsed.netloc.lower().removeprefix("www.")
        return domain

    def _freshness_analysis(
        self,
        source_dates: list[dict[str, str]],
        requires_fresh_source: bool,
        window_hours: int,
        threshold: float,
    ) -> tuple[float, list[str]]:
        if not requires_fresh_source:
            return 100.0, []
        if not source_dates:
            return 0.0, [f"current sports topic has no dated sources; requires one within {window_hours} hours"]

        now = datetime.now(timezone.utc)
        parsed_dates = [
            parsed
            for parsed in (self._parse_iso_datetime(item.get("source_date", "")) for item in source_dates)
            if parsed is not None
        ]
        if not parsed_dates:
            return 0.0, [f"current sports topic source dates could not be parsed; requires one within {window_hours} hours"]

        newest = max(parsed_dates)
        age_hours = max(0.0, (now - newest).total_seconds() / 3600)
        if age_hours <= window_hours:
            score = max(threshold, 100.0 - age_hours * 0.35)
            return round(min(100.0, score), 1), [f"fresh dated source found {age_hours:.1f} hours old"]

        stale_score = max(0.0, min(threshold - 1.0, threshold - 1.0 - ((age_hours - window_hours) * 0.25)))
        return round(stale_score, 1), [
            f"newest dated source is {age_hours:.1f} hours old; requires one within {window_hours} hours"
        ]

    def _requires_fresh_source(self, topic: TrendTopic, text_blob: str) -> bool:
        if not self.research_config.get("current_topic_freshness_enabled", True):
            return False
        text = f"{topic.title} {topic.niche} {text_blob}".lower()
        sports_terms = {
            "aces",
            "baseball",
            "basketball",
            "bowl",
            "championship",
            "college world series",
            "cricket",
            "final",
            "football",
            "fifa",
            "hockey",
            "mlb",
            "nba",
            "ncaa",
            "nfl",
            "nhl",
            "olympics",
            "playoff",
            "quarterfinal",
            "semifinal",
            "soccer",
            "softball",
            "sports",
            "tennis",
            "tournament",
            "ufc",
            "valkyries",
            "wnba",
            "world cup",
        }
        current_terms = {
            "breaking",
            "injury",
            "live",
            "odds",
            "prediction",
            "ranked",
            "schedule",
            "score",
            "today",
            "tonight",
            "trending",
            "vs",
        }
        has_sports_context = any(term in text for term in sports_terms)
        has_current_context = topic.source in {"google_trends", "reddit", "tiktok"} or any(term in text for term in current_terms)
        return has_sports_context or ("current" in text and has_current_context)

    def _coerce_source_date(self, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(microsecond=0).isoformat()
        text = clean_text(str(value))
        if not text:
            return ""
        parsed = self._parse_iso_datetime(text)
        if parsed:
            return parsed.replace(microsecond=0).isoformat()
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _parse_iso_datetime(self, value: str) -> datetime | None:
        text = clean_text(value)
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _dedupe_facts(self, facts: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for fact in facts:
            value = clean_text(fact).rstrip(".") + "."
            key = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            cleaned.append(value)
        return cleaned

    def _dedupe_text(self, values: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = clean_text(value)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            output.append(cleaned)
        return output

    def _extract_entities(self, text: str) -> list[str]:
        banned = {"The", "This", "That", "Google Trends", "Reddit", "Wikipedia"}
        matches = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b", text)
        return self._dedupe_text([match for match in matches if match not in banned])[:12]

    def _extract_dates(self, text: str) -> list[str]:
        values = re.findall(self._date_pattern(), text, flags=re.IGNORECASE)
        years = re.findall(r"\b(?:19|20)\d{2}\b", text)
        return self._dedupe_text([" ".join(item) if isinstance(item, tuple) else item for item in values] + years)[:10]

    def _extract_locations(self, text: str) -> list[str]:
        known = [
            "United States",
            "Canada",
            "Mexico",
            "Texas",
            "Nebraska",
            "Oregon",
            "New York",
            "Dominican Republic",
            "Brazil",
            "Argentina",
            "England",
            "France",
            "Germany",
            "Spain",
        ]
        lower = text.lower()
        return [place for place in known if place.lower() in lower]

    def _date_pattern(self) -> str:
        return r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)\s+\d{1,2}(?:,\s*\d{4})?\b"
