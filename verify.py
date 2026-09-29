"""End-to-end verification for the Japan trip planner.

Runs the page in headless Chromium and checks: in-app checks, map placement,
Google Maps pasting, autosave + reload, migration of old saved plans, editing,
playback, zoom, and shared sync between two people (against a fake Supabase).

    pip install playwright && playwright install chromium
    python verify.py            # expects index.html next to this file
"""
import json, re, sys, threading, time, datetime, functools, http.server, pathlib
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
SRC = (HERE / "index.html").read_text()
PORT = 8765
results = []

def check(name, ok, detail=""):
    results.append((bool(ok), name, detail))
    print(("  PASS " if ok else "  FAIL ") + name + (f"  — {detail}" if detail else ""))

# A copy of the page with sharing switched on, pointed at a fake backend.
test_src = SRC.replace("supabaseUrl: '',", "supabaseUrl: 'https://fake.supabase.test',") \
              .replace("supabaseKey: '',", "supabaseKey: 'sb_publishable_test',") \
              .replace("resolverUrl: '',", "resolverUrl: 'https://fake-resolver.test',")
(HERE / "_shared_test.html").write_text(test_src)

server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(type("Quiet", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None}), directory=str(HERE)))
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

# ── fake Supabase: same conflict rule as the real SQL function ───────────────
store = {}
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "POST, OPTIONS"}
def fake_supabase(route):
    req = route.request
    if req.method == "OPTIONS":
        return route.fulfill(status=204, headers=CORS)
    body, fn = json.loads(req.post_data or "{}"), req.url.rsplit("/", 1)[-1]
    row = store.get(body.get("p_key"))
    if fn == "get_trip":
        out = [row] if row else []
    else:
        if row and (body["p_base"] is None or body["p_base"] != row["saved_at"]):
            out = [{"ok": False, **row}]
        else:
            time.sleep(0.002)
            row = {"trip": body["p_data"], "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            store[body["p_key"]] = row
            out = [{"ok": True, **row}]
    route.fulfill(status=200, headers={**CORS, "Content-Type": "application/json"}, body=json.dumps(out))
def fake_resolver(route):
    if route.request.method == "OPTIONS":
        return route.fulfill(status=204, headers=CORS)
    route.fulfill(status=200, headers=CORS, body="https://www.google.com/maps/place/Himeji+Castle/@34.839,134.69,17z/data=!3d34.8394!4d134.6939")

def new_page(ctx, errors):
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and "fonts.g" not in m.text and "ERR_" not in m.text and errors.append(m.text))
    pg.route("https://fonts.googleapis.com/**", lambda r: r.abort())
    pg.route("https://fonts.gstatic.com/**", lambda r: r.abort())
    pg.route("https://fake.supabase.test/**", fake_supabase)
    pg.route("https://fake-resolver.test/**", fake_resolver)
    return pg

def paste(pg, selector, text):
    pg.evaluate("""([sel, text]) => { const dt = new DataTransfer(); dt.setData('text/plain', text);
        document.querySelector(sel).dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""", [selector, text])

T = lambda pg, js, *a: pg.evaluate(js, *a)
HIMEJI = "https://www.google.com/maps/place/Himeji+Castle/@34.8394,134.6939,17z/data=!3m1!4b1!4m6!3m5!1s0x0:0x0!8m2!3d34.8394!4d134.6939"

