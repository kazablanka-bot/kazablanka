"""إعدادات التطبيق: تخزين مؤقت بالذاكرة أو PostgreSQL للنشر الدائم."""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """إعدادات آمنة مع تخزين SQLite دائم افتراضياً."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: SecretStr
    admin_ids: Annotated[list[int], NoDecode, Field(min_length=1)]
    storage_mode: Literal["memory", "sqlite", "postgresql"] = "sqlite"
    database_url: str | None = None

    default_syp_per_usd: Decimal = Field(gt=0)
    default_usdt_per_usd: Decimal = Field(gt=0)
    minimum_usd: Decimal = Field(gt=0)
    default_usd_to_usdt_fee: Decimal = Field(ge=0, lt=100)
    default_usdt_to_usd_fee: Decimal = Field(ge=0, lt=100)
    global_fee_percentage: Decimal = Field(
    default=Decimal("1"),
    ge=0,
    lt=100,
)
    quote_ttl_minutes: int = Field(default=15, ge=1, le=60)
    review_ttl_minutes: int = Field(default=5, ge=1, le=60)
    referral_signup_bonus: Decimal = Field(default=Decimal("1"), ge=0)
    # إعدادات البرنامج الجديد مستقلة وآمنة: لا تُدفع مكافآت الإحالات قبل ضبطها.
    referral_level_1_reward: Decimal = Field(default=Decimal("0"), ge=0)
    referral_level_2_reward: Decimal = Field(default=Decimal("0"), ge=0)
    referral_level_3_reward: Decimal = Field(default=Decimal("0"), ge=0)
    referral_level_4_reward: Decimal = Field(default=Decimal("0"), ge=0)
    withdrawal_minimum_usd: Decimal = Field(default=Decimal("50"), gt=0)
    withdrawal_review_hours: int = Field(default=24, ge=24, le=48)
    completion_eta: str = "من 5 إلى 10 دقائق"
    contact_url: str | None = None
    required_channel: str | None = None
    required_channel_url: str | None = None
    service_margin_notice: str = ""
    service_margin_notice2: str = ""
    log_level: str = "INFO"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> list[int]:
        """يدعم ADMIN_IDS=123 وADMIN_IDS=[123] وADMIN_IDS=123,456."""
        if isinstance(value, int):
            parsed = [value]
        elif isinstance(value, list):
            parsed = [int(item) for item in value]
        elif isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("ADMIN_IDS يجب أن تحتوي على معرف أدمن واحد على الأقل")
            if normalized.startswith("["):
                decoded = json.loads(normalized)
                if not isinstance(decoded, list):
                    raise ValueError("ADMIN_IDS يجب أن تكون قائمة أرقام")
                parsed = [int(item) for item in decoded]
            else:
                parsed = [int(item.strip()) for item in normalized.split(",") if item.strip()]
        else:
            raise ValueError("ADMIN_IDS يجب أن تحتوي على معرف أدمن واحد على الأقل")

        if not parsed or any(item <= 0 for item in parsed):
            raise ValueError("ADMIN_IDS يجب أن تحتوي على معرفات تيليغرام موجبة")
        return parsed

    @field_validator("global_fee_percentage")
    @classmethod
    def validate_global_fee_precision(cls, value: Decimal) -> Decimal:
        if value != value.quantize(Decimal("0.0001")):
            raise ValueError("GLOBAL_FEE_PERCENTAGE supports at most four decimal places")
        return value

    @field_validator("contact_url")
    @classmethod
    def validate_contact_url(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("CONTACT_URL يجب أن يبدأ بـ https:// أو http://")
        return value or None

    @field_validator("required_channel", "required_channel_url")
    @classmethod
    def normalize_channel_setting(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        return value or None

    @field_validator("database_url")
    @classmethod
    def strip_database_url(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @model_validator(mode="after")
    def validate_storage_configuration(self) -> "Settings":
        if self.storage_mode == "memory":
            return self
        # التوافق مع ملفات البيئة القديمة التي كانت تمرر رابط PostgreSQL فقط.
        if self.storage_mode == "sqlite" and self.database_url and self.database_url.startswith(
            ("postgres://", "postgresql://", "postgresql+asyncpg://")
        ):
            self.storage_mode = "postgresql"
        if self.storage_mode == "sqlite":
            if not self.database_url:
                self.database_url = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'databases.db'}"
            if not self.database_url.startswith(("sqlite+aiosqlite://", "sqlite:///")):
                raise ValueError("DATABASE_URL يجب أن تكون رابط SQLite صالحاً")
            if self.database_url.startswith("sqlite:///") and not self.database_url.startswith("sqlite+aiosqlite:///"):
                self.database_url = "sqlite+aiosqlite:///" + self.database_url.removeprefix("sqlite:///")
            return self
        if not self.database_url:
            raise ValueError("DATABASE_URL مطلوبة عند اختيار STORAGE_MODE=postgresql")
        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql+asyncpg://" + self.database_url.removeprefix("postgres://")
        elif self.database_url.startswith("postgresql://"):
            self.database_url = "postgresql+asyncpg://" + self.database_url.removeprefix("postgresql://")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL يجب أن تكون رابط PostgreSQL صالحاً")
        return self

    @field_validator(
        "default_syp_per_usd", "default_usdt_per_usd", "minimum_usd",
        "referral_level_1_reward", "referral_level_2_reward", "referral_level_3_reward",
        "referral_level_4_reward", "referral_signup_bonus", "withdrawal_minimum_usd",
    )
    @classmethod
    def positive_decimal(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("يجب ألا تكون القيمة سالبة")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()