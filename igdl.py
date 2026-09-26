#!/usr/bin/env python3
"""
Download Instagram media: individual posts/reels, or the posts you recently liked.

Requirements:
    pip install -U -r requirements.txt
    (ffmpeg on PATH is recommended for best-quality reels, but not required)

Usage:
    # one or more post/reel URLs
    python igdl.py post "https://www.instagram.com/p/SHORTCODE/"

    # posts you liked since the last run -- run this hourly for "liked in the last hour"
    python igdl.py liked
    python igdl.py liked --count 50 --dry-run

    # backfill from an Instagram "Download your information" export (real like times)
    python igdl.py export instagram-export.zip --hours 24

On "liked in the last hour": Instagram's liked feed is ordered by when you liked each
post, but carries no like timestamp -- only the post's own creation time, which is
unrelated. So a wall-clock window is not available there. Instead this remembers what it
has already fetched in <out>/.igdl-liked.json and takes whatever is new. Run it on a
schedule and each run covers the period since the previous one.

The export path is the only source with true like timestamps, so --hours works there,
but an export takes hours or days to generate and is stale on arrival.
"""

import argparse
import http.cookiejar
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterator
from http.client import HTTPResponse
from pathlib import Path
from typing import ClassVar, cast

from PIL import Image

POST_URL = re.compile(r"^https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/[^/?#]+", re.IGNORECASE)

# The liked feed rejects browser user agents with "useragent mismatch"; it is a
# mobile-app endpoint and needs an app UA even though the session cookie is a web one.
MOBILE_UA = (
    "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; "
    "samsung; SM-G991B; o1s; exynos2100; en_US; 458229258)"
)
IG_APP_ID = "936619743392459"
LIKED_FEED = "https://www.instagram.com/api/v1/feed/liked/"
STATE_FILE = ".igdl-liked.json"


# --------------------------------------------------------------------------- urls


def clean_url(url: str) -> str:
    """Strip query string/fragment (utm_source, igsh, stkn, ...) and validate it's a post or reel."""
    parts = urllib.parse.urlsplit(url.strip())
    if "instagram.com" not in parts.netloc:
        sys.exit(f"Not an Instagram URL: {url}")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[-2] not in ("p", "reel", "reels", "tv"):
        sys.exit(f"Expected a post or reel URL (/p/..., /reel/...), got: {url}")
    path = f"/{segments[-2]}/{segments[-1]}/"
    return urllib.parse.urlunsplit(("https", "www.instagram.com", path, "", ""))


def post_url(shortcode: str) -> str:
    return f"https://www.instagram.com/p/{shortcode}/"


# ----------------------------------------------------------------------- download


