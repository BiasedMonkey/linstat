#!/usr/bin/env bash
# Installe LinStat pour l'utilisateur courant (aucun droit root nécessaire).
#   ./install.sh           → appli + icône + lanceur dans le menu
#   ./install.sh --favori  → et l'ajoute aux favoris du menu KDE
set -euo pipefail

src="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
dest="$data/linstat"

python3 -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('WebKit', '6.0')" 2>/dev/null || \
    echo "Remarque : GTK 4 + WebKitGTK 6 introuvables, LinStat s'ouvrira dans le navigateur" \
         "(Arch / CachyOS : sudo pacman -S python-gobject webkitgtk-6.0)." >&2

install -Dm755 "$src/linstat.py"  "$dest/linstat.py"
install -Dm644 "$src/index.html"  "$dest/index.html"
install -Dm644 "$src/linstat.svg" "$dest/linstat.svg"
install -Dm644 "$src/linstat.svg" "$data/icons/hicolor/scalable/apps/linstat.svg"
mkdir -p "$data/applications"
sed -e "s|@DEST@|$dest|g" -e "s|@HOME@|$HOME|g" "$src/app.linstat.LinStat.desktop" \
    > "$data/applications/app.linstat.LinStat.desktop"
chmod 644 "$data/applications/app.linstat.LinStat.desktop"

command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null && update-desktop-database "$data/applications" 2>/dev/null || true

for opt in "$@"; do
    case "$opt" in
        --favori)
            command -v qdbus6 >/dev/null && qdbus6 org.kde.ActivityManager /ActivityManager/Resources/Linking \
                org.kde.ActivityManager.ResourcesLinking.LinkResourceToActivity \
                org.kde.plasma.favorites.applications applications:app.linstat.LinStat.desktop :global \
                && echo "Ajouté aux favoris du menu KDE." ;;
    esac
done

echo "LinStat installé dans $dest — lance-le depuis le menu."
