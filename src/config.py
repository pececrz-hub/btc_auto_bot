\
from pydantic import BaseModel, Field, ValidationError
from typing import Tuple, List, Literal
import json, os
from dotenv import load_dotenv

class BanditCfg(BaseModel):
    min_pct_range: Tuple[float, float] = (0.03, 0.10)
    max_pct_range: Tuple[float, float] = (0.10, 0.14)
    num_configs: int = 5
    exploration_eps: float = 0.25
    switch_every_trades: int = 8
    switch_every_minutes: int = 30

class GridCfg(BaseModel):
    enabled: bool = True
    buy_levels: int = 1
    sell_levels: int = 1
    spacing_pct: float = 0.03
    rearm_threshold_pct: float = 0.015
    order_ttl_seconds: int = 1800

    min_pct_range: Tuple[float, float] = (0.03, 0.10)
    max_pct_range: Tuple[float, float] = (0.10, 0.14)
    num_configs: int = 5
    exploration_eps: float = 0.25
    switch_every_trades: int = 8

class ArbitrageCfg(BaseModel):
    enabled: bool = False
    secondary_exchange: str = "okx"
    min_edge_pct_net: float = 0.30
    paper: bool = True

class AppCfg(BaseModel):
    symbol: str = "BTCUSDT"
    quote_asset: str = "USDT"
    use_testnet: bool = True
    poll_interval_seconds: int = 10
    target_balance: float = 1_000_000.0
    cancel_open_orders_on_start: bool = True
    base_risk_frac: float = 0.25
    min_profit_pct_net: float = 0.10
    extra_fee_safety_bps: int = 10
    bandit: BanditCfg = BanditCfg()
    grid: GridCfg = GridCfg()
    arbitrage: ArbitrageCfg = ArbitrageCfg()
    mode: Literal["LIVE", "PAPER"] = "LIVE"
    resume_on_start: bool = True

def load_config(config_path: str = "config.json") -> AppCfg:
    load_dotenv(override=True)
    if not os.path.exists(config_path):
        raise FileNotFoundError("Crie config.json a partir de config.json.example")
    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    try:
        return AppCfg(**raw)
    except ValidationError as e:
        raise RuntimeError(f"Config inválida: {e}")

def load_secrets():
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET não definidos no .env")
    return api_key, api_secret
