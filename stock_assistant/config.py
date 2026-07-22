from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    initial_capital: float = 50_000.0
    commission_rate: float = 0.0005
    minimum_commission: float = 5.0
    max_trade_loss: float = 500.0
    max_stock_weight: float = 0.25
    max_industry_weight: float = 0.35
    minimum_average_turnover: float = 100_000_000.0
    drawdown_warning: float = 0.10
    drawdown_reduce: float = 0.15
    drawdown_protection: float = 0.20
    database_path: Path = BASE_DIR / "data" / "stock_assistant.db"


settings = Settings()
