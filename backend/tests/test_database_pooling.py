"""Create real async engines to verify worker and web pooling contracts."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.pool import NullPool

from backend.shared.database_manager_v2 import DatabaseConfig, DatabaseManager


@pytest.mark.parametrize("unpooled", [False, True])
def test_master_and_slave_pool_configuration(monkeypatch, unpooled):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused@127.0.0.1/test")
    monkeypatch.setenv("DB_USER", "unused")
    monkeypatch.setenv("DB_PASSWORD", "")
    monkeypatch.setenv("DB_SLAVE_HOSTS", "127.0.0.2:5432")
    monkeypatch.setenv("DB_POOL_SIZE", "6")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "4")
    monkeypatch.setenv("DB_USE_NULL_POOL", str(unpooled).lower())
    manager = DatabaseManager(DatabaseConfig())
    monkeypatch.setattr(manager, "health_check", AsyncMock())

    async def check():
        try:
            # No queries: actual SQLAlchemy validates the pool's accepted options.
            await manager.initialize()
            engines = [manager._master_engine, *manager._slave_engines]
            assert len(engines) == 2
            assert all(isinstance(engine.pool, NullPool) == unpooled for engine in engines)
            status = await manager.get_pool_status()
            if unpooled:
                assert status["master"]["pooling"] is False
                assert status["slaves"][0]["checked_out"] is None
            else:
                assert status["master"]["size"] == 6
                assert status["slaves"][0]["size"] == 3
        finally:
            await manager.close()

    asyncio.run(check())


def test_web_pooling_remains_default(monkeypatch):
    monkeypatch.delenv("DB_USE_NULL_POOL", raising=False)
    assert DatabaseConfig().use_null_pool is False
