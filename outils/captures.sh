#!/usr/bin/env bash
# Régénère les captures du README sur un dossier de démonstration créé dans /tmp
# (en mémoire sur la plupart des systèmes : rien n'est écrit sur le disque).
# Nécessite : ffmpeg, Chromium (paquet ou Flatpak org.chromium.Chromium), python-pillow.
set -euo pipefail

src="$(cd "$(dirname "$0")/.." && pwd)"
demo=/tmp/Maison
port=18799
work="$src/captures/.travail"  # dans le dossier perso : Chromium en Flatpak n'a pas accès à /tmp

if command -v chromium >/dev/null; then chromium=(chromium)
else chromium=(flatpak run --command=chromium org.chromium.Chromium); fi

# ---------- Dossier de démonstration ----------
# fallocate réserve la taille sans écrire de données : 2,7 Go en une seconde.
rm -rf "$demo" "$work"
mkdir -p "$demo" "$work"
mk() { mkdir -p "$(dirname "$demo/$1")"; fallocate -l "$2" "$demo/$1"; }
g="SteamLibrary/steamapps/common"
mk "$g/Aventure Lointaine/Content/Paks/pakchunk0-Windows.pak" 210M
mk "$g/Aventure Lointaine/Content/Paks/pakchunk1-Windows.pak" 95M
mk "$g/Aventure Lointaine/Binaries/Win64/Aventure.exe" 38M
for i in $(seq 1 14); do mk "$g/Aventure Lointaine/Binaries/Win64/lib$i.dll" $((i * 300 + 400))K; done
mk "$g/Course Rapide/Content/global.ucas" 140M
mk "$g/Course Rapide/Content/global.utoc" 3M
for i in $(seq 1 25); do mk "$g/Course Rapide/Content/Movies/intro$i.bk2" $((i * 90 + 200))K; done
mk "ComfyUI/models/checkpoints/sdxl-base.safetensors" 170M
for i in 1 2 3 4 5; do mk "ComfyUI/models/loras/style-$i.safetensors" $((i * 9 + 20))M; done
mk "LLM/qwen-14b-q4_k_m.gguf" 120M
mk "LLM/mistral-7b-q5.gguf" 64M
for i in $(seq 1 18); do mk "Photos/RAW/IMG_$((4100 + i)).CR2" $((i % 5 * 2 + 22))M; done
for i in $(seq 1 30); do mk "Musique/Album $((i % 4 + 1))/piste-$i.flac" $((i % 7 * 3 + 18))M; done
mk "VM/ubuntu-26.04-desktop.iso" 150M
mk "Archives/sauvegarde-2025.zip" 88M
mk "Archives/projets.tar.zst" 31M
for i in $(seq 1 12); do mk "Documents/rapport-$i.pdf" $((i * 120 + 80))K; done
for i in $(seq 1 400); do mk "Projets/site/node_modules/pkg$((i % 40))/lib/f$i.js" $((i % 9 * 6 + 8))K; done
mk "Téléchargements/setup-jeu.exe" 47M
# de vraies vidéos, pour que les miniatures s'affichent
mkdir -p "$demo/Vidéos/Vacances"
i=0
for s in "testsrc2=size=1280x720" "smptehdbars=size=1280x720" "mandelbrot=size=1280x720" \
         "rgbtestsrc=size=1280x720" "cellauto=size=1280x720:rule=110" \
         "life=size=1280x720:mold=10:ratio=0.1:death_color=#C83232:life_color=#00ff00"; do
    i=$((i + 1))
    sub=""; [ $i -gt 3 ] && sub="Vacances/"
    ffmpeg -y -loglevel error -f lavfi -i "${s}:rate=30" -t $((4 + i * 3)) \
        -c:v libx264 -b:v 12M -pix_fmt yuv420p "$demo/Vidéos/${sub}clip-$i.mp4"
done

# ---------- Serveur ----------
python3 "$src/linstat.py" --port "$port" --no-window 2>/dev/null &
server=$!
trap 'kill $server 2>/dev/null; rm -rf "$demo" "$work"' EXIT
sleep 1
url="http://127.0.0.1:$port"
curl -sf -o /dev/null -X POST -H "X-LinStat: 1" -H "Content-Type: application/json" \
    -d "{\"path\":\"$demo\"}" "$url/api/scan"
for f in "$demo"/Vidéos/*.mp4 "$demo"/Vidéos/Vacances/*.mp4; do  # miniatures prêtes avant la capture
    curl -sf -o /dev/null -G --data-urlencode "path=$f" "$url/api/thumb"
done

shot() {  # shot <fichier> <paramètres d'URL>
    "${chromium[@]}" --headless=new --disable-gpu --hide-scrollbars --window-size=1400,900 \
        --virtual-time-budget=10000 --screenshot="$1" "$url/$2" >/dev/null 2>&1
}
shot "$src/captures/carte.png" ""
shot "$src/captures/categories.png" "?vue=categories&cat=Vid%C3%A9os"
themes=(aurore lagon foret agrumes bonbon sobre)
for t in "${themes[@]}"; do shot "$work/$t.png" "?theme=$t"; done

# ---------- Planche des thèmes ----------
python3 - "$work" "$src/captures/themes.png" "${themes[@]}" <<'EOF'
import sys
from PIL import Image, ImageDraw, ImageFont
work, out, themes = sys.argv[1], sys.argv[2], sys.argv[3:]
noms = {"aurore": "Aurore (par défaut)", "lagon": "Lagon", "foret": "Forêt",
        "agrumes": "Agrumes", "bonbon": "Bonbon", "sobre": "Sobre"}
w, h, gap, label = 620, 398, 16, 34
cols, rows = 3, (len(themes) + 2) // 3
sheet = Image.new("RGB", (cols * w + (cols + 1) * gap, rows * (h + label) + (rows + 1) * gap), "#14111f")
try:
    font = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
except OSError:
    font = ImageFont.load_default()
draw = ImageDraw.Draw(sheet)
for k, t in enumerate(themes):
    x = gap + (k % cols) * (w + gap)
    y = gap + (k // cols) * (h + label + gap)
    im = Image.open(f"{work}/{t}.png").convert("RGB").resize((w, h), Image.LANCZOS)
    draw.text((x + 2, y + 4), noms.get(t, t), fill="#f4effe", font=font)
    sheet.paste(im, (x, y + label))
sheet.save(out, optimize=True)
EOF
echo "Captures régénérées dans $src/captures/"
