from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://grid:grid@db:5432/grid"

    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_base_url: str = "https://api-demo.bybit.com"

    grid_symbol: str = "BTCUSDT"
    grid_levels: int = 3
    grid_step_pct: Decimal = Decimal("0.005")
    grid_quote_per_level: Decimal = Decimal("25")
    grid_poll_seconds: float = 3.0
    grid_fee_buffer_pct: Decimal = Decimal("0.002")


settings = Settings()
