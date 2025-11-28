from binance.client import Client
from binance.exceptions import BinanceAPIException
from decimal import Decimal, ROUND_DOWN
import secrets
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from typing import Optional, Tuple, Dict, Any
import time

class TransientError(Exception):
    pass

class BinanceWrapper:
    def __init__(self, api_key: str, api_secret: str, use_testnet: bool = True):
        self.client = Client(api_key, api_secret)
        if use_testnet:
            # Testnet oficial (Spot)
            self.client.API_URL = "https://testnet.binance.vision/api"
        self._filters_cache: Dict[str, Dict[str, Decimal]] = {}
        self._fees_cache: Dict[str, Dict[str, float]] = {}

    # ---------- helpers ----------
    @staticmethod
    def _decimals_from_step(s: Decimal) -> int:
        d = Decimal(str(s)).normalize()
        exp = d.as_tuple().exponent
        return -exp if exp < 0 else 0

    @staticmethod
    def _fmt_dec(x: Decimal, places: int) -> str:
        q = Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
        return format(q, "f")  # evita notação científica

    @staticmethod
    def _mk_cid(prefix: str) -> str:
        base = f"{prefix}_{time.time_ns()}_{secrets.token_hex(3)}"  # ex.: SELL_LM_..._a1b2c3
        safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in base)
        return safe[:36]

    @staticmethod
    def _post_quant_checks(qty: Decimal, price: Decimal, f: Dict[str, Decimal]) -> bool:
        # qty >= min_qty e qty*price >= min_notional (se price > 0)
        if qty < Decimal(str(f["min_qty"])):
            return False
        if price and (qty * price) < Decimal(str(f["min_notional"])):
            return False
        return True

    # ---------- safe call com retry ----------
    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(TransientError),
    )
    def _safe_call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e).lower()
            if any(s in msg for s in ("timeout", "too many requests", "connection", "temporarily")):
                raise TransientError(e)
            raise

    # ---------- diagnóstico ----------
    def validate_api(self) -> Tuple[bool, str]:
        try:
            _ = self._safe_call(self.client.get_account)
            return True, ""
        except BinanceAPIException as e:
            code = getattr(e, "code", None)
            msg = str(e)
            if code == -2015 or "invalid api-key" in msg.lower():
                return False, "API inválida/IP/permissões."
            if code == -1021 or "timestamp" in msg.lower():
                return False, "Clock fora de sincronia (-1021)."
            return False, f"Erro Binance: {code} - {msg}"
        except Exception as e:
            return False, f"Falha ao validar API: {e}"

    # ---------- market data ----------
    def get_price(self, symbol: str) -> float:
        ticker = self._safe_call(self.client.get_symbol_ticker, symbol=symbol)
        return float(ticker["price"])

    def get_symbol_info_raw(self, symbol: str) -> Dict[str, Any]:
        info = self._safe_call(self.client.get_symbol_info, symbol=symbol)
        if not info:
            raise RuntimeError(f"Símbolo {symbol} não encontrado.")
        return info

    def get_symbol_assets(self, symbol: str):
        info = self.get_symbol_info_raw(symbol)
        return info.get("baseAsset"), info.get("quoteAsset")

    def get_symbol_filters(self, symbol: str) -> Dict[str, Decimal]:
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        info = self.get_symbol_info_raw(symbol)
        filters = {f["filterType"]: f for f in info["filters"]}
        lot = filters.get("LOT_SIZE", {})
        notional = filters.get("MIN_NOTIONAL", {})
        price_filter = filters.get("PRICE_FILTER", {})
        res = {
            "min_qty": Decimal(str(lot.get("minQty", "0.00000001"))),
            "step_size": Decimal(str(lot.get("stepSize", "0.00000001"))),
            "min_notional": Decimal(str(notional.get("minNotional", "5"))),
            "tick_size": Decimal(str(price_filter.get("tickSize", "0.01"))),
        }
        self._filters_cache[symbol] = res
        return res

    def get_trade_fees(self, symbol: str) -> Dict[str, float]:
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
        if not info:
            return 0.0
        return float(info["free"])

    # ---------- order ops ----------
    def cancel_all_open_orders(self, symbol: str):
        try:
            self._safe_call(self.client.cancel_open_orders, symbol=symbol)
        except Exception:
            pass

    def quantize_step(self, value: Decimal, step: Decimal) -> Decimal:
        return (value // step) * step

    def quantize_tick(self, price: Decimal, tick: Decimal) -> Decimal:
        return (price // tick) * tick

    def order_limit_maker(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        client_order_id: Optional[str] = None,
    ):
        f = self.get_symbol_filters(symbol)
        q_places = self._decimals_from_step(f["step_size"])
        p_places = self._decimals_from_step(f["tick_size"])

        qd = Decimal(str(quantity)).quantize(Decimal(1).scaleb(-q_places), rounding=ROUND_DOWN)
        pd = Decimal(str(price)).quantize(Decimal(1).scaleb(-p_places), rounding=ROUND_DOWN)

        if not self._post_quant_checks(qd, pd, f):
            raise ValueError("Fails filters after quantize (minQty/minNotional)")

        qty_str = self._fmt_dec(qd, q_places)
        price_str = self._fmt_dec(pd, p_places)

        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT_MAKER",
           # "timeInForce": "GTC",
            "quantity": qty_str,
            "price": price_str,
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id[:36]

        try:
            return self._safe_call(self.client.create_order, **params)
        except BinanceAPIException as e:
            # -2010: ordem viraria taker → apenas rearmar no loop
            if getattr(e, "code", None) == -2010:
                print("[Maker] Rejeitado: viraria taker. Rearma no próximo ciclo.")
                return {}
            raise

    def order_market(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: Optional[str] = None,
    ):
        f = self.get_symbol_filters(symbol)
        q_places = self._decimals_from_step(f["step_size"])
        qd = Decimal(str(quantity)).quantize(Decimal(1).scaleb(-q_places), rounding=ROUND_DOWN)
        if qd < f["min_qty"]:
            raise ValueError("Market qty abaixo do minQty")

        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": self._fmt_dec(qd, q_places),
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id[:36]

        return self._safe_call(self.client.create_order, **params)

    def get_open_orders(self, symbol: str):
        return self._safe_call(self.client.get_open_orders, symbol=symbol)

    def get_order(self, symbol: str, order_id: int = None, client_order_id: str = None):
        if client_order_id:
            return self._safe_call(self.client.get_order, symbol=symbol, origClientOrderId=client_order_id)
        return self._safe_call(self.client.get_order, symbol=symbol, orderId=order_id)

    def get_order_status_by_client(self, symbol: str, client_order_id: str):
        try:
            od = self._safe_call(self.client.get_order, symbol=symbol, origClientOrderId=client_order_id)
            return od.get("status"), od
        except Exception:
            return None, None
