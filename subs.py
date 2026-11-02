"""Subtitle handling: fetch, decode (incl. legacy Persian encodings), parse, convert to WebVTT."""
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request

from charset_normalizer import from_bytes

SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub")
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
ARABIC_SCRIPT = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
PERSIAN_ONLY = re.compile(r"[پچژگ]")  # پ چ ژ گ
UA = {"User-Agent": "castremote/1.0"}


# ---------------------------------------------------------------- decoding

def decode_bytes(data: bytes, encoding: str = "auto") -> str:
    if encoding and encoding != "auto":
        return data.decode(encoding, errors="replace")
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    if data.count(b"\x00") > len(data) // 4:  # UTF-16 without BOM
        le = data[1::2].count(b"\x00") > data[0::2].count(b"\x00")
        return data.decode("utf-16-le" if le else "utf-16-be", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Legacy 8-bit. Persian/Arabic subtitles are almost always Windows-1256.
    text = data.decode("cp1256", errors="replace")
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if ARABIC_SCRIPT.match(c)) / len(letters) > 0.3:
        return text
    best = from_bytes(data).best()
    return str(best) if best else data.decode("latin-1")


def is_persian(text: str) -> bool:
    return bool(PERSIAN_ONLY.search(text))


def fix_persian(text: str) -> str:
    """Use Persian \u06CC/\u06A9 instead of the Arabic forms (Windows-1256 files only have those)."""
    return text.replace("\u064A", "\u06CC").replace("\u0643", "\u06A9")


# ---------------------------------------------------------------- parsing

TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
CUE_RE = re.compile(TS + r"\s*-->\s*" + TS + r"[^\n]*\n(.*?)(?=\n\s*\n|\Z)", re.S)


def _secs(h, m, s, ms):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_cues(text: str):
    """Parse SRT or VTT text into [(start, end, text)]."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for m in CUE_RE.finditer(text):
        g = m.groups()
        body = m.group(9).strip("\n")
        # drop a trailing SRT index line that slipped in (shouldn't, but be safe)
        cues.append((_secs(*g[0:4]), _secs(*g[4:8]), body))
    return cues


BIDI_CONTROLS = re.compile("[\u200E\u200F\u202A-\u202E\u2066-\u2069]")
PUNCT = ".!?\u061F\u060C\u061B:\u2026,"  # includes the Persian ? , ; and the ellipsis
LEADING_PUNCT = re.compile(r"^((?:<[ibu]>)*)([" + PUNCT + r"]+)\s*(.*?)((?:</[ibu]>)*)$")


def _fix_rtl_punctuation(line: str) -> str:
    """Many Persian subtitles type end punctuation first ("!thanks") so it looks right in left-to-right
    players. We show the line right-to-left, so move it back to the end unless the line already has an ending."""
    line = BIDI_CONTROLS.sub("", line).strip()
    m = LEADING_PUNCT.match(line)
    if m and m.group(3) and m.group(3)[-1] not in PUNCT:
        return m.group(1) + m.group(3) + m.group(2) + m.group(4)
    return line


def _clean_line(line: str) -> str:
    line = re.sub(r"\{[^}]*\}", "", line)                       # ASS override tags {\an8}
    line = re.sub(r"<(?!/?(?:i|b|u)>)[^>]*>", "", line)          # keep only <i> <b> <u>
    if ARABIC_SCRIPT.search(line):
        line = _fix_rtl_punctuation(line)
    line = line.replace("&", "&amp;")
    line = re.sub(r"<(?!/?(?:i|b|u)>)", "&lt;", line)
    line = line.replace("-->", "--&gt;")
    if ARABIC_SCRIPT.search(line):
        line = "‏" + line + "‏"                          # RLM: keep RTL punctuation in place
    return line


def _fmt(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def to_vtt(cues, offset: float = 0.0) -> str:
    out = ["WEBVTT", ""]
    for start, end, body in cues:
        if end + offset <= 0:
            continue
        lines = [_clean_line(l) for l in body.split("\n") if l.strip()]
        if not lines:
            continue
        out.append(f"{_fmt(start + offset)} --> {_fmt(end + offset)}")
        out.extend(lines)
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- ffmpeg helpers

def run(cmd, timeout=600, input_bytes=None):
    p = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-800:])
    return p.stdout


def ass_to_srt_text(text: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".ass", encoding="utf-8", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        return run(["ffmpeg", "-v", "error", "-i", path, "-f", "srt", "-"]).decode("utf-8", "replace")
    finally:
        os.unlink(path)


def probe(url: str):
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", url], timeout=60)
    return json.loads(out)


def extract_embedded(url: str, stream_index: int, codec: str, encoding="auto") -> str:
    """Extract an embedded text subtitle stream; returns UTF-8 SRT/VTT-ish text."""
    if codec in ("subrip", "srt"):
        raw = run(["ffmpeg", "-v", "error", "-i", url, "-map", f"0:{stream_index}", "-c:s", "copy", "-f", "srt", "-"])
        return decode_bytes(raw, encoding)
    if codec in ("ass", "ssa"):
        raw = run(["ffmpeg", "-v", "error", "-i", url, "-map", f"0:{stream_index}", "-c:s", "copy", "-f", "ass", "-"])
        return ass_to_srt_text(decode_bytes(raw, encoding))
    raw = run(["ffmpeg", "-v", "error", "-i", url, "-map", f"0:{stream_index}", "-f", "srt", "-"])
    return decode_bytes(raw, encoding)


def load_external(data: bytes, name: str, encoding="auto") -> str:
    text = decode_bytes(data, encoding)
    if name.lower().endswith((".ass", ".ssa")):
        text = ass_to_srt_text(text)
    return text


# ---------------------------------------------------------------- finding sidecar files

def fetch(url: str, limit=20 * 1024 * 1024, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def _exists(url: str) -> bool:
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={**UA, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status < 400
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return False
        except Exception:
            return False
    return False


def find_sidecars(video_url: str):
    """Look for subtitle files next to the video: parse the directory listing, else guess names."""
    parts = urllib.parse.urlsplit(video_url)
    dir_path, fname = parts.path.rsplit("/", 1)
    base = urllib.parse.unquote(fname).rsplit(".", 1)[0]
    dir_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, dir_path + "/", "", ""))
    found = []
    try:
        html = fetch(dir_url, limit=2 * 1024 * 1024, timeout=8).decode("utf-8", "replace")
        for href in re.findall(r'href="([^"]+)"', html, re.I):
            name = urllib.parse.unquote(href.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
            if name.lower().endswith(SUB_EXTS):
                found.append({"name": name, "url": urllib.parse.urljoin(dir_url, href),
                              "match": name.lower().startswith(base.lower())})
    except Exception:
        pass
    if not found:
        for suffix in ("", ".fa", ".fas", ".per", ".persian", ".farsi", ".en", ".eng"):
            for ext in (".srt", ".vtt", ".ass"):
                name = base + suffix + ext
                url = urllib.parse.urljoin(dir_url, urllib.parse.quote(name))
                if _exists(url):
                    found.append({"name": name, "url": url, "match": True})
    found.sort(key=lambda f: (not f["match"], f["name"].lower()))
    return found
