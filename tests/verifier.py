#!/usr/bin/env python3
"""Vérifie LinStat sans écran, dans un dossier temporaire jetable.

    python3 tests/verifier.py

- totaux exacts face à une référence naïve (os.walk + inodes déjà vus), y compris avec des
  liens physiques vus par deux tâches différentes et une arborescence très profonde ;
- extensions et catégories qui retombent sur le total ;
- règles de catégories par chemin et leurs priorités ;
- opérations sur les fichiers : ce qui doit marcher, et tout ce qui doit être refusé.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import linstat  # noqa: E402

echecs = 0


def verifie(ok, message):
    global echecs
    print(("  ok    " if ok else "  ÉCHEC ") + message)
    echecs += not ok


def reference(root):
    seen, total, files = set(), 0, 0
    for dp, _, fns in os.walk(root):
        for f in fns:
            st = os.lstat(os.path.join(dp, f))
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                total += st.st_blocks * 512
                files += 1
    return total, files


def arborescence(t):
    os.makedirs(f"{t}/a")
    os.makedirs(f"{t}/b")
    for i in range(1, 200):
        with open(f"{t}/a/f{i}.dat", "wb") as f:
            f.write(os.urandom(i * 137))
        os.link(f"{t}/a/f{i}.dat", f"{t}/b/lien{i}.dat")  # liens physiques
    with open(f"{t}/modele.safetensors", "wb") as f:
        f.write(os.urandom(50_000))
    with open(f"{t}/archive.", "w") as f:
        f.write("x")  # « archive. » n'a pas d'extension
    p = f"{t}/profond"
    os.mkdir(p)
    for _ in range(1200):  # chemin d'environ 2 400 caractères
        p = os.path.join(p, "d")
        os.mkdir(p)
    with open(f"{p}/fond.txt", "w") as f:
        f.write("x" * 10_000)


def analyse(t):
    print("Analyse")
    ref = reference(t)
    # budget minuscule : les doublons de liens tombent dans des tâches différentes
    for procs, budget in ((1, 50), (4, 50), (8, linstat.BUDGET)):
        sc = linstat.Scanner(t, procs=procs, budget=budget)
        tree = sc.tree(sc.run())
        verifie((tree["s"], sc.file_count) == ref,
                f"{procs} processus, budget {budget} : {tree['s']} octets, {sc.file_count} fichiers (réf. {ref})")
    exts = dict((e, s) for e, s, _ in sc.extensions())
    verifie(sum(exts.values()) == ref[0], "la somme des extensions égale le total")
    verifie(sum(c[1] for c in sc.categories()) == ref[0], "la somme des catégories égale le total")
    verifie("safetensors" in exts, "« .safetensors » est reconnu comme extension")
    cats = {c[0] for c in sc.categories()}
    verifie("Modèles IA" in cats, "le .safetensors est classé « Modèles IA »")


def regles():
    print("Catégories par chemin")
    home = os.path.expanduser("~")
    cas = {
        f"{home}/Games": "Jeux",
        f"{home}/.local/share/Steam/steamapps/common": "Jeux",
        f"{home}/.cache/huggingface": "Modèles IA",
        "/usr/share/ollama/.ollama/models": "Modèles IA",
        f"{home}/projet/node_modules": "Code & dév.",
        f"{home}/.local/share/Trash": "Corbeille",
        "/usr": "Programmes & système",
    }
    for p, attendu in cas.items():
        verifie(linstat.path_hint(p, None) == attendu, f"{p} → {attendu}")
    verifie(linstat.path_hint(f"{home}/x/node_modules", "Modèles IA") == "Modèles IA",
            "une règle faible (Code) n'écrase pas une forte (IA)")
    verifie(linstat.path_hint(f"{home}/.cache/huggingface", "Caches") == "Modèles IA",
            "une règle forte (IA) écrase une faible (Caches)")
    verifie(linstat.path_hint("/x/.ollama/models", "Corbeille") == "Corbeille", "rien n'écrase la Corbeille")


def operations(t):
    print("Opérations sur les fichiers")
    os.makedirs(f"{t}/a/sous")
    os.makedirs(f"{t}/b")
    os.makedirs(f"{t}/garder")
    for p in ("a/f1.txt", "a/sous/f2.txt", "b/f3.txt", "garder/important.txt"):
        with open(f"{t}/{p}", "w") as f:
            f.write(p)
    os.symlink(f"{t}/garder", f"{t}/a/lien")

    def op(*args, **kw):
        return linstat.file_op(*args, t, **kw)[0]["ok"]

    verifie(op("rename", [f"{t}/a/f1.txt"], name="renommé.txt") and os.path.exists(f"{t}/a/renommé.txt"), "renommer")
    verifie(not op("rename", [f"{t}/a/renommé.txt"], name="x/y"), "refus : nom contenant /")
    verifie(not op("rename", [f"{t}/a/renommé.txt"], name="sous"), "refus : nom déjà pris")
    verifie(op("move", [f"{t}/a/sous"], dest=f"{t}/b") and os.path.exists(f"{t}/b/sous/f2.txt"), "déplacer")
    verifie(not op("move", [f"{t}/b"], dest=f"{t}/b/sous"), "refus : dossier déplacé dans lui-même")
    verifie(not op("move", [f"{t}/b/f3.txt"], dest=f"{t}/b"), "refus : destination déjà occupée")
    verifie(not op("delete", [t]), "refus : le dossier analysé lui-même")
    verifie(not op("delete", ["/etc/hostname"]), "refus : en dehors du dossier analysé")
    verifie(not op("delete", [f"{t}/a/../../ailleurs"]), "refus : évasion par ../")
    verifie(op("delete", [f"{t}/a/lien"]) and os.path.exists(f"{t}/garder/important.txt"),
            "supprimer un lien symbolique laisse sa cible intacte")
    verifie(op("delete", [f"{t}/a"]) and not os.path.exists(f"{t}/a"), "supprimer un dossier")
    verifie(not op("delete", [f"{t}/a"]), "refus : élément déjà supprimé")


def doublons(t):
    print("Doublons")
    mio = os.urandom(1 << 20)
    os.makedirs(f"{t}/photos/copie")
    os.makedirs(f"{t}/projet/node_modules/lib")
    for p in ("photos/a.jpg", "photos/copie/a.jpg", "photos/a-bis.jpg"):  # 3 copies identiques
        with open(f"{t}/{p}", "wb") as f:
            f.write(mio)
    # même taille, même début et même fin, milieu différent : l'empreinte rapide les confond
    piege = bytearray(mio)
    piege[len(piege) // 2] ^= 0xFF
    with open(f"{t}/photos/presque.jpg", "wb") as f:
        f.write(piege)
    os.link(f"{t}/photos/a.jpg", f"{t}/photos/lien-physique.jpg")  # même fichier, pas un doublon
    for p in ("petit1.txt", "petit2.txt"):  # sous la taille minimale
        with open(f"{t}/{p}", "w") as f:
            f.write("x" * 1000)
    for p in ("projet/node_modules/lib/x.js", "projet/node_modules/x.js"):  # copies « normales » de code
        with open(f"{t}/{p}", "wb") as f:
            f.write(mio[:200_000])

    sc = linstat.Scanner(t, procs=2, budget=3)
    sc.tree(sc.run())
    groupes = linstat.find_duplicates(sc.dupc, linstat.DUP_MIN, linstat.DUP_SKIP, {})
    noms = [sorted(os.path.relpath(f[0], t) for f in g["files"]) for g in groupes]
    verifie(len(groupes) == 1, f"un seul groupe en ignorant le code (trouvé : {noms})")
    verifie(len(groupes) == 1 and len(groupes[0]["files"]) == 3,
            "les 3 copies identiques, sans le lien physique ni le fichier presque identique")
    tout = linstat.find_duplicates(sc.dupc, linstat.DUP_MIN, set(), {})
    verifie(len(tout) == 2, "les copies de node_modules apparaissent quand on n'ignore rien")
    verifie(not any("petit" in f[0] for g in tout for f in g["files"]), "les petits fichiers sont ignorés")


def main():
    sys.setrecursionlimit(10_000)
    with tempfile.TemporaryDirectory() as t:
        doublons(t)
    with tempfile.TemporaryDirectory() as t:
        arborescence(t)
        analyse(t)
    regles()
    with tempfile.TemporaryDirectory() as t:
        operations(t)
    print("\nTout est bon." if not echecs else f"\n{echecs} échec(s).")
    sys.exit(1 if echecs else 0)


if __name__ == "__main__":
    main()
