#!/usr/bin/env python3
"""
ufile_dl — Download and sync files from ufile.io folders.

Usage:
    python ufile_dl.py goknh
    python ufile_dl.py https://ufile.io/f/goknh
    python ufile_dl.py goknh --output /mnt/r/ufile
    python ufile_dl.py goknh --dry-run
"""

import argparse
import io
import os
import re
import sys

# Ensure stdout/stderr handle Unicode on Windows (cp1252 can't print some chars)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from browser_scraper import scrape_folder, download_files_via_browser
from downloader import find_orphans


def parse_folder_slug(input_str: str) -> str:
    """Extract folder slug from a URL or bare slug string."""
    # Full URL: https://ufile.io/f/goknh
    m = re.search(r"ufile\.io/f/([A-Za-z0-9_-]+)", input_str)
    if m:
        return m.group(1)
    # Bare slug
    if re.match(r"^[A-Za-z0-9_-]+$", input_str):
        return input_str
    raise ValueError(f"Cannot parse folder slug from: {input_str}")


def main():
    parser = argparse.ArgumentParser(
        description="Download and sync files from ufile.io folders",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "folder",
        help="Ufile folder slug or URL (e.g. goknh or https://ufile.io/f/goknh)",
    )
    parser.add_argument(
        "-o", "--output",
        default="/mnt/r/ufile",
        help="Base output directory (default: /mnt/r/ufile)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without downloading",
    )
    args = parser.parse_args()

    folder_slug = parse_folder_slug(args.folder)
    print(f"Folder slug: {folder_slug}")

    # --- Phase 1: Scrape folder page (headless) ---
    print("\nScraping folder listing...")
    try:
        folder_name, files = scrape_folder(folder_slug)
        print(f"  Folder: {folder_name}")
        print(f"  Files found: {len(files)}")
    except Exception as e:
        print(f"  Browser scraping failed: {e}")
        sys.exit(1)

    if not files:
        print("No files found. The folder may be empty, removed, or expired.")
        sys.exit(1)

    # --- Determine output directory ---
    if not folder_name or folder_name == folder_slug or folder_name.isdigit():
        folder_name = folder_slug
    output_dir = os.path.join(args.output, folder_name)
    print(f"\nOutput directory: {output_dir}")
    os.makedirs(output_dir, exist_ok=True)

    # --- Check which files need downloading ---
    files_to_download = []
    files_skipped = []
    for f in files:
        local_path = os.path.join(output_dir, f["path"])
        size = f.get("size")
        if os.path.exists(local_path):
            local_size = os.path.getsize(local_path)
            if size is not None and local_size == size:
                files_skipped.append(f)
                continue
            elif size is None and local_size > 0:
                files_skipped.append(f)
                continue
        files_to_download.append(f)

    if files_skipped:
        print(f"\nSkipping {len(files_skipped)} already-downloaded file(s):")
        for f in files_skipped:
            print(f"  SKIP: {f['name']}")

    if not files_to_download:
        print("\nAll files already downloaded.")
        _report_orphans(files, output_dir)
        return

    if args.dry_run:
        print(f"\nDRY RUN — would download {len(files_to_download)} file(s):")
        for f in files_to_download:
            size_str = _fmt_size(f.get("size"))
            print(f"  {f['name']} ({size_str})")
        _report_orphans(files, output_dir)
        return

    # --- Phase 2: Download files via headed browser ---
    print(f"\nDownloading {len(files_to_download)} file(s)...")
    result = download_files_via_browser(files_to_download, output_dir)

    print(f"\nDone: {result['downloaded']} downloaded, {len(files_skipped)} skipped, {result['errors']} errors")

    # --- Check for orphaned local files ---
    _report_orphans(files, output_dir)


def _report_orphans(files: list[dict], output_dir: str):
    """Check for and report orphaned local files."""
    print("\nChecking for orphaned local files...")
    orphans = find_orphans(files, [], output_dir)
    if orphans:
        print(f"Found {len(orphans)} local file(s) not present on the server:")
        for o in orphans:
            print(f"  ORPHAN: {o}")
        print(f"\nThese files may have been deleted, moved, or renamed on the server.")
        print(f"They have NOT been deleted locally — review and remove manually if desired.")
    else:
        print("No orphaned files found — local copy matches server.")


def _fmt_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if isinstance(size, float) or size >= 100 else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


if __name__ == "__main__":
    main()