def fetch(urls: list[str], out_dir: Path, cookies: Path) -> None:
    """Run gallery-dl once over every URL, so it authenticates and rate-limits one time."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        _ = fh.write("\n".join(urls))
        listfile = Path(fh.name)
    try:
        cmd = [
            sys.executable, "-m", "gallery_dl",
            "--cookies", str(cookies),
            "-D", str(out_dir),   # save straight into out_dir, no nested folders
            "-f", "{username}_{shortcode}_{num}.{extension}",
            "-i", str(listfile),  # gallery-dl skips already-downloaded files by default
        ]
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            print(
                f"gallery-dl exited {result.returncode}; some posts may have been skipped."
                + " If it redirected to login, re-export cookies.txt.",
                file=sys.stderr,
            )
    finally:
        listfile.unlink(missing_ok=True)


def webp_to_jpg(path: Path, quality: int, keep_webp: bool) -> Path:
    target = path.with_suffix(".jpg")
    with Image.open(path) as im:
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.save(target, "JPEG", quality=quality, optimize=True)
    if not keep_webp:
        path.unlink()
    return target


def convert_webps(out_dir: Path, quality: int, keep_webp: bool) -> None:
    for webp in sorted(out_dir.rglob("*.webp")):
        try:
            jpg = webp_to_jpg(webp, quality, keep_webp)
            print(f"Converted {webp.name} -> {jpg.name}")
        except (OSError, ValueError) as e:
            # OSError covers UnidentifiedImageError and truncated/unreadable files;
            # one bad file should not abort the rest of the batch.
            print(f"Failed to convert {webp}: {e}", file=sys.stderr)


# -------------------------------------------------------------------- liked feed


def open_session(cookies: Path) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.MozillaCookieJar(str(cookies))
    try:
        jar.load()
    except OSError as e:
        sys.exit(f"Could not read cookies from {cookies}: {e}")
    if not any(c.name == "sessionid" for c in jar):
        sys.exit(f"No sessionid cookie in {cookies}; re-export it while logged in.")
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def api_get(opener: urllib.request.OpenerDirector, url: str, params: dict[str, str]) -> dict[str, object]:
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}" if params else url,
        headers={"User-Agent": MOBILE_UA, "X-IG-App-ID": IG_APP_ID, "Accept": "*/*"},
    )
    try:
        # OpenerDirector.open is typed as returning Any; for http(s) it is an HTTPResponse
        resp = cast("HTTPResponse", opener.open(req, timeout=30))
        with resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace")
        if e.code in (401, 403):
            sys.exit(f"Instagram rejected the session ({e.code}). Re-export cookies.txt. {detail}")
        sys.exit(f"Instagram returned HTTP {e.code} for {url}. {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Could not reach Instagram: {e.reason}")
    parsed = cast("object", json.loads(body))
    if not isinstance(parsed, dict):
        sys.exit(f"Unexpected response from {url}: not a JSON object")
    return cast("dict[str, object]", parsed)


def iter_liked_shortcodes(opener: urllib.request.OpenerDirector, limit: int) -> Iterator[str]:
    """Yield shortcodes from the liked feed, most recently liked first.

    The feed carries no like timestamps, so ordering is the only recency signal.
    """
    max_id: str | None = None
    seen = 0
    while seen < limit:
        params = {"count": "30"}
        if max_id:
            params["max_id"] = max_id
        page = api_get(opener, LIKED_FEED, params)
        items = page.get("items")
        if not isinstance(items, list):
            return
        for raw in cast("list[object]", items):
            if not isinstance(raw, dict):
                continue
            code = cast("dict[str, object]", raw).get("code")
            if isinstance(code, str):
                yield code
                seen += 1
                if seen >= limit:
                    return
        nxt = page.get("next_max_id")
        if not page.get("more_available") or not isinstance(nxt, str):
            return
        max_id = nxt
        time.sleep(1)  # be gentle on a private endpoint


def load_state(out_dir: Path) -> set[str]:
    path = out_dir / STATE_FILE
    if not path.is_file():
        return set()
    try:
        parsed = cast("object", json.loads(path.read_bytes()))
    except (OSError, ValueError) as e:
        print(f"Ignoring unreadable state file {path}: {e}", file=sys.stderr)
        return set()
    if isinstance(parsed, dict):
        codes = cast("dict[str, object]", parsed).get("fetched")
        if isinstance(codes, list):
            return {c for c in cast("list[object]", codes) if isinstance(c, str)}
    return set()


def save_state(out_dir: Path, codes: set[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / STATE_FILE
    payload = {"fetched": sorted(codes), "updated": int(time.time())}
    try:
        _ = path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"Could not write state file {path}: {e}", file=sys.stderr)


# ------------------------------------------------------------------ export source


def find_likes_json(source: Path) -> bytes:
    """Return the raw bytes of liked_posts.json from a zip, a directory, or the file itself."""
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            names = [n for n in zf.namelist() if n.endswith("liked_posts.json")]
            if not names:
                sys.exit(f"No liked_posts.json inside {source}. Was this a JSON export, not HTML?")
            return zf.read(names[0])
    if source.is_dir():
        matches = sorted(source.rglob("liked_posts.json"))
        if not matches:
            sys.exit(f"No liked_posts.json found under {source}")
        return matches[0].read_bytes()
    if source.is_file():
        return source.read_bytes()
    sys.exit(f"No such file or directory: {source}")


def walk_likes(node: object) -> Iterator[tuple[int, str]]:
    """Yield (timestamp, href) for every like entry, wherever it sits in the tree.

    The export nests likes under keys such as "likes_media_likes", and the exact shape
    has changed between export versions, so this walks the whole tree and picks up any
    object carrying both an href and a timestamp instead of hard-coding one path.
    """
    if isinstance(node, dict):
        fields = cast("dict[str, object]", node)
        href = fields.get("href")
        ts = fields.get("timestamp")
        # bool is an int subclass, so exclude it explicitly
        if isinstance(href, str) and isinstance(ts, int) and not isinstance(ts, bool):
            yield ts, href
        for key, value in fields.items():
            # "likes_comment_likes" holds comments you liked, not posts; its hrefs point
            # at the surrounding post, which would otherwise look like a post like
            if "comment" in key.lower():
                continue
            yield from walk_likes(value)
    elif isinstance(node, list):
        for item in cast("list[object]", node):
            yield from walk_likes(item)


def recent_liked_urls(raw: bytes, hours: float) -> list[str]:
    """Post URLs liked within the last `hours`, newest like first, de-duplicated."""
    data = cast("object", json.loads(raw))
    cutoff = time.time() - hours * 3600
    seen: set[str] = set()
    urls: list[str] = []
    for ts, href in sorted(walk_likes(data), reverse=True):
        if ts < cutoff or not POST_URL.match(href):
            continue
        url = href.split("?")[0].rstrip("/") + "/"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ------------------------------------------------------------------------ the CLI


class Args(argparse.Namespace):
    """Typed view of the parsed command line; the values here are the defaults."""

    mode: str = ""
    # ClassVar: a placeholder argparse always overwrites, never mutated in place
    urls: ClassVar[list[str]] = []
    source: str = ""
    hours: float = 24.0
    count: int = 60
    again: bool = False
    out: str = "downloads"
    cookies: str = "cookies.txt"
    quality: int = 92
    keep_webp: bool = False
    dry_run: bool = False


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Download Instagram posts, reels, and your recent likes.")
    subs = ap.add_subparsers(dest="mode", required=True, metavar="{post,liked,export}")

    # Defaults are taken from Args so that class stays the single source of truth. They
    # must be passed explicitly here: a subparser parses into a fresh namespace and copies
    # every attribute back, so it would otherwise overwrite the Args defaults with None.
    common = argparse.ArgumentParser(add_help=False)
    _ = common.add_argument("-o", "--out", default=Args.out, help="output folder (default: downloads)")
    _ = common.add_argument("-c", "--cookies", default=Args.cookies,
                            help="Netscape cookies.txt (default: cookies.txt)")
    _ = common.add_argument("-q", "--quality", type=int, default=Args.quality,
                            help="JPEG quality 1-95 (default: 92)")
    _ = common.add_argument("--keep-webp", action="store_true", default=Args.keep_webp,
                            help="keep original .webp files")
    _ = common.add_argument("--dry-run", action="store_true", default=Args.dry_run,
                            help="list what would download, then stop")

    p = subs.add_parser("post", parents=[common], help="download specific post/reel URLs")
    _ = p.add_argument("urls", nargs="+", help="one or more Instagram post or reel URLs")

    liked = subs.add_parser("liked", parents=[common], help="download posts you liked since the last run")
    _ = liked.add_argument("--count", type=int, default=Args.count,
                           help="how many recent likes to examine (default: 60)")
    _ = liked.add_argument("--again", action="store_true", default=Args.again,
                           help="ignore saved state and re-fetch")

    exp = subs.add_parser("export", parents=[common], help="download likes from a data export")
    _ = exp.add_argument("source", help="export .zip, its extracted folder, or liked_posts.json")
    _ = exp.add_argument("--hours", type=float, default=Args.hours,
                         help="how far back to look (default: 24)")

    return ap


def collect_urls(args: Args) -> tuple[list[str], set[str]]:
    """Return the URLs to download, plus the shortcodes to record in state afterwards."""
    if args.mode == "post":
        return [clean_url(u) for u in args.urls], set()

    if args.mode == "export":
        return recent_liked_urls(find_likes_json(Path(args.source)), args.hours), set()

    out_dir = Path(args.out)
    already: set[str] = set() if args.again else load_state(out_dir)
    if not already and not args.again:
        print(
            f"No previous state in {out_dir / STATE_FILE}; treating the {args.count} most recent"
            + " likes as new. Later runs will only pick up what you liked since this one."
        )
    opener = open_session(Path(args.cookies))
    fresh = [c for c in iter_liked_shortcodes(opener, args.count) if c not in already]
    return [post_url(c) for c in fresh], set(fresh)


def main() -> None:
    args = build_parser().parse_args(namespace=Args())

    cookies = Path(args.cookies)
    if args.mode != "export" and not cookies.is_file():
        sys.exit(f"Cookies file not found: {cookies}")

    urls, new_codes = collect_urls(args)
    if not urls:
        if args.mode == "liked":
            # Not an error: a scheduled hourly run finding nothing is the normal case,
            # and a non-zero exit would look like a failure to the scheduler.
            print("Nothing new: no likes since the last run.")
            return
        if args.mode == "export":
            sys.exit(
                f"No liked posts in the last {args.hours:g}h. The export is a snapshot:"
                + " likes made after you requested it are not in it."
            )
        sys.exit("Nothing to download.")

    print(f"{len(urls)} post(s) to download:")
    for url in urls:
        print(f"  {url}")
    if args.dry_run:
        return

    out_dir = Path(args.out)
    before: set[Path] = set()
    if out_dir.exists():
        before = {p.resolve() for p in out_dir.rglob("*") if p.is_file()}

    fetch(urls, out_dir, cookies)
    convert_webps(out_dir, args.quality, args.keep_webp)

    if new_codes:
        save_state(out_dir, load_state(out_dir) | new_codes)

    after = {p.resolve() for p in out_dir.rglob("*") if p.is_file()}
    added = sorted(p for p in after - before if p.name != STATE_FILE)
    print(f"\nDone: {len(added)} new file(s)")
    for p in added:
        print(f"  {p}")


if __name__ == "__main__":
    main()
