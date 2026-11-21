import difflib
import json
import re
import os
import statistics
import tempfile
import threading
import unicodedata
import urllib.parse
import urllib.request

import yt_dlp
from flask import Flask, jsonify, redirect, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

MAX_SECONDS = 10 * 60   # bỏ video dài hơn 10 phút (mix, playlist, live). Đặt None để giữ lại tất cả
DURATION_TOLERANCE = 6   # giây, độ lệch tối đa khi ghép lời với bài (trước đây 12 -> hay ghép nhầm bản thu khác)
META = {}                # id -> {title, artist, duration}, lưu lại từ lúc tìm kiếm
LYRICS = {}              # id -> kết quả lời, tránh hỏi lại
USE_WHISPER = True       # tự nghe nhạc để căn lời khi video không có phụ đề (cần: pip install faster-whisper + ffmpeg)
WHISPER_MODEL = "small"  # base nhanh hơn nhưng nhận tiếng Việt kém hơn; small cân bằng
WHISPER_SECONDS = 100    # chỉ nghe 100 giây đầu của bài
_whisper = {"model": None}
_whisper_lock = threading.Lock()
OFFSETS = {}             # id -> độ lệch tự tính (giây) hoặc None


def run_search(q, n=25):
    opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
    out = []
    for e in info["entries"]:
        if not e.get("id"):
            continue
        dur = e.get("duration")
        if MAX_SECONDS and dur and dur > MAX_SECONDS:
            continue
        artist = e.get("channel") or e.get("uploader")
        META[e["id"]] = {"title": e.get("title") or "", "artist": artist or "", "duration": dur}
        out.append({
            "id": e["id"],
            "title": e.get("title"),
            "artist": artist,
            "duration": dur,
            "thumb": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
        })
    return out


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        out = run_search(q)
        if len(out) < 6:
            # kết quả toàn mix/playlist dài bị lọc hết -> tìm lại thiên về bài đơn lẻ
            seen = {x["id"] for x in out}
            out += [x for x in run_search(q + " official mv") if x["id"] not in seen]
    except Exception as e:
        print("[search] lỗi:", repr(e))
        return jsonify([])
    return jsonify(out[:12])


