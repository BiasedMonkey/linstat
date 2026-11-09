#!/usr/bin/env python3
"""LinStat — un WinDirStat minimaliste pour Linux.

Analyse un dossier, puis affiche un treemap « coussiné », l'arborescence triée
par taille et la liste des extensions, dans sa propre fenêtre (GTK4 + WebKit)
ou à défaut dans le navigateur.

    python3 linstat.py [dossier] [--browser]
"""
import argparse
import gzip
import hashlib
import heapq
import io
import itertools
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

HERE = Path(__file__).resolve().parent
APP_ID = "app.linstat.LinStat"

# Un élément plus petit que total/DETAIL fait moins de quelques pixels sur la carte :
# il est fusionné dans un bloc « ‹N petits éléments› ».
DETAIL = 50_000
PSEUDO_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "ramfs", "cgroup", "cgroup2",
             "securityfs", "debugfs", "tracefs", "pstore", "bpf", "configfs", "fusectl",
             "mqueue", "hugetlbfs", "autofs", "efivarfs", "binfmt_misc", "nsfs", "rpc_pipefs"}


# ---------- Catégories ----------
# (nom, couleur, extensions). L'ordre fixe aussi les couleurs de la carte.
CATEGORIES = [
    ("Modèles IA", (171, 71, 188), "gguf ggml safetensors ckpt pt pth onnx tflite mlmodel h5 keras"),
    ("Jeux", (66, 133, 244), "pak ucas utoc tfc ress resource assets vpk bsa ba2 wad upk uasset uexp forge rpf pck gcf"),
    ("Vidéos", (234, 67, 53), "mp4 mkv mov avi webm m4v wmv flv mpg mpeg mts m2ts 3gp vob ogv"),
    ("Audio", (255, 167, 38), "mp3 wav flac ogg opus m4a aac wma aiff aif mid midi ape aup3 aup"),
    ("Images", (52, 168, 83), "jpg jpeg png gif webp heic heif avif tif tiff bmp svg ico psd kra xcf exr hdr "
                              "cr2 cr3 nef arw dng raf orf rw2 srw pef"),
    ("Documents", (251, 188, 5), "pdf doc docx odt rtf txt md epub mobi xls xlsx ods csv ppt pptx odp tex"),
    ("Archives", (141, 110, 99), "zip 7z rar tar gz tgz xz zst bz2 lz4 lzma cab"),
    ("VM & images disque", (38, 166, 154), "iso img qcow2 vdi vmdk vhd vhdx ova dmg wim"),
    ("Programmes & système", (0, 188, 212), "so dll exe msi msp appimage deb rpm apk xapk dylib a o ko sys efi"),
    ("Code & dév.", (236, 64, 122), "py js mjs cjs ts tsx jsx c h cpp hpp cc rs go java kt cs rb php sh lua "
                                    "json yaml yml toml xml html css scss map wasm pyc class jar whl pack idx rlib"),
    ("Données", (92, 107, 192), "db sqlite sqlite3 ldb sst log dat parquet npz npy pkl hdf5"),
    ("Polices", (212, 225, 87), "ttf otf woff woff2 ttc"),
    ("Caches", (120, 144, 156), "cache tmp"),
    ("Corbeille", (255, 112, 67), ""),
    ("Autres", (150, 150, 150), ""),
]
EXT_CAT = {e: name for name, _, exts in CATEGORIES for e in exts.split()}

