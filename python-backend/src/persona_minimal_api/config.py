from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL")
    static_bearer_token: SecretStr = Field(validation_alias="PERSONA_STATIC_BEARER_TOKEN")
    static_user_id: str = Field(validation_alias="PERSONA_STATIC_USER_ID")
    static_display_name: str = Field(validation_alias="PERSONA_STATIC_DISPLAY_NAME")
    cursor_signing_key: SecretStr = Field(validation_alias="PERSONA_CURSOR_SIGNING_KEY")
    database_timeout_seconds: float = Field(
        default=2.0, validation_alias="PERSONA_DB_TIMEOUT_SECONDS"
    )

    @field_validator("static_user_id", "static_display_name")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("cursor_signing_key")
    @classmethod
    def cursor_key_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("must be at least 16 characters")
        return value

    @field_validator("database_timeout_seconds")
    @classmethod
    def database_timeout_is_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value
