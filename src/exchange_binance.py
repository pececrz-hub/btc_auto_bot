from binance.client import Client
from binance.enums import *
from decimal import Decimal
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from typing import Optional

class TransientError(Exception):
    pass

class BinanceWrapper:
    def __init__(self, api_key: str, api_secret: str, use_testnet: bool = True):
        self.client = Client(api_key, api_secret)
        if use_testnet:
            self.client.API_URL = "https://testnet.binance.vision/api"
        self._filters_cache = {}
        self._fees_cache = {}

    # ---------- safe call with retry ----------
    @retry(reraise=True, stop=stop_after_attempt(5), wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
           retry=retry_if_exception_type(TransientError))
    def _safe_call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e).lower()
            if any(s in msg for s in ["timeout", "too many requests", "connection", "temporarily"]):
                raise TransientError(e)
            raise

    # ---------- market data ----------
    def get_price(self, symbol: str) -> float:
        ticker = self._safe_call(self.client.get_symbol_ticker, symbol=symbol)
        return float(ticker["price"])

    def get_symbol_info_raw(self, symbol: str):
        info = self._safe_call(self.client.get_symbol_info, symbol)
        if not info:
            raise RuntimeError(f"Símbolo {symbol} não encontrado na Binance.")
        return info

    def get_symbol_assets(self, symbol: str):
        info = self.get_symbol_info_raw(symbol)
        return info.get("baseAsset"), info.get("quoteAsset")

    def get_symbol_filters(self, symbol: str):
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        info = self.get_symbol_info_raw(symbol)
        filters = {f["filterType"]: f for f in info["filters"]}
        lot = filters.get("LOT_SIZE", {})
        notional = filters.get("MIN_NOTIONAL", {})
        price_filter = filters.get("PRICE_FILTER", {})
        res = {
            "min_qty": Decimal(lot.get("minQty", "0.00000001")),
            "step_size": Decimal(lot.get("stepSize", "0.00000001")),
            "min_notional": Decimal(notional.get("minNotional", "5")),
            "tick_size": Decimal(price_filter.get("tickSize", "0.01"))
        }
        self._filters_cache[symbol] = res
        return res

    def get_trade_fees(self, symbol: str):
        if symbol in self._fees_cache:
            return self._fees_cache[symbol]
        try:
            fees = self._safe_call(self.client.get_trade_fee, symbol=symbol)
            row = fees[0] if isinstance(fees, list) and fees else fees
            maker = float(row.get("makerCommission", 0.001))
            taker = float(row.get("takerCommission", 0.001))
        except Exception:
            maker = 0.001
            taker = 0.001
        self._fees_cache[symbol] = {"maker": maker, "taker": taker}
        return self._fees_cache[symbol]

    # ---------- balances ----------
    def get_asset_balance(self, asset: str) -> float:
        info = self._safe_call(self.client.get_asset_balance, asset=asset)
        if info is None:
            return 0.0
        return float(info["free"])

    def cancel_all_open_orders(self, symbol: str):
        try:
            self._safe_call(self.client.cancel_open_orders, symbol=symbol)
        except Exception:
            pass

    # ---------- order ops ----------
    def quantize_step(self, value: Decimal, step: Decimal) -> Decimal:
        return (value // step) * step

    def quantize_tick(self, price: Decimal, tick: Decimal) -> Decimal:
        return (price // tick) * tick

    def order_limit_maker(self, symbol: str, side: str, quantity: float, price: float, client_order_id: Optional[str] = None):
        params = {
            "symbol": symbol,
            "side": side,
            "type": ORDER_TYPE_LIMIT_MAKER,
            "quantity": quantity,
            "price": f"{price:.8f}"
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return self._safe_call(self.client.create_order, **params)

    def order_market(self, symbol: str, side: str, quantity: float, client_order_id: Optional[str] = None):
        params = {
            "symbol": symbol,
            "side": side,
            "type": ORDER_TYPE_MARKET,
            "quantity": quantity
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return self._safe_call(self.client.create_order, **params)

    def get_order(self, symbol: str, order_id: int = None, client_order_id: str = None):
        if client_order_id:
            return self._safe_call(self.client.get_order, symbol=symbol, origClientOrderId=client_order_id)
        return self._safe_call(self.client.get_order, symbol=symbol, orderId=order_id)

    def get_open_orders(self, symbol: str):
        return self._safe_call(self.client.get_open_orders, symbol=symbol)

    def get_order_status_by_client(self, symbol: str, client_order_id: str):
        try:
            od = self._safe_call(self.client.get_order, symbol=symbol, origClientOrderId=client_order_id)
            return od.get("status"), od
        except Exception:
            return None, None

    def cancel_order_by_client(self, symbol: str, client_order_id: str):
        try:
            return self._safe_call(self.client.cancel_order, symbol=symbol, origClientOrderId=client_order_id)
        except Exception:
            return None
