"""Web dashboard: the browser-facing view of the same data the bot serves.

A small aiohttp app with JSON endpoints over the SQLite store (congressional
trades, flow events, aggregates) plus live per-ticker GEX and dark pool
lookups, and a single responsive HTML page that renders it all. Runs inside
the bot process when ENABLE_DASHBOARD=true.
"""

from __future__ import annotations

import io
import json
import logging

from aiohttp import web

from src.config import settings

log = logging.getLogger(__name__)

_MAX_LIMIT = 100

_MANIFEST = {
    "name": "Politician Trade Agent",
    "short_name": "TradeAgent",
    "description": "Congressional trades, options flow, GEX, and dark pool data",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0e1117",
    "theme_color": "#0e1117",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}

_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="12" fill="#0e1117"/>
<rect x="10" y="30" width="8" height="22" rx="2" fill="#2ecc71"/>
<rect x="22" y="20" width="8" height="32" rx="2" fill="#e74c3c"/>
<rect x="34" y="12" width="8" height="40" rx="2" fill="#2ecc71"/>
<rect x="46" y="24" width="8" height="28" rx="2" fill="#3498db"/>
</svg>"""

# Minimal network-first service worker: enough for installability, never
# serves stale market data.
_SERVICE_WORKER = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => e.respondWith(fetch(e.request)));
"""


