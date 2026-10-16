#!/usr/bin/env python3
"""Veille de prix Kraken pour Debian.

Lit les cours publics via Kraken CLI et envoie des notifications XFCE.
Aucun ordre n'est passé : les lectures long/short sont des règles mécaniques,
pas un conseil d'investissement.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

APP = "Kraken alertes"
LOG = logging.getLogger("kraken-alerts")

HOME = Path.home()
CONFIG_PATH = Path(
    os.environ.get("KRAKEN_ALERTS_CONFIG", HOME / ".config/kraken-alerts/config.toml")
)
STATE_PATH = Path(
    os.environ.get(
        "KRAKEN_ALERTS_STATE", HOME / ".local/state/kraken-alerts/state.json"
    )
)
HISTORY_PATH = STATE_PATH.with_name("alerts.log")
SNAPSHOT_PATH = STATE_PATH.with_name("snapshot.json")
UNREAD_PATH = STATE_PATH.with_name("unread")
FUTURES_CHARTS = "https://futures.kraken.com/api/charts/v1/trade"

RULES = """\
Règles utilisées (mécaniques, pas un conseil)

Prix
  Alerte si le cours s'écarte du dernier point signalé de plus que le seuil
  du pair (price_move_pct dans la config).
  Un seuil below ou above ne part qu'une fois, au franchissement.

Heures silencieuses
  Entre quiet_start et quiet_end, les bulles sont retenues.
  À digest_hour, une seule notification résume la nuit et les cours.

Court terme — chandeliers de 15 minutes
  RSI 14, moyenne mobile 8 et moyenne mobile 21, sur les bougies clôturées.
  LONG  si MM8 > MM21 et RSI >= 55
  SHORT si MM8 < MM21 et RSI <= 45
  neutre sinon

Long terme — chandeliers journaliers
  Moyenne mobile 20 et moyenne mobile 50, comparées au prix actuel.
  LONG  si prix > MM20 et MM20 > MM50
  SHORT si prix < MM20 et MM20 < MM50
  neutre sinon

Lecture
  Les deux horizons alignés : biais dans le même sens.
  Court terme contre long terme : repli ou rebond dans la tendance de fond.

Aucun ordre spot, marge ou futures n'est envoyé.
"""


@dataclass
class PairConfig:
    symbol: str
    label: str
    asset_class: str | None = None
    market: str | None = None
    engine: str = "spot"
    price_move_pct: float = 1.5
    below: list[float] = field(default_factory=list)
    above: list[float] = field(default_factory=list)


def market_name(pair: PairConfig) -> str:
    if pair.market:
        return pair.market
    if pair.engine == "futures":
        return "Futures"
    return {
        "tokenized_asset": "Action tokenisée",
        "forex": "Forex",
        "futures": "Futures",
    }.get(pair.asset_class or "", "Spot")


@dataclass
class Settings:
    kraken_bin: str
    poll_seconds: int = 60
    short_refresh_seconds: int = 300
    long_refresh_seconds: int = 1800
    urgent_move_pct: float = 4.0
    price_cooldown_seconds: int = 1200
    bias_cooldown_seconds: int = 7200
    quiet_start: int | None = 23 * 60
    quiet_end: int | None = 8 * 60
    digest_minute: int | None = 8 * 60
    pairs: list[PairConfig] = field(default_factory=list)


@dataclass
class Bias:
    side: str
    detail: str
    rsi: float | None = None
    fast: float | None = None
    slow: float | None = None


def default_config_text() -> str:
    return """\
# Veille Kraken. Modifie les pairs, puis : systemctl --user restart kraken-alerts
# Données publiques uniquement. Aucun ordre n'est passé.

poll_seconds = 60
short_refresh_seconds = 300
long_refresh_seconds = 1800
urgent_move_pct = 4.0
price_cooldown_seconds = 1200
bias_cooldown_seconds = 7200
quiet_start = "23:00"
quiet_end = "08:00"
digest_hour = "08:00"

# market : Spot, Futures, Forex, Action tokenisée
# engine : spot (défaut) ou futures
# below / above : seuil franchi une seule fois

[[pair]]
symbol = "BTCUSD"
label = "BTC"
market = "Spot"
price_move_pct = 1.2
below = [70000]

