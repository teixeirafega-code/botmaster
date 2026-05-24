import os

import pytest

from app.db.postgres import PostgresDomainRepository


@pytest.mark.asyncio
async def test_postgres_healthcheck_when_database_url_is_available():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    repository = PostgresDomainRepository(database_url)
    await repository.connect()
    try:
        await repository.init_schema()
        assert await repository.healthcheck() is True
    finally:
        await repository.close()
