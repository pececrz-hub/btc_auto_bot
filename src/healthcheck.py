import os
from src.config import load_config, load_secrets
from src.exchange_binance import BinanceWrapper

def main():
    cfg = load_config(os.getenv("CONFIG_PATH", "config.json"))
    api_key, api_secret = load_secrets()
    ex = BinanceWrapper(api_key, api_secret, use_testnet=cfg.use_testnet)
    ok, why = ex.validate_api()
    if not ok:
        raise SystemExit("FAIL: " + why)
    print("OK")

if __name__ == "__main__":
    main()
