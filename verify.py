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
def with_config(src, url, key, resolver):
    src = re.sub(r"supabaseUrl: '[^']*',", f"supabaseUrl: '{url}',", src)
    src = re.sub(r"supabaseKey: '[^']*',", f"supabaseKey: '{key}',", src)
    return re.sub(r"resolverUrl: '[^']*',", f"resolverUrl: '{resolver}',", src)
SRC = with_config(SRC, '', '', '')            # local tests never touch the real database
(HERE / "_local_test.html").write_text(SRC)
test_src = with_config(SRC, 'https://fake.supabase.test', 'sb_publishable_test', 'https://fake-resolver.test')
SEED_PLACES = [
    {"name": "Bar Benfiddich", "note": "Tiny whisky bar, book ahead", "lat": 35.6906, "lng": 139.6983, "url": "https://maps.google.com/?q=35.6906,139.6983"},
    {"name": "Kuromon Market", "note": "Street food", "lat": 34.6655, "lng": 135.5064, "url": ""},
    {"name": "Hotel Okinawa Beach", "note": "", "lat": 26.2124, "lng": 127.6809, "url": "", "stop": "okinawa"},
]
SEEDS_RE = re.compile(r"const SEEDS = \{id: '[^']*', places: \[.*?\]\};", re.S)
assert SEEDS_RE.search(SRC), "SEEDS block not found"
(HERE / "_seed_test.html").write_text(SEEDS_RE.sub(lambda m: "const SEEDS = {id: 'gmaps-list-test', places: " + json.dumps(SEED_PLACES) + "};", SRC, count=1))
FULL_LIST_SRC = SRC
SRC = SEEDS_RE.sub("const SEEDS = {id: 'none', places: []};", SRC, count=1)   # other tests start without the list
(HERE / "_local_test.html").write_text(SRC)
(HERE / "_shared_test.html").write_text(test_src)

server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(type("Quiet", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None}), directory=str(HERE)))
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

