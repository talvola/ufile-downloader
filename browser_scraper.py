"""Playwright-based scraper for extracting file listings and download URLs from ufile.io."""

import os
import re
import shutil
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext
except ImportError:
    sync_playwright = None
    Page = None
    BrowserContext = None

try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None

# Persistent browser profile directory (stores cookies, localStorage, etc.
# across runs so reCAPTCHA builds up trust over time)
_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".browser_profile")


def scrape_folder(folder_slug: str) -> tuple[str, list[dict]]:
    """
    Use a headless browser to load ufile.io/f/{folder_slug} and extract
    the file listing.

    Returns (folder_name, list_of_file_dicts).
    Each file dict has: name, path, link (individual file page URL), size, create_time (may be None).
    """
    if sync_playwright is None:
        raise RuntimeError(
            "playwright is not installed. Install it with:\n"
            "  pip install playwright && python -m playwright install chromium"
        )

    url = f"https://ufile.io/f/{folder_slug}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.dismiss())

        print(f"  Loading folder page: {url}")
        page.goto(url, wait_until="networkidle", timeout=60000)

        # Wait for file listing to render
        _wait_for_content(page)

        # Extract folder name
        folder_name = _extract_folder_name(page, folder_slug)

        # Extract file listing
        files = _extract_file_list(page)

        browser.close()

    return folder_name, files


def scrape_file(file_slug: str) -> tuple[str, list[dict]]:
    """
    Use a headless browser to load ufile.io/{file_slug} and extract
    metadata for a single file.

    Returns (file_name, [one_file_dict]).
    The file dict mirrors what scrape_folder yields per file:
    name, path, link, size, create_time.
    """
    if sync_playwright is None:
        raise RuntimeError(
            "playwright is not installed. Install it with:\n"
            "  pip install playwright && python -m playwright install chromium"
        )

    url = f"https://ufile.io/{file_slug}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.dismiss())

        print(f"  Loading file page: {url}")
        page.goto(url, wait_until="networkidle", timeout=60000)

        name = _extract_file_page_name(page, file_slug)
        size = _extract_file_page_size(page)
        create_time = _extract_create_time(page)

        browser.close()

    return name, [{
        "name": name,
        "path": name,
        "link": url,
        "size": size,
        "create_time": create_time,
    }]


