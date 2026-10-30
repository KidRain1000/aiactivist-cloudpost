#!/usr/bin/env python3
"""AI Activist cloud poster. Same rules as ../ai-activist-autopost/poster.py, cloud adapters.

Runs every 10 min in GitHub Actions (.github/workflows/post.yml). One pass: post what is due,
write results to queue.json + post-log.csv, the workflow commits them back.

Rules (copied from poster.py, keep in sync):
  - Daily caps per CT day (DAILY_CAP), never two posts on one platform within MIN_GAP (70 min).
  - Due = slot_at passed. More than 6h late = failed ("stale"), never posted late.
  - Never double-post: the item is CLAIMED (claimed_at saved, and in GHA committed + pushed)
    before the one irreversible call. A claimed item found later is resolved, never re-sent
    blindly: Bluesky checks its pre-chosen record key, Instagram checks the container's
    status_code (PUBLISHED or not), X is marked failed for a human check.
  - No replies. Failures email chris.pearson@savoytx.com via Resend. One heartbeat a day.

No sleeping in Actions (house rule: feedback_no_long_sleep_in_gha). Every slow step is
two-phase and stateless: run N starts the work and stores an id on the item, run N+1 checks it.
  - Instagram: create REELS container (ig_container_id) -> later run sees FINISHED -> publish.
  - X video: INIT/APPEND/FINALIZE (x_media_id) -> later run sees STATUS succeeded -> post.
  - Bluesky video: video.bsky.app job (bsky_video_job) -> later run gets the blob -> post.

Usage:
  cloudpost.py                  live run
  cloudpost.py --dry-run        plan only: no network, no writes, no email
  cloudpost.py --dry-run --now 2026-10-30T08:30   (pretend time, CT)
Stdlib only.
"""
import argparse, base64, csv, datetime as dt, hashlib, hmac, html.parser, json, os, random, re
import secrets, subprocess, sys, tempfile, time, traceback, urllib.error, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CT = ZoneInfo("America/Chicago")
QUEUE = os.path.join(HERE, "queue.json")
LOG = os.path.join(HERE, "post-log.csv")
MEDIA_DIR = os.environ.get("MEDIA_DIR") or os.path.join(HERE, "media")  # optional local copies

PLATFORMS = ("bluesky", "x", "instagram")
STATUSES = ("draft", "approved", "posted", "failed", "killed")
DAILY_CAP = {"bluesky": 1, "x": 8, "instagram": 7}  # same as poster.py
MIN_GAP = dt.timedelta(minutes=70)
STALE_AFTER = dt.timedelta(hours=6)
MAX_ATTEMPTS = 3
HEARTBEAT_HOUR = 19  # first run at or after 7 PM CT sends the daily heartbeat
LOW_RUNWAY_DAYS = 3

BSKY_HOST = "https://bsky.social"
BSKY_MAX_BLOB = 976_000
X_API = "https://api.x.com"
X_CHUNK = 4 * 1024 * 1024  # docs: keep APPEND segments <= 5 MB
X_MEDIA_MAX_AGE = dt.timedelta(hours=20)  # upload sessions expire after 86400s; re-upload before that
IG_CAPTION_MAX = 2200
ALERT_TO = "chris.pearson@savoytx.com"  # internal only
ALERT_FROM = "Rainmaker Alerts <noreply@getnmad.com>"
LOG_FIELDS = ["logged_at", "id", "platform", "slot_at", "status", "posted_url", "error"]
UA = "ai-activist-cloudpost/1"


def env(k, default=None):
    return os.environ.get(k) or default


class PrePostError(Exception):
    """Failed before anything was sent. Safe to retry on a later run."""


class AmbiguousPostError(Exception):
    """The post may or may not be live. Never retried automatically."""


class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