# Règles sur le chemin, plus parlantes que l'extension : un jeu contient des .dll et des .mp4,
# un modèle Ollama n'a pas d'extension. Tout le sous-dossier hérite de la catégorie.
# « ~/x » : ce chemin exact ; « */x/y » : tout dossier dont le chemin finit ainsi.
_HOME = os.path.expanduser("~")
PATH_RULES = {
    "Corbeille": ["~/.local/share/Trash", f"*/.Trash-{os.getuid()}"],
    "Modèles IA": ["~/models", "~/.lmstudio", "*/.ollama/models", "*/.cache/huggingface", "~/.unsloth",
                   "*/.var/app/ai.lmstudio.lm-studio", "*/.cache/lm-studio", "*/ComfyUI/models",
                   "*/stable-diffusion-webui/models"],
    "Jeux": ["~/Games", "~/.local/share/Steam", "*/steamapps/common", "*/steamapps/compatdata", "*/SteamLibrary",
             "~/.local/share/lutris", "~/.config/heroic", "*/.var/app/com.usebottles.bottles",
             "*/.var/app/com.valvesoftware.Steam", "*/.var/app/com.heroicgameslauncher.hgl"],
    "VM & images disque": ["/var/lib/docker", "/var/lib/libvirt", "/var/lib/machines", "~/.local/share/containers",
                           "*/VirtualBox VMs", "~/.config/Claude/vm_bundles"],
    "Programmes & système": ["/usr", "/opt", "/var/lib/flatpak", "~/.local/share/flatpak", "/var/lib/pacman"],
    "Code & dév.": ["*/node_modules", "~/.cargo", "~/.rustup", "~/.npm", "~/.gradle", "~/.m2", "*/.venv", "*/.git",
                    "~/.conda", "*/conda/envs", "~/miniconda3", "~/anaconda3"],
    "Caches": ["~/.cache", "/var/cache", "/var/tmp"],
}
# Faible : seulement si rien n'est encore décidé ; forte : la plus profonde gagne ; finale : jamais écrasée.
PRIO = {None: 0, "Code & dév.": 1, "Caches": 1, "Corbeille": 3}
RULES = {}  # nom du dossier -> [(motif, exact ?, catégorie)], pour un test en O(1)
for _cat, _pats in PATH_RULES.items():
    for _p in _pats:
        if _p.startswith("*/"):
            _p, _exact = _p[1:], False  # « /x/y » : suffixe du chemin
        else:
            _p, _exact = (_HOME + _p[1:] if _p.startswith("~/") else _p), True
        RULES.setdefault(os.path.basename(_p), []).append((_p, _exact, _cat))


def path_hint(path, hint):
    """Catégorie d'un dossier d'après son chemin, en tenant compte de celle de son parent."""
    for pat, exact, cat in RULES.get(os.path.basename(path), ()):
        if (path == pat if exact else path.endswith(pat)) and PRIO.get(cat, 2) >= PRIO.get(hint, 2):
            hint = cat
    return hint


def fmt(n):
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unit == "To":
            return f"{n:.1f} {unit}" if unit != "o" else f"{n} o"
        n /= 1024


def within(path, base):
    return path == base or path.startswith(base.rstrip("/") + "/")


def read_mounts():
    """[(point de montage, source, type, maj:min, racine)] depuis /proc/self/mountinfo."""
    unesc = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)
    out = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, right = line.split(" - ", 1)
            a, b = left.split(), right.split()
            out.append((unesc(a[4]), b[1], b[0], a[2], unesc(a[3])))
    return out


class Dir:
    __slots__ = ("name", "dirs", "fsize", "fcount", "total", "kept", "cat", "parent")

    def __init__(self, name):
        self.name = name
        self.dirs = []
        self.fsize = 0    # octets des fichiers directs
        self.fcount = 0   # nombre de fichiers directs
        self.total = 0
        self.kept = None  # [(nom, octets)] des fichiers assez gros pour être affichés
        self.cat = None   # catégorie imposée par une règle de chemin (héritée par le sous-arbre)
        self.parent = None

    def path(self):
        parts, d = [], self
        while d is not None:
            parts.append(d.name)  # le nom de la racine est son chemin absolu
            d = d.parent
        return os.path.join(*reversed(parts))


# Budget d'entrées par tâche : assez pour amortir l'envoi entre processus,
# assez peu pour que le travail se répartisse bien entre les cœurs.
BUDGET = 40_000
# Fichiers listés par catégorie dans la vue « Catégories », même s'ils sont trop petits pour la carte
TOP_PER_CAT = 250
CAT_NAMES = [name for name, *_ in CATEGORIES]

_foreign = frozenset()
_counters = None


class Counter:
    """Même interface qu'un multiprocessing.Value, pour l'analyse sans sous-processus."""

    def __init__(self):
        self.value = 0
        self._lock = threading.Lock()

    def get_lock(self):
        return self._lock


def init_worker(foreign, counters):
    global _foreign, _counters
    _foreign, _counters = foreign, counters


def report(files, size):
    with _counters[0].get_lock():
        _counters[0].value += files
    with _counters[1].get_lock():
        _counters[1].value += size


