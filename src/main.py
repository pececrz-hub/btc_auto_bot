import os
import math
import time
import traceback
from datetime import datetime
from decimal import Decimal

from src.config import load_config, load_secrets
from src.exchange_binance import BinanceWrapper
from src.db import (
    get_open_position_from_trades,   # já tinha no teu projeto
    insert_lot, set_lot_sell, close_lot, get_open_lots,  # novos p/ "lots"
)

BOT_TAG = "BTCGRID"

# =================== Utils ===================
def now_ts():
    return datetime.utcnow().strftime("%Y%m%d%H%M%S")

def mask_key(k: str) -> str:
    if not k: return "N/A"
    return f"{k[:3]}...{k[-3:]}"

def trades_to_target(current_val: float, target: float, net_gain_pct: float) -> int:
    if current_val <= 0: return 0
    g = 1.0 + max(net_gain_pct, 1e-6)
    if g <= 1.0: return 0
    try:
        return math.ceil(math.log(target / current_val, g))
    except Exception:
        return 0

def portfolio_estimate(price: float, free_quote: float, free_base: float) -> float:
    return float(free_quote + free_base * price)

def print_startup_diags(cfg, api_key: str, ex: BinanceWrapper):
    print("=== Startup diagnostics ===")
    print(f"[Diag] CONFIG_PATH={os.getenv('CONFIG_PATH','config.json')}  DB={os.getenv('TRADES_DB_PATH','/data/trades.db')}")
    print(f"[Diag] use_testnet={cfg.use_testnet}")
    endpoint = getattr(ex.client, "API_URL", "https://api.binance.com/api")
    print(f"[Diag] endpoint={endpoint}")
    # IP de saída (best effort)
    try:
        import urllib.request
        ipv4 = urllib.request.urlopen("https://api.ipify.org", timeout=2).read().decode()
    except Exception:
        ipv4 = "N/A"
    print(f"[Diag] IPv4 egress={ipv4}  IPv6 egress=N/A")
    print(f"[Diag] API key len={len(api_key) if api_key else 0}  mask={mask_key(api_key)}")

# ========== Alvo de venda por lote (lucro líquido cfg + 2x maker + safety) ==========
def calc_sell_target(ex: BinanceWrapper, cfg, buy_price: float) -> tuple[float, float]:
    fees = ex.get_trade_fees(cfg.symbol)
    maker_fee = max(fees.get("maker", 0.001), 0.0)  # fallback 0.1%
    safety = (getattr(cfg, "extra_fee_safety_bps", 0) or 0) / 10000.0
    # prioridade: take_profit_pct; fallback: min_profit_pct_net; senão 0.10
    tp = getattr(cfg, "take_profit_pct", None)
    if tp is None:
        tp = getattr(cfg, "min_profit_pct_net", 0.10)
    target = buy_price * (1 + tp + 2 * maker_fee + safety)
    return target, tp

# ========== Armar SELL LIMIT_MAKER para um lote ==========
def try_arm_sell_for_lot(ex: BinanceWrapper, cfg, filters: dict, lot_id: int, qty: float, target_price: float):
    qty_dec = ex.quantize_step(Decimal(str(qty)), filters["step_size"])
    prc_dec = ex.quantize_tick(Decimal(str(target_price)), filters["tick_size"])
    if qty_dec < filters["min_qty"] or qty_dec * prc_dec < filters["min_notional"]:
        return False, "minNotional/minQty"
    client_id = f"{BOT_TAG}_LOT{lot_id}_SELL_{now_ts()}"
    ex.order_limit_maker(cfg.symbol, "SELL", float(qty_dec), float(prc_dec), client_order_id=client_id)
    set_lot_sell(lot_id, client_id, float(prc_dec))
    print(f"[LOT#{lot_id}] SELL armado: qty={format(qty_dec,'f')} price={format(prc_dec,'f')}")
    return True, "OK"