# ---------------------------------------------------------------- http (tests replace http())
def http(method, url, headers=None, data=None, timeout=60):
    """One HTTP call. Returns (status, body bytes). Network errors raise."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_json(method, url, headers=None, data=None, timeout=60):
    status, body = http(method, url, headers, data, timeout)
    if not 200 <= status < 300:
        raise ApiError(status, body.decode("utf-8", "replace")[:400])
    return json.loads(body or b"{}")


def multipart(fields):
    """fields: {name: str | (filename, bytes, mime)}. Returns (body, content-type)."""
    b = "----cloudpost" + secrets.token_hex(12)
    out = []
    for k, v in fields.items():
        if isinstance(v, tuple):
            fn, data, mime = v
            out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
                       f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
        else:
            out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


# ---------------------------------------------------------------- time + queue io (from poster.py)
def now_ct():
    return dt.datetime.now(CT)


def parse_ct(s):
    t = dt.datetime.fromisoformat(s)
    return t.replace(tzinfo=CT) if t.tzinfo is None else t.astimezone(CT)


def iso(t):
    return t.astimezone(CT).isoformat(timespec="seconds")


def load_queue(path):
    with open(path) as f:
        q = json.load(f)
    if not isinstance(q, dict) or not isinstance(q.get("items"), list):
        raise ValueError(f"{path}: expected {{'version':1,'items':[...]}}")
    return q


def save_queue(path, q):
    """Atomic: temp file in the same dir, fsync, rename over."""
    q["updated_at"] = iso(now_ct())
    fd, tmp = tempfile.mkstemp(prefix=".queue-", suffix=".json", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(q, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_log(path, item, when):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"logged_at": iso(when), "id": item.get("id"), "platform": item.get("platform"),
                    "slot_at": item.get("slot_at"), "status": item.get("status"),
                    "posted_url": item.get("posted_url") or "", "error": item.get("error") or ""})


def read_log(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def read_log_posts(path):
    out = []
    for r in read_log(path):
        if r.get("status") == "posted" and r.get("logged_at"):
            try:
                out.append((r["platform"], parse_ct(r["logged_at"])))
            except ValueError:
                pass
    return out


def git_checkpoint(msg):
    """In GHA: commit + push queue/log so a claim survives a killed runner. Off locally and in tests."""
    if env("CLOUDPOST_GIT_CHECKPOINT") != "1":
        return
    try:
        for cmd in (["git", "-C", HERE, "add", "queue.json", "post-log.csv"],
                    ["git", "-C", HERE, "commit", "-q", "-m", msg],
                    ["git", "-C", HERE, "pull", "-q", "--rebase"],
                    ["git", "-C", HERE, "push", "-q"]):
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as e:
        raise PrePostError(f"claim checkpoint failed ({' '.join(e.cmd[3:5])}): {e.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        raise PrePostError("claim checkpoint timed out")


# ---------------------------------------------------------------- text helpers (from poster.py)
URL_RE = re.compile(r"https?://[^\s<>\"]+|(?<![\w@/.])(?:[a-z0-9-]+\.)+(?:com|org|net|io|co|ai|app|us)(?:/[^\s<>\"]*)?", re.I)
TAG_RE = re.compile(r"(?<![\w#])#([A-Za-z][A-Za-z0-9_]{0,63})")


def find_urls(text):
    for m in URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;:!?)”’'\"")
        yield m.start(), m.start() + len(u), u


def bsky_facets(text):
    """Link + hashtag facets with UTF-8 byte offsets, as the Bluesky lexicon requires."""
    facets = []

    def bpos(i):
        return len(text[:i].encode("utf-8"))

    for s, e, u in find_urls(text):
        full = u if u.lower().startswith("http") else "https://" + u
        facets.append({"index": {"byteStart": bpos(s), "byteEnd": bpos(e)},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": full}]})
    for m in TAG_RE.finditer(text):
        facets.append({"index": {"byteStart": bpos(m.start()), "byteEnd": bpos(m.end())},
                       "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}]})
    return facets


def x_text(item):
    """Cloud rule: no link on X unless the item says x_link=true (a URL makes the post $0.20, not $0.015)."""
    t = item["text"].rstrip()
    link = item.get("link")
    if item.get("x_link") and link and link not in t:
        t = f"{t}\n\n{link}"
    return t


def x_weighted_len(text):
    """twitter-text v3 approximation: URLs weigh 23, Latin-ish chars 1, everything else 2."""
    n, last = 0, 0
    for s, e, _ in find_urls(text):
        n += _weigh(text[last:s]) + 23
        last = e
    return n + _weigh(text[last:])


def _weigh(s):
    w = 0
    for ch in s:
        c = ord(ch)
        light = c <= 0x10FF or 0x2000 <= c <= 0x200D or 0x2010 <= c <= 0x201F or 0x2032 <= c <= 0x2037
        w += 1 if light else 2
    return w


def html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- validation
def validate(item):
    """Return an error string, or None if the item is well formed for the cloud poster."""
    for k in ("id", "platform", "slot_at", "status"):
        if not item.get(k):
            return f"missing {k}"
    if item["platform"] not in PLATFORMS:
        return f"unknown platform {item['platform']!r}"
    if item["status"] not in STATUSES:
        return f"unknown status {item['status']!r}"
    try:
        parse_ct(item["slot_at"])
    except ValueError:
        return f"bad slot_at {item['slot_at']!r}"
    if item.get("reply_to"):
        return "replies are never automated (plan 08 / Socrates 11)"
    if not (item.get("text") or "").strip():
        return "empty text"
    if item.get("image_path"):
        return "images are not built in the cloud poster (video or text only)"
    if item.get("video_path"):
        return "video_path is a Mac path; cloud items use video + video_url (run tools/export_queue.py)"
    if item.get("video") and not item.get("video_url"):
        return "video without video_url"
    p = item["platform"]
    if p == "bluesky" and len(item["text"]) > 300:
        return f"Bluesky text {len(item['text'])} chars > 300"
    if p == "x":
        t = x_text(item)
        if not item.get("x_link") and any(True for _ in find_urls(t)):
            return "X text contains a URL ($0.20/post); remove it or set x_link=true"
        n = x_weighted_len(t)
        if n > 280:
            return f"X text weighs {n} > 280"
    if p == "instagram":
        if not item.get("video"):
            return "Instagram posts need a video (Reels only)"
        if len(item["text"]) > IG_CAPTION_MAX:
            return f"Instagram caption {len(item['text'])} chars > {IG_CAPTION_MAX}"
        if len(TAG_RE.findall(item["text"])) > 30:
            return "Instagram caption has more than 30 hashtags"
    return None


# ---------------------------------------------------------------- caps (from poster.py)
def posted_times(queue, log_path, platform, extra=()):
    ts = [parse_ct(i["posted_at"]) for i in queue["items"]
          if i.get("platform") == platform and i.get("status") == "posted" and i.get("posted_at")]
    ts += [t for p, t in read_log_posts(log_path) if p == platform]
    ts += list(extra)
    return sorted(set(ts))


def cap_block(platform, now, times):
    """Reason the platform can't post right now, or None."""
    today = [t for t in times if t.astimezone(CT).date() == now.date()]
    if len(today) >= DAILY_CAP[platform]:
        return f"daily cap {DAILY_CAP[platform]}/day reached ({len(today)} today)"
    recent = [t for t in times if dt.timedelta(0) <= now - t < MIN_GAP]
    if recent:
        return f"gap rule: last {platform} post at {iso(max(recent))}"
    return None