def scan_batch(paths, budget=BUDGET, known_total=0, floors=None):
    """Analyse des dossiers (dans un sous-processus) jusqu'à épuiser le budget d'entrées.

    Renvoie un résultat compact : les dossiers vus sous forme de listes parallèles
    (nom, parent, octets et nombre de fichiers directs), les gros fichiers candidats,
    les fichiers à liens multiples, les extensions, et les dossiers pas encore ouverts.
    paths : [(chemin, catégorie héritée)] ; floors : taille minimale pour entrer dans le
    classement de chaque catégorie, d'après ce que les tâches précédentes ont déjà trouvé."""
    foreign, rules, catmap = _foreign, RULES, EXT_CAT
    floors = floors or {}
    min_floor = min(floors.get(c, 0) for c in CAT_NAMES)
    tops = {}
    names, parents = [None] * len(paths), [-1] * len(paths)
    fsizes, fcounts = [0] * len(paths), [0] * len(paths)
    stack = [(p, k, h) for k, (p, h) in enumerate(paths)]
    by_hint, cands, links, seen, hints = {}, [], [], set(), {}
    errors = entries = total = rep_files = rep_size = 0
    while stack and entries < budget:
        path, idx, hint = stack.pop()
        exts = by_hint.setdefault(hint, {})  # extensions comptées par catégorie de chemin
        fsize = fcount = 0
        try:
            with os.scandir(path) as it:
                for e in it:
                    entries += 1
                    try:
                        # is_dir() lit le type dans l'entrée de répertoire : pas de stat()
                        if e.is_dir(follow_symlinks=False):
                            if e.path not in foreign:
                                h = path_hint(e.path, hint) if e.name in rules else hint
                                if h != hint:
                                    hints[len(names)] = h
                                stack.append((e.path, len(names), h))
                                names.append(e.name)
                                parents.append(idx)
                                fsizes.append(0)
                                fcounts.append(0)
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        errors += 1
                        continue
                    size = st.st_blocks * 512
                    name = e.name
                    i = name.rfind(".")
                    x = name[i + 1:] if 0 < i < len(name) - 1 and len(name) - i <= 16 else ""
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                        links.append((key, size, idx, name, x, hint))
                    fsize += size
                    fcount += 1
                    if (s := exts.get(x)) is None:
                        exts[x] = [size, 1]
                    else:
                        s[0] += size
                        s[1] += 1
                    if size >= 4096:
                        cands.append((size, name, idx))
                    if size >= min_floor:
                        cat = hint or catmap.get(x) or catmap.get(x.lower(), "Autres")
                        if size >= floors.get(cat, 0):
                            tops.setdefault(cat, []).append((size, name, idx))
        except OSError:
            errors += 1
        fsizes[idx], fcounts[idx] = fsize, fcount
        total += fsize
        rep_files += fcount
        rep_size += fsize
        if rep_files >= 20_000:
            report(rep_files, rep_size)
            rep_files = rep_size = 0
    report(rep_files, rep_size)
    # Le total final est au moins celui de cette tâche, et au moins ce que l'analyse
    # a déjà compté : tout fichier sous ce seuil-ci restera sous le seuil final.
    floor = max(max(total, known_total) // DETAIL, 4096)
    return {"names": names, "parents": parents, "fsizes": fsizes, "fcounts": fcounts,
            "cands": [c for c in cands if c[0] >= floor], "links": links, "exts": by_hint,
            "hints": hints, "errors": errors, "left": stack,
            "top": {c: heapq.nlargest(TOP_PER_CAT, items) for c, items in tops.items()}}


class Scanner:
    """Répartit l'analyse sur plusieurs processus : chacun a son propre GIL, alors
    que des threads se disputeraient le GIL à chaque appel système."""

    def __init__(self, root, one_fs=True, procs=None, budget=BUDGET):
        self.root = os.path.realpath(root)
        self.procs = procs or min(os.cpu_count() or 4, 12)
        self.budget = budget
        self.foreign = frozenset(self._foreign_mounts(one_fs))
        self.counters = (Counter(), Counter())
        self.seen = set()  # (dev, inode) des fichiers à liens multiples, toutes tâches confondues
        self.exts = {}
        self.cands = []
        self.known = 0  # octets déjà comptés : sert de seuil minimal aux tâches suivantes
        self.top = {}   # catégorie -> tas des TOP_PER_CAT plus gros fichiers (octets, n°, nom, Dir)
        self.cat_floors = {}
        self.tie = itertools.count()
        self.errors = 0

    def _foreign_mounts(self, one_fs):
        """Points de montage à ne pas traverser."""
        mounts = read_mounts()
        own = max((m for m in mounts if within(self.root, m[0])), key=lambda m: len(m[0]))
        # Ce que l'analyse voit déjà : (périphérique, sous-arbre)
        visible = [(own[3], os.path.normpath(os.path.join(own[4], os.path.relpath(self.root, own[0]))))]
        foreign = set()
        for mnt, src, fstype, dev, mroot in mounts:
            if mnt == own[0] or not within(mnt, self.root):
                continue
            # On compare la *source* et non st_dev : sur btrfs chaque sous-volume
            # (/home, /var/log…) a son propre st_dev alors qu'il est sur le même disque.
            if fstype in PSEUDO_FS or (one_fs and src != own[1]):
                foreign.add(mnt)
            elif any(d == dev and within(mroot, r) for d, r in visible):
                foreign.add(mnt)  # bind mount d'un contenu déjà compté ailleurs
            else:
                visible.append((dev, mroot))
        return foreign

    @property
    def files(self):
        return self.counters[0].value

    @property
    def total(self):
        return self.counters[1].value

    def run(self):
        root = Dir(self.root)
        # Catégorie de la racine : on rejoue les règles sur chacun de ses ancêtres
        hint, p = None, "/"
        for part in self.root.strip("/").split("/"):
            p = os.path.join(p, part)
            hint = path_hint(p, hint)
        root.cat = hint
        try:
            ctx = multiprocessing.get_context("forkserver")
            self.counters = (ctx.Value("q", 0), ctx.Value("q", 0))
            pool = ProcessPoolExecutor(self.procs, mp_context=ctx, initializer=init_worker,
                                       initargs=(self.foreign, self.counters))
            pool.submit(int).result()  # les processus démarrent ici : on veut l'erreur maintenant
        except (OSError, BrokenProcessPool) as e:  # pas de /dev/shm ni de socket Unix : un seul processus
            print(f"Analyse sur un seul processus ({e})", file=sys.stderr)
            self.counters = (Counter(), Counter())
            init_worker(self.foreign, self.counters)
            todo = [(self.root, hint, root)]
            while todo:
                batch, todo = todo[-64:], todo[:-64]
                todo += self._graft(scan_batch([(p, h) for p, h, _ in batch], self.budget, self.known,
                                               self.cat_floors), [d for *_, d in batch])
            return root
        with pool:
            pending = {pool.submit(scan_batch, [(self.root, hint)], 2_000, 0): [root]}
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    left = self._graft(fut.result(), pending.pop(fut))
                    # Peu de tâches en attente : petites tâches pour occuper vite tous les cœurs.
                    starving = len(pending) < 2 * self.procs
                    size, budget = (8, 2_000) if starving else (64, self.budget)
                    for i in range(0, len(left), size):
                        batch = left[i:i + size]
                        fut = pool.submit(scan_batch, [(p, h) for p, h, _ in batch], budget, self.known,
                                          dict(self.cat_floors))
                        pending[fut] = [d for *_, d in batch]
        return root

    def _graft(self, res, targets):
        """Raccroche le résultat d'une tâche à l'arbre ; renvoie les dossiers restant à explorer."""
        names, parents = res["names"], res["parents"]
        nodes = list(targets) + [Dir(n) for n in names[len(targets):]]
        for i in range(len(targets), len(nodes)):
            nodes[i].parent = nodes[parents[i]]
            nodes[parents[i]].dirs.append(nodes[i])
        for d, s, c in zip(nodes, res["fsizes"], res["fcounts"]):
            d.fsize, d.fcount = s, c
        for i, cat in res["hints"].items():
            nodes[i].cat = cat
        for hint, exts in res["exts"].items():
            for x, (s, c) in exts.items():
                e = self.exts.setdefault((hint, x), [0, 0])
                e[0] += s
                e[1] += c
        # Un même fichier à liens multiples a pu être vu par deux tâches : on retire le doublon.
        dup = set()
        for key, size, idx, name, x, hint in res["links"]:
            if key in self.seen:
                nodes[idx].fsize -= size
                nodes[idx].fcount -= 1
                self.exts[hint, x][0] -= size
                self.exts[hint, x][1] -= 1
                dup.add((idx, name))
            else:
                self.seen.add(key)
        self.known += sum(d.fsize for d in nodes)  # doublons déjà retirés
        self.cands += ((s, n, nodes[i]) for s, n, i in res["cands"] if (i, n) not in dup)
        if len(self.cands) > 200_000:
            floor = self.known // DETAIL
            self.cands = [c for c in self.cands if c[0] >= floor]
        for cat, items in res["top"].items():
            heap = self.top.setdefault(cat, [])
            for s, n, i in items:
                if (i, n) in dup:
                    continue
                entry = (s, next(self.tie), n, nodes[i])
                if len(heap) < TOP_PER_CAT:
                    heapq.heappush(heap, entry)
                elif s > heap[0][0]:
                    heapq.heappushpop(heap, entry)
            if len(heap) >= TOP_PER_CAT:
                self.cat_floors[cat] = heap[0][0]
        self.errors += res["errors"]
        return [(p, h, nodes[i]) for p, i, h in res["left"]]

    def category_files(self):
        """{catégorie: [[chemin, octets], …]} du plus gros au plus petit."""
        return {cat: [[os.path.join(d.path(), n), s] for s, _, n, d in sorted(heap, reverse=True)]
                for cat, heap in self.top.items()}

    def extensions(self):
        merged = {}
        for (_, raw), (s, n) in self.exts.items():
            m = merged.setdefault(raw.lower() if raw else "(aucune)", [0, 0])
            m[0] += s
            m[1] += n
        return sorted(([e, s, n] for e, (s, n) in merged.items() if n), key=lambda x: -x[1])

    def categories(self):
        """[[catégorie, octets, nombre, [3 principales extensions]]], par taille décroissante."""
        cats, top = {}, {}
        for (hint, raw), (s, n) in self.exts.items():
            ext = raw.lower()
            cat = hint or EXT_CAT.get(ext, "Autres")
            c = cats.setdefault(cat, [0, 0])
            c[0] += s
            c[1] += n
            t = top.setdefault(cat, {})
            t[ext or "(aucune)"] = t.get(ext or "(aucune)", 0) + s
        return sorted(([c, s, n, sorted(top[c], key=top[c].get, reverse=True)[:3]]
                       for c, (s, n) in cats.items() if n), key=lambda x: -x[1])

    def tree(self, root):
        """Calcule les totaux puis construit l'arbre élagué envoyé à l'interface."""
        order, stack = [], [root]
        while stack:
            d = stack.pop()
            order.append(d)
            stack.extend(d.dirs)
        for d in reversed(order):
            d.total = d.fsize + sum(c.total for c in d.dirs)
        # Le compteur de progression a pu compter deux fois un fichier à liens multiples
        # vu par deux tâches : le nombre exact se lit dans l'arbre.
        self.file_count = sum(d.fcount for d in order)
        min_size = max(root.total // DETAIL, 4096)
        for size, name, d in self.cands:
            if size >= min_size:
                if d.kept is None:
                    d.kept = []
                d.kept.append((name, size))
        return build(root, min_size)


def build(d, min_size):
    """{"n", "s", "c"} où les éléments < min_size sont fusionnés en
    {"n": "‹N petits éléments›", "s": somme, "x": N}."""
    kids, small = [], []
    for c in d.dirs:
        (kids if c.total >= min_size else small).append(c)
    kids = [build(c, min_size) for c in kids]
    kept = d.kept or ()
    kids += ({"n": n, "s": s} for n, s in kept)
    n_small = len(small) + d.fcount - len(kept)
    if n_small == 1 and small:
        kids.append(build(small[0], min_size))  # un seul : on garde son nom
    elif n_small:
        s_small = sum(c.total for c in small) + d.fsize - sum(s for _, s in kept)
        label = "‹1 petit élément›" if n_small == 1 else f"‹{n_small} petits éléments›"
        kids.append({"n": label, "s": s_small, "x": n_small})
    node = {"n": d.name, "s": d.total, "c": kids}
    if d.cat:
        node["k"] = d.cat  # l'interface propage la catégorie à tout le sous-arbre
    return node


def places():
    """Emplacements proposés au démarrage : dossier perso + un par disque monté."""
    home = os.path.expanduser("~")
    out = [{"label": "Dossier personnel", "path": home}]
    seen = set()
    for mnt, src, *_ in read_mounts():
        if (not src.startswith("/dev/") or src.startswith("/dev/loop") or src in seen
                or mnt.startswith(("/boot", "/efi")) or "/." in mnt):
            continue
        seen.add(src)
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        label = "Disque système" if mnt == "/" else os.path.basename(mnt) or mnt
        out.append({"label": label, "path": mnt, "total": total,
                    "used": total - st.f_bfree * st.f_frsize})
    return out


def pick_directory(title="Dossier à analyser", start=None):
    """Ouvre le sélecteur de dossier natif (KDE, sinon GTK)."""
    start = start if start and os.path.isdir(start) else os.path.expanduser("~")
    for cmd in (["kdialog", "--title", title, "--getexistingdirectory", start],
                ["zenity", "--file-selection", "--directory", f"--title={title}", f"--filename={start}/"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except OSError:
            continue
        return r.stdout.strip() or None if r.returncode == 0 else None
    return None


def show_in_file_manager(path):
    """Ouvre le gestionnaire de fichiers sur l'élément (sélectionné si possible)."""
    uri = Path(path).as_uri()
    try:
        r = subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
             "--type=method_call", "/org/freedesktop/FileManager1",
             "org.freedesktop.FileManager1.ShowItems", f"array:string:{uri}", "string:"],
            capture_output=True, timeout=5)
        if r.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    target = path if os.path.isdir(path) else os.path.dirname(path)
    subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------- Opérations sur les fichiers ----------
def checked_target(path, root):
    """Chemin normalisé d'un élément modifiable, sinon ValueError. Jamais la racine analysée,
    jamais en dehors, jamais un point de montage (on effacerait un autre disque)."""
    p = os.path.normpath(path)
    if not root or not within(p, root):
        raise ValueError("en dehors du dossier analysé")
    if p == root:
        raise ValueError("c'est le dossier analysé lui-même")
    if not os.path.lexists(p):
        raise ValueError("n'existe plus")
    if os.path.ismount(p):
        raise ValueError("c'est un point de montage")
    return p


def to_trash(p):
    """Corbeille freedesktop (celle de Dolphin), restaurable."""
    errors = []
    for cmd in (["gio", "trash", "--", p], ["kioclient", "move", p, "trash:/"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(str(e))
            continue
        if r.returncode == 0 and not os.path.lexists(p):
            return
        errors.append(r.stderr.strip())
    raise OSError(errors[0] or "impossible de mettre à la corbeille")


def file_op(op, paths, root, dest=None, name=None):
    """Applique une opération à chaque chemin ; renvoie un résultat par chemin."""
    results = []
    for raw in paths:
        res = {"path": raw, "ok": False}
        try:
            p = checked_target(raw, root)
            if op == "trash":
                to_trash(p)
            elif op == "delete":
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)  # ne suit pas les liens symboliques
                else:
                    os.unlink(p)
            elif op == "move":
                d = os.path.normpath(dest or "")
                if not os.path.isdir(d):
                    raise ValueError("dossier de destination introuvable")
                if within(d, p):
                    raise ValueError("impossible de déplacer un dossier dans lui-même")
                target = os.path.join(d, os.path.basename(p))
                if os.path.lexists(target):
                    raise ValueError(f"« {os.path.basename(p)} » existe déjà dans la destination")
                shutil.move(p, target)
                res["newPath"] = target
            elif op == "rename":
                if not name or "/" in name or "\0" in name or name in (".", ".."):
                    raise ValueError("nom invalide")
                target = os.path.join(os.path.dirname(p), name)
                if os.path.lexists(target):
                    raise ValueError("un élément porte déjà ce nom")
                os.rename(p, target)
                res["newPath"] = target
            elif op == "open":
                subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                raise ValueError("opération inconnue")
            res["ok"] = True
        except (OSError, ValueError, shutil.Error) as e:
            res["error"] = (e.strerror if isinstance(e, OSError) and e.strerror else str(e))
        results.append(res)
    return results


# ---------- Miniatures ----------
THUMB_IMG = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "avif"}
THUMB_VID = set(dict((n, e) for n, _, e in CATEGORIES)["Vidéos"].split())
_thumbs = {}                        # chemin -> (mtime, octets, type MIME)
_thumb_slots = threading.Semaphore(4)  # au plus 4 décodages en parallèle


def thumbnail(path):
    """Miniature ~160 px : celle que Dolphin a déjà mise en cache si elle existe,
    sinon générée (Pillow pour les images, ffmpegthumbnailer pour les vidéos)."""
    mtime = os.stat(path).st_mtime
    if (hit := _thumbs.get(path)) and hit[0] == mtime:
        return hit[1], hit[2]
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    digest = hashlib.md5(Path(path).as_uri().encode()).hexdigest()
    data = ctype = None
    for size in ("normal", "large", "x-large"):
        f = os.path.join(cache, "thumbnails", size, digest + ".png")
        if os.path.exists(f) and os.stat(f).st_mtime >= mtime:
            data, ctype = Path(f).read_bytes(), "image/png"
            break
    if data is None:
        ext = path.rsplit(".", 1)[-1].lower()
        with _thumb_slots:
            if ext in THUMB_IMG:
                from PIL import Image, ImageOps
                with Image.open(path) as im:
                    im.draft("RGB", (320, 320))  # décodage JPEG réduit : bien plus rapide
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((160, 160))
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=80)
                data = buf.getvalue()
            elif ext in THUMB_VID:
                r = subprocess.run(["ffmpegthumbnailer", "-i", path, "-o", "-", "-s", "160", "-c", "jpeg"],
                                   capture_output=True, timeout=20)
                data = r.stdout or None
            if not data:
                raise ValueError("pas de miniature")
            ctype = "image/jpeg"
    _thumbs[path] = (mtime, data, ctype)
    if len(_thumbs) > 800:
        _thumbs.pop(next(iter(_thumbs)))
    return data, ctype


# ---------- Préférences ----------
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")) / "linstat" / "config.json"


def load_config():
    try:
        cfg = json.loads(CONFIG.read_text())
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(changes):
    """Seules des clés connues, aux valeurs vérifiées, sont enregistrées."""
    theme = changes.get("theme")
    if not (isinstance(theme, str) and re.fullmatch(r"[a-z]{1,20}", theme)):
        raise ValueError("thème invalide")
    cfg = load_config()
    cfg["theme"] = theme
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG)  # remplacement atomique : jamais de fichier à moitié écrit


