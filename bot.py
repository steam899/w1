#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WolfBet Multi-Strategy Dice Bot (patched)
- Fix: when switching strategy, first bet uses last loss (saved in last_loss_for_switch)
- Win resets bet for continuing same strategy
- last_loss_for_switch is only updated on losses (so a later strategy switch can try to recover)
- UI via rich Live
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

        token = str(self.cfg.get("access_token", "")).strip()
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
        self.max_bet = float(self.cfg.get("max_bet", 0.0))  # 0 => disabled cap
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
        self.auto_strategy_change = bool(self.cfg.get("auto_strategy_change", False))
        self.strategy_cycle = [s.lower() for s in self.cfg.get("strategy_cycle", [self.strategy])]
        self.strategy_switch_mode = str(self.cfg.get("strategy_switch_mode", "on_win")).lower()
        self.loss_streak_trigger = int(self.cfg.get("loss_streak_trigger", 5))

        # jackpot & high-risk parameters
        self.jackpot_chance = float(self.cfg.get("jackpot_chance", 1.0))
        self.jackpot_raise_min = float(self.cfg.get("jackpot_raise_min_pct", 1.02))
        self.jackpot_raise_max = float(self.cfg.get("jackpot_raise_max_pct", 1.05))

        self.high_risk_chance = float(self.cfg.get("high_risk_chance", 5.0))
        self.high_risk_raise_min = float(self.cfg.get("high_risk_raise_min_pct", 1.10))
        self.high_risk_raise_max = float(self.cfg.get("high_risk_raise_max_pct", 1.20))
        self.high_risk_interval = int(self.cfg.get("high_risk_interval", 20))

        # randomized config
        self.randomized_mode = str(self.cfg.get("randomized_mode", "multiplier")).lower()
        self.randomized_min_mult = float(self.cfg.get("randomized_min_mult", 1.02))
        self.randomized_max_mult = float(self.cfg.get("randomized_max_mult", 1.5))

        # runtime state
        self.session_profit = 0.0
        self.session_count = 0                     # ensure exists
        self.current_bet = self.base_bet
        self.last_bet_amount = 0.0                 # server-reported last bet amount
        self.last_loss_amount = 0.0                # last lost amount (updated on loss)
        self.last_loss_for_switch = 0.0            # preserved last loss to use when switching strategies
        self.last_outcome = None                   # "win" or "lose"
        self.total_bets = 0
        self.win_count = 0
        self.lose_count = 0
        self.loss_streak_count = 0
        self.start_time = None
        self.bet_history = []
        # fibonacci state
        self.fibo_seq = [self.base_bet, self.base_bet]
        self.fibo_index = 0
        # strategy index & active
        self.current_strategy = self.strategy
        self.strategy_index = (self.strategy_cycle.index(self.strategy)
                               if self.strategy in self.strategy_cycle else 0)

    # ---------- HTTP helpers ----------
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

    def get_balances(self):
        r = self._get("/user/balances")
        if not r:
            return None
        try:
            return r.json().get("balances", [])
        except Exception:
            return None

    def get_balance_currency(self, currency):
        balances = self.get_balances()
        if not balances:
            return None
        for b in balances:
            if str(b.get("currency", "")).lower() == currency.lower():
                try:
                    return float(b.get("amount", 0))
                except Exception:
                    return None
        return None

    def place_dice_bet(self, amount, rule, bet_value):
        """Place bet via API and return parsed response dict or None."""
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
            return None
        try:
            return r.json()
        except Exception:
            return None

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

    def get_starting_bet_for_new_strategy(self):
        """When switching strategy: first bet uses last_loss_for_switch if available"""
        return self.last_loss_for_switch if (self.last_loss_for_switch and self.last_loss_for_switch > 0) else self.base_bet

    # ---------- strategy implementations ----------
    def strat_martingale_next(self, won):
        if won:
            return self.base_bet
        return round(max(self.current_bet * self.multiplier, self.base_bet), 8)

    def strat_fibonacci_next(self, won):
        if won:
            self.fibo_seq = [self.base_bet, self.base_bet]
            self.fibo_index = 0
            return self.base_bet
        # advance index and ensure seq
        self.fibo_index += 1
        if self.fibo_index >= len(self.fibo_seq):
            self.fibo_seq.append(self.fibo_seq[-1] + self.fibo_seq[-2])
        return round(self.fibo_seq[self.fibo_index], 8)

    def strat_flat_next(self, won):
        return self.base_bet

    def strat_jackpot_hunter_next(self, won):
        # small raise (2-5%) relative to last bet
        if won:
            return self.base_bet
        ref = self.last_bet_amount if self.last_bet_amount > 0 else self.base_bet
        factor = random.uniform(self.jackpot_raise_min, self.jackpot_raise_max)
        return round(ref * factor, 8)

    def strat_high_risk_pulse_next(self, won):
        if won:
            return self.base_bet
        ref = self.last_bet_amount if self.last_bet_amount > 0 else self.base_bet
        factor = random.uniform(self.high_risk_raise_min, self.high_risk_raise_max)
        return round(ref * factor, 8)

    def strat_randomized_next(self, won):
        if won:
            return self.base_bet
        if self.randomized_mode == "multiplier":
            ref = self.last_bet_amount if self.last_bet_amount > 0 else self.base_bet
            factor = random.uniform(self.randomized_min_mult, self.randomized_max_mult)
            return round(ref * factor, 8)
        else:
            upper = max(self.last_loss_amount, self.base_bet)
            return round(random.uniform(self.base_bet, upper), 8)

    # ---------- UI helpers ----------
    def _summary_panel(self, start_balance, current_balance, total_bets, win, lose, runtime):
        txt = f"""
[bold yellow]🏦 Baki Awal :[/bold yellow] {start_balance:.8f} {self.currency.upper()}
[bold cyan]💱 Baki Sekarang:[/bold cyan] {current_balance:.8f} {self.currency.upper()}
[bold green]🏧 Profit/Rugi:[/bold green] {self.session_profit:.8f} {self.currency.upper()}
[bold magenta]🔄 Jumlah BET :[/bold magenta] {total_bets} (WIN {win} / LOSE {lose})
[bold white]⏰ Runtime :[/bold white] {runtime}
[bold red]🚦 Session :[/bold red] {self.session_count}
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
        tbl = Table.grid(expand=True)
        tbl.add_column("k", ratio=2)
        tbl.add_column("v", ratio=4)
        tbl.add_row("[yellow]BetSpeed[/yellow]", f"[magenta]{speed} bets/sec[/magenta]")
        tbl.add_row("[yellow]Mode[/yellow]", f"[cyan bold]{self.current_strategy.upper()}[/cyan bold]")
        return Panel(tbl, title="[ GUNA VPS UNTUK + SPEED ]", border_style="green")

    def _update_ui(self, start_balance, current_balance, total_bets, win, lose, live):
        elapsed = int(time.time() - self.start_time)
        runtime_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
        layout = Layout()
        layout.split(
            Layout(name="summary", size=11),
            Layout(name="bets", ratio=3),
            Layout(name="speed", size=6)
        )
        layout["summary"].update(self._summary_panel(start_balance, current_balance, total_bets, win, lose, runtime_str))
        layout["bets"].update(self._bet_table())
        layout["speed"].update(self._speed_panel(total_bets))
        live.update(layout)

    def draw_logo(self):
        # small colored ASCII logo (ANSI may not render in all consoles)
        try:
            gradient = ["\033[91m", "\033[93m", "\033[92m", "\033[96m", "\033[94m", "\033[95m"]
            logo = "W O L F  D I C E  B O T"
            for i, c in enumerate(logo):
                print(f"{gradient[i % len(gradient)]}{c}\033[0m", end="")
            print("\n")
            print("🎲🐺  🎲🐺  🎲🐺  🎲🐺  🎲🐺\n")
        except Exception:
            console.print("[bold cyan]WOLF DICE BOT[/bold cyan]\n")

    # ---------- strategy switching ----------
    def switch_to_next_strategy(self, reason="auto"):
        if not self.strategy_cycle:
            return
        old = self.current_strategy
        self.strategy_index = (self.strategy_index + 1) % len(self.strategy_cycle)
        self.current_strategy = self.strategy_cycle[self.strategy_index]
        # set first bet for new strategy to preserved last loss (for switch) if available
        self.current_bet = self.get_starting_bet_for_new_strategy()
        console.print(f"[cyan]🔁 Strategy switched ({reason}): {old} -> {self.current_strategy}[/cyan]")

    # ---------- main loop ----------
    def run(self):
        self.session_count += 1
        console.clear()
        self.draw_logo()
        start_balance = self.get_balance_currency(self.currency)
        if start_balance is None:
            console.print("[red]❌ Gagal dapatkan baki - semak token/endpoint[/red]")
            return

        # reset runtime state
        self.session_profit = 0.0
        self.current_bet = self.base_bet
        self.last_bet_amount = 0.0
        self.last_loss_amount = 0.0
        self.last_loss_for_switch = 0.0
        self.last_outcome = None
        self.total_bets = 0
        self.win_count = 0
        self.lose_count = 0
        self.loss_streak_count = 0
        self.start_time = time.time()
        self.bet_history = []

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

                # enforce max_bet if >0
                if self.max_bet and self.max_bet > 0 and self.current_bet > self.max_bet:
                    self.current_bet = self.max_bet

                # prepare override chance (strategies may set)
                override_chance = None

                # compute rule & threshold (strategy funcs may set override_chance before next iter)
                rule, bet_value = self.chance_to_rule_and_threshold(override_chance)

                # place bet
                resp = self.place_dice_bet(amount=self.current_bet, rule=rule, bet_value=bet_value)
                if not resp or not resp.get("bet"):
                    time.sleep(self.cooldown)
                    continue

                bet = resp["bet"]
                state = bet.get("state")   # "win" or "lose"
                profit = float(bet.get("profit", 0) or 0)
                amount = float(bet.get("amount", self.current_bet))
                result_value = str(bet.get("result_value", ""))
                self.total_bets += 1
                self.last_bet_amount = amount
                self.last_outcome = state

                # WIN handling: reset to base_bet for continuing same strategy
                if state == "win":
                    self.session_profit += profit
                    self.win_count += 1
                    self.loss_streak_count = 0
                    # DO NOT clear last_loss_for_switch here (preserve for switches)
                    display_profit = f"[bold green]{profit:.8f}[/bold green]"
                    # reset strategy-related counters for continuation
                    self.current_bet = self.base_bet
                    self.fibo_seq = [self.base_bet, self.base_bet]
                    self.fibo_index = 0

                else:
                    # LOSS handling
                    loss_amount = amount
                    self.session_profit -= loss_amount
                    self.lose_count += 1
                    self.loss_streak_count += 1
                    self.last_loss_amount = loss_amount
                    # preserve for future strategy switches
                    self.last_loss_for_switch = loss_amount
                    display_profit = f"[red]{-loss_amount:.8f}[/red]"

                    # compute next bet per active strategy (for continuation)
                    if self.current_strategy == "martingale":
                        self.current_bet = self.strat_martingale_next(False)
                    elif self.current_strategy == "fibonacci":
                        self.current_bet = self.strat_fibonacci_next(False)
                    elif self.current_strategy == "flat":
                        self.current_bet = self.strat_flat_next(False)
                    elif self.current_strategy == "jackpot_hunter":
                        self.current_bet = self.strat_jackpot_hunter_next(False)
                    elif self.current_strategy == "high_risk_pulse":
                        # occasional pulse: moderate doubling every interval
                        if self.total_bets > 0 and (self.total_bets % self.high_risk_interval) == 0:
                            ref = self.last_loss_amount if self.last_loss_amount > 0 else self.base_bet
                            self.current_bet = round(ref * 2.0, 8)
                        else:
                            self.current_bet = self.strat_high_risk_pulse_next(False)
                    elif self.current_strategy == "randomized":
                        self.current_bet = self.strat_randomized_next(False)
                    else:
                        self.current_bet = self.base_bet

                # add to history (show next bet)
                arrow = "↑" if rule == "over" else "↓"
                wl = "[bold green]WIN[/bold green]" if state == "win" else "[red]LOSE[/red]"
                self.bet_history.append([
                    f"{bet_value:.2f}[cyan]{arrow}[/cyan]",
                    result_value,
                    f"{self.current_bet:.8f}",
                    wl,
                    display_profit
                ])

                # auto-switching
                if self.auto_strategy_change and self.strategy_cycle:
                    if self.strategy_switch_mode == "on_win" and state == "win":
                        # switch and because last was win -> indicate win_last True so new strategy starts from base_bet
                        self.switch_to_next_strategy(reason="on_win")
                    elif self.strategy_switch_mode == "on_loss_streak" and self.loss_streak_count >= self.loss_streak_trigger:
                        # switch and because triggered by losses -> new strategy first bet should attempt recover using last_loss_for_switch
                        # set current_bet using last_loss_for_switch inside switch_to_next_strategy
                        self.switch_to_next_strategy(reason="on_loss_streak")
                        self.loss_streak_count = 0

                # update UI
                self._update_ui(start_balance, start_balance + self.session_profit, self.total_bets, self.win_count, self.lose_count, live)
                time.sleep(self.cooldown)

        # final summary
        final_runtime = time.strftime("%H:%M:%S", time.gmtime(int(time.time() - self.start_time)))
        console.print(self._summary_panel(start_balance, start_balance + self.session_profit, self.total_bets, self.win_count, self.lose_count))


if __name__ == "__main__":
    bot = WolfBetBot("config.json")
    while True:
        bot.run()
        if not bot.auto_start:
            break
        console.print(f"\n[cyan]🔄 Auto-restart in {bot.auto_start_delay} seconds...[/cyan]")
        time.sleep(bot.auto_start_delay)
