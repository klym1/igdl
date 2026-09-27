# igdl

Download Instagram media you already have access to: specific posts and reels, the posts
you recently liked, or the posts you recently saved. WebP images are converted to JPEG on
the way in.

A single-file tool (`igdl.py`) wrapping [gallery-dl](https://github.com/mikf/gallery-dl)
for the downloading, with its own reader for the liked and saved feeds, which gallery-dl
does not cover.

## Install

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -U -r requirements.txt   # Windows
# .venv/bin/python -m pip install -U -r requirements.txt         # macOS/Linux
```

Install **ffmpeg** as well and put it on `PATH`. Without it, reels arrive as a separate
video stream and audio stream rather than one playable file — the tool warns when it is
missing, but cannot merge them itself.

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
```

## Authentication

Every mode except `export` needs your own logged-in session, as a Netscape-format
`cookies.txt` exported from a browser where you are signed in to Instagram. Save it next
to `igdl.py` or pass `-c /path/to/cookies.txt`.

**That file is a live credential.** It is in `.gitignore` and must stay out of version
control; anyone holding it can act as you on Instagram. The tool logs when the session
expires so you know when to re-export it.

## Usage

```bash
python igdl.py post "https://www.instagram.com/p/SHORTCODE/"   # specific URLs
python igdl.py liked                                           # new likes since last run
python igdl.py saved                                           # new saves since last run
python igdl.py export instagram-export.zip --hours 24          # from a data export
```

Shared options: `-o/--out` (default `downloads`), `-c/--cookies` (default `cookies.txt`),
`-q/--quality` for JPEG quality, `--keep-webp`, `--dry-run`, `-v/--verbose`, `--quiet`.
`liked` and `saved` also take `--count` (how many recent items to examine, default 60)
and `--again` (ignore recorded state and re-fetch).

### Why there is no `--hours` for likes and saves

Instagram's liked and saved feeds are ordered by when you liked or saved each post, but
they carry **no timestamp for that action** — the only time on an item is the post's own
creation time, which is unrelated, and the feeds are demonstrably not sorted by it. A
wall-clock window is therefore not available from these feeds.

Instead each mode records the shortcodes it has fetched and takes whatever is new:

| Mode | State file |
| --- | --- |
| `liked` | `<out>/.igdl-liked.json` |
| `saved` | `<out>/.igdl-saved.json` |

Run a mode on a schedule and each run covers the period since the previous one — hourly
gives you "liked in the last hour". State is written **after each post**, so an
interrupted run keeps what it already fetched, and a post whose download fails is left
unrecorded so the next run retries it.

The files are separate on purpose: a post can be both liked and saved, and a shared file
would let whichever mode ran first hide it from the other. Deleting a state file makes
the next run treat the `--count` most recent items as new again.

Hourly, on Windows:

```bash
schtasks /Create /TN igdl-liked /SC HOURLY /TR "C:\path\to\.venv\Scripts\python.exe C:\path\to\igdl.py liked -c C:\path\to\cookies.txt -o C:\path\to\downloads"
```

### The `export` mode

Instagram's own **Download your information** archive (request it as JSON, not HTML) is
the one source that *does* carry true like timestamps, in
`your_instagram_activity/likes/liked_posts.json` — so `--hours` means real wall-clock
there. Point the mode at the `.zip`, its extracted folder, or that JSON file directly.

The trade-off is freshness: an archive takes hours or days to generate and is already
stale when it arrives, so it suits backfilling history rather than catching recent
activity. There is no equivalent archive for saved posts.

## Notes

- Uses undocumented endpoints (`api/v1/feed/liked/`, `api/v1/feed/saved/posts/`) for the
  two feeds, because no public API lists them. They need a mobile app user agent — a
  browser one is rejected with `useragent mismatch`. Instagram can change these without
  notice, which would break `liked` and `saved` while `post` and `export` keep working.
- Only reaches content your own session can already see. Intended for personal archiving
  of your own activity; downloading other people's media may conflict with Instagram's
  terms and with the rights of whoever made it.
- Type-checked with [basedpyright](https://github.com/DetachHead/basedpyright) and linted
  with [ruff](https://github.com/astral-sh/ruff); both are clean and configured in
  `pyrightconfig.json` and `ruff.toml`.

## License

[MIT](LICENSE)