# ========== Gerente de lotes (rearmar / fechar) ==========
def manage_lots(ex: BinanceWrapper, cfg):
    filters = ex.get_symbol_filters(cfg.symbol)
    for lot in get_open_lots():
        lot_id = lot["id"]
        qty = float(lot["qty_remaining"])
        tgt = float(lot["target_price"])
        cid = lot.get("sell_client_id")
        status = lot.get("status", "OPEN")

        qty_dec = ex.quantize_step(Decimal(str(qty)), filters["step_size"])
        prc_dec = ex.quantize_tick(Decimal(str(tgt)), filters["tick_size"])
        if qty_dec < filters["min_qty"] or qty_dec * prc_dec < filters["min_notional"]:
            continue  # ainda não dá pra vender esse pedaço

        if status == "OPEN" or not cid:
            try_arm_sell_for_lot(ex, cfg, filters, lot_id, float(qty_dec), float(prc_dec))
            continue

        st, od = ex.get_order_status_by_client(cfg.symbol, cid)
        if st == "FILLED":
            close_lot(lot_id)
            print(f"[LOT#{lot_id}] SELL filled a {od.get('price') if od else 'n/a'}")
        elif st in ("CANCELED", "REJECTED", None):
            try_arm_sell_for_lot(ex, cfg, filters, lot_id, float(qty_dec), float(prc_dec))

