import difflib
import time
import json
import re
import os
import shutil
import statistics
import tempfile
import threading
import unicodedata
import urllib.parse
import urllib.request

import yt_dlp
from flask import Flask, jsonify, redirect, request
from flask_cors import CORS

# ---------- Cookies (Render Secret File) ----------
COOKIE_SRC = "/etc/secrets/cookies.txt"
COOKIE_FILE = "/tmp/cookies.txt"
if os.path.exists(COOKIE_SRC):
    shutil.copy(COOKIE_SRC, COOKIE_FILE)


def ydl_opts(**extra):
    o = {"quiet": True, "skip_download": True}
    if os.path.exists(COOKIE_FILE):
        o["cookiefile"] = COOKIE_FILE
    o.update(extra)
    return o


app = Flask(__name__)
CORS(app)

MAX_SECONDS = 10 * 60   # bỏ video dài hơn 10 phút (mix, playlist, live). Đặt None để giữ lại tất cả
DURATION_TOLERANCE = 6   # giây, độ lệch tối đa khi ghép lời với bài
META = {}                # id -> {title, artist, duration}, lưu lại từ lúc tìm kiếm
LYRICS = {}              # id -> kết quả lời, tránh hỏi lại
USE_WHISPER = False      # Render free không đủ RAM chạy Whisper
WHISPER_MODEL = "small"
WHISPER_SECONDS = 100
_whisper = {"model": None}
_whisper_lock = threading.Lock()
OFFSETS = {}             # id -> độ lệch tự tính (giây) hoặc None


SEARCH_CACHE = {}        # q -> (thời điểm, kết quả)
SEARCH_TTL = 6 * 3600    # giữ kết quả tìm kiếm 6 giờ
_search_lock = threading.Lock()
HOME_QUERIES = ["nhạc trẻ việt hay nhất mv", "lofi chill song", "nhạc đêm khuya buồn mv",
                "ballad việt mv", "piano nhạc không lời"]


def run_search(q, n=15):
    opts = ydl_opts(extract_flat=True)
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


def cached_search(q, limit=12):
    now = time.time()
    key = (q, limit)
    hit = SEARCH_CACHE.get(key)
    if hit and now - hit[0] < SEARCH_TTL:
        return hit[1]
    fetch_n = 15 if limit <= 12 else min(limit + 15, 60)
    out = run_search(q, fetch_n)
    if len(out) < min(limit, 6):
        seen = {x["id"] for x in out}
        out += [x for x in run_search(q + " official mv", fetch_n) if x["id"] not in seen]
    out = out[:limit]
    if out:
        SEARCH_CACHE[key] = (now, out)
    return out


def warm_home():
    """tải sẵn các mục trang chủ, lần lượt từng cái để không nghẽn CPU"""
    while True:
        for q in HOME_QUERIES:
            try:
                SEARCH_CACHE.pop((q, 12), None)
                cached_search(q)
            except Exception as e:
                print("[warm] lỗi:", repr(e))
            time.sleep(2)
        time.sleep(SEARCH_TTL - 600)


threading.Thread(target=warm_home, daemon=True).start()


@app.route("/")
def home():
    return jsonify({"status": "ok"})


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        n = max(1, min(int(request.args.get("n", 12)), 50))
        out = cached_search(q, n)
    except Exception as e:
        print("[search] lỗi:", repr(e))
        return jsonify([])
    return jsonify(out)


@app.route("/stream/<vid>")
def stream(vid):
    opts = ydl_opts(format="bestaudio[ext=m4a]/bestaudio")
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
    opts = ydl_opts()
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
    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
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
        opts = ydl_opts(skip_download=False, format="bestaudio",
                        outtmpl=os.path.join(tmp, "a.%(ext)s"),
                        download_ranges=yt_dlp.utils.download_range_func(None, [(0, WHISPER_SECONDS)]),
                        force_keyframes_at_cuts=True)
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


# ---------- Tài khoản & yêu thích (Postgres, ví dụ Neon) ----------
from contextlib import contextmanager
from itsdangerous import URLSafeTimedSerializer
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
_db_ready = {"ok": False}
_hits = {}


def _tokens():
    return URLSafeTimedSerializer(SECRET_KEY, salt="nhac-auth")


def db():
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    if not _db_ready["ok"]:
        with conn:
            with conn.cursor() as c:
                c.execute("CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, "
                          "username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL)")
                c.execute("CREATE TABLE IF NOT EXISTS user_favs (user_id INTEGER PRIMARY KEY "
                          "REFERENCES users(id) ON DELETE CASCADE, data TEXT NOT NULL DEFAULT '[]')")
        _db_ready["ok"] = True
    return conn


