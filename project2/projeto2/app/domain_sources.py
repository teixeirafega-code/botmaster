from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from app.models import DomainCandidate

DOMAIN_RE = re.compile(
    r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b",
    re.IGNORECASE,
)
DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%Y %H:%M",
    "%d/%m/%Y",
    "%d/%m/%Y %H:%M",
)


def normalize_domain(value: str) -> str:
    match = DOMAIN_RE.search(value.strip().lower())
    if not match:
        return ""
    domain = match.group(0).strip(".")
    if ".." in domain or domain.startswith("-") or ".-" in domain or "-." in domain:
        return ""
    return domain


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number = number // 1000
        return datetime.fromtimestamp(number, tz=UTC)
    text = text.replace(" UTC", "Z")
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def best_value(row: dict[str, Any], *names: str) -> Any:
    normalized = {str(key).strip().lower().replace(" ", "").replace("_", "").replace("-", ""): value for key, value in row.items()}
    for name in names:
        value = normalized.get(name.lower().replace(" ", "").replace("_", "").replace("-", ""))
        if value not in (None, ""):
            return value
    return None


async def get_bytes(url: str, headers: dict[str, str], timeout_seconds: int) -> tuple[bytes, str]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.read(), resp.headers.get("Content-Type", "")


def iter_zip_texts(payload: bytes) -> list[tuple[str, str]]:
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        return [("payload", payload.decode("utf-8", errors="replace"))]
    texts: list[tuple[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            with archive.open(name) as handle:
                texts.append((name, handle.read().decode("utf-8", errors="replace")))
    return texts


def parse_csv_text(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines[:5]))
    except csv.Error:
        dialect = csv.excel
    return [dict(row) for row in csv.DictReader(lines, dialect=dialect)]


def candidate_from_row(row: dict[str, Any], source: str) -> DomainCandidate | None:
    domain = normalize_domain(
        str(
            best_value(
                row,
                "domain",
                "domainname",
                "domain_name",
                "fqdn",
                "name",
                "sld",
            )
            or ""
        )
    )
    if not domain:
        for value in row.values():
            domain = normalize_domain(str(value))
            if domain:
                break
    if not domain:
        return None

    target_time = parse_datetime(
        best_value(
            row,
            "dropdate",
            "drop_date",
            "deletiondate",
            "expirydate",
            "expirationdate",
            "auctionendtime",
            "auctionend",
            "endtime",
            "orderby",
            "availabledate",
        )
    )
    age_years = _int_value(best_value(row, "age", "ageyears", "domainage"))
    backlinks = _int_value(best_value(row, "backlinks", "bl", "refdomains", "referringdomains"))
    metadata = {str(key): value for key, value in row.items() if value not in (None, "")}
    return DomainCandidate(
        name=domain,
        source=source,
        age_years=age_years,
        backlinks=backlinks,
        expires_at=target_time,
        auction_end_at=target_time,
        source_metadata=metadata,
    )


def candidates_from_json_payload(text: str, source: str) -> list[DomainCandidate]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows: list[dict[str, Any]] = []
    _collect_rows(payload, rows)
    candidates = []
    seen: set[str] = set()
    for row in rows:
        candidate = candidate_from_row(row, source)
        if candidate and candidate.name not in seen:
            seen.add(candidate.name)
            candidates.append(candidate)
    return candidates


def candidates_from_html_links(html: str, base_url: str, source: str) -> list[DomainCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[DomainCandidate] = []
    seen: set[str] = set()
    for node in soup.select("td, a, span, div"):
        domain = normalize_domain(node.get_text(" ", strip=True))
        if domain and domain not in seen:
            seen.add(domain)
            candidates.append(DomainCandidate(name=domain, source=source, source_metadata={"source_url": base_url}))
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if isinstance(href, str) and any(marker in href.lower() for marker in (".csv", ".zip", "download")):
            urljoin(base_url, href)
    return candidates


def candidates_from_csv_text(text: str, source: str) -> list[DomainCandidate]:
    candidates: list[DomainCandidate] = []
    seen: set[str] = set()
    rows = parse_csv_text(text)
    if not rows:
        rows = [{"domain": line.strip()} for line in text.splitlines() if line.strip()]
    for row in rows:
        candidate = candidate_from_row(row, source)
        if candidate and candidate.name not in seen:
            seen.add(candidate.name)
            candidates.append(candidate)
    return candidates


def _collect_rows(value: Any, rows: list[dict[str, Any]]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_rows(item, rows)
        return
    if not isinstance(value, dict):
        return
    if any(normalize_domain(str(item)) for item in value.values()):
        rows.append(value)
    for item in value.values():
        if isinstance(item, (list, dict)):
            _collect_rows(item, rows)


def _int_value(value: Any) -> int:
    text = str(value or "").strip().replace(",", "")
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else 0
