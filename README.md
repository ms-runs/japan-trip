# Japan trip planner

A single-page trip planner: map of Japan, stops, travel legs, saved places from Google Maps, and a day-by-day playback. No build step and no libraries — `index.html` is the whole app.

| File | What it's for |
|---|---|
| `index.html` | The planner. Works on its own; saves in your browser. |
| `supabase-setup.sql` | One-time database setup so friends share one plan. |
| `maps-link-expander.js` | Optional. Lets short `maps.app.goo.gl` links work. |
| `verify.py` | Optional. Full automated test in a headless browser. |

## Put it online and share it (all free)

You need about 20 minutes, a GitHub account and a free Supabase account.

### 1. Host the page on GitHub Pages

1. On GitHub, create a new **public** repository, e.g. `japan-trip`. (Free Pages needs a public repo. Your plan data isn't stored in the repo, only the page.)
2. **Add file → Upload files**, add `index.html` (and this README if you like), commit.
3. **Settings → Pages →** Source: *Deploy from a branch*, Branch: `main`, folder `/ (root)` → Save.
4. After a minute your page is at `https://YOUR-USERNAME.github.io/japan-trip/`.

At this point it works, but each person's edits stay in their own browser.

### 2. Turn on shared saving with Supabase

1. Sign up at supabase.com and create a project (free plan; any region — Tokyo or Sydney are close).
2. Open **SQL Editor → New query**, paste all of `supabase-setup.sql`, press **Run**.
3. Open **Project Settings → API Keys** (older dashboards: **Settings → API**). Copy the **Project URL** and the **anon / publishable** key. This key is designed to be public — the database only answers to your trip code.
4. On GitHub, open `index.html`, click the pencil to edit, and fill in the settings at the top of the script:
   ```js
   const CONFIG = {
     supabaseUrl: 'https://abcdefgh.supabase.co',
     supabaseKey: 'sb_publishable_…',
     resolverUrl: '',
   };
   ```
   Commit.
5. Make up a long trip code (12+ characters, e.g. `lads-japan-2026-k7f3q`) and share this link with your two friends:
   ```
   https://YOUR-USERNAME.github.io/japan-trip/#trip=lads-japan-2026-k7f3q
   ```
   The first person to open it uploads their current plan; everyone after that loads the shared one.

**Treat the link like a password** — anyone with it can edit. The trip code is after `#`, so it isn't sent to GitHub or kept in server logs.

How syncing behaves: every change saves in your browser straight away and reaches the shared copy about a second later. Other people's changes arrive within 15 seconds, or as soon as you switch back to the tab. If two of you save in the same second, the first save wins and the other person sees a message asking them to redo their change — nothing is silently merged or corrupted. Edits made offline are kept and sent when you reconnect.

**Free-plan caveat:** Supabase pauses free projects after about a week with no activity. The plan isn't lost — open the Supabase dashboard and press **Restore**. While paused, the page keeps working from each browser's saved copy.

### 3. Optional: make short Google Maps links work

When you tap **Share** in the Google Maps app you get a short `maps.app.goo.gl` link, which hides the location. Browsers can't unwrap it, so without this step you get the place added but have to pick its stop yourself (or paste the long link from the browser address bar instead).

1. Sign up at cloudflare.com (free).
2. **Workers & Pages → Create → Create Worker** → *Hello World* → Deploy → **Edit code**.
3. Replace everything with `maps-link-expander.js` and **Deploy**.
4. Copy the worker's address (like `https://maps-links.YOUR-NAME.workers.dev`) into `resolverUrl` in `index.html`.

### 4. Using your Squarespace site

Three options, simplest first:

- **Link to it** from a page or your navigation. Simplest, and nothing to maintain.
- **Your own address**, e.g. `trip.migsousa.com`: in Squarespace **Domains → your domain → DNS**, add a `CNAME` record with host `trip` pointing to `YOUR-USERNAME.github.io`. Then in GitHub **Settings → Pages → Custom domain**, enter `trip.migsousa.com` and tick *Enforce HTTPS* once it's offered.
- **Embed it** in a page with an Embed or Code block (which one depends on your Squarespace plan):
  ```html
  <iframe src="https://YOUR-USERNAME.github.io/japan-trip/#trip=YOUR-CODE" style="width:100%;height:85vh;border:0"></iframe>
  ```
  Only do this on a password-protected page — the trip code would otherwise be visible to anyone viewing the page source.

## Everyday use

- **Add a place:** in Google Maps, Share → copy link, then paste it into *Add a place* (or anywhere on the page). It's filed under the nearest stop; you can change the stop and add a note. Notes show in the stop's panel and can be edited there.
- **New stop:** paste a Maps link for the town and choose *New stop at this place*. Use **Edit stops** to reorder, rename or remove.
- **Bookings:** click the travel line between two stops to add flight or train details. Lines on the map turn solid once something is booked.
- **Checks:** the header button runs the built-in checks (map placement, dates vs bookings, saving, sync). It turns amber or red if something needs a look.
- **Getting Claude to change it:** press **Copy plan** and paste the text into a chat with Claude, then ask for the change. You'll get back an updated `index.html` to upload over the old one. **Load plan** pastes plan text back in.

## Running the full test (optional)

```bash
pip install playwright && playwright install chromium
python verify.py
```

It drives the page in a headless browser — map placement, pasting links, saving and reloading, editing, playback, upgrading older saved plans, and two people syncing against a stand-in database — and saves three screenshots to review.
