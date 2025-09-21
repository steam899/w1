#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WolfBet Multi-Strategy Dice Bot (patched version)
- Fix: session_count attribute
- Fix: martingale reset ke base_bet bila win (classic style)
- Fix: auto strategy switch, bet pertama ikut last_loss_amount
"""

import json
import time
import random
import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout

console = Console()
API_BASE = "https://wolfbet.com/api/v1"


class WolfBetBot:
    def __init__(self, cfg_path="config.json"):
        with open(cfg_path, "r") as f:
            self.cfg = json.load(f)

        token = self.cfg.get("access_token", "").strip()
        if not token:
            raise ValueError("access_token kosong dalam config.json")

        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }

        # core settings
        self.currency = str(self.cfg.get("currency", "btc")).lower()
        self.base_bet = float(self.cfg.get("base_bet", 0.00000001))
        self.multiplier = float(self.cfg.get("multiplier", 2.0))
        self.max_bet = float(self.cfg.get("max_bet", 0.0001))
        self.chance = float(self.cfg.get("chance", 49.5))
        self.rule_mode = str(self.cfg.get("rule_mode", "auto")).lower()
        self.take_profit = float(self.cfg.get("take_profit", 0.0005))
        self.stop_loss = float(self.cfg.get("stop_loss", -0.0005))
        self.cooldown = float(self.cfg.get("cooldown_sec", 1.0))
        self.debug = bool(self.cfg.get("debug", True))
        self.auto_start = bool(self.cfg.get("auto_start", False))
        self.auto_start_delay = int(self.cfg.get("auto_start_delay", 5))

        # strategy config
        self.strategy = str(self.cfg.get("strategy", "martingale")).lower()
        self.auto_strategy_change = bool(self.cfg.get("auto_strategy_change", True))
        self.strategy_cycle = [s.lower() for s in self.cfg.get("strategy_cycle", [self.strategy])]
        self.strategy_switch_mode = str(self.cfg.get("strategy_switch_mode", "on_win")).lower()
        self.loss_streak_trigger = int(self.cfg.get("loss_streak_trigger", 5))

        # jackpot & high-risk parameters
        self.jackpot_chance = float(self.cfg.get("jackpot_chance", 1.0))  # percent
        self.jackpot_raise_min = float(self.cfg.get("jackpot_raise_min_pct", 1.02))  # 2%
        self.jackpot_raise_max = float(self.cfg.get("jackpot_raise_max_pct", 1.05))  # 5%

        self.high_risk_chance = float(self.cfg.get("high_risk_chance", 5.0))  # percent
        self.high_risk_raise_min = float(self.cfg.get("high_risk_raise_min_pct", 1.10))  # 10%
        self.high_risk_raise_max = float(self.cfg.get("high_risk_raise_max_pct", 1.20))  # 20%
        self.high_risk_interval = int(self.cfg.get("high_risk_interval", 20))

        # randomized config
        self.randomized_mode = str(self.cfg.get("randomized_mode", "multiplier")).lower()
        self.randomized_min_mult = float(self.cfg.get("randomized_min_mult", 1.02))
        self.randomized_max_mult = float(self.cfg.get("randomized_max_mult", 1.35))

        # runtime state
        self.session_profit = 0.0
        self.session_count = 0   # ✅ PATCH: tambah attribute
        self.current_bet = self.base_bet
        self.bet_history = []
        self.start_time = None
        self.loss_streak_total = 0.0
        self.loss_streak_count = 0
        self.total_bets = 0
        self.win_count = 0
        self.lose_count = 0
        self.current_strategy = self.strategy
        self.strategy_index = (self.strategy_cycle.index(self.strategy)
                               if self.strategy in self.strategy_cycle else 0)
        self.last_loss_amount = self.base_bet
        self.last_outcome = None
        self.override_chance = None
        self.fibo_seq = [self.base_bet]

    # -------------------- HTTP --------------------
    def _get(self, path):
        try:
            r = requests.get(f"{API_BASE}{path}", headers=self.headers, timeout=20)
            return r
        except Exception as e:
            if self.debug:
                console.print(f"[red]⚠️ GET {path} error:[/red] {e}")
            return None

    def _post(self, path, payload):
        try:
            r = requests.post(f"{API_BASE}{path}", headers=self.headers, json=payload, timeout=20)
            return r
        except Exception as e:
            if self.debug:
                console.print(f"[yellow]⚠️ POST {path} error:[/yellow] {e}")
            return None

    def get_balance_currency(self, currency):
        r = self._get("/user/balances")
        if not r:
            return None
        try:
            for b in r.json().get("balances", []):
                if str(b.get("currency", "")).lower() == currency.lower():
                    return float(b.get("amount", 0))
        except Exception:
            return None
        return None

    def place_dice_bet(self, amount, rule, bet_value):
        amount = round(float(amount), 8)
        win_chance = bet_value if rule == "under" else (100.0 - bet_value)
        win_chance = max(win_chance, 0.01)
        multiplier = float(f"{99.0 / win_chance:.4f}")
        payload = {
            "currency": self.currency,
            "game": "dice",
            "amount": str(amount),
            "rule": rule,
            "bet_value": str(bet_value),
            "multiplier": str(multiplier)
        }
        r = self._post("/bet/place", payload)
        if not r:
            return None
        try:
            return r.json()
        except Exception:
            return None

    # -------------------- Strategy --------------------
    def strat_martingale(self, win, last_bet):
        if win:
            return self.base_bet   # ✅ classic martingale reset
        return last_bet * self.multiplier

    def strat_fibonacci(self, win):
        if win:
            self.fibo_seq = [self.base_bet]
            return self.base_bet
        if len(self.fibo_seq) < 2:
            self.fibo_seq.append(self.base_bet)
        else:
            self.fibo_seq.append(self.fibo_seq[-1] + self.fibo_seq[-2])
        return self.fibo_seq[-1]

    def strat_flat(self, win, last_bet):
        return self.base_bet

    def strat_jackpot_hunter(self, win):
        self.override_chance = self.jackpot_chance
        if win:
            return self.base_bet
        factor = random.uniform(self.jackpot_raise_min, self.jackpot_raise_max)
        return self.current_bet * factor

    def strat_high_risk_pulse(self, win):
        self.override_chance = self.high_risk_chance
        if win:
            return self.base_bet
        factor = random.uniform(self.high_risk_raise_min, self.high_risk_raise_max)
        return self.current_bet * factor

    def strat_randomized(self):
        if self.randomized_mode == "multiplier":
            factor = random.uniform(self.randomized_min_mult, self.randomized_max_mult)
            return self.current_bet * factor
        else:
            upper = max(self.last_loss_amount, self.base_bet)
            return random.uniform(self.base_bet, upper)

    # -------------------- Strategy Switch --------------------
    def switch_strategy(self):
        old = self.current_strategy
        self.strategy_index = (self.strategy_index + 1) % len(self.strategy_cycle)
        self.current_strategy = self.strategy_cycle[self.strategy_index]
        console.print(f"[yellow]🔁 Strategy switched: {old} -> {self.current_strategy}[/yellow]")

        # ✅ first bet ikut last_loss_amount kalau ada
        if self.last_loss_amount > self.base_bet:
            self.current_bet = self.last_loss_amount
        else:
            self.current_bet = self.base_bet

    # -------------------- Main Loop --------------------
    def main_loop(self):
        console.clear()
        start_balance = self.get_balance_currency(self.currency)
        if start_balance is None:
            console.print("[red]❌ Failed get balance[/red]")
            return

        self.session_profit = 0.0
        self.current_bet = self.base_bet
        self.bet_history = []
        self.start_time = time.time()
        self.loss_streak_total = 0.0
        self.loss_streak_count = 0
        self.last_loss_amount = self.base_bet
        self.total_bets = 0
        self.win_count = 0
        self.lose_count = 0
        self.current_strategy = self.strategy
        self.strategy_index = self.strategy_cycle.index(self.current_strategy)

        with Live(refresh_per_second=4, screen=True) as live:
            while True:
                if self.session_profit <= self.stop_loss or self.session_profit >= self.take_profit:
                    break

                rule = "under" if random.randint(0, 1) else "over"
                bet_value = self.chance if rule == "under" else 100 - self.chance
                data = self.place_dice_bet(self.current_bet, rule, bet_value)
                if not data:
                    time.sleep(self.cooldown)
                    continue

                bet = data.get("bet")
                if not bet:
                    time.sleep(self.cooldown)
                    continue

                state = bet.get("state")
                profit = float(bet.get("profit", 0))
                self.total_bets += 1
                self.last_outcome = state

                if state == "win":
                    self.session_profit += profit
                    self.win_count += 1
                    self.loss_streak_total = 0
                    self.loss_streak_count = 0
                    self.last_loss_amount = self.base_bet

                    if self.current_strategy == "martingale":
                        self.current_bet = self.strat_martingale(True, self.current_bet)
                    elif self.current_strategy == "fibonacci":
                        self.current_bet = self.strat_fibonacci(True)
                    elif self.current_strategy == "flat":
                        self.current_bet = self.strat_flat(True, self.current_bet)
                    elif self.current_strategy == "jackpot_hunter":
                        self.current_bet = self.strat_jackpot_hunter(True)
                    elif self.current_strategy == "high_risk_pulse":
                        self.current_bet = self.strat_high_risk_pulse(True)
                    elif self.current_strategy == "randomized":
                        self.current_bet = self.strat_randomized()

                    if self.auto_strategy_change and self.strategy_switch_mode == "on_win":
                        self.switch_strategy()

                else:  # lose
                    loss_amount = float(bet.get("amount", self.current_bet))
                    self.session_profit -= loss_amount
                    self.lose_count += 1
                    self.loss_streak_count += 1
                    self.loss_streak_total += loss_amount
                    self.last_loss_amount = loss_amount

                    if self.current_strategy == "martingale":
                        self.current_bet = self.strat_martingale(False, self.current_bet)
                    elif self.current_strategy == "fibonacci":
                        self.current_bet = self.strat_fibonacci(False)
                    elif self.current_strategy == "flat":
                        self.current_bet = self.strat_flat(False, self.current_bet)
                    elif self.current_strategy == "jackpot_hunter":
                        self.current_bet = self.strat_jackpot_hunter(False)
                    elif self.current_strategy == "high_risk_pulse":
                        self.current_bet = self.strat_high_risk_pulse(False)
                    elif self.current_strategy == "randomized":
                        self.current_bet = self.strat_randomized()

                    if self.auto_strategy_change and self.strategy_switch_mode == "on_loss_streak":
                        if self.loss_streak_count >= self.loss_streak_trigger:
                            self.switch_strategy()
                            self.loss_streak_count = 0

                time.sleep(self.cooldown)

    def run(self):
        self.session_count += 1  # ✅ now attribute exists
        self.main_loop()


if __name__ == "__main__":
    bot = WolfBetBot("config.json")
    while True:
        bot.run()
        if not bot.auto_start:
            break
        console.print(f"\n[cyan]🔄 Auto-restart in {bot.auto_start_delay} sec...[/cyan]")
        time.sleep(bot.auto_start_delay)