# ---------------------------------------------------------------- media
def load_media(item):
    """Video bytes: local media/ copy if present, else download video_url (the public media repo)."""
    local = os.path.join(MEDIA_DIR, os.path.basename(item["video"]))
    if os.path.isfile(local):
        with open(local, "rb") as f:
            return f.read()
    try:
        status, body = http("GET", item["video_url"], timeout=120)
    except Exception as e:
        raise PrePostError(f"video download failed ({item['video_url']}): {e}")
    if status != 200 or not body:
        raise PrePostError(f"video download HTTP {status} ({item['video_url']})")
    return body


# ---------------------------------------------------------------- Bluesky (AT Protocol, as poster.py)
_B32 = "234567abcdefghijklmnopqrstuvwxyz"


def new_tid():
    """atproto TID: 53-bit microsecond timestamp + 10-bit clock id, base32-sortable, 13 chars."""
    v = (int(time.time() * 1_000_000) << 10) | random.getrandbits(10)
    s = ""
    for _ in range(13):
        s = _B32[v & 31] + s
        v >>= 5
    return s


class Bluesky:
    def __init__(self):
        self.s = None
        self.handle = env("BSKY_HANDLE", "theaiactivist.com")

    def _xrpc(self, method, nsid, body=None, raw=None, ctype=None, params=None, auth=True):
        url = f"{BSKY_HOST}/xrpc/{nsid}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers, data = {}, None
        if raw is not None:
            data, headers["Content-Type"] = raw, ctype
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.session()['accessJwt']}"
        try:
            return http_json(method, url, headers, data, timeout=60)
        except ApiError as e:
            raise ApiError(e.status, f"{nsid}: {e.detail}")

    def session(self):
        if self.s is None:
            pw = env("BSKY_APP_PASSWORD")
            if not pw:
                raise PrePostError("BSKY_APP_PASSWORD not set")
            try:
                self.s = self._xrpc("POST", "com.atproto.server.createSession",
                                    {"identifier": self.handle, "password": pw}, auth=False)
            except Exception as e:
                raise PrePostError(f"Bluesky login failed: {e}")
        return self.s

    def did(self):
        return self.session()["did"]

    def posts_today(self, now):
        """Our own top-level posts, straight from the feed (catches hand posts)."""
        feed = self._xrpc("GET", "app.bsky.feed.getAuthorFeed",
                          params={"actor": self.did(), "limit": 30, "filter": "posts_no_replies"})
        out = []
        for f in feed.get("feed", []):
            post = f.get("post", {})
            if f.get("reason") or post.get("author", {}).get("did") != self.did():
                continue
            ca = post.get("record", {}).get("createdAt")
            if ca:
                out.append(dt.datetime.fromisoformat(ca.replace("Z", "+00:00")).astimezone(CT))
        return out

    def upload(self, data, mime):
        return self._xrpc("POST", "com.atproto.repo.uploadBlob", raw=data, ctype=mime)["blob"]

    def start_video(self, data, name):
        """video.bsky.app upload with a service-auth token. Returns the job id (no waiting)."""
        pds = next(sv["serviceEndpoint"] for sv in self.session()["didDoc"]["service"] if sv["id"].endswith("atproto_pds"))
        aud = "did:web:" + urllib.parse.urlparse(pds).hostname
        tok = self._xrpc("GET", "com.atproto.server.getServiceAuth",
                         params={"aud": aud, "lxm": "com.atproto.repo.uploadBlob",
                                 "exp": int(time.time()) + 1800})["token"]
        url = ("https://video.bsky.app/xrpc/app.bsky.video.uploadVideo?"
               + urllib.parse.urlencode({"did": self.did(), "name": name}))
        status, body = http("POST", url, {"Authorization": f"Bearer {tok}", "Content-Type": "video/mp4"},
                            data, timeout=180)
        job = json.loads(body or b"{}")  # 409 already_exists still carries the jobId
        jid = job.get("jobId") or job.get("jobStatus", {}).get("jobId")
        if not jid:
            raise PrePostError(f"Bluesky video upload gave no job (HTTP {status}): {str(job)[:200]}")
        return jid

    def job_status(self, jid):
        st = http_json("GET", "https://video.bsky.app/xrpc/app.bsky.video.getJobStatus?jobId="
                       + urllib.parse.quote(jid))["jobStatus"]
        return st.get("state"), st.get("blob"), st.get("error")

    def record_exists(self, rkey):
        try:
            self._xrpc("GET", "com.atproto.repo.getRecord",
                       params={"repo": self.did(), "collection": "app.bsky.feed.post", "rkey": rkey})
            return True
        except ApiError as e:
            if e.status in (400, 404):
                return False
            raise

    def url_for(self, rkey):
        return f"https://bsky.app/profile/{self.handle}/post/{rkey}"


