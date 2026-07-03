from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    dart_api_key: str = ""

    app_host: str = "127.0.0.1"
    app_port: int = 8001


settings = Settings()
