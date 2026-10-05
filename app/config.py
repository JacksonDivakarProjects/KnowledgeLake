from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    kl_mode: str = "webhook"
    kl_log_level: str = "INFO"
    kl_tz: str = "Asia/Kolkata"
    kl_auto_create_schema: bool = True

    # DB
    database_url: str


settings = Settings()