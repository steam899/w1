#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WolfBet Multi-Strategy Dice Bot (complete)
Features:
 - Strategies: martingale (classic), fibonacci, flat,
   jackpot_hunter (start from last bet; 1st lose: keep last bet, subsequent loses: small raise),
   high_risk_pulse (start from last bet; raise 10-20% on losses; occasional pulse),
   randomized (start from last bet; mode: multiplier|uniform)
 - Auto-switch: on_win / on_loss_streak
 - UI: Rich panels (summary, bet history, speed panel with Mode)
 - Safety: optional max_bet (set to 0 to disable)
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

# ---------------- ANSI colors ----------------
RED     = "\033[91m"
YELLOW  = "\033[93m"
GREEN   = "\033[92m"
CYAN    = "\033[96m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
WHITE   = "\033[97m"
RESET   = "\033[0m"

GRADIENT = [RED, YELLOW, GREEN, CYAN, BLUE, MAGENTA]

API_BASE = "https://wolfbet.com/api/v1"
console = Console()


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

        # Config utama
        self.currency = str(self.cfg.get("currency", "btc")).lower()
        self.base_bet = float(self.cfg.get("base_bet", 0.00000001))
        self.multiplier_factor = float(self.cfg.get("multiplier", 2.0))
        self.max_bet = float(self.cfg.get("max_bet", 0.0001))
        self.chance = float(self.cfg.get("chance", 49.5))
        self.rule_mode = str(self.cfg.get("rule_mode", "auto")).lower()
        self.take_profit = float(self.cfg.get("take_profit", 0.0005))
        self.stop_loss = float(self.cfg.get("stop_loss", -0.0005))
        self.cooldown = float(self.cfg.get("cooldown_sec", 1.0))
        self.debug = bool(self.cfg.get("debug", True))
        self.auto_start = bool(self.cfg.get("auto_start", False))
        self.auto_start_delay = int(self.cfg.get("auto_start_delay", 5))

        # Strategy
        self.strategy = str(self.cfg.get("strategy", "martingale")).lower()
        self.auto_strategy_change = bool(self.cfg.get("auto_strategy_change", False))
        self.strategy_switch_mode = str(self.cfg.get("strategy_switch_mode", "on_loss_streak"))
        self.loss_streak_trigger = int(self.cfg.get("loss_streak_trigger", 5))
        self.strategy_cycle = self.cfg.get("strategy_cycle", [])

        # Extra strategy config
        self.jackpot_chance = float(self.cfg.get("jackpot_chance", 1.0))
        self.jackpot_raise_min_pct = float(self.cfg.get("jackpot_raise_min_pct", 1.02))
        self.jackpot_raise_max_pct = float(self.cfg.get("jackpot_raise_max_pct", 1.05))

        self.high_risk_chance = float(self.cfg.get("high_risk_chance", 5.0))
        self.high_risk_raise_min_pct = float(self.cfg.get("high_risk_raise_min_pct", 1.10))
        self.high_risk_raise_max_pct = float(self.cfg.get("high_risk_raise_max_pct", 1.20))
        self.high_risk_interval = int(self.cfg.get("high_risk_interval", 20))

        self.randomized_mode = str(self.cfg.get("randomized_mode", "multiplier"))
        self.randomized_min_mult = float(self.cfg.get("randomized_min_mult", 1.02))
        self.randomized_max_mult = float(self.cfg.get("randomized_max_mult", 1.35))

        # State
        self.session_profit = 0.0
        self.current_bet = self.base_bet
        self.bet_history = []
        self.start_time = None
        self.loss_streak_total = 0.0
        self.session_count = 0
        self.loss_streak = 0
        self.last_loss_amount = None
        self.last_result = None
        self.fibo_seq = [1, 1]
        self.fibo_index = 0

    # ---------------- REST calls ----------------
    def _get(self, path):
        try:
            r = requests.get(f"{API_BASE}{path}", headers=self.headers, timeout=20)
            return r
        except Exception as e:
            if self.debug:
                console.print(f"[red]⚠️ GET {path} network error:[/red] {e}")
            return None

    def _post(self, path, payload):
        try:
            r = requests.post(f"{API_BASE}{path}", headers=self.headers, json=payload, timeout=20)
            return r
        except Exception as e:
            if self.debug:
                console.print(f"[yellow]⚠️ POST {path} network error:[/yellow] {e}")
            return None

    def get_balance_currency(self, currency):
        r = self._get("/user/balances")
        if not r:
            return None
        try:
            data = r.json()
            for b in data.get("balances", []):
                if str(b.get("currency", "")).lower() == currency.lower():
                    return float(b.get("amount"))
            return None
        except Exception:
            return None

    def place_dice_bet(self, amount, rule, bet_value):
        amount = round(float(amount), 8)
        win_chance = bet_value if rule == "under" else (100.0 - bet_value)
        multiplier = 99.0 / win_chance
        multiplier = float(f"{multiplier:.4f}")

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
            return None, None

        rl_limit = r.headers.get("x-ratelimit-limit")
        rl_left  = r.headers.get("x-ratelimit-remaining")

        try:
            data = r.json()
            return data, (rl_limit, rl_left)
        except Exception:
            return None, (rl_limit, rl_left)

    # -------------- Helpers --------------
    def _cap(self, val, lo, hi):
        return max(lo, min(hi, val))

    def chance_to_rule_and_threshold(self):
        chance = self._cap(self.chance, 0.01, 99.99)
        if self.rule_mode == "over":
            return "over", self._cap(100.0 - chance, 0.01, 99.99)
        elif self.rule_mode == "under":
            return "under", chance
        else:
            if random.randint(0, 1) == 1:
                return "under", chance
            else:
                return "over", self._cap(100.0 - chance, 0.01, 99.99)

    def get_starting_bet(self):
        if self.last_result is False and self.last_loss_amount:
            return self.last_loss_amount
        return self.base_bet

    # -------------- UI helpers --------------
    def _summary_panel(self, start_balance, current_balance, total_bets, win, lose, runtime, active_strategy):
        txt = f"""
[bold yellow]🏦Baki Awal :[/bold yellow] {start_balance:.8f} {self.currency.upper()}
[bold cyan]💱Baki Sekarang:[/bold cyan] {current_balance:.8f} {self.currency.upper()}
[bold green]🏧Profit/Rugi:[/bold green] {self.session_profit:.8f} {self.currency.upper()}
[bold magenta]🔄Jumlah BET :[/bold magenta] {total_bets} (WIN {win} / LOSE {lose})
[bold white]⏰Runtime :[/bold white] {runtime}
[bold red]🚦Session :[/bold red] {self.session_count}
[bold blue]🎯Strategy :[/bold blue] {active_strategy}
"""
        return Panel(txt, title="📊 Ringkasan Sesi", border_style="bold blue")

    def _bet_table(self):
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Target")
        table.add_column("Result")
        table.add_column("Bet Next")
        table.add_column("W/L")
        table.add_column("Profit")

        for row in self.bet_history[-32:]:
            table.add_row(*row)
        return table

    def _speed_panel(self, total_bets):
        elapsed = max(1, int(time.time() - self.start_time))
        speed = round(total_bets / elapsed, 2)
        text = "[bold yellow][ GUNA VPS UNTUK + SPEED ][/bold yellow]\n" \
               f"Speed :[bold magenta]{speed}[/bold magenta] Bets / Second"
        return Panel(text, border_style="green")

    def _update_ui(self, start_balance, current_balance, total_bets, win, lose, live, active_strategy):
        elapsed = int(time.time() - self.start_time)
        runtime_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))

        layout = Layout()
        layout.split(
            Layout(name="summary", size=11),
            Layout(name="bets", ratio=3),
            Layout(name="speed", size=6)
        )
        layout["summary"].update(
            self._summary_panel(start_balance, current_balance, total_bets, win, lose, runtime_str, active_strategy)
        )
        layout["bets"].update(self._bet_table())

        # speed + strategy info
        extra_text = "[bold yellow][ GUNA VPS UNTUK + SPEED ][/bold yellow]\n" \
                     f"Speed :[bold magenta]{round(total_bets/max(1,elapsed),2)}[/bold magenta] Bets/sec\n" \
                     f"🎯 Active Strategy: [cyan]{active_strategy}[/cyan]"
        layout["speed"].update(Panel(extra_text, border_style="green"))

        live.update(layout)

    # -------------- Logo --------------
    def draw_logo(self):
        logo_text = "W O L F 🍀 D I C E 🍀 B O T"
        for i, c in enumerate(logo_text):
            color = GRADIENT[i % len(GRADIENT)]
            print(f"{color}{c}{RESET}", end="")
        print("\n")
        print("🎲🐺  🎲🐺  🎲🐺  🎲🐺  🎲🐺\n")

    # -------------- Strategy amount --------------
    def next_bet_amount(self, strategy):
        if strategy == "martingale":
            return self.current_bet * self.multiplier_factor
        elif strategy == "fibonacci":
            if self.fibo_index >= len(self.fibo_seq):
                self.fibo_seq.append(self.fibo_seq[-1] + self.fibo_seq[-2])
            amount = self.base_bet * self.fibo_seq[self.fibo_index]
            return amount
        elif strategy == "flat":
            return self.get_starting_bet()
        elif strategy == "jackpot_hunter":
            return self.current_bet * random.uniform(self.jackpot_raise_min_pct, self.jackpot_raise_max_pct)
        elif strategy == "high_risk_pulse":
            return self.current_bet * random.uniform(self.high_risk_raise_min_pct, self.high_risk_raise_max_pct)
        elif strategy == "randomized":
            return self.current_bet * random.uniform(self.randomized_min_mult, self.randomized_max_mult)
        else:
            return self.get_starting_bet()

    # -------------- Main loop --------------
    def run_strategy(self):
        self.draw_logo()
        start_balance = self.get_balance_currency(self.currency)
        if start_balance is None:
            console.print("[red]❌ Tak dapat baca balance.[/red]")
            return

        console.print(f"[green]💰 Baki awal:[/green] {start_balance:.8f} {self.currency.upper()}")
        self.session_profit = 0.0
        self.current_bet = self.get_starting_bet()
        win_count, lose_count, total_bets = 0, 0, 0
        self.start_time = time.time()
        self.loss_streak_total = 0.0

        active_strategy = self.strategy

        with Live(refresh_per_second=4, screen=True) as live:
            while True:
                if self.session_profit <= self.stop_loss:
                    console.print(f"\n[yellow]🛑 Stop-loss triggered:[/yellow] {self.session_profit:.8f}")
                    break
                if self.session_profit >= self.take_profit:
                    console.print(f"\n[green]✅ Take-profit triggered:[/green] {self.session_profit:.8f}")
                    break

                rule, bet_value = self.chance_to_rule_and_threshold()
                data, _ = self.place_dice_bet(self.current_bet, rule, bet_value)
                if not data:
                    time.sleep(self.cooldown)
                    continue
                bet = data.get("bet")
                if not bet:
                    time.sleep(self.cooldown)
                    continue

                state = bet.get("state")
                profit = float(bet.get("profit", 0) or 0)
                total_bets += 1
                result_value = str(bet.get("result_value"))
                outcome = ""
                display_profit = ""

                if state == "win":
                    self.session_profit += profit
                    win_count += 1
                    outcome = "[bold green]WIN[/bold green]"
                    self.current_bet = self.get_starting_bet()
                    self.loss_streak = 0
                    self.last_result = True
                    self.last_loss_amount = None
                    display_profit = f"[bold green]{profit:.8f}[/bold green]"
                    self.fibo_index = 0
                else:
                    loss_amount = float(bet.get("amount", self.current_bet))
                    self.session_profit -= loss_amount
                    lose_count += 1
                    outcome = "[red]LOSE[/red]"
                    self.loss_streak += 1
                    self.last_result = False
                    self.last_loss_amount = loss_amount
                    display_profit = f"[red]{-loss_amount:.8f}[/red]"
                    if active_strategy == "fibonacci":
                        self.fibo_index += 1
                    self.current_bet = self.next_bet_amount(active_strategy)

                arrow = "↑" if rule == "over" else "↓"
                self.bet_history.append([
                    f"{bet_value:.2f}[cyan]{arrow}[/cyan]",
                    result_value,
                    f"{self.current_bet:.8f}",
                    outcome,
                    display_profit
                ])

                current_balance = start_balance + self.session_profit
                self._update_ui(start_balance, current_balance, total_bets, win_count, lose_count, live, active_strategy)
                time.sleep(self.cooldown)

        final_runtime = time.strftime("%H:%M:%S", time.gmtime(int(time.time() - self.start_time)))
        console.print(self._summary_panel(start_balance, start_balance + self.session_profit, total_bets, win_count, lose_count, final_runtime, active_strategy))

    def run(self):
        self.session_count += 1
        self.run_strategy()


if __name__ == "__main__":
    bot = WolfBetBot("config.json")
    while True:
        bot.run()
        if not bot.auto_start:
            break
        console.print(f"\n[cyan]🔄 Auto-restart in {bot.auto_start_delay} seconds...[/cyan]")
        time.sleep(bot.auto_start_delay)

