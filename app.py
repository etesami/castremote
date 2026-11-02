"""castremote: paste a video URL on your phone, play it on a Chromecast with subtitles.

Run on any always-on machine on the same network as the Chromecast:
    python app.py            # then open http://<this-machine-ip>:8765 on your phone
Settings live in config.json next to this file: media_dir, port, cast_hosts, public_host, log_requests.
Env vars PORT, CAST_HOSTS and CASTREMOTE_CONFIG override it.
"""
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.parse
import uuid

import pychromecast
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

import subs

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.environ.get("CASTREMOTE_CONFIG", os.path.join(HERE, "config.json"))
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise SystemExit(f"Could not parse {path}: {e}")


CONFIG = load_config()
PORT = int(os.environ.get("PORT") or CONFIG.get("port") or 8765)
_hosts = os.environ.get("CAST_HOSTS")
KNOWN_HOSTS = ([h.strip() for h in _hosts.split(",") if h.strip()] if _hosts else CONFIG.get("cast_hosts")) or None
PUBLIC_HOST = CONFIG.get("public_host") or None  # address the Chromecast uses to reach this machine
MEDIA_DIR = os.path.realpath(os.path.expanduser(CONFIG["media_dir"])) if CONFIG.get("media_dir") else None
VIDEO_EXTS = (".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".ts", ".mp3", ".m4a")

# The page polls /api/status every second, so request lines flood the log. Errors are still shown.
if not CONFIG.get("log_requests"):
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__, static_folder=None)
lock = threading.RLock()
casts = {}          # uuid -> Chromecast
browser = None
vtt_store = {}      # name -> vtt text
session = {}        # current playback: cast, url, ctype, title, cues, offset, lang


# ---------------------------------------------------------------- devices

def discover():
    global browser
    found, new_browser = pychromecast.get_chromecasts(known_hosts=KNOWN_HOSTS, tries=2, timeout=10)
    with lock:
        if browser:
            try:
                browser.stop_discovery()
            except Exception:
                pass
        browser = new_browser
        active = session.get("cast")
        for cc in found:
            u = str(cc.uuid)
            if active is not None and str(active.uuid) == u:
                continue  # keep the connected one
            casts[u] = cc
    return list_devices()


def list_devices():
    with lock:
        devs = [{"id": u, "name": c.name, "model": c.model_name} for u, c in casts.items()]
    devs.sort(key=lambda d: ("ultra" not in (d["model"] or "").lower(), d["name"] or ""))
    return devs


def get_cast(dev_id):
    with lock:
        cc = casts.get(dev_id) if dev_id else (session.get("cast") or next(iter(casts.values()), None))
    if cc is None:
        raise ValueError("No Chromecast found. Tap 'Search' (and check you're on the same network).")
    cc.wait(timeout=10)
    return cc


def lan_ip_towards(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 8009))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------- media folder

def resolve_media(rel):
    """Absolute path for a path inside MEDIA_DIR, or None if it's outside the folder or hidden."""
    if not MEDIA_DIR:
        return None
    path = os.path.realpath(os.path.join(MEDIA_DIR, rel))
    if path != MEDIA_DIR and not path.startswith(MEDIA_DIR + os.sep):
        return None
    inner = os.path.relpath(path, MEDIA_DIR)
    if inner != "." and any(part.startswith(".") for part in inner.split(os.sep)):
        return None
    return path


def base_url():
    if PUBLIC_HOST:
        return f"http://{PUBLIC_HOST}:{PORT}"
    with lock:
        cc = session.get("cast") or next(iter(casts.values()), None)
    return f"http://{lan_ip_towards(cc.cast_info.host if cc else '8.8.8.8')}:{PORT}"


def media_url(rel):
    return f"{base_url()}/media/{urllib.parse.quote(rel)}"


def local_media_path(url):
    """If url points at this app's /media/ route, return the file's local path."""
    parts = urllib.parse.urlsplit(url)
    if not parts.path.startswith("/media/") or parts.netloc not in (request.host, urllib.parse.urlsplit(base_url()).netloc):
        return None
    path = resolve_media(urllib.parse.unquote(parts.path[len("/media/"):]))
    return path if path and os.path.isfile(path) else None


def natural_key(name):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def local_sidecars(path):
    folder, fname = os.path.split(path)
    base = os.path.splitext(fname)[0].lower()
    found = []
    for name in os.listdir(folder):
        if name.lower().endswith(subs.SUB_EXTS) and not name.startswith("."):
            rel = os.path.relpath(os.path.join(folder, name), MEDIA_DIR)
            found.append({"name": name, "url": media_url(rel), "match": name.lower().startswith(base)})
    found.sort(key=lambda f: (not f["match"], natural_key(f["name"])))
    return found


