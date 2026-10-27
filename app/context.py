"""سياق التطبيق الذي يختار بين التخزين المؤقت وPostgreSQL."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.memory_store import MemoryService
from app.services import ExchangeService


class NoopSession:
    """بديل جلسة للتوافق مع إغلاق جلسات المعالجات في وضع الذاكرة."""

    async def close(self) -> None:
        return None


@dataclass(frozen=True)
class ApplicationContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession] | None = None
    memory_service: MemoryService | None = None
    _monitoring_cache: dict[str, bool | float] = field(default_factory=dict, compare=False, repr=False)
    _monitoring_cache_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        compare=False,
        repr=False,
    )

    async def monitoring_enabled_cached(self, ttl_seconds: float = 5.0) -> bool:
        now = time.monotonic()
        expires_at = self._monitoring_cache.get("expires_at", 0.0)
        cached_value = self._monitoring_cache.get("enabled")
        if isinstance(expires_at, float) and now < expires_at and isinstance(cached_value, bool):
            return cached_value

        async with self._monitoring_cache_lock:
            now = time.monotonic()
            expires_at = self._monitoring_cache.get("expires_at", 0.0)
            cached_value = self._monitoring_cache.get("enabled")
            if isinstance(expires_at, float) and now < expires_at and isinstance(cached_value, bool):
                return cached_value

            session = self.open_session()
            try:
                enabled = await self.service(session).monitoring_enabled()
            finally:
                await session.close()

            self._monitoring_cache.update(
                enabled=enabled,
                expires_at=time.monotonic() + max(0.0, ttl_seconds),
            )
            return enabled

    def cache_monitoring_enabled(self, enabled: bool) -> None:
        self._monitoring_cache.update(
            enabled=enabled,
            expires_at=time.monotonic() + 5.0,
        )

    def service(self, session: AsyncSession | NoopSession) -> ExchangeService | MemoryService:
        if self.memory_service is not None:
            return self.memory_service
        if self.session_factory is None:
            raise RuntimeError("لم تهيأ خدمة التخزين.")
        if not isinstance(session, AsyncSession):
            raise RuntimeError("جلسة PostgreSQL غير صالحة.")
        return ExchangeService(session=session, settings=self.settings)

    def open_session(self) -> AsyncSession | NoopSession:
        if self.memory_service is not None:
            return NoopSession()
        if self.session_factory is None:
            raise RuntimeError("لم تهيأ جلسات PostgreSQL.")
        return self.session_factory()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids
