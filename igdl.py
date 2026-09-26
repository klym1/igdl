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

    # same, for posts you saved
    python igdl.py saved

    # backfill from an Instagram "Download your information" export (real like times)
    python igdl.py export instagram-export.zip --hours 24

On "liked in the last hour": Instagram's liked and saved feeds are ordered by when you
liked or saved each post, but carry no such timestamp -- only the post's own creation
time, which is unrelated. So a wall-clock window is not available there. Instead this
remembers what it has already fetched, in <out>/.igdl-liked.json and
<out>/.igdl-saved.json respectively, and takes whatever is new. Run it on a schedule and
each run covers the period since the previous one.

The export path is the only source with true like timestamps, so --hours works there,
but an export takes hours or days to generate and is stale on arrival.
"""

import argparse
import datetime as dt
import http.cookiejar
import json
import logging
import re
import shutil
import subprocess
import sys
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

log = logging.getLogger("igdl")

POST_URL = re.compile(r"^https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/[^/?#]+", re.IGNORECASE)

# The liked feed rejects browser user agents with "useragent mismatch"; it is a
# mobile-app endpoint and needs an app UA even though the session cookie is a web one.
MOBILE_UA = (
    "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; "
    "samsung; SM-G991B; o1s; exynos2100; en_US; 458229258)"
)
IG_APP_ID = "936619743392459"

# Both feeds answer the same headers and paginate the same way, but their items differ:
# the liked feed's items *are* the media, the saved feed wraps each in {"media": {...}}.
# Neither carries a like/save timestamp, so their ordering is the only recency signal.
FEEDS = {
    "liked": "https://www.instagram.com/api/v1/feed/liked/",
    "saved": "https://www.instagram.com/api/v1/feed/saved/posts/",
}
# One state file per feed: a post can be both liked and saved, and sharing a file would
# make whichever ran first hide it from the other.
STATE_FILES = {"liked": ".igdl-liked.json", "saved": ".igdl-saved.json"}


def state_file(mode: str) -> str:
    return STATE_FILES.get(mode, ".igdl-liked.json")


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


def shortcode_of(url: str) -> str:
    """The trailing shortcode of a cleaned post URL, e.g. .../p/ABC123/ -> ABC123."""
    return url.rstrip("/").rsplit("/", 1)[-1]


# ------------------------------------------------------------------------ logging


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def stamp(ts: float) -> str:
    # tz-aware then converted to local time, so logs read in the reader's own clock
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def files_under(out_dir: Path) -> dict[Path, int]:
    """Every file below out_dir mapped to its size, so runs can be diffed by content."""
    if not out_dir.exists():
        return {}
    known_state = set(STATE_FILES.values())
    found: dict[Path, int] = {}
    for p in out_dir.rglob("*"):
        if p.is_file() and p.name not in known_state:
            try:
                found[p.resolve()] = p.stat().st_size
            except OSError:  # vanished between the glob and the stat
                continue
    return found


# ----------------------------------------------------------------------- download


def fetch_one(url: str, out_dir: Path, cookies: Path) -> bool:
    """Download a single post with gallery-dl. Returns True if it exited cleanly.

    One post per invocation rather than one batch over an input file: that costs an
    interpreter start per post, but it is what lets the caller record progress between
    posts, so an interrupted run does not have to be redone from the beginning.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--cookies", str(cookies),
        "-D", str(out_dir),   # save straight into out_dir, no nested folders
        "-f", "{username}_{shortcode}_{num}.{extension}",
        url,                  # gallery-dl skips already-downloaded files by default
    ]
    log.debug("running: %s", " ".join(cmd))
    started = time.monotonic()
    before = files_under(out_dir)
    result = subprocess.run(cmd, check=False)
    elapsed = time.monotonic() - started
    added = {p: n for p, n in files_under(out_dir).items() if p not in before}

    if result.returncode != 0:
        log.error(
            "gallery-dl exited %d after %.1fs for %s; leaving it unrecorded so the next run"
            + " retries it. If it redirected to login, re-export cookies.txt.",
            result.returncode, elapsed, url,
        )
        return False

    if added:
        log.info("  +%d file(s), %s in %.1fs", len(added), human_bytes(sum(added.values())), elapsed)
        for p in sorted(added):
            log.debug("    %s (%s)", p.name, human_bytes(added[p]))
    else:
        log.info("  nothing new in %.1fs (already downloaded)", elapsed)
    return True


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
    webps = sorted(out_dir.rglob("*.webp"))
    if not webps:
        log.debug("no .webp files to convert in %s", out_dir)
        return
    log.info("converting %d .webp file(s) to .jpg at quality %d", len(webps), quality)
    converted = failed = 0
    for webp in webps:
        try:
            jpg = webp_to_jpg(webp, quality, keep_webp)
        except (OSError, ValueError) as e:
            # OSError covers UnidentifiedImageError and truncated/unreadable files;
            # one bad file should not abort the rest of the batch.
            log.error("failed to convert %s: %s", webp, e)
            failed += 1
            continue
        converted += 1
        log.debug("  %s -> %s (%s)", webp.name, jpg.name, human_bytes(jpg.stat().st_size))
    log.info("converted %d, failed %d%s", converted, failed,
             "" if keep_webp else " (originals removed)")


