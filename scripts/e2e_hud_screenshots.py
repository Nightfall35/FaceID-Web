"""HUD UI end-to-end run in headless Chrome: login, enroll (upload + camera), identify (upload + camera),
settings, mobile, camera fallback. Writes PNGs to screenshots-hud/.

    python scripts/e2e_hud_screenshots.py --url http://localhost:8010 --password faceid --fake-video /tmp/hud-cam.y4m
"""
import argparse
import glob
import os
import shutil

from playwright.sync_api import expect, sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8010")
ap.add_argument("--password", default="faceid")
ap.add_argument("--chrome", default=shutil.which("google-chrome") or shutil.which("chromium"))
ap.add_argument("--fake-video", default="")
a = ap.parse_args()
S = os.path.join(ROOT, "screenshots-hud")
os.makedirs(S, exist_ok=True)
enroll = {n: sorted(glob.glob(os.path.join(ROOT, "samples/enroll", n + "_*.jpg"))) for n in ("Colin_Powell", "Serena_Williams")}
errors = []

args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
if a.fake_video:
    args.append(f"--use-file-for-fake-video-capture={a.fake_video}")

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=a.chrome, headless=True, args=args)
    ctx = b.new_context(viewport={"width": 1360, "height": 900}, device_scale_factor=1, permissions=["camera"])
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
    pg.goto(a.url)
    pg.wait_for_timeout(1200)
    pg.screenshot(path=f"{S}/01_login.png")
    pg.fill("input[name=password]", a.password)
    pg.press("input[name=password]", "Enter")
    pg.wait_for_url(a.url + "/", timeout=15000)
    pg.wait_for_timeout(600)
    sfx = pg.evaluate("typeof window.FaceSfx")
    print("FaceSfx:", sfx)

    pg.click(".tab[data-view=enroll]")
    for name, files in enroll.items():
        pretty = name.replace("_", " ")
        if pg.locator(".person", has_text=pretty).count() == 0:
            pg.fill("#new-person input[name=name]", pretty)
            pg.click("#new-person button")
            card = pg.locator(".person", has_text=pretty)
            expect(card).to_have_count(1)
            card.locator("input[type=file]").set_input_files(files[:3])
            expect(pg.locator(".person", has_text=pretty).locator(".enroll-log .ok")).to_have_count(3, timeout=60000)
    # camera enrollment: add a 4th photo for Colin via the webcam (fake video is a Colin Powell frame)
    pg.locator(".person", has_text="Colin Powell").locator(".cam-enroll").click()
    expect(pg.locator("#cam-snap")).to_be_enabled(timeout=10000)
    pg.wait_for_timeout(1500)
    pg.screenshot(path=f"{S}/05_camera_enroll_reticle.png")
    pg.click("#cam-snap")
    expect(pg.locator(".person", has_text="Colin Powell").locator(".enroll-log span")).not_to_have_count(0, timeout=60000)
    print("camera enroll log:", pg.locator(".person", has_text="Colin Powell").locator(".enroll-log").inner_text())
    # a person with 1 photo to show the incomplete ring
    if pg.locator(".person", has_text="Tony Blair").count() == 0:
        pg.fill("#new-person input[name=name]", "Tony Blair")
        pg.click("#new-person button")
        expect(pg.locator(".person", has_text="Tony Blair")).to_have_count(1)
        pg.locator(".person", has_text="Tony Blair").locator("input[type=file]").set_input_files(
            "/workspace/lfw_cache/lfw/Tony_Blair/Tony_Blair_0002.jpg")
        expect(pg.locator(".person", has_text="Tony Blair").locator(".enroll-log span")).not_to_have_count(0, timeout=60000)
    pg.wait_for_timeout(1500)
    pg.screenshot(path=f"{S}/04_enroll.png", full_page=True)

    pg.click(".tab[data-view=identify]")
    ids = sorted(glob.glob(os.path.join(ROOT, "samples/identify/*.jpg")))
    pg.set_input_files("#id-files", ids)
    pg.wait_for_timeout(450)
    pg.screenshot(path=f"{S}/02_identify_scanning.png")
    expect(pg.locator(".result")).to_have_count(len(ids), timeout=60000)
    pg.wait_for_timeout(2200)
    print("STATUS:", pg.inner_text("#id-status"))
    for r in pg.locator(".result").all():
        fn = r.locator("h3").inner_text()
        names = [n.inner_text() for n in r.locator(".face-item .nm").all()]
        print(f"  {fn}: {names}")
    print("verdict:", pg.inner_text("#status-title"), "|", pg.inner_text("#status-sub").replace("\n", " "), "|", pg.inner_text("#conf-val"))
    pg.evaluate("scrollTo(0,0)")
    pg.screenshot(path=f"{S}/03_identify_live_verdict.png")
    pg.screenshot(path=f"{S}/03a_identify_results_full.png", full_page=True)
    pg.locator(".result", has_text="group_composite").screenshot(path=f"{S}/03b_identify_group.png")
    pg.locator(".result", has_text="Tony_Blair").screenshot(path=f"{S}/03c_identify_unknown.png")
    # hover tilt
    card = pg.locator(".result").first
    card.scroll_into_view_if_needed()
    bb = card.bounding_box()
    pg.mouse.move(bb["x"] + 10, bb["y"] + 10)
    pg.mouse.move(bb["x"] + bb["width"] * 0.9, bb["y"] + bb["height"] * 0.15, steps=6)
    pg.wait_for_timeout(300)
    print("tilt transform:", card.evaluate("e => getComputedStyle(e).transform"),
          "| hover+fine-pointer media:", pg.evaluate("matchMedia('(hover: hover) and (pointer: fine)').matches"))
    pg.mouse.move(5, 5)

    # camera identify (inline on the stage)
    pg.evaluate("scrollTo(0,0)")
    pg.click("#id-cam")
    expect(pg.locator("#stage-snap")).to_be_visible(timeout=10000)
    pg.wait_for_timeout(1500)
    pg.screenshot(path=f"{S}/06_camera_identify_live.png")
    pg.click("#stage-snap")
    expect(pg.locator(".result")).to_have_count(1, timeout=60000)
    pg.wait_for_timeout(2000)
    print("camera identify:", [n.inner_text() for n in pg.locator(".face-item .nm").all()])
    print("verdict:", pg.inner_text("#status-title"), "|", pg.inner_text("#conf-val"))
    pg.screenshot(path=f"{S}/07_camera_identify_result.png")

    pg.click(".tab[data-view=settings]")
    pg.wait_for_timeout(900)
    pg.screenshot(path=f"{S}/08_settings.png", full_page=True)
    # toast + mute toggle
    pg.click("#sfx-toggle")
    print("muted after toggle:", pg.evaluate("window.FaceSfx && window.FaceSfx.muted"), pg.inner_text("#sfx-toggle"))
    pg.click("#sfx-toggle")
    pg.click("#save-thr")
    pg.wait_for_timeout(500)
    pg.screenshot(path=f"{S}/08b_settings_toast.png")

    # mobile
    m = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
                      storage_state=ctx.storage_state())
    mp = m.new_page()
    mp.goto(a.url + "/#identify")
    mp.set_input_files("#id-files", [os.path.join(ROOT, "samples/identify/group_composite.jpg")])
    expect(mp.locator(".result")).to_have_count(1, timeout=60000)
    mp.wait_for_timeout(2000)
    mp.screenshot(path=f"{S}/09_mobile_identify.png", full_page=True)
    mp.goto(a.url + "/#enroll")
    mp.wait_for_timeout(1500)
    mp.screenshot(path=f"{S}/10_mobile_enroll.png", full_page=True)
    m.close()

    # camera fallback: permission denied
    d = b.new_context(viewport={"width": 1360, "height": 900}, storage_state=ctx.storage_state())
    dp = d.new_page()
    dp.add_init_script("navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('denied', 'NotAllowedError'));")
    dp.goto(a.url + "/#identify")
    dp.click("#id-cam")
    expect(dp.locator("#stage-msg")).to_be_visible(timeout=5000)
    print("fallback msg (stage):", dp.inner_text("#stage-msg").replace("\n", " "))
    dp.screenshot(path=f"{S}/11_camera_fallback.png")
    d.close()

    # hologram contract: stub window.FaceHologram and record the calls the app makes
    hctx = b.new_context(viewport={"width": 1360, "height": 900}, storage_state=ctx.storage_state())
    hp = hctx.new_page()
    hp.add_init_script("""window.__holo=[];window.FaceHologram={
      mount:(el)=>__holo.push(['mount', el && el.id]), setState:(s)=>__holo.push(['setState', s]),
      update:(o)=>__holo.push(['update', o]), setPeople:(p)=>__holo.push(['setPeople', p.length, p[0]]), destroy:()=>__holo.push(['destroy'])};""")
    hp.goto(a.url + "/#identify")
    hp.set_input_files("#id-files", [os.path.join(ROOT, "samples/identify/Serena_Williams_0020.jpg")])
    expect(hp.locator(".result")).to_have_count(1, timeout=60000)
    hp.wait_for_timeout(300)
    import json
    for c in hp.evaluate("window.__holo"):
        print("  holo:", json.dumps(c)[:200])
    hctx.close()

    # reduced motion
    r = b.new_context(viewport={"width": 1360, "height": 900}, reduced_motion="reduce", storage_state=ctx.storage_state())
    rp = r.new_page()
    rp.goto(a.url + "/#identify")
    rp.set_input_files("#id-files", [os.path.join(ROOT, "samples/identify/Colin_Powell_0020.jpg")])
    expect(rp.locator(".result")).to_have_count(1, timeout=60000)
    rp.wait_for_timeout(300)
    rp.screenshot(path=f"{S}/12_reduced_motion.png")
    r.close()
    b.close()

print("JS errors:", errors or "none")
