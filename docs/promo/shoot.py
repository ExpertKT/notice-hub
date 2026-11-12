import sys
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "docs" / "promo" / "cards.html"
OUT = ROOT / "docs" / "promo" / "cards"
OUT.mkdir(parents=True, exist_ok=True)

NAMES = {
    "01": "01-cover",
    "02": "02-whitelist",
    "03": "03-inbox",
    "04": "04-tasks",
    "05": "05-finish-fix",
    "06": "06-remind",
    "07": "07-calendar",
    "08": "08-mobile",
    "09": "09-backfill",
}

only = sys.argv[1:] if len(sys.argv) > 1 else None

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1180, "height": 1200}, device_scale_factor=2)
    page.goto(SRC.as_uri())
    page.wait_for_timeout(700)
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(400)
    ids = page.eval_on_selector_all(".card", "els => els.map(e => e.id)")
    for cid in ids:
        num = cid.split("-")[1]
        if only and num not in only:
            continue
        el = page.query_selector("#" + cid)
        raw = OUT / (cid + "-2x.png")
        el.screenshot(path=str(raw))
        im = Image.open(raw).convert("RGB")
        if im.size != (1080, 1080):
            im = im.resize((1080, 1080), Image.LANCZOS)
        out = OUT / (NAMES.get(num, cid) + ".png")
        im.save(out, "PNG", optimize=True)
        raw.unlink(missing_ok=True)
        print("WROTE", out.name, im.size)
    browser.close()
print("DONE")