# =================== Core ===================
def main():
    cfg = load_config(os.getenv("CONFIG_PATH", "config.json"))
    api_key, api_secret = load_secrets()
    ex = BinanceWrapper(api_key, api_secret, use_testnet=cfg.use_testnet)

    print_startup_diags(cfg, api_key, ex)
    ok, why = ex.validate_api()
    if not ok:
        raise SystemExit(why)

    # Info do símbolo / filtros
    filters = ex.get_symbol_filters(cfg.symbol)
    fees = ex.get_trade_fees(cfg.symbol)
    maker_fee = fees["maker"]
    taker_fee = fees["taker"]

    # Saldos e preço
    base_asset, quote_asset = ex.get_symbol_assets(cfg.symbol)
    free_quote = ex.get_asset_balance(quote_asset)
    free_base = ex.get_asset_balance(base_asset)
    price = ex.get_price(cfg.symbol)
    port = portfolio_estimate(price, free_quote, free_base)

    print(f"Saldo livre real: {free_quote:.4f} {quote_asset}, {free_base:.8f} {base_asset}")
    print(f"Preço spot: {price:.2f} {quote_asset}")
    print(f"Portfólio estimado: {port:.2f} {quote_asset}")
    print(f"Taxas: maker={maker_fee*100:.4f}% | taker={taker_fee*100:.4f}%")
    print(f"MinNotional={filters['min_notional']}, StepSize={format(filters['step_size'],'f')}, Tick={format(filters['tick_size'],'f')}")

    # “sonho”: trades até 1M usando tp do config
    tp_cfg = getattr(cfg, "take_profit_pct", None)
    if tp_cfg is None:
        tp_cfg = getattr(cfg, "min_profit_pct_net", 0.10)
    trades_need = trades_to_target(port, cfg.target_balance, tp_cfg)
    print(f"Trades a {tp_cfg*100:.2f}% até 1M: {trades_need}")

    # RESUME: cria 1 lote da posição aberta registrada e arma SELL
    try:
        if getattr(cfg, "resume_on_start", True):
            resume = get_open_position_from_trades()
            if resume:
                free_base_now = ex.get_asset_balance(base_asset)
                qty_resume = min(resume["qty"], float(free_base_now))
                qty_resume = float(qty_resume)  # deixa acumular mesmo se < min_qty

                sell_target, eff_tp = calc_sell_target(ex, cfg, resume["entry_price"])
                qprice_dec = ex.quantize_tick(Decimal(str(sell_target)), filters["tick_size"])

                # acumula/mescla no lote aberto
                from src.db import upsert_accum_lot
                lot_id, new_bp, new_qty = upsert_accum_lot(resume["entry_price"], qty_resume, float(qprice_dec))
                print(f"[RESUME] lote #{lot_id} acumulado: qty={new_qty:.8f} avg_buy={new_bp:.2f}")

                # tenta armar SELL (se ainda não atingir minNotional/minQty, a função retorna False e segue acumulando)
                ok, msg = try_arm_sell_for_lot(ex, cfg, filters, lot_id, new_qty, float(qprice_dec))
                if ok:
                    print(f"HOLD_LONG | alvo venda líquido >= {eff_tp*100:.2f}% | target={float(qprice_dec):.2f} | price={price:.2f}")
                else:
                    print(f"[RESUME] aguardando mínimo p/ SELL (motivo: {msg})")
            else:
                print("[RESUME] nenhuma posição registrada no trades.db")
    except Exception as e:
        print("[RESUME] Falha ao retomar posição:", e)


    print(f"Bot rodando. Modo: {cfg.mode}")

    # ====== Estado para auto-ajuste / grid ======
    ref_price = price
    poll = max(int(cfg.poll_interval_seconds), 5)
    base_risk_frac = float(getattr(cfg, "base_risk_frac", 0.35))
    spacing_base = float(getattr(cfg, "grid", None).spacing_pct if getattr(cfg, "grid", None) else 0.006)
    rearm_thr = float(getattr(cfg, "grid", None).rearm_threshold_pct if getattr(cfg, "grid", None) else 0.003)
    # EWMA de volatilidade (retorno absoluto)
    vol_ewma = 0.0
    last_price = price

    while True:
        try:
            price = ex.get_price(cfg.symbol)

            # atualiza vol (EWMA)
            if last_price > 0:
                ret_abs = abs((price - last_price) / last_price)
                vol_ewma = 0.9 * vol_ewma + 0.1 * ret_abs
            last_price = price

            # gerenciar SELLs por lote
            manage_lots(ex, cfg)

            # ====== Auto-reprogramação simples ======
            # inventário atual
            free_quote = ex.get_asset_balance(quote_asset)
            free_base = ex.get_asset_balance(base_asset)
            base_value = free_base * price
            total_val = base_value + free_quote if (base_value + free_quote) > 0 else 1.0
            inv_ratio = base_value / total_val

            # spacing dinâmico: base max( spacing_base, 2x vol ) limitado
            spacing = max(spacing_base, min(0.02, vol_ewma * 2.0))   # 0.3%–2.0% prático
            # risco dinâmico: reduz se carregado demais
            risk_frac = base_risk_frac
            if inv_ratio > 0.70:
                risk_frac = 0.0          # muito carregado → pausa compras
            elif inv_ratio > 0.50:
                risk_frac *= 0.5         # reduz pela metade
            # rearme de referência se o preço recupera um pouco
            if price > ref_price * (1 + rearm_thr):
                ref_price = price

            # ====== Gatilho de compra por queda (GRID) ======
            drop = (ref_price - price) / ref_price if ref_price > 0 else 0.0
            want_buy = (drop >= spacing) and (risk_frac > 0.0)

            if want_buy:
                quote_to_use = free_quote * risk_frac
                if quote_to_use >= float(filters["min_notional"]):
                    # qty bruta pelo preço atual (vamos comprar MARKET p/ garantir execução)
                    qty = Decimal(str(quote_to_use / price))
                    qty = ex.quantize_step(qty, filters["step_size"])

                    if qty >= filters["min_qty"] and qty * Decimal(str(price)) >= filters["min_notional"]:
                        # ---- BUY MARKET (simples/certeiro p/ capital baixo) ----
                        od = ex.order_market(cfg.symbol, "BUY", float(qty))
                        fill_price = price   # aproximação
                        fill_qty = float(qty)

                        # cria lote e arma SELL (TP líquido cfg)
                       # cria/mescla lote e tenta armar SELL
                        sell_target, eff_tp = calc_sell_target(ex, cfg, fill_price)
                        from src.db import upsert_accum_lot
                        lot_id, new_bp, new_qty = upsert_accum_lot(float(fill_price), float(fill_qty), float(sell_target))
                        ok, msg = try_arm_sell_for_lot(ex, cfg, filters, lot_id, float(new_qty), float(sell_target))
                        if ok:
                            print(f"[BUY] qty={format(qty,'f')} @~{fill_price:.2f} → [LOT#{lot_id}] SELL alvo {sell_target:.2f}")
                        else:
                            print(f"[BUY] qty={format(qty,'f')} @~{fill_price:.2f} → [LOT#{lot_id}] aguardando mínimo p/ SELL ({msg})")
                        # atualiza referência após compra para evitar avalanche
                        ref_price = price
                    else:
                        print(f"[BUY-SKIP] qty*price < minNotional (qty={format(qty,'f')}, price={price:.2f})")
                else:
                    print(f"[BUY-SKIP] quote_to_use < minNotional ({quote_to_use:.2f} < {float(filters['min_notional']):.2f})")

            # log curto
            lots_open = len(get_open_lots())
            print(f"[{datetime.utcnow().isoformat()}] REF={ref_price:.2f} price={price:.2f} drop={drop*100:.2f}% inv={inv_ratio*100:.1f}% lots={lots_open} spacing={spacing*100:.2f}% risk={risk_frac*100:.1f}%")

            time.sleep(poll)
        except KeyboardInterrupt:
            print("Interrompido por usuário.")
            break
        except Exception as e:
            print("Erro no loop:", e)
            traceback.print_exc()
            time.sleep(poll)

if __name__ == "__main__":
    main()
