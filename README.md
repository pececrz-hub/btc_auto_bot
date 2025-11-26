# BTC Auto Bot (10% NET)

Bot de trade para Binance Spot com:
- Lucro **mínimo líquido** alvo de **10%** (após taxas), pré-configurado
- Ordens **LIMIT_MAKER** (evita pagar taker, reduz taxa)
- Gerenciador de parâmetros (bandit) para auto-ajuste
- SQLite local para histórico
- Modo `LIVE` e `PAPER`
- (Opcional) Monitor de arbitragem via CCXT (só monitoramento por padrão)

## ⚠️ Avisos
- Capital muito pequeno sofre com mínimos de ordem e taxas.
- **Nenhuma garantia de lucro.** Use primeiro `use_testnet=true` ou `mode=PAPER`.
- Não compartilhe suas chaves. Use `.env`.

## Instalação
```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuração
1. Copie `.env.example` para `.env` e preencha `BINANCE_API_KEY/SECRET`.
2. Copie `config.json.example` para `config.json` e ajuste conforme necessário.
   - `min_profit_pct_net` = 0.10 (10% líquido pós-taxas)
   - `use_testnet`: `true` para começar
   - `mode`: `PAPER` para simular sem enviar ordens

## Executar
```bash
python -m src.main
```

Logs ficam no console. Banco em `trades.db`.

## Arbitragem (opcional)
- Preencha credenciais da segunda exchange no `.env` e ative `arbitrage.enabled` no `config.json` (código de monitor está em `src/arbitrage_monitor.py`). Este projeto **não** desloca fundos entre exchanges (risco, tempo, taxas). O fluxo recomendado é manter saldos pré-alocados nas duas exchanges e só executar se o edge líquido superar suas taxas + segurança.

## Segurança & Boas práticas
- `.env` no `.gitignore` (se versionar, não suba chaves).
- `LIMIT_MAKER` evita pagar taxa de **taker** (ordem rejeitada se cruzar o book).
- O alvo de venda já embute taxas (maker) + um buffer (`extra_fee_safety_bps`). Ajuste conforme sua conta/nível VIP.
- Se sua ordem não preencher, permanece no book (risco de não executar). Para capital pequeno, latência/step/minNotional podem bloquear execução.


## Validações no start
- Checa taxas e filtros (tick/step/minNotional)
- Calcula **portfólio total** (USDT + BTC*preço) e mostra
- Calcula **quantas trades a 10%** faltam para 1M (composto)
- Avisa se o risco base (ex.: 25%) não atinge o **minNotional**

## Motor de Reprogramação Automática
- Troca de configuração por **n trades** ou **n minutos**, o que vier antes.
- Reposiciona ordens maker se o preço desviar mais do que `rearm_threshold_pct` ou se exceder `order_ttl_seconds`.
- Sempre respeita `minNotional`, `stepSize`, `minQty` e `tickSize`.
