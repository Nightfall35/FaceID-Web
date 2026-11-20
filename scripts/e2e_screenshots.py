"""Drive the real UI in headless Chromium: log in, enroll 2 people, identify photos, take screenshots.

    python scripts/e2e_screenshots.py --url http://localhost:8000 --password faceid
"""
import argparse
import glob
import os
import shutil

from playwright.sync_api import expect, sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8000")
ap.add_argument("--password", default="faceid")
ap.add_argument("--chrome", default=shutil.which("google-chrome") or shutil.which("chromium"))
a = ap.parse_args()
S = os.path.join(ROOT, "screenshots")
os.makedirs(S, exist_ok=True)
enroll = {n: sorted(glob.glob(os.path.join(ROOT, "samples/enroll", n + "_*.jpg"))) for n in ("Colin_Powell", "Serena_Williams")}

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=a.chrome, headless=True)
    pg = b.new_page(viewport={"width": 1360, "height": 900}, device_scale_factor=1)
    pg.goto(a.url)
    pg.screenshot(path=f"{S}/0_login.png")
    pg.fill("input[name=password]", a.password)
    pg.click("button[type=submit]")
    pg.wait_for_url(a.url + "/")
    pg.click("text=Enroll")
    for name, files in enroll.items():
        pretty = name.replace("_", " ")
        if pg.locator(".person", has_text=pretty).count() == 0:
            pg.fill("#new-person input[name=name]", pretty)
            pg.click("#new-person button")
            card = pg.locator(".person", has_text=pretty)
            expect(card).to_have_count(1)
            card.locator("input[type=file]").set_input_files(files)
            expect(pg.locator(".person", has_text=pretty).locator(".enroll-log .ok")).to_have_count(len(files), timeout=60000)
    pg.wait_for_timeout(500)
    pg.screenshot(path=f"{S}/1_enroll.png", full_page=True)

    pg.click("text=Identify")
    ids = sorted(glob.glob(os.path.join(ROOT, "samples/identify/*.jpg")))
    pg.set_input_files("#id-files", ids)
    expect(pg.locator(".result")).to_have_count(len(ids), timeout=60000)
    pg.wait_for_timeout(800)
    status = pg.inner_text("#id-status")
    rows = pg.locator(".result").all()
    report = []
    for r in rows:
        fn = r.locator("h3").inner_text()
        names = [n.inner_text() for n in r.locator(".face-item .nm").all()]
        report.append((fn, names))
    print("STATUS:", status)
    for fn, names in report:
        print(f"  {fn}: {names}")
    pg.screenshot(path=f"{S}/2_identify.png", full_page=True)
    pg.locator(".result", has_text="group_composite").screenshot(path=f"{S}/3_identify_group.png")

    pg.click("text=Settings")
    pg.wait_for_timeout(400)
    pg.screenshot(path=f"{S}/4_settings.png", full_page=True)

    m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    m.context.add_cookies(pg.context.cookies())
    m.goto(a.url + "/#enroll")
    m.wait_for_selector(".person")
    m.wait_for_timeout(500)
    m.screenshot(path=f"{S}/5_mobile_enroll.png")
    b.close()
