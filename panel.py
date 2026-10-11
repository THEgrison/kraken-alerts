"""Fenêtre de récapitulatif ouverte depuis le menu Debian."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from kraken_alerts import SNAPSHOT_PATH, Watcher, load_settings, load_state, side_label

ICON = Path.home() / ".local/share/icons/hicolor/48x48/apps/kraken-alerts.png"

CSS = b"""
.title { font-size: 18px; font-weight: 700; }
.name { font-size: 16px; font-weight: 700; }
.price { font-size: 20px; font-weight: 700; }
.muted { color: alpha(@theme_fg_color, 0.62); font-size: 12px; }
.detail { color: alpha(@theme_fg_color, 0.78); font-size: 12px; }
.card {
  background-color: alpha(@theme_fg_color, 0.05);
  border-radius: 12px;
  padding: 10px 12px;
}
.badge {
  background-color: alpha(@theme_fg_color, 0.08);
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 700;
}
.long { color: #1f8a4c; font-weight: 700; }
.short { color: #d14343; font-weight: 700; }
.neutre { font-weight: 700; color: alpha(@theme_fg_color, 0.72); }
.up { color: #1f8a4c; font-weight: 700; }
.down { color: #d14343; font-weight: 700; }
.flat { font-weight: 700; }
"""


def service_active() -> bool:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    proc = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "kraken-alerts"],
        env=env,
        check=False,
    )
    return proc.returncode == 0


def read_snapshot() -> tuple[str, dict | None]:
    try:
        raw = SNAPSHOT_PATH.read_text(encoding="utf-8")
    except OSError:
        return "", None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw, None
    return raw, data if isinstance(data, dict) else None


class Panel(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="Alertes Kraken")
        self.set_default_size(620, 640)
        self.set_position(Gtk.WindowPosition.CENTER)
        if ICON.exists():
            self.set_icon_from_file(str(ICON))

        self.raw = ""
        self.busy = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_border_width(16)
        self.add(root)

        title = Gtk.Label(label="Alertes Kraken", xalign=0)
        title.get_style_context().add_class("title")
        subtitle = Gtk.Label(
            label="Prix, type de marché, biais court terme et long terme. Aucun ordre n'est passé.",
            xalign=0,
        )
        subtitle.set_line_wrap(True)
        subtitle.get_style_context().add_class("muted")
        self.status = Gtk.Label(xalign=0)
        self.status.get_style_context().add_class("muted")

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(520)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scrolled.add(self.cards)

        actions = Gtk.Box(spacing=8)
        self.refresh_button = Gtk.Button(label="Actualiser")
        self.refresh_button.connect("clicked", lambda *_: self.refresh())
        hint = Gtk.Label(label="Fermer cette fenêtre laisse les alertes actives.", xalign=0)
        hint.set_hexpand(True)
        hint.get_style_context().add_class("muted")
        actions.pack_start(hint, True, True, 0)
        actions.pack_end(self.refresh_button, False, False, 0)

        root.pack_start(title, False, False, 0)
        root.pack_start(subtitle, False, False, 0)
        root.pack_start(self.status, False, False, 0)
        root.pack_start(scrolled, True, True, 0)
        root.pack_start(actions, False, False, 0)

        self.poll()
        GLib.idle_add(self.place_on_pointer_monitor)
        GLib.timeout_add_seconds(2, self.poll)
        updated = 0.0
        if self.raw:
            try:
                updated = float(json.loads(self.raw).get("updated") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                updated = 0.0
        if time.time() - updated > 90:
            self.refresh()

    def place_on_pointer_monitor(self) -> bool:
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        pointer = display.get_default_seat().get_pointer()
        _, x, y = pointer.get_position()
        monitor = display.get_monitor_at_point(x, y)
        rect = monitor.get_geometry()
        width, height = self.get_size()
        self.move(
            rect.x + max(24, (rect.width - width) // 2),
            rect.y + max(24, (rect.height - height) // 2),
        )
        return False

    def poll(self) -> bool:
        raw, data = read_snapshot()
        self.update_status(data)
        if raw != self.raw:
            self.raw = raw
            self.render(data)
        return True

    def update_status(self, data: dict | None) -> None:
        active = "Veille active" if service_active() else "Veille arrêtée"
        if not data or not data.get("updated"):
            self.status.set_text(f"{active} · en attente d'une première lecture")
            return
        stamp = time.strftime("%H:%M:%S", time.localtime(float(data["updated"])))
        markets = sorted({row.get("market") or "Spot" for row in data.get("pairs") or []})
        market_text = " · ".join(markets) if markets else "Spot"
        late = ""
        if time.time() - float(data["updated"]) > 180:
            late = " · lecture en retard"
        count = len(data.get("pairs") or [])
        self.status.set_text(f"{active} · {count} suivis · {market_text} · mis à jour à {stamp}{late}")

    def render(self, data: dict | None) -> None:
        for child in self.cards.get_children():
            self.cards.remove(child)
        rows = (data or {}).get("pairs") or []
        if not rows:
            empty = Gtk.Label(label="Lecture des prix en cours.", xalign=0)
            empty.get_style_context().add_class("muted")
            self.cards.pack_start(empty, False, False, 0)
            self.cards.show_all()
            return
        for row in rows:
            self.cards.pack_start(self.card(row), False, False, 0)
        self.cards.show_all()

    def card(self, row: dict) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.get_style_context().add_class("card")

        head = Gtk.Box(spacing=8)
        name = Gtk.Label(label=row.get("label") or "", xalign=0)
        name.get_style_context().add_class("name")
        name.set_xalign(0)
        symbol = Gtk.Label(label=row.get("symbol") or "", xalign=0)
        symbol.set_xalign(0)
        symbol.get_style_context().add_class("muted")
        spacer = Gtk.Label()
        spacer.set_hexpand(True)
        badge = Gtk.Label(label=row.get("market") or "Spot")
        badge.get_style_context().add_class("badge")
        head.pack_start(name, False, False, 0)
        head.pack_start(symbol, False, False, 0)
        head.pack_start(spacer, True, True, 0)
        head.pack_start(badge, False, False, 0)

        price_row = Gtk.Box(spacing=12)
        price = Gtk.Label(label=f"{row.get('price')} $", xalign=0)
        price.get_style_context().add_class("price")
        price.set_hexpand(True)
        day = Gtk.Label(label=str(row.get("day") or ""), xalign=1)
        day_value = row.get("day_value")
        if isinstance(day_value, (int, float)) and day_value > 0.05:
            day.get_style_context().add_class("up")
        elif isinstance(day_value, (int, float)) and day_value < -0.05:
            day.get_style_context().add_class("down")
        else:
            day.get_style_context().add_class("flat")
        price_row.pack_start(price, True, True, 0)
        price_row.pack_end(day, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        self.bias_line(grid, 0, "Court terme", row.get("short") or "neutre", row.get("short_detail") or "")
        self.bias_line(grid, 1, "Long terme", row.get("long") or "neutre", row.get("long_detail") or "")

        lecture = Gtk.Label(label=row.get("lecture") or "", xalign=0)
        lecture.set_line_wrap(True)
        lecture.set_max_width_chars(48)
        lecture.get_style_context().add_class("detail")

        span = Gtk.Label(
            label=f"Fourchette 24 h · {row.get('low')} $ – {row.get('high')} $",
            xalign=0,
        )
        span.get_style_context().add_class("muted")

        box.pack_start(head, False, False, 0)
        box.pack_start(price_row, False, False, 0)
        box.pack_start(grid, False, False, 0)
        box.pack_start(lecture, False, False, 0)
        box.pack_start(span, False, False, 0)
        return box

    def bias_line(self, grid: Gtk.Grid, row: int, horizon: str, side: str, detail: str) -> None:
        horizon_label = Gtk.Label(label=horizon, xalign=0)
        horizon_label.set_size_request(108, -1)
        horizon_label.get_style_context().add_class("muted")
        side_name = side if side in {"long", "short", "neutre"} else "neutre"
        side_label_widget = Gtk.Label(label=side_label(side), xalign=0)
        side_label_widget.set_size_request(72, -1)
        side_label_widget.get_style_context().add_class(side_name)
        detail_label = Gtk.Label(label=detail, xalign=0)
        detail_label.set_hexpand(True)
        detail_label.set_line_wrap(True)
        detail_label.get_style_context().add_class("detail")
        grid.attach(horizon_label, 0, row, 1, 1)
        grid.attach(side_label_widget, 1, row, 1, 1)
        grid.attach(detail_label, 2, row, 1, 1)

    def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.refresh_button.set_sensitive(False)
        self.status.set_text("Lecture des prix…")

        def work() -> None:
            try:
                watcher = Watcher(load_settings(), load_state(), announce=False)
                watcher.cycle(force_indicators=True)
            except Exception as exc:
                GLib.idle_add(self.refresh_failed, str(exc))
                return
            GLib.idle_add(self.refresh_done)

        threading.Thread(target=work, daemon=True).start()

    def refresh_done(self) -> bool:
        self.busy = False
        self.refresh_button.set_sensitive(True)
        self.poll()
        return False

    def refresh_failed(self, message: str) -> bool:
        self.busy = False
        self.refresh_button.set_sensitive(True)
        self.status.set_text(f"Lecture impossible · {message}")
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="app.kraken.Alerts")
        self.window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        from gi.repository import Gdk

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def do_activate(self):
        if self.window is None:
            self.window = Panel(self)
            self.window.show_all()
        self.window.present()


def run_panel() -> int:
    GLib.set_prgname("kraken-alerts")
    return App().run([])
