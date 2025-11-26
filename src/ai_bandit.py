\
import random
from typing import Dict, List
from src.db import get_all_configs, insert_config, get_config_performance

class ParamManager:
    def __init__(self,
                 base_risk_frac: float,
                 min_pct_range=(0.03, 0.10),
                 max_pct_range=(0.10, 0.14),
                 num_configs: int = 5,
                 exploration_eps: float = 0.25):
        self.base_risk_frac = base_risk_frac
        self.min_pct_range = min_pct_range
        self.max_pct_range = max_pct_range
        self.num_configs = num_configs
        self.exploration_eps = exploration_eps
        self._ensure_configs_exist()

    def _ensure_configs_exist(self):
        configs = get_all_configs()
        if configs:
            return
        min_low, min_high = self.min_pct_range
        max_low, max_high = self.max_pct_range
        steps = max(1, self.num_configs - 1)
        for i in range(self.num_configs):
            min_pct = min_low + (min_high - min_low) * i / steps
            max_pct = max_low + (max_high - max_low) * i / steps
            insert_config(min_pct, max_pct, self.base_risk_frac)

    def _score_config(self, perf_row: dict) -> float:
        avg = perf_row["avg_pnl"]
        n = perf_row["num_trades"]
        bonus = min(n / 20.0, 1.0)
        return avg * bonus

    def choose_active_config(self) -> Dict:
        configs = get_all_configs()
        if not configs:
            raise RuntimeError("Nenhuma config encontrada.")
        if random.random() < self.exploration_eps:
            cfg = random.choice(configs)
            cfg["reason"] = "exploration"
            return cfg
        perf_index = {p["config_id"]: p for p in get_config_performance()}
        scored = []
        for cfg in configs:
            p = perf_index.get(cfg["id"])
            if p is None:
                scored.append((0.0, cfg, 0, 0.0))
            else:
                scored.append((self._score_config(p), cfg, p["num_trades"], p["avg_pnl"]))
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_cfg, n_trades, avg_pnl = scored[0]
        best_cfg["reason"] = f"exploitation(score={best_score:.4f}, trades={n_trades}, avg={avg_pnl:.6f})"
        return best_cfg