@contextmanager
def dbc():
    conn = db()
    try:
        with conn:
            with conn.cursor() as c:
                yield c
    finally:
        conn.close()


def not_ready():
    if not DATABASE_URL or not SECRET_KEY:
        return jsonify({"error": "Server chưa cấu hình cơ sở dữ liệu (DATABASE_URL, SECRET_KEY)."}), 503
    return None


def limited():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    now = time.time()
    h = [t for t in _hits.get(ip, []) if now - t < 600] + [now]
    _hits[ip] = h
    return len(h) > 20


def auth_user():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    try:
        return _tokens().loads(h[7:], max_age=90 * 86400)
    except Exception:
        return None


def read_creds():
    d = request.get_json(silent=True) or {}
    return re.sub(r"\s+", "", str(d.get("username") or "")).lower(), str(d.get("password") or "")


@app.route("/register", methods=["POST"])
def register():
    err = not_ready()
    if err:
        return err
    if limited():
        return jsonify({"error": "Thử quá nhiều lần, vui lòng đợi vài phút."}), 429
    u, p = read_creds()
    if not re.fullmatch(r"[a-z0-9_.]{3,30}", u):
        return jsonify({"error": "Tên đăng nhập 3-30 ký tự: chữ không dấu, số, dấu _ hoặc dấu chấm."}), 400
    if not 6 <= len(p) <= 100:
        return jsonify({"error": "Mật khẩu cần từ 6 ký tự trở lên."}), 400
    try:
        with dbc() as c:
            c.execute("INSERT INTO users (username, pw_hash) VALUES (%s, %s) RETURNING id",
                      (u, generate_password_hash(p)))
            uid = c.fetchone()[0]
    except Exception as e:
        if getattr(e, "pgcode", None) == "23505":
            return jsonify({"error": "Tên đăng nhập đã có người dùng."}), 409
        print("[register] lỗi:", repr(e))
        return jsonify({"error": "Lỗi server, thử lại sau."}), 500
    return jsonify({"token": _tokens().dumps({"id": uid, "u": u}), "username": u})


@app.route("/login", methods=["POST"])
def login():
    err = not_ready()
    if err:
        return err
    if limited():
        return jsonify({"error": "Thử quá nhiều lần, vui lòng đợi vài phút."}), 429
    u, p = read_creds()
    try:
        with dbc() as c:
            c.execute("SELECT id, pw_hash FROM users WHERE username = %s", (u,))
            row = c.fetchone()
    except Exception as e:
        print("[login] lỗi:", repr(e))
        return jsonify({"error": "Lỗi server, thử lại sau."}), 500
    if not row or not check_password_hash(row[1], p):
        return jsonify({"error": "Sai tên đăng nhập hoặc mật khẩu."}), 401
    return jsonify({"token": _tokens().dumps({"id": row[0], "u": u}), "username": u})


@app.route("/favs", methods=["GET", "PUT"])
def favs():
    err = not_ready()
    if err:
        return err
    me = auth_user()
    if not me:
        return jsonify({"error": "Chưa đăng nhập."}), 401
    try:
        if request.method == "GET":
            with dbc() as c:
                c.execute("SELECT data FROM user_favs WHERE user_id = %s", (me["id"],))
                row = c.fetchone()
            return jsonify({"favs": json.loads(row[0]) if row else []})
        items = (request.get_json(silent=True) or {}).get("favs")
        if not isinstance(items, list) or len(items) > 2000:
            return jsonify({"error": "Dữ liệu không hợp lệ."}), 400
        clean = []
        for f in items:
            if isinstance(f, dict) and re.fullmatch(r"[\w-]{6,20}", str(f.get("id", ""))):
                clean.append({"id": f["id"], "title": str(f.get("title") or "")[:300],
                              "artist": str(f.get("artist") or "")[:200],
                              "mood": f.get("mood") if isinstance(f.get("mood"), str) else None,
                              "duration": f.get("duration") if isinstance(f.get("duration"), (int, float)) else None})
        with dbc() as c:
            c.execute("INSERT INTO user_favs (user_id, data) VALUES (%s, %s) "
                      "ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data",
                      (me["id"], json.dumps(clean, ensure_ascii=False)))
        return jsonify({"ok": True})
    except Exception as e:
        print("[favs] lỗi:", repr(e))
        return jsonify({"error": "Lỗi server, thử lại sau."}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)