import pytest

from app.config.settings import Settings
from app.db.postgres import MemoryDomainRepository
from app.services.telegram_notifier import TelegramNotifier


class FailingBot:
    async def send_message(self, **_kwargs):
        raise RuntimeError("telegram down")


class RecordingBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return object()


class TokenLeakingFailingBot:
    def __init__(self):
        self.attempts = 0

    async def send_message(self, **_kwargs):
        self.attempts += 1
        raise RuntimeError("telegram-token leaked")


async def run_without_retry(_provider, operation, **_kwargs):
    return await operation()


@pytest.mark.asyncio
async def test_send_message_skips_when_not_configured():
    notifier = TelegramNotifier(Settings(telegram_bot_token=None, telegram_chat_id=None))
    assert await notifier.send_message("hello") is False


def test_safe_error_redacts_secrets():
    settings = Settings(godaddy_api_key="secret-key", telegram_bot_token="telegram-token")
    notifier = TelegramNotifier(settings)
    message = notifier._safe_error("failed with secret-key and telegram-token")
    assert "secret-key" not in message
    assert "telegram-token" not in message
    assert message.count("[redacted]") == 2


@pytest.mark.asyncio
async def test_send_alert_rate_limits_by_event_type_when_disabled():
    repository = MemoryDomainRepository()
    settings = Settings(telegram_enabled=False, telegram_bot_token="token", telegram_chat_id="123")
    notifier = TelegramNotifier(settings, repository)

    assert await notifier.send_alert("budget_limit_reached", "daily budget reached") is False
    assert await notifier.send_alert("budget_limit_reached", "weekly budget reached") is False

    assert len(repository.alerts) == 1
    assert repository.alerts[0][1] == "daily budget reached"


@pytest.mark.asyncio
async def test_telegram_failure_is_logged_without_raising(caplog):
    repository = MemoryDomainRepository()
    settings = Settings(telegram_enabled=True, telegram_bot_token="token", telegram_chat_id="123")
    notifier = TelegramNotifier(settings, repository)
    notifier._bot = FailingBot()

    with caplog.at_level("ERROR"):
        assert await notifier.send_alert("critical_exception", "boom") is False

    assert len(repository.alerts) == 1
    assert repository.alerts[0] == ("telegram", "boom", False)
    assert "Telegram notification failed" in caplog.text


@pytest.mark.asyncio
async def test_startup_health_check_success_sends_once(monkeypatch):
    monkeypatch.setattr("app.services.telegram_notifier.run_resilient", run_without_retry)
    repository = MemoryDomainRepository()
    settings = Settings(
        telegram_enabled=True,
        telegram_bot_token="telegram-token",
        telegram_chat_id="123",
        safe_mode=True,
        dry_run_purchases=True,
    )
    notifier = TelegramNotifier(settings, repository)
    bot = RecordingBot()
    notifier._bot = bot

    assert await notifier.send_startup_health_check() is True
    assert await notifier.send_startup_health_check() is False

    assert len(bot.messages) == 1
    message = bot.messages[0]["text"]
    assert "✅ Domain Hunter iniciado com sucesso." in message
    assert "SAFE_MODE=ATIVO" in message
    assert "DRY_RUN_PURCHASES=ATIVO" in message
    assert "Horario:" in message
    assert "telegram-token" not in message
    assert repository.alerts == [("telegram", message, True)]


@pytest.mark.asyncio
async def test_startup_health_check_failure_logs_and_continues(monkeypatch, caplog):
    monkeypatch.setattr("app.services.telegram_notifier.run_resilient", run_without_retry)
    repository = MemoryDomainRepository()
    settings = Settings(
        telegram_enabled=True,
        telegram_bot_token="telegram-token",
        telegram_chat_id="123",
        safe_mode=True,
        dry_run_purchases=True,
    )
    notifier = TelegramNotifier(settings, repository)
    bot = TokenLeakingFailingBot()
    notifier._bot = bot

    with caplog.at_level("ERROR"):
        assert await notifier.send_startup_health_check() is False
        assert await notifier.send_startup_health_check() is False

    assert bot.attempts == 1
    assert len(repository.alerts) == 1
    assert repository.alerts[0][2] is False
    assert "Telegram notification failed" in caplog.text
    assert "[redacted]" in caplog.text
    assert "telegram-token" not in caplog.text