class State:
    def __init__(self, one_fs):
        self.one_fs = one_fs
        self.lock = threading.Lock()
        self.payload = None
        self.root = None
        self.scanner = None
        self.dirty = False  # une opération a modifié le disque depuis l'analyse

    def scan(self, path):
        with self.lock:
            t0 = time.time()
            print(f"Analyse de {path}…", file=sys.stderr, flush=True)
            sc = self.scanner = Scanner(path, self.one_fs)
            tree = sc.tree(sc.run())
            data = {"tree": tree, "exts": sc.extensions()[:300], "cats": sc.categories(),
                    "catColors": {name: color for name, color, _ in CATEGORIES}, "catByExt": EXT_CAT,
                    "catFiles": sc.category_files(), "home": os.path.expanduser("~"),
                    "files": sc.file_count,
                    "errors": sc.errors, "seconds": round(time.time() - t0, 1)}
            raw = json.dumps(data, separators=(",", ":")).encode()
            self.payload = gzip.compress(raw, compresslevel=5)
            self.root = sc.root
            self.dirty = False
            print(f"  {sc.file_count:,} fichiers, {fmt(tree['s'])} en {data['seconds']} s"
                  f" ({sc.errors} erreurs d'accès) — JSON {fmt(len(raw))}", file=sys.stderr)


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", gz=False):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body, ctype = body.encode(), "text/plain; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            # Contre le « DNS rebinding » : un site piégé qui se fait résoudre en 127.0.0.1
            # enverrait un autre Host. Sans ce contrôle il pourrait lire l'arbre des fichiers.
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_address[1]}"

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, "")
            url = urlparse(self.path)
            path = url.path
            if path == "/":
                self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, {"scanned": state.payload is not None, "dirty": state.dirty, "root": state.root})
            elif path == "/api/tree" and state.payload:
                self._send(200, state.payload, gz=True)
            elif path == "/api/progress":
                sc = state.scanner
                self._send(200, {"files": sc.files if sc else 0, "size": sc.total if sc else 0})
            elif path == "/api/places":
                self._send(200, places())
            elif path == "/api/config":
                self._send(200, load_config())
            elif path == "/api/thumb" and state.root:
                p = os.path.normpath(parse_qs(url.query).get("path", [""])[0])
                try:
                    if not within(p, state.root) or not os.path.isfile(p):
                        raise ValueError
                    data, ctype = thumbnail(p)
                except Exception:
                    return self._send(404, "")
                self._send(200, data, ctype)
            else:
                self._send(404, "")

        def do_POST(self):
            # L'en-tête personnalisé force un « preflight » CORS que l'on ne valide
            # jamais : un autre site ouvert dans le navigateur ne peut pas appeler ceci.
            if self.headers.get("X-LinStat") != "1" or not self._host_ok():
                return self._send(403, "")
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n)) if n else {}
            if self.path == "/api/scan":
                target = os.path.expanduser(body.get("path", ""))
                if not os.path.isdir(target):
                    return self._send(400, f"Pas un dossier : {target}")
                try:
                    state.scan(target)
                except Exception as e:
                    return self._send(500, f"{type(e).__name__} : {e}")
                self._send(200, state.payload, gz=True)
            elif self.path == "/api/config":
                try:
                    save_config(body)
                except ValueError as e:
                    return self._send(400, str(e))
                self._send(204, b"")
            elif self.path == "/api/pick":
                self._send(200, {"path": pick_directory(body.get("title") or "Dossier à analyser",
                                                        body.get("start"))})
            elif self.path == "/api/fileop":
                paths = body.get("paths")
                if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                    return self._send(400, "paths attendu")
                results = file_op(body.get("op"), paths, state.root, body.get("dest"), body.get("name"))
                if body.get("op") != "open" and any(r["ok"] for r in results):
                    state.dirty = True  # l'analyse en mémoire ne correspond plus au disque
                self._send(200, {"results": results})
            elif self.path == "/api/open" and state.root:
                # normpath et non realpath : un lien symbolique du dossier analysé
                # peut pointer ailleurs, il reste légitime de l'afficher.
                p = os.path.normpath(body.get("path", ""))
                if not within(p, state.root):
                    return self._send(403, "")
                show_in_file_manager(p)
                self._send(204, b"")
            else:
                self._send(404, "")

    return Handler


