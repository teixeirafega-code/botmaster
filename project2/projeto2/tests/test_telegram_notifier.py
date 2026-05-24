import pytest

from app.config.settings import Settings
from app.services.telegram_notifier import TelegramNotifier


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

