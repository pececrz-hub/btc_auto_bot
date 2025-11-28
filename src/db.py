import os
\
import sqlite3
from datetime import datetime
from typing import Optional, Tuple, List, Dict

DB_PATH = os.getenv('TRADES_DB_PATH', '/data/trades.db')

def get_conn():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    # schema (idempotente)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            qty REAL NOT NULL,
            fee REAL NOT NULL,
            pnl REAL NOT NULL,
            balance_after REAL NOT NULL,
            config_id INTEGER NOT NULL,
            order_id TEXT,
            client_order_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            min_change_pct REAL NOT NULL,
            max_change_pct REAL NOT NULL,
            trade_qty_frac REAL NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        )
    """)
    return conn

def insert_trade(side: str, price: float, qty: float, fee: float,
                 pnl: float, balance_after: float, config_id: int,
                 order_id: str = None, client_order_id: str = None):
    conn = get_conn()
    with conn:
        conn.execute("""
            INSERT INTO trades (ts, side, price, qty, fee, pnl, balance_after, config_id, order_id, client_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), side, price, qty, fee, pnl, balance_after, config_id, order_id, client_order_id))
    conn.close()

def get_stats() -> Tuple[float, float, int]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(AVG(pnl), 0) FROM trades")
    num_trades, total_pnl, avg_pnl = cur.fetchone()
    conn.close()
    return total_pnl, avg_pnl, num_trades or 0

def get_last_balance() -> Optional[float]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT balance_after FROM trades ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def get_config_performance() -> List[Dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT config_id,
               COUNT(*) as num_trades,
               COALESCE(SUM(pnl), 0) as total_pnl,
               COALESCE(AVG(pnl), 0) as avg_pnl
        FROM trades
        GROUP BY config_id
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {"config_id": r[0], "num_trades": r[1], "total_pnl": r[2], "avg_pnl": r[3]}
        for r in rows
    ]

def insert_config(min_change_pct: float, max_change_pct: float, trade_qty_frac: float) -> int:
    conn = get_conn()
    with conn:
        cur = conn.execute("""
            INSERT INTO configs (min_change_pct, max_change_pct, trade_qty_frac, created_at)
            VALUES (?, ?, ?, ?)
        """, (min_change_pct, max_change_pct, trade_qty_frac, datetime.utcnow().isoformat()))
        cid = cur.lastrowid
    conn.close()
    return cid

def get_all_configs():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, min_change_pct, max_change_pct, trade_qty_frac FROM configs
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "min_change_pct": r[1], "max_change_pct": r[2], "trade_qty_frac": r[3]}
        for r in rows
    ]

def kv_set(k: str, v: str):
    conn = get_conn()
    with conn:
        conn.execute("INSERT INTO state(k,v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    conn.close()

def kv_get(k: str) -> Optional[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT v FROM state WHERE k=?", (k,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def get_open_position_from_trades():
    """
    Retorna dict com {'entry_price': float, 'qty': float} se o último BUY
    não foi fechado por um SELL posterior. Caso contrário, retorna None.
    """
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, price, qty FROM trades WHERE side='BUY' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        buy_id, entry_price, qty = row
        # existe algum SELL com id > buy_id? se sim, essa posição já foi fechada
        cur.execute("SELECT 1 FROM trades WHERE side='SELL' AND id > ? LIMIT 1", (buy_id,))
        closed = cur.fetchone()
        if closed:
            return None
        return {"entry_price": float(entry_price), "qty": float(qty)}
    finally:
        conn.close()
# ================= Lotes (grid/TP por lote) =================

def _ensure_lots_schema(conn):
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      buy_price REAL NOT NULL,
      qty_remaining REAL NOT NULL,
      target_price REAL NOT NULL,
      sell_client_id TEXT,
      status TEXT NOT NULL DEFAULT 'OPEN', -- OPEN | SELL_PLACED | CLOSED
      created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lots_status ON lots(status);")
    conn.commit()

def insert_lot(buy_price: float, qty: float, target_price: float) -> int:
    conn = get_conn()
    _ensure_lots_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO lots(buy_price, qty_remaining, target_price, status) VALUES (?,?,?,'OPEN')",
        (buy_price, qty, target_price),
    )
    conn.commit()
    return cur.lastrowid

def set_lot_sell(lot_id: int, client_id: str, price: float) -> None:
    conn = get_conn()
    _ensure_lots_schema(conn)
    conn.execute(
        "UPDATE lots SET sell_client_id=?, target_price=?, status='SELL_PLACED', updated_ts=CURRENT_TIMESTAMP WHERE id=?",
        (client_id, price, lot_id),
    )
    conn.commit()

def close_lot(lot_id: int) -> None:
    conn = get_conn()
    _ensure_lots_schema(conn)
    conn.execute(
        "UPDATE lots SET status='CLOSED', updated_ts=CURRENT_TIMESTAMP WHERE id=?",
        (lot_id,),
    )
    conn.commit()

def get_open_lots():
    conn = get_conn()
    _ensure_lots_schema(conn)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, buy_price, qty_remaining, target_price, sell_client_id, status
        FROM lots
        WHERE status IN ('OPEN','SELL_PLACED')
    """)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
def upsert_accum_lot(buy_price: float, qty: float, target_price: float):
    """
    Junta a compra em um lote OPEN sem ordem de venda (acumulador).
    Se não existir, cria. Retorna (lot_id, new_buy_price, new_qty).
    """
    conn = get_conn()
    _ensure_lots_schema(conn)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, buy_price, qty_remaining
        FROM lots
        WHERE status='OPEN' AND (sell_client_id IS NULL OR sell_client_id='')
        LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        lot_id, bp, q = row
        new_qty = float(q) + float(qty)
        if new_qty <= 0:
            new_qty = 0.0
        new_bp = (float(bp) * float(q) + float(buy_price) * float(qty)) / new_qty if new_qty > 0 else float(buy_price)
        cur.execute(
            "UPDATE lots SET buy_price=?, qty_remaining=?, target_price=?, updated_ts=CURRENT_TIMESTAMP WHERE id=?",
            (new_bp, new_qty, target_price, lot_id),
        )
        conn.commit()
        return lot_id, new_bp, new_qty
    else:
        cur.execute(
            "INSERT INTO lots(buy_price, qty_remaining, target_price, status) VALUES (?,?,?,'OPEN')",
            (buy_price, qty, target_price),
        )
        conn.commit()
        return cur.lastrowid, float(buy_price), float(qty)
