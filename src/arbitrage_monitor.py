\
"""
Monitor de arbitragem simples via CCXT (opcional).
Não move fundos entre exchanges; a ideia é detectar edges para execução com saldos pré-alocados.
Por padrão fica em modo "paper" e só loga oportunidades.
"""
import ccxt
from typing import Optional, Tuple
import os

def build_exchange(name: str, use_testnet: bool = False):
    name = name.lower()
    if name == "okx":
        ex = ccxt.okx({
            "apiKey": os.getenv("OKX_API_KEY"),
            "secret": os.getenv("OKX_API_SECRET"),
            "password": os.getenv("OKX_PASSWORD"),
            "enableRateLimit": True
        })
    elif name == "bybit":
        ex = ccxt.bybit({
            "apiKey": os.getenv("BYBIT_API_KEY"),
            "secret": os.getenv("BYBIT_API_SECRET"),
            "enableRateLimit": True
        })
    else:
        raise ValueError(f"Exchange não suportada: {name}")
    return ex

def best_bid_ask(ex, symbol_ccxt: str) -> Tuple[float, float]:
    orderbook = ex.fetch_order_book(symbol_ccxt)
    best_bid = orderbook['bids'][0][0] if orderbook['bids'] else None
    best_ask = orderbook['asks'][0][0] if orderbook['asks'] else None
    return best_bid, best_ask

def edge_pct(buy_price: float, sell_price: float, buy_fee: float, sell_fee: float, extra_bps: int = 10) -> float:
    extra = extra_bps / 10_000.0
    cost = buy_price * (1 + buy_fee + extra)
    revenue = sell_price * (1 - sell_fee - extra)
    return (revenue - cost) / cost
