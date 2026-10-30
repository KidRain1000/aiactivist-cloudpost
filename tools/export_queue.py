#!/usr/bin/env python3
"""Mac queue -> cloud queue. Only status=approved items go across.

  - video_path (absolute Mac path) becomes video (filename) + video_url (MEDIA_BASE_URL/filename).
  - The referenced mp4s are copied into --media-out (the media repo's media/ folder).
  - X items get x_link=false unless they already say otherwise (no URL on X, $0.015 not $0.20).
  - Merge, never clobber: an id already in the cloud queue is left exactly as the cloud has it
    (posted / claimed / mid-upload state survives a re-export).
  - Copies are made Reels-safe with ffmpeg (Meta spec: moov atom at the front, AAC <= 48 kHz;
    our renders are moov-at-end, 96 kHz): video stream copied untouched, audio resampled to
    48 kHz AAC 128k, +faststart. Sources are never modified. --no-fix copies byte for byte.
  - Skipped with a reason: image items (not built in the cloud), slots already more than 6h past,
    missing video files.

Usage:
  export_queue.py --base-url https://<user>.github.io/<media-repo>/media --dry-run
  export_queue.py --base-url ... [--src LOCAL_QUEUE] [--into CLOUD_QUEUE] [--media-out DIR]
"""
import argparse, json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import cloudpost  # noqa: E402

SRC = os.path.join(os.path.dirname(ROOT), "ai-activist-autopost", "queue.json")
KEEP = ("id", "platform", "slot_at", "text", "link", "alt", "quiet", "art_checked", "x_link")


def convert(item, base_url):
    """Cloud item, or (None, reason)."""
    if item.get("image_path"):
        return None, "image items are not built in the cloud poster"
    out = {k: item[k] for k in KEEP if k in item}
    vp = item.get("video_path")
    if vp:
        name = os.path.basename(vp)
        out["video"] = name
        out["video_url"] = base_url.rstrip("/") + "/" + name
    if out["platform"] == "x":
        out.setdefault("x_link", False)
    out.update(status="approved", posted_url=None, error=None)
    return out, None


def reels_safe_copy(src, dst):
    """Remux for Instagram Reels. Writes to a temp name, then renames, so a crash never leaves half a file."""
    tmp = dst + ".tmp.mp4"
    subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-b:a", "128k", "-movflags", "+faststart", tmp],
                   check=True, capture_output=True, text=True)
    os.replace(tmp, dst)


def export(src, into, media_out, base_url, now, dry_run=False, out=print, fix=True):
    local = cloudpost.load_queue(src)
    cloud = cloudpost.load_queue(into) if os.path.exists(into) else {"version": 1, "items": []}
    have = {i["id"] for i in cloud["items"]}
    added, kept, skipped, copies = [], 0, [], {}
    for item in local["items"]:
        if item.get("status") != "approved":
            continue
        if item["id"] in have:
            kept += 1
            continue
        try:
            if now - cloudpost.parse_ct(item["slot_at"]) > cloudpost.STALE_AFTER:
                skipped.append((item["id"], "slot already more than 6h past"))
                continue
        except (KeyError, ValueError):
            skipped.append((item.get("id"), "bad slot_at"))
            continue
        c, why = convert(item, base_url)
        if not c:
            skipped.append((item["id"], why))
            continue
        vp = item.get("video_path")
        if vp:
            if not os.path.isfile(vp):
                skipped.append((item["id"], f"video missing: {vp}"))
                continue
            prev = copies.get(c["video"])
            if prev and os.path.getsize(prev) != os.path.getsize(vp):
                skipped.append((item["id"], f"two different files named {c['video']}"))
                continue
        err = cloudpost.validate(c)
        if err:
            skipped.append((item["id"], f"invalid in cloud: {err}"))
            continue
        if vp:
            copies[c["video"]] = vp
        added.append(c)

    copied = 0
    if not dry_run:
        os.makedirs(media_out, exist_ok=True)
        for name, vp in copies.items():
            dst = os.path.join(media_out, name)
            if fix:
                if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(vp):
                    reels_safe_copy(vp, dst)
                    copied += 1
            elif not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(vp):
                shutil.copy2(vp, dst)
                copied += 1
        cloud["items"].extend(added)
        cloudpost.save_queue(into, cloud)

    tag = "DRY " if dry_run else ""
    by = {}
    for c in added:
        by[c["platform"]] = by.get(c["platform"], 0) + 1
    out(f"{tag}added {len(added)} {by}, already in cloud queue {kept}, skipped {len(skipped)}")
    out(f"{tag}videos referenced {len(copies)}" + ("" if dry_run else f", copied {copied} to {media_out}"))
    for i, why in skipped:
        out(f"{tag}  SKIP {i}: {why}")
    return added, skipped, copies


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=SRC, help="local Mac queue.json")
    ap.add_argument("--into", default=os.path.join(ROOT, "queue.json"), help="cloud queue.json (merged into)")
    ap.add_argument("--media-out", default=os.path.join(ROOT, "export", "media"), help="media repo media/ folder")
    ap.add_argument("--base-url", default=os.environ.get("MEDIA_BASE_URL"), help="public URL of the media folder")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-fix", action="store_true", help="copy mp4s byte for byte (skip the Reels-safe remux)")
    a = ap.parse_args()
    if not a.base_url:
        ap.error("--base-url (or env MEDIA_BASE_URL) is required")
    if not a.no_fix and not a.dry_run and not shutil.which("ffmpeg"):
        ap.error("ffmpeg not found (needed for the Reels-safe remux); install it or pass --no-fix")
    export(a.src, a.into, a.media_out, a.base_url, cloudpost.now_ct(), dry_run=a.dry_run, fix=not a.no_fix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
