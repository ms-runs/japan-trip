// Optional helper: expands Google Maps short links (maps.app.goo.gl/…) so the
// planner can read the location. Browsers can't follow these redirects themselves.
//
// Setup (free): Cloudflare dashboard > Workers & Pages > Create > Worker >
// "Hello World" > Edit code > replace everything with this file > Deploy.
// Copy the worker's URL into CONFIG.resolverUrl in index.html.
//
// It only accepts Google Maps short links, so it can't be used as an open proxy.

export default {
  async fetch(request) {
    const cors = { 'Access-Control-Allow-Origin': '*' };
    const start = new URL(request.url).searchParams.get('url') || '';
    if (!/^https:\/\/(maps\.app\.goo\.gl|goo\.gl\/maps)\//.test(start)) {
      return new Response('Only Google Maps short links are accepted', { status: 400, headers: cors });
    }
    let url = start;
    for (let hop = 0; hop < 6; hop++) {
      const res = await fetch(url, { redirect: 'manual', headers: { 'User-Agent': 'Mozilla/5.0' } });
      const next = res.headers.get('location');
      if (!next) break;
      url = new URL(next, url).toString();
      const behindConsent = /consent\.google\./.test(url) && new URL(url).searchParams.get('continue');
      if (behindConsent) url = behindConsent;          // cookie-consent page wraps the real link
      if (/google\.[a-z.]+\/maps/.test(url)) break;     // reached the full Maps link
    }
    return new Response(url, { headers: cors });
  },
};
