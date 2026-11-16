#!/usr/bin/env python3
"""Eigener Uploader für ClipsForTheUnemployed: YouTube Shorts + Instagram Reels über die offiziellen APIs.

Kostenlos, ohne Metricool. Läuft in der Cloud (GitHub Actions, cloud_sync.py run alle ~10 Min.); der Mac schiebt nur neue Clips hoch (cloud_sync.py push).
Nur Python-Standardbibliothek (certifi wird genutzt, wenn vorhanden).

Befehle
  einrichten youtube|instagram|dropbox|facebook   einmalige Anmeldung (im Terminal auf dem Mac ausführen)
  test                                    prüft alle Zugänge, zeigt Limits
  add DATEI.mp4 --zeit "2026-11-17 15:00" [--txt DATEI.txt] [--nur youtube|instagram] [--privat]
                                          Clip in die Warteschlange (_auftrag/upload/<id>.json)
  tick                                    arbeitet die Warteschlange ab (lädt hoch, veröffentlicht zur Zeit)
  status                                  Übersicht
  zeit ID "2026-11-16 20:00"              Termin eines noch nicht veröffentlichten Clips ändern
  tiktok-paket [ID ...]                   Video + Caption nach Clips/tiktok/ legen (TikTok postet Joey selbst)

TikTok: kein Auto-Upload (TikTok lässt Eigengebrauch-Apps nicht zu). Bei jedem `add` landet das Video samt
  Caption in Clips/tiktok/, Reihenfolge und Zeiten stehen in Clips/tiktok/_POSTEN.txt.
  probe-instagram DATEI.mp4               Dropbox-Link + Instagram-Container OHNE Veröffentlichen (Test)
  probe-youtube DATEI.mp4                 Test-Upload als PRIVATES Video
  probe-facebook DATEI.mp4                Test-Upload als ENTWURF auf der Facebook-Seite

YouTube: Upload sofort, Veröffentlichung über status.publishAt (YouTube plant selbst).
Instagram: API kennt keine Planung -> Datei wird vorab in Joeys Dropbox (App-Ordner) gelegt,
  ~25 min vor dem Termin wird der Container erstellt, zur Zeit veröffentlicht, danach Dropbox-Datei gelöscht.
Facebook: Reel wird direkt hochgeladen (ohne Dropbox) und von Facebook selbst zur Zeit veröffentlicht.
Die Rechteprüfungen der Plattformen (Content ID usw.) gelten ganz normal – hier wird nichts umgangen.
"""
import base64, hashlib, http.client, json, os, re, secrets, socket, ssl, sys, time, urllib.parse, webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=1))

HERE = os.path.dirname(os.path.abspath(__file__))
CLIPS = os.environ.get("CLIPS_DIR") or os.path.abspath(os.path.join(HERE, "..", ".."))
QUEUE = os.path.join(CLIPS, "_auftrag", "upload")
STATUS_TXT = os.path.join(CLIPS, "_auftrag", "upload_status.txt")
FEHLER_TXT = os.path.join(CLIPS, "_auftrag", "upload_FEHLER.txt")
TIKTOK = os.path.join(CLIPS, "tiktok")          # Videos + Captions zum manuellen Posten auf TikTok
TIKTOK_PRO_TAG = 10                               # TikTok sperrt ab ~15 Posts/Tag
ZUGANG = os.environ.get("UPLOAD_ZUGANG") or os.path.join(HERE, "zugang.json")
IG_VERSION = "v25.0"
IG_VORLAUF_MIN = 25          # Container so viele Minuten vor dem Termin anlegen
YT_MIN_VORLAUF_MIN = 10      # liegt der Termin näher, wird sofort veröffentlicht
MAX_VERSUCHE = 6
MIN_ABSTAND_MIN = 20         # mindestens so viele Minuten zwischen zwei öffentlichen Posts (gegen Spam-Sperren)
MOCK = os.environ.get("PUBLISH_MOCK")  # nur für Tests: http://127.0.0.1:PORT


# ---------------------------------------------------------------- HTTP-Grundlagen
def _ctx():
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca and os.path.exists(ca):
        return ssl.create_default_context(cafile=ca)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _target(url):
    """Zerlegt URL -> (scheme, host, port, pfad). Im Testmodus wird jede https-URL auf den Mock-Server umgebogen."""
    if MOCK and url.startswith("https://"):
        u = urllib.parse.urlsplit(url)
        url = MOCK.rstrip("/") + "/" + u.netloc + u.path + (("?" + u.query) if u.query else "")
    u = urllib.parse.urlsplit(url)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return u.scheme, u.hostname, u.port, path


def anfrage(method, url, body=None, headers=None, timeout=120):
    """Ein Request ohne Weiterleitungen. Gibt (status, headers(dict, lowercase), bytes) zurück."""
    scheme, host, port, path = _target(url)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if scheme == "https" and proxy and not MOCK:
        pu = urllib.parse.urlsplit(proxy)
        conn = http.client.HTTPSConnection(pu.hostname, pu.port or 80, timeout=timeout, context=_ctx())
        hdr = {}
        if pu.username:
            hdr["Proxy-Authorization"] = "Basic " + base64.b64encode(
                f"{urllib.parse.unquote(pu.username)}:{urllib.parse.unquote(pu.password or '')}".encode()).decode()
        conn.set_tunnel(host, port or 443, headers=hdr)   # CONNECT-Tunnel (nur falls ein Proxy gesetzt ist)
    elif scheme == "https":
        conn = http.client.HTTPSConnection(host, port or 443, timeout=timeout, context=_ctx())
    else:
        conn = http.client.HTTPConnection(host, port or 80, timeout=timeout)
    h = dict(headers or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        body = body.encode()
    try:
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        conn.close()


def jhttp(method, url, body=None, headers=None, form=None, ok=(200, 201)):
    h = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form)
        h["Content-Type"] = "application/x-www-form-urlencoded"
    st, hd, data = anfrage(method, url, body, h)
    try:
        js = json.loads(data.decode() or "{}")
    except Exception:
        js = {"_raw": data[:500].decode(errors="replace")}
    if st not in ok:
        raise ApiFehler(st, js, url)
    return js


class ApiFehler(Exception):
    def __init__(self, status, js, url=""):
        self.status, self.js, self.url = status, js, url
        super().__init__(f"HTTP {status} {urllib.parse.urlsplit(url).netloc}: {json.dumps(js)[:400]}")

    def grund(self):
        """Fehlergrund (YouTube: errors[0].reason, OAuth: error, Meta: error.code/message)."""
        e = self.js.get("error") if isinstance(self.js, dict) else None
        if isinstance(e, str):
            return e
        if isinstance(e, dict):
            errs = e.get("errors") or []
            if errs and errs[0].get("reason"):
                return errs[0]["reason"]
            if e.get("error_subcode") or e.get("code"):
                return f"meta_{e.get('code')}_{e.get('error_subcode', '')}"
            return str(e.get("status") or e.get(".tag") or "")
        if isinstance(self.js, dict) and self.js.get("error_summary"):
            return self.js["error_summary"]
        return ""


