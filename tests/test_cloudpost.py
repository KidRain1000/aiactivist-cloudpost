#!/usr/bin/env python3
"""Offline tests. cloudpost.http is replaced by FakeNet; any unexpected URL fails the test,
so nothing here can reach X, Meta, Bluesky, Resend or GitHub.

Run: python3 tests/test_cloudpost.py -v
"""
import datetime as dt, json, os, re, shutil, sys, tempfile, unittest, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

for k in ("CLOUDPOST_GIT_CHECKPOINT", "AUTOPOST_NO_EMAIL"):
    os.environ.pop(k, None)
os.environ.update(X_API_KEY="ck", X_API_SECRET="cs", X_ACCESS_TOKEN="tk", X_ACCESS_SECRET="ts",
                  IG_USER_ID="1789", IG_ACCESS_TOKEN="igtok", BSKY_APP_PASSWORD="pw",
                  BSKY_HANDLE="theaiactivist.com", RESEND_API_KEY="re_test")

import cloudpost as cp  # noqa: E402
import export_queue  # noqa: E402

NOW = cp.parse_ct("2026-10-30T08:30:00")
MIN10 = dt.timedelta(minutes=10)


class FakeNet:
    """Routes: (METHOD, url regex, handler(url, headers, data) -> (status, dict|bytes)). Records calls."""

    def __init__(self):
        self.routes, self.calls = [], []

    def on(self, method, pattern, handler):
        self.routes.insert(0, (method, re.compile(pattern), handler))  # newest wins

    def __call__(self, method, url, headers=None, data=None, timeout=60):
        self.calls.append((method, url, dict(headers or {}), data))
        for m, rx, h in self.routes:
            if m == method and rx.search(url):
                status, body = h(url, headers or {}, data)
                return status, body if isinstance(body, bytes) else json.dumps(body).encode()
        raise AssertionError(f"unexpected network call {method} {url}")

    def hits(self, method, pattern):
        return [c for c in self.calls if c[0] == method and re.search(pattern, c[1])]


def item(id, platform, slot=NOW, **kw):
    d = {"id": id, "platform": platform, "slot_at": cp.iso(slot), "text": f"Committee Bulletin {id}",
         "status": "approved", "posted_url": None, "error": None}
    if platform == "x":
        d["x_link"] = False
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cloudpost-test-")
        self.q = os.path.join(self.d, "queue.json")
        self.log = os.path.join(self.d, "post-log.csv")
        self.media = os.path.join(self.d, "media")
        os.makedirs(self.media)
        open(os.path.join(self.media, "b0001.mp4"), "wb").write(b"\x00" * (9 * 1024 * 1024))  # 3 X chunks
        self.net = FakeNet()
        self._http, self._media = cp.http, cp.MEDIA_DIR
        cp.http, cp.MEDIA_DIR = self.net, self.media
        self.net.on("POST", r"api\.resend\.com/emails", lambda u, h, d: (200, {"id": "em_1"}))

    def tearDown(self):
        cp.http, cp.MEDIA_DIR = self._http, self._media
        shutil.rmtree(self.d)

    def write(self, *items, **top):
        json.dump(dict({"version": 1, "items": list(items)}, **top), open(self.q, "w"))

    def run_at(self, now=NOW, dry=False):
        self.lines = []
        p = cp.run(self.q, self.log, now, dry_run=dry, out=self.lines.append)
        return {i: (a, d) for i, a, d in p}

    def get(self, id):
        return next(i for i in cp.load_queue(self.q)["items"] if i["id"] == id)

    def queued_claim(self, id):
        return self.get(id).get("claimed_at")

    def emails(self):
        return [json.loads(c[3]) for c in self.net.hits("POST", r"api\.resend\.com")]


