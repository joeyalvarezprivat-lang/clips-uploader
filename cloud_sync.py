#!/usr/bin/env python3
"""Cloud-Betrieb für den ClipsUploader.

Die Warteschlange und die Videos liegen im Dropbox-App-Ordner unter /cloud.
GitHub Actions führt alle ~10 Minuten `run` aus, damit Joeys Mac nachts aus sein kann.

  push         (Mac)    neue/lokale Jobs + Videos nach Dropbox /cloud schieben, lokal ins Archiv verschieben
  pull-status  (Mac)    Status, Log und Fehler aus der Cloud holen (für _auftrag/ und TikTok-Übersicht)
  run          (Cloud)  Warteschlange laden, `publish.py tick` ausführen, Ergebnis zurückschreiben, aufräumen
  zeit ID "2026-11-17 15:00"   (Mac) Termin eines Cloud-Jobs ändern
"""
import json, os, re, subprocess, sys, tempfile, shutil
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import publish as P  # noqa: E402

ROOT = "/cloud"
Q = ROOT + "/queue"
DONE = ROOT + "/done"
FILES = ROOT + "/files"


_GEHEIM = [
    (re.compile(r"(access_token|client_secret|refresh_token|fb_exchange_token|code)=([^&\s\"']+)"), r"\1=***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    (re.compile(r"\b(EAA[A-Za-z0-9]{20,}|ya29\.[A-Za-z0-9._\-]+|sl\.[A-Za-z0-9._\-]{20,}|IG[A-Za-z0-9]{40,})"), "***"),
    (re.compile(r"\"(access_token|refresh_token|client_secret|page_token|token|app_secret)\"\s*:\s*\"[^\"]+\""), r'"\1": "***"'),
]


def schwaerzen(text):
    """Zugangsdaten aus Ausgaben entfernen (die Actions-Logs eines öffentlichen Repos sind sichtbar)."""
    for rx, ersatz in _GEHEIM:
        text = rx.sub(ersatz, text)
    return text


# ---------------------------------------------------------------- Dropbox-Grundfunktionen
def _h(z):
    return {"Authorization": "Bearer " + P.dbx_token(z)}


def liste(z, pfad):
    try:
        js = P.jhttp("POST", P.DBX_API + "/2/files/list_folder", headers=_h(z), body={"path": pfad, "recursive": False})
    except P.ApiFehler as e:
        if e.status == 409:
            return []
        raise
    out = list(js.get("entries", []))
    while js.get("has_more"):
        js = P.jhttp("POST", P.DBX_API + "/2/files/list_folder/continue", headers=_h(z), body={"cursor": js["cursor"]})
        out += js.get("entries", [])
    return [e for e in out if e.get(".tag") == "file"]


def lade(z, pfad):
    st, hd, data = P.anfrage("POST", P.DBX_CONTENT + "/2/files/download", None, {
        **_h(z), "Dropbox-API-Arg": json.dumps({"path": pfad})}, timeout=600)
    if st == 409:
        return None
    if st != 200:
        raise P.ApiFehler(st, P._js(data), pfad)
    return data


def schreibe_bytes(z, pfad, data):
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(data)
    tmp.close()
    try:
        P.dbx_hochladen(z, tmp.name, pfad)
    finally:
        os.unlink(tmp.name)


def verschiebe(z, von, nach):
    try:
        P.jhttp("POST", P.DBX_API + "/2/files/move_v2", headers=_h(z),
                body={"from_path": von, "to_path": nach, "autorename": True})
    except P.ApiFehler as e:
        P.log(f"(verschieben {von}: {e})")


def loesche(z, pfad):
    try:
        P.jhttp("POST", P.DBX_API + "/2/files/delete_v2", headers=_h(z), body={"path": pfad})
    except P.ApiFehler as e:
        if e.status != 409:
            raise


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")


# ---------------------------------------------------------------- Mac: push
def cmd_push(args):
    z = P.zugang()
    if "dropbox" not in z:
        sys.exit("Dropbox nicht eingerichtet")
    archiv = os.path.join(P.CLIPS, "_auftrag", "upload_cloud")
    os.makedirs(archiv, exist_ok=True)
    jobs = P.alle_jobs()
    for job in jobs:
        datei = P.datei_von(job)
        rel = job["datei"] if not os.path.isabs(job["datei"]) else "fertig/" + os.path.basename(job["datei"])
        job["datei"] = rel
        if os.path.exists(datei):
            P.dbx_hochladen(z, datei, f"{FILES}/{rel}")
        schreibe_bytes(z, f"{Q}/{job['id']}.json", jdump(job))
        os.replace(P.job_pfad(job["id"]), os.path.join(archiv, job["id"] + ".json"))
        P.log(f"☁ {job['id']} in die Cloud übertragen")


# ---------------------------------------------------------------- Mac: pull-status
def cmd_pull_status(args):
    z = P.zugang()
    if "dropbox" not in z:
        return
    for name, ziel in (("status.txt", P.STATUS_TXT), ("FEHLER.txt", P.FEHLER_TXT),
                       ("log.txt", os.path.join(P.CLIPS, "_auftrag", "upload_log_cloud.txt"))):
        data = lade(z, f"{ROOT}/{name}")
        if data is not None:
            with open(ziel, "wb") as f:
                f.write(data)
    jobs = []
    for e in liste(z, Q):
        data = lade(z, e["path_lower"])
        if data:
            jobs.append(json.loads(data))
    jobs.sort(key=lambda j: j["zeit"])
    try:
        P.schreibe_tiktok_uebersicht(jobs)
    except Exception as ex:
        P.log(f"(TikTok-Übersicht: {ex})")


def cmd_zeit(args):
    jid, zeit = args[0], args[1]
    z = P.zugang()
    data = lade(z, f"{Q}/{jid}.json")
    if not data:
        sys.exit(f"Nicht in der Cloud-Warteschlange: {jid}")
    job = json.loads(data)
    if P._fixiert(job):
        sys.exit("Schon geplant/veröffentlicht – Termin nicht mehr änderbar.")
    job["zeit"] = P.iso(P.parse_zeit(zeit))
    schreibe_bytes(z, f"{Q}/{jid}.json", jdump(job))
    print(f"{jid} -> {P.parse_zeit(zeit).astimezone(P.TZ):%d.%m. %H:%M}")


# ---------------------------------------------------------------- Cloud: run
def _bald(d, minuten=20):
    n = d.get("naechster_versuch")
    return not n or P.parse_zeit(n) <= P.jetzt() + P.timedelta(minutes=minuten)


def braucht_datei(job):
    for p in ("youtube", "instagram", "facebook"):
        d = job.get(p) or {}
        if d.get("aktiv") and d.get("status") == "wartet" and _bald(d):
            return True
    return False


def wartet_irgendwo(job):
    return any((job.get(p) or {}).get("aktiv") and (job.get(p) or {}).get("status") == "wartet"
               for p in ("youtube", "instagram", "facebook"))


def fertig(job):
    for p in ("youtube", "instagram", "facebook"):
        d = job.get(p) or {}
        if d.get("aktiv") and d.get("status") not in ("veroeffentlicht", "geplant", "fehler"):
            return False
    return True


LOCK = ROOT + "/lock.json"


def sperre_nehmen(z, wer):
    alt = lade(z, LOCK)
    if alt:
        try:
            d = json.loads(alt)
            if (P.jetzt() - P.parse_zeit(d["seit"])).total_seconds() < 50 * 60:
                return False
        except Exception:
            pass
    schreibe_bytes(z, LOCK, jdump({"wer": wer, "seit": P.iso(P.jetzt())}))
    return True


def letzter_lauf_min(z):
    d = lade(z, f"{ROOT}/letzter_lauf.txt")
    if not d:
        return 10 ** 6
    return (P.jetzt() - P.parse_zeit(d.decode().strip())).total_seconds() / 60


def cmd_run(args):
    """args: [] in der Cloud; ["--ersatz"] auf dem Mac: nur laufen, wenn die Cloud seit > 20 Min. nicht lief."""
    zpfad = os.environ.get("UPLOAD_ZUGANG") or P.ZUGANG
    z = P.zugang()
    wer = "mac" if "--ersatz" in args else "github"
    if wer == "mac" and letzter_lauf_min(z) < 20:
        return
    if not sperre_nehmen(z, wer):
        print("Anderer Lauf aktiv – übersprungen")
        return
    try:
        _run(z, zpfad)
        schreibe_bytes(z, f"{ROOT}/letzter_lauf.txt", P.iso(P.jetzt()).encode())
        if wer == "mac":
            P.log("(Ersatzlauf auf dem Mac – GitHub lief nicht)")
    finally:
        loesche(z, LOCK)


def _run(z, zpfad):
    arbeit = tempfile.mkdtemp(prefix="clips_")
    qdir = os.path.join(arbeit, "_auftrag", "upload")
    os.makedirs(qdir)
    vorher = {}
    for e in liste(z, Q):
        data = lade(z, e["path_lower"])
        if not data:
            continue
        job = json.loads(data)
        vorher[job["id"]] = data
        with open(os.path.join(qdir, job["id"] + ".json"), "wb") as f:
            f.write(data)
        if braucht_datei(job):
            ziel = os.path.join(arbeit, job["datei"])
            os.makedirs(os.path.dirname(ziel), exist_ok=True)
            vid = lade(z, f"{FILES}/{job['datei']}")
            if vid:
                with open(ziel, "wb") as f:
                    f.write(vid)
        elif wartet_irgendwo(job):
            # Video wird erst später gebraucht (z. B. YouTube-Limit, nächster Versuch in >20 Min):
            # Platzhalter, damit tick den Job nicht als "Datei fehlt" verwirft. Kein Schritt ist jetzt fällig.
            ziel = os.path.join(arbeit, job["datei"])
            os.makedirs(os.path.dirname(ziel), exist_ok=True)
            open(ziel, "wb").close()
    if not vorher:
        print("Warteschlange leer")
        return
    env = dict(os.environ, CLIPS_DIR=arbeit, UPLOAD_ZUGANG=zpfad)
    r = subprocess.run([sys.executable, os.path.join(HERE, "publish.py"), "tick"], env=env,
                       capture_output=True, text=True, timeout=3000)
    ausgabe = schwaerzen((r.stdout or "") + (r.stderr or ""))
    print(ausgabe)
    z = P.zugang()   # evtl. erneuerte Tokens (nur im Speicher dieses Laufs)
    for jid, alt in vorher.items():
        p = os.path.join(qdir, jid + ".json")
        neu = open(p, "rb").read()
        if neu != alt:
            schreibe_bytes(z, f"{Q}/{jid}.json", neu)
        job = json.loads(neu)
        if fertig(job):
            verschiebe(z, f"{Q}/{jid}.json", f"{DONE}/{jid}.json")
            loesche(z, f"{FILES}/{job['datei']}")
            P.log(f"🧹 {jid} fertig – Video aus der Cloud gelöscht")
    for name, quelle in (("status.txt", os.path.join(arbeit, "_auftrag", "upload_status.txt")),
                         ("FEHLER.txt", os.path.join(arbeit, "_auftrag", "upload_FEHLER.txt"))):
        if os.path.exists(quelle):
            neu = open(quelle, "rb").read()
            if name == "FEHLER.txt":
                alt = lade(z, f"{ROOT}/{name}") or b""
                neu = alt + neu
            schreibe_bytes(z, f"{ROOT}/{name}", neu)
    zeilen = [l for l in ausgabe.splitlines() if l.startswith("[")]
    if zeilen:
        alt = (lade(z, f"{ROOT}/log.txt") or b"").decode("utf-8", "replace").splitlines()[-3000:]
        schreibe_bytes(z, f"{ROOT}/log.txt", ("\n".join(alt + zeilen) + "\n").encode("utf-8"))
    shutil.rmtree(arbeit, ignore_errors=True)


def cmd_stopp(args):
    """Clips nicht (weiter) veröffentlichen: offene Instagram-Posts abbrechen, geplante Facebook-Reels löschen.
    Geplante YouTube-Shorts kann der Zugang nicht ändern (nur Upload-Rechte) -> wird gemeldet."""
    z = P.zugang()
    for jid in args:
        data = lade(z, f"{Q}/{jid}.json")
        if not data:
            print(f"{jid}: nicht in der Warteschlange")
            continue
        job = json.loads(data)
        ig = job.get("instagram") or {}
        if ig.get("aktiv") and ig.get("status") != "veroeffentlicht":
            if ig.get("dropbox_pfad"):
                loesche(z, ig["dropbox_pfad"])
            ig.update(aktiv=False, gestoppt=P.iso(P.jetzt()))
            print(f"{jid}: Instagram gestoppt")
        fb = job.get("facebook") or {}
        if fb.get("aktiv") and fb.get("status") in ("wartet", "hochladen"):
            fb.update(aktiv=False, gestoppt=P.iso(P.jetzt()))
            print(f"{jid}: Facebook gestoppt")
        elif fb.get("aktiv") and fb.get("status") == "geplant" and fb.get("video_id"):
            try:
                f = P.fb_zugang(z)
                P.jhttp("DELETE", f"{P.FB}/{P.FB_VERSION}/{fb['video_id']}?access_token=" + P.urllib.parse.quote(f["page_token"]))
                fb.update(status="geloescht", aktiv=False, gestoppt=P.iso(P.jetzt()))
                print(f"{jid}: geplantes Facebook-Reel gelöscht")
            except P.ApiFehler as e:
                print(f"{jid}: Facebook-Löschen fehlgeschlagen: {schwaerzen(str(e))}")
        yt = job.get("youtube") or {}
        if yt.get("aktiv") and yt.get("status") == "wartet":
            yt.update(aktiv=False, gestoppt=P.iso(P.jetzt()))
            print(f"{jid}: YouTube gestoppt")
        elif yt.get("status") == "geplant":
            print(f"{jid}: YouTube ist schon bei YouTube eingeplant ({yt.get('url')}) – in YouTube Studio auf privat stellen")
        job["gestoppt"] = True
        schreibe_bytes(z, f"{Q}/{jid}.json", jdump(job))
        verschiebe(z, f"{Q}/{jid}.json", f"{DONE}/{jid}.json")
        loesche(z, f"{FILES}/{job['datei']}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    try:
        {"push": cmd_push, "pull-status": cmd_pull_status, "run": cmd_run, "zeit": cmd_zeit, "stopp": cmd_stopp}[cmd](args)
    except SystemExit:
        raise
    except BaseException as e:   # nie Rohdaten (URLs mit Tokens) ins öffentliche Log
        print("FEHLER: " + schwaerzen(f"{type(e).__name__}: {e}"))
        sys.exit(1)


if __name__ == "__main__":
    main()