# ---------------------------------------------------------------- helpers

def content_type_for(url):
    ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
    return {".webm": "video/webm", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
            ".m3u8": "application/x-mpegURL"}.get(ext, "video/mp4")  # mp4 also works for most mkv


SUPPORTED_V = {"h264", "hevc", "vp8", "vp9", "av1"}
SUPPORTED_A = {"aac", "mp3", "opus", "vorbis", "flac"}


def err(msg, code=400):
    return jsonify({"error": str(msg)}), code


def publish_vtt(cast):
    """Render current cues (+offset) to a fresh VTT URL the Chromecast can fetch."""
    name = uuid.uuid4().hex[:12] + ".vtt"
    vtt_store[name] = subs.to_vtt(session["cues"], session.get("offset", 0.0))
    for old in list(vtt_store)[:-5]:
        vtt_store.pop(old, None)
    return f"http://{lan_ip_towards(cast.cast_info.host)}:{PORT}/subs/{name}"


def load_media(cast, current_time=None):
    mc = cast.media_controller
    kw = {}
    if session.get("cues"):
        kw = dict(subtitles=publish_vtt(cast), subtitles_lang=session.get("lang", "fa"), subtitles_mime="text/vtt")
    mc.play_media(session["url"], session["ctype"], title=session["title"], current_time=current_time, **kw)
    mc.block_until_active(timeout=15)


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/subs/<name>")
def serve_sub(name):
    text = vtt_store.get(name)
    if text is None:
        return Response("not found", 404)
    return Response(text.encode("utf-8"), content_type="text/vtt; charset=utf-8",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


@app.route("/media/<path:rel>", methods=["GET", "HEAD"])
def serve_media(rel):
    path = resolve_media(rel)
    if not path or not os.path.isfile(path):
        return Response("not found", 404)
    resp = send_file(path, conditional=True)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.get("/api/browse")
def api_browse():
    if not MEDIA_DIR:
        return jsonify({"enabled": False})
    rel = request.args.get("path", "").strip("/")
    path = resolve_media(rel)
    if not path or not os.path.isdir(path):
        return err("That folder isn't available.", 404)
    dirs, files = [], []
    try:
        for entry in os.scandir(path):
            child = os.path.join(rel, entry.name) if rel else entry.name
            if not resolve_media(child):
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": child})
            elif entry.is_file() and entry.name.lower().endswith(VIDEO_EXTS):
                files.append({"name": entry.name, "size": entry.stat().st_size, "url": media_url(child)})
    except OSError as e:
        return err(f"Can't read that folder: {e.strerror}", 500)
    dirs.sort(key=lambda d: natural_key(d["name"]))
    files.sort(key=lambda d: natural_key(d["name"]))
    return jsonify({"enabled": True, "root": os.path.basename(MEDIA_DIR), "path": rel, "dirs": dirs, "files": files})


@app.get("/api/devices")
def api_devices():
    if request.args.get("refresh") or not casts:
        return jsonify(discover())
    return jsonify(list_devices())


@app.post("/api/probe")
def api_probe():
    url = (request.json or {}).get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return err("Paste an http(s) link to the video.")
    result = {"embedded": [], "sidecars": [], "warnings": []}
    local = local_media_path(url)
    try:
        info = subs.probe(local or url)
        for st in info.get("streams", []):
            ct, codec = st.get("codec_type"), st.get("codec_name", "?")
            if ct == "subtitle":
                tags = st.get("tags", {})
                result["embedded"].append({
                    "index": st["index"], "codec": codec,
                    "lang": tags.get("language", ""), "title": tags.get("title", ""),
                    "text": codec in subs.TEXT_SUB_CODECS})
            elif ct == "video" and codec not in SUPPORTED_V and codec not in ("mjpeg", "png"):
                result["warnings"].append(f"Video codec '{codec}' may not play on Chromecast.")
            elif ct == "audio" and codec not in SUPPORTED_A:
                result["warnings"].append(f"Audio codec '{codec}' may not play on Chromecast (no sound).")
        result["duration"] = float(info.get("format", {}).get("duration") or 0)
    except Exception as e:
        result["warnings"].append(f"Could not inspect the video: {str(e)[-200:]}")
    try:
        result["sidecars"] = local_sidecars(local) if local else subs.find_sidecars(url)
    except Exception:
        pass
    return jsonify(result)


@app.post("/api/play")
def api_play():
    """Form fields: url, device, sub_kind (none|embedded|sidecar|upload), sub_ref, codec, encoding,
    lang, file, resume (1 = swap subtitles on the video already playing, keeping the position)."""
    f = request.form
    url = f.get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return err("The video link must start with http:// or https://.")
    kind, ref, enc = f.get("sub_kind", "none"), f.get("sub_ref", ""), f.get("encoding", "auto")
    if kind == "sidecar" and not ref.startswith(("http://", "https://")):
        return err("The subtitle link must start with http:// or https://.")
    cues = []
    try:
        if kind == "embedded":
            text = subs.extract_embedded(local_media_path(url) or url, int(ref), f.get("codec", "subrip"), enc)
            cues = subs.parse_cues(text)
        elif kind == "sidecar":
            text = subs.load_external(subs.fetch(ref), ref, enc)
            cues = subs.parse_cues(text)
        elif kind == "upload" and "file" in request.files:
            up = request.files["file"]
            cues = subs.parse_cues(subs.load_external(up.read(), up.filename or "x.srt", enc))
    except Exception as e:
        return err(f"Subtitle problem: {e}")
    if kind != "none" and not cues:
        return err("Couldn't read any subtitle lines from that file (try another encoding).")
    lang = f.get("lang") or "fa"
    if lang == "fa" or (lang != "ar" and subs.is_persian(" ".join(t for _, _, t in cues))):
        cues = [(s, e, subs.fix_persian(t)) for s, e, t in cues]
    try:
        with lock:
            if f.get("resume") == "1":
                if session.get("url") != url:
                    return err("That isn't the video that's playing now.")
                session.update(cues=cues, offset=0.0, lang=lang)
                cast = session["cast"]
                load_media(cast, current_time=cast.media_controller.status.adjusted_current_time)
                return jsonify({"ok": True, "subtitle_lines": len(cues)})
        cast = get_cast(f.get("device"))
        with lock:
            session.clear()
            session.update(cast=cast, url=url, ctype=content_type_for(url), cues=cues, offset=0.0,
                           lang=lang,
                           title=urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]))
            load_media(cast)
    except Exception as e:
        return err(f"Cast failed: {e}", 500)
    return jsonify({"ok": True, "subtitle_lines": len(cues)})


@app.post("/api/control")
def api_control():
    d = request.json or {}
    action = d.get("action")
    cast = session.get("cast")
    if action == "disconnect":
        # Also works after an app restart, when there's no session but the TV is still casting.
        try:
            (cast or get_cast(d.get("device"))).quit_app()
        except Exception as e:
            return err(e, 500)
        with lock:
            session.clear()
        return jsonify({"ok": True})
    if not cast:
        return err("Nothing is playing.")
    mc = cast.media_controller
    try:
        if action == "toggle":
            mc.pause() if mc.status.player_state == "PLAYING" else mc.play()
        elif action == "seek_rel":
            mc.seek(max(0, (mc.status.adjusted_current_time or 0) + float(d.get("value", 0))))
        elif action == "seek_abs":
            mc.seek(max(0, float(d.get("value", 0))))
        elif action == "stop":
            mc.stop()
        elif action == "volume":
            cast.set_volume(min(1.0, max(0.0, (cast.status.volume_level or 0) + float(d.get("value", 0)))))
        elif action == "sub_toggle":
            if mc.status.current_subtitle_tracks:
                mc.disable_subtitle()
            elif session.get("cues"):
                mc.enable_subtitle(1)
        elif action == "sub_delay":
            if not session.get("cues"):
                return err("No subtitles loaded.")
            with lock:
                session["offset"] = round(session.get("offset", 0.0) + float(d.get("value", 0)), 2)
                load_media(cast, current_time=mc.status.adjusted_current_time)  # reload with shifted subs
        else:
            return err("Unknown action.")
    except Exception as e:
        return err(e, 500)
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    cast = session.get("cast")
    if not cast:
        return jsonify({"state": "IDLE"})
    st = cast.media_controller.status
    return jsonify({
        "state": st.player_state, "title": session.get("title"), "url": session.get("url"),
        "time": st.adjusted_current_time or 0, "duration": st.duration or 0,
        "volume": cast.status.volume_level if cast.status else None,
        "subs": bool(session.get("cues")), "subs_on": bool(st.current_subtitle_tracks),
        "offset": session.get("offset", 0.0), "device": cast.name,
    })


if __name__ == "__main__":
    threading.Thread(target=discover, daemon=True).start()
    try:
        me = lan_ip_towards("8.8.8.8")
    except OSError:
        me = "this-machine-ip"
    print(f"\n  Open on your phone:  http://{me}:{PORT}")
    if MEDIA_DIR and not os.path.isdir(MEDIA_DIR):
        print(f"  WARNING: media_dir not found: {MEDIA_DIR}")
    elif MEDIA_DIR:
        print(f"  Media folder:        {MEDIA_DIR}")
    print()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