def run_window(url, path):
    """Affiche l'interface dans une vraie fenêtre GTK4 + WebKit. Lève ImportError/ValueError
    si les bibliothèques manquent."""
    # Évite la fenêtre blanche de WebKitGTK avec certains pilotes (NVIDIA + Wayland)
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gio, GLib, Gtk, WebKit

    app = Gtk.Application(application_id=APP_ID)
    app.register(None)
    if app.get_is_remote():
        # LinStat est déjà ouvert : on lui demande d'analyser ce dossier et on s'arrête.
        if path:
            app.activate_action("scan", GLib.Variant("s", path))
        app.run([])  # transmet « activate » (fenêtre au premier plan) puis rend la main proprement
        return

    view = None

    def on_scan(action, param):
        if view:
            view.evaluate_javascript(f"scan({json.dumps(param.get_string())})", -1, None, None, None, None)

    action = Gio.SimpleAction.new("scan", GLib.VariantType.new("s"))
    action.connect("activate", on_scan)
    app.add_action(action)

    def on_activate(app):
        nonlocal view
        if app.get_active_window():
            return app.get_active_window().present()
        win = Gtk.ApplicationWindow(application=app, title="LinStat")
        win.set_default_size(1280, 840)
        win.set_icon_name("linstat")
        view = WebKit.WebView()
        view.load_uri(url + (f"?scan={quote(path)}" if path else ""))
        win.set_child(view)
        win.present()

    app.connect("activate", on_activate)
    app.run([])