# ---------------------------------------------------------------- Dateien / Zustand
def lade_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def speichere_json(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    if p == ZUGANG:
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def zugang():
    return lade_json(ZUGANG, {}) or {}


def speichere_zugang(z):
    speichere_json(ZUGANG, z)


def jetzt():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_zeit(s):
    s = s.strip().replace("T", " ")
    if s.endswith("Z"):
        return datetime.strptime(s[:-1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19] if len(s) > 16 else s, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    raise ValueError(f"Zeit nicht lesbar: {s!r} (Format: 2026-11-17 15:00)")


def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- Google / YouTube
G_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
G_TOKEN = "https://oauth2.googleapis.com/token"
YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
YT_API = "https://www.googleapis.com/youtube/v3"
YT_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"


class AuthFehler(Exception):
    pass


def yt_token(z):
    y = z.get("youtube") or {}
    if not y.get("refresh_token"):
        raise AuthFehler("YouTube nicht eingerichtet (python3 publish.py einrichten youtube)")
    if y.get("access_token") and y.get("expires_at", 0) > time.time() + 120:
        return y["access_token"]
    form = {"client_id": y["client_id"], "refresh_token": y["refresh_token"], "grant_type": "refresh_token"}
    if y.get("client_secret"):
        form["client_secret"] = y["client_secret"]
    try:
        js = jhttp("POST", G_TOKEN, form=form)
    except ApiFehler as e:
        if e.grund() in ("invalid_grant", "invalid_client", "unauthorized_client"):
            raise AuthFehler(f"YouTube-Zugang ungültig ({e.grund()}) – bitte 'einrichten youtube' neu ausführen")
        raise
    y["access_token"] = js["access_token"]
    y["expires_at"] = time.time() + int(js.get("expires_in", 3600))
    if js.get("refresh_token"):
        y["refresh_token"] = js["refresh_token"]
    z["youtube"] = y
    speichere_zugang(z)
    return y["access_token"]


class _Loopback(BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        _Loopback.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _Loopback.result
        self.wfile.write(("<h2>%s</h2><p>Du kannst dieses Fenster schließen.</p>" %
                          ("YouTube verbunden ✅" if ok else "Fehler: %s" % _Loopback.result)).encode())

    def log_message(self, *a):
        pass


def einrichten_youtube(args):
    z = zugang()
    cid = csec = None
    pfad = args[0] if args else None
    if not pfad:
        dl = os.path.expanduser("~/Downloads")
        kand = sorted([os.path.join(dl, f) for f in os.listdir(dl) if f.startswith("client_secret") and f.endswith(".json")],
                      key=os.path.getmtime) if os.path.isdir(dl) else []
        pfad = kand[-1] if kand else None
    if pfad and os.path.exists(pfad):
        js = lade_json(pfad)
        inst = js.get("installed") or js.get("web") or {}
        cid, csec = inst.get("client_id"), inst.get("client_secret")
        print(f"Client-Datei gelesen: {pfad}")
    if not cid:
        cid = input("Google OAuth Client-ID: ").strip()
        csec = input("Client-Secret: ").strip()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    srv = HTTPServer(("127.0.0.1", 0), _Loopback)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    state = secrets.token_urlsafe(16)
    url = G_AUTH + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": YT_SCOPE,
        "access_type": "offline", "prompt": "consent", "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state})
    print("\nBrowser öffnet sich. Mit dem Google-Konto des YouTube-Kanals anmelden und zustimmen.")
    print("(Warnung 'App nicht überprüft' -> 'Erweitert' -> 'Weiter zu ... (unsicher)' – es ist deine eigene App.)")
    print("Falls sich nichts öffnet, diesen Link kopieren:\n" + url + "\n")
    webbrowser.open(url)
    srv.timeout = 300
    while not _Loopback.result:
        srv.handle_request()
    r = _Loopback.result
    if r.get("state") != state or "code" not in r:
        sys.exit(f"Abgebrochen: {r}")
    form = {"client_id": cid, "client_secret": csec, "code": r["code"], "code_verifier": verifier,
            "redirect_uri": redirect, "grant_type": "authorization_code"}
    js = jhttp("POST", G_TOKEN, form=form)
    if not js.get("refresh_token"):
        sys.exit("Kein Refresh-Token erhalten. Unter myaccount.google.com/permissions die App entfernen und erneut versuchen.")
    z["youtube"] = {"client_id": cid, "client_secret": csec, "refresh_token": js["refresh_token"],
                    "access_token": js["access_token"], "expires_at": time.time() + int(js.get("expires_in", 3600)),
                    "eingerichtet": iso(jetzt())}
    speichere_zugang(z)
    kanal = yt_kanal(z)
    print(f"✅ YouTube verbunden: {kanal}")
    print("WICHTIG: In der Google Cloud Console unter 'Zielgruppe' den Status auf 'In Produktion' stellen,")
    print("sonst läuft die Anmeldung nach 7 Tagen ab.")


def yt_kanal(z):
    js = jhttp("GET", YT_API + "/channels?part=snippet&mine=true", headers={"Authorization": "Bearer " + yt_token(z)})
    it = js.get("items") or []
    return it[0]["snippet"]["title"] if it else "(kein Kanal gefunden)"


def yt_upload(z, datei, meta, fortschritt=None):
    """Resumable Upload. meta = videos-Resource (snippet+status). Gibt Video-ID zurück."""
    groesse = os.path.getsize(datei)
    st, hd, data = anfrage("POST", YT_UPLOAD + "?uploadType=resumable&part=snippet,status&notifySubscribers=true",
                        json.dumps(meta).encode(),
                        {"Authorization": "Bearer " + yt_token(z), "Content-Type": "application/json; charset=UTF-8",
                         "X-Upload-Content-Length": str(groesse), "X-Upload-Content-Type": "video/mp4"})
    if st != 200 or "location" not in hd:
        raise ApiFehler(st, _js(data), YT_UPLOAD)
    sitzung = hd["location"]
    start, versuch = 0, 0
    while True:
        try:
            with open(datei, "rb") as f:
                f.seek(start)
                rest = groesse - start
                h = {"Authorization": "Bearer " + yt_token(z), "Content-Length": str(rest), "Content-Type": "video/mp4"}
                if start:
                    h["Content-Range"] = f"bytes {start}-{groesse - 1}/{groesse}"
                st, hd, data = anfrage("PUT", sitzung, f, h, timeout=600)
        except (OSError, http.client.HTTPException) as e:
            st, data = 0, str(e).encode()
        if st in (200, 201):
            return json.loads(data)["id"]
        if st == 404:
            raise ApiFehler(404, {"error": "upload_session_expired"}, sitzung)
        if st in (0, 308, 500, 502, 503, 504):
            versuch += 1
            if versuch > 5:
                raise ApiFehler(st, _js(data), sitzung)
            time.sleep(min(60, 2 ** versuch))
            # Stand abfragen
            st2, hd2, _ = anfrage("PUT", sitzung, b"", {"Authorization": "Bearer " + yt_token(z),
                                                     "Content-Length": "0", "Content-Range": f"bytes */{groesse}"})
            if st2 in (200, 201):
                return json.loads(d2)["id"] if d2 else None
            if st2 == 308:
                rg = hd2.get("range")
                start = int(rg.split("-")[1]) + 1 if rg else 0
                continue
            if st2 == 404:
                raise ApiFehler(404, {"error": "upload_session_expired"}, sitzung)
            continue
        raise ApiFehler(st, _js(data), sitzung)


def _js(data):
    try:
        return json.loads(data.decode() or "{}")
    except Exception:
        return {"_raw": data[:300].decode(errors="replace")}


def yt_meta(job, privat, publish_at):
    titel = re.sub(r"[<>]", "", job["titel"]).strip()[:100]
    beschr = job.get("caption", "")
    tags_txt = " ".join(job.get("hashtags", []))
    desc = (titel + "\n\n" + beschr + ("\n\n" + tags_txt if tags_txt else "")).strip()
    while len(desc.encode("utf-8")) > 5000:
        desc = desc[:-50]
    tags, laenge = [], 0
    for t in job.get("tags") or [h.lstrip("#") for h in job.get("hashtags", [])]:
        t = t.replace("<", "").replace(">", "").strip()
        n = len(t) + (2 if " " in t else 0) + 1
        if t and laenge + n <= 480:
            tags.append(t)
            laenge += n
    status = {"selfDeclaredMadeForKids": False, "containsSyntheticMedia": False, "embeddable": True,
              "license": "youtube"}
    if privat:
        status["privacyStatus"] = "private"
    elif publish_at:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at
    else:
        status["privacyStatus"] = "public"
    return {"snippet": {"title": titel, "description": desc, "tags": tags, "categoryId": "24",
                        "defaultLanguage": "en", "defaultAudioLanguage": "en"}, "status": status}


# ---------------------------------------------------------------- Instagram
IG = "https://graph.instagram.com"


def ig_token(z):
    i = z.get("instagram") or {}
    if not i.get("token"):
        raise AuthFehler("Instagram nicht eingerichtet (python3 publish.py einrichten instagram)")
    if i.get("modus") == "fb":          # über Facebook-Seite verknüpft: Seiten-Token läuft nicht ab
        return i["token"]
    alter = time.time() - i.get("erneuert", 0)
    ablauf = i.get("expires_at", 0)
    if ablauf and ablauf < time.time():
        raise AuthFehler("Instagram-Token abgelaufen – im Meta-Dashboard neues Token erzeugen, dann 'einrichten instagram'")
    if alter > 5 * 86400 and alter > 86400:      # alle 5 Tage verlängern (frühestens nach 24 h erlaubt)
        try:
            js = jhttp("GET", IG + "/refresh_access_token?" + urllib.parse.urlencode(
                {"grant_type": "ig_refresh_token", "access_token": i["token"]}))
            i["token"] = js["access_token"]
            i["erneuert"] = time.time()
            i["expires_at"] = time.time() + int(js.get("expires_in", 60 * 86400))
            z["instagram"] = i
            speichere_zugang(z)
            log("Instagram-Token verlängert (gültig bis %s)" % datetime.fromtimestamp(i["expires_at"], TZ).date())
        except ApiFehler as e:
            log(f"Instagram-Token-Verlängerung fehlgeschlagen: {e}")
            if ablauf and ablauf - time.time() < 3 * 86400:
                raise AuthFehler("Instagram-Token läuft in <3 Tagen ab und ließ sich nicht verlängern")
    return i["token"]


def einrichten_instagram(args):
    z = zugang()
    print("Im Meta-Dashboard deiner App: Instagram > API-Einrichtung mit Instagram-Login > 'Token generieren'.")
    tok = (args[0] if args else input("Access-Token einfügen: ")).strip()
    me = jhttp("GET", f"{IG}/{IG_VERSION}/me?fields=user_id,username,account_type&access_token=" + urllib.parse.quote(tok))
    i = {"token": tok, "user_id": str(me.get("user_id") or me.get("id")), "username": me.get("username"),
         "erneuert": time.time(), "expires_at": time.time() + 59 * 86400, "eingerichtet": iso(jetzt())}
    # kurzlebiges Token? -> mit App-Secret tauschen
    sec = (args[1] if len(args) > 1 else input("Instagram-App-Secret (Enter = überspringen): ")).strip()
    if sec:
        try:
            js = jhttp("GET", IG + "/access_token?" + urllib.parse.urlencode(
                {"grant_type": "ig_exchange_token", "client_secret": sec, "access_token": tok}))
            i["token"] = js["access_token"]
            i["expires_at"] = time.time() + int(js.get("expires_in", 60 * 86400))
            print("Langlebiges Token erhalten.")
        except ApiFehler as e:
            print(f"(Tausch nicht nötig/möglich: {e.grund() or e.status} – Token ist vermutlich schon langlebig)")
    z["instagram"] = i
    speichere_zugang(z)
    print(f"✅ Instagram verbunden: @{i['username']} ({me.get('account_type')})")
    if me.get("account_type") not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR", None):
        print("⚠️ Konto ist kein Business-/Creator-Konto – Veröffentlichen per API geht nur mit Profi-Konto.")


def ig_base(z):
    i = z.get("instagram") or {}
    return f"{FB}/{FB_VERSION}" if i.get("modus") == "fb" else f"{IG}/{IG_VERSION}"


def ig_container(z, video_url, caption):
    i = z["instagram"]
    js = jhttp("POST", f"{ig_base(z)}/{i['user_id']}/media", form={
        "media_type": "REELS", "video_url": video_url, "caption": caption, "share_to_feed": "true",
        "thumb_offset": "1500", "access_token": ig_token(z)})
    return js["id"]


def ig_container_status(z, cid):
    js = jhttp("GET", f"{ig_base(z)}/{cid}?fields=status_code,status&access_token=" + urllib.parse.quote(ig_token(z)))
    return js.get("status_code"), js.get("status")


def ig_publish(z, cid):
    i = z["instagram"]
    js = jhttp("POST", f"{ig_base(z)}/{i['user_id']}/media_publish",
               form={"creation_id": cid, "access_token": ig_token(z)})
    mid = js["id"]
    try:
        p = jhttp("GET", f"{ig_base(z)}/{mid}?fields=permalink&access_token=" + urllib.parse.quote(ig_token(z)))
        return mid, p.get("permalink")
    except ApiFehler:
        return mid, None


def ig_limit(z):
    i = z["instagram"]
    js = jhttp("GET", f"{ig_base(z)}/{i['user_id']}/content_publishing_limit?fields=quota_usage,config&access_token="
               + urllib.parse.quote(ig_token(z)))
    d = (js.get("data") or [{}])[0]
    return d.get("quota_usage"), (d.get("config") or {}).get("quota_total")


# ---------------------------------------------------------------- Dropbox (öffentliche Video-URL für Instagram)
DBX_API = "https://api.dropboxapi.com"
DBX_CONTENT = "https://content.dropboxapi.com"


def dbx_token(z):
    d = z.get("dropbox") or {}
    if not d.get("refresh_token"):
        raise AuthFehler("Dropbox nicht eingerichtet (python3 publish.py einrichten dropbox)")
    if d.get("access_token") and d.get("expires_at", 0) > time.time() + 120:
        return d["access_token"]
    try:
        js = jhttp("POST", DBX_API + "/oauth2/token", form={
            "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
            "client_id": d["app_key"], "client_secret": d["app_secret"]})
    except ApiFehler as e:
        if e.status in (400, 401):
            raise AuthFehler(f"Dropbox-Zugang ungültig ({e.grund()}) – 'einrichten dropbox' neu ausführen")
        raise
    d["access_token"] = js["access_token"]
    d["expires_at"] = time.time() + int(js.get("expires_in", 14400))
    z["dropbox"] = d
    speichere_zugang(z)
    return d["access_token"]


def einrichten_dropbox(args):
    z = zugang()
    key = (args[0] if args else input("Dropbox App-Key: ")).strip()
    sec = (args[1] if len(args) > 1 else input("Dropbox App-Secret: ")).strip()
    url = "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode(
        {"client_id": key, "response_type": "code", "token_access_type": "offline"})
    print("Browser öffnet sich: 'Zulassen' klicken, dann den angezeigten Code hier einfügen.\n" + url)
    webbrowser.open(url)
    code = input("Code: ").strip()
    js = jhttp("POST", DBX_API + "/oauth2/token", form={"code": code, "grant_type": "authorization_code",
                                                         "client_id": key, "client_secret": sec})
    z["dropbox"] = {"app_key": key, "app_secret": sec, "refresh_token": js["refresh_token"],
                    "access_token": js["access_token"], "expires_at": time.time() + int(js.get("expires_in", 14400)),
                    "eingerichtet": iso(jetzt())}
    speichere_zugang(z)
    acc = jhttp("POST", DBX_API + "/2/users/get_current_account", body=b"null",
                headers={"Authorization": "Bearer " + dbx_token(z), "Content-Type": "application/json"})
    print(f"✅ Dropbox verbunden: {acc.get('email') or acc.get('name', {}).get('display_name')}")


def dbx_hochladen(z, datei, ziel):
    groesse = os.path.getsize(datei)
    arg = json.dumps({"path": ziel, "mode": "overwrite", "autorename": False, "mute": True})
    if groesse <= 140 * 1024 * 1024:
        with open(datei, "rb") as f:
            st, hd, data = anfrage("POST", DBX_CONTENT + "/2/files/upload", f, {
                "Authorization": "Bearer " + dbx_token(z), "Dropbox-API-Arg": arg,
                "Content-Type": "application/octet-stream", "Content-Length": str(groesse)}, timeout=600)
        if st != 200:
            raise ApiFehler(st, _js(data), DBX_CONTENT)
        return
    # große Dateien: Upload-Session in 64-MB-Stücken
    CH = 64 * 1024 * 1024
    with open(datei, "rb") as f:
        js = jhttp("POST", DBX_CONTENT + "/2/files/upload_session/start", f.read(CH), {
            "Authorization": "Bearer " + dbx_token(z), "Dropbox-API-Arg": json.dumps({"close": False}),
            "Content-Type": "application/octet-stream"})
        sid, off = js["session_id"], f.tell()
        while off < groesse:
            chunk = f.read(CH)
            letzt = off + len(chunk) >= groesse
            if letzt:
                jhttp("POST", DBX_CONTENT + "/2/files/upload_session/finish", chunk, {
                    "Authorization": "Bearer " + dbx_token(z), "Content-Type": "application/octet-stream",
                    "Dropbox-API-Arg": json.dumps({"cursor": {"session_id": sid, "offset": off},
                                                   "commit": json.loads(arg)})})
            else:
                jhttp("POST", DBX_CONTENT + "/2/files/upload_session/append_v2", chunk, {
                    "Authorization": "Bearer " + dbx_token(z), "Content-Type": "application/octet-stream",
                    "Dropbox-API-Arg": json.dumps({"cursor": {"session_id": sid, "offset": off}, "close": False})},
                      ok=(200,))
            off += len(chunk)


def dbx_link(z, ziel):
    h = {"Authorization": "Bearer " + dbx_token(z)}
    try:
        js = jhttp("POST", DBX_API + "/2/sharing/create_shared_link_with_settings", headers=h,
                   body={"path": ziel, "settings": {"requested_visibility": "public", "audience": "public",
                                                    "access": "viewer"}})
        url = js["url"]
    except ApiFehler as e:
        err = (e.js or {}).get("error") or {}
        if e.status == 409 and err.get(".tag") == "shared_link_already_exists":
            url = (err.get("shared_link_already_exists") or {}).get("metadata", {}).get("url")
            if not url:
                js = jhttp("POST", DBX_API + "/2/sharing/list_shared_links", headers=h,
                           body={"path": ziel, "direct_only": True})
                url = js["links"][0]["url"]
        else:
            raise
    return url


def direkt_url(share_url):
    """Dropbox-Teilen-Link -> direkte Datei-URL ohne Weiterleitung (Instagram lädt keine Redirects)."""
    u = urllib.parse.urlsplit(share_url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k not in ("dl", "raw", "st")]
    q.append(("dl", "1"))
    kand = urllib.parse.urlunsplit(("https", "dl.dropboxusercontent.com", u.path, urllib.parse.urlencode(q), ""))
    # Weiterleitungen selbst auflösen, bis 200 + video/*
    url = kand
    for _ in range(5):
        st, hd, _ = anfrage("HEAD", url, None, {"User-Agent": "Mozilla/5.0"}, timeout=60)
        if st in (301, 302, 303, 307, 308) and hd.get("location"):
            url = urllib.parse.urljoin(url, hd["location"])
            continue
        if st == 200:
            return url, hd.get("content-type", "")
        break
    raise ApiFehler(st, {"error": "dropbox_link_nicht_direkt", "url": url}, url)


def dbx_loeschen(z, ziel):
    try:
        jhttp("POST", DBX_API + "/2/files/delete_v2", headers={"Authorization": "Bearer " + dbx_token(z)},
              body={"path": ziel})
    except ApiFehler as e:
        if e.status != 409:
            raise


# ---------------------------------------------------------------- Facebook (Reels auf der Seite)
FB = "https://graph.facebook.com"
FB_UP = "https://rupload.facebook.com/video-upload"
FB_VERSION = "v26.0"


def fb_zugang(z):
    f = z.get("facebook") or {}
    if not f.get("page_token"):
        raise AuthFehler("Facebook nicht eingerichtet (python3 publish.py einrichten facebook)")
    return f


def einrichten_facebook(args):
    """args (optional): APP_ID APP_SECRET USER_TOKEN [SEITEN-NR]"""
    z = zugang()
    print("Graph API Explorer (developers.facebook.com/tools/explorer): App 'ClipsUploader' wählen,")
    print("Berechtigungen pages_show_list, pages_read_engagement, pages_manage_posts, instagram_basic, instagram_content_publishing, business_management hinzufügen,")
    print("'Generate Access Token' -> mit Facebook anmelden, Seite auswählen -> Token kopieren.")
    app_id = (args[0] if len(args) > 0 else input("App-ID: ")).strip()
    sec = (args[1] if len(args) > 1 else input("App-Secret (App-Einstellungen > Allgemein): ")).strip()
    kurz = (args[2] if len(args) > 2 else input("Benutzer-Token aus dem Graph API Explorer: ")).strip()
    lang = jhttp("GET", f"{FB}/{FB_VERSION}/oauth/access_token?" + urllib.parse.urlencode(
        {"grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": sec, "fb_exchange_token": kurz}))
    utok = lang["access_token"]
    seiten = jhttp("GET", f"{FB}/{FB_VERSION}/me/accounts?fields=id,name,access_token,tasks&access_token="
                   + urllib.parse.quote(utok)).get("data") or []
    if not seiten:
        sys.exit("Keine Facebook-Seite gefunden – beim Token-Erzeugen die Seite auswählen und pages_show_list erlauben.")
    if len(seiten) == 1:
        wahl = seiten[0]
    else:
        for k, sd in enumerate(seiten, 1):
            print(f"  {k}) {sd['name']}")
        nr = args[3] if len(args) > 3 else input("Welche Seite? Nummer: ")
        wahl = seiten[int(nr) - 1]
    if "CREATE_CONTENT" not in (wahl.get("tasks") or ["CREATE_CONTENT"]):
        print("⚠️ Für diese Seite fehlt dir die Rolle 'Inhalte erstellen'.")
    z["facebook"] = {"app_id": app_id, "page_id": wahl["id"], "page_name": wahl["name"],
                     "page_token": wahl["access_token"], "eingerichtet": iso(jetzt())}
    speichere_zugang(z)
    name = jhttp("GET", f"{FB}/{FB_VERSION}/{wahl['id']}?fields=name&access_token="
                 + urllib.parse.quote(wahl["access_token"])).get("name")
    print(f"✅ Facebook verbunden: Seite '{name}' (Seiten-Token läuft nicht ab)")
    try:
        ig = jhttp("GET", f"{FB}/{FB_VERSION}/{wahl['id']}?fields=instagram_business_account{{id,username}}&access_token="
                   + urllib.parse.quote(wahl["access_token"])).get("instagram_business_account")
    except ApiFehler as e:
        ig = None
        print(f"(Instagram-Verknüpfung nicht lesbar: {e.grund() or e.status})")
    if ig and ig.get("id"):
        z["instagram"] = {"modus": "fb", "token": wahl["access_token"], "user_id": str(ig["id"]),
                          "username": ig.get("username"), "eingerichtet": iso(jetzt())}
        speichere_zugang(z)
        print(f"✅ Instagram verbunden über die Seite: @{ig.get('username')}")
    else:
        print("⚠️ Kein Instagram-Konto mit dieser Seite verknüpft – in Instagram: Einstellungen > Kontenübersicht/"
              "Profilübergreifend > Facebook-Seite verknüpfen, dann 'einrichten facebook' wiederholen.")


def fb_reel(z, datei, job, termin_ts=None, entwurf=False):
    """Reel hochladen (Datei direkt, kein Dropbox nötig). termin_ts -> Facebook veröffentlicht selbst zur Zeit."""
    f = fb_zugang(z)
    tok, pid = f["page_token"], f["page_id"]
    vid = jhttp("POST", f"{FB}/{FB_VERSION}/{pid}/video_reels",
                form={"upload_phase": "start", "access_token": tok})["video_id"]
    groesse = os.path.getsize(datei)
    offset, versuch = 0, 0
    while True:
        try:
            with open(datei, "rb") as fh:
                fh.seek(offset)
                st, hd, data = anfrage("POST", f"{FB_UP}/{FB_VERSION}/{vid}", fh, {
                    "Authorization": "OAuth " + tok, "offset": str(offset), "file_size": str(groesse),
                    "Content-Type": "application/octet-stream", "Content-Length": str(groesse - offset)}, timeout=600)
        except (OSError, http.client.HTTPException) as e:
            st, data = 0, str(e).encode()
        if st == 200 and _js(data).get("success"):
            break
        versuch += 1
        if versuch > 4 or (st and st < 500 and st != 429):
            raise ApiFehler(st, _js(data), FB_UP)
        time.sleep(min(60, 2 ** versuch))
        stat = jhttp("GET", f"{FB}/{FB_VERSION}/{vid}?fields=status&access_token=" + urllib.parse.quote(tok))
        offset = int(((stat.get("status") or {}).get("uploading_phase") or {}).get("bytes_transfered") or 0)
    form = {"upload_phase": "finish", "video_id": vid, "access_token": tok,
            "title": re.sub(r"\s+", " ", job["titel"])[:255], "description": ig_text(job)}
    if entwurf:
        form["video_state"] = "DRAFT"
    elif termin_ts:
        form["video_state"] = "SCHEDULED"
        form["scheduled_publish_time"] = str(int(termin_ts))
    else:
        form["video_state"] = "PUBLISHED"
    js = jhttp("POST", f"{FB}/{FB_VERSION}/{pid}/video_reels", form=form)
    if js.get("success") is False:
        raise ApiFehler(200, js, FB)
    return vid


def fb_status(z, vid):
    f = fb_zugang(z)
    st = jhttp("GET", f"{FB}/{FB_VERSION}/{vid}?fields=status,permalink_url&access_token="
               + urllib.parse.quote(f["page_token"]))
    s = st.get("status") or {}
    return s.get("video_status"), (s.get("publishing_phase") or {}).get("publish_status"), st.get("permalink_url")


# ---------------------------------------------------------------- Warteschlange
def lies_txt(p):
    """render.py-.txt: TITEL / CAPTION / Hashtags."""
    s = open(p, encoding="utf-8").read()
    titel = re.search(r"TITEL:\s*(.+)", s)
    cap = re.search(r"CAPTION:\s*\n(.*?)\n\s*\n", s, re.S)
    tags = re.findall(r"(?<!\S)#[\w\d_]+", s.split("Quelle:")[0])
    return {"titel": titel.group(1).strip() if titel else "", "caption": cap.group(1).strip() if cap else "",
            "hashtags": list(dict.fromkeys(tags))}


def job_pfad(jid):
    return os.path.join(QUEUE, jid + ".json")


def archiv_ordner():
    return [QUEUE, os.path.join(CLIPS, "_auftrag", "upload_cloud"), os.path.join(CLIPS, "_auftrag", "upload_konflikt")]


def id_vergeben(jid):
    """True, wenn die ID schon lokal wartet oder schon in die Cloud übertragen wurde (Archiv)."""
    return any(os.path.exists(os.path.join(d, jid + ".json")) for d in archiv_ordner())


_NR = re.compile(r"^p?(\d{1,4})_")


def naechste_nummer():
    """Nächste freie Clip-Nummer über fertig/, tiktok/ (auch gepostet/), Warteschlange und Cloud-Archiv."""
    hoch = 0
    ordner = archiv_ordner() + [os.path.join(CLIPS, "fertig"), TIKTOK, os.path.join(TIKTOK, "gepostet")]
    for d in ordner:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            m = _NR.match(f)
            if m:
                hoch = max(hoch, int(m.group(1)))
    return hoch + 1


def cmd_nummer(args):
    n = int(args[0]) if args else 1
    start = naechste_nummer()
    print(" ".join(f"{i:02d}" for i in range(start, start + n)))


def alle_jobs():
    if not os.path.isdir(QUEUE):
        return []
    out = []
    for f in sorted(os.listdir(QUEUE)):
        if f.endswith(".json"):
            j = lade_json(os.path.join(QUEUE, f))
            if j:
                out.append(j)
    return sorted(out, key=lambda j: j["zeit"])


def cmd_add(args):
    import argparse
    ap = argparse.ArgumentParser(prog="publish.py add")
    ap.add_argument("datei")
    ap.add_argument("--zeit", required=True)
    ap.add_argument("--txt")
    ap.add_argument("--titel")
    ap.add_argument("--caption")
    ap.add_argument("--hashtags", help="durch Leerzeichen getrennt")
    ap.add_argument("--tags", help="YouTube-Tags, durch Komma getrennt")
    ap.add_argument("--nur", choices=["youtube", "instagram", "facebook"])
    ap.add_argument("--privat", action="store_true", help="Copyright-Risiko: YouTube privat, kein Instagram")
    ap.add_argument("--id")
    ap.add_argument("--kein-tiktok", action="store_true", help="kein TikTok-Paket anlegen")
    a = ap.parse_args(args)
    datei = os.path.abspath(a.datei)
    if not os.path.exists(datei):
        sys.exit(f"Datei fehlt: {datei}")
    txt = a.txt or os.path.splitext(datei)[0] + ".txt"
    info = lies_txt(txt) if os.path.exists(txt) else {"titel": "", "caption": "", "hashtags": []}
    if a.titel: info["titel"] = a.titel
    if a.caption: info["caption"] = a.caption
    if a.hashtags: info["hashtags"] = a.hashtags.split()
    if not info["titel"]:
        sys.exit("Kein Titel (--titel oder .txt mit 'TITEL:')")
    zeit = parse_zeit(a.zeit)
    jid = a.id or os.path.splitext(os.path.basename(datei))[0]
    if id_vergeben(jid):
        sys.exit(f"ID schon vergeben (Warteschlange/Cloud): {jid} – neue Nummer mit `publish.py nummer` holen und Datei umbenennen")
    rel = os.path.relpath(datei, CLIPS)
    job = {"id": jid, "datei": rel if not rel.startswith("..") else datei, "zeit": iso(zeit),
           "titel": info["titel"], "caption": info["caption"], "hashtags": info["hashtags"],
           "tags": [t.strip() for t in a.tags.split(",")] if a.tags else None, "privat": a.privat,
           "youtube": {"aktiv": a.nur in (None, "youtube"), "status": "wartet", "versuche": 0},
           "instagram": {"aktiv": a.nur in (None, "instagram") and not a.privat, "status": "wartet", "versuche": 0},
           "facebook": {"aktiv": a.nur in (None, "facebook") and not a.privat, "status": "wartet", "versuche": 0},
           "tiktok": {"aktiv": not a.kein_tiktok},
           "angelegt": iso(jetzt())}
    if job["tiktok"]["aktiv"]:
        tiktok_paket(job)
    speichere_json(job_pfad(jid), job)
    schreibe_tiktok_uebersicht(alle_jobs())
    print(f"+ {jid}  {zeit.astimezone(TZ):%d.%m. %H:%M}  YT={'ja' if job['youtube']['aktiv'] else 'nein'}"
          f"  FB={'ja' if job['facebook']['aktiv'] else 'nein'}"
          f"{' (privat)' if a.privat else ''}  IG={'ja' if job['instagram']['aktiv'] else 'nein'}  {info['titel']}")


def datei_von(job):
    d = job["datei"]
    return d if os.path.isabs(d) else os.path.join(CLIPS, d)


def ig_text(job):
    t = job["titel"] + ("\n" + job["caption"] if job.get("caption") else "")
    tags = [h for h in job.get("hashtags", [])][:30]
    t = (t + ("\n\n" + " ".join(tags) if tags else "")).strip()
    return t[:2200]


def _fehler(job, plat, e, z_warte_min=10, endgueltig=False):
    p = job[plat]
    p["versuche"] = p.get("versuche", 0) + 1
    p["letzter_fehler"] = str(e)[:500]
    if endgueltig or p["versuche"] >= MAX_VERSUCHE:
        p["status"] = "fehler"
        melde_fehler(f"{job['id']} {plat}: {e}")
    else:
        p["naechster_versuch"] = iso(jetzt() + timedelta(minutes=z_warte_min * p["versuche"]))
    log(f"! {job['id']} {plat}: {e}")


def melde_fehler(text):
    with open(FEHLER_TXT, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(TZ):%Y-%m-%d %H:%M} {text}\n")


def bereit(p):
    n = p.get("naechster_versuch")
    return not n or parse_zeit(n) <= jetzt()


def schritt_youtube(z, job):
    p = job["youtube"]
    if "youtube" not in z or not p.get("aktiv") or p["status"] not in ("wartet",) or not bereit(p):
        return False
    termin = parse_zeit(job["zeit"])
    sofort = termin <= jetzt() + timedelta(minutes=YT_MIN_VORLAUF_MIN)
    meta = yt_meta(job, job.get("privat"), None if sofort else iso(termin))
    try:
        vid = yt_upload(z, datei_von(job), meta)
    except AuthFehler as e:
        _fehler(job, "youtube", e, endgueltig=True)
        return True
    except ApiFehler as e:
        g = e.grund()
        if g in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"):
            p["naechster_versuch"] = iso(jetzt() + timedelta(hours=3))
            p["letzter_fehler"] = g
            log(f"~ {job['id']} youtube: {g} – neuer Versuch in 3 h")
            if termin < jetzt():
                melde_fehler(f"{job['id']} youtube: {g} (Termin verpasst, wird nachgeholt)")
        elif g in ("invalidTitle", "invalidDescription", "invalidTags", "invalidPublishAt", "invalidCategoryId",
                   "mediaBodyRequired", "invalidVideoMetadata"):
            _fehler(job, "youtube", e, endgueltig=True)
        else:
            _fehler(job, "youtube", e)
        return True
    except (OSError, http.client.HTTPException) as e:
        _fehler(job, "youtube", e)
        return True
    p.update({"status": "privat" if job.get("privat") else ("veroeffentlicht" if sofort else "geplant"),
              "video_id": vid, "url": f"https://youtube.com/shorts/{vid}", "hochgeladen": iso(jetzt())})
    p.pop("naechster_versuch", None)
    log(f"✓ {job['id']} youtube {p['status']} {p['url']}"
        + ("" if sofort or job.get("privat") else f" (öffentlich ab {termin.astimezone(TZ):%d.%m. %H:%M})"))
    return True


def schritt_instagram(z, job):
    p = job["instagram"]
    if "instagram" not in z or "dropbox" not in z or not p.get("aktiv") \
            or p["status"] in ("veroeffentlicht", "fehler") or not bereit(p):
        return False
    termin = parse_zeit(p.get("zeit") or job["zeit"])   # eigener IG-Termin möglich
    try:
        # 1) Datei in Dropbox + direkter Link (jederzeit vorab)
        if p["status"] == "wartet":
            ziel = "/" + os.path.basename(datei_von(job))
            dbx_hochladen(z, datei_von(job), ziel)
            p["dropbox_pfad"] = ziel
            p["share_url"] = dbx_link(z, ziel)
            p["status"] = "dropbox"
            log(f"· {job['id']} instagram: in Dropbox ({ziel})")
            return True
        # 2) Container kurz vor dem Termin
        if p["status"] == "dropbox":
            if termin - jetzt() > timedelta(minutes=IG_VORLAUF_MIN):
                return False
            url, ctype = direkt_url(p["share_url"])
            p["video_url"] = url
            p["container_id"] = ig_container(z, url, ig_text(job))
            p["container_seit"] = iso(jetzt())
            p["status"] = "container"
            log(f"· {job['id']} instagram: Container {p['container_id']} ({ctype})")
            return True
        # 3) warten bis FINISHED, dann zur Zeit veröffentlichen
        if p["status"] == "container":
            code, info = ig_container_status(z, p["container_id"])
            p["container_status"] = code
            if code == "PUBLISHED":
                p["status"] = "veroeffentlicht"
                log(f"✓ {job['id']} instagram (war schon veröffentlicht)")
            elif code in ("ERROR", "EXPIRED"):
                p["status"] = "dropbox"           # neuer Container beim nächsten Durchlauf
                _fehler(job, "instagram", f"Container {code}: {info}", z_warte_min=5)
            elif code == "FINISHED" and termin <= jetzt():
                mid, link = ig_publish(z, p["container_id"])
                p.update({"status": "veroeffentlicht", "media_id": mid, "url": link, "veroeffentlicht": iso(jetzt())})
                log(f"✓ {job['id']} instagram veröffentlicht {link or mid}")
                try:
                    dbx_loeschen(z, p["dropbox_pfad"])
                    p["dropbox_geloescht"] = True
                except Exception as e:
                    log(f"(Dropbox-Löschen später: {e})")
            elif code == "IN_PROGRESS" and parse_zeit(p["container_seit"]) < jetzt() - timedelta(hours=2):
                p["status"] = "dropbox"
                _fehler(job, "instagram", "Container hängt > 2 h", z_warte_min=5)
            return True
    except AuthFehler as e:
        _fehler(job, "instagram", e, endgueltig=True)
        return True
    except ApiFehler as e:
        g = e.grund()
        if "dropbox_link_nicht_direkt" in json.dumps(e.js):
            _fehler(job, "instagram", e, z_warte_min=5)
        elif e.status == 429 or "meta_4_" in g or "meta_9_" in g or "meta_32" in g:
            p["naechster_versuch"] = iso(jetzt() + timedelta(minutes=30))
            p["letzter_fehler"] = g
            log(f"~ {job['id']} instagram: Limit ({g}) – neuer Versuch in 30 min")
        else:
            _fehler(job, "instagram", e)
        return True
    except (OSError, http.client.HTTPException) as e:
        _fehler(job, "instagram", e)
        return True
    return False


def schritt_facebook(z, job):
    p = job.get("facebook")
    if not p or not p.get("aktiv") or "facebook" not in z or p["status"] == "fehler" or not bereit(p):
        return False
    termin = parse_zeit(job["zeit"])
    try:
        if p["status"] == "wartet":
            vorlauf = termin - jetzt()
            if vorlauf > timedelta(days=28):
                return False
            plan = vorlauf > timedelta(minutes=12)
            vid = fb_reel(z, datei_von(job), job, termin.timestamp() if plan else None)
            p.update({"status": "geplant" if plan else "veroeffentlicht", "video_id": vid,
                      "url": f"https://www.facebook.com/reel/{vid}", "hochgeladen": iso(jetzt()),
                      "naechster_versuch": iso(jetzt() + timedelta(minutes=10))})
            log(f"✓ {job['id']} facebook {p['status']} {p['url']}"
                + (f" (öffentlich ab {termin.astimezone(TZ):%d.%m. %H:%M})" if plan else ""))
            return True
        if p["status"] in ("geplant", "veroeffentlicht") and not p.get("bestaetigt"):
            vs, pub, link = fb_status(z, p["video_id"])
            p["fb_status"] = f"{vs}/{pub}"
            if vs in ("error", "expired", "upload_failed") or pub == "error":
                _fehler(job, "facebook", f"Facebook-Verarbeitung: {vs}/{pub}", endgueltig=True)
            elif pub == "published":
                p.update({"status": "veroeffentlicht", "bestaetigt": True})
                if link:
                    p["url"] = link if link.startswith("http") else "https://www.facebook.com" + link
                p.pop("naechster_versuch", None)
            else:
                p["naechster_versuch"] = iso(jetzt() + timedelta(minutes=10))
            return True
    except AuthFehler as e:
        _fehler(job, "facebook", e, endgueltig=True)
        return True
    except ApiFehler as e:
        g = e.grund()
        if e.status == 429 or "meta_4_" in g or "meta_32_" in g or "meta_368" in g:
            p["naechster_versuch"] = iso(jetzt() + timedelta(minutes=30))
            p["letzter_fehler"] = g
            log(f"~ {job['id']} facebook: Limit ({g}) – neuer Versuch in 30 min")
        elif "meta_190" in g:
            _fehler(job, "facebook", "Facebook-Token ungültig – 'einrichten facebook' neu ausführen", endgueltig=True)
        else:
            _fehler(job, "facebook", e)
        return True
    except (OSError, http.client.HTTPException) as e:
        _fehler(job, "facebook", e)
        return True
    return False


def _fixiert(job):
    """Termin steht fest, sobald etwas öffentlich geplant/gepostet ist oder der IG-Container läuft."""
    yt, ig, fb = job["youtube"], job["instagram"], job.get("facebook") or {}
    return (yt.get("aktiv") and yt["status"] in ("geplant", "veroeffentlicht")) or \
           (ig.get("aktiv") and ig["status"] in ("container", "veroeffentlicht")) or \
           (fb.get("aktiv") and fb.get("status") in ("geplant", "veroeffentlicht"))


def _oeffentlich(job):
    yt, ig, fb = job["youtube"], job["instagram"], job.get("facebook") or {}
    return (yt.get("aktiv") and not job.get("privat") and yt["status"] != "fehler") or \
           (ig.get("aktiv") and ig["status"] != "fehler") or \
           (fb.get("aktiv") and fb.get("status") != "fehler")


def staffeln(jobs):
    """Verhindert Post-Lawinen: Jeder noch offene Clip bekommt den frühesten Termin >= seinem Wunschtermin
    (frühestens jetzt + 5 Min.), der zu allen anderen öffentlichen Posts >= MIN_ABSTAND_MIN Abstand hat."""
    gap = timedelta(minutes=MIN_ABSTAND_MIN)
    frueh = (jetzt() + timedelta(minutes=5)).replace(second=0, microsecond=0) + timedelta(minutes=1)
    oeff = [j for j in jobs if _oeffentlich(j)]
    belegt = [parse_zeit(j["zeit"]) for j in oeff if _fixiert(j)]
    for job in sorted((j for j in oeff if not _fixiert(j)), key=lambda j: j["zeit"]):
        t = parse_zeit(job["zeit"])
        neu = max(t, frueh)
        while True:
            konflikt = [o for o in belegt if abs(o - neu) < gap]
            if not konflikt:
                break
            neu = max(konflikt) + gap
        belegt.append(neu)
        if neu != t:
            job.setdefault("urspruenglich", job["zeit"])
            job["zeit"] = iso(neu)
            speichere_json(job_pfad(job["id"]), job)
            log(f"→ {job['id']} verschoben auf {neu.astimezone(TZ):%d.%m. %H:%M}")


def cmd_zeit(args):
    """zeit ID "2026-11-16 20:00"  – Termin eines noch nicht veröffentlichten Clips ändern."""
    job = lade_json(job_pfad(args[0]))
    if not job:
        sys.exit("Unbekannt: " + args[0])
    if _fixiert(job):
        sys.exit("Schon geplant/veröffentlicht – Termin nicht mehr änderbar.")
    job["zeit"] = iso(parse_zeit(args[1]))
    speichere_json(job_pfad(job["id"]), job)
    print(f"{job['id']} -> {parse_zeit(job['zeit']).astimezone(TZ):%d.%m. %H:%M}")


def cmd_tick(args):
    z = zugang()
    jobs = alle_jobs()
    if not jobs:
        schreibe_status([])
        return
    staffeln(jobs)
    jobs = alle_jobs()
    for job in jobs:
        if "facebook" not in job and parse_zeit(job["zeit"]) > jetzt():   # Jobs von vor dem Facebook-Einbau
            job["facebook"] = {"aktiv": not job.get("privat"), "status": "wartet", "versuche": 0}
            speichere_json(job_pfad(job["id"]), job)
        plats = [p for p in ("youtube", "instagram", "facebook") if p in job]
        if not os.path.exists(datei_von(job)) and any(
                job[p].get("aktiv") and job[p]["status"] in ("wartet",) for p in plats):
            for plat in plats:
                if job[plat].get("aktiv") and job[plat]["status"] == "wartet":
                    _fehler(job, plat, "Videodatei fehlt", endgueltig=True)
            speichere_json(job_pfad(job["id"]), job)
            continue
        for schritt in (schritt_youtube, schritt_instagram, schritt_facebook):
            try:
                if schritt(z, job):
                    speichere_json(job_pfad(job["id"]), job)
            except Exception as e:  # nie den ganzen Lauf abbrechen
                log(f"!! {job['id']} unerwartet: {e!r}")
    schreibe_status(alle_jobs())


def tiktok_text(job):
    t = job["titel"] + ("\n" + job["caption"] if job.get("caption") else "")
    tags = " ".join(job.get("hashtags", []))
    return (t + ("\n\n" + tags if tags else "")).strip()[:2200]


def tiktok_paket(job):
    """Kopiert das Video nach Clips/tiktok/ und legt die Caption daneben (zum manuellen Posten)."""
    import shutil
    os.makedirs(TIKTOK, exist_ok=True)
    ziel = os.path.join(TIKTOK, job["id"] + ".mp4")
    quelle = datei_von(job)
    if os.path.exists(quelle) and (not os.path.exists(ziel) or os.path.getsize(ziel) != os.path.getsize(quelle)):
        shutil.copyfile(quelle, ziel)
    warn = ("⚠️ FREMDE MUSIK / COPYRIGHT-RISIKO: nur PRIVAT posten oder weglassen\n\n" if job.get("privat") else "")
    with open(os.path.join(TIKTOK, job["id"] + ".txt"), "w", encoding="utf-8") as f:
        f.write(warn + tiktok_text(job) + "\n")
    job.setdefault("tiktok", {})["aktiv"] = True
    job["tiktok"]["paket"] = os.path.relpath(ziel, CLIPS)


def schreibe_tiktok_uebersicht(jobs):
    """Clips/tiktok/_POSTEN.txt: welche Videos wann auf TikTok posten (max. 10/Tag, >= 60 Min. Abstand)."""
    if not os.path.isdir(TIKTOK):
        return
    offen = [j for j in jobs if (j.get("tiktok") or {}).get("aktiv")
             and os.path.exists(os.path.join(TIKTOK, j["id"] + ".mp4"))]
    z = ["TIKTOK – zum Posten bereit (Stand %s)" % datetime.now(TZ).strftime("%d.%m. %H:%M"),
         "So geht's: Video aus diesem Ordner hochladen, Text aus der gleichnamigen .txt einfügen, posten.",
         "Danach .mp4 + .txt löschen (oder in einen Unterordner 'gepostet' schieben) – dann verschwindet der Clip hier.",
         "Regeln: max. %d Posts pro Tag, mind. 60–90 Min. Abstand (TikTok sperrt ab ~15/Tag)." % TIKTOK_PRO_TAG, ""]
    tag, n = None, 0
    for j in sorted(offen, key=lambda j: j["zeit"]):
        t = parse_zeit(j["zeit"]).astimezone(TZ)
        if t.date() != tag:
            tag, n = t.date(), 0
            z.append(f"— {t:%A %d.%m.} —".replace("Monday", "Montag").replace("Tuesday", "Dienstag")
                     .replace("Wednesday", "Mittwoch").replace("Thursday", "Donnerstag").replace("Friday", "Freitag")
                     .replace("Saturday", "Samstag").replace("Sunday", "Sonntag"))
        n += 1
        hinweis = "  ⚠️ nur privat (Musik)" if j.get("privat") else ""
        if n > TIKTOK_PRO_TAG:
            hinweis += "  ⏭ Tageslimit – erst morgen posten"
        z.append(f"  {t:%H:%M}  {j['id']}.mp4{hinweis}")
    if not offen:
        z.append("(nichts offen)")
    with open(os.path.join(TIKTOK, "_POSTEN.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(z) + "\n")


def cmd_tiktok_paket(args):
    """Legt fehlende TikTok-Pakete für alle (oder die genannten) Jobs an."""
    for job in alle_jobs():
        if args and job["id"] not in args:
            continue
        if (job.get("tiktok") or {}).get("aktiv") is False and not args:
            continue
        if not os.path.exists(datei_von(job)):
            print(f"- {job['id']}: Videodatei fehlt")
            continue
        tiktok_paket(job)
        speichere_json(job_pfad(job["id"]), job)
        print(f"+ TikTok-Paket: tiktok/{job['id']}.mp4 + .txt")
    schreibe_tiktok_uebersicht(alle_jobs())


def schreibe_status(jobs):
    zeilen = [f"Stand {datetime.now(TZ):%Y-%m-%d %H:%M}"]
    for j in jobs:
        t = parse_zeit(j["zeit"]).astimezone(TZ)
        yt, ig = j["youtube"], j["instagram"]
        zeilen.append(f"{t:%d.%m. %H:%M}  {j['id'][:45]:45}  YT:{yt['status'] if yt.get('aktiv') else '-':16}"
                      f" IG:{ig['status'] if ig.get('aktiv') else '-':16}"
                      f" FB:{(j.get('facebook') or {}).get('status', '-') if (j.get('facebook') or {}).get('aktiv') else '-':16}"
                      f" {yt.get('url', '')} {ig.get('url') or ''} {(j.get('facebook') or {}).get('url') or ''}".rstrip())
    os.makedirs(os.path.dirname(STATUS_TXT), exist_ok=True)
    with open(STATUS_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(zeilen) + "\n")
    try:
        schreibe_tiktok_uebersicht(jobs)
    except Exception as e:
        log(f"(TikTok-Übersicht: {e})")


def cmd_status(args):
    if not alle_jobs() and os.path.isdir(os.path.join(CLIPS, "_auftrag", "upload_cloud")) and os.path.exists(STATUS_TXT):
        print("(Cloud-Betrieb – Stand der letzten Cloud-Runde; aktualisieren: python3 cloud_sync.py pull-status)")
        print(open(STATUS_TXT, encoding="utf-8").read())
        if os.path.exists(FEHLER_TXT):
            print("FEHLER:\n" + open(FEHLER_TXT, encoding="utf-8").read()[-2000:])
        return
    schreibe_status(alle_jobs())
    print(open(STATUS_TXT, encoding="utf-8").read())
    if os.path.exists(FEHLER_TXT):
        print("FEHLER:\n" + open(FEHLER_TXT, encoding="utf-8").read()[-2000:])


def cmd_test(args):
    z = zugang()
    for name, fn in (("YouTube", lambda: "Kanal: " + yt_kanal(z)),
                     ("Instagram", lambda: "@%s, Posts letzte 24 h: %s/%s" % ((z.get("instagram") or {}).get("username"), *ig_limit(z))),
                     ("Facebook", lambda: "Seite: " + (jhttp("GET", f"{FB}/{FB_VERSION}/{fb_zugang(z)['page_id']}?fields=name&access_token="
                                                          + urllib.parse.quote(fb_zugang(z)["page_token"])).get("name") or "?")),
                     ("Dropbox", lambda: jhttp("POST", DBX_API + "/2/users/get_current_account", body=b"null",
                                              headers={"Authorization": "Bearer " + dbx_token(z),
                                                       "Content-Type": "application/json"}).get("email", "ok"))):
        try:
            print(f"✅ {name}: {fn()}")
        except Exception as e:
            print(f"❌ {name}: {e}")


def cmd_probe_youtube(args):
    z = zugang()
    job = {"titel": "Test-Upload (privat) – bitte ignorieren", "caption": "API-Test", "hashtags": ["#test"]}
    vid = yt_upload(z, args[0], yt_meta(job, True, None))
    print(f"✅ privates Test-Video: https://youtube.com/shorts/{vid} (in YouTube Studio prüfen/löschen)")


def cmd_probe_instagram(args):
    z = zugang()
    ziel = "/_probe_" + os.path.basename(args[0])
    dbx_hochladen(z, args[0], ziel)
    url, ctype = direkt_url(dbx_link(z, ziel))
    print(f"Direkt-URL: {url} ({ctype})")
    cid = ig_container(z, url, "Test")
    for _ in range(20):
        code, info = ig_container_status(z, cid)
        print("Container:", code, info or "")
        if code in ("FINISHED", "ERROR", "EXPIRED"):
            break
        time.sleep(15)
    dbx_loeschen(z, ziel)
    print("✅ Instagram nimmt den Dropbox-Link an (nichts veröffentlicht)." if code == "FINISHED"
          else "❌ Instagram konnte das Video nicht laden – Details oben.")


def cmd_probe_facebook(args):
    z = zugang()
    job = {"titel": "Test (Entwurf) – bitte ignorieren", "caption": "API-Test", "hashtags": []}
    vid = fb_reel(z, args[0], job, entwurf=True)
    for _ in range(12):
        vs, pub, _l = fb_status(z, vid)
        print("Status:", vs, pub)
        if vs in ("ready", "error", "expired", "upload_failed"):
            break
        time.sleep(10)
    print("✅ Facebook-Upload klappt (nur ENTWURF, nichts veröffentlicht – in der Meta Business Suite löschen)."
          if vs == "ready" else "❌ Facebook-Upload hat nicht geklappt – Status oben.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "einrichten":
        {"youtube": einrichten_youtube, "instagram": einrichten_instagram, "dropbox": einrichten_dropbox,
         "facebook": einrichten_facebook}[args[0]](args[1:])
    elif cmd == "add":
        cmd_add(args)
    elif cmd == "nummer":
        cmd_nummer(args)
    elif cmd == "tick":
        cmd_tick(args)
    elif cmd == "tiktok-paket":
        cmd_tiktok_paket(args)
    elif cmd == "zeit":
        cmd_zeit(args)
    elif cmd == "status":
        cmd_status(args)
    elif cmd == "test":
        cmd_test(args)
    elif cmd == "probe-youtube":
        cmd_probe_youtube(args)
    elif cmd == "probe-facebook":
        cmd_probe_facebook(args)
    elif cmd == "probe-instagram":
        cmd_probe_instagram(args)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