# ---------------------------------------------------------------- OAuth 1.0a
class OAuth(unittest.TestCase):
    def test_known_vector(self):
        # Worked example from X's "Creating a signature" doc (developer.x.com, OAuth 1.0a).
        h = cp.oauth1_header(
            "POST", "https://api.twitter.com/1.1/statuses/update.json?include_entities=true",
            {"status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
            "xvz1evFS4wEEPTGEFPHBog", "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
            "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
            nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp=1318622958)
        self.assertIn('oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"', h)
        self.assertTrue(h.startswith("OAuth "))

    def test_query_params_are_signed(self):
        args = ("ck", "cs", "tk", "ts")
        kw = dict(nonce="n", timestamp=1)
        a = cp.oauth1_header("GET", "https://api.x.com/2/media/upload?command=STATUS&media_id=7", None, *args, **kw)
        b = cp.oauth1_header("GET", "https://api.x.com/2/media/upload", {"command": "STATUS", "media_id": "7"}, *args, **kw)
        c = cp.oauth1_header("GET", "https://api.x.com/2/media/upload", None, *args, **kw)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


# ---------------------------------------------------------------- X
class XTests(Base):
    def tweets_ok(self, id="555"):
        def h(u, hd, d):
            self.assertTrue(hd["Authorization"].startswith("OAuth "))
            self.assertIn("oauth_signature=", hd["Authorization"])
            self.tweet_bodies.append(json.loads(d))
            self.claim_at_send.append(self.queued_claim(self.sending))
            return 201, {"data": {"id": id, "text": "x"}}
        self.tweet_bodies, self.claim_at_send = [], []
        self.net.on("POST", r"api\.x\.com/2/tweets$", h)

    def test_text_post_no_link_by_default(self):
        self.write(item("x1", "x", link="https://theaiactivist.com/?utm_source=x"))
        self.sending = "x1"
        self.tweets_ok()
        p = self.run_at()
        self.assertEqual(p["x1"][0], "posted")
        self.assertEqual(self.get("x1")["posted_url"], "https://x.com/aiactivisthq/status/555")
        self.assertNotIn("theaiactivist.com", self.tweet_bodies[0]["text"])
        self.assertNotIn("media", self.tweet_bodies[0])
        self.assertTrue(self.claim_at_send[0], "claim must be on disk before the send")
        self.assertIsNone(self.get("x1").get("claimed_at"))
        self.assertIn("x1,x,", open(self.log).read())

    def test_x_link_true_appends_link(self):
        self.write(item("x1", "x", link="https://theaiactivist.com/", x_link=True))
        self.sending = "x1"
        self.tweets_ok()
        self.run_at()
        self.assertTrue(self.tweet_bodies[0]["text"].endswith("\n\nhttps://theaiactivist.com/"))

    def test_url_in_text_without_x_link_is_refused(self):
        self.write(item("x1", "x", text="see theaiactivist.com"))
        p = self.run_at()
        self.assertEqual(p["x1"][0], "failed")
        self.assertIn("x_link", p["x1"][1])
        self.assertEqual(self.net.hits("POST", r"api\.x\.com"), [])

    def test_video_two_phase(self):
        self.write(item("x1", "x", video="b0001.mp4", video_url="https://media.example/b0001.mp4"))
        self.sending = "x1"
        self.net.on("POST", r"/2/media/upload/initialize$", lambda u, h, d: (200, {"data": {"id": "77", "expires_after_secs": 86400}}))
        self.net.on("POST", r"/2/media/upload/77/append$", lambda u, h, d: (200, {"data": {"expires_at": 1}}))
        self.net.on("POST", r"/2/media/upload/77/finalize$", lambda u, h, d: (200, {"data": {
            "id": "77", "processing_info": {"state": "pending", "check_after_secs": 5}}}))
        self.tweets_ok()
        p = self.run_at()
        self.assertEqual(p["x1"][0], "preparing")
        self.assertEqual(self.get("x1")["x_media_id"], "77")
        self.assertEqual(len(self.net.hits("POST", r"/append$")), 3)
        init = json.loads(self.net.hits("POST", r"/initialize$")[0][3])
        self.assertEqual(init, {"media_type": "video/mp4", "total_bytes": 9 * 1024 * 1024, "media_category": "tweet_video"})
        self.assertIn(b'name="segment_index"\r\n\r\n2\r\n', self.net.hits("POST", r"/append$")[2][3])
        self.assertEqual(self.tweet_bodies, [])
        # run 2: still processing
        self.net.on("GET", r"/2/media/upload\?command=STATUS&media_id=77", lambda u, h, d: (200, {"data": {
            "id": "77", "processing_info": {"state": "in_progress"}}}))
        self.assertEqual(self.run_at(NOW + MIN10)["x1"][0], "preparing")
        # run 3: done -> post with the media id
        self.net.on("GET", r"/2/media/upload\?command=STATUS&media_id=77", lambda u, h, d: (200, {"data": {
            "id": "77", "processing_info": {"state": "succeeded"}}}))
        self.assertEqual(self.run_at(NOW + 2 * MIN10)["x1"][0], "posted")
        self.assertEqual(self.tweet_bodies[0]["media"], {"media_ids": ["77"]})
        self.assertEqual(len(self.net.hits("POST", r"/initialize$")), 1, "never re-uploads")

    def test_video_downloads_from_url_when_not_local(self):
        os.remove(os.path.join(self.media, "b0001.mp4"))
        self.write(item("x1", "x", video="b0001.mp4", video_url="https://media.example/b0001.mp4"))
        self.net.on("GET", r"media\.example/b0001\.mp4", lambda u, h, d: (200, b"\x00" * 10))
        self.net.on("POST", r"/initialize$", lambda u, h, d: (200, {"data": {"id": "9"}}))
        self.net.on("POST", r"/9/append$", lambda u, h, d: (200, {}))
        self.net.on("POST", r"/9/finalize$", lambda u, h, d: (200, {"data": {"id": "9", "processing_info": {"state": "pending"}}}))
        self.assertEqual(self.run_at()["x1"][0], "preparing")
        self.assertEqual(len(self.net.hits("GET", r"media\.example")), 1)

    def test_4xx_is_retryable_5xx_is_ambiguous(self):
        self.write(item("x1", "x"), item("x2", "x", slot=NOW - dt.timedelta(hours=3)))
        self.net.on("POST", r"/2/tweets$", lambda u, h, d: (403, {"detail": "duplicate"}))
        p = self.run_at()
        x = [k for k, v in p.items() if v[0] == "deferred" and "attempt 1" in v[1]]
        self.assertEqual(len(x), 1)
        self.assertIsNone(self.get(x[0]).get("claimed_at"))
        self.net.on("POST", r"/2/tweets$", lambda u, h, d: (503, {"detail": "busy"}))
        p = self.run_at(NOW + MIN10)
        self.assertTrue(any(v[0] == "failed" and "check x.com" in v[1] for v in p.values()))

    def test_interrupted_x_claim_is_never_retried(self):
        self.write(item("x1", "x", claimed_at=cp.iso(NOW - MIN10)))
        p = self.run_at()
        self.assertEqual(p["x1"][0], "failed")
        self.assertIn("interrupted", p["x1"][1])
        self.assertEqual(self.net.hits("POST", r"api\.x\.com"), [])


# ---------------------------------------------------------------- Instagram
class IGTests(Base):
    URL = "https://media.example/b0001.mp4"

    def setUp(self):
        super().setUp()
        self.status = "IN_PROGRESS"
        self.publishes = []
        self.net.on("POST", r"graph\.facebook\.com/v26\.0/1789/media$", self.create)
        self.net.on("GET", r"graph\.facebook\.com/v26\.0/C1\?fields=status_code", lambda u, h, d: (200, {"status_code": self.status, "id": "C1"}))
        self.net.on("POST", r"/1789/media_publish$", self.publish)
        self.net.on("GET", r"/M1\?fields=permalink", lambda u, h, d: (200, {"permalink": "https://www.instagram.com/reel/abc/"}))

    def create(self, u, h, d):
        f = dict(urllib.parse.parse_qsl(d.decode()))
        self.assertEqual(f["media_type"], "REELS")
        self.assertEqual(f["video_url"], self.URL)
        self.assertEqual(f["access_token"], "igtok")
        self.assertIn("caption", f)
        return 200, {"id": "C1"}

    def publish(self, u, h, d):
        f = dict(urllib.parse.parse_qsl(d.decode()))
        self.assertEqual(f["creation_id"], "C1")
        self.publishes.append(self.queued_claim("ig1"))
        return 200, {"id": "M1"}

    def ig(self, **kw):
        return item("ig1", "instagram", video="b0001.mp4", video_url=self.URL, **kw)

    def test_two_phase_state_machine(self):
        self.write(self.ig())
        p = self.run_at()
        self.assertEqual(p["ig1"][0], "preparing")
        self.assertEqual(self.get("ig1")["ig_container_id"], "C1")
        self.assertEqual(self.publishes, [], "never publishes in the run that created the container")
        self.assertEqual(self.net.hits("GET", r"fields=status_code"), [])
        self.assertEqual(self.run_at(NOW + MIN10)["ig1"][0], "preparing")  # IN_PROGRESS
        self.status = "FINISHED"
        p = self.run_at(NOW + 2 * MIN10)
        self.assertEqual(p["ig1"][0], "posted")
        self.assertEqual(self.get("ig1")["posted_url"], "https://www.instagram.com/reel/abc/")
        self.assertEqual(len(self.publishes), 1)
        self.assertTrue(self.publishes[0], "claim must be on disk before media_publish")
        self.assertEqual(len(self.net.hits("POST", r"/1789/media$")), 1)
        self.assertNotIn("igtok", open(self.log).read())

    def test_error_container_retries_then_fails(self):
        self.write(self.ig(ig_container_id="C1"))
        self.status = "ERROR"
        for n in range(1, 3):
            p = self.run_at(NOW + n * MIN10)
            self.assertEqual(p["ig1"][0], "deferred", p)
            self.assertNotIn("ig_container_id", self.get("ig1"))
            q = cp.load_queue(self.q)
            q["items"][0]["ig_container_id"] = "C1"  # pretend the next run made a new one
            cp.save_queue(self.q, q)
        p = self.run_at(NOW + 3 * MIN10)
        self.assertEqual(p["ig1"][0], "failed")
        self.assertIn("ERROR", p["ig1"][1])

    def test_interrupted_claim_published_is_recovered(self):
        self.write(self.ig(ig_container_id="C1", claimed_at=cp.iso(NOW - MIN10)))
        self.status = "PUBLISHED"
        p = self.run_at()
        self.assertEqual(p["ig1"][0], "posted")
        self.assertEqual(self.publishes, [])

    def test_interrupted_claim_not_published_publishes(self):
        self.write(self.ig(ig_container_id="C1", claimed_at=cp.iso(NOW - MIN10)))
        self.status = "FINISHED"
        self.assertEqual(self.run_at()["ig1"][0], "posted")
        self.assertEqual(len(self.publishes), 1)

    def test_publish_network_error_but_published(self):
        self.write(self.ig(ig_container_id="C1"))
        self.status = "FINISHED"

        def boom(u, h, d):
            self.status = "PUBLISHED"
            raise TimeoutError("read timed out")
        self.net.on("POST", r"/1789/media_publish$", boom)
        p = self.run_at()
        self.assertEqual(p["ig1"][0], "posted")
        self.assertEqual(self.get("ig1")["posted_url"], "https://www.instagram.com/aiactivisthq/reels/")

    def test_publish_network_error_unknown_is_failed(self):
        self.write(self.ig(ig_container_id="C1"))
        self.status = "FINISHED"
        self.net.on("POST", r"/1789/media_publish$", lambda u, h, d: (_ for _ in ()).throw(TimeoutError("t")))
        p = self.run_at()
        self.assertEqual(p["ig1"][0], "failed")  # container still FINISHED but we can't tell: human check

    def test_stale_while_processing(self):
        self.write(self.ig(ig_container_id="C1", slot_at=cp.iso(NOW - dt.timedelta(hours=7))))
        p = self.run_at()
        self.assertEqual(p["ig1"][0], "failed")
        self.assertIn("stale", p["ig1"][1])

    def test_ig_needs_video(self):
        self.write(item("ig1", "instagram"))
        self.assertIn("Reels only", self.run_at()["ig1"][1])


# ---------------------------------------------------------------- Bluesky
class BskyTests(Base):
    def setUp(self):
        super().setUp()
        self.state = {"state": "JOB_STATE_ENCODING"}
        self.records = []
        sess = {"did": "did:plc:me", "accessJwt": "jwt",
                "didDoc": {"service": [{"id": "#atproto_pds", "serviceEndpoint": "https://pds.example.host"}]}}
        self.net.on("POST", r"createSession$", lambda u, h, d: (200, sess))
        self.net.on("GET", r"getAuthorFeed", lambda u, h, d: (200, {"feed": []}))
        self.net.on("GET", r"getServiceAuth", lambda u, h, d: (200, {"token": "svc"}))
        self.net.on("POST", r"video\.bsky\.app/xrpc/app\.bsky\.video\.uploadVideo", lambda u, h, d: (200, {"jobId": "J1"}))
        self.net.on("GET", r"getJobStatus\?jobId=J1", lambda u, h, d: (200, {"jobStatus": self.state}))
        self.net.on("POST", r"createRecord$", self.create)

    def create(self, u, h, d):
        self.records.append((json.loads(d), self.queued_claim("bs1")))
        return 200, {"uri": "at://x", "cid": "c"}

    def test_video_two_phase(self):
        self.write(item("bs1", "bluesky", video="b0001.mp4", video_url="https://media.example/b0001.mp4",
                        text="Bulletin\n\ntheaiactivist.com", alt="text card"))
        self.assertEqual(self.run_at()["bs1"][0], "preparing")
        self.assertEqual(self.get("bs1")["bsky_video_job"], "J1")
        self.state = {"state": "JOB_STATE_COMPLETED", "blob": {"$type": "blob", "ref": {"$link": "b"}, "mimeType": "video/mp4", "size": 9}}
        p = self.run_at(NOW + MIN10)
        self.assertEqual(p["bs1"][0], "posted")
        rec, claim = self.records[0]
        self.assertTrue(claim)
        self.assertEqual(rec["record"]["embed"]["$type"], "app.bsky.embed.video")
        self.assertEqual(rec["record"]["embed"]["video"]["ref"]["$link"], "b")
        self.assertEqual(rec["record"]["facets"][0]["features"][0]["uri"], "https://theaiactivist.com")
        self.assertEqual(self.get("bs1")["posted_url"], f"https://bsky.app/profile/theaiactivist.com/post/{rec['rkey']}")
        self.assertEqual(len(self.net.hits("POST", r"uploadVideo")), 1)

    def test_live_feed_counts_toward_cap(self):
        self.net.on("GET", r"getAuthorFeed", lambda u, h, d: (200, {"feed": [{"post": {
            "author": {"did": "did:plc:me"}, "record": {"createdAt": "2026-10-30T12:00:00.000Z"}}}]}))  # 7:00 CT, by hand
        self.write(item("bs1", "bluesky"))
        p = self.run_at()
        self.assertEqual(p["bs1"][0], "deferred")
        self.assertIn("daily cap 1/day", p["bs1"][1])

    def test_missing_password_retries(self):
        os.environ.pop("BSKY_APP_PASSWORD")
        try:
            self.write(item("bs1", "bluesky"))
            p = self.run_at()
            self.assertEqual(p["bs1"][0], "deferred")
            self.assertIn("BSKY_APP_PASSWORD", p["bs1"][1])
        finally:
            os.environ["BSKY_APP_PASSWORD"] = "pw"


# ---------------------------------------------------------------- caps, stale, dry run
class Rules(Base):
    def test_window_caps_and_gap_dry(self):
        self.write(item("x-future", "x", slot=NOW + MIN10),
                   item("x-stale", "x", slot=NOW - dt.timedelta(hours=6, minutes=1)),
                   item("x-due", "x"),
                   item("x-second", "x", slot=NOW - dt.timedelta(minutes=1)),
                   item("ig-due", "instagram", video="b.mp4", video_url="https://m/b.mp4"),
                   item("x-reply", "x", reply_to="at://someone"),
                   item("x-draft", "x", status="draft"))
        before = open(self.q).read()
        p = self.run_at(dry=True)
        self.assertEqual(p["x-future"][0], "skipped")
        self.assertEqual(p["x-stale"][0], "failed")
        self.assertIn("stale", p["x-stale"][1])
        self.assertEqual(p["x-second"][0], "would_post")  # earliest slot first
        self.assertEqual(p["x-due"][0], "deferred")
        self.assertIn("in progress this run", p["x-due"][1])
        self.assertEqual(p["ig-due"][0], "would_prepare")
        self.assertIn("replies are never automated", p["x-reply"][1])
        self.assertNotIn("x-draft", p)
        self.assertEqual(open(self.q).read(), before, "dry run must not write the queue")
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(self.net.calls, [], "dry run must not touch the network")

    def test_gap_rule_from_log(self):
        with open(self.log, "w") as f:
            f.write("logged_at,id,platform,slot_at,status,posted_url,error\n"
                    f"{cp.iso(NOW - dt.timedelta(minutes=69))},prev,x,,posted,u,\n")
        self.write(item("x1", "x"))
        p = self.run_at(dry=True)
        self.assertEqual(p["x1"][0], "deferred")
        self.assertIn("gap rule", p["x1"][1])
        with open(self.log, "w") as f:
            f.write("logged_at,id,platform,slot_at,status,posted_url,error\n"
                    f"{cp.iso(NOW - dt.timedelta(minutes=71))},prev,x,,posted,u,\n")
        self.assertEqual(self.run_at(dry=True)["x1"][0], "would_post")

    def test_daily_cap_from_queue(self):
        done = [item(f"p{i}", "x", status="posted", posted_at=cp.iso(NOW.replace(hour=0, minute=5) + i * dt.timedelta(minutes=71)))
                for i in range(cp.DAILY_CAP["x"])]
        self.write(*done, item("x1", "x"))
        p = self.run_at(dry=True)
        self.assertEqual(p["x1"][0], "deferred")
        self.assertIn(f"daily cap {cp.DAILY_CAP['x']}/day", p["x1"][1])

    def test_failure_email_internal_only(self):
        self.write(item("x1", "x", slot=NOW - dt.timedelta(hours=7)))
        self.run_at()
        mails = self.emails()
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails[0]["to"], ["chris.pearson@savoytx.com"])
        self.assertIn("noreply@getnmad.com", mails[0]["from"])
        h = self.net.hits("POST", r"api\.resend\.com")[0][2]
        self.assertTrue(h.get("User-Agent"))

    def test_validate_rejects_mac_paths(self):
        self.assertIn("export_queue", cp.validate(item("x1", "x", video_path="/Users/cc/b.mp4")))


