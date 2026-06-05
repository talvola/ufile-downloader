#!/usr/bin/env python3
"""
ufile_dl — Download and sync files from ufile.io folders or single files.

Usage:
    python ufile_dl.py goknh                          # folder by slug
    python ufile_dl.py https://ufile.io/f/goknh       # folder by URL
    python ufile_dl.py https://ufile.io/01hot8kn      # single file by URL
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

from browser_scraper import scrape_folder, scrape_file, download_files_via_browser
from downloader import find_orphans


def parse_ufile_url(input_str: str) -> tuple[str, str]:
    """
    Parse a ufile.io URL or bare slug.

    Returns (kind, slug) where kind is 'folder' or 'file':
      - ufile.io/f/{slug}  → ('folder', slug)
      - ufile.io/{slug}    → ('file', slug)
      - bare slug          → length-based guess: 8 chars → 'file', else 'folder'
                             (folder slugs observed at 5–6 chars, file slugs at 8)
    """
    m = re.search(r"ufile\.io/f/([A-Za-z0-9_-]+)", input_str)
    if m:
        return "folder", m.group(1)
    m = re.search(r"ufile\.io/([A-Za-z0-9_-]+)", input_str)
    if m:
        return "file", m.group(1)
    if re.match(r"^[A-Za-z0-9_-]+$", input_str):
        kind = "file" if len(input_str) >= 8 else "folder"
        return kind, input_str
    raise ValueError(f"Cannot parse ufile URL: {input_str}")


def main():
    parser = argparse.ArgumentParser(
        description="Download and sync files from ufile.io folders",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "target",
        help="Ufile folder slug/URL or single-file URL "
             "(e.g. goknh, https://ufile.io/f/goknh, https://ufile.io/01hot8kn)",
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

    kind, slug = parse_ufile_url(args.target)
    print(f"Target: {kind} — {slug}")

    # --- Phase 1: Scrape (headless) ---
    if kind == "folder":
        print("\nScraping folder listing...")
        try:
            folder_name, files = scrape_folder(slug)
            print(f"  Folder: {folder_name}")
            print(f"  Files found: {len(files)}")
        except Exception as e:
            print(f"  Browser scraping failed: {e}")
            sys.exit(1)

        if not files:
            print("No files found. The folder may be empty, removed, or expired.")
            sys.exit(1)

        if not folder_name or folder_name == slug or folder_name.isdigit():
            folder_name = slug
        output_dir = os.path.join(args.output, folder_name)
    else:  # single file
        print("\nScraping file page...")
        try:
            _, files = scrape_file(slug)
            if files:
                print(f"  File: {files[0]['name']}")
        except Exception as e:
            print(f"  Browser scraping failed: {e}")
            sys.exit(1)

        if not files:
            print("No file found. The file may be removed or expired.")
            sys.exit(1)

        # Single file goes directly into the output base dir, no subfolder
        output_dir = args.output

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

    # Only check for orphans in folder mode — in single-file mode we'd flag
    # every unrelated file in the output dir.
    should_report_orphans = kind == "folder"

    if not files_to_download:
        print("\nAll files already downloaded.")
        if should_report_orphans:
            _report_orphans(files, output_dir)
        return

    if args.dry_run:
        print(f"\nDRY RUN — would download {len(files_to_download)} file(s):")
        for f in files_to_download:
            size_str = _fmt_size(f.get("size"))
            print(f"  {f['name']} ({size_str})")
        if should_report_orphans:
            _report_orphans(files, output_dir)
        return

    # --- Phase 2: Download files via headed browser ---
    print(f"\nDownloading {len(files_to_download)} file(s)...")
    result = download_files_via_browser(files_to_download, output_dir)

    print(f"\nDone: {result['downloaded']} downloaded, {len(files_skipped)} skipped, {result['errors']} errors")

    if should_report_orphans:
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
