"""Nachbildung der YouTube-, Instagram- und Dropbox-Endpunkte für Offline-Tests von publish.py.
Pfade: /<host>/<pfad> (publish.py biegt https://host/pfad im Testmodus hierher um)."""
import json, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
BASE = f"http://127.0.0.1:{PORT}"
S = {"yt_sessions": {}, "yt_videos": {}, "dbx": {}, "links": {}, "containers": {}, "published": [],
     "polls": {}, "log": [], "fail_first_put": True, "quota_titles": set(),
     "fb": {}, "fb_fail_first": True, "fb_alle_veroeffentlicht": False}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code, obj=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def log_message(self, *a):
        pass

    def route(self):
        u = urllib.parse.urlsplit(self.path)
        parts = u.path.lstrip("/").split("/", 1)
        host, path = parts[0], "/" + (parts[1] if len(parts) > 1 else "")
        q = dict(urllib.parse.parse_qsl(u.query))
        return host, path, q

    def do_HEAD(self):
        host, path, q = self.route()
        S["log"].append(("HEAD", host, path))
        if host == "dl.dropboxusercontent.com":
            if q.get("rlkey") != "K1":
                return self._send(404, {"error": "bad rlkey"})
            return self._send(302, headers={"Location": f"https://uc42.dl.dropboxusercontent.com/cd/0/get/TMP{path}"})
        if host == "uc42.dl.dropboxusercontent.com":
            return self._send(200, headers={"Content-Type": "video/mp4"}, raw=b"")
        self._send(404, {})

    def do_GET(self):
        host, path, q = self.route()
        S["log"].append(("GET", host, path))
        if host == "www.googleapis.com" and path.startswith("/youtube/v3/channels"):
            return self._send(200, {"items": [{"snippet": {"title": "ClipsForTheUnemployed"}}]})
        if host == "graph.facebook.com":
            if path.endswith("/oauth/access_token"):
                assert q.get("grant_type") == "fb_exchange_token" and q.get("client_secret"), q
                return self._send(200, {"access_token": "FB_LONG_USER", "token_type": "bearer"})
            if path.endswith("/me/accounts"):
                return self._send(200, {"data": [{"id": "PAGE1", "name": "ClipsForTheUnemployed",
                                                  "access_token": "PAGE_TOK", "tasks": ["CREATE_CONTENT", "MANAGE"]}]})
            if path.endswith("/PAGE1"):
                return self._send(200, {"name": "ClipsForTheUnemployed", "id": "PAGE1"})
            vid = path.rsplit("/", 1)[-1]
            if vid in S["fb"]:
                v = S["fb"][vid]
                st = v.get("state")
                pub = {"DRAFT": "draft", "PUBLISHED": "published",
                       "SCHEDULED": "published" if S["fb_alle_veroeffentlicht"] else "scheduled"}.get(st, "not_started")
                return self._send(200, {"status": {"video_status": "ready" if v["bytes"] == v.get("size") else "uploading",
                                                   "uploading_phase": {"bytes_transfered": v["bytes"]},
                                                   "publishing_phase": {"publish_status": pub}},
                                        "permalink_url": f"/reel/{vid}"})
        if host == "graph.instagram.com":
            if path.endswith("/me"):
                return self._send(200, {"user_id": "17841400000", "username": "clipsfortheunemployed", "account_type": "BUSINESS"})
            if path == "/access_token":
                return self._send(200, {"access_token": "IG_LONG", "token_type": "bearer", "expires_in": 5183944})
            if path == "/refresh_access_token":
                return self._send(200, {"access_token": "IG_REFRESHED", "expires_in": 5183944})
            if path.endswith("/content_publishing_limit"):
                return self._send(200, {"data": [{"quota_usage": len(S["published"]), "config": {"quota_total": 100}}]})
            cid = path.rsplit("/", 1)[-1]
            if cid in S["containers"]:
                c = S["containers"][cid]
                S["polls"][cid] = S["polls"].get(cid, 0) + 1
                code = "PUBLISHED" if c.get("pub") else ("FINISHED" if S["polls"][cid] >= 2 else "IN_PROGRESS")
                return self._send(200, {"status_code": code, "id": cid})
            if cid.startswith("M"):
                return self._send(200, {"permalink": f"https://www.instagram.com/reel/{cid}/"})
        self._send(404, {"error": {"message": "unknown " + host + path}})

    def do_PUT(self):
        host, path, q = self.route()
        body = self._body()
        S["log"].append(("PUT", host, path, len(body), self.headers.get("Content-Range")))
        sid = path.rsplit("/", 1)[-1]
        sess = S["yt_sessions"].get(sid)
        if not sess:
            return self._send(404, {"error": "no session"})
        cr = self.headers.get("Content-Range")
        if cr and cr.startswith("bytes */"):
            got = len(sess["data"])
            if got >= sess["total"]:
                return self._send(200, {"id": sess["vid"]})
            return self._send(308, headers={"Range": f"bytes=0-{got - 1}"} if got else {})
        if S["fail_first_put"] and not sess["data"]:
            # nur die erste Hälfte "kommt an", dann 503
            sess["data"] = body[: len(body) // 2]
            S["fail_first_put"] = False
            return self._send(503, {"error": {"errors": [{"reason": "backendError"}]}})
        start = int(cr.split()[1].split("-")[0]) if cr else 0
        assert start == len(sess["data"]), (start, len(sess["data"]))
        sess["data"] += body
        if len(sess["data"]) != sess["total"]:
            return self._send(400, {"error": "size mismatch"})
        S["yt_videos"][sess["vid"]] = {"meta": sess["meta"], "bytes": len(sess["data"])}
        return self._send(200, {"id": sess["vid"], "status": sess["meta"]["status"]})

    def do_POST(self):
        host, path, q = self.route()
        body = self._body()
        S["log"].append(("POST", host, path))
        form = dict(urllib.parse.parse_qsl(body.decode(errors="ignore"))) if b"=" in body[:200] and not body.startswith(b"{") else {}
        if host == "oauth2.googleapis.com" and path == "/token":
            if form.get("refresh_token") == "REVOKED":
                return self._send(400, {"error": "invalid_grant"})
            if form.get("grant_type") == "authorization_code":
                assert form.get("code_verifier") and form.get("redirect_uri", "").startswith("http://127.0.0.1:"), form
                return self._send(200, {"access_token": "ya29.TEST", "expires_in": 3599, "refresh_token": "RT_NEU"})
            return self._send(200, {"access_token": "ya29.TEST", "expires_in": 3599})
        if host == "www.googleapis.com" and path.startswith("/upload/youtube/v3/videos"):
            meta = json.loads(body)
            if meta["snippet"]["title"] in S["quota_titles"]:
                return self._send(403, {"error": {"errors": [{"reason": "uploadLimitExceeded"}]}})
            sid = f"s{len(S['yt_sessions']) + 1}"
            S["yt_sessions"][sid] = {"meta": meta, "total": int(self.headers["X-Upload-Content-Length"]),
                                     "data": b"", "vid": f"VID{len(S['yt_sessions']) + 1}"}
            return self._send(200, {}, headers={"Location": f"{BASE}/www.googleapis.com/upload/session/{sid}"})
        if host == "api.dropboxapi.com":
            if path == "/oauth2/token":
                return self._send(200, {"access_token": "sl.DBX", "expires_in": 14400})
            if path == "/2/users/get_current_account":
                return self._send(200, {"email": "joey@example.com"})
            if path == "/2/sharing/create_shared_link_with_settings":
                p = json.loads(body)["path"]
                if p not in S["dbx"]:
                    return self._send(409, {"error_summary": "path/not_found/", "error": {".tag": "path"}})
                url = f"https://www.dropbox.com/scl/fi/abc{len(S['links'])}{p}?rlkey=K1&dl=0"
                if p in S["links"]:
                    return self._send(409, {"error_summary": "shared_link_already_exists/", "error": {
                        ".tag": "shared_link_already_exists",
                        "shared_link_already_exists": {"metadata": {"url": S["links"][p]}}}})
                S["links"][p] = url
                return self._send(200, {"url": url})
            if path == "/2/files/delete_v2":
                p = json.loads(body)["path"]
                if S["dbx"].pop(p, None) is None:
                    return self._send(409, {"error_summary": "path_lookup/not_found/"})
                return self._send(200, {"metadata": {"path_display": p}})
        if host == "content.dropboxapi.com" and path == "/2/files/upload":
            arg = json.loads(self.headers["Dropbox-API-Arg"])
            S["dbx"][arg["path"]] = len(body)
            return self._send(200, {"path_display": arg["path"], "size": len(body)})
        if host == "graph.facebook.com" and path.endswith("/PAGE1/video_reels"):
            assert form.get("access_token") == "PAGE_TOK", form
            if form.get("upload_phase") == "start":
                vid = f"FV{len(S['fb']) + 1}"
                S["fb"][vid] = {"bytes": 0}
                return self._send(200, {"video_id": vid, "upload_url": f"https://rupload.facebook.com/video-upload/{vid}"})
            if form.get("upload_phase") == "finish":
                v = S["fb"][form["video_id"]]
                assert v["bytes"] == v["size"], "Upload unvollständig"
                if form["video_state"] == "SCHEDULED":
                    import time as _t
                    ts = int(form["scheduled_publish_time"])
                    if ts < _t.time() + 600 or ts > _t.time() + 29 * 86400:
                        return self._send(400, {"error": {"code": 100, "message": "invalid scheduled_publish_time"}})
                    v["ts"] = ts
                v.update(state=form["video_state"], title=form.get("title"), description=form.get("description"))
                return self._send(200, {"success": True})
        if host == "rupload.facebook.com":
            vid = path.rsplit("/", 1)[-1]
            v = S["fb"][vid]
            assert self.headers.get("Authorization") == "OAuth PAGE_TOK"
            off, size = int(self.headers["offset"]), int(self.headers["file_size"])
            v["size"] = size
            assert off == v["bytes"], (off, v["bytes"])
            if S["fb_fail_first"]:
                S["fb_fail_first"] = False
                v["bytes"] += len(body) // 3
                return self._send(500, {"error": {"message": "transient"}})
            v["bytes"] += len(body)
            return self._send(200, {"success": True})
        if host == "graph.instagram.com":
            if path.endswith("/media"):
                assert form.get("media_type") == "REELS", form
                assert form.get("video_url", "").startswith("https://uc42.dl.dropboxusercontent.com/"), form
                cid = f"C{len(S['containers']) + 1}"
                S["containers"][cid] = {"form": form}
                return self._send(200, {"id": cid})
            if path.endswith("/media_publish"):
                cid = form["creation_id"]
                c = S["containers"][cid]
                if c.get("pub"):
                    return self._send(400, {"error": {"code": 9007, "message": "already published"}})
                c["pub"] = True
                mid = f"M{len(S['published']) + 1}"
                S["published"].append((cid, mid, c["form"]["caption"]))
                return self._send(200, {"id": mid})
        self._send(404, {"error": {"message": "unknown " + host + path}})


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    start()
    print("mock on", BASE)
    threading.Event().wait()
