#!/bin/sh
# Installe la veille dans la session de l'utilisateur.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)

mkdir -p \
  "$HOME/.local/bin" \
  "$HOME/.local/share/kraken-alerts" \
  "$HOME/.local/share/applications" \
  "$HOME/.config/systemd/user" \
  "$HOME/.config/kraken-alerts" \
  "$HOME/.local/share/icons/hicolor/scalable/apps" \
  "$HOME/.local/share/icons/hicolor/48x48/apps" \
  "$HOME/.local/share/icons/hicolor/64x64/apps" \
  "$HOME/.local/share/icons/hicolor/128x128/apps"

cp "$ROOT/kraken_alerts.py" "$ROOT/panel.py" "$ROOT/tray.py" "$HOME/.local/share/kraken-alerts/"
chmod 755 "$HOME/.local/share/kraken-alerts/kraken_alerts.py"

cp "$ROOT/icons/hicolor/scalable/apps/kraken-alerts.svg" \
  "$HOME/.local/share/icons/hicolor/scalable/apps/kraken-alerts.svg"
cp "$ROOT/icons/hicolor/48x48/apps/kraken-alerts.png" \
  "$HOME/.local/share/icons/hicolor/48x48/apps/kraken-alerts.png"
cp "$ROOT/icons/hicolor/64x64/apps/kraken-alerts.png" \
  "$HOME/.local/share/icons/hicolor/64x64/apps/kraken-alerts.png"
cp "$ROOT/icons/hicolor/128x128/apps/kraken-alerts.png" \
  "$HOME/.local/share/icons/hicolor/128x128/apps/kraken-alerts.png"

cp "$ROOT/packaging/kraken-alerts.service" \
  "$HOME/.config/systemd/user/kraken-alerts.service"
cp "$ROOT/packaging/kraken-alerts-tray.service" \
  "$HOME/.config/systemd/user/kraken-alerts-tray.service"

if [ ! -f "$HOME/.config/kraken-alerts/config.toml" ]; then
  cp "$ROOT/config.toml" "$HOME/.config/kraken-alerts/config.toml"
fi

cat > "$HOME/.local/bin/kraken-alerts" << EOF
#!/bin/sh
exec python3 "\$HOME/.local/share/kraken-alerts/kraken_alerts.py" "\$@"
EOF
chmod 755 "$HOME/.local/bin/kraken-alerts"

sed \
  -e "s|@BIN@|$HOME/.local/bin/kraken-alerts|" \
  -e "s|@ICON@|$HOME/.local/share/icons/hicolor/48x48/apps/kraken-alerts.png|" \
  "$ROOT/packaging/kraken-alerts.desktop" \
  > "$HOME/.local/share/applications/kraken-alerts.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi

echo "Installé. Active la veille avec : systemctl --user enable --now kraken-alerts"