@app.route("/stream/<vid>")
def stream(vid):
    opts = {"quiet": True, "skip_download": True,
            "format": "bestaudio[ext=m4a]/bestaudio"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    return redirect(info["url"])


# ---------- Lời bài hát (LRCLIB) ----------

def clean_title(s):
    s = re.sub(r"[\(\[\{【][^\)\]\}】]*[\)\]\}】]", " ", s)
    s = re.sub(r"\b(official|lyrics?|lyric video|music video|mv|m/v|audio|hd|4k)\b", " ", s, flags=re.I)
    s = re.sub(r"[\"“”#]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def lrclib(path, **params):
    url = "https://lrclib.net/api/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "nhac-local-player/1.0"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.load(r)


def get_meta(vid):
    if vid in META and META[vid].get("title"):
        return META[vid]
    opts = {"quiet": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        i = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    META[vid] = {
        "title": i.get("track") or i.get("title") or "",
        "artist": i.get("artist") or i.get("channel") or i.get("uploader") or "",
        "duration": i.get("duration"),
    }
    return META[vid]


def pack(x, dur):
    return {
        "syncedLyrics": x.get("syncedLyrics"),
        "plainLyrics": x.get("plainLyrics"),
        "lrcDuration": x.get("duration"),
        "videoDuration": dur,
    }


def strip_feat(s):
    """bỏ phần 'ft. ...', 'feat. ...' và ký tự thừa ở hai đầu"""
    s = re.sub(r"\s+(ft\.?|feat\.?|featuring)\s+.*$", "", s, flags=re.I)
    return s.strip(" .-–—|/:")


def clean_artist(a):
    a = re.sub(r"\s*-\s*Topic$", "", a or "", flags=re.I)
    a = re.sub(r"\s*(official|vevo|channel|music|tv)\s*$", "", a, flags=re.I)
    return a.strip()


def compact(s):
    return norm(s).replace(" ", "")


def find_lyrics(meta):
    dur = meta.get("duration")
    title = clean_title(meta["title"])
    artist = clean_artist(meta.get("artist"))
    # tách "Bài - Ca sĩ", "Ca sĩ | Bài", "Bài / Ca sĩ"...
    parts = [p for p in (strip_feat(x) for x in re.split(r"\s[-–—|/]\s", title)) if p]
    whole = strip_feat(title)
    raw_c = compact(meta["title"])  # tiêu đề gốc, bỏ dấu/khoảng trắng, để đối chiếu tên bài

    def verified(x):
        """kết quả LRCLIB phải có tên bài nằm trong tiêu đề video, không thì bỏ (tránh lấy nhầm bài khác)"""
        nm = compact(re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", x.get("trackName") or ""))
        return len(nm) >= 2 and nm in raw_c
    first = parts[0] if parts else whole

    # các cách đoán, thử lần lượt: (nghệ sĩ, tên bài)
    cands = []
    if len(parts) >= 2:
        cands += [(parts[1], parts[0]), (parts[0], parts[1])]
    cands += [(artist, first), (artist, whole), ("", first), ("", whole)]
    if len(parts) >= 2:
        cands.append(("", parts[1]))
    queries = [{"track_name": t, **({"artist_name": a} if a else {})} for a, t in cands if len(compact(t)) >= 3]
    # tìm tự do theo từ khóa (LRCLIB tự khớp mờ) khi đoán tên/ca sĩ không trúng
    if len(compact(first)) >= 4:
        queries += [{"q": f"{first} {artist}".strip()}, {"q": first}]

    seen, fallback = set(), None
    for params in queries:
        key = json.dumps(params, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)

        # /get khớp chính xác theo thời lượng (LRCLIB cho lệch ~2s) -> ít lệch nhất
        if dur and "track_name" in params and "artist_name" in params:
            try:
                x = lrclib("get", duration=int(dur), **params)
                if x.get("syncedLyrics") and verified(x):
                    return pack(x, dur)
            except Exception:
                pass

        try:
            res = [x for x in lrclib("search", **params) if verified(x)]
        except Exception:
            continue

        def diff(x):
            if not dur or not x.get("duration"):
                return 0
            return abs(x["duration"] - dur)

        # chọn bản synced có thời lượng GẦN NHẤT
        synced = sorted((x for x in res if x.get("syncedLyrics")), key=diff)
        if synced and diff(synced[0]) <= DURATION_TOLERANCE:
            return pack(synced[0], dur)
        if fallback is None:
            plain = [x for x in res if x.get("plainLyrics") and diff(x) <= 15]
            if synced and diff(synced[0]) <= 15:
                fallback = synced[0]
            elif plain:
                fallback = plain[0]
    return pack(fallback, dur) if fallback else {}


@app.route("/lyrics/<vid>")
def lyrics(vid):
    if vid not in LYRICS:
        try:
            LYRICS[vid] = find_lyrics(get_meta(vid))
        except Exception:
            return jsonify({})
    return jsonify(LYRICS[vid])


# ---------- Tự canh độ lệch bằng phụ đề của video ----------

def norm(s):
    s = unicodedata.normalize("NFD", s.lower().replace("đ", "d"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def parse_lrc(txt):
    out = []
    for l in txt.split("\n"):
        text = re.sub(r"\[[^\]]*\]", "", l).strip()
        for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]", l):
            if text:
                out.append((int(m[1]) * 60 + float(m[2]), text))
    return sorted(out)


def caption_cues(vid):
    """[(giây, chữ)] từ phụ đề video (ưu tiên phụ đề thật, sau đó phụ đề tự động tiếng Việt)"""
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
        i = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    for pool in (i.get("subtitles") or {}, i.get("automatic_captions") or {}):
        for lang in ("vi", "vi-orig", "vi-VN"):
            fm = [f for f in pool.get(lang, []) if f.get("ext") == "json3"]
            if not fm:
                continue
            req = urllib.request.Request(fm[0]["url"], headers={"User-Agent": "nhac-local-player/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.load(r)
            cues = []
            for e in data.get("events", []):
                txt = "".join(sg.get("utf8", "") for sg in e.get("segs", [])).replace("\n", " ").strip()
                if txt:
                    cues.append((e.get("tStartMs", 0) / 1000, txt))
            if cues:
                return cues
    return []


def estimate_offset(lrc, cues):
    """lệch = (giờ trong video) - (giờ trong LRC), lấy trung vị trên các dòng khớp chữ"""
    cn = [(t, norm(x)) for t, x in cues]
    deltas = []
    for lt, text in lrc[:40]:
        a = norm(text)
        if len(a) < 8:
            continue
        best, bt = 0, None
        for ct, b in cn:
            if not b:
                continue
            sm = difflib.SequenceMatcher(None, a, b)
            score = sm.ratio()
            if len(a) >= 10:
                score = max(score, sm.find_longest_match(0, len(a), 0, len(b)).size / len(a))
            if score > best:
                best, bt = score, ct
        if best >= 0.7:
            deltas.append(bt - lt)
    if len(deltas) < 3:
        return None
    m = statistics.median(deltas)
    good = [d for d in deltas if abs(d - m) <= 3]
    if len(good) < max(3, len(deltas) // 2):
        return None
    return round(statistics.median(good) * 2) / 2


def whisper_words(vid):
    """nghe ~100 giây đầu bài, trả về [(giây, từ)]"""
    from faster_whisper import WhisperModel
    with tempfile.TemporaryDirectory() as tmp:
        opts = {"quiet": True, "format": "bestaudio", "outtmpl": os.path.join(tmp, "a.%(ext)s"),
                "download_ranges": yt_dlp.utils.download_range_func(None, [(0, WHISPER_SECONDS)]),
                "force_keyframes_at_cuts": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={vid}"])
        path = os.path.join(tmp, os.listdir(tmp)[0])
        with _whisper_lock:
            if _whisper["model"] is None:
                _whisper["model"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            segs, _ = _whisper["model"].transcribe(path, language="vi", word_timestamps=True,
                                                   condition_on_previous_text=False)
            return [(w.start, w.word) for sg in segs for w in (sg.words or [])]


def estimate_offset_words(lrc, words, k=4):
    """căn theo từ: tìm k từ đầu của mỗi dòng lời trong chữ Whisper nghe được"""
    nw = [(t, norm(w)) for t, w in words]
    nw = [(t, w) for t, w in nw if w]
    deltas = []
    for lt, text in lrc[:40]:
        lw = norm(text).split()
        if len(lw) < 3:
            continue
        kk = min(k, len(lw))
        target = " ".join(lw[:kk])
        best, bt = 0, None
        for i in range(len(nw) - kk + 1):
            r = difflib.SequenceMatcher(None, target, " ".join(w for _, w in nw[i:i + kk])).ratio()
            if r > best:
                best, bt = r, nw[i][0]
        if best >= 0.75:
            deltas.append(bt - lt)
    if len(deltas) < 3:
        return None
    m = statistics.median(deltas)
    good = [d for d in deltas if abs(d - m) <= 3]
    if len(good) < max(3, len(deltas) // 2):
        return None
    return round(statistics.median(good) * 2) / 2


@app.route("/offset/<vid>")
def offset(vid):
    if vid not in OFFSETS:
        off = None
        try:
            if vid not in LYRICS:
                LYRICS[vid] = find_lyrics(get_meta(vid))
            synced = LYRICS[vid].get("syncedLyrics")
            if synced:
                lrc = parse_lrc(synced)
                try:
                    off = estimate_offset(lrc, caption_cues(vid))
                except Exception as e:
                    print("[offset] phụ đề:", repr(e))
                if off is None and USE_WHISPER:
                    try:
                        off = estimate_offset_words(lrc, whisper_words(vid))
                    except ImportError:
                        print("[offset] chưa cài faster-whisper: pip install faster-whisper")
                    except Exception as e:
                        print("[offset] whisper:", repr(e))
        except Exception as e:
            print("[offset] lỗi:", repr(e))
        OFFSETS[vid] = off
    return jsonify({"offset": OFFSETS[vid]})



if __name__ == "__main__":
    app.run(port=5000, threaded=True)