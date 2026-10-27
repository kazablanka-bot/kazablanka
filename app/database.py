"""تهيئة قاعدة البيانات غير المتزامنة وجلسات SQLAlchemy."""

from __future__ import annotations

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base


def create_engine(database_url: str) -> AsyncEngine:
    """ينشئ محرك PostgreSQL أو SQLite وفق الرابط المهيأ."""
    if database_url.startswith("sqlite+aiosqlite://"):
        engine = create_async_engine(database_url, connect_args={"check_same_thread": False})

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        return engine
    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("رابط قاعدة البيانات يجب أن يكون SQLite أو PostgreSQL.")
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def initialize_database(engine: AsyncEngine) -> None:
    """ينشئ الجداول ويضيف أعمدة الإعدادات الجديدة للتثبيتات السابقة."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if connection.dialect.name == "sqlite":
            sqlite_migrations = {
                "exchange_routes": {
                    "rate_value": "NUMERIC(24,8) NOT NULL DEFAULT 0",
                    "fee_percentage": "NUMERIC(8,4) NOT NULL DEFAULT 0",
                    "minimum_usd": "NUMERIC(24,8) NOT NULL DEFAULT 0",
                },
                "exchange_requests": {
                    "barcode_file_id_snapshot": "VARCHAR(256)",
                    "payment_reference": "VARCHAR(128)",
                    "payment_proof_file_id": "VARCHAR(256)",
                    "payment_proof_type": "VARCHAR(16)",
                    "payment_reported_at": "DATETIME",
                    "crypto_method": "VARCHAR(32)",
                    "payment_message_id": "BIGINT",
                    "review_message_id": "BIGINT",
                    "rate_snapshot": "NUMERIC(24,8) NOT NULL DEFAULT 1",
                    "fee_percentage_snapshot": "NUMERIC(8,4) NOT NULL DEFAULT 0",
                },
                # Events الموجودة قبل البرنامج الجديد تبقى تاريخية وغير مؤهلة للمستويات.
                "referral_events": {
                    "is_active": "BOOLEAN NOT NULL DEFAULT 1",
                    "revoked_at": "DATETIME",
                    "revocation_reason": "TEXT",
                    "eligible_for_levels": "BOOLEAN NOT NULL DEFAULT 0",
                    "reward_granted": "NUMERIC(24,8) NOT NULL DEFAULT 0",
                    "reward_reversed": "NUMERIC(24,8) NOT NULL DEFAULT 0",
                },
            }
            for table_name, columns in sqlite_migrations.items():
                existing = await connection.run_sync(
                    lambda sync_connection, table=table_name: {
                        column["name"]
                        for column in inspect(sync_connection).get_columns(table)
                    }
                )
                for column_name, definition in columns.items():
                    if column_name not in existing:
                        await connection.exec_driver_sql(
                            f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}'
                        )
        # PostgreSQL يحتاج ترقيات صريحة أيضاً لأن create_all لا يغيّر الجداول القائمة.
        if connection.dialect.name == "postgresql":
            await connection.exec_driver_sql("ALTER TABLE exchange_routes ADD COLUMN IF NOT EXISTS rate_value NUMERIC(24,8) NOT NULL DEFAULT 0")
            await connection.exec_driver_sql("ALTER TABLE exchange_routes ADD COLUMN IF NOT EXISTS fee_percentage NUMERIC(8,4) NOT NULL DEFAULT 0")
            await connection.exec_driver_sql("ALTER TABLE exchange_routes ADD COLUMN IF NOT EXISTS minimum_usd NUMERIC(24,8) NOT NULL DEFAULT 0")
            await connection.exec_driver_sql("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS rate_snapshot NUMERIC(24,8) NOT NULL DEFAULT 1")
            await connection.exec_driver_sql("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS fee_percentage_snapshot NUMERIC(8,4) NOT NULL DEFAULT 0")
            await connection.exec_driver_sql("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS barcode_file_id_snapshot VARCHAR(256)")
            await connection.exec_driver_sql("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(128)")
            await connection.exec_driver_sql("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS payment_reported_at TIMESTAMPTZ")
            await connection.exec_driver_sql("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS payment_message_id BIGINT")
            await connection.exec_driver_sql(
                "ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS crypto_method VARCHAR(32)"
            )
            await connection.exec_driver_sql(
                "ALTER TABLE exchange_requests "
                "ADD COLUMN IF NOT EXISTS payment_proof_file_id VARCHAR(256)"
            )
            await connection.exec_driver_sql(
                "ALTER TABLE exchange_requests "
                "ADD COLUMN IF NOT EXISTS payment_proof_type VARCHAR(16)"
            )
            await connection.exec_driver_sql(
                "ALTER TABLE exchange_requests "
                "ADD COLUMN IF NOT EXISTS review_message_id BIGINT"
            )
            await connection.exec_driver_sql("ALTER TABLE referral_events ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
            await connection.exec_driver_sql("ALTER TABLE referral_events ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ")
            await connection.exec_driver_sql("ALTER TABLE referral_events ADD COLUMN IF NOT EXISTS revocation_reason TEXT")
            await connection.exec_driver_sql("ALTER TABLE referral_events ADD COLUMN IF NOT EXISTS eligible_for_levels BOOLEAN NOT NULL DEFAULT FALSE")
            await connection.exec_driver_sql("ALTER TABLE referral_events ADD COLUMN IF NOT EXISTS reward_granted NUMERIC(24,8) NOT NULL DEFAULT 0")
            await connection.exec_driver_sql("ALTER TABLE referral_events ADD COLUMN IF NOT EXISTS reward_reversed NUMERIC(24,8) NOT NULL DEFAULT 0")

        await connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_exchange_requests_payment_reference "
            "ON exchange_requests (payment_reference) "
            "WHERE payment_reference IS NOT NULL"
        )