class _OG(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.title, self._in = {}, "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k and a.get("content") and k not in self.meta:
                self.meta[k] = a["content"]
        elif tag == "title":
            self._in = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in = False

    def handle_data(self, d):
        if self._in:
            self.title += d


def bsky_link_card(bs, link):
    """External embed from og: tags. No sips on Linux, so an oversize thumb is just left off."""
    try:
        status, body = http("GET", link, timeout=20)
        if status != 200:
            raise ApiError(status, "")
    except Exception as e:
        raise PrePostError(f"link card fetch failed for {link}: {e}")
    p = _OG()
    p.feed(body[:5_000_000].decode("utf-8", "replace"))
    m = p.meta
    ext = {"uri": link, "title": (m.get("og:title") or m.get("twitter:title") or p.title or "").strip()[:300],
           "description": (m.get("og:description") or m.get("description") or "").strip()[:1000]}
    img = m.get("og:image") or m.get("twitter:image")
    if img:
        try:
            status, data = http("GET", urllib.parse.urljoin(link, img), timeout=20)
            mime = "image/png" if img.lower().endswith(".png") else "image/jpeg"
            if status == 200 and 0 < len(data) <= BSKY_MAX_BLOB:
                ext["thumb"] = bs.upload(data, mime)
        except Exception:
            pass  # a card without a thumb beats no post
    return {"$type": "app.bsky.embed.external", "external": ext}


def bsky_record(item, bs):
    text = item["text"]
    rec = {"$type": "app.bsky.feed.post", "text": text, "langs": ["en"],
           "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    facets = bsky_facets(text)
    if facets:
        rec["facets"] = facets
    link = item.get("link") or next((u if u.startswith("http") else "https://" + u
                                     for _, _, u in find_urls(text)), None)
    if item.get("video"):
        rec["embed"] = {"$type": "app.bsky.embed.video", "alt": item.get("alt", ""), "video": item["bsky_blob"],
                        "aspectRatio": {"width": 1080, "height": 1920}}
    elif link:
        rec["embed"] = bsky_link_card(bs, link)
    return rec


def bsky_prepare(item, bs):
    """True when the post can go now. Video: start the job (run N), pick up the blob (run N+1)."""
    if not item.get("video") or item.get("bsky_blob"):
        return True
    if not item.get("bsky_video_job"):
        item["bsky_video_job"] = bs.start_video(load_media(item), os.path.basename(item["video"]))
    state, blob, err = bs.job_status(item["bsky_video_job"])
    if blob:
        item["bsky_blob"] = blob
        return True
    if state == "JOB_STATE_FAILED":
        item.pop("bsky_video_job", None)
        raise PrePostError(f"Bluesky video processing failed: {err}")
    return False


def post_bluesky(item, bs, save_claim):
    rkey = item.get("bsky_rkey") or new_tid()
    item["bsky_rkey"] = rkey
    rec = bsky_record(item, bs)
    save_claim()
    try:
        bs._xrpc("POST", "com.atproto.repo.createRecord",
                 {"repo": bs.did(), "collection": "app.bsky.feed.post", "rkey": rkey, "record": rec})
    except Exception as e:
        try:  # created anyway (timeout after commit)? check before calling it a failure
            if bs.record_exists(rkey):
                return bs.url_for(rkey)
        except Exception:
            raise AmbiguousPostError(f"createRecord error and could not check rkey: {e}")
        if isinstance(e, ApiError) and 400 <= e.status < 500:
            if "blob" in e.detail.lower():  # blob gone: start the video over
                item.pop("bsky_blob", None)
                item.pop("bsky_video_job", None)
            raise PrePostError(f"Bluesky refused the post: {e}")
        raise AmbiguousPostError(f"createRecord error: {e}")
    return bs.url_for(rkey)


# ---------------------------------------------------------------- X (API v2, OAuth 1.0a user context)
# Docs checked 2026-10-29:
#   POST /2/tweets                      https://docs.x.com/x-api/posts/create-post
#   POST /2/media/upload/initialize     https://docs.x.com/x-api/media/initialize-media-upload
#   POST /2/media/upload/{id}/append    https://docs.x.com/x-api/media/append-media-upload
#   POST /2/media/upload/{id}/finalize  https://docs.x.com/x-api/media/quickstart/media-upload-chunked
#   GET  /2/media/upload?command=STATUS&media_id=  https://docs.x.com/x-api/media/get-media-upload-status
#   Pricing (pay-per-use): https://docs.x.com/x-api/getting-started/pricing
# OAuth 1.0a signing (RFC 5849): JSON and multipart bodies are not signed, query params are.
def _pct(s):
    return urllib.parse.quote(str(s), safe="~")


def oauth1_header(method, url, params, consumer_key, consumer_secret, token, token_secret,
                  nonce=None, timestamp=None):
    """Authorization header value. params = form-urlencoded body params (query params are read from url)."""
    oauth = {"oauth_consumer_key": consumer_key, "oauth_nonce": nonce or secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(timestamp or int(time.time())),
             "oauth_token": token, "oauth_version": "1.0"}
    u = urllib.parse.urlsplit(url)
    base_url = f"{u.scheme.lower()}://{u.netloc.lower()}{u.path}"
    allp = urllib.parse.parse_qsl(u.query, keep_blank_values=True) + list((params or {}).items()) + list(oauth.items())
    pstr = "&".join(f"{k}={v}" for k, v in sorted((_pct(k), _pct(v)) for k, v in allp))
    base = "&".join((method.upper(), _pct(base_url), _pct(pstr)))
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}".encode()
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


class XClient:
    def __init__(self):
        self.keys = [env(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")]
        if not all(self.keys):
            raise PrePostError("X API keys not set (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET)")
        self.handle = env("X_HANDLE", "aiactivisthq")

    def call(self, method, path, json_body=None, raw=None, ctype=None, timeout=60):
        url = X_API + path
        ck, cs, tk, ts = self.keys
        headers = {"Authorization": oauth1_header(method, url, None, ck, cs, tk, ts)}
        data = None
        if json_body is not None:
            data, headers["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif raw is not None:
            data, headers["Content-Type"] = raw, ctype
        return http_json(method, url, headers, data, timeout)

    def upload_video(self, data):
        """INIT, APPEND chunks, FINALIZE. Returns (media_id, processing_info or None). No waiting."""
        init = self.call("POST", "/2/media/upload/initialize",
                         {"media_type": "video/mp4", "total_bytes": len(data), "media_category": "tweet_video"})
        mid = init["data"]["id"]
        for i, off in enumerate(range(0, len(data), X_CHUNK)):
            body, ctype = multipart({"segment_index": str(i), "media": ("chunk.mp4", data[off:off + X_CHUNK],
                                                                         "application/octet-stream")})
            self.call("POST", f"/2/media/upload/{mid}/append", raw=body, ctype=ctype, timeout=120)
        fin = self.call("POST", f"/2/media/upload/{mid}/finalize")
        return mid, fin.get("data", {}).get("processing_info")

    def media_state(self, mid):
        r = self.call("GET", "/2/media/upload?" + urllib.parse.urlencode({"command": "STATUS", "media_id": mid}))
        pi = r.get("data", {}).get("processing_info")
        return (pi or {}).get("state", "succeeded"), (pi or {}).get("error")

    def create_post(self, text, media_id=None):
        body = {"text": text}
        if media_id:
            body["media"] = {"media_ids": [media_id]}
        r = self.call("POST", "/2/tweets", body)
        return f"https://x.com/{self.handle}/status/{r['data']['id']}"


def x_prepare(item, xc, now):
    if not item.get("video"):
        return True
    if item.get("x_media_id") and now - parse_ct(item["x_media_at"]) > X_MEDIA_MAX_AGE:
        for k in ("x_media_id", "x_media_at", "x_media_ready"):
            item.pop(k, None)  # upload session about to expire: start over
    if item.get("x_media_ready"):
        return True
    if not item.get("x_media_id"):
        mid, pi = xc.upload_video(load_media(item))
        item.update(x_media_id=mid, x_media_at=iso(now))
        state = (pi or {}).get("state", "succeeded")
    else:
        state, err = xc.media_state(item["x_media_id"])
        if state == "failed":
            for k in ("x_media_id", "x_media_at"):
                item.pop(k, None)
            raise PrePostError(f"X video processing failed: {err}")
    if state == "succeeded":
        item["x_media_ready"] = True
        return True
    return False


def post_x(item, xc, save_claim):
    text = x_text(item)
    save_claim()
    try:
        return xc.create_post(text, item.get("x_media_id"))
    except ApiError as e:
        if 400 <= e.status < 500:  # X answered no: nothing was created
            raise PrePostError(f"X refused the post: {e}")
        raise AmbiguousPostError(f"X POST /2/tweets {e}; check x.com/{xc.handle} before re-approving")
    except Exception as e:
        raise AmbiguousPostError(f"X POST /2/tweets {e!r}; check x.com/{xc.handle} before re-approving")


# ---------------------------------------------------------------- Instagram (Graph API, Reels)
# Docs checked 2026-10-29:
#   Content publishing flow: https://developers.facebook.com/docs/instagram-platform/content-publishing
#   POST /{ig-user-id}/media (REELS params, video specs, 400 containers/24h):
#     https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
#   GET /{container-id}?fields=status_code -> EXPIRED|ERROR|FINISHED|IN_PROGRESS|PUBLISHED
#   POST /{ig-user-id}/media_publish creation_id=  (100 API posts / 24h, GET /{ig-user-id}/content_publishing_limit)
# Host graph.facebook.com = Facebook Login (Page-linked business account), the setup in README.
class Instagram:
    def __init__(self):
        self.uid, self.tok = env("IG_USER_ID"), env("IG_ACCESS_TOKEN")
        if not (self.uid and self.tok):
            raise PrePostError("IG_USER_ID / IG_ACCESS_TOKEN not set")
        self.base = f"{env('IG_GRAPH_HOST', 'https://graph.facebook.com')}/{env('IG_API_VERSION', 'v26.0')}"
        self.handle = env("IG_HANDLE", "aiactivisthq")

    def _post(self, path, fields):
        fields = dict(fields, access_token=self.tok)
        return http_json("POST", f"{self.base}{path}", {"Content-Type": "application/x-www-form-urlencoded"},
                         urllib.parse.urlencode(fields).encode())

    def _get(self, path, fields):
        return http_json("GET", f"{self.base}{path}?" + urllib.parse.urlencode({"fields": fields, "access_token": self.tok}))

    def create_container(self, item):
        r = self._post(f"/{self.uid}/media", {"media_type": "REELS", "video_url": item["video_url"],
                                               "caption": item["text"], "share_to_feed": "true"})
        return r["id"]

    def container_status(self, cid):
        r = self._get(f"/{cid}", "status_code,status")
        return r.get("status_code"), r.get("status")

    def publish(self, cid):
        return self._post(f"/{self.uid}/media_publish", {"creation_id": cid})["id"]

    def permalink(self, media_id):
        try:
            return self._get(f"/{media_id}", "permalink").get("permalink")
        except Exception:
            return None

    def profile_url(self):
        return f"https://www.instagram.com/{self.handle}/reels/"


def ig_prepare(item, ig):
    """Run N creates the container. A later run publishes only once it reports FINISHED."""
    if not item.get("ig_container_id"):
        try:
            item["ig_container_id"] = ig.create_container(item)
        except ApiError as e:
            raise PrePostError(f"Instagram container create failed: {e}")
        return False  # never publish in the run that created it
    code, detail = ig.container_status(item["ig_container_id"])
    if code == "FINISHED":
        return True
    if code in ("ERROR", "EXPIRED"):
        item.pop("ig_container_id", None)
        raise PrePostError(f"Instagram container {code}: {detail}")
    if code == "PUBLISHED":
        return "published"
    return False  # IN_PROGRESS


def post_instagram(item, ig, save_claim):
    cid = item["ig_container_id"]
    save_claim()
    try:
        mid = ig.publish(cid)
    except Exception as e:
        try:  # the container tells us whether it went live
            code, _ = ig.container_status(cid)
        except Exception:
            raise AmbiguousPostError(f"media_publish error and status check failed: {e}")
        if code == "PUBLISHED":
            return ig.profile_url()
        if code == "FINISHED" and isinstance(e, ApiError) and 400 <= e.status < 500:
            raise PrePostError(f"Instagram refused media_publish: {e}")
        raise AmbiguousPostError(f"media_publish error (container {code}): {e}")
    return ig.permalink(mid) or ig.profile_url()


# ---------------------------------------------------------------- email (Resend)
def send_email(subject, body_html):
    """Returns True if Resend accepted it. User-Agent header is required (Resend 403s urllib's default)."""
    if env("AUTOPOST_NO_EMAIL"):
        return False
    key = env("RESEND_API_KEY")
    if not key:
        print("EMAIL NOT SENT: no RESEND_API_KEY", file=sys.stderr)
        return False
    body = {"from": ALERT_FROM, "to": [ALERT_TO], "subject": subject,
            "html": f"<div style='font-family:system-ui,sans-serif;font-size:14px'>{body_html}</div>"}
    try:
        status, resp = http("POST", "https://api.resend.com/emails",
                            {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                             "User-Agent": "ai-activist-autopost/1"}, json.dumps(body).encode(), timeout=20)
    except Exception as e:
        print(f"EMAIL SEND FAILED: {e}", file=sys.stderr)
        return False
    if not 200 <= status < 300:
        print(f"EMAIL SEND FAILED: HTTP {status} {resp[:200]!r}", file=sys.stderr)
        return False
    return True


def email_failures(failed):
    if not failed:
        return
    rows = "".join(f"<li><b>{i.get('platform')}</b> {i.get('id')} (slot {i.get('slot_at')}): "
                   f"<code>{html_escape(i.get('error') or '')}</code><br>"
                   f"<span style='color:#64748b'>{html_escape((i.get('text') or '')[:140])}</span></li>" for i in failed)
    send_email(f"AI Activist cloud poster: {len(failed)} post(s) failed",
               "<p>The AI Activist cloud poster (GitHub Actions) could not post these. Nothing was retried late.</p>"
               f"<ul>{rows}</ul>")


# ---------------------------------------------------------------- heartbeat
def runway(q, now):
    """Per platform: (approved items still ahead, last slot date, days of items left)."""
    out = {}
    for p in PLATFORMS:
        ahead = [parse_ct(i["slot_at"]) for i in q["items"]
                 if i.get("platform") == p and i.get("status") == "approved" and i.get("slot_at")
                 and parse_ct(i["slot_at"]) > now]
        last = max(ahead).date() if ahead else None
        out[p] = (len(ahead), last, (last - now.date()).days if last else 0)
    return out


def heartbeat_body(q, log_path, now):
    since = now - dt.timedelta(hours=24)
    rows = []
    for r in read_log(log_path):
        try:
            if parse_ct(r["logged_at"]) >= since:
                rows.append(r)
        except (KeyError, ValueError):
            pass
    posted = {p: [r for r in rows if r["platform"] == p and r["status"] == "posted"] for p in PLATFORMS}
    failed = [r for r in rows if r["status"] == "failed"]
    rw = runway(q, now)
    # only platforms with keys count: X and IG are parked until their keys exist (10/30 false alarm)
    keys = {"bluesky": "BSKY_APP_PASSWORD", "x": "X_ACCESS_TOKEN", "instagram": "IG_ACCESS_TOKEN"}
    low = [p for p in PLATFORMS if os.environ.get(keys.get(p, "")) and rw[p][2] < LOW_RUNWAY_DAYS]
    subject = ("QUEUE RUNNING LOW: " if low else "") + f"AI Activist daily heartbeat {now.date().isoformat()}"
    h = ["<p><b>Posted in the last 24h</b></p><ul>"]
    for p in PLATFORMS:
        links = "".join(f"<br><a href='{html_escape(r['posted_url'])}'>{html_escape(r['id'])}</a>" for r in posted[p])
        h.append(f"<li>{p}: {len(posted[p])}{links}</li>")
    h.append("</ul><p><b>Failures in the last 24h</b></p>")
    h.append("<ul>" + "".join(f"<li>{html_escape(r['platform'])} {html_escape(r['id'])}: "
                              f"<code>{html_escape(r.get('error') or '')}</code></li>" for r in failed) + "</ul>"
             if failed else "<p>None.</p>")
    h.append("<p><b>Approved items left</b></p><ul>")
    for p in PLATFORMS:
        n, last, days = rw[p]
        flag = " <b style='color:#b91c1c'>LOW</b>" if p in low else ""
        h.append(f"<li>{p}: {n} items" + (f", last slot {last.isoformat()} ({days} days)" if last else "") + f"{flag}</li>")
    h.append("</ul>")
    return subject, "".join(h), low


def maybe_heartbeat(q, log_path, now, dry_run, out=print):
    """First run at/after 7 PM CT each day. Returns the subject if sent (or would send), else None."""
    today = now.date().isoformat()
    if now.hour < HEARTBEAT_HOUR or q.get("heartbeat_sent_on") == today:
        return None
    subject, body, _ = heartbeat_body(q, log_path, now)
    if dry_run:
        out(f"DRY HEARTBEAT {subject}")
        return subject
    if send_email(subject, body):
        q["heartbeat_sent_on"] = today
        out(f"HEARTBEAT  {subject}")
        return subject
    out(f"HEARTBEAT NOT SENT (retry next run): {subject}")
    return None


# ---------------------------------------------------------------- main loop
IN_FLIGHT = ("ig_container_id", "x_media_id", "bsky_video_job")


def run(queue_path, log_path, now, dry_run=False, out=print):
    """Returns list of (id, action, detail). action in posted|would_post|preparing|failed|deferred|skipped."""
    plan = []
    q = load_queue(queue_path)
    failures = []
    sim_posts = {p: [] for p in PLATFORMS}
    busy = set()  # one item per platform touched per run
    clients = {}

    def client(p):
        if p not in clients:
            clients[p] = {"bluesky": Bluesky, "x": XClient, "instagram": Instagram}[p]()
        return clients[p]

    def persist(item, final):
        if dry_run:
            return
        save_queue(queue_path, q)
        if final:
            append_log(log_path, item, now)

    def fail(item, reason):
        item["status"], item["error"] = "failed", reason
        item.pop("claimed_at", None)
        plan.append((item["id"], "failed", reason))
        failures.append(item)
        persist(item, True)

    def posted(item, url, note=None):
        item.update(status="posted", posted_url=url, posted_at=iso(now), error=None)
        item.pop("claimed_at", None)
        plan.append((item["id"], "posted", note or url))
        persist(item, True)

    def retry_or_fail(item, msg):
        item.pop("claimed_at", None)
        item["attempts"] = item.get("attempts", 0) + 1
        if item["attempts"] >= MAX_ATTEMPTS or "ABORT" in msg:
            fail(item, msg)
        else:
            item["error"] = f"attempt {item['attempts']}: {msg}"
            plan.append((item["id"], "deferred", item["error"]))
            persist(item, False)

    # Items already mid-flight go first so a half-done upload finishes before a new one starts.
    items = sorted(q["items"], key=lambda i: (not any(i.get(k) for k in IN_FLIGHT), str(i.get("slot_at", ""))))
    for item in items:
        if item.get("status") != "approved":
            continue
        err = validate(item)
        if err:
            fail(item, f"invalid: {err}")
            continue
        p, slot = item["platform"], parse_ct(item["slot_at"])

        # 1. Resolve an interrupted earlier attempt. Never re-post blindly.
        if item.get("claimed_at"):
            if dry_run:
                plan.append((item["id"], "deferred", "interrupted claim: live run resolves it first"))
                continue
            try:
                if p == "bluesky" and item.get("bsky_rkey"):
                    bs = client(p)
                    if bs.record_exists(item["bsky_rkey"]):
                        posted(item, bs.url_for(item["bsky_rkey"]), "recovered interrupted post")
                        continue
                    item.pop("claimed_at", None)  # confirmed not live: continue below
                elif p == "instagram" and item.get("ig_container_id"):
                    ig = client(p)
                    code, _ = ig.container_status(item["ig_container_id"])
                    if code == "PUBLISHED":
                        posted(item, ig.profile_url(), "recovered interrupted post")
                        continue
                    if code not in ("FINISHED", "IN_PROGRESS"):
                        fail(item, f"interrupted mid-post, container {code}; check the profile")
                        continue
                    item.pop("claimed_at", None)  # container not published: safe to publish below
                else:
                    fail(item, f"interrupted mid-post at {item['claimed_at']}; may be live, check the profile, never auto-retried")
                    continue
            except Exception as e:
                plan.append((item["id"], "deferred", f"can't check interrupted claim yet: {e}"))
                continue

        # 2. Time window.
        if slot > now:
            plan.append((item["id"], "skipped", f"not due until {iso(slot)}"))
            continue
        if now - slot > STALE_AFTER:
            fail(item, f"stale: slot {iso(slot)} passed more than 6h ago; not posted late")
            continue

        # 3. One item per platform per run, then caps (queue + log + this run; Bluesky also the live feed).
        if p in busy:
            plan.append((item["id"], "deferred", f"another {p} item is in progress this run"))
            continue
        times = posted_times(q, log_path, p, sim_posts[p])
        if p == "bluesky" and not dry_run:
            try:
                times = sorted(set(times) | set(client(p).posts_today(now)))
            except Exception as e:
                busy.add(p)
                retry_or_fail(item, f"Bluesky feed check failed: {e}")
                continue
        block = cap_block(p, now, times)
        if block:
            plan.append((item["id"], "deferred", block))
            continue
        busy.add(p)

        if dry_run:
            if p == "x":
                detail = f"{x_weighted_len(x_text(item))}/280: {x_text(item)!r}"
            else:
                detail = f"{item['text'][:80]!r}"
            if item.get("video"):
                detail += f" + video {item['video_url']}"
            first = "would_prepare" if p == "instagram" and not item.get("ig_container_id") else "would_post"
            if first == "would_post":
                sim_posts[p].append(now)
            plan.append((item["id"], first, detail))
            continue

        # 4. Prepare (two-phase media), then claim and post.
        def save_claim():
            item["claimed_at"] = iso(now)
            persist(item, False)
            git_checkpoint(f"claim {item['id']}")

        try:
            c = client(p)
            if p == "bluesky":
                ready = bsky_prepare(item, c)
            elif p == "x":
                ready = x_prepare(item, c, now)
            else:
                ready = ig_prepare(item, c)
            if ready == "published":
                posted(item, c.profile_url(), "container already PUBLISHED")
                continue
            if not ready:
                item["error"] = None
                plan.append((item["id"], "preparing", "media processing; a later run posts it"))
                persist(item, False)
                continue
            if p == "bluesky":
                url = post_bluesky(item, c, save_claim)
            elif p == "x":
                url = post_x(item, c, save_claim)
            else:
                url = post_instagram(item, c, save_claim)
        except PrePostError as e:
            retry_or_fail(item, str(e))
            continue
        except AmbiguousPostError as e:
            fail(item, str(e))
            continue
        except Exception as e:
            if item.get("claimed_at"):
                fail(item, f"error after claim (may be live, check profile): {e!r}")
            else:
                retry_or_fail(item, f"{e!r}")
            continue
        posted(item, url)

    hb = maybe_heartbeat(q, log_path, now, dry_run, out)
    if hb and not dry_run:
        save_queue(queue_path, q)

    if not plan:
        out(f"{'DRY ' if dry_run else ''}nothing approved and due ({iso(now)})")
    for row in plan:
        out(f"{'DRY ' if dry_run else ''}{row[1].upper():13} {row[0]}  {row[2]}")
    if not dry_run:
        email_failures(failures)
    return plan


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="plan only: no network, no writes, no email")
    ap.add_argument("--queue", default=QUEUE)
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--now", help="pretend time (ISO, CT), dry-run only")
    a = ap.parse_args()
    if a.now and not a.dry_run:
        ap.error("--now is for dry runs only")
    now = parse_ct(a.now) if a.now else now_ct()
    try:
        run(a.queue, a.log, now, dry_run=a.dry_run)
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if not a.dry_run:
            email_failures([{"platform": "cloudpost", "id": "-", "slot_at": "-", "text": "", "error": tb[-800:]}])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
