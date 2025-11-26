\
from dataclasses import dataclass
from typing import Optional, Tuple
from decimal import Decimal

@dataclass
class StrategyConfig:
    symbol: str
    min_change_pct: float
    max_change_pct: float
    target_balance: float
    min_profit_pct_net: float
    fee_maker: float
    fee_taker: float
    extra_fee_safety_bps: int
    tick_size: Decimal

@dataclass
class Position:
    side: str
    entry_price: Optional[float] = None
    qty: float = 0.0

class PercentStrategy:
    """
    Regras:
      - Quando sem posição: coloca BUY LIMIT_MAKER em ref*(1 - min_change)
      - Quando comprado: coloca SELL LIMIT_MAKER no preço que garante lucro líquido >= min_profit_pct_net, arredondado por tick
      - Sempre atualiza ordens alvo se referência/estado mudar
    """
    def __init__(self, cfg: StrategyConfig, initial_balance: float):
        self.cfg = cfg
        self.position = Position(side="NONE")
        self.ref_price: Optional[float] = None
        self.balance = initial_balance
        self.current_buy_order: Optional[dict] = None
        self.current_sell_order: Optional[dict] = None

    def update_reference_price(self, price: float):
        if self.ref_price is None:
            self.ref_price = price

    def _net_profit_pct(self, pb: float, ps: float, maker_on_both=True) -> float:
        """
        Retorna % de lucro líquido considerando taxas.
        """
        fee_buy = self.cfg.fee_maker if maker_on_both else self.cfg.fee_taker
        fee_sell = self.cfg.fee_maker if maker_on_both else self.cfg.fee_taker
        extra = self.cfg.extra_fee_safety_bps / 10_000.0
        buy_cost = pb * (1 + fee_buy + extra)
        sell_revenue = ps * (1 - fee_sell - extra)
        return (sell_revenue - buy_cost) / buy_cost

    def target_sell_for_net(self, pb: float, net_target: float, maker_on_both=True) -> float:
        fee_buy = self.cfg.fee_maker if maker_on_both else self.cfg.fee_taker
        fee_sell = self.cfg.fee_maker if maker_on_both else self.cfg.fee_taker
        extra = self.cfg.extra_fee_safety_bps / 10_000.0
        # Queremos: (ps*(1-fee_sell-extra) - pb*(1+fee_buy+extra)) / (pb*(1+fee_buy+extra)) >= net_target
        denom = (1 - fee_sell - extra)
        base = pb * (1 + fee_buy + extra) * (1 + net_target)
        ps = base / denom
        # arredonda por tick
        from math import floor
        tick = self.cfg.tick_size
        # usar Decimal para manter precisão
        from decimal import Decimal
        dps = (Decimal(str(ps)) // tick) * tick
        return float(dps)

    def maybe_prices(self, price: float) -> Tuple[str, str, float, float]:
        """
        Decide ação e sugere preços alvo para ordens.
        Retorna: (state, reason, buy_price, sell_price)
        """
        self.update_reference_price(price)
        ref = self.ref_price
        buy_price = None
        sell_price = None

        if self.position.side == "NONE":
            change = (price - ref) / ref
            if change <= -self.cfg.min_change_pct:
                buy_price = ref * (1 - self.cfg.min_change_pct)
                return "WANT_BUY", f"queda {change:.2%} de ref {ref:.2f}", buy_price, None
            return "HOLD", "sem gatilho de compra", None, None

        if self.position.side == "LONG":
            # calcula preço-alvo para lucro líquido >= min_profit_pct_net
            sell_price = self.target_sell_for_net(self.position.entry_price, self.cfg.min_profit_pct_net, maker_on_both=True)
            change_from_entry = (price - self.position.entry_price) / self.position.entry_price
            if change_from_entry >= self.cfg.max_change_pct:
                return "WANT_SELL", f"alta {change_from_entry:.2%} desde entrada", None, sell_price
            # mesmo se não chegou a max_change_pct, podemos manter ordem de venda no alvo calculado
            return "HOLD_LONG", f"alvo venda p/ lucro líquido >= {self.cfg.min_profit_pct_net:.2%}", None, sell_price

        return "HOLD", "estado desconhecido", None, None

    def on_buy_executed(self, price: float, qty: float, fee: float):
        self.position = Position(side="LONG", entry_price=price, qty=qty)
        self.balance -= price * qty + fee
        self.ref_price = price

    def on_sell_executed(self, price: float, qty: float, fee: float):
        gross = price * qty
        cost = self.position.entry_price * self.position.qty
        pnl = gross - cost - fee
        self.balance += gross - fee
        self.position = Position(side="NONE", entry_price=None, qty=0.0)
        self.ref_price = price
        return pnl, self.balance