# ---------------------------------------------------------------- heartbeat
class Heartbeat(Base):
    def test_once_a_day_after_7pm_and_low_flag(self):
        eve = NOW.replace(hour=18, minute=50)
        self.write(item("x-tom", "x", slot=eve + dt.timedelta(days=1)))
        with open(self.log, "w") as f:
            f.write("logged_at,id,platform,slot_at,status,posted_url,error\n"
                    f"{cp.iso(eve - dt.timedelta(hours=2))},x0,x,,posted,https://x.com/a/status/1,\n"
                    f"{cp.iso(eve - dt.timedelta(hours=1))},ig0,instagram,,failed,,stale\n"
                    f"{cp.iso(eve - dt.timedelta(hours=30))},old,x,,posted,https://x.com/a/status/0,\n")
        self.run_at(eve)
        self.assertEqual(self.emails(), [])
        self.run_at(eve + dt.timedelta(minutes=15))  # 19:05
        mails = self.emails()
        self.assertEqual(len(mails), 1)
        self.assertTrue(mails[0]["subject"].startswith("QUEUE RUNNING LOW"))
        self.assertIn("x: 1", mails[0]["html"])
        self.assertNotIn("old", mails[0]["html"])
        self.assertIn("stale", mails[0]["html"])
        self.assertEqual(cp.load_queue(self.q)["heartbeat_sent_on"], "2026-10-30")
        self.run_at(eve + dt.timedelta(minutes=25))
        self.assertEqual(len(self.emails()), 1, "one heartbeat per day")

    def test_not_low_with_runway(self):
        eve = NOW.replace(hour=19, minute=5)
        far = eve + dt.timedelta(days=5)
        self.write(item("x", "x", slot=far), item("b", "bluesky", slot=far),
                   item("i", "instagram", slot=far, video="b.mp4", video_url="https://m/b.mp4"))
        self.run_at(eve)
        self.assertFalse(self.emails()[0]["subject"].startswith("QUEUE RUNNING LOW"))

    def test_failed_send_retries_next_run(self):
        self.net.on("POST", r"api\.resend\.com/emails", lambda u, h, d: (500, {"error": "x"}))
        self.write()
        self.run_at(NOW.replace(hour=19, minute=5))
        self.assertNotIn("heartbeat_sent_on", cp.load_queue(self.q))