[[pair]]
symbol = "ETHUSD"
label = "ETH"
market = "Spot"
price_move_pct = 1.5

[[pair]]
symbol = "SOLUSD"
label = "SOL"
market = "Spot"
price_move_pct = 2.0
above = [120]

[[pair]]
symbol = "PF_XBTUSD"
label = "BTC perp"
market = "Futures"
engine = "futures"
price_move_pct = 1.2

[[pair]]
symbol = "AAPLx/USD"
label = "AAPL"
market = "Action tokenisée"
asset_class = "tokenized_asset"
price_move_pct = 1.5
"""


def clock_minutes(value: str | None, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    hour, minute = str(value).split(":", 1)
    return int(hour) * 60 + int(minute)


def number_list(value) -> list[float]:
    if value is None or value == "":
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, str):
        parts = value.replace(";", ",").split(",")
        return [float(part) for part in parts if part.strip()]
    return [float(item) for item in value]


def keep_symbol(value: str) -> str:
    text = value.strip()
    if any(char.islower() for char in text):
        return text
    return text.upper()


def pair_engine(item: dict, symbol: str) -> str:
    engine = str(item.get("engine") or "").lower()
    if engine in {"futures", "spot"}:
        return engine
    market = str(item.get("market") or "").lower()
    if market == "futures" or symbol.startswith(("PF_", "PI_", "FI_")):
        return "futures"
    return "spot"


def load_settings() -> Settings:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(default_config_text(), encoding="utf-8")
    with CONFIG_PATH.open("rb") as fh:
        raw = tomllib.load(fh)
    kraken = raw.get("kraken_bin") or shutil.which("kraken") or str(HOME / ".cargo/bin/kraken")
    pairs = []
    for item in raw.get("pair", []):
        symbol = keep_symbol(str(item["symbol"]))
        pairs.append(
            PairConfig(
                symbol=symbol,
                label=str(item.get("label") or symbol),
                asset_class=item.get("asset_class") or None,
                market=str(item["market"]) if item.get("market") else None,
                engine=pair_engine(item, symbol),
                price_move_pct=float(item.get("price_move_pct", raw.get("price_move_pct", 1.5))),
                below=number_list(item.get("below")),
                above=number_list(item.get("above")),
            )
        )
    if not pairs:
        raise SystemExit(f"Aucun pair dans {CONFIG_PATH}")
    return Settings(
        kraken_bin=kraken,
        poll_seconds=int(raw.get("poll_seconds", 60)),
        short_refresh_seconds=int(raw.get("short_refresh_seconds", 300)),
        long_refresh_seconds=int(raw.get("long_refresh_seconds", 1800)),
        urgent_move_pct=float(raw.get("urgent_move_pct", 4.0)),
        price_cooldown_seconds=int(raw.get("price_cooldown_seconds", 1200)),
        bias_cooldown_seconds=int(raw.get("bias_cooldown_seconds", 7200)),
        quiet_start=clock_minutes(raw.get("quiet_start"), 23 * 60),
        quiet_end=clock_minutes(raw.get("quiet_end"), 8 * 60),
        digest_minute=clock_minutes(raw.get("digest_hour"), 8 * 60),
        pairs=pairs,
    )


def minutes_now(moment: datetime | None = None) -> int:
    moment = moment or datetime.now()
    return moment.hour * 60 + moment.minute


def in_quiet(settings: Settings, moment: datetime | None = None) -> bool:
    start = settings.quiet_start
    end = settings.quiet_end
    if start is None or end is None or start == end:
        return False
    now = minutes_now(moment)
    if start < end:
        return start <= now < end
    return now >= start or now < end


def clock_label(minutes: int | None) -> str:
    if minutes is None:
        return ""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def kraken_json(settings: Settings, args: list[str], timeout: int = 40) -> dict:
    cmd = [settings.kraken_bin, *args, "-o", "json", "--log-format", "compact"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = err[-1] if err else f"code {proc.returncode}"
        raise RuntimeError(detail)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON illisible ({exc})") from exc
    if not isinstance(data, dict):
        raise RuntimeError("réponse inattendue")
    return data


def fetch_ticker(settings: Settings, pair: PairConfig) -> dict:
    if pair.engine == "futures":
        return fetch_futures_ticker(settings, pair)
    args = ["ticker", pair.symbol]
    if pair.asset_class:
        args += ["--asset-class", pair.asset_class]
    data = kraken_json(settings, args)
    if not data:
        raise RuntimeError("ticker vide")
    return next(iter(data.values()))


def fetch_futures_ticker(settings: Settings, pair: PairConfig) -> dict:
    data = kraken_json(settings, ["futures", "ticker", pair.symbol])
    ticker = data.get("ticker") or {}
    if "last" not in ticker:
        raise RuntimeError("ticker futures vide")
    return {
        "last_price": float(ticker["last"]),
        "open": float(ticker.get("open24h") or 0),
        "high_24h": float(ticker.get("high24h") or 0),
        "low_24h": float(ticker.get("low24h") or 0),
    }


def fetch_ohlc(settings: Settings, pair: PairConfig, interval: int) -> list[dict]:
    if pair.engine == "futures":
        mapped = {15: "15m", 60: "1h", 1440: "1d"}.get(interval, "15m")
        return fetch_futures_candles(pair.symbol, mapped)
    args = ["ohlc", pair.symbol, "--interval", str(interval)]
    if pair.asset_class:
        args += ["--asset-class", pair.asset_class]
    data = kraken_json(settings, args)
    candles = list(data.get("candles") or [])
    if len(candles) >= 2:
        candles = candles[:-1]
    return candles


def fetch_futures_candles(symbol: str, interval: str) -> list[dict]:
    request = urllib.request.Request(
        f"{FUTURES_CHARTS}/{symbol}/{interval}",
        headers={"User-Agent": "kraken-alerts"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"chandeliers futures indisponibles ({exc})") from exc
    candles = []
    for candle in data.get("candles") or []:
        candles.append(
            {
                "time": int(candle["time"]) // 1000,
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": float(candle["volume"]),
            }
        )
    if len(candles) >= 2:
        candles = candles[:-1]
    return candles


def sparkline(candles: list[dict]) -> list[float]:
    closes = [float(candle["close"]) for candle in candles]
    window = closes[-96:]
    if len(window) < 2:
        return window
    step = max(1, len(window) // 24)
    return window[::step][-24:]


def sma(values: list[float], length: int) -> float | None:
    if len(values) < length:
        return None
    window = values[-length:]
    return sum(window) / length


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def short_bias(candles: list[dict]) -> Bias:
    closes = [float(c["close"]) for c in candles]
    fast = sma(closes, 8)
    slow = sma(closes, 21)
    value = rsi(closes, 14)
    if fast is None or slow is None or value is None:
        return Bias("indisponible", "pas assez de bougies 15 min")
    if fast > slow and value >= 55:
        side = "long"
    elif fast < slow and value <= 45:
        side = "short"
    else:
        side = "neutre"
    relation = "au-dessus" if fast > slow else "en-dessous" if fast < slow else "sur"
    detail = f"RSI {value:.0f}, MM8 {relation} de MM21"
    return Bias(side, detail, rsi=value, fast=fast, slow=slow)


def long_bias(candles: list[dict], price: float) -> Bias:
    closes = [float(c["close"]) for c in candles]
    fast = sma(closes, 20)
    slow = sma(closes, 50)
    if fast is None or slow is None:
        return Bias("indisponible", "pas assez de bougies journalières")
    if price > fast and fast > slow:
        side = "long"
    elif price < fast and fast < slow:
        side = "short"
    else:
        side = "neutre"
    detail = "prix face aux MM20 et MM50 journalières"
    return Bias(side, detail, fast=fast, slow=slow)


def lecture(short: str, long: str) -> str:
    if short == "indisponible" or long == "indisponible":
        return "Lecture incomplète, données encore insuffisantes."
    if short == "long" and long == "long":
        return "Court et long terme alignés : biais haussier."
    if short == "short" and long == "short":
        return "Court et long terme alignés : biais baissier."
    if short == "short" and long == "long":
        return "Repli court terme dans une tendance longue haussière."
    if short == "long" and long == "short":
        return "Rebond court terme dans une tendance longue baissière."
    if long == "long":
        return "Tendance longue haussière, court terme sans direction nette."
    if long == "short":
        return "Tendance longue baissière, court terme sans direction nette."
    if short == "long":
        return "Poussée court terme, tendance longue encore neutre."
    if short == "short":
        return "Pression court terme, tendance longue encore neutre."
    return "Pas de direction nette sur les deux horizons."


_QUOTE_MARKS = (
    ("USDT", "$"),
    ("USDC", "$"),
    ("USD", "$"),
    ("EUR", "€"),
    ("GBP", "£"),
    ("JPY", "¥"),
    ("CHF", "CHF"),
    ("CAD", "CA$"),
    ("AUD", "A$"),
)


def quote_mark(symbol: str) -> str:
    text = symbol.upper()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    for code, mark in _QUOTE_MARKS:
        if text.endswith(code):
            return mark
    return text[-3:] if len(text) >= 3 else text


def money(value: float, symbol: str) -> str:
    return f"{fmt_price(value)} {quote_mark(symbol)}"


def fmt_price(value: float) -> str:
    if value >= 10000:
        text = f"{value:,.0f}"
    elif value >= 1:
        text = f"{value:,.2f}"
    else:
        text = f"{value:,.4f}"
    return text.replace(",", " ").replace(".", ",")


def fmt_pct(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}".replace(".", ",") + " %"


def change_pct(price: float, open_price: float) -> float | None:
    if not open_price:
        return None
    return (price - open_price) / open_price * 100.0


def side_label(side: str) -> str:
    return {"long": "LONG", "short": "SHORT", "neutre": "neutre"}.get(side, side)


def mark_unread() -> None:
    UNREAD_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNREAD_PATH.write_text(str(time.time()), encoding="utf-8")


def clear_unread() -> None:
    UNREAD_PATH.unlink(missing_ok=True)


def notify(
    title: str,
    body: str,
    urgency: str = "normal",
    timeout_ms: int = 14000,
    hold: bool = False,
    state: dict | None = None,
) -> None:
    line = f"{title} — {body.replace(chr(10), ' | ')}"
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + line + "\n")
    LOG.info("%s", line)
    mark_unread()
    if hold:
        if state is not None:
            pending = state.setdefault("pending", [])
            pending.append({"title": title, "at": time.time()})
            del pending[:-40]
        LOG.info("notification retenue (heures silencieuses)")
        return
    cmd = [
        "notify-send",
        "-a",
        APP,
        "-u",
        urgency,
        "-t",
        str(timeout_ms),
        "-i",
        "dialog-information",
        title,
        body,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        LOG.warning("notify-send a échoué : %s", (proc.stderr or "").strip())


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"pairs": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"pairs": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def pair_state(state: dict, symbol: str) -> dict:
    return state.setdefault("pairs", {}).setdefault(symbol, {})


class Watcher:
    def __init__(self, settings: Settings, state: dict, announce: bool):
        self.settings = settings
        self.state = state
        self.announce = announce
        self.started = False
        self.last_short = 0.0
        self.last_long = 0.0
        self.cache: dict[str, dict] = {}
        self.kraken_down_since: float | None = None
        self.last_down_alert = float(state.get("kraken_down_alert") or 0)

    def cycle(self, force_indicators: bool = False) -> list[str]:
        now = time.time()
        do_short = force_indicators or now - self.last_short >= self.settings.short_refresh_seconds
        do_long = force_indicators or now - self.last_long >= self.settings.long_refresh_seconds
        lines: list[str] = []
        failures = 0
        snapshots = []
        for pair in self.settings.pairs:
            try:
                snap = self._read_pair(pair, do_short, do_long)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                failures += 1
                LOG.warning("%s : %s", pair.label, exc)
                lines.append(f"{pair.label} : erreur ({exc})")
                continue
            snapshots.append(snap)
            lines.append(self._format_line(snap))
        if failures == len(self.settings.pairs):
            self._kraken_down()
        else:
            self.kraken_down_since = None
        if do_short and failures < len(self.settings.pairs):
            self.last_short = now
        if do_long and failures < len(self.settings.pairs):
            self.last_long = now
        if not self.announce:
            write_snapshot(snapshots, self.state, self.settings)
            return lines
        if snapshots and not self.started:
            last = float(self.state.get("announced_at") or 0)
            if now - last > 600:
                self._summary(snapshots)
                self.state["announced_at"] = now
            for snap in snapshots:
                self._store_bias(snap)
            self.started = True
        for snap in snapshots:
            self._price_alert(snap)
            self._level_alert(snap)
            self._bias_alert(snap)
        self._maybe_digest(snapshots)
        write_snapshot(snapshots, self.state, self.settings)
        save_state(self.state)
        return lines

    def _read_pair(self, pair: PairConfig, do_short: bool, do_long: bool) -> dict:
        ticker = fetch_ticker(self.settings, pair)
        price = float(ticker["last_price"])
        open_price = float(ticker.get("open") or 0)
        cached = self.cache.setdefault(pair.symbol, {})
        if do_short or "short" not in cached:
            short_candles = fetch_ohlc(self.settings, pair, 15)
            cached["short"] = short_bias(short_candles)
            cached["spark"] = sparkline(short_candles)
        if do_long or "long" not in cached:
            cached["long"] = long_bias(fetch_ohlc(self.settings, pair, 1440), price)
        else:
            # Le prix a bougé : on recalcule le biais long avec les moyennes déjà connues.
            old = cached["long"]
            if old.fast is not None and old.slow is not None:
                cached["long"] = long_bias_from_averages(price, old.fast, old.slow)
        day = change_pct(price, open_price)
        return {
            "pair": pair,
            "price": price,
            "day": day,
            "high": float(ticker.get("high_24h") or 0),
            "low": float(ticker.get("low_24h") or 0),
            "short": cached["short"],
            "long": cached["long"],
            "spark": list(cached.get("spark") or []),
        }

    def _format_line(self, snap: dict) -> str:
        pair: PairConfig = snap["pair"]
        day = fmt_pct(snap["day"]) if snap["day"] is not None else "n/a"
        return (
            f"{pair.label} {money(snap['price'], pair.symbol)} · 24h {day} · "
            f"CT {side_label(snap['short'].side)} · LT {side_label(snap['long'].side)}"
        )

    def _emit(self, title: str, body: str, urgency: str = "normal", timeout_ms: int = 14000, hold: bool | None = None) -> None:
        if hold is None:
            hold = in_quiet(self.settings)
        notify(title, body, urgency=urgency, timeout_ms=timeout_ms, hold=hold, state=self.state)

    def _summary(self, snapshots: list[dict]) -> None:
        body = "\n".join(self._format_line(snap) for snap in snapshots)
        body += "\nAucun ordre n'est passé."
        self._emit("Veille démarrée", body, urgency="low", timeout_ms=12000)

    def _price_alert(self, snap: dict) -> None:
        pair: PairConfig = snap["pair"]
        slot = pair_state(self.state, pair.symbol)
        price = snap["price"]
        anchor = slot.get("anchor_price")
        now = time.time()
        if not anchor:
            slot["anchor_price"] = price
            return
        move = (price - float(anchor)) / float(anchor) * 100.0
        if abs(move) < pair.price_move_pct:
            return
        urgent = abs(move) >= self.settings.urgent_move_pct
        elapsed = now - float(slot.get("last_price_alert") or 0)
        if not urgent and elapsed < self.settings.price_cooldown_seconds:
            return
        day = f" · 24h {fmt_pct(snap['day'])}" if snap["day"] is not None else ""
        body = (
            f"{money(price, pair.symbol)} · {fmt_pct(move)} depuis la dernière alerte{day}\n"
            f"24h : {money(snap['low'], pair.symbol)} – {money(snap['high'], pair.symbol)}\n"
            f"Court terme {side_label(snap['short'].side)} · "
            f"long terme {side_label(snap['long'].side)}"
        )
        self._emit(
            f"{pair.label} · mouvement",
            body,
            urgency="critical" if urgent else "normal",
            timeout_ms=20000 if urgent else 12000,
        )
        slot["anchor_price"] = price
        slot["last_price_alert"] = now

    def _bias_alert(self, snap: dict) -> None:
        if not self.started:
            self._store_bias(snap)
            return
        pair: PairConfig = snap["pair"]
        slot = pair_state(self.state, pair.symbol)
        short = snap["short"].side
        long = snap["long"].side
        prev_short = slot.get("bias_short")
        prev_long = slot.get("bias_long")
        if prev_short is None and prev_long is None:
            self._store_bias(snap)
            return
        changed = (prev_short and prev_short != short) or (prev_long and prev_long != long)
        if not changed:
            return
        now = time.time()
        flip = {prev_short, short} == {"long", "short"} or {prev_long, long} == {"long", "short"}
        elapsed = now - float(slot.get("last_bias_alert") or 0)
        if not flip and elapsed < self.settings.bias_cooldown_seconds:
            return
        day = f" · 24h {fmt_pct(snap['day'])}" if snap["day"] is not None else ""
        body = (
            f"{money(snap['price'], pair.symbol)}{day}\n"
            f"Court terme : {side_label(short)} ({snap['short'].detail})\n"
            f"Long terme : {side_label(long)} ({snap['long'].detail})\n"
            f"{lecture(short, long)}"
        )
        self._emit(f"{pair.label} · lecture", body, urgency="normal", timeout_ms=16000)
        slot["last_bias_alert"] = now
        self._store_bias(snap)

    def _level_alert(self, snap: dict) -> None:
        pair: PairConfig = snap["pair"]
        price = float(snap["price"])
        slot = pair_state(self.state, pair.symbol)
        armed = slot.setdefault("armed_levels", {})
        fired = slot.setdefault("fired_levels", {})
        for kind, levels in (("below", pair.below), ("above", pair.above)):
            for level in levels:
                key = f"{kind}:{level:.8f}"
                crossed = price <= level if kind == "below" else price >= level
                if not crossed:
                    armed[key] = True
                    continue
                if not armed.get(key) or fired.get(key):
                    continue
                word = "sous" if kind == "below" else "au-dessus de"
                body = (
                    f"{money(price, pair.symbol)} a passé {word} {money(level, pair.symbol)}\n"
                    f"Seuil à usage unique. Change le montant dans la config pour le réarmer."
                )
                self._emit(f"{pair.label} · seuil", body, urgency="critical", timeout_ms=20000)
                fired[key] = time.time()
                armed[key] = False

    def _maybe_digest(self, snapshots: list[dict]) -> None:
        if self.settings.digest_minute is None or not snapshots:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        minute = minutes_now(now)
        target = self.settings.digest_minute
        last_day = self.state.get("clock_day")
        last_minute = self.state.get("clock_minute")
        crossed = False
        if last_day == today and last_minute is not None and last_minute < target <= minute:
            crossed = True
        elif last_day not in (None, today) and minute >= target:
            crossed = True
        self.state["clock_day"] = today
        self.state["clock_minute"] = minute
        if not crossed or self.state.get("digest_day") == today:
            return
        pending = list(self.state.get("pending") or [])
        lines = [self._format_line(snap) for snap in snapshots]
        body = "\n".join(lines)
        if pending:
            titles = "\n".join(f"· {item['title']}" for item in pending[-8:])
            body += f"\nCette nuit :\n{titles}"
        else:
            body += "\nRien d'urgent pendant les heures silencieuses."
        self._emit("Résumé du matin", body, urgency="normal", timeout_ms=20000, hold=False)
        self.state["pending"] = []
        self.state["digest_day"] = today

    def _store_bias(self, snap: dict) -> None:
        slot = pair_state(self.state, snap["pair"].symbol)
        slot["bias_short"] = snap["short"].side
        slot["bias_long"] = snap["long"].side
        slot["anchor_price"] = slot.get("anchor_price") or snap["price"]

    def _kraken_down(self) -> None:
        now = time.time()
        if self.kraken_down_since is None:
            self.kraken_down_since = now
        if now - self.kraken_down_since < 120:
            return
        if now - self.last_down_alert < 1800:
            return
        self._emit(
            "Kraken injoignable",
            "La veille n'arrive plus à lire les prix. Nouvel essai automatique.",
            urgency="normal",
        )
        self.last_down_alert = now
        self.state["kraken_down_alert"] = now


def long_bias_from_averages(price: float, fast: float, slow: float) -> Bias:
    if price > fast and fast > slow:
        side = "long"
    elif price < fast and fast < slow:
        side = "short"
    else:
        side = "neutre"
    return Bias(side, "prix face aux MM20 et MM50 journalières", fast=fast, slow=slow)


def write_snapshot(snapshots: list[dict], state: dict | None = None, settings: Settings | None = None) -> None:
    if not snapshots:
        return
    payload = {
        "updated": time.time(),
        "quiet": bool(settings and in_quiet(settings)),
        "quiet_end": clock_label(settings.quiet_end) if settings else "",
        "digest_hour": clock_label(settings.digest_minute) if settings else "",
        "pending": len((state or {}).get("pending") or []),
        "pairs": [public_row(snap, state or {}) for snap in snapshots],
    }
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SNAPSHOT_PATH)


def level_rows(pair: PairConfig, state: dict) -> list[dict]:
    slot = (state.get("pairs") or {}).get(pair.symbol) or {}
    armed = slot.get("armed_levels") or {}
    fired = slot.get("fired_levels") or {}
    rows = []
    for kind, levels in (("below", pair.below), ("above", pair.above)):
        for level in levels:
            key = f"{kind}:{level:.8f}"
            rows.append(
                {
                    "side": kind,
                    "price": fmt_price(level),
                    "armed": bool(armed.get(key)),
                    "fired": bool(fired.get(key)),
                }
            )
    return rows


def public_row(snap: dict, state: dict | None = None) -> dict:
    pair: PairConfig = snap["pair"]
    day = snap["day"]
    return {
        "symbol": pair.symbol,
        "label": pair.label,
        "market": market_name(pair),
        "unit": quote_mark(pair.symbol),
        "price": fmt_price(snap["price"]),
        "day": fmt_pct(day) if day is not None else "n/a",
        "day_value": day,
        "high": fmt_price(snap["high"]),
        "low": fmt_price(snap["low"]),
        "short": snap["short"].side,
        "long": snap["long"].side,
        "short_detail": snap["short"].detail,
        "long_detail": snap["long"].detail,
        "lecture": lecture(snap["short"].side, snap["long"].side),
        "spark": [float(value) for value in snap.get("spark") or []],
        "levels": level_rows(pair, state or {}),
    }


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_check(settings: Settings) -> int:
    watcher = Watcher(settings, load_state(), announce=False)
    print(f"Source : {settings.kraken_bin}")
    print(f"Config : {CONFIG_PATH}")
    for line in watcher.cycle(force_indicators=True):
        print(line)
    for pair in settings.pairs:
        cached = watcher.cache.get(pair.symbol)
        if not cached:
            continue
        print(f"  {pair.label} CT {cached['short'].detail}")
        print(f"  {pair.label} LT {cached['long'].detail}")
        print(f"  {pair.label} {lecture(cached['short'].side, cached['long'].side)}")
    return 0


def cmd_run(settings: Settings) -> int:
    LOG.info("veille démarrée (%d pairs, toutes les %ds)", len(settings.pairs), settings.poll_seconds)
    watcher = Watcher(settings, load_state(), announce=True)
    while True:
        try:
            for line in watcher.cycle():
                LOG.info("%s", line)
        except Exception:
            LOG.exception("cycle interrompu")
        time.sleep(settings.poll_seconds)


def main(argv: list[str]) -> int:
    setup_logging()
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd in {"-h", "--help", "help"}:
        print(
            "Usage : kraken-alerts [run|panel|tray|check|rules]\n"
            "  run    veille permanente (défaut)\n"
            "  panel  fenêtre de récapitulatif\n"
            "  tray   icône dans la barre\n"
            "  check  une lecture immédiate, sans notification\n"
            "  rules  explique les règles\n"
            f"Config : {CONFIG_PATH}"
        )
        return 0
    if cmd == "rules":
        print(RULES)
        return 0
    settings = load_settings()
    if not Path(settings.kraken_bin).exists() and shutil.which(settings.kraken_bin) is None:
        raise SystemExit(f"Kraken CLI introuvable : {settings.kraken_bin}")
    if cmd == "panel":
        from panel import run_panel

        return run_panel()
    if cmd == "tray":
        from tray import run_tray

        return run_tray()
    if cmd == "check":
        return cmd_check(settings)
    if cmd == "run":
        return cmd_run(settings)
    raise SystemExit(f"Commande inconnue : {cmd}")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except KeyboardInterrupt:
        raise SystemExit(0)