def _render_icon_png(size: int) -> bytes | None:
    """Rasterize the app icon with Pillow (a pdfplumber dependency)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - Pillow ships with pdfplumber
        return None

    img = Image.new("RGBA", (size, size), "#0e1117")
    draw = ImageDraw.Draw(img)
    unit = size / 64
    bars = [
        (10, 30, "#2ecc71"),
        (22, 20, "#e74c3c"),
        (34, 12, "#2ecc71"),
        (46, 24, "#3498db"),
    ]
    for x, top, color in bars:
        draw.rounded_rectangle(
            [x * unit, top * unit, (x + 8) * unit, 52 * unit],
            radius=2 * unit,
            fill=color,
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class DashboardServer:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._runner: web.AppRunner | None = None
        self._icon_cache: dict[int, bytes | None] = {}

    def build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/manifest.webmanifest", self.manifest),
                web.get("/sw.js", self.service_worker),
                web.get("/icon.svg", self.icon_svg),
                web.get("/icon-{size:\\d+}.png", self.icon_png),
                web.get("/api/trades", self.api_trades),
                web.get("/api/flow", self.api_flow),
                web.get("/api/top", self.api_top),
                web.get("/api/active", self.api_active),
                web.get("/api/gex/{ticker}", self.api_gex),
                web.get("/api/darkpool/{ticker}", self.api_darkpool),
            ]
        )
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, settings.dashboard_host, settings.dashboard_port)
        await site.start()
        log.info(
            "Dashboard running at http://%s:%d",
            settings.dashboard_host,
            settings.dashboard_port,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # -- handlers ------------------------------------------------------------

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def manifest(self, request: web.Request) -> web.Response:
        return web.Response(
            text=json.dumps(_MANIFEST), content_type="application/manifest+json"
        )

    async def service_worker(self, request: web.Request) -> web.Response:
        return web.Response(text=_SERVICE_WORKER, content_type="application/javascript")

    async def icon_svg(self, request: web.Request) -> web.Response:
        return web.Response(text=_ICON_SVG, content_type="image/svg+xml")

    async def icon_png(self, request: web.Request) -> web.Response:
        size = min(max(int(request.match_info["size"]), 48), 1024)
        if size not in self._icon_cache:
            self._icon_cache[size] = _render_icon_png(size)
        data = self._icon_cache[size]
        if data is None:
            raise web.HTTPNotFound(text="PNG icons unavailable (Pillow not installed)")
        return web.Response(body=data, content_type="image/png")

    async def api_trades(self, request: web.Request) -> web.Response:
        from src.storage.database import get_recent_trades

        limit = _bounded_int(request.query.get("limit"), default=25)
        ticker = request.query.get("ticker") or None
        politician = request.query.get("politician") or None
        rows = await get_recent_trades(
            self.db_path, limit=limit, politician=politician, ticker=ticker
        )
        return web.json_response(rows)

    async def api_flow(self, request: web.Request) -> web.Response:
        from src.storage.database import get_recent_flow_events

        limit = _bounded_int(request.query.get("limit"), default=25)
        return web.json_response(await get_recent_flow_events(self.db_path, limit=limit))

    async def api_top(self, request: web.Request) -> web.Response:
        from src.storage.database import get_top_tickers

        days = _bounded_int(request.query.get("days"), default=30, maximum=365)
        return web.json_response(await get_top_tickers(self.db_path, days=days))

    async def api_active(self, request: web.Request) -> web.Response:
        from src.storage.database import get_most_active_politicians

        days = _bounded_int(request.query.get("days"), default=30, maximum=365)
        return web.json_response(
            await get_most_active_politicians(self.db_path, days=days)
        )

    async def api_gex(self, request: web.Request) -> web.Response:
        from src.analysis.gex import compute_gex
        from src.data.options import build_options_provider

        ticker = request.match_info["ticker"].upper()[:10]
        chain = await build_options_provider().fetch_chain(ticker)
        if chain is None or not chain.contracts or chain.spot <= 0:
            return web.json_response({"error": f"no chain for {ticker}"}, status=404)
        profile = compute_gex(chain)
        return web.json_response(
            {
                "ticker": profile.ticker,
                "spot": profile.spot,
                "net_gex": profile.net_gex,
                "call_gex": profile.call_gex,
                "put_gex": profile.put_gex,
                "zero_gamma": profile.zero_gamma_estimate,
                "top_strikes": [
                    {"strike": strike, "gex": exposure}
                    for strike, exposure in profile.top_strikes()
                ],
            }
        )

    async def api_darkpool(self, request: web.Request) -> web.Response:
        from src.data.darkpool import DarkPoolService

        ticker = request.match_info["ticker"].upper()[:10]
        service = DarkPoolService()
        short_days = await service.get_short_volume(ticker)
        ats = await service.get_ats_weekly(ticker)
        if not short_days and not ats:
            return web.json_response({"error": f"no data for {ticker}"}, status=404)
        return web.json_response(
            {
                "ticker": ticker,
                "short_volume": [
                    {
                        "day": d.day.isoformat(),
                        "short_ratio": d.short_ratio,
                        "short_volume": d.short_volume,
                        "total_volume": d.total_volume,
                    }
                    for d in short_days
                ],
                "ats_weekly": ats,
            }
        )


def _bounded_int(raw: str | None, *, default: int, maximum: int = _MAX_LIMIT) -> int:
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    return max(1, min(value, maximum))


_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0e1117">
<title>Politician Trade Agent</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon-180.png">
<style>
  :root { --bg:#0e1117; --panel:#171b24; --text:#dbe1ea; --dim:#8b94a3;
          --green:#2ecc71; --red:#e74c3c; --accent:#3498db; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--text);
         font:14px/1.5 system-ui,-apple-system,sans-serif; padding:16px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { color:var(--dim); margin-bottom:16px; font-size:12px; }
  .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); }
  .panel { background:var(--panel); border-radius:8px; padding:14px; overflow-x:auto; }
  .panel h2 { font-size:14px; margin-bottom:10px; color:var(--accent); }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th { text-align:left; color:var(--dim); font-weight:600; padding:3px 8px 3px 0; }
  td { padding:3px 8px 3px 0; border-top:1px solid #232a36; white-space:nowrap; }
  .buy,.bullish { color:var(--green); } .sell,.sale,.bearish { color:var(--red); }
  input,button { background:#0e1117; color:var(--text); border:1px solid #2a3242;
                 border-radius:6px; padding:6px 10px; font-size:13px; }
  button { cursor:pointer; border-color:var(--accent); }
  pre { font-size:12px; white-space:pre-wrap; color:var(--text); margin-top:8px; }
</style>
</head>
<body>
<h1>Politician Trade Agent</h1>
<div class="sub">Congressional trades &middot; options flow &middot; gamma exposure &middot; dark pool — auto-refreshes every 60s</div>
<div class="grid">
  <div class="panel"><h2>Congressional Trades</h2><div id="trades">loading…</div></div>
  <div class="panel"><h2>Unusual Options Flow</h2><div id="flow">loading…</div></div>
  <div class="panel"><h2>Top Tickers (30d)</h2><div id="top">loading…</div></div>
  <div class="panel"><h2>Most Active Politicians (30d)</h2><div id="active">loading…</div></div>
  <div class="panel">
    <h2>Ticker Tools</h2>
    <input id="tk" placeholder="SPY" size="8">
    <button onclick="lookup('gex')">GEX</button>
    <button onclick="lookup('darkpool')">Dark Pool</button>
    <pre id="tool"></pre>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const fmt$ = v => v >= 1e6 ? '$'+(v/1e6).toFixed(1)+'M' : v >= 1e3 ? '$'+(v/1e3).toFixed(0)+'K' : '$'+Math.round(v);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function table(rows, cols) {
  if (!rows.length) return '<span style="color:var(--dim)">no data yet</span>';
  const head = cols.map(c => '<th>'+esc(c.h)+'</th>').join('');
  const body = rows.map(r => '<tr>'+cols.map(c => '<td class="'+esc(c.cls?c.cls(r):'')+'">'+c.f(r)+'</td>').join('')+'</tr>').join('');
  return '<table><tr>'+head+'</tr>'+body+'</table>';
}
async function load() {
  try {
    const [trades, flow, top, active] = await Promise.all(
      ['/api/trades','/api/flow','/api/top','/api/active'].map(u => fetch(u).then(r => r.json())));
    $('trades').innerHTML = table(trades, [
      {h:'Politician', f:r=>esc(r.politician_name)},
      {h:'Ticker', f:r=>esc(r.ticker||'—')},
      {h:'Type', f:r=>esc((r.transaction_type||'').toUpperCase()), cls:r=>r.transaction_type},
      {h:'Amount', f:r=>esc(r.amount_range||'?')},
      {h:'Filed', f:r=>esc(r.filing_date||'')}]);
    $('flow').innerHTML = table(flow, [
      {h:'Ticker', f:r=>esc(r.ticker)},
      {h:'Contract', f:r=>esc(r.option_type.toUpperCase()+' $'+r.strike+' '+r.expiration), cls:r=>r.sentiment},
      {h:'Premium', f:r=>fmt$(r.premium_usd)},
      {h:'Vol/OI', f:r=>r.vol_oi_ratio.toFixed(1)+'x'}]);
    $('top').innerHTML = table(top, [
      {h:'Ticker', f:r=>esc(r.ticker)},
      {h:'Trades', f:r=>r.trade_count},
      {h:'B/S', f:r=>r.buys+'/'+r.sells},
      {h:'Politicians', f:r=>r.politician_count}]);
    $('active').innerHTML = table(active, [
      {h:'Politician', f:r=>esc(r.politician_name)},
      {h:'Trades', f:r=>r.trade_count},
      {h:'B/S', f:r=>r.buys+'/'+r.sells}]);
  } catch (e) { console.error(e); }
}
async function lookup(kind) {
  const t = $('tk').value.trim() || 'SPY';
  $('tool').textContent = 'loading '+t+'…';
  const data = await fetch('/api/'+kind+'/'+encodeURIComponent(t)).then(r => r.json());
  $('tool').textContent = JSON.stringify(data, null, 2);
}
load(); setInterval(load, 60000);
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
</script>
</body>
</html>
"""
