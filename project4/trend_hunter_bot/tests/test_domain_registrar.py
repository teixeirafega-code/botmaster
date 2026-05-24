from app.actions.domain_registrar import DomainRegistrar
from app.config.settings import DomainSettings


def test_paper_mode_registration_records_intent_without_purchase() -> None:
    registrar = DomainRegistrar(DomainSettings(), paper_mode=True)

    action = registrar.register_domain("trendtest.local")

    assert action.available is True
    assert action.action == "paper_registered"
    assert action.mode == "paper"


def test_invalid_domain_is_skipped() -> None:
    registrar = DomainRegistrar(DomainSettings(), paper_mode=True)

    action = registrar.register_domain("not a domain")

    assert action.available is None
    assert action.action == "skipped"