def download_files_via_browser(files: list[dict], output_dir: str) -> dict:
    """
    Open a visible (headed) browser and download each file through the
    ufile.io free download flow.

    The CDN download URLs are session/IP-bound to the browser, so we let
    Playwright handle the actual file download.

    The flow per file:
    1. Navigate to the file page
    2. Extract the file creation date from the page
    3. Click "Free Download (1MB/s)" — first click may open an ad popup
    4. Click again if needed — shows "Before your download starts..." modal
    5. Click "Slow speed (1MB/s) is fine" — triggers reCAPTCHA
    6. User solves reCAPTCHA if needed
    7. Browser downloads the file; we save it to the output directory
    8. Set file modification time to the server's creation date

    Returns dict with counts: downloaded, errors.
    """
    if sync_playwright is None:
        raise RuntimeError(
            "playwright is not installed. Install it with:\n"
            "  pip install playwright && python -m playwright install chromium"
        )

    to_download = [f for f in files if f.get("link")]
    if not to_download:
        return {"downloaded": 0, "errors": 0}

    print("  Opening browser window (reCAPTCHA requires a visible browser)...")
    print("  You may need to solve a CAPTCHA for each file.\n")

    downloaded = 0
    errors = 0

    with sync_playwright() as p:
        # Use a persistent browser context so cookies/reCAPTCHA trust
        # accumulates across runs
        os.makedirs(_PROFILE_DIR, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            user_data_dir=_PROFILE_DIR,
            headless=False,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        # Apply stealth patches to avoid automation detection
        if stealth_sync:
            stealth_sync(page)

        page.on("dialog", lambda dialog: dialog.dismiss())

        # Auto-close ad popup tabs
        def _close_popup(new_page):
            if new_page != page:
                try:
                    new_page.close()
                except Exception:
                    pass
        context.on("page", _close_popup)

        for i, f in enumerate(to_download, 1):
            file_url = f["link"]
            name = f["name"]
            local_path = os.path.join(output_dir, f["path"])

            print(f"  [{i}/{len(to_download)}] {name}")

            success, create_time = _download_single_file(page, file_url, local_path)
            if success:
                actual_size = os.path.getsize(local_path)
                print(f"    OK ({_fmt_size(actual_size)})", end="")
                # Update the file dict with the create_time we scraped
                if create_time:
                    f["create_time"] = create_time
                    _set_file_time(local_path, create_time)
                    print(" [date set]", end="")
                print()
                downloaded += 1
            else:
                print(f"    FAILED")
                errors += 1
                # Clean up partial/error files (small files are likely error pages)
                if os.path.exists(local_path) and os.path.getsize(local_path) < 1024:
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass

        context.close()

    return {"downloaded": downloaded, "errors": errors}


def _download_single_file(page: Page, file_page_url: str, local_path: str) -> tuple[bool, float | None]:
    """
    Navigate to a file page, walk through the download flow, and save the
    file to local_path. Returns (success, create_time_timestamp).
    """
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    create_time = None

    try:
        print(f"    Loading page...", end="", flush=True)
        page.goto(file_page_url, wait_until="networkidle", timeout=60000)

        # Extract file creation date from the page before starting download
        create_time = _extract_create_time(page)
        if create_time:
            dt = datetime.fromtimestamp(create_time)
            print(f"\r    File date: {dt.strftime('%Y-%m-%d %H:%M:%S')}", end="", flush=True)

        # Step 1: Click "Free Download" (first click often opens ad popup)
        btn = page.wait_for_selector("a.slow-download-button.free-download", timeout=10000)
        if not btn:
            print("\r    No download button found" + " " * 30)
            return False, create_time

        # Small natural delay + mouse movement to look more human
        page.wait_for_timeout(1000)
        box = btn.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.wait_for_timeout(500)

        print(f"\r    Clicking Free Download..." + " " * 30, end="", flush=True)
        btn.click()
        page.wait_for_timeout(3000)

        # Step 2: Click again to show the modal (first click was consumed by ad)
        offer_modal = page.query_selector(".download-offer")
        offer_visible = offer_modal and page.evaluate(
            "el => getComputedStyle(el).visibility === 'visible'", offer_modal
        )

        if not offer_visible:
            print(f"\r    Re-clicking Free Download..." + " " * 30, end="", flush=True)
            btn = page.query_selector("a.slow-download-button.free-download")
            if btn:
                btn.click(force=True)
                page.wait_for_timeout(2000)

        # Step 3: Click "Slow speed (1MB/s) is fine" in the modal
        slow_btn = page.query_selector(".download-offer a.download-button")
        if not slow_btn:
            print(f"\r    Slow speed button not found" + " " * 30)
            return False, create_time

        print(f"\r    Solve reCAPTCHA in the browser if needed..." + " " * 15, end="", flush=True)
        slow_btn.click(force=True)

        # Step 4: Wait for the download to start (reCAPTCHA must complete first).
        # The JS sets window.location.href to the CDN URL after reCAPTCHA,
        # which triggers a browser download event.
        with page.expect_download(timeout=180000) as download_info:
            pass  # wait for the download event triggered by reCAPTCHA callback

        download = download_info.value
        print(f"\r    Downloading..." + " " * 50, end="", flush=True)

        # .path() blocks until the download completes
        download_path = download.path()
        if download_path:
            shutil.move(str(download_path), local_path)
            return os.path.exists(local_path) and os.path.getsize(local_path) > 0, create_time

        print(f"\r    Download completed but no file returned" + " " * 30)
        return False, create_time

    except Exception as e:
        print(f"\r    Error: {e}" + " " * 40)
        return False, create_time


def _extract_create_time(page: Page) -> float | None:
    """
    Extract the 'File created' date from a ufile.io file page.

    The page shows text like: "File created: 4/13/2026, 1:13:26 PM"
    """
    try:
        text = page.inner_text("body")
        # Match "File created: M/D/YYYY, H:MM:SS AM/PM"
        m = re.search(
            r"File created:\s*(\d{1,2}/\d{1,2}/\d{4},?\s*\d{1,2}:\d{2}:\d{2}\s*[APap][Mm])",
            text,
        )
        if m:
            date_str = m.group(1)
            # Try parsing with comma separator
            for fmt in [
                "%m/%d/%Y, %I:%M:%S %p",
                "%m/%d/%Y %I:%M:%S %p",
            ]:
                try:
                    dt = datetime.strptime(date_str, fmt)
                    return dt.timestamp()
                except ValueError:
                    continue

        # Fallback: try ISO-style dates
        m = re.search(r"File created:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text)
        if m:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
            return dt.timestamp()

    except Exception:
        pass
    return None


def _extract_file_page_name(page: Page, fallback_slug: str) -> str:
    """Extract the filename shown on a single-file page."""
    # Strategy 1: data-filename attribute on the download button
    try:
        el = page.query_selector("a.slow-download-button.free-download, a[data-filename]")
        if el:
            fn = el.get_attribute("data-filename")
            if fn and fn.strip():
                return fn.strip()
    except Exception:
        pass

    # Strategy 2: common filename selectors on the page
    for sel in [
        ".file-name", ".filename", "h1.name", "h1.filename",
        ".name", "h1", "h2",
    ]:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            text = (el.inner_text() or "").split("\n")[0].strip()
            if text and "." in text and len(text) < 300 and not text.lower().startswith("ufile"):
                return text
        except Exception:
            continue

    # Strategy 3: page title, stripped of site suffix
    try:
        title = (page.title() or "").strip()
        for sep in [" - ", " | ", " — "]:
            if sep in title:
                title = title.split(sep)[0].strip()
        if title and "." in title and len(title) < 300:
            return title
    except Exception:
        pass

    return fallback_slug


def _extract_file_page_size(page: Page) -> int | None:
    """Extract the file size shown on a single-file page."""
    try:
        text = page.inner_text("body")
        return _parse_size(text)
    except Exception:
        return None


def _set_file_time(path: str, timestamp: float):
    """Set both atime and mtime on a file."""
    try:
        os.utime(path, (timestamp, timestamp))
    except OSError as e:
        print(f"    Warning: could not set date on {path}: {e}")


def _fmt_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if isinstance(size, float) or size >= 100 else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


# ── Folder page helpers ──────────────────────────────────────────────

def _wait_for_content(page: Page):
    """Wait for the file listing to render on a folder page."""
    selectors = [
        ".file-wrapper",
        ".folder",
        ".file",
        "[class*='file']",
        "table",
    ]
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=10000)
            return
        except Exception:
            continue
    page.wait_for_timeout(5000)


def _extract_folder_name(page: Page, folder_slug: str) -> str:
    """Try to get a meaningful folder name from the page."""
    try:
        for selector in ["h1", "h2", ".folder-name", "[class*='title']", ".heading"]:
            el = page.query_selector(selector)
            if el:
                text = el.inner_text().strip()
                if text and len(text) < 200 and not text.isdigit():
                    return text
    except Exception:
        pass
    return folder_slug


def _extract_file_list(page: Page) -> list[dict]:
    """Extract file information from the folder page DOM."""
    files = []
    try:
        items = page.evaluate("""() => {
            const results = [];

            // ufile.io folder pages have .file-wrapper elements
            const wrappers = document.querySelectorAll('.file-wrapper, .file');
            for (const wrapper of wrappers) {
                const linkEl = wrapper.querySelector('a[href]');
                if (!linkEl) continue;
                const href = linkEl.href;

                // Get the filename: try .name first, then the first text node
                // of the link, avoiding child elements that contain size/expiry
                const nameEl = wrapper.querySelector('.name');
                let name = '';
                if (nameEl) {
                    // .name element — take only direct text, first line
                    name = nameEl.childNodes[0]?.textContent?.trim() || nameEl.textContent.split('\\n')[0].trim();
                } else {
                    // Fallback: first line of the link text
                    name = linkEl.textContent.split('\\n')[0].trim();
                }

                const allText = wrapper.innerText || '';

                if (name && href) {
                    results.push({
                        name: name,
                        link: href,
                        info: '',
                        allText: allText,
                    });
                }
            }

            // Fallback: look for table rows
            if (results.length === 0) {
                const rows = document.querySelectorAll('tr, [class*="file-row"]');
                for (const row of rows) {
                    const link = row.querySelector('a[href]');
                    if (!link) continue;
                    const name = link.textContent.split('\\n')[0].trim();
                    const href = link.href;
                    const allText = row.innerText || '';
                    if (name && href && !href.includes('/f/')) {
                        results.push({
                            name: name,
                            link: href,
                            info: '',
                            allText: allText,
                        });
                    }
                }
            }

            // Fallback: any link that looks like a file page
            if (results.length === 0) {
                const links = document.querySelectorAll('a[href]');
                for (const link of links) {
                    const href = link.href;
                    const name = link.textContent.split('\\n')[0].trim();
                    if (name && href && href.match(/ufile\\.io\\/[a-z0-9]+$/i)
                        && !href.includes('/f/') && !href.includes('/terms')
                        && !href.includes('/privacy') && !href.includes('/faq')
                        && !href.includes('/pricing') && !href.includes('/business')
                        && name.length > 2) {
                        results.push({
                            name: name,
                            link: href,
                            info: '',
                            allText: link.closest('div, tr, li')?.innerText || '',
                        });
                    }
                }
            }

            return results;
        }""")

        seen = set()
        for item in items:
            name = item.get("name", "").strip()
            # Safety: take only the first line, strip trailing whitespace
            name = name.split("\n")[0].strip()
            link = item.get("link", "")
            if not name or name in seen:
                continue
            seen.add(name)

            all_text = item.get("allText", "") + " " + item.get("info", "")
            size = _parse_size(all_text)

            files.append({
                "name": name,
                "path": name,
                "link": link,
                "size": size,
                "create_time": None,
            })

    except Exception as e:
        print(f"  File list extraction error: {e}")

    return files


def _parse_size(text: str) -> int | None:
    """Try to parse a file size from text."""
    size_re = re.compile(r"([\d.]+)\s*(KB|MB|GB|TB|B)\b", re.IGNORECASE)
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    m = size_re.search(text)
    if m:
        val = float(m.group(1))
        unit = m.group(2).upper()
        return int(val * multipliers.get(unit, 1))
    return None
