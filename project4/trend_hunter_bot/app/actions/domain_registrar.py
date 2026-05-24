from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import requests

from app.config.settings import ConfigError, DomainSettings
from app.models import DomainAction


DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class DomainRegistrar:
    def __init__(self, settings: DomainSettings, paper_mode: bool) -> None:
        self.settings = settings
        self.paper_mode = paper_mode

    def process_candidates(self, candidates: tuple[str, ...]) -> tuple[DomainAction, ...]:
        actions: list[DomainAction] = []
        registrations = 0

        for domain in candidates:
            available = self.check_availability(domain)
            if available is True and registrations < self.settings.register_max_per_trend:
                action = self.register_domain(domain)
                registrations += 1
            elif available is True:
                action = DomainAction(
                    domain=domain,
                    available=True,
                    action="available_not_registered",
                    mode=self._mode,
                    reason="registration limit reached for this trend",
                )
            elif available is False:
                action = DomainAction(
                    domain=domain,
                    available=False,
                    action="skipped",
                    mode=self._mode,
                    reason="domain is already registered",
                )
            else:
                action = DomainAction(
                    domain=domain,
                    available=None,
                    action="skipped",
                    mode=self._mode,
                    reason="availability could not be verified safely",
                )
            actions.append(action)

        return tuple(actions)

    def check_availability(self, domain: str) -> bool | None:
        normalized = domain.strip().lower()
        if not DOMAIN_RE.match(normalized):
            return None

        whois_result = self._check_whois(normalized)
        if whois_result is not None:
            return whois_result
        return self._check_rdap(normalized)

    def register_domain(self, domain: str) -> DomainAction:
        normalized = domain.strip().lower()
        if not DOMAIN_RE.match(normalized):
            return DomainAction(
                domain=domain,
                available=None,
                action="skipped",
                mode=self._mode,
                reason="invalid domain format",
            )

        if self.paper_mode:
            return DomainAction(
                domain=normalized,
                available=True,
                action="paper_registered",
                mode=self._mode,
                reason="paper mode recorded the registration intent without purchasing",
            )

        self._validate_godaddy_config()
        availability = self._godaddy_available(normalized)
        if availability is not True:
            return DomainAction(
                domain=normalized,
                available=availability,
                action="skipped",
                mode=self._mode,
                reason="GoDaddy availability check did not confirm availability",
            )

        response = requests.post(
            f"{self.settings.godaddy_base_url.rstrip('/')}/v1/domains/purchase",
            headers=self._godaddy_headers(),
            json=self._purchase_payload(normalized),
            timeout=30,
        )
        if response.status_code not in {200, 201, 202}:
            raise ConfigError(f"GoDaddy purchase failed for {normalized}: {response.status_code} {response.text}")

        return DomainAction(
            domain=normalized,
            available=True,
            action="registered",
            mode=self._mode,
            reason="domain purchase request accepted by GoDaddy",
        )

    @property
    def _mode(self) -> str:
        return "paper" if self.paper_mode else "production"

    def _check_whois(self, domain: str) -> bool | None:
        try:
            import whois
        except ImportError:
            return None

        try:
            result = whois.whois(domain)
        except Exception as exc:
            message = str(exc).lower()
            available_markers = (
                "no match",
                "not found",
                "no data found",
                "status: free",
                "available",
                "object does not exist",
            )
            if any(marker in message for marker in available_markers):
                return True
            return None

        domain_name = getattr(result, "domain_name", None)
        if not domain_name:
            return True
        if isinstance(domain_name, list):
            return not any(domain_name)
        return False

    def _check_rdap(self, domain: str) -> bool | None:
        try:
            response = requests.get(f"https://rdap.org/domain/{domain}", timeout=12)
        except requests.RequestException:
            return None
        if response.status_code == 404:
            return True
        if response.status_code == 200:
            return False
        return None

    def _godaddy_available(self, domain: str) -> bool | None:
        response = requests.get(
            f"{self.settings.godaddy_base_url.rstrip('/')}/v1/domains/available",
            headers=self._godaddy_headers(),
            params={"domain": domain, "checkType": "FAST"},
            timeout=20,
        )
        if response.status_code != 200:
            raise ConfigError(f"GoDaddy availability failed for {domain}: {response.status_code} {response.text}")
        payload = response.json()
        available = payload.get("available")
        return bool(available) if available is not None else None

    def _validate_godaddy_config(self) -> None:
        if not self.settings.godaddy_api_key or not self.settings.godaddy_api_secret:
            raise ConfigError("GoDaddy API key and secret are required for production registrations")

        required_contact_fields = {
            "first_name",
            "last_name",
            "email",
            "phone",
            "address1",
            "city",
            "state",
            "postal_code",
            "country",
        }
        missing = sorted(field for field in required_contact_fields if not self.settings.contact.get(field))
        if missing:
            raise ConfigError(f"GoDaddy contact configuration is missing: {', '.join(missing)}")

    def _godaddy_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"sso-key {self.settings.godaddy_api_key}:{self.settings.godaddy_api_secret}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _purchase_payload(self, domain: str) -> dict[str, Any]:
        contact = self._contact_payload()
        payload = {
            "domain": domain,
            "period": self.settings.period_years,
            "privacy": self.settings.privacy,
            "renewAuto": self.settings.auto_renew,
            "contactAdmin": contact,
            "contactBilling": contact,
            "contactRegistrant": contact,
            "contactTech": contact,
            "consent": {
                "agreedAt": datetime.now(timezone.utc).isoformat(),
                "agreedBy": "Trend Hunter Bot",
                "agreementKeys": ["DNRA"],
            },
        }
        if self.settings.shopper_id:
            payload["shopperId"] = self.settings.shopper_id
        return payload

    def _contact_payload(self) -> dict[str, Any]:
        contact = self.settings.contact
        address = {
            "address1": contact["address1"],
            "city": contact["city"],
            "state": contact["state"],
            "postalCode": contact["postal_code"],
            "country": contact["country"],
        }
        if contact.get("address2"):
            address["address2"] = contact["address2"]
        return {
            "nameFirst": contact["first_name"],
            "nameLast": contact["last_name"],
            "email": contact["email"],
            "phone": contact["phone"],
            "addressMailing": address,
        }

