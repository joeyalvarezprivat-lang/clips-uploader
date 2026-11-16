"""End-to-End-Test von publish.py gegen mock_server.py (keine echten Konten nötig)."""
import importlib.util, json, os, shutil, sys, tempfile, time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mock_server as M

TMP = tempfile.mkdtemp(prefix="clips_")
os.makedirs(os.path.join(TMP, "fertig"))
os.makedirs(os.path.join(TMP, "_auftrag"))
os.environ.update({"PUBLISH_MOCK": M.BASE, "CLIPS_DIR": TMP, "UPLOAD_ZUGANG": os.path.join(TMP, "zugang.json")})
srv = M.start()
spec = importlib.util.spec_from_file_location("publish", os.path.join(HERE, "..", "publish.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)
_echt = P.jetzt
UHR = {"plus": timedelta()}
P.jetzt = lambda: _echt() + UHR["plus"]          # Test-Uhr zum Vorspulen
def vorspulen(**kw):
    UHR["plus"] += timedelta(**kw)

ok = 0
def check(cond, text):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + text)
    if not cond:
        raise SystemExit(1)
    ok += 1

def clip(name, titel, mb=3):
    p = os.path.join(TMP, "fertig", name + ".mp4")
    with open(p, "wb") as f:
        f.write(os.urandom(mb * 1024 * 1024))
    with open(p[:-4] + ".txt", "w") as f:
        f.write(f"TITEL: {titel}\n\nCAPTION:\n'Zitat aus dem Clip' 💀\n\n#ishowspeed #speed #shorts\n\nQuelle: 0:01–0:20 (19s)\nWarum: test\n")
    return p

def berlin(dt):
    return dt.astimezone(P.TZ).strftime("%Y-%m-%d %H:%M")

def job(jid):
    return json.load(open(os.path.join(TMP, "_auftrag", "upload", jid + ".json")))

print("1) Einrichtung Instagram (Token-Tausch) + vorhandene Zugänge")
P.einrichten_instagram(["IG_SHORT", "APPSECRET"])
z = P.zugang()
check(z["instagram"]["token"] == "IG_LONG" and z["instagram"]["user_id"] == "17841400000", "Instagram-Token getauscht, user_id gespeichert")
z["youtube"] = {"client_id": "cid", "client_secret": "cs", "refresh_token": "RT"}
z["dropbox"] = {"app_key": "k", "app_secret": "s", "refresh_token": "DRT"}
P.speichere_zugang(z)
check(oct(os.stat(P.ZUGANG).st_mode)[-3:] == "600", "zugang.json nur für Joey lesbar (600)")

print("1b) Einrichtung Facebook (Token-Tausch, Seite wählen)")
P.einrichten_facebook(["APPID", "APPSECRET", "FB_SHORT"])
check(P.zugang()["facebook"]["page_token"] == "PAGE_TOK" and P.zugang()["facebook"]["page_id"] == "PAGE1",
      "Facebook: Seiten-Token gespeichert")

print("2) Clips in die Warteschlange")
now = datetime.now(timezone.utc)
a = clip("27_speed-backflip", "Speed Did A Backflip After Winning Streamer Of The Year AGAIN 🏆🤸")
b = clip("28_speed-advice", "Speed's Advice For Small Creators Took A Sudden Turn 😭💀")
c = clip("29_musik-clip", "Clip mit fremder Musik")
d = clip("30_limit", "LIMIT-TEST")
M.S["quota_titles"].add("LIMIT-TEST")
P.cmd_add([a, "--zeit", berlin(now + timedelta(minutes=8))])
P.cmd_add([b, "--zeit", berlin(now + timedelta(hours=3))])
P.cmd_add([c, "--zeit", berlin(now + timedelta(hours=1)), "--privat"])
P.cmd_add([d, "--zeit", berlin(now + timedelta(hours=2)), "--nur", "youtube"])
j = job("27_speed-backflip")
check(j["hashtags"] == ["#ishowspeed", "#speed", "#shorts"] and j["caption"].startswith("'Zitat"), "Titel/Caption/Hashtags aus .txt gelesen")
try:
    P.cmd_add([a, "--zeit", "2026-11-20 10:00"]); dup = False
except SystemExit:
    dup = True
check(dup, "Doppelt einreihen wird verhindert")

tk = os.path.join(TMP, "tiktok")
check(os.path.exists(os.path.join(tk, "27_speed-backflip.mp4")) and os.path.getsize(os.path.join(tk, "27_speed-backflip.mp4")) == os.path.getsize(a),
      "TikTok: Video nach Clips/tiktok/ kopiert")
ttxt = open(os.path.join(tk, "27_speed-backflip.txt")).read()
check(ttxt.startswith("Speed Did A Backflip") and "#ishowspeed" in ttxt, "TikTok: Caption-Datei (Titel + Caption + Hashtags)")
check("FREMDE MUSIK" in open(os.path.join(tk, "29_musik-clip.txt")).read(), "TikTok: Musik-Clip mit Warnhinweis")
posten = open(os.path.join(tk, "_POSTEN.txt")).read()
check("27_speed-backflip.mp4" in posten and "nur privat" in posten, "TikTok: _POSTEN.txt mit Reihenfolge/Zeiten")
os.remove(os.path.join(tk, "28_speed-advice.mp4"))
P.cmd_status([])
check("28_speed-advice" not in open(os.path.join(tk, "_POSTEN.txt")).read(), "TikTok: gepostete (gelöschte) Clips verschwinden aus der Liste")

print("3) Erster Lauf")
P.cmd_tick([])
ja, jb, jc, jd = (job(x) for x in ("27_speed-backflip", "28_speed-advice", "29_musik-clip", "30_limit"))
check(ja["youtube"]["status"] == "veroeffentlicht", "A: YouTube sofort öffentlich (Termin in < 10 Min.)")
va = M.S["yt_videos"][ja["youtube"]["video_id"]]
check(va["bytes"] == 3 * 1024 * 1024, "A: Upload nach Abbruch (503) per Range fortgesetzt, Datei vollständig")
check(va["meta"]["status"]["privacyStatus"] == "public" and "publishAt" not in va["meta"]["status"], "A: privacyStatus public")
vb = M.S["yt_videos"][jb["youtube"]["video_id"]]["meta"]["status"]
check(jb["youtube"]["status"] == "geplant" and vb["privacyStatus"] == "private" and vb["publishAt"].endswith("Z"),
      "B: YouTube geplant (private + publishAt %s)" % vb["publishAt"])
vc = M.S["yt_videos"][jc["youtube"]["video_id"]]["meta"]["status"]
check(jc["youtube"]["status"] == "privat" and vc["privacyStatus"] == "private" and "publishAt" not in vc, "C: Copyright-Clip bleibt privat")
check(not jc["instagram"]["aktiv"], "C: kein Instagram")
check(jd["youtube"]["status"] == "wartet" and jd["youtube"]["letzter_fehler"] == "uploadLimitExceeded" and jd["youtube"].get("naechster_versuch"),
      "D: Upload-Limit -> neuer Versuch in 3 h, kein Abbruch")
check(ja["instagram"]["status"] == "dropbox" and jb["instagram"]["status"] == "dropbox", "A+B: Videos in Dropbox, Link erzeugt")
check(M.S["yt_videos"][ja["youtube"]["video_id"]]["meta"]["snippet"]["categoryId"] == "24", "Kategorie Entertainment")
fa, fbb = M.S["fb"][ja["facebook"]["video_id"]], M.S["fb"][jb["facebook"]["video_id"]]
check(ja["facebook"]["status"] == "veroeffentlicht" and fa["state"] == "PUBLISHED" and fa["bytes"] == 3 * 1024 * 1024,
      "Facebook A: Reel direkt hochgeladen (nach Abbruch fortgesetzt) und sofort veröffentlicht")
check(jb["facebook"]["status"] == "geplant" and fbb["state"] == "SCHEDULED" and abs(fbb["ts"] - P.parse_zeit(jb["zeit"]).timestamp()) < 2,
      "Facebook B: von Facebook selbst zur Zeit geplant (SCHEDULED)")
check(fa["description"].startswith("Speed Did A Backflip") and "#ishowspeed" in fa["description"], "Facebook: Beschreibung mit Hashtags")
check(not jc["facebook"]["aktiv"] and not jd["facebook"]["aktiv"], "Facebook: kein Post bei Musik-Clip bzw. --nur youtube")

print("4) Weitere Läufe (Instagram-Container -> warten -> veröffentlichen)")
vorspulen(minutes=10)
for _ in range(4):
    P.cmd_tick([])
ja, jb = job("27_speed-backflip"), job("28_speed-advice")
check(ja["instagram"]["status"] == "veroeffentlicht" and ja["instagram"]["url"].startswith("https://www.instagram.com/reel/"),
      "A: Reel veröffentlicht " + ja["instagram"]["url"])
check(ja["instagram"].get("dropbox_geloescht") and "/27_speed-backflip.mp4" not in M.S["dbx"], "A: Dropbox-Datei danach gelöscht")
check(jb["instagram"]["status"] == "dropbox", "B: Instagram wartet noch (Termin in 3 h, kein Container zu früh)")
cap = M.S["published"][0][2]
check(cap.startswith("Speed Did A Backflip") and "#ishowspeed" in cap, "Caption = Titel + Zeile + Hashtags")
n_pub = len(M.S["published"])
P.cmd_tick([]); P.cmd_tick([])
check(len(M.S["published"]) == n_pub, "Kein Doppelpost bei weiteren Läufen")

P.cmd_tick([])
check(job("27_speed-backflip")["facebook"].get("bestaetigt"), "Facebook A: Veröffentlichung bestätigt")

print("5) Termin von B ist erreicht")
M.S["fb_alle_veroeffentlicht"] = True
vorspulen(hours=3)
for _ in range(4):
    P.cmd_tick([])
jb = job("28_speed-advice")
check(jb["instagram"]["status"] == "veroeffentlicht", "B: Reel zur Zeit veröffentlicht")
check(len(M.S["published"]) == 2, "Genau 2 Reels insgesamt")
jb2 = job("28_speed-advice")
if not jb2["facebook"].get("bestaetigt"):
    vorspulen(minutes=11); P.cmd_tick([]); jb2 = job("28_speed-advice")
check(jb2["facebook"]["status"] == "veroeffentlicht" and jb2["facebook"].get("bestaetigt"), "Facebook B: nach Termin als veröffentlicht bestätigt")
check(len([v for v in M.S["fb"].values() if v.get("state")]) == 2, "Facebook: keine Doppel-Uploads")

print("6) Container-Neustart, wenn Container bereits veröffentlicht war (Absturz zwischen publish und Speichern)")
e = clip("31_absturz", "Absturz-Test")
P.cmd_add([e, "--zeit", berlin(P.jetzt() - timedelta(minutes=1)), "--nur", "instagram"])
P.cmd_tick([])
check(P.parse_zeit(job("31_absturz")["zeit"]) >= P.parse_zeit(job("28_speed-advice")["zeit"]) + timedelta(minutes=P.MIN_ABSTAND_MIN),
      "Überfälliger Clip wird mit >= Mindestabstand zum letzten Post neu eingeplant")
vorspulen(hours=2)
for _ in range(3):
    P.cmd_tick([])
je = job("31_absturz")
cid = je["instagram"]["container_id"]
M.S["containers"][cid]["pub"] = True            # wurde veröffentlicht, aber Status ging verloren
je["instagram"]["status"] = "container"; P.speichere_json(P.job_pfad("31_absturz"), je)
P.cmd_tick([])
check(job("31_absturz")["instagram"]["status"] == "veroeffentlicht" and len(M.S["published"]) == 2,
      "Erkennt 'PUBLISHED' und postet nicht doppelt")

print("6b) Lawinen-Schutz: 3 überfällige Clips auf einmal")
zs = []
for k in range(3):
    pk = clip(f"4{k}_lawine", f"Lawine {k}")
    P.cmd_add([pk, "--zeit", berlin(P.jetzt() - timedelta(hours=1))])
P.cmd_tick([])
zs = sorted(P.parse_zeit(job(f"4{k}_lawine")["zeit"]) for k in range(3))
alle = sorted(P.parse_zeit(j["zeit"]) for j in P.alle_jobs() if P._oeffentlich(j))
check(zs[0] >= P.jetzt() + timedelta(minutes=4) and all(b - a >= timedelta(minutes=P.MIN_ABSTAND_MIN) for a, b in zip(alle, alle[1:])),
      "Zeiten: " + ", ".join(f"{t.astimezone(P.TZ):%H:%M}" for t in zs) + " (alle öffentlichen Posts je >= Mindestabstand)")
yt_pub = [j for j in (job(f"4{k}_lawine") for k in range(3)) if j["youtube"]["status"] == "veroeffentlicht"]
check(len(yt_pub) <= 1, "Höchstens ein Clip geht sofort raus, Rest per YouTube-Planung")

print("7) Instagram-Token wird automatisch verlängert")
z = P.zugang(); z["instagram"]["erneuert"] = time.time() - 6 * 86400; P.speichere_zugang(z)
P.ig_token(P.zugang())
check(P.zugang()["instagram"]["token"] == "IG_REFRESHED", "Token nach >5 Tagen verlängert")

print("8) YouTube-Zugang widerrufen -> klare Fehlermeldung")
z = P.zugang(); z["youtube"]["refresh_token"] = "REVOKED"; z["youtube"].pop("access_token", None); P.speichere_zugang(z)
f = clip("32_auth", "Auth-Test")
P.cmd_add([f, "--zeit", berlin(datetime.now(timezone.utc) + timedelta(hours=5)), "--nur", "youtube"])
P.cmd_tick([])
jf = job("32_auth")
fehler = open(P.FEHLER_TXT).read()
check(jf["youtube"]["status"] == "fehler" and "einrichten youtube" in fehler, "Status 'fehler' + Hinweis in upload_FEHLER.txt")

print("9) Fehlende Datei")
g = clip("33_weg", "Weg")
P.cmd_add([g, "--zeit", berlin(datetime.now(timezone.utc) + timedelta(hours=6)), "--nur", "instagram"])
os.remove(g)
P.cmd_tick([])
check(job("33_weg")["instagram"]["status"] == "fehler", "Fehlende Videodatei wird gemeldet statt abzustürzen")

print("9b) Facebook nicht eingerichtet -> wird still übersprungen")
z = P.zugang(); fbz = z.pop("facebook"); P.speichere_zugang(z)
h = clip("34_ohne_fb", "Ohne FB")
P.cmd_add([h, "--zeit", berlin(P.jetzt() + timedelta(hours=9)), "--nur", "facebook"])
P.cmd_tick([])
check(job("34_ohne_fb")["facebook"]["status"] == "wartet", "Kein Fehler, Clip wartet bis Facebook eingerichtet ist")
z = P.zugang(); z["facebook"] = fbz; P.speichere_zugang(z)

print("10) Statusübersicht")
P.cmd_status([])
check(os.path.exists(P.STATUS_TXT), "upload_status.txt geschrieben")
print(f"\nALLE {ok} PRÜFUNGEN BESTANDEN")
srv.shutdown()
shutil.rmtree(TMP)
