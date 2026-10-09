from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    kl_mode: str = "polling"
    kl_log_level: str = "INFO"
    kl_tz: str = "Asia/Kolkata"
    kl_auto_create_schema: bool = True

    # DB
    database_url: str

    # Telegram
    telegram_bot_token: str
    webhook_secret: str = ""
    webhook_url: str = ""

    # LLM — Groq primary
    groq_api_key: str = ""
    kl_llm_model: str = "openai/gpt-oss-120b"
    kl_llm_fast_model: str = "openai/gpt-oss-20b"

    # LLM — NVIDIA fallback
    nvidia_api_key: str = ""
    nvidia_model: str = "google/gemma-4-31b-it"

    # LLM — general
    kl_llm_provider_order: str = "groq,nvidia"
    kl_llm_timeout_s: float = 30.0
    kl_classify_confidence_threshold: float = 0.6

    # Retrieval
    kl_retrieve_limit: int = 5

    @field_validator("kl_mode", "kl_log_level", "kl_tz", "webhook_url", "kl_llm_provider_order", mode="before")
    @classmethod
    def _strip_inline_comment(cls, v):
        # Some env_file parsers keep "value   # comment" verbatim.
        if isinstance(v, str):
            v = v.strip()
            v = "" if v.startswith("#") else v.split(" #", 1)[0].split("\t#", 1)[0].strip()
        return v

    @field_validator("kl_mode", "kl_log_level", mode="after")
    @classmethod
    def _case(cls, v: str) -> str:
        return v.lower() if v.lower() in ("polling", "webhook") else v.upper()

    @property
    def provider_order(self) -> list[str]:
        return [p.strip() for p in self.kl_llm_provider_order.split(",") if p.strip()]


settings = Settings()