# -------------------------------------------------------------------- liked feed


def open_session(cookies: Path) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.MozillaCookieJar(str(cookies))
    try:
        jar.load()
    except OSError as e:
        sys.exit(f"Could not read cookies from {cookies}: {e}")
    names = sorted(c.name for c in jar)
    if "sessionid" not in names:
        sys.exit(f"No sessionid cookie in {cookies}; re-export it while logged in.")
    log.info("loaded %d cookie(s) from %s: %s", len(names), cookies, ", ".join(names))
    expiries = [c.expires for c in jar if c.name == "sessionid" and c.expires]
    if expiries:
        log.info("sessionid expires %s", stamp(min(expiries)))
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


def item_shortcode(raw: object) -> str | None:
    """The shortcode of one feed item, whether or not it is wrapped in a "media" object."""
    if not isinstance(raw, dict):
        return None
    fields = cast("dict[str, object]", raw)
    inner = fields.get("media")
    if isinstance(inner, dict):
        fields = cast("dict[str, object]", inner)
    code = fields.get("code")
    return code if isinstance(code, str) else None


def iter_feed_shortcodes(
    opener: urllib.request.OpenerDirector, mode: str, limit: int
) -> Iterator[str]:
    """Yield shortcodes from the liked or saved feed, most recent action first.

    Neither feed carries a like/save timestamp, so ordering is the only recency signal.
    """
    url = FEEDS[mode]
    max_id: str | None = None
    seen = 0
    page_no = 0
    log.info("reading %s feed (up to %d most recent)", mode, limit)
    while seen < limit:
        params = {"count": "30"}
        if max_id:
            params["max_id"] = max_id
        page_no += 1
        page = api_get(opener, url, params)
        items = page.get("items")
        if not isinstance(items, list):
            log.warning("%s feed page %d had no items; stopping", mode, page_no)
            return
        entries = cast("list[object]", items)
        log.info("  page %d: %d item(s) (%d read so far)", page_no, len(entries), seen + len(entries))
        for raw in entries:
            code = item_shortcode(raw)
            if code is None:
                log.debug("  item without a shortcode, skipping")
                continue
            yield code
            seen += 1
            if seen >= limit:
                log.debug("reached --count %d, stopping after page %d", limit, page_no)
                return
        nxt = page.get("next_max_id")
        if not page.get("more_available") or not isinstance(nxt, str):
            log.info("  reached the end of the %s feed after %d item(s)", mode, seen)
            return
        max_id = nxt
        time.sleep(1)  # be gentle on a private endpoint


def load_state(out_dir: Path, mode: str) -> set[str]:
    path = out_dir / state_file(mode)
    if not path.is_file():
        log.debug("no state file at %s", path)
        return set()
    try:
        parsed = cast("object", json.loads(path.read_bytes()))
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable state file %s: %s", path, e)
        return set()
    if isinstance(parsed, dict):
        fields = cast("dict[str, object]", parsed)
        codes = fields.get("fetched")
        updated = fields.get("updated")
        if isinstance(codes, list):
            known = {c for c in cast("list[object]", codes) if isinstance(c, str)}
            when = f", last updated {stamp(updated)}" if isinstance(updated, int) else ""
            log.info("state (%s): %d post(s) already fetched%s", mode, len(known), when)
            return known
    log.warning("state file %s has an unexpected shape; treating it as empty", path)
    return set()


