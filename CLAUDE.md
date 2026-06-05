# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI tool that downloads and syncs files from ufile.io folders and single-file pages. ufile.io gates downloads behind an ad-popup → modal → reCAPTCHA flow, so downloads cannot be done with plain HTTP — they require driving a real browser via Playwright.

## Commands

```bash
# Install deps (Playwright also needs its browser binary)
pip install -r requirements.txt
python -m playwright install chromium

# Run
python ufile_dl.py goknh                       # folder by bare slug
python ufile_dl.py https://ufile.io/f/goknh    # folder by URL
python ufile_dl.py https://ufile.io/01hot8kn   # single file by URL
python ufile_dl.py goknh --output ./downloads  # override output dir
python ufile_dl.py goknh --dry-run             # list what would download, no browser download

# The default output dir is /mnt/r/ufile (a WSL mount) — almost always override with --output.
```

There is no test suite, linter, or build step. Validate changes with `python -m py_compile *.py` and a `--dry-run` invocation.

## Architecture

Two phases, split by which browser mode they need:

1. **Scrape (headless)** — `browser_scraper.scrape_folder()` / `scrape_file()` load the page and return `(name, list_of_file_dicts)`. Both return the **same file-dict shape**: `{name, path, link, size, create_time}`. `link` is the individual file-page URL, not a direct download URL.
2. **Download (headed)** — `browser_scraper.download_files_via_browser()` opens a *visible* window and walks each file through the ufile.io download flow (see the docstring on that function for the click-by-click steps). reCAPTCHA requires a visible browser and may need manual solving per file.

`ufile_dl.py` is the orchestrator: parse target → scrape → diff against local files (skip when size matches) → download missing → report orphans.

### Key design constraints (don't "fix" these without understanding why)

- **Downloads must go through the headed browser, not `requests`.** The CDN URLs are session/IP-bound to the browser that solved the reCAPTCHA. This is why `download_files_via_browser` (in `browser_scraper.py`) exists and is what actually runs.
- **`downloader.py` is largely legacy.** Only `find_orphans()` is imported and used by `ufile_dl.py`. Its `download_files()` / `_download_file()` (direct-`requests` download path) are **not wired into the current flow** and don't work against ufile's gated CDN. Don't assume editing them changes behavior.
- **Persistent browser profile.** `download_files_via_browser` uses `launch_persistent_context` against `.browser_profile/` (gitignored) so reCAPTCHA trust accumulates across runs. Deleting it resets that trust.
- **`playwright_stealth` is optional.** Imported in a try/except; stealth patches are applied only if installed. Note it's not in `requirements.txt`.

### Folder vs. single-file mode (`parse_ufile_url`)

- `ufile.io/f/{slug}` → folder; downloads into `{output}/{folder_name}/`.
- `ufile.io/{slug}` → single file; downloads directly into `{output}/` (no subfolder).
- Bare slug → length heuristic: ≥8 chars guessed as a file, else folder (folder slugs observed at 5–6 chars, file slugs at 8). This is a guess; URLs are unambiguous.
- **Orphan reporting runs in folder mode only** — in single-file mode the output dir holds unrelated files that would all be flagged.

### Scraping is selector-heuristic and brittle by nature

ufile.io's markup isn't a stable API. The extractors (`_extract_file_list`, `_extract_folder_name`, `_extract_file_page_name`, `_extract_create_time`, `_parse_size`) try multiple selectors/regexes with fallbacks and swallow exceptions. When scraping breaks, it's usually because the site's DOM changed — `sample.html` (gitignored) is a captured folder page kept for reference when adjusting selectors.

## Conventions

- Targets Python 3.10+ (uses `X | None` / `tuple[...]` builtin generics).
- stdout/stderr are rewrapped to UTF-8 at the top of `ufile_dl.py` because Windows consoles default to cp1252 and choke on some filenames.
- `_fmt_size` and `_set_file_time` are duplicated across `browser_scraper.py` and `downloader.py` — keep them in sync if you touch one.
