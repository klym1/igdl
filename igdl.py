#!/usr/bin/env python3
"""
Download images/videos from an Instagram post or reel URL, then convert any .webp to .jpg.

Requirements:
    pip install -U gallery-dl yt-dlp pillow
    (ffmpeg on PATH is recommended for best-quality reels, but not required)

Usage:
    python igdl.py "https://www.instagram.com/p/SHORTCODE/"
    python igdl.py "https://www.instagram.com/reel/SHORTCODE/?utm_source=..." -o downloads -c cookies.txt
    python igdl.py URL --keep-webp
"""

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PIL import Image


def clean_url(url: str) -> str:
    """Strip query string/fragment (utm_source, igsh, stkn, ...) and validate it's a post or reel."""
    parts = urlsplit(url.strip())
    if "instagram.com" not in parts.netloc:
        sys.exit(f"Not an Instagram URL: {url}")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[-2] not in ("p", "reel", "reels", "tv"):
        sys.exit(f"Expected a post or reel URL (/p/..., /reel/...), got: {url}")
    path = f"/{segments[-2]}/{segments[-1]}/"
    return urlunsplit(("https", "www.instagram.com", path, "", ""))


def snapshot(folder: Path) -> set[Path]:
    return {p.resolve() for p in folder.rglob("*") if p.is_file()} if folder.exists() else set()


def download(url: str, out_dir: Path, cookies: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    before = snapshot(out_dir)

    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--cookies", str(cookies),
        "-D", str(out_dir),              # save straight into out_dir, no nested folders
        "-f", "{username}_{shortcode}_{num}.{extension}",
        url,
    ]
    # check=False: the non-zero case is handled below with a friendlier message
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        sys.exit(
            f"gallery-dl failed (exit code {result.returncode})."
            + " If it redirected to login, re-export cookies.txt."
        )

    new_files = sorted(snapshot(out_dir) - before)
    if not new_files:
        print("Nothing new downloaded (files may already exist in the output folder).")
    return new_files


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


class Args(argparse.Namespace):
    """Typed view of the parsed command line; the values here are the defaults."""

    url: str = ""
    out: str = "downloads"
    cookies: str = "cookies.txt"
    quality: int = 92
    keep_webp: bool = False


def main() -> None:
    ap = argparse.ArgumentParser(description="Download an Instagram post/reel and convert WebP to JPG.")
    _ = ap.add_argument("url", help="Instagram post or reel URL")
    _ = ap.add_argument("-o", "--out", help="output folder (default: downloads)")
    _ = ap.add_argument("-c", "--cookies", help="Netscape cookies.txt (default: cookies.txt)")
    _ = ap.add_argument("-q", "--quality", type=int, help="JPEG quality 1-95 (default: 92)")
    _ = ap.add_argument("--keep-webp", action="store_true", help="keep original .webp files")
    args = ap.parse_args(namespace=Args())

    cookies = Path(args.cookies)
    if not cookies.is_file():
        sys.exit(f"Cookies file not found: {cookies}")

    url = clean_url(args.url)
    out_dir = Path(args.out)
    print(f"Downloading {url} -> {out_dir.resolve()}")

    files = download(url, out_dir, cookies)

    # Convert new webps, plus any webps left over in the folder from earlier runs
    webps = {p for p in files if p.suffix.lower() == ".webp"} | {p.resolve() for p in out_dir.rglob("*.webp")}
    results = [p for p in files if p.suffix.lower() != ".webp"]
    for w in sorted(webps):
        try:
            jpg = webp_to_jpg(w, args.quality, args.keep_webp)
            print(f"Converted {w.name} -> {jpg.name}")
            results.append(jpg.resolve())
        except (OSError, ValueError) as e:
            # OSError covers UnidentifiedImageError and truncated/unreadable files;
            # one bad file should not abort the rest of the batch.
            print(f"Failed to convert {w}: {e}", file=sys.stderr)

    print("\nDone:")
    for p in sorted(set(results)):
        print(f"  {p}")


if __name__ == "__main__":
    main()
