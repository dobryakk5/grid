from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://grid:grid@127.0.0.1:5432/grid"

    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_base_url: str = "https://api-demo.bybit.com"

    grid_poll_seconds: float = 3.0
    grid_fee_buffer_pct: Decimal = Decimal("0.002")


settings = Settings()