def main():
    ap = argparse.ArgumentParser(description="Visualise l'occupation disque façon WinDirStat.")
    ap.add_argument("path", nargs="?", help="dossier à analyser tout de suite (sinon : choix au démarrage)")
    ap.add_argument("--port", type=int, default=0, help="port local (aléatoire par défaut)")
    ap.add_argument("--cross-fs", action="store_true",
                    help="descendre aussi dans les autres disques montés")
    ap.add_argument("--browser", action="store_true", help="ouvrir dans le navigateur au lieu d'une fenêtre")
    ap.add_argument("--no-window", action="store_true", help="serveur seul (pour le développement)")
    args = ap.parse_args()
    path = os.path.abspath(os.path.expanduser(args.path)) if args.path else None
    if path and not os.path.isdir(path):
        sys.exit(f"Pas un dossier : {args.path}")

    sys.setrecursionlimit(10_000)  # build() descend aussi profond que l'arborescence
    state = State(one_fs=not args.cross_fs)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    # L'analyse est lancée par la page elle-même, pour que la progression s'affiche.
    start_url = url + (f"?scan={quote(path)}" if path else "")
    print(f"Interface : {start_url}", file=sys.stderr, flush=True)

    if not (args.browser or args.no_window):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            return run_window(url, path)  # la fenêtre fermée, le programme s'arrête
        except (ImportError, ValueError) as e:
            print(f"Fenêtre native indisponible ({e}), ouverture dans le navigateur.", file=sys.stderr)
            server.shutdown()
    if not args.no_window:
        webbrowser.open(start_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
