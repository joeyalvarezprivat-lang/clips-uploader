"""Test für cloud_sync.py: Mac push -> Cloud run (publish tick gegen mock_server) -> Status/Aufräumen.
Die Dropbox-Ablage /cloud wird im Speicher nachgebildet; Plattformen über mock_server.py."""
import json, os, sys, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import mock_server as M

MAC = tempfile.mkdtemp(prefix="mac_")
os.makedirs(os.path.join(MAC, "fertig"))
os.makedirs(os.path.join(MAC, "_auftrag"))
ZPF = os.path.join(MAC, "zugang.json")
os.environ.update({"PUBLISH_MOCK": M.BASE, "CLIPS_DIR": MAC, "UPLOAD_ZUGANG": ZPF})
M.start()
import cloud_sync as C
P = C.P

STORE = {}
_orig_up = P.dbx_hochladen


def up(z, datei, ziel):
    if ziel.startswith("/cloud"):
        STORE[ziel] = open(datei, "rb").read()
    else:
        _orig_up(z, datei, ziel)


P.dbx_hochladen = up
C.liste = lambda z, pfad: [{".tag": "file", "path_lower": k} for k in sorted(STORE) if os.path.dirname(k) == pfad]
C.lade = lambda z, pfad: STORE.get(pfad)
C.verschiebe = lambda z, a, b: STORE.__setitem__(b, STORE.pop(a))
C.loesche = lambda z, p: STORE.pop(p, None)

ok = 0


def check(c, t):
    global ok
    print(("  ✅ " if c else "  ❌ ") + t)
    if not c:
        raise SystemExit(1)
    ok += 1


def clip(name, titel):
    p = os.path.join(MAC, "fertig", name + ".mp4")
    open(p, "wb").write(os.urandom(2 * 1024 * 1024))
    open(p[:-4] + ".txt", "w").write(f"TITEL: {titel}\n\nCAPTION:\nzeile\n\n#shorts\n\nQuelle: x\n")
    return p


z = {"youtube": {"client_id": "c", "client_secret": "s", "refresh_token": "RT"},
     "dropbox": {"app_key": "k", "app_secret": "s", "refresh_token": "DRT"},
     "instagram": {"token": "IG_LONG", "user_id": "17841400000", "username": "x", "erneuert": 9e12},
     "facebook": {"app_id": "A", "page_id": "PAGE1", "page_name": "S", "page_token": "PAGE_TOK"}}
P.speichere_zugang(z)
now = datetime.now(timezone.utc)
b = lambda dt: dt.astimezone(P.TZ).strftime("%Y-%m-%d %H:%M")
P.cmd_add([clip("40_alle", "Alle Plattformen"), "--zeit", b(now + timedelta(hours=2))])
P.cmd_add([clip("41_nur_yt", "Nur YouTube"), "--zeit", b(now + timedelta(hours=3)), "--nur", "youtube"])
P.cmd_add([clip("42_limit", "LIMIT-TEST"), "--zeit", b(now + timedelta(hours=4)), "--nur", "youtube"])
M.S["quota_titles"].add("LIMIT-TEST")

print("1) Mac: push")
C.cmd_push([])
check(not os.listdir(os.path.join(MAC, "_auftrag", "upload")), "lokale Warteschlange leer (Jobs in der Cloud)")
check(len(os.listdir(os.path.join(MAC, "_auftrag", "upload_cloud"))) == 3, "lokale Kopien im Archiv")
check("/cloud/files/fertig/40_alle.mp4" in STORE and "/cloud/queue/40_alle.json" in STORE, "Video + Job in Dropbox /cloud")

print("2) Cloud: run (anderes Arbeitsverzeichnis, Zugang aus Secret-Datei)")
os.environ["CLIPS_DIR"] = tempfile.mkdtemp(prefix="leer_")   # Cloud sieht den Mac-Ordner nicht
C.cmd_run([])
j40 = json.loads(STORE["/cloud/queue/40_alle.json"])
check(j40["youtube"]["status"] == "geplant", "YouTube geplant")
check(j40["facebook"]["status"] == "geplant", "Facebook geplant")
check(j40["instagram"]["status"] == "dropbox", "Instagram-Video im Dropbox-App-Ordner")
check("/cloud/done/41_nur_yt.json" in STORE and "/cloud/queue/41_nur_yt.json" not in STORE, "fertiger Job nach done/ verschoben")
check("/cloud/files/fertig/41_nur_yt.mp4" not in STORE, "Video des fertigen Jobs gelöscht")
j42 = json.loads(STORE["/cloud/queue/42_limit.json"])
check(j42["youtube"]["status"] == "wartet" and j42["youtube"].get("naechster_versuch"), "Limit: wartet auf neuen Versuch")
check(b"status" in STORE.get("/cloud/status.txt", b"").lower() or STORE.get("/cloud/status.txt"), "status.txt geschrieben")
check(b"geplant" in STORE.get("/cloud/log.txt", b""), "Log in der Cloud")

print("3) Cloud: zweiter Lauf, Limit-Job nicht fällig -> Platzhalter, kein Verwerfen")
n_vid = len(M.S["yt_videos"])
C.cmd_run([])
j42 = json.loads(STORE["/cloud/queue/42_limit.json"])
check(j42["youtube"]["status"] == "wartet" and "fehlt" not in json.dumps(j42), "Limit-Job bleibt wartend (Video nicht unnötig geladen)")
check(len(M.S["yt_videos"]) == n_vid, "keine Doppel-Uploads")

print("4) Mac: pull-status + Termin ändern")
os.environ["CLIPS_DIR"] = MAC
C.cmd_pull_status([])
check(os.path.exists(os.path.join(MAC, "_auftrag", "upload_status.txt")), "Status auf dem Mac")
C.cmd_zeit(["42_limit", b(now + timedelta(hours=6))])
check(json.loads(STORE["/cloud/queue/42_limit.json"])["zeit"] == P.iso(P.parse_zeit(b(now + timedelta(hours=6)))), "Termin in der Cloud geändert")

print("5) Schwärzen")
t = C.schwaerzen("x?access_token=EAAB12345678901234567890xyz&a=1 Bearer sl.abcdefghijklmnopqrstuvwxyz")
check("EAAB" not in t and "sl.abc" not in t, "Tokens werden aus Logs entfernt")
print(f"\n{ok} Prüfungen bestanden")
