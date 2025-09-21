# WolfBet Multi-Strategy Dice Bot

Bot automatik untuk [WolfBet](https://wolfbet.com) dice game dengan pelbagai strategi yang boleh bertukar secara automatik.

## 🚀 Ciri-ciri
- Strategi sokongan:
  - **Martingale**
  - **Fibonacci**
  - **Flat**
  - **Jackpot Hunter** (kenaikan 2–5% bila kalah)
  - **High-Risk Pulse** (kenaikan 10–20% bila kalah, ada pulse mode)
  - **Randomized** (guna `last_loss_amount` sebagai upper bound)
- Auto-switch strategy:
  - `on_win`
  - `on_loss_streak`
- Cover-loss system: cuba recover semua loss + profit asas.
- UI terminal interaktif (guna **rich**) dengan:
  - Ringkasan sesi
  - Senarai bet terkini
  - Kelajuan bets/sec

## 📦 Keperluan
Python **3.9+** dan library berikut:

```bash
pip install -r requirements.txt
```

File `requirements.txt`:
```txt
requests>=2.31.0
rich>=13.7.0
```

## ⚙️ Konfigurasi
Edit file `config.json`:
```json
{
  "access_token": "ISI_TOKEN_WOLFBET_DI_SINI",
  "currency": "btc",
  "base_bet": 0.00000001,
  "multiplier": 2.0,
  "chance": 49.5,
  "rule_mode": "auto",
  "take_profit": 0.0005,
  "stop_loss": -0.0005,
  "cooldown_sec": 1.0,
  "debug": true,
  "auto_start": true,
  "auto_start_delay": 5,
  "strategy": "martingale",
  "auto_strategy_change": true,
  "strategy_switch_mode": "on_loss_streak",
  "loss_streak_trigger": 5,
  "strategy_cycle": [
    "martingale",
    "fibonacci",
    "flat",
    "jackpot_hunter",
    "high_risk_pulse",
    "randomized"
  ],
  "jackpot_chance": 1.0,
  "jackpot_raise_min_pct": 1.02,
  "jackpot_raise_max_pct": 1.05,
  "high_risk_chance": 5.0,
  "high_risk_raise_min_pct": 1.10,
  "high_risk_raise_max_pct": 1.20,
  "high_risk_interval": 20
}
```

## ▶️ Jalankan Bot
```bash
python bot.py
```

## 📸 Contoh UI
![UI Screenshot](A_screenshot_of_a_terminal-based_user_interface_di.png)

---
⚠️ **Disclaimer**: Bot ini untuk tujuan eksperimen. Penggunaan sebenar di platform perjudian adalah risiko anda sendiri.