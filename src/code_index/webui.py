"""Tiny local status web UI (stdlib http.server only, opt-in).

`code-index web` starts a single-threaded HTTP server that serves:
  - GET /            an HTML page that polls /api/status every second
  - GET /api/status  JSON snapshot: registry + per-service live status + stats

It is intentionally minimal and low-cost: no framework, no background threads,
no websockets. The browser polls; the server only does work on request. Run it
only when you want to watch from a browser; close it when done.

Nothing is written into any indexed repo (it only reads the external registry,
the SQLite stats and the status JSON files under CACHE_HOME/status).
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from .progress import read_status
from .registry import load_registry
from .store import Store


def _snapshot() -> dict:
    """Build the JSON payload describing every registered service."""
    services = []
    for s in load_registry():
        st = read_status(s.id) or {}
        try:
            store = Store(s.settings().db_path)
            stats = store.stats()
            store.close()
        except Exception:
            stats = {"files": None, "symbols": None}
        services.append(
            {
                "id": s.id,
                "name": s.name,
                "path": str(s.path),
                "phase": st.get("phase", "idle"),
                "done": st.get("done", 0),
                "total": st.get("total", 0),
                "current": st.get("current", ""),
                "indexed": st.get("indexed", 0),
                "removed": st.get("removed", 0),
                "semantic": st.get("semantic"),
                "updated_at": st.get("updated_at"),
                "finished_at": st.get("finished_at"),
                "error": st.get("error"),
                "files": stats.get("files"),
                "symbols": stats.get("symbols"),
            }
        )
    return {"services": services}


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>code-index status</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 1.5rem; background:#0f1115; color:#e6e6e6; }
  h1 { font-size: 1.2rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #2a2f3a; }
  th { color:#8aa; font-weight:600; }
  .bar { height: 8px; background:#222836; border-radius:4px; overflow:hidden; min-width:120px; }
  .bar > span { display:block; height:100%; background:#4caf50; width:0%; }
  .phase-indexing,.phase-scanning,.phase-removing { color:#e7c14a; }
  .phase-done { color:#5fbf6b; }
  .phase-error { color:#e5534b; }
  .phase-idle { color:#778; }
  .cur { color:#9aa; font-size:.85rem; max-width:380px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .muted { color:#667; font-size:.8rem; }
</style>
</head>
<body>
<h1>code-index status <span class="muted" id="ts"></span></h1>
<table>
<thead><tr>
  <th>service</th><th>phase</th><th>progress</th><th>files</th><th>symbols</th><th>current</th>
</tr></thead>
<tbody id="rows"></tbody>
</table>
<script>
async function tick(){
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    const rows = document.getElementById('rows');
    rows.innerHTML = '';
    for (const s of data.services){
      const pct = s.total ? Math.round(s.done/s.total*100) : 0;
      const tr = document.createElement('tr');
      tr.innerHTML =
        `<td>${s.name}</td>`+
        `<td class="phase-${s.phase}">${s.phase}${s.error?(': '+s.error):''}</td>`+
        `<td><div class="bar"><span style="width:${pct}%"></span></div>`+
        `<span class="muted">${s.total?(s.done+'/'+s.total+' ('+pct+'%)'):''}</span></td>`+
        `<td>${s.files ?? '?'}</td>`+
        `<td>${s.symbols ?? '?'}</td>`+
        `<td class="cur">${s.current||''}</td>`;
      rows.appendChild(tr);
    }
    document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  } catch(e){ /* server stopped */ }
}
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path.rstrip("/") in ("", "/index.html") or self.path == "/":
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/status"):
            body = json.dumps(_snapshot()).encode("utf-8")
            self._send(200, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args) -> None:  # silence per-request logging
        pass


def serve(host: str = "127.0.0.1", port: int = 8765) -> int:
    httpd = HTTPServer((host, port), _Handler)
    print(f"code-index web UI: http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopping web UI...")
    finally:
        httpd.server_close()
    return 0
