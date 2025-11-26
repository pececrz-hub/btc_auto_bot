import time
import math
from decimal import Decimal
from datetime import datetime

from src.config import load_config, load_secrets
from src.db import get_last_balance, insert_trade, get_stats
from src.ai_bandit import ParamManager
from src.exchange_binance import BinanceWrapper
from src.strategy_percent import PercentStrategy, StrategyConfig

BOT_TAG = "BTCBOT10PCT"

def now_ts() -> int:
    return int(time.time())

def trades_needed_compound(current: float, target: float, net_rate: float) -> int:
    """Retorna o número de trades com ganho líquido fixo (composto) até atingir target.
       -1 se não der para calcular (saldo atual <= 0 ou rate <= 0)."""
    if current <= 0 or net_rate <= 0:
        return -1
    if current >= target:
        return 0
    return math.ceil(math.log(target / current, 1 + net_rate))

def main():
    cfg = load_config()
    api_key, api_secret = load_secrets()

    ex = BinanceWrapper(api_key, api_secret, use_testnet=cfg.use_testnet)
    filters = ex.get_symbol_filters(cfg.symbol)
    fees = ex.get_trade_fees(cfg.symbol)  # {'maker': x, 'taker': y}

    if cfg.cancel_open_orders_on_start:
        try:
            ex.cancel_all_open_orders(cfg.symbol)
        except Exception:
            pass

    # Saldos e portfólio
    base_asset, quote_asset = ex.get_symbol_assets(cfg.symbol)
    spot_price = ex.get_price(cfg.symbol)
    free_quote = ex.get_asset_balance(quote_asset)
    free_base = ex.get_asset_balance(base_asset)
    portfolio_value = free_quote + free_base * spot_price

    last_balance = get_last_balance()
    initial_balance = portfolio_value if last_balance is None else last_balance

    print(f"Saldo livre real: {free_quote:.4f} {quote_asset}, {free_base:.6f} {base_asset}")
    print(f"Preço spot: {spot_price:.2f} {quote_asset}")
    print(f"Portfólio estimado: {portfolio_value:.2f} {quote_asset}")
    print(f"Taxas: maker={fees['maker']:.4%} | taker={fees['taker']:.4%}")
    print(f"MinNotional={float(filters['min_notional'])}, StepSize={filters['step_size']}, Tick={filters['tick_size']}")

    n10 = trades_needed_compound(portfolio_value, cfg.target_balance, cfg.min_profit_pct_net)
    if n10 >= 0:
        print(f"Trades a 10% até 1M: {n10}")
    else:
        print("Impossível estimar trades a 10% com saldo atual.")
    notional_test = portfolio_value * cfg.base_risk_frac
    if notional_test < float(filters['min_notional']):
        print(f"AVISO: {cfg.base_risk_frac:.0%} do portfólio ({notional_test:.2f}) < minNotional "
              f"({float(filters['min_notional']):.2f}). O bot aguardará até superar o mínimo.")

    # IA de parâmetros (bandit)
    pm = ParamManager(
        base_risk_frac=cfg.base_risk_frac,
        min_pct_range=tuple(cfg.bandit.min_pct_range),
        max_pct_range=tuple(cfg.bandit.max_pct_range),
        num_configs=cfg.bandit.num_configs,
        exploration_eps=cfg.bandit.exploration_eps,
    )
    active_cfg = pm.choose_active_config()
    print("Config ativa:", active_cfg)

    s_cfg = StrategyConfig(
        symbol=cfg.symbol,
        min_change_pct=active_cfg["min_change_pct"],
        max_change_pct=active_cfg["max_change_pct"],
        target_balance=cfg.target_balance,
        min_profit_pct_net=cfg.min_profit_pct_net,
        fee_maker=fees["maker"],
        fee_taker=fees["taker"],
        extra_fee_safety_bps=cfg.extra_fee_safety_bps,
        tick_size=filters["tick_size"],
    )
    strat = PercentStrategy(s_cfg, initial_balance)
    config_id = active_cfg["id"]
    # --- Resume de posição no start ---
    try:
        if getattr(cfg, "resume_on_start", False):
            from src.db import get_open_position_from_trades
            resume = get_open_position_from_trades()
            if resume:
                # Garante que tem BTC suficiente para o qty gravado
                base_asset, quote_asset = ex.get_symbol_assets(cfg.symbol)
                free_base_now = ex.get_asset_balance(base_asset)
                qty_resume = min(resume["qty"], float(free_base_now))
                qty_resume = float(ex.quantize_step(Decimal(str(qty_resume)), filters["step_size"]))
                if qty_resume >= float(filters["min_qty"]):
                    # Marca posição “LONG” internamente
                    strat.on_buy_executed(resume["entry_price"], qty_resume, fee=0.0)
                    # Arma SELL no alvo líquido mínimo
                    sell_target = strat.target_sell_for_net(resume["entry_price"], strat.cfg.min_profit_pct_net, maker_on_both=True)
                    qprice = ex.quantize_tick(Decimal(str(sell_target)), filters["tick_size"])
                    if cfg.mode == "LIVE":
                        client_id = f"{BOT_TAG}_RESUME_SELL_{now_ts()}"
                        ex.order_limit_maker(cfg.symbol, "SELL", qty_resume, float(qprice), client_order_id=client_id)
                        print(f"[RESUME] SELL LIMIT_MAKER armado: qty={qty_resume} price={qprice}")
                    else:
                        print(f"[RESUME][PAPER] posição restaurada; alvo de venda em {qprice}")
                else:
                    print("[RESUME] Sem BTC suficiente para retomar posição; ignorando.")
    except Exception as e:
        print("[RESUME] Falha ao retomar posição:", e)
    # ---------------------------------
    trades_since_switch = 0
    last_switch_ts = now_ts()
    rearm_th = cfg.grid.rearm_threshold_pct
    ttl = cfg.grid.order_ttl_seconds

    # Controle de ordens abertas (por client id)
    open_buy_id = None
    open_sell_id = None
    open_buy_cid = None
    open_sell_cid = None
    open_buy_price = None
    open_sell_price = None
    open_buy_ts = None
    open_sell_ts = None

    print("Bot rodando. Modo:", cfg.mode)

    while True:
        try:
            price = ex.get_price(cfg.symbol)
            state, reason, buy_price, sell_price = strat.maybe_prices(price)
            print(f"[{datetime.utcnow().isoformat()}] {state} | {reason} | price={price:.2f}")

            # ========================
            # BUY logic (maker) + rearm
            # ========================
            if state == "WANT_BUY" and strat.position.side == "NONE":
                free_quote = ex.get_asset_balance(cfg.quote_asset)
                notional = Decimal(str(free_quote)) * Decimal(str(active_cfg["trade_qty_frac"]))
                if notional >= filters["min_notional"]:
                    qty = (Decimal(str(notional)) / Decimal(str(buy_price)))
                    qty = ex.quantize_step(qty, filters["step_size"])
                    if qty >= filters["min_qty"]:
                        qprice = ex.quantize_tick(Decimal(str(buy_price)), filters["tick_size"])
                        if cfg.mode == "LIVE":
                            need_place = True
                            if open_buy_cid:
                                st, od = ex.get_order_status_by_client(cfg.symbol, open_buy_cid)
                                if st == "FILLED":
                                    ex_qty = float(od.get("executedQty", 0) or 0)
                                    cq = float(od.get("cummulativeQuoteQty", 0) or 0)
                                    exec_px = cq / ex_qty if ex_qty > 0 else float(qprice)
                                    fee = exec_px * ex_qty * fees["maker"]
                                    strat.on_buy_executed(exec_px, ex_qty, fee)
                                    insert_trade("BUY", exec_px, ex_qty, fee, 0.0, strat.balance, config_id,
                                                 order_id=str(od.get("orderId")), client_order_id=open_buy_cid)
                                    print(f"BUY FILLED {ex_qty} @ {exec_px}")
                                    open_buy_cid = open_buy_price = open_buy_ts = open_buy_id = None
                                    need_place = False
                                elif st in ("NEW", "PARTIALLY_FILLED"):
                                    # rearm por desvio de preço ou TTL
                                    if (open_buy_price and abs((float(qprice) - float(open_buy_price)) / float(open_buy_price)) >= rearm_th) \
                                       or (open_buy_ts and (now_ts() - open_buy_ts) > ttl):
                                        ex.cancel_order_by_client(cfg.symbol, open_buy_cid)
                                        open_buy_cid = open_buy_price = open_buy_ts = open_buy_id = None
                                    else:
                                        need_place = False
                                else:
                                    open_buy_cid = open_buy_price = open_buy_ts = open_buy_id = None
                            if need_place:
                                client_id = f"{BOT_TAG}_BUY_{now_ts()}"
                                if cfg.mode == "LIVE":
                                    order = ex.order_limit_maker(cfg.symbol, "BUY", float(qty), float(qprice), client_order_id=client_id)
                                    open_buy_id = order.get("orderId")
                                    open_buy_cid = client_id
                                    open_buy_price = float(qprice)
                                    open_buy_ts = now_ts()
                                    print(f"BUY LIMIT_MAKER: qty={qty} price={qprice} id={open_buy_id}")
                        else:
                            exec_price = float(qprice)
                            fee = exec_price * float(qty) * fees["maker"]
                            strat.on_buy_executed(exec_price, float(qty), fee)
                            insert_trade("BUY", exec_price, float(qty), fee, 0.0, strat.balance, config_id,
                                         order_id="PAPER", client_order_id=f"{BOT_TAG}_BUY_PAPER")
                            print(f"[PAPER] BUY {qty} @ {exec_price}")

            # ==================================
            # SELL logic (maker target) + rearm
            # ==================================
            if strat.position.side == "LONG":
                sell_target = strat.target_sell_for_net(strat.position.entry_price, strat.cfg.min_profit_pct_net, maker_on_both=True)
                qprice = ex.quantize_tick(Decimal(str(sell_target)), filters["tick_size"])
                if cfg.mode == "LIVE":
                    need_place_s = True
                    if open_sell_cid:
                        st, od = ex.get_order_status_by_client(cfg.symbol, open_sell_cid)
                        if st == "FILLED":
                            ex_qty = float(od.get("executedQty", 0) or 0)
                            cq = float(od.get("cummulativeQuoteQty", 0) or 0)
                            exec_px = cq / ex_qty if ex_qty > 0 else float(qprice)
                            fee = exec_px * ex_qty * fees["maker"]
                            pnl, new_balance = strat.on_sell_executed(exec_px, ex_qty, fee)
                            insert_trade("SELL", exec_px, ex_qty, fee, pnl, new_balance, config_id,
                                         order_id=str(od.get("orderId")), client_order_id=open_sell_cid)
                            print(f"SELL FILLED {ex_qty} @ {exec_px} | PnL {pnl:.4f}")
                            trades_since_switch += 1
                            open_sell_cid = open_sell_price = open_sell_ts = open_sell_id = None
                        elif st in ("NEW", "PARTIALLY_FILLED"):
                            if (open_sell_price and abs((float(qprice) - float(open_sell_price)) / float(open_sell_price)) >= rearm_th) \
                               or (open_sell_ts and (now_ts() - open_sell_ts) > ttl):
                                ex.cancel_order_by_client(cfg.symbol, open_sell_cid)
                                open_sell_cid = open_sell_price = open_sell_ts = open_sell_id = None
                            else:
                                need_place_s = False
                        else:
                            open_sell_cid = open_sell_price = open_sell_ts = open_sell_id = None
                    if need_place_s and strat.position.qty > 0:
                        client_id = f"{BOT_TAG}_SELL_{now_ts()}"
                        order = ex.order_limit_maker(cfg.symbol, "SELL", strat.position.qty, float(qprice), client_order_id=client_id)
                        open_sell_id = order.get("orderId")
                        open_sell_cid = client_id
                        open_sell_price = float(qprice)
                        open_sell_ts = now_ts()
                        print(f"SELL LIMIT_MAKER: qty={strat.position.qty} price={qprice} id={open_sell_id}")
                else:
                    if float(price) >= float(qprice):
                        qty = strat.position.qty
                        exec_price = float(qprice)
                        fee = exec_price * qty * fees["maker"]
                        pnl, new_balance = strat.on_sell_executed(exec_price, qty, fee)
                        insert_trade("SELL", exec_price, qty, fee, pnl, new_balance, config_id,
                                     order_id="PAPER", client_order_id=f"{BOT_TAG}_SELL_PAPER")
                        print(f"[PAPER] SELL {qty} @ {exec_price} | PnL {pnl:.4f}")
                        trades_since_switch += 1

            # =============================
            # Troca de config (bandit)
            # =============================
            if trades_since_switch >= cfg.bandit.switch_every_trades or (now_ts() - last_switch_ts) >= cfg.bandit.switch_every_minutes * 60:
                active_cfg = pm.choose_active_config()
                print("Troca de config:", active_cfg)
                strat.cfg.min_change_pct = active_cfg["min_change_pct"]
                strat.cfg.max_change_pct = active_cfg["max_change_pct"]
                config_id = active_cfg["id"]
                trades_since_switch = 0
                last_switch_ts = now_ts()

            # =============================
            # Dashboard sintético
            # =============================
            total_pnl, avg_pnl, n = get_stats()
            print(f"Stats: total_pnl={total_pnl:.4f} avg_pnl={avg_pnl:.6f} trades={n}")

            time.sleep(cfg.poll_interval_seconds)

        except KeyboardInterrupt:
            print("Encerrado pelo usuário.")
            break
        except Exception as e:
            print("Erro no loop:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
