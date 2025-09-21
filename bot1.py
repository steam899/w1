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

        # core settings
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

        # strategy settings
        self.current_strategy = self.cfg.get("strategy", "martingale")
        self.auto_strategy_change = bool(self.cfg.get("auto_strategy_change", True))
        self.strategy_cycle = self.cfg.get("strategy_cycle", ["martingale"])
        self.strategy_switch_mode = self.cfg.get("strategy_switch_mode", "on_win")
        self.loss_streak_trigger = int(self.cfg.get("loss_streak_trigger", 5))
        # optional strategy params
        self.jackpot_chance = float(self.cfg.get("jackpot_chance", 1.0))  # percent
        self.high_risk_interval = int(self.cfg.get("high_risk_interval", 10))

        # runtime state
        self.session_profit = 0.0
        self.current_bet = self.base_bet
        self.bet_history = []
        self.start_time = None
        self.loss_streak_total = 0.0
        self.session_count = 0
        self.last_loss_amount = self.base_bet  # used by randomized strategy as "last lost"

        # fibonacci seq
        self.fibo_seq = [self.base_bet]
        self.total_bets = 0

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

    # -------------- Dice helpers --------------
    @staticmethod
    def _cap(val, lo, hi):
        return max(lo, min(hi, val))

    def chance_to_rule_and_threshold(self, chance_override=None):
        # allow per-bet chance override (used by jackpot hunter etc.)
        chance = self._cap(chance_override if chance_override is not None else self.chance, 0.01, 99.99)
        if self.rule_mode == "over":
            rule = "over"
            bet_value = self._cap(100.0 - chance, 0.01, 99.99)
        elif self.rule_mode == "under":
            rule = "under"
            bet_value = self._cap(chance, 0.01, 99.99)
        else:
            if random.randint(0, 1) == 1:
                rule = "under"
                bet_value = self._cap(chance, 0.01, 99.99)
            else:
                rule = "over"
                bet_value = self._cap(100.0 - chance, 0.01, 99.99)
        return rule, bet_value

    # -------------- Strategy Implementations --------------
    def _martingale_strategy(self, win, last_bet):
        return self.base_bet if win else round(last_bet * self.multiplier_factor, 12)

    def _fibonacci_strategy(self, win):
        if win:
            self.fibo_seq = [self.base_bet]
        else:
            if len(self.fibo_seq) < 2:
                self.fibo_seq.append(self.base_bet)
            else:
                self.fibo_seq.append(self.fibo_seq[-1] + self.fibo_seq[-2])
        return self.fibo_seq[-1]

    def _dalembert_strategy(self, win, last_bet):
        if win:
            new_bet = max(self.base_bet, last_bet - self.base_bet)
        else:
            new_bet = last_bet + self.base_bet
        return round(new_bet, 12)

    def _flat_strategy(self, win, last_bet):
        return self.base_bet

    def _jackpot_hunter_strategy(self):
        # always small base bet; caller will request chance_override for jackpot
        return self.base_bet

    def _high_risk_pulse_strategy(self):
        # every N bets place a larger pulse
        if self.total_bets > 0 and self.total_bets % self.high_risk_interval == 0:
            return min(self.max_bet, self.base_bet * 50)
        return self.base_bet

    def _randomized_strategy(self):
        # Use last single loss as the upper bound for randomization (user requested)
        upper = max(self.last_loss_amount, self.base_bet)
        # ensure not negative
        upper = max(upper, self.base_bet)
        amount = round(random.uniform(self.base_bet, upper), 8)
        return amount

    # -------------- UI helpers --------------
    def _summary_panel(self, start_balance, current_balance, total_bets, win, lose, runtime):
        txt = f"""
[bold yellow]🏦Baki Awal :[/bold yellow] {start_balance:.8f} {self.currency.upper()}
[bold cyan]💱Baki Sekarang:[/bold cyan] {current_balance:.8f} {self.currency.upper()}
[bold green]🏧Profit/Rugi:[/bold green] {self.session_profit:.8f} {self.currency.upper()}
[bold magenta]🔄Jumlah BET :[/bold magenta] {total_bets} (WIN {win} / LOSE {lose})
[bold white]⏰Runtime :[/bold white] {runtime}
[bold red]🚦Session :[/bold red] {self.session_count}
[bold blue]🎯Strategy :[/bold blue] {self.current_strategy}
[bold red]⚠ Last loss:[/bold red] {self.last_loss_amount:.8f}
"""
        return Panel(txt, title="📊 Ringkasan Sesi", border_style="bold blue")

    def _bet_table(self):
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Target")
        table.add_column("Result")
        table.add_column("Bet Session")
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
        layout["summary"].update(
            self._summary_panel(start_balance, current_balance, total_bets, win, lose, runtime_str)
        )
        layout["bets"].update(self._bet_table())
        layout["speed"].update(self._speed_panel(total_bets))

        live.update(layout)

    # -------------- Logo --------------
    def draw_logo(self):
        logo_text = "W O L F 🍀 D I C E 🍀 B O T"
        for i, c in enumerate(logo_text):
            color = GRADIENT[i % len(GRADIENT)]
            print(f"{color}{c}{RESET}", end="")
        print("\n")
        emoji_line = "🎲🐺  🎲🐺  🎲🐺  🎲🐺  🎲🐺"
        print(emoji_line, "\n")

    # -------------- Main Loop --------------
    def main_loop(self):
        self.draw_logo()
        start_balance = self.get_balance_currency(self.currency)
        if start_balance is None:
            console.print(f"[red]❌ Tak dapat baca balance. Semak token/endpoint atau headers.[/red]")
            return
        console.print(f"[green]💰 Baki awal:[/green] {start_balance:.8f} {self.currency.upper()}")

        self.session_profit = 0.0
        self.current_bet = self.base_bet
        win_count, lose_count = 0, 0
        self.start_time = time.time()
        self.loss_streak_total = 0.0
        self.last_loss_amount = self.base_bet
        self.total_bets = 0

        with Live(refresh_per_second=4, screen=True) as live:
            while True:
                # stop conditions
                if self.session_profit <= self.stop_loss:
                    console.print(f"\n[yellow]🛑 Stop-loss triggered:[/yellow] {self.session_profit:.8f} {self.currency.upper()}")
                    break
                if self.session_profit >= self.take_profit:
                    console.print(f"\n[green]✅ Take-profit triggered:[/green] {self.session_profit:.8f} {self.currency.upper()}")
                    break

                # ensure bet cap
                if self.current_bet > self.max_bet:
                    console.print(f"\n[cyan]⚠️ current_bet above max_bet - capping to max_bet.[/cyan]")
                    self.current_bet = self.max_bet

                # choose per-bet chance override for certain strategies
                chance_override = None
                if self.current_strategy == "jackpot_hunter":
                    chance_override = self.jackpot_chance

                rule, bet_value = self.chance_to_rule_and_threshold(chance_override)

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

                if state == "win":
                    self.session_profit += profit
                    win_count += 1
                    outcome = "[bold green]WIN[/bold green]"
                    display_profit = f"[bold green]{profit:.8f}[/bold green]"

                    # reset loss trackers
                    self.loss_streak_total = 0.0
                    self.last_loss_amount = self.base_bet

                    # after win, apply strategy's win behavior
                    if self.current_strategy == "martingale":
                        self.current_bet = self._martingale_strategy(True, self.current_bet)
                    elif self.current_strategy == "fibonacci":
                        self.current_bet = self._fibonacci_strategy(True)
                    elif self.current_strategy == "dalembert":
                        self.current_bet = self._dalembert_strategy(True, self.current_bet)
                    elif self.current_strategy == "flat":
                        self.current_bet = self._flat_strategy(True, self.current_bet)
                    elif self.current_strategy == "jackpot_hunter":
                        self.current_bet = self._jackpot_hunter_strategy()
                    elif self.current_strategy == "high_risk_pulse":
                        self.current_bet = self._high_risk_pulse_strategy()
                    elif self.current_strategy == "randomized":
                        self.current_bet = self._randomized_strategy()

                    # auto-switch on win (if configured)
                    if self.auto_strategy_change and self.strategy_switch_mode == "on_win":
                        old = self.current_strategy
                        self.current_strategy = random.choice(self.strategy_cycle)
                        console.print(f"[yellow]🔄 Strategy changed on win: {old} -> {self.current_strategy}[/yellow]")

                else:
                    # loss handling
                    loss_amount = float(bet.get("amount", self.current_bet))
                    self.session_profit -= loss_amount
                    lose_count += 1
                    outcome = "[red]LOSE[/red]"

                    # update loss trackers
                    self.loss_streak_total += loss_amount
                    self.last_loss_amount = loss_amount
                    display_profit = f"[red]{-self.loss_streak_total:.8f}[/red]"

                    # auto-switch on loss streak
                    if self.auto_strategy_change and self.strategy_switch_mode == "on_loss_streak":
                        if lose_count % self.loss_streak_trigger == 0:
                            old = self.current_strategy
                            self.current_strategy = random.choice(self.strategy_cycle)
                            console.print(f"[red]🔄 Strategy changed on loss streak: {old} -> {self.current_strategy}[/red]")

                    # determine next bet according to strategy after a loss
                    if self.current_strategy == "martingale":
                        next_bet = self._martingale_strategy(False, self.current_bet)
                    elif self.current_strategy == "fibonacci":
                        next_bet = self._fibonacci_strategy(False)
                    elif self.current_strategy == "dalembert":
                        next_bet = self._dalembert_strategy(False, self.current_bet)
                    elif self.current_strategy == "flat":
                        next_bet = self._flat_strategy(False, self.current_bet)
                    elif self.current_strategy == "jackpot_hunter":
                        next_bet = self._jackpot_hunter_strategy()
                    elif self.current_strategy == "high_risk_pulse":
                        next_bet = self._high_risk_pulse_strategy()
                    elif self.current_strategy == "randomized":
                        # randomized uses last lost (user requested) as upper bound
                        next_bet = self._randomized_strategy()
                    else:
                        next_bet = self.base_bet

                    # enforce cover-loss: ensure next bet tries to cover cumulative loss + base profit
                    cover_needed = abs(self.loss_streak_total) + self.base_bet
                    if next_bet < cover_needed:
                        next_bet = cover_needed

                    # If cover_needed > max_bet, warn and cap to max_bet
                    if next_bet > self.max_bet:
                        console.print(f"[magenta]⚠️ Required cover {next_bet:.8f} exceeds max_bet {self.max_bet:.8f}. Capping to max_bet[/magenta]")
                        next_bet = self.max_bet

                    self.current_bet = round(next_bet, 8)

                # store history
                arrow = "↑" if rule == "over" else "↓"
                self.bet_history.append([
                    f"{bet_value:.2f}[cyan]{arrow}[/cyan]",
                    result_value,
                    f"{self.current_bet:.8f}",
                    outcome,
                    display_profit
                ])

                current_balance = start_balance + self.session_profit
                self._update_ui(start_balance, current_balance, self.total_bets, win_count, lose_count, live)

                time.sleep(self.cooldown)

        # final summary
        final_runtime = time.strftime("%H:%M:%S", time.gmtime(int(time.time() - self.start_time)))
        final_panel = self._summary_panel(start_balance, start_balance + self.session_profit, self.total_bets, win_count, lose_count, final_runtime)
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