# ---------------------------------------------------------------- export
class Export(unittest.TestCase):
    def test_export(self):
        d = tempfile.mkdtemp()
        try:
            vids = os.path.join(d, "Mac", "video", "approved")
            os.makedirs(vids)
            open(os.path.join(vids, "b1.mp4"), "wb").write(b"v1")
            src = os.path.join(d, "local.json")
            into = os.path.join(d, "cloud.json")
            mo = os.path.join(d, "media")
            vp = os.path.join(vids, "b1.mp4")
            later = NOW + dt.timedelta(hours=2)
            local = {"version": 1, "items": [
                {"id": "x1", "platform": "x", "slot_at": cp.iso(later), "text": "t", "status": "approved",
                 "link": "https://theaiactivist.com/", "video_path": vp},
                {"id": "ig1", "platform": "instagram", "slot_at": cp.iso(later), "text": "t", "status": "approved", "video_path": vp},
                {"id": "bs1", "platform": "bluesky", "slot_at": cp.iso(later), "text": "t", "status": "draft", "video_path": vp},
                {"id": "x-old", "platform": "x", "slot_at": cp.iso(NOW - dt.timedelta(hours=8)), "text": "t", "status": "approved"},
                {"id": "x-img", "platform": "x", "slot_at": cp.iso(later), "text": "t", "status": "approved", "image_path": vp},
                {"id": "x-posted-in-cloud", "platform": "x", "slot_at": cp.iso(later), "text": "t", "status": "approved"}]}
            json.dump(local, open(src, "w"))
            json.dump({"version": 1, "items": [{"id": "x-posted-in-cloud", "platform": "x", "status": "posted"}]}, open(into, "w"))
            added, skipped, _ = export_queue.export(src, into, mo, "https://cdn.example/media/", NOW, out=lambda s: None, fix=False)
            self.assertEqual(sorted(a["id"] for a in added), ["ig1", "x1"])
            cloud = {i["id"]: i for i in json.load(open(into))["items"]}
            self.assertEqual(cloud["x1"]["video"], "b1.mp4")
            self.assertEqual(cloud["x1"]["video_url"], "https://cdn.example/media/b1.mp4")
            self.assertIs(cloud["x1"]["x_link"], False)
            self.assertNotIn("video_path", cloud["x1"])
            self.assertNotIn("x_link", cloud["ig1"])
            self.assertEqual(cloud["x-posted-in-cloud"]["status"], "posted", "never clobbers cloud state")
            self.assertNotIn("bs1", cloud)
            self.assertEqual({i for i, _ in skipped}, {"x-old", "x-img"})
            self.assertEqual(os.listdir(mo), ["b1.mp4"])
            self.assertIsNone(cp.validate(cloud["x1"]))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