# ── fake Supabase: same conflict rule as the real SQL function ───────────────
store = {}
files_store = {}
PDFJS_DIR = pathlib.Path(__import__("os").environ.get("PDFJS_DIR", "/home/claude/pdfjs/package/build"))
PDFS = HERE / "testpdfs"
HAVE_PDFS = (PDFS / "ANA-eticket.pdf").exists() and (PDFJS_DIR / "pdf.min.js").exists()   # test files and a local pdf.js; skipped if absent
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "POST, OPTIONS"}
def fake_supabase(route):
    req = route.request
    if req.method == "OPTIONS":
        return route.fulfill(status=204, headers=CORS)
    body, fn = json.loads(req.post_data or "{}"), req.url.rsplit("/", 1)[-1]
    row = store.get(body.get("p_key"))
    if fn == "get_trip":
        out = [row] if row else []
    elif fn == "save_file":
        out = bool(row)
        if row: files_store[(body["p_key"], body["p_id"])] = body
    elif fn == "get_file":
        f = files_store.get((body["p_key"], body["p_id"]))
        out = [{"name": f["p_name"], "type": f["p_type"], "data": f["p_data"]}] if f else []
    elif fn == "delete_file":
        out = files_store.pop((body["p_key"], body["p_id"]), None) is not None
    else:
        if row and ((row["trip"].get("tripId") != body["p_data"].get("tripId")) or body["p_base"] is None or body["p_base"] != row["saved_at"]):
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
    pg.route("https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/*", lambda r: r.fulfill(status=200, content_type="application/javascript",
             body=(PDFJS_DIR / r.request.url.rsplit("/", 1)[-1]).read_bytes()))
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
    pg = new_page(ctx, errors); pg.goto(f"{BASE}/_local_test.html"); pg.wait_for_timeout(400)
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
    check("map zoomed in to the stop", T(pg, "ui.view[2]") < 400, f"view width {T(pg, "ui.view[2]"):.0f}")
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
    check("'New stop at this place' inserts it before the last stop, in the right prefecture", st[-2][0] == "Nikko" and st[-2][1] == "JP09", str([x[0] for x in st]))

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
    pg.goto(f"{BASE}/_local_test.html")
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

    print("\n7. Categories, collapsing saved places, removing things")
    errors = []
    ctx = browser.new_context(viewport={"width": 1400, "height": 860}); pg = new_page(ctx, errors)
    pg.goto(f"{BASE}/_local_test.html"); T(pg, "localStorage.clear()"); pg.reload(); pg.wait_for_timeout(300)
    paste(pg, "#paste-in", "Diggers Bar https://www.google.com/maps/place/Diggers+Bar/@34.6697,135.5010,17z/data=!3d34.6697!4d135.5010")
    pg.wait_for_selector("#place-dlg[open]")
    check("category guessed from the name (Diggers Bar -> Bars)", pg.input_value("#pl-cat") == "drink")
    pg.fill("#pl-note", "Aussie bar, good for the rugby"); pg.click("#place-dlg button[value=add]"); pg.wait_for_timeout(300)
    paste(pg, "#paste-in", "https://www.google.com/maps/place/Ichiran+Dotonbori/@34.6687,135.5019,17z/data=!3d34.6687!4d135.5019")
    pg.wait_for_selector("#place-dlg[open]")
    check("brand name with no clue in it falls back to Other", pg.input_value("#pl-cat") == "other")
    pg.fill("#pl-note", "Famous ramen booths")
    check("typing a note updates the guess (ramen -> Restaurants)", pg.input_value("#pl-cat") == "eat")
    pg.click("#place-dlg button[value=add]"); pg.wait_for_timeout(300)
    heads = pg.locator(".stop.open h5").all_inner_texts()
    check("saved places grouped by category, in category order", heads == ["🍜 Restaurants", "🍸 Bars"], str(heads))
    pg.locator(".stop.open [data-action=toggle-places]").click()
    check("saved places collapse to one summary line", pg.locator(".stop.open .places").count() == 0 and "(2)" in pg.locator(".stop.open .pl-toggle").inner_text(), pg.locator(".stop.open .pl-toggle").inner_text())
    check("collapsing is a personal view setting, not saved into the shared plan", "openPlaces" not in T(pg, "JSON.stringify(trip)"))
    pg.reload(); pg.wait_for_timeout(300); pg.locator(".stop[data-sid=osaka] .stop-head").click()
    check("collapsed state remembered after reload", pg.locator(".stop.open .places").count() == 0)
    pg.locator(".stop.open [data-action=toggle-places]").click()
    pg.locator(".stop.open select[aria-label^='Category for Diggers']").select_option("eat"); pg.wait_for_timeout(200)
    check("changing a category re-sorts and saves", T(pg, "trip.stops.find(s => s.id === 'osaka').places.find(p => p.name.startsWith('Diggers')).cat") == "eat"
          and pg.locator(".stop.open h5").all_inner_texts() == ["🍜 Restaurants"])
    n = T(pg, "trip.stops.find(s => s.id === 'osaka').sections[0].items.length")
    pg.locator(".stop.open .things li .x").first.click()
    check("planned item can be removed", T(pg, "trip.stops.find(s => s.id === 'osaka').sections[0].items.length") == n - 1)
    pg.locator("#toast button").click(); pg.wait_for_timeout(150)
    check("undo brings the item back", T(pg, "trip.stops.find(s => s.id === 'osaka').sections[0].items.length") == n)
    pg.locator(".stop.open [data-action=delete-place]").first.click()
    check("saved place can be removed, with undo offered", T(pg, "trip.stops.find(s => s.id === 'osaka').places.length") == 1 and pg.locator("#toast button").count() == 1)
    pg.reload(); pg.wait_for_timeout(300)
    check("removal survives a reload", T(pg, "trip.stops.find(s => s.id === 'osaka').places.length") == 1)
    pg.click("#edit-btn"); pg.locator(".stop[data-sid=hiroshima] [data-action=delete-stop]").click()
    check("removing a stop needs no pop-up and can be undone", "hiroshima" not in T(pg, "trip.stops.map(s => s.id)"))
    pg.locator("#toast button").click(); pg.wait_for_timeout(150)
    check("undo restores the stop in place", T(pg, "trip.stops.map(s => s.id)")[3] == "hiroshima")
    R = T(pg, "runChecks()")
    check("in-app checks clean (incl. category checks)", not [r for r in R if r["state"] == "fail"], "; ".join(f"{r['name']}: {r['detail']}" for r in R if r["state"] == "fail"))
    ctx.close()

    print("\n8. Google Maps list import happens once")
    ctx = browser.new_context(); pg = new_page(ctx, errors)
    pg.goto(f"{BASE}/_seed_test.html"); T(pg, "localStorage.clear()"); pg.reload(); pg.wait_for_timeout(300)
    where = T(pg, "Object.fromEntries(trip.stops.flatMap(s => s.places.map(p => [p.name, [s.id, p.cat, p.note]])))")
    check("list places filed under their nearest stop", where.get("Bar Benfiddich", [""])[0] in ("tokyo1", "tokyo2") and where.get("Kuromon Market", [""])[0] == "osaka", str(where))
    check("an explicit stop overrides nearest", where.get("Hotel Okinawa Beach", [""])[0] == "okinawa")
    check("comments kept as notes, categories assigned", where["Bar Benfiddich"][1:] == ["drink", "Tiny whisky bar, book ahead"] and where["Kuromon Market"][1] == "eat" and where["Hotel Okinawa Beach"][1] == "stay", str(where))
    T(pg, "(() => { const s = trip.stops.find(s => s.id === 'osaka'); s.places = []; save(); })()")
    pg.reload(); pg.wait_for_timeout(300)
    check("a deleted list place doesn't come back on reload", T(pg, "trip.stops.find(s => s.id === 'osaka').places.length") == 0)
    check("list isn't added twice", T(pg, "trip.stops.flatMap(s => s.places).length") == 2)
    check("no script errors in sections 7-8", not errors, "; ".join(errors[:3]))
    ctx.close()

    print("\n9. The real Google Maps list")
    (HERE / "_list_test.html").write_text(FULL_LIST_SRC)
    expected = json.loads((HERE / "seeds.json").read_text()) if (HERE / "seeds.json").exists() else None
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}); pg = new_page(ctx, errors)
    pg.goto(f"{BASE}/_list_test.html"); T(pg, "localStorage.clear()"); pg.reload(); pg.wait_for_timeout(500)
    got = T(pg, "trip.stops.flatMap(s => s.places.map(p => ({name: p.name, stop: s.id, cat: p.cat, note: p.note, side: Boolean(p.side)})))")
    check("all 102 list places imported", len(got) == 102, str(len(got)))
    if expected:
        want = {(e["name"], e["stop"], e["cat"], e["note"], bool(e.get("side"))) for e in expected}
        have = {(g["name"], g["stop"], g["cat"], g["note"], g["side"]) for g in got}
        check("every place in the planned stop, category and note", want == have, f"{len(want ^ have)} differ")
    check("16 places tagged as side trips", pg.evaluate("trip.stops.flatMap(s => s.places).filter(p => p.side).length") == 16)
    R = T(pg, "runChecks()")
    check("in-app checks clean with the full list", not [r for r in R if r["state"] == "fail"], "; ".join(f"{r['name']}: {r['detail']}" for r in R if r["state"] != "pass"))
    pg.locator(".stop[data-sid=tokyo2] .stop-head").click(); pg.wait_for_timeout(500)
    check("Tokyo panel starts collapsed despite 70 places", pg.locator(".stop.open .places").count() == 0 and "(70)" in pg.locator(".stop.open .pl-toggle").inner_text())
    pg.locator(".stop.open [data-action=toggle-places]").click(); pg.wait_for_timeout(200)
    check("side-trip tag shows distance", "Side trip" in pg.locator(".stop.open").inner_text())
    pg.screenshot(path=str(HERE / "shot-tokyo-list.png"))
    pg.reload(); pg.wait_for_timeout(400)
    check("list not imported twice on reload", T(pg, "trip.stops.flatMap(s => s.places).length") == 102)
    check("no script errors with the full list", not errors, "; ".join(errors[:3]))
    ctx.close()

    print("\n10. Instagram ideas page")
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}); pg = new_page(ctx, errors)
    pg.goto(f"{BASE}/_list_test.html"); T(pg, "localStorage.clear()"); pg.reload(); pg.wait_for_timeout(500)
    pg.click("[data-action=page][data-v=ideas]"); pg.wait_for_timeout(200)
    check("ideas page opens and the map/plan is hidden", pg.locator("#ideas").is_visible() and not pg.locator(".app").is_visible())
    check("all 45 ideas shown", pg.locator(".idea").count() == 45, str(pg.locator(".idea").count()))
    already = pg.locator(".inplan").all_inner_texts()
    check("ideas already on the Google Maps list show 'In plan'", len(already) == 7, f"{len(already)} marked")
    pg.locator("[data-action=idea-filter][data-g=where][data-v=Tokyo]").click()
    check("filter by where (Tokyo)", pg.locator(".idea").count() == 15)
    pg.locator("[data-action=idea-filter][data-g=what][data-v=Bars]").click()
    check("filter by where + what (Tokyo bars)", pg.locator(".idea").count() == 4, str(pg.locator(".idea").count()))
    pg.locator("[data-action=idea-filter][data-g=where][data-v='']").click(); pg.locator("[data-action=idea-filter][data-g=what][data-v='']").click()
    pg.fill("#idea-q", "katsu sando")
    check("search finds the katsu sando post", pg.locator(".idea").count() == 1)
    pg.locator(".idea [data-action=idea-add]").first.click(); pg.wait_for_selector("#place-dlg[open]")
    check("+ opens the add dialog, prefilled for Tokyo / Restaurants with credit", pg.input_value("#pl-stop") == "tokyo2" and pg.input_value("#pl-cat") == "eat" and "@withkj_" in pg.input_value("#pl-note"),
          f"{pg.input_value('#pl-stop')} {pg.input_value('#pl-cat')} {pg.input_value('#pl-note')[:40]}")
    pg.click("#place-dlg button[value=add]"); pg.wait_for_timeout(300)
    p = T(pg, "trip.stops.find(s => s.id === 'tokyo2').places.find(p => p.name.startsWith('Tonkatsu Marushichi'))")
    check("added with the Instagram link and an approximate map position", p and "instagram.com" in p["url"] and p.get("approx") and p["lat"] > 35, str(p)[:120])
    check("the card now says 'In plan'", pg.locator(".idea .inplan").count() == 1 and pg.locator("#ideas").is_visible())
    pg.fill("#idea-q", "regional passes")
    pg.locator(".idea [data-action=idea-add]").first.click(); pg.wait_for_selector("#place-dlg[open]")
    check("a tip with no place defaults to the arrival stop", pg.input_value("#pl-stop") == "tokyo1")
    pg.keyboard.press("Escape"); pg.wait_for_function("!document.querySelector('#place-dlg').open"); pg.fill("#idea-q", "")
    before = pg.locator(".idea").count()
    pg.locator(".idea [data-action=idea-hide]").first.click(); pg.wait_for_timeout(150)
    check("hiding an idea removes it from the list, with undo", pg.locator(".idea").count() == before - 1 and pg.locator("#toast button").count() == 1)
    check("hidden ideas are shared (saved in the plan)", len(T(pg, "trip.hiddenIdeas")) == 1)
    pg.locator("[data-action=idea-filter][data-v=__hidden]").click()
    check("hidden ideas can be viewed and unhidden", pg.locator(".idea.is-hidden").count() == 1)
    pg.locator(".idea [data-action=idea-hide]").first.click(); pg.wait_for_timeout(150)
    check("unhide works", len(T(pg, "trip.hiddenIdeas")) == 0)
    check("after unhiding the last one, the list goes back to all ideas", pg.locator(".idea").count() == 45 and pg.locator("[data-action=idea-filter][data-g=what][data-v='']").get_attribute("aria-pressed") == "true")
    pg.reload(); pg.wait_for_timeout(400)
    check("reopens on the ideas page after reload", pg.locator("#ideas").is_visible())
    R = T(pg, "runChecks()")
    check("checks stay clean while on the ideas page", not [r for r in R if r["state"] == "fail"] and pg.get_attribute("#checks-btn", "data-state") != "fail",
          "; ".join(f"{r['name']}: {r['detail']}" for r in R if r["state"] == "fail")[:200])
    pg.screenshot(path=str(HERE / "shot-ideas.png"))
    pg.click("[data-action=page][data-v=plan]"); pg.wait_for_timeout(300)
    check("back to the plan: map drawn with pins", pg.locator(".app").is_visible() and T(pg, "document.querySelectorAll('#pins > g[data-sid]').length") == 9)
    R = T(pg, "runChecks()")
    check("in-app checks clean with ideas", not [r for r in R if r["state"] == "fail"], "; ".join(f"{r['name']}: {r['detail']}" for r in R if r["state"] == "fail"))
    mob = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True); mp = new_page(mob, errors)
    mp.goto(f"{BASE}/_list_test.html"); mp.wait_for_timeout(400); mp.click("[data-action=page][data-v=ideas]"); mp.wait_for_timeout(200)
    mp.screenshot(path=str(HERE / "shot-ideas-mobile.png"))
    check("no script errors on the ideas page", not errors, "; ".join(errors[:3]))
    mob.close(); ctx.close()

    if not HAVE_PDFS:
        print("\n11. Booking PDFs: skipped (needs testpdfs/ and a local pdf.js; set PDFJS_DIR)")
    else:
        print("\n11. Booking PDFs")
        ctx = browser.new_context(viewport={"width": 1400, "height": 900}); pg = new_page(ctx, errors)
        pg.goto(f"{BASE}/_local_test.html"); T(pg, "localStorage.clear()"); pg.reload(); pg.wait_for_timeout(400)
        pg.set_input_files("#bk-file", str(PDFS / "ANA-eticket.pdf")); pg.wait_for_selector("#bk-dlg[open]", timeout=15000)
        got = {k: pg.input_value(f"#bk-{k}") for k in ("kind", "provider", "ref", "names", "dates", "times", "number", "attach", "title")}
        check("e-ticket read: flight, ANA, K7QX2M, NH 55, 15 Nov", got["kind"] == "flight" and got["provider"] == "ANA" and got["ref"] == "K7QX2M" and got["number"] == "NH 55" and got["dates"] == "2026-11-15", str(got))
        check("passenger and times read", got["names"].startswith("SMITH/ALEX") and got["times"] == "07:30 → 09:05", str(got))
        check("HND → CTS flight attached to the Tokyo → Sapporo leg", got["attach"] == "leg:sapporo", got["attach"])
        pg.click("#bk-dlg button[value=save]"); pg.wait_for_timeout(500)
        L = T(pg, "trip.stops.find(s => s.id === 'sapporo').leg")
        check("the leg is filled in and shows as booked", L["confirmation"] == "K7QX2M" and L["number"] == "NH 55" and L["depTime"] == "07:30" and L["mode"] == "flight"
              and "Booked" in pg.locator("[data-action=toggle-leg][data-id=sapporo]").inner_text(), str(L)[:150])
        check("booking card shown on the leg with the reference", "K7QX2M" in pg.locator(".leg.open .bk").inner_text())
        T(pg, "window.__opened = null; window.open = () => ({close() {}, set location(u) { window.__opened = u; }})")   # headless Chrome can't display PDFs, so capture where the tab is sent
        pg.locator(".leg.open [data-action=bk-open]").click(); pg.wait_for_function("window.__opened", timeout=10000)
        check("Open PDF sends the tab to the stored file", T(pg, "fetch(window.__opened).then(r => r.blob()).then(b => b.type === 'application/pdf' && b.size === %d)" % (PDFS / "ANA-eticket.pdf").stat().st_size))
        check("Open in ANA link present", pg.locator(".leg.open .bk a", has_text="Open in ANA").count() == 1)
        pg.set_input_files("#bk-file", [str(PDFS / "booking-com-osaka.pdf"), str(PDFS / "smartex-hiroshima-osaka.pdf")])
        pg.wait_for_selector("#bk-dlg[open]", timeout=15000)
        got = {k: pg.input_value(f"#bk-{k}") for k in ("kind", "provider", "ref", "title", "dates", "price", "address", "attach", "names")}
        check("hotel confirmation read: Booking.com, stay, ref, both dates, price, address", got["kind"] == "stay" and got["provider"] == "Booking.com" and got["ref"] == "4123.567.890"
              and got["dates"] == "2026-11-20, 2026-11-24" and "98,400" in got["price"] and "Shinsaibashisuji" in got["address"] and got["title"] == "Cross Hotel Osaka" and got["names"] == "Alex Smith", str(got))
        check("hotel attached to the Osaka stop", got["attach"] == "stop:osaka", got["attach"])
        pg.click("#bk-dlg button[value=save]"); pg.wait_for_function("document.querySelector('#bk-dlg').open && document.querySelector('#bk-kind').value === 'train'", timeout=15000)
        got = {k: pg.input_value(f"#bk-{k}") for k in ("kind", "ref", "number", "attach", "dates")}
        check("second PDF in the batch opens next: shinkansen on the Hiroshima → Osaka leg, dated 19 Nov", got == {"kind": "train", "ref": "2468", "number": "Nozomi 32", "attach": "leg:osaka", "dates": "2026-11-19"}, str(got))
        pg.click("#bk-dlg button[value=save]"); pg.wait_for_timeout(500)
        L = T(pg, "trip.stops.find(s => s.id === 'osaka').leg")
        check("the train leg gets its date and times", (L["depDate"], L["depTime"], L["arrTime"]) == ("2026-11-19", "10:12", "11:40"), str((L["depDate"], L["depTime"], L["arrTime"])))
        check("three bookings saved", T(pg, "trip.bookings.length") == 3)
        if not pg.locator(".stop[data-sid=osaka].open").count(): pg.locator(".stop[data-sid=osaka] .stop-head").click(); pg.wait_for_timeout(300)
        check("hotel card on the stop, with Directions and Open in Booking.com", pg.locator(".stop.open .bk a", has_text="Directions").count() == 1 and pg.locator(".stop.open .bk a", has_text="Booking.com").count() == 1)
        pg.screenshot(path=str(HERE / "shot-bookings.png"))
        pg.set_input_files("#bk-file", str(PDFS / "scanned-ticket.pdf")); pg.wait_for_selector("#bk-dlg[open]", timeout=15000)
        check("a scan with no text says to fill it in by hand", "fill the details in by hand" in pg.inner_text("#bk-info"))
        pg.keyboard.press("Escape"); pg.wait_for_function("!document.querySelector('#bk-dlg').open")
        check("cancelling adds nothing", T(pg, "trip.bookings.length") == 3)
        b64 = __import__("base64").b64encode((PDFS / "ANA-eticket.pdf").read_bytes()).decode()
        dt = pg.evaluate_handle("""b64 => { const dt = new DataTransfer(); dt.items.add(new File([Uint8Array.from(atob(b64), c => c.charCodeAt(0))], 'dropped.pdf', {type: 'application/pdf'})); return dt; }""", b64)
        pg.dispatch_event("body", "drop", {"dataTransfer": dt}); pg.wait_for_selector("#bk-dlg[open]", timeout=15000)
        check("dropping a PDF on the page opens the booking box", pg.input_value("#bk-ref") == "K7QX2M")
        pg.keyboard.press("Escape"); pg.wait_for_function("!document.querySelector('#bk-dlg').open")
        pg.set_input_files("#bk-file", {"name": "photo.png", "mimeType": "image/png", "buffer": b"\x89PNG...."}); pg.wait_for_timeout(200)
        check("non-PDF files are turned away", "Only PDF" in pg.inner_text("#toast") and not T(pg, "document.querySelector('#bk-dlg').open"))
        pg.reload(); pg.wait_for_timeout(500)
        check("bookings survive a reload", T(pg, "trip.bookings.length") == 3)
        pg.locator(".stop[data-sid=osaka] .stop-head").click(); pg.wait_for_timeout(200)
        T(pg, "window.__opened = null; window.open = () => ({close() {}, set location(u) { window.__opened = u; }})")   # headless Chrome can't display PDFs, so capture where the tab is sent
        pg.locator(".stop.open [data-action=bk-open]").click(); pg.wait_for_function("window.__opened", timeout=10000)
        check("stored PDF still opens after reload, byte for byte", T(pg, "fetch(window.__opened).then(r => r.blob()).then(b => b.size === %d)" % (PDFS / "booking-com-osaka.pdf").stat().st_size))
        pg.locator(".stop.open [data-action=bk-edit]").click(); pg.wait_for_selector("#bk-dlg[open]")
        pg.fill("#bk-title", "Cross Hotel Osaka (twin room)"); pg.click("#bk-dlg button[value=save]"); pg.wait_for_timeout(300)
        check("editing updates the booking without duplicating it", T(pg, "trip.bookings.length") == 3 and "twin room" in pg.locator(".stop.open .bk").inner_text())
        fid = T(pg, "trip.bookings.find(b => b.kind === 'stay').fileId")
        pg.locator(".stop.open [data-action=bk-delete]").click()
        check("removing a booking offers undo", T(pg, "trip.bookings.length") == 2 and pg.locator("#toast button").count() == 1)
        pg.locator("#toast button").click(); pg.wait_for_timeout(200)
        check("undo restores it", T(pg, "trip.bookings.length") == 3)
        pg.locator(".stop.open [data-action=bk-delete]").click(); pg.wait_for_timeout(9800)
        check("once the undo window passes, the PDF itself is deleted", T(pg, "id => FILES.get(id).then(b => b === null)", fid))
        R = T(pg, "runChecks()")
        check("in-app checks clean with bookings", not [r for r in R if r["state"] == "fail"], "; ".join(f"{r['name']}: {r['detail']}" for r in R if r["state"] == "fail"))
        check("no script errors with bookings", not errors, "; ".join(errors[:3]))
        ctx.close()

    print("\n12. Sharing: two people editing the same plan")
    errA, errB = [], []
    ctxA, ctxB = browser.new_context(), browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    A = new_page(ctxA, errA); A.goto(f"{BASE}/_shared_test.html#trip=lads-trip-2026-okinawa"); A.wait_for_timeout(700)
    check("first person's plan uploaded to the shared copy", "lads-trip-2026-okinawa" in store)
    A.wait_for_function("document.querySelector('#status').textContent.toLowerCase().includes('synced')", timeout=8000)
    check("status shows synced", "synced" in A.inner_text("#status").lower(), A.inner_text("#status"))
    check("the list reached the shared copy, once", sum(len(s["places"]) for s in store["lads-trip-2026-okinawa"]["trip"]["stops"]) == 102)
    B = new_page(ctxB, errB); B.goto(f"{BASE}/_shared_test.html#trip=lads-trip-2026-okinawa"); B.wait_for_timeout(700)
    check("second person gets the list from the shared copy without re-importing", T(B, "trip.stops.flatMap(s => s.places).length") == 102 and T(B, "trip.imports").count("gmaps-list-japan") == 1)
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
    if HAVE_PDFS:
        A.set_input_files("#bk-file", str(PDFS / "ANA-eticket.pdf")); A.wait_for_selector("#bk-dlg[open]", timeout=15000)
        A.click("#bk-dlg button[value=save]"); A.wait_for_timeout(1500)
        check("a shared upload stores the PDF behind the trip code", len(files_store) == 1 and next(iter(files_store))[0] == "lads-trip-2026-okinawa")
        T(B, "poll()"); B.wait_for_timeout(500)
        check("the other person sees the booking", T(B, "trip.bookings.length") == 1)
        blob_ok = T(B, "FILES.get(trip.bookings[0].fileId).then(b => b && b.size > 1000 && b.type === 'application/pdf')")
        check("and can open the same PDF", blob_ok)
    R = T(B, "runChecks()")
    check("in-app checks pass on the shared, mobile copy", not [r for r in R if r["state"] == "fail"], "; ".join(f"{r['name']}: {r['detail']}" for r in R if r["state"] == "fail"))
    B.screenshot(path=str(HERE / "shot-mobile.png"))
    check("no script errors for either person", not (errA or errB), "; ".join((errA + errB)[:3]))

    print("\n13. Enter key, and share codes used by the multi-trip planner")
    errC = []
    C = new_page(browser.new_context(viewport={"width": 1400, "height": 900}), errC); C.goto(f"{BASE}/_local_test.html"); C.wait_for_timeout(400)
    C.fill("#paste-in", HIMEJI); C.press("#paste-in", "Enter"); C.wait_for_timeout(300)
    check("Enter in 'Add a place' opens the dialog and keeps it open", T(C, "$('#place-dlg').open"))
    C.keyboard.press("Escape")
    store["tetons-code-in-use-1"] = {"trip": {"tripId": "tetons-yellowstone", "version": 3, "stops": [{"id": "jackson", "name": "Jackson", "lat": 43.48, "lng": -110.76, "nights": 2}]},
                                     "saved_at": "2026-01-01T00:00:00+00:00"}
    before = json.dumps(store["tetons-code-in-use-1"])
    D = new_page(browser.new_context(viewport={"width": 1400, "height": 900}), errC); D.goto(f"{BASE}/_shared_test.html#trip=tetons-code-in-use-1"); D.wait_for_timeout(900)
    D.locator('.stop [data-action=nights][data-d="1"]').first.click(); D.wait_for_timeout(1300)
    check("another planner's plan isn't loaded", T(D, "trip.stops[0].id") != "jackson")
    check("another planner's plan isn't overwritten", json.dumps(store["tetons-code-in-use-1"]) == before)
    check("the page says why", "another trip" in D.inner_text("#status"), D.inner_text("#status"))
    check("no script errors", not errC, "; ".join(errC[:3]))
    browser.close()

server.shutdown()
for f in ["_shared_test.html", "_local_test.html", "_seed_test.html", "_list_test.html"]: (HERE / f).unlink()
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)} of {len(results)} checks passed.")
sys.exit(1 if failed else 0)
