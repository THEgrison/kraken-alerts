# Alertes Kraken

Veille de prix pour Debian. Elle lit les cours publics avec [Kraken CLI](https://www.kraken.com/fr/kraken-cli), envoie des notifications, et ouvre un panneau depuis le menu des applications.

Aucun ordre n'est passé. Les lectures long et short sont des règles mécaniques, pas un conseil d'investissement.

## Prérequis

- une session graphique Debian (testé sous XFCE)
- Python 3.11+ avec GTK 3 (`python3-gi`, `gir1.2-gtk-3.0`)
- `notify-send`
- [Kraken CLI](https://github.com/krakenfx/kraken-cli)

## Installation

```bash
./install.sh
systemctl --user enable --now kraken-alerts
```

Le panneau **Alertes Kraken** apparaît dans le menu, catégorie Bureautique. Le fermer n'arrête pas les alertes. L'icône dans la barre se lance avec :

```bash
systemctl --user enable --now kraken-alerts-tray
```

## Configuration

Le fichier personnel est `~/.config/kraken-alerts/config.toml`. `config.toml` dans ce dépôt sert de modèle et n'est copié que s'il n'existe pas encore.

`market` peut valoir `Spot`, `Futures`, `Forex` ou `Action tokenisée`. Un perpétuel utilise `engine = "futures"` (par exemple `PF_XBTUSD`). Une action tokenisée garde la casse du symbole, comme `AAPLx/USD`, avec `asset_class = "tokenized_asset"`.

`below` et `above` sont des seuils franchis une seule fois. Entre `quiet_start` et `quiet_end`, les notifications sont retenues. À `digest_hour`, un seul résumé part.

Après une modification :

```bash
systemctl --user restart kraken-alerts
```

## Commandes

```bash
kraken-alerts panel
kraken-alerts check
kraken-alerts rules
systemctl --user status kraken-alerts
```
