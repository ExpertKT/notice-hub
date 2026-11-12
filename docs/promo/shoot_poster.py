import sys
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "docs" / "promo" / "poster.html"
OUT = ROOT / "docs" / "promo"
OUT.mkdir(parents=True, exist_ok=True)

TARGET = {
    "poster-169": ("poster-16x9", (1920, 1080)),
    "poster-portrait": ("poster-portrait", (1080, 1920)),
}

only = sys.argv[1:] if len(sys.argv) > 1 else None

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 2000, "height": 2100}, device_scale_factor=2)
    page.goto(SRC.as_uri())
    page.wait_for_timeout(700)
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(400)
    for cid, (name, size) in TARGET.items():
        if only and cid not in only and name not in only:
            continue
        el = page.query_selector("#" + cid)
        raw = OUT / (cid + "-2x.png")
        el.screenshot(path=str(raw))
        im = Image.open(raw).convert("RGB")
        if im.size != size:
            im = im.resize(size, Image.LANCZOS)
        out = OUT / (name + ".png")
        im.save(out, "PNG", optimize=True)
        raw.unlink(missing_ok=True)
        print("WROTE", out.name, im.size)
    browser.close()
print("DONE")
