"""Icône de barre XFCE. Le clic ouvre le panneau, une pastille signale une alerte."""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, GLib, Gtk

from kraken_alerts import UNREAD_PATH

ICON = Path.home() / ".local/share/icons/hicolor/48x48/apps/kraken-alerts.png"
LOCK_PATH = UNREAD_PATH.with_name("tray.lock")
LAUNCHER = Path.home() / ".local/bin/kraken-alerts"


def open_panel(*_args) -> None:
    command = [str(LAUNCHER), "panel"] if LAUNCHER.exists() else ["kraken-alerts", "panel"]
    subprocess.Popen(command, start_new_session=True)


def badge(base: GdkPixbuf.Pixbuf, unread: bool) -> GdkPixbuf.Pixbuf:
    pix = base.copy()
    if not unread:
        return pix
    dot = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 8, 8)
    dot.fill(0xD14343FF)
    width = pix.get_width()
    dot.composite(
        pix,
        max(0, width - 8),
        0,
        8,
        8,
        max(0, width - 8),
        0,
        1,
        1,
        GdkPixbuf.InterpType.NEAREST,
        255,
    )
    return pix


def run_tray() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    lock.write(str(GLib.get_monotonic_time()))
    lock.flush()

    if ICON.exists():
        base = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(ICON), 24, 24, True)
    else:
        base = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 24, 24)
        base.fill(0x12352FFF)

    icon = Gtk.StatusIcon()
    icon.set_visible(True)
    icon.set_title("Alertes Kraken")
    icon.connect("activate", open_panel)

    def popup(_icon, button, activate_time) -> None:
        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label="Ouvrir le panneau")
        open_item.connect("activate", open_panel)
        quit_item = Gtk.MenuItem(label="Retirer l'icône")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(open_item)
        menu.append(quit_item)
        menu.show_all()
        menu.popup(None, None, None, None, button, activate_time)

    icon.connect("popup-menu", popup)

    def refresh() -> bool:
        unread = UNREAD_PATH.exists()
        icon.set_from_pixbuf(badge(base, unread))
        icon.set_tooltip_text("Alertes Kraken · nouvelle alerte" if unread else "Alertes Kraken")
        return True

    GLib.timeout_add_seconds(2, refresh)
    refresh()
    Gtk.main()
    return 0