with sync_playwright() as p:
    browser = p.chromium.launch()

    print("\n1. Loads cleanly and in-app checks pass")
    errors = []
    ctx = browser.new_context(viewport={"width": 1400, "height": 860})
    pg = new_page(ctx, errors); pg.goto(f"{BASE}/index.html"); pg.wait_for_timeout(400)
    R = T(pg, "runChecks()")
    for r in R:
        check(f"in-app: {r['name']}", r["state"] != "fail", f"{r['state']}: {r['detail']}")
    check("47 prefecture shapes drawn", T(pg, "document.querySelectorAll('#land path').length") == 47)
    check("one pin per stop", T(pg, "document.querySelectorAll('#pins > g[data-sid]').length") == T(pg, "trip.stops.length"))
    pins = T(pg, """[...document.querySelectorAll('#pins > g[data-sid] > circle:nth-of-type(2), #pins > g[data-sid] > circle:last-of-type')]
                     .filter(c => c.getAttribute('stroke') === '#fff').map(c => { const b = c.getBoundingClientRect(); return [b.x + b.width/2, b.y + b.height/2, b.width]; })""")
    overlap = [(i, j) for i in range(len(pins)) for j in range(i + 1, len(pins)) if ((pins[i][0]-pins[j][0])**2 + (pins[i][1]-pins[j][1])**2) ** .5 < pins[i][2] * .9]
    check("no two pins cover each other at the whole-trip view", not overlap, f"overlapping pairs {overlap}" if overlap else f"{len(pins)} pins")
    pg.screenshot(path=str(HERE / "shot-desktop.png"))

    print("\n2. Pasting a Google Maps link files the place under the nearest stop, with a note")
    paste(pg, "#paste-in", HIMEJI)
    pg.wait_for_selector("#place-dlg[open]")
    check("dialog opens with the place name", pg.input_value("#pl-name") == "Himeji Castle")
    check("nearest stop pre-selected (Osaka / Kobe)", pg.input_value("#pl-stop") == "osaka")
    pg.fill("#pl-note", "Day trip, go early"); pg.click("#place-dlg button[value=add]"); pg.wait_for_timeout(450)
    osaka = T(pg, "trip.stops.find(s => s.id === 'osaka').places")
    check("place saved to Osaka / Kobe with note and coordinates", len(osaka) == 1 and osaka[0]["note"] == "Day trip, go early" and abs(osaka[0]["lat"] - 34.8394) < 1e-4)
    check("stop opens and shows the note", pg.locator(".stop.open .pl-note").input_value() == "Day trip, go early")
    check("map zoomed in to the stop", T(pg, "ui.view[2]") < 400, f"view width {T(pg, 'ui.view[2]'):.0f}")
    check("place dot drawn on the map", T(pg, "document.querySelectorAll('#places circle').length") == 1)
    pg.screenshot(path=str(HERE / "shot-osaka.png"))

    paste(pg, "body", "Golden Gai\nhttps://maps.google.com/?q=35.6938,139.7046")
    pg.wait_for_selector("#place-dlg[open]")
    check("paste anywhere on the page also works", pg.input_value("#pl-name") == "Golden Gai")
    check("Tokyo tie goes to the longer Tokyo stay", pg.input_value("#pl-stop") == "tokyo2")
    pg.keyboard.press("Escape"); pg.wait_for_function("!document.querySelector('#place-dlg').open")
    check("Esc cancels without adding", T(pg, "trip.stops.find(s => s.id === 'tokyo2').places.length") == 0)

    paste(pg, "#paste-in", "https://maps.app.goo.gl/28QK9YA48XZBVTBr9")
    pg.wait_for_selector("#place-dlg[open]")
    check("short link without an expander explains what to do", "Short share links" in pg.inner_text("#place-info"))
    pg.keyboard.press("Escape")
    pg.wait_for_function("!document.querySelector('#place-dlg').open")

    paste(pg, "#paste-in", "Nikko https://www.google.com/maps/@36.7500,139.6000,13z")
    pg.wait_for_selector("#place-dlg[open]")
    pg.wait_for_function("document.querySelector('#pl-name').value === 'Nikko'")
    pg.select_option("#pl-stop", "__new"); pg.click("#place-dlg button[value=add]"); pg.wait_for_timeout(450)
    st = T(pg, "trip.stops.map(s => [s.name, s.prefIds.join(), s.region])")
    check("'New stop at this place' inserts it before the last stop, in the right prefecture", st[-2][0] == "Nikko" and st[-2][1] == "JP09", str(st[-2]))

    print("\n3. Changes save as they're made and survive a reload")
    pg.locator('.stop[data-sid=sapporo] [data-action=nights][data-d="1"]').click()
    pg.locator('.stop[data-sid=sapporo] .stop-head').click()
    pg.locator('.stop[data-sid=sapporo] textarea').press_sequentially("Book Nijo market breakfast")
    pg.locator('[data-action=toggle-leg][data-id=sapporo]').click()
    pg.locator('input[data-bind="stops.1.leg.number"]').press_sequentially("NH 55")   # never blurred
    pg.fill("#start-date", "2026-11-14")
    pg.reload(); pg.wait_for_timeout(400)
    s = T(pg, "trip.stops.find(s => s.id === 'sapporo')")
    check("nights change kept", s["nights"] == 2)
    check("note typed without leaving the box kept", s["notes"] == "Book Nijo market breakfast")
    check("flight number typed without leaving the box kept", s["leg"]["number"] == "NH 55")
    check("start date kept, dates shown in header", "14 Nov" in pg.inner_text("#trip-summary") or "Nov 14" in pg.inner_text("#trip-summary"), pg.inner_text("#trip-summary"))
    check("saved places kept", len(T(pg, "trip.stops.find(s => s.id === 'osaka').places")) == 1)
    check("booked leg marked as booked", "Booked" in pg.locator('[data-action=toggle-leg][data-id=sapporo]').inner_text())

    print("\n4. Editing stops")
    pg.click("#edit-btn")
    pg.locator('.stop[data-sid=sapporo] [data-action=move][data-d="-1"]').click()
    order = T(pg, "trip.stops.map(s => s.id)")
    check("move up works", order[0] == "sapporo")
    check("first stop never has an incoming leg; the old first gains one", "leg" not in T(pg, "trip.stops[0]") and T(pg, "Boolean(trip.stops[1].leg)"))
    pg.locator('.stop[data-sid=sapporo] [data-action=move][data-d="1"]').click()
    pg.once("dialog", lambda d: d.accept())
    pg.locator('.stop[data-sid] [data-action=delete-stop]').nth(T(pg, "trip.stops.findIndex(s => s.name === 'Nikko')")).click()
    check("delete stop works", "Nikko" not in T(pg, "trip.stops.map(s => s.name)"))
    pg.click("#edit-btn")
    R = T(pg, "runChecks()")
    check("in-app checks still clean after edits", not [r for r in R if r["state"] == "fail"], "; ".join(f"{r['name']}: {r['detail']}" for r in R if r["state"] != "pass"))

    print("\n5. Playback, copy/load")
    pg.click("[data-action=step][data-d='1']"); pg.click("[data-action=step][data-d='1']")
    check("day marker shown and label reads day 2", T(pg, "document.querySelectorAll('#pins .bob').length") == 1 and "Day 2" in pg.inner_text("#day-label"), pg.inner_text("#day-label"))
    exported = T(pg, "JSON.stringify(trip)")
    T(pg, "trip.stops[2].nights = 9; save(); renderAll()")
    pg.click("[data-action=import]"); pg.fill("#import-text", exported); pg.click("#import-dlg button[value=load]"); pg.wait_for_timeout(200)
    check("loading copied plan text restores it", T(pg, "trip.stops[2].nights") != 9)
    check("no script errors so far", not errors, "; ".join(errors[:3]))
    ctx.close()

    print("\n6. Older saved plans (previous version's format) are upgraded")
    errors = []
    ctx = browser.new_context(); pg = new_page(ctx, errors)
    pg.goto(f"{BASE}/index.html")
    old = {"startDate": None, "stops": [
        {"id": "tokyo1", "name": "Tokyo", "region": "Kantō", "lat": 35.6895, "lng": 139.6917, "color": "#be3a2a", "nights": 1, "prefIds": ["JP13"],
         "tabs": [{"id": "a", "label": "Essentials", "color": "#be3a2a", "items": [{"text": "Hotel", "tags": []}]}]},
        {"id": "ski", "name": "Ski", "region": "Nagano", "lat": 36.69, "lng": 137.85, "nights": 3, "prefIds": ["JP20"], "skiPicker": True, "skiOptions": [],
         "leg": {"mode": "flight", "est": "4h"}}]}
    T(pg, "o => { localStorage.clear(); localStorage.setItem('japanTripPlanV1', JSON.stringify(o)); }", old)
    pg.reload(); pg.wait_for_timeout(300)
    t = T(pg, "trip")
    check("old plan loads with tabs turned into sections", t["stops"][0]["sections"][0]["label"] == "Essentials" and "tabs" not in t["stops"][0])
    check("old ski picker fields removed, missing leg fields filled", "skiPicker" not in t["stops"][1] and t["stops"][1]["leg"]["carrier"] == "")
    check("no script errors", not errors, "; ".join(errors[:3]))
    ctx.close()

    print("\n7. Sharing: two people editing the same plan")
    errA, errB = [], []
    ctxA, ctxB = browser.new_context(), browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    A = new_page(ctxA, errA); A.goto(f"{BASE}/_shared_test.html#trip=lads-trip-2026-okinawa"); A.wait_for_timeout(700)
    check("first person's plan uploaded to the shared copy", "lads-trip-2026-okinawa" in store)
    check("status shows synced", "synced" in A.inner_text("#status").lower(), A.inner_text("#status"))
    B = new_page(ctxB, errB); B.goto(f"{BASE}/_shared_test.html#trip=lads-trip-2026-okinawa"); B.wait_for_timeout(700)
    A.locator('.stop[data-sid=kyoto] [data-action=nights][data-d="1"]').click(); A.wait_for_timeout(1000)
    check("A's change reaches the shared copy", store["lads-trip-2026-okinawa"]["trip"]["stops"][5]["nights"] == 3)
    T(B, "poll()"); B.wait_for_timeout(400)
    check("B picks up A's change", T(B, "trip.stops[5].nights") == 3)
    # both edit before either hears from the other
    A.locator('.stop[data-sid=nara] [data-action=nights][data-d="1"]').click(); A.wait_for_timeout(1000)
    B.locator('.stop[data-sid=hiroshima] [data-action=nights][data-d="1"]').click(); B.wait_for_timeout(1200)
    shared = store["lads-trip-2026-okinawa"]["trip"]["stops"]
    check("simultaneous edit: first save wins, nothing silently merged into a broken state", shared[6]["nights"] == 3 and T(B, "trip.stops[6].nights") == 3)
    check("the person whose save lost is told", "same moment" in B.inner_text("#toast"), B.inner_text("#toast"))
    paste(B, "#paste-in", "https://maps.app.goo.gl/abc123")
    B.wait_for_selector("#place-dlg[open]")
    check("short links are expanded when the link expander is set up", B.input_value("#pl-name") == "Himeji Castle" and B.input_value("#pl-stop") == "osaka")
    B.keyboard.press("Escape")
    R = T(B, "runChecks()")
    check("in-app checks pass on the shared, mobile copy", not [r for r in R if r["state"] == "fail"], "; ".join(f"{r['name']}: {r['detail']}" for r in R if r["state"] == "fail"))
    B.screenshot(path=str(HERE / "shot-mobile.png"))
    check("no script errors for either person", not (errA or errB), "; ".join((errA + errB)[:3]))
    browser.close()

server.shutdown()
(HERE / "_shared_test.html").unlink()
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)} of {len(results)} checks passed.")
sys.exit(1 if failed else 0)
