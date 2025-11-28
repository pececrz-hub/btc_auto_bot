from binance.client import Client
from binance.enums import *
from decimal import Decimal, ROUND_DOWN
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from typing import Optional
from binance.exceptions import BinanceAPIException


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
    # ---------- helpers ----------
    @staticmethod
    def _decimals_from_step(s: str) -> int:
        d = Decimal(str(s)).normalize()
        return -d.as_tuple().exponent if d.as_tuple().exponent < 0 else 0

    @staticmethod
    def _fmt_dec(x, places: int) -> str:
        q = Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
        return format(q, 'f')  # evita notação científica

    @staticmethod
    def _mk_cid(prefix: str) -> str:
        # máx 36 chars permitidos
        base = f"{prefix}_{int(time.time())}"
        safe = ''.join(ch if ch.isalnum() or ch in "_-" else "_" for ch in base)
        return safe[:36]

    @staticmethod
    def _post_quant_checks(qty: Decimal, price: Decimal, f: dict) -> bool:
        return not (qty < Decimal(str(f["min_qty"])) or (qty * price) < Decimal(str(f["min_notional"])))
        
    def validate_api(self) -> (bool, str):
        """Tenta um endpoint assinado p/ validar chave/permissões."""
        try:
            _ = self._safe_call(self.client.get_account)
            return True, ""
        except BinanceAPIException as e:
            code = getattr(e, "code", None)
            msg = str(e)
            if code == -2015 or "invalid api-key" in msg.lower():
                return False, (
                    "API inválida/IP/permissões.\n"
                    "1) Se use_testnet=True, use CHAVES do Testnet.\n"
                    "2) Em produção, habilite 'Enable Spot & Margin Trading'.\n"
                    "3) Se whitelistar IP, inclua o IP do container.\n"
                    "4) KEY/SECRET no .env sem aspas/espaços."
                )
            if code == -1021 or "timestamp" in msg.lower():
                return False, "Relógio do servidor fora de sincronia (-1021). Sincronize NTP."
            return False, f"Erro Binance: {code} - {msg}"
        except Exception as e:
            return False, f"Falha ao validar API: {e}"

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

 # ---- envio centralizado ----
    def order_limit_maker(self, symbol: str, side: str, quantity, price, client_order_id: Optional[str] = None):
        # converte para Decimal e aplica filtros de step/tick
        f = self.get_symbol_filters(symbol)
        step = f["step_size"]
        tick = f["tick_size"]

        qd = self.quantize_step(Decimal(str(quantity)), step)
        pd = self.quantize_tick(Decimal(str(price)), tick)

        qty_str = format(qd, "f")     # ex: '0.00008' (sem notação científica)
        price_str = format(pd, "f")   # ex: '87691.08000000'

        params = {
            "symbol": symbol,
            "side": side,
            "type": ORDER_TYPE_LIMIT_MAKER,
            "quantity": qty_str,
            "price": price_str,
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id

        return self._safe_call(self.client.create_order, **params)


    def order_market(self, symbol: str, side: str, quantity, client_order_id: Optional[str] = None):
        f = self.get_symbol_filters(symbol)
        step = f["step_size"]

        qd = self.quantize_step(Decimal(str(quantity)), step)
        qty_str = format(qd, "f")

        params = {
            "symbol": symbol,
            "side": side,
            "type": ORDER_TYPE_MARKET,
            "quantity": qty_str,
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
