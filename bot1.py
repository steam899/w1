#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WolfBet Multi-Strategy Dice Bot — COMPLETE
- Strategies: martingale, fibonacci, dalembert (smooth), flat,
  jackpot_hunter (2-5% raise on loss), high_risk_pulse (10-20% raise on loss),
  randomized (uses last_loss_amount upper bound)
- Auto-switch modes: on_win, on_loss_streak
- Cover-loss: next bet will attempt to cover cumulative loss + base_bet
- NOTE: This version intentionally DOES NOT enforce a `max_bet` cap.
  That means bet sizes can grow arbitrarily large. Use with extreme caution.

Dependencies: requests, rich
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
        # NOTE: no max_bet in this version (user requested)
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

        # runtime state
        self.session_profit = 0.0
        self.current_bet = self.base_bet
        self.bet_history = []
        self.start_time = None
        self.loss_streak_total = 0.0
        self.loss_streak_count = 0
        self.session_count = 0
        self.current_strategy = self.strategy
        self.strategy_index = (self.strategy_cycle.index(self.strategy)
                               if self.strategy in self.strategy_cycle else 0)
        self.last_loss_amount = self.base_bet
        self.last_outcome = None
        self.override_chance = None
        self.fibo_seq = [self.base_bet]
        self.total_bets = 0
        self.win_count = 0
        self.lose_count = 0

    # ---------- HTTP helpers ----------
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

    def get_balances(self):
        r = self._get("/user/balances")
        if not r:
            return None
        try:
            data = r.json()
            return data.get("balances", [])
        except Exception:
            return None

    def get_balance_currency(self, currency):
        balances = self.get_balances()
        if not balances:
            return None
        for b in balances:
            if str(b.get("currency", "")).lower() == currency.lower():
                try:
                    return float(b.get("amount"))
                except Exception:
                    return None
        return None

    def place_dice_bet(self, amount, rule, bet_value):
        amount = round(float(amount), 8)
        win_chance = bet_value if rule == "under" else (100.0 - bet_value)
        win_chance = max(win_chance, 0.01)
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
        rl_left = r.headers.get("x-ratelimit-remaining")

        try:
            data = r.json()
            return data, (rl_limit, rl_left)
        except Exception:
            return None, (rl_limit, rl_left)

    # ---------- helpers ----------
    @staticmethod
    def _cap(val, lo, hi):
        return max(lo, min(hi, val))

    def chance_to_rule_and_threshold(self, chance_override=None):
        ch = chance_override if chance_override is not None else self.chance
        ch = self._cap(ch, 0.01, 99.99)
        if self.rule_mode == "over":
            rule = "over"
            bet_value = self._cap(100.0 - ch, 0.01, 99.99)
        elif self.rule_mode == "under":
            rule = "under"
            bet_value = self._cap(ch, 0.01, 99.99)
        else:
            if random.randint(0, 1) == 1:
                rule = "under"
                bet_value = self._cap(ch, 0.01, 99.99)
            else:
                rule = "over"
                bet_value = self._cap(100.0 - ch, 0.01, 99.99)
        return rule, bet_value

    # ---------- strategies ----------
    def strat_martingale(self, win, last_bet):
        if win:
            return self.base_bet
        return round(last_bet * self.multiplier, 8)

    def strat_fibonacci(self, win):
        if win:
            self.fibo_seq = [self.base_bet]
            return self.base_bet
        if len(self.fibo_seq) < 2:
            self.fibo_seq.append(self.base_bet)
        else:
            self.fibo_seq.append(self.fibo_seq[-1] + self.fibo_seq[-2])
        return round(self.fibo_seq[-1], 8)

    def strat_dalembert(self, win, last_bet):
        # smooth D'Alembert: step = 25% of base_bet
        step = self.base_bet * 0.25
        if win:
            return max(self.base_bet, round(last_bet - step, 8))
        return round(last_bet + step, 8)

    def strat_flat(self, win, last_bet):
        return self.base_bet

    def strat_jackpot_hunter(self):
        # conservative jackpot hunter: small chance override, small raises on loss
        self.override_chance = self.jackpot_chance
        if self.last_outcome == "lose":
            factor = random.uniform(self.jackpot_raise_min, self.jackpot_raise_max)
            bet = round(self.current_bet * factor, 8)
        else:
            bet = self.base_bet
        return bet

    def strat_high_risk_pulse(self):
        # moderate high-risk pulse: chance override and small raises on loss
        self.override_chance = self.high_risk_chance
        if self.last_outcome == "lose":
            factor = random.uniform(self.high_risk_raise_min, self.high_risk_raise_max)
            bet = round(self.current_bet * factor, 8)
        else:
            # occasional pulse based on interval
            if self.total_bets > 0 and (self.total_bets % self.high_risk_interval) == 0:
                bet = round(self.base_bet * 5, 8)
            else:
                bet = self.base_bet
        return bet

    def strat_randomized(self):
        # use last_loss_amount as upper bound (not capped)
        upper = max(self.last_loss_amount, self.base_bet)
        amount = round(random.uniform(self.base_bet, upper), 8)
        return amount

    # ---------- UI helpers ----------
    def _summary_panel(self, start_balance, current_balance, total_bets, win, lose, runtime):
        txt = f"""
[bold yellow]🏦 Baki Awal :[/bold yellow] {start_balance:.8f} {self.currency.upper()}
[bold cyan]💱 Baki Sekarang:[/bold cyan] {current_balance:.8f} {self.currency.upper()}
[bold green]🏧 Profit/Rugi:[/bold green] {self.session_profit:.8f} {self.currency.upper()}
[bold magenta]🔄 Jumlah BET :[/bold magenta] {total_bets} (WIN {win} / LOSE {lose})
[bold white]⏰ Runtime :[/bold white] {runtime}
[bold red]🚦 Session :[/bold red] {self.session_count}
[bold blue]🎯 Strategy :[/bold blue] {self.current_strategy}
[bold red]⚠ Loss Streak Total :[/bold red] {self.loss_streak_total:.8f}
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

    def _update_ui(self, start_balance, current_balance, total_bets, win, lose, live):
        elapsed = int(time.time() - self.start_time)
        runtime_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
        layout = Layout()
        layout.split(
            Layout(name="summary", size=11),
            Layout(name="bets", ratio=3),
            Layout(name="speed", size=4)
        )
        layout["summary"].update(self._summary_panel(start_balance, current_balance, total_bets, win, lose, runtime_str))
        layout["bets"].update(self._bet_table())
        layout["speed"].update(self._speed_panel(total_bets))
        live.update(layout)

    # ---------- main loop ----------
    def main_loop(self):
        console.clear()
        console.print("\n[bold cyan]Starting WolfBet Multi-Strategy Bot[/bold cyan]\n")
        start_balance = self.get_balance_currency(self.currency)
        if start_balance is None:
            console.print("[red]❌ Gagal dapatkan baki - semak token/endpoint[/red]")
            return

        # reset runtime state
        self.session_profit = 0.0
        self.current_bet = self.base_bet
        self.bet_history = []
        self.start_time = time.time()
        self.loss_streak_total = 0.0
        self.loss_streak_count = 0
        self.last_loss_amount = self.base_bet
        self.last_outcome = None
        self.override_chance = None
        self.total_bets = 0
        self.win_count = 0
        self.lose_count = 0
        self.current_strategy = self.strategy
        if self.current_strategy in self.strategy_cycle:
            self.strategy_index = self.strategy_cycle.index(self.current_strategy)
        else:
            self.strategy_index = 0

        console.print(f"[green]💰 Baki awal:[/green] {start_balance:.8f} {self.currency.upper()}  |  [blue]Start strategy:[/blue] {self.current_strategy}\n")

        with Live(refresh_per_second=4, screen=True) as live:
            while True:
                # stop conditions
                if self.session_profit <= self.stop_loss:
                    console.print(f"\n[yellow]🛑 Stop-loss triggered:[/yellow] {self.session_profit:.8f} {self.currency.upper()}")
                    break
                if self.session_profit >= self.take_profit:
                    console.print(f"\n[green]✅ Take-profit triggered:[/green] {self.session_profit:.8f} {self.currency.upper()}")
                    break

                # prepare chance override
                self.override_chance = None

                # determine rule & threshold using any override
                rule, bet_value = self.chance_to_rule_and_threshold(self.override_chance)

                # place bet
                data, _ = self.place_dice_bet(amount=self.current_bet, rule=rule, bet_value=bet_value)
                if not data:
                    time.sleep(self.cooldown)
                    continue
                bet = data.get("bet")
                if bet is None:
                    time.sleep(self.cooldown)
                    continue

                state = bet.get("state")
                profit = float(bet.get("profit", 0) or 0)
                result_value = str(bet.get("result_value"))
                self.total_bets += 1

                # record outcome
                self.last_outcome = state

                if state == "win":
                    self.session_profit += profit
                    self.win_count += 1
                    outcome = "[bold green]WIN[/bold green]"
                    display_profit = f"[bold green]{profit:.8f}[/bold green]"

                    # reset loss trackers
                    self.loss_streak_total = 0.0
                    self.loss_streak_count = 0
                    self.last_loss_amount = self.base_bet

                    # determine next bet according to strategy win behaviour
                    if self.current_strategy == "martingale":
                        self.current_bet = self.strat_martingale(True, self.current_bet)
                    elif self.current_strategy == "fibonacci":
                        self.current_bet = self.strat_fibonacci(True)
                    elif self.current_strategy == "dalembert":
                        self.current_bet = self.strat_dalembert(True, self.current_bet)
                    elif self.current_strategy == "flat":
                        self.current_bet = self.strat_flat(True, self.current_bet)
                    elif self.current_strategy == "jackpot_hunter":
                        self.current_bet = self.strat_jackpot_hunter()
                    elif self.current_strategy == "high_risk_pulse":
                        self.current_bet = self.strat_high_risk_pulse()
                    elif self.current_strategy == "randomized":
                        self.current_bet = self.strat_randomized()
                    else:
                        self.current_bet = self.base_bet

                    # auto-switch strategy if configured and mode = on_win
                    if self.auto_strategy_change and self.strategy_switch_mode == "on_win":
                        if len(self.strategy_cycle) > 0:
                            self.strategy_index = (self.strategy_index + 1) % len(self.strategy_cycle)
                            old = self.current_strategy
                            self.current_strategy = self.strategy_cycle[self.strategy_index]
                            console.print(f"[yellow]🔁 Strategy switched (on win): {old} -> {self.current_strategy}[/yellow]")

                else:
                    # LOSS
                    loss_amount = float(bet.get("amount", self.current_bet))
                    self.session_profit -= loss_amount
                    self.lose_count += 1
                    self.loss_streak_count += 1
                    outcome = "[red]LOSE[/red]"
                    self.loss_streak_total += loss_amount
                    self.last_loss_amount = loss_amount
                    display_profit = f"[red]{-self.loss_streak_total:.8f}[/red]"

                    # determine next bet according to strategy loss behaviour
                    if self.current_strategy == "martingale":
                        next_bet = self.strat_martingale(False, self.current_bet)
                    elif self.current_strategy == "fibonacci":
                        next_bet = self.strat_fibonacci(False)
                    elif self.current_strategy == "dalembert":
                        next_bet = self.strat_dalembert(False, self.current_bet)
                    elif self.current_strategy == "flat":
                        next_bet = self.strat_flat(False, self.current_bet)
                    elif self.current_strategy == "jackpot_hunter":
                        next_bet = self.strat_jackpot_hunter()
                    elif self.current_strategy == "high_risk_pulse":
                        next_bet = self.strat_high_risk_pulse()
                    elif self.current_strategy == "randomized":
                        next_bet = self.strat_randomized()
                    else:
                        next_bet = self.base_bet

                    # COVER LOSS: ensure next bet tries to cover cumulative loss + base_bet
                    cover_needed = abs(self.loss_streak_total) + self.base_bet
                    if next_bet < cover_needed:
                        next_bet = cover_needed

                    # NOTE: no max_bet cap here (user requested). We still round.
                    self.current_bet = round(next_bet, 8)

                    # auto-switch on loss_streak if configured
                    if self.auto_strategy_change and self.strategy_switch_mode == "on_loss_streak":
                        if self.loss_streak_count >= self.loss_streak_trigger and len(self.strategy_cycle) > 0:
                            old = self.current_strategy
                            self.strategy_index = (self.strategy_index + 1) % len(self.strategy_cycle)
                            self.current_strategy = self.strategy_cycle[self.strategy_index]
                            console.print(f"[red]🔁 Strategy switched (on loss streak): {old} -> {self.current_strategy}[/red]")
                            self.loss_streak_count = 0

                # history & UI
                arrow = "↑" if rule == "over" else "↓"
                self.bet_history.append([
                    f"{bet_value:.2f}[cyan]{arrow}[/cyan]",
                    result_value,
                    f"{self.current_bet:.8f}",
                    outcome,
                    display_profit
                ])

                current_balance = start_balance + self.session_profit
                self._update_ui(start_balance, current_balance, self.total_bets, self.win_count, self.lose_count, live)

                # clear override
                self.override_chance = None

                time.sleep(self.cooldown)

        # final summary
        final_runtime = time.strftime("%H:%M:%S", time.gmtime(int(time.time() - self.start_time)))
        final_panel = self._summary_panel(start_balance, start_balance + self.session_profit, self.total_bets, self.win_count, self.lose_count, final_runtime)
        console.print(final_panel)

    def run(self):
        self.session_count += 1
        self.main_loop()


if __name__ == "__main__":
    bot = WolfBetBot("config.json")
    while True:
        bot.run()
        if not bot.auto_start:
            break
        console.print(f"\n[cyan]🔄 Auto-restart in {bot.auto_start_delay} seconds...[/cyan]")
        time.sleep(bot.auto_start_delay)