def save_state(out_dir: Path, codes: set[str], mode: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / state_file(mode)
    payload = {"fetched": sorted(codes), "updated": int(time.time())}
    try:
        _ = path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError as e:
        log.error("could not write state file %s: %s", path, e)
        return
    log.debug("state saved: %d post(s) recorded in %s", len(codes), path)


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
    entries = sorted(walk_likes(data), reverse=True)
    log.info("export holds %d like(s)", len(entries))
    if entries:
        log.info("  spanning %s to %s", stamp(entries[-1][0]), stamp(entries[0][0]))
    log.info("keeping likes newer than %s (--hours %g)", stamp(cutoff), hours)

    seen: set[str] = set()
    urls: list[str] = []
    skipped_old = skipped_nonpost = 0
    for ts, href in entries:
        if ts < cutoff:
            skipped_old += 1
            continue
        if not POST_URL.match(href):
            skipped_nonpost += 1
            log.debug("  not a post URL, skipping: %s", href)
            continue
        url = href.split("?")[0].rstrip("/") + "/"
        if url in seen:
            log.debug("  duplicate like on %s, already queued", url)
            continue
        seen.add(url)
        urls.append(url)
    log.info(
        "  %d in window, %d outside it, %d not post URLs, %d duplicate(s) collapsed",
        len(urls), skipped_old, skipped_nonpost, len(entries) - skipped_old - skipped_nonpost - len(urls),
    )
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
    verbose: bool = False
    quiet: bool = False


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Download Instagram posts, reels, and your recent likes.")
    subs = ap.add_subparsers(dest="mode", required=True, metavar="{post,liked,saved,export}")

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
    _ = common.add_argument("-v", "--verbose", action="store_true", default=Args.verbose,
                            help="log every file, request and skip decision")
    _ = common.add_argument("--quiet", action="store_true", default=Args.quiet,
                            help="only log warnings and errors")

    p = subs.add_parser("post", parents=[common], help="download specific post/reel URLs")
    _ = p.add_argument("urls", nargs="+", help="one or more Instagram post or reel URLs")

    # liked and saved differ only in which feed they read, so they take the same options
    for name, what in (("liked", "liked"), ("saved", "saved")):
        sub = subs.add_parser(
            name, parents=[common], help=f"download posts you {what} since the last run"
        )
        _ = sub.add_argument("--count", type=int, default=Args.count,
                             help=f"how many recent {what} posts to examine (default: 60)")
        _ = sub.add_argument("--again", action="store_true", default=Args.again,
                             help="ignore recorded state and re-fetch")

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
    if args.again:
        log.info("--again given: ignoring recorded state, every post found counts as new")
        already: set[str] = set()
    else:
        already = load_state(out_dir, args.mode)
        if not already:
            log.warning(
                "no previous state in %s; treating the %d most recent %s posts as new."
                + " Later runs will only pick up what changed since this one.",
                out_dir / state_file(args.mode), args.count, args.mode,
            )
    opener = open_session(Path(args.cookies))
    examined = list(iter_feed_shortcodes(opener, args.mode, args.count))
    fresh = [c for c in examined if c not in already]
    log.info(
        "examined %d %s post(s): %d already fetched, %d new",
        len(examined), args.mode, len(examined) - len(fresh), len(fresh),
    )
    return [post_url(c) for c in fresh], set(fresh)


def setup_logging(verbose: bool, quiet: bool) -> None:
    """Timestamped logging: a scheduled run's output needs to say when each step happened."""
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main() -> None:
    args = build_parser().parse_args(namespace=Args())
    setup_logging(args.verbose, args.quiet)
    started = time.monotonic()

    out_dir = Path(args.out)
    log.info("igdl %s mode -> %s", args.mode, out_dir.resolve())
    if args.mode == "export":
        log.info("source %s, window %g hour(s)", Path(args.source).resolve(), args.hours)
    else:
        log.info("cookies %s", Path(args.cookies).resolve())
    log.debug("full settings: %s", vars(args))

    cookies = Path(args.cookies)
    if args.mode != "export" and not cookies.is_file():
        sys.exit(f"Cookies file not found: {cookies}")

    urls, new_codes = collect_urls(args)
    if not urls:
        if args.mode in FEEDS:
            # Not an error: a scheduled hourly run finding nothing is the normal case,
            # and a non-zero exit would look like a failure to the scheduler.
            log.info(
                "nothing new: no %s posts since the last run (%.1fs)",
                args.mode, time.monotonic() - started,
            )
            return
        if args.mode == "export":
            sys.exit(
                f"No liked posts in the last {args.hours:g}h. The export is a snapshot:"
                + " likes made after you requested it are not in it."
            )
        sys.exit("Nothing to download.")

    log.info("%d post(s) queued:", len(urls))
    for url in urls:
        log.info("  %s", url)
    if args.dry_run:
        log.info("--dry-run: stopping without downloading")
        return

    if shutil.which("ffmpeg") is None:
        log.warning(
            "ffmpeg is not on PATH: reels will arrive as separate video and audio streams"
            + " instead of one playable file"
        )

    before = files_under(out_dir)
    log.info("output folder holds %d file(s) before this run", len(before))

    # Record progress after every post, not once at the end: a run interrupted part way
    # through then keeps what it already got instead of starting over next time. A post
    # that failed is left unrecorded, so the next run retries it.
    recorded: set[str] = load_state(out_dir, args.mode) if new_codes else set()
    failures = 0
    for i, url in enumerate(urls, 1):
        log.info("[%d/%d] %s", i, len(urls), url)
        if not fetch_one(url, out_dir, cookies):
            failures += 1
            continue
        code = shortcode_of(url)
        if code in new_codes:
            recorded.add(code)
            save_state(out_dir, recorded, args.mode)
            log.info("  recorded %s (%d post(s) in %s state)", code, len(recorded), args.mode)

    convert_webps(out_dir, args.quality, args.keep_webp)

    added = {p: n for p, n in files_under(out_dir).items() if p not in before}
    elapsed = time.monotonic() - started
    log.info(
        "done in %.1fs: %d of %d post(s) ok, %d new file(s), %s",
        elapsed, len(urls) - failures, len(urls), len(added), human_bytes(sum(added.values())),
    )
    if failures:
        log.warning("%d post(s) failed and stay unrecorded; the next run will retry them", failures)
    for p in sorted(added):
        log.info("  %s (%s)", p, human_bytes(added[p]))


if __name__ == "__main__":
    main()
