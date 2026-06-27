"""Tiny local status web UI (stdlib http.server only, opt-in).

`code-index web` starts a single-threaded HTTP server that serves:
  - GET  /              an HTML page that polls /api/status every second
  - GET  /api/status    JSON snapshot: registry + per-service live status + stats
  - POST /api/reindex   start an incremental reindex of one service (?service=ID)

It is intentionally minimal and low-cost: no framework, no websockets. The
browser polls; the server only does work on request. A user-triggered reindex
runs in a short-lived background thread so the single-threaded server stays
responsive. Run it only when you want to watch from a browser; close it when
done.

Nothing is written into any indexed repo (it only reads the external registry,
the SQLite stats and the status JSON files under CACHE_HOME/status, and writes
its own status file there while indexing).
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .indexer import is_reindex_active, start_background_reindex
from .progress import read_status
from .registry import Service, load_registry
from .store import Store


def _is_busy(service_id: str, phase: str) -> bool:
    """Busy if a reindex is running for this service (our process or another).

    The phase check also catches a `code-index-watch` daemon (a different
    process) indexing the same service, so the button stays disabled then too.
    """
    return is_reindex_active(service_id, phase)


def start_reindex(service: Service, full: bool = False) -> tuple[bool, str]:
    """Start a reindex in a background thread (idempotent per service).

    full=True forces a complete rebuild (slow; re-embeds everything). Delegates
    to the shared `start_background_reindex` helper so the job guard is the same
    one the MCP server and CLI use.
    """
    phase = (read_status(service.id) or {}).get("phase")
    return start_background_reindex(
        service.id, service.name, service.settings(), full=full, status_phase=phase
    )


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
        phase = st.get("phase", "idle")
        services.append(
            {
                "id": s.id,
                "name": s.name,
                "path": str(s.path),
                "phase": phase,
                "done": st.get("done", 0),
                "total": st.get("total", 0),
                "current": st.get("current", ""),
                "indexed": st.get("indexed", 0),
                "removed": st.get("removed", 0),
                "semantic": st.get("semantic"),
                "semantic_failures": st.get("semantic_failures", 0),
                "semantic_embed_failures": st.get("semantic_embed_failures", 0),
                "updated_at": st.get("updated_at"),
                "finished_at": st.get("finished_at"),
                "error": st.get("error"),
                "files": stats.get("files"),
                "symbols": stats.get("symbols"),
                "busy": _is_busy(s.id, phase),
            }
        )
    return {"services": services}


def _service_by_id(service_id: str) -> Service | None:
    for s in load_registry():
        if s.id == service_id or s.name == service_id:
            return s
    return None


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>code-index status</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 1.5rem; background:#0f1115; color:#e6e6e6; }
  h1 { font-size: 1.2rem; }
  /* Fixed layout + explicit column widths: text length changes never reflow. */
  table { border-collapse: collapse; width: 100%; table-layout: fixed; }
  th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #2a2f3a;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  th { color:#8aa; font-weight:600; }
  col.c-name { width: 12rem; }
  col.c-phase { width: 8rem; }
  col.c-prog { width: 16rem; }
  col.c-files { width: 6rem; }
  col.c-syms { width: 7rem; }
  col.c-act { width: 12rem; }
  /* current takes all remaining width (no fixed size) so long paths fit. */
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .bar { height: 8px; background:#222836; border-radius:4px; overflow:hidden; }
  /* Animate width so the bar glides instead of jumping each refresh. */
  .bar > span { display:block; height:100%; background:#4caf50; width:0%;
                transition: width .8s linear; }
  .phase-indexing,.phase-scanning,.phase-removing { color:#e7c14a; }
  .phase-done { color:#5fbf6b; }
  .phase-warn { color:#e7a33a; }
  .phase-error { color:#e5534b; }
  .phase-idle { color:#778; }
  .cur { color:#9aa; font-size:.85rem; }
  .muted { color:#667; font-size:.8rem; }
  td.act { overflow: visible; white-space: nowrap; }
  button.btn { font: inherit; font-size:.82rem; padding:.25rem .7rem; cursor:pointer;
               border-radius:5px; transition: background .15s; }
  button.btn:disabled { color:#778; background:#1a1f29; border-color:#2a2f3a;
                        cursor:default; }
  button.reidx { color:#dfe; background:#1f6f3a; border:1px solid #2c8f4e; }
  button.reidx:hover:not(:disabled) { background:#268048; }
  button.full { color:#f0d9b5; background:#6e4a1e; border:1px solid #8f6a2c;
                margin-left:.4rem; }
  button.full:hover:not(:disabled) { background:#80582a; }
  /* Confirmation modal for the destructive full rebuild. */
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6);
             display: none; align-items: center; justify-content: center; z-index: 10; }
  .overlay.show { display: flex; }
  .modal { background:#161a22; border:1px solid #2a2f3a; border-radius:8px;
           padding:1.2rem 1.4rem; max-width:30rem; box-shadow:0 8px 30px rgba(0,0,0,.5); }
  .modal h2 { margin:0 0 .6rem; font-size:1.05rem; }
  .modal p { margin:.4rem 0; color:#cdd; font-size:.9rem; line-height:1.4; }
  .modal .warn { color:#e7c14a; }
  .modal .row { margin-top:1rem; display:flex; justify-content:flex-end; gap:.6rem; }
  button.cancel { color:#cdd; background:#2a2f3a; border:1px solid #3a4150; }
  button.cancel:hover { background:#333a48; }
  button.danger { color:#fff; background:#9b2f29; border:1px solid #c0413a; }
  button.danger:hover { background:#b03830; }
</style>
</head>
<body>
<h1>code-index status <span class="muted" id="ts"></span></h1>
<table>
<colgroup>
  <col class="c-name"><col class="c-phase"><col class="c-prog">
  <col class="c-files"><col class="c-syms"><col class="c-cur"><col class="c-act">
</colgroup>
<thead><tr>
  <th>service</th><th>phase</th><th>progress</th>
  <th class="num">files</th><th class="num">symbols</th><th>current</th><th></th>
</tr></thead>
<tbody id="rows"></tbody>
</table>

<div class="overlay" id="overlay">
  <div class="modal" role="dialog" aria-modal="true">
    <h2>Full reindex?</h2>
    <p>Service: <strong id="m-name"></strong></p>
    <p class="warn">This wipes the existing index and rebuilds it from scratch.</p>
    <p>It re-reads every file and <strong>re-embeds all chunks via the API</strong>
       (semantic layer), so it can be slow and consume API quota. The incremental
       <em>Reindex</em> button is usually enough.</p>
    <div class="row">
      <button class="btn cancel" id="m-cancel" type="button">Cancel</button>
      <button class="btn danger" id="m-confirm" type="button">Full reindex</button>
    </div>
  </div>
</div>
<script>
// Update rows in place (keyed by service id) so the DOM is never torn down and
// rebuilt — that is what made the page flicker on every refresh.
const rowCache = new Map();

function setText(el, value){
  // Only touch the DOM when the value actually changed (avoids repaint churn).
  if (el.textContent !== value) el.textContent = value;
}

// Service ids the user just clicked: keep the button disabled optimistically
// until the next poll reports the service as busy (avoids a flicker/double-click
// while the background thread spins up).
const pending = new Set();
const nameById = new Map();

async function reindex(id, full){
  pending.add(id);
  const c = rowCache.get(id);
  if (c){ c.btn.disabled = true; c.full.disabled = true; setText(c.btn, 'starting…'); }
  try {
    await fetch('/api/reindex?service=' + encodeURIComponent(id) + (full ? '&full=1' : ''),
                {method: 'POST'});
  } catch(e){ pending.delete(id); }
  tick();
}

// --- full reindex confirmation modal ---
let modalTarget = null;
const overlay = document.getElementById('overlay');
function openModal(id){
  modalTarget = id;
  setText(document.getElementById('m-name'), nameById.get(id) || id);
  overlay.classList.add('show');
}
function closeModal(){ modalTarget = null; overlay.classList.remove('show'); }
document.getElementById('m-cancel').addEventListener('click', closeModal);
document.getElementById('m-confirm').addEventListener('click', () => {
  const id = modalTarget; closeModal();
  if (id) reindex(id, true);
});
overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

function makeRow(id){
  const tr = document.createElement('tr');
  tr.innerHTML =
    '<td class="name"></td>'+
    '<td class="phase"></td>'+
    '<td><div class="bar"><span></span></div><span class="muted prog"></span></td>'+
    '<td class="num files"></td>'+
    '<td class="num syms"></td>'+
    '<td class="cur" title=""></td>'+
    '<td class="act">'+
      '<button class="btn reidx" type="button">Reindex</button>'+
      '<button class="btn full" type="button" title="Full rebuild (slow)">Full</button>'+
    '</td>';
  const btn = tr.querySelector('button.reidx');
  const full = tr.querySelector('button.full');
  btn.addEventListener('click', () => reindex(id, false));
  full.addEventListener('click', () => openModal(id));
  const cells = {
    row: tr,
    name: tr.querySelector('.name'),
    phase: tr.querySelector('.phase'),
    bar: tr.querySelector('.bar > span'),
    prog: tr.querySelector('.prog'),
    files: tr.querySelector('.files'),
    syms: tr.querySelector('.syms'),
    cur: tr.querySelector('.cur'),
    btn: btn,
    full: full,
  };
  rowCache.set(id, cells);
  return cells;
}

async function tick(){
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    const tbody = document.getElementById('rows');
    const seen = new Set();
    for (const s of data.services){
      seen.add(s.id);
      let c = rowCache.get(s.id);
      if (!c){ c = makeRow(s.id); tbody.appendChild(c.row); }
      const pct = s.total ? Math.round(s.done/s.total*100) : 0;
      setText(c.name, s.name);
      const lost = (s.semantic_embed_failures || 0) + (s.semantic_failures || 0);
      let phaseText = s.phase + (s.error ? (': ' + s.error) : '');
      if (lost > 0) phaseText += ' \u26a0 ' + lost + ' semantic lost';
      setText(c.phase, phaseText);
      c.phase.className = 'phase phase-' + (lost > 0 && s.phase === 'done' ? 'warn' : s.phase);
      c.phase.title = lost > 0
        ? (s.semantic_embed_failures || 0) + ' chunks failed to embed, '
          + (s.semantic_failures || 0) + ' vectors failed to upsert'
        : '';
      c.bar.style.width = pct + '%';
      setText(c.prog, s.total ? (s.done + '/' + s.total + ' (' + pct + '%)') : '');
      setText(c.files, s.files ?? '?');
      setText(c.syms, s.symbols ?? '?');
      setText(c.cur, s.current || '');
      c.cur.title = s.current || '';
      nameById.set(s.id, s.name);
      // Once the server confirms the service is busy, clear our optimistic flag.
      if (s.busy) pending.delete(s.id);
      const busy = s.busy || pending.has(s.id);
      c.btn.disabled = busy;
      c.full.disabled = busy;
      setText(c.btn, busy ? 'indexing…' : 'Reindex');
    }
    // Drop rows for services that disappeared from the registry.
    for (const [id, c] of rowCache){
      if (!seen.has(id)){ c.row.remove(); rowCache.delete(id); }
    }
    setText(document.getElementById('ts'), new Date().toLocaleTimeString());
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

    def _send_json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = urlparse(self.path).path
        if path.rstrip("/") in ("", "/index.html"):
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send_json(200, _snapshot())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        if parsed.path != "/api/reindex":
            self._send(404, b"not found", "text/plain")
            return
        params = parse_qs(parsed.query)
        service_id = (params.get("service") or [""])[0]
        full = (params.get("full") or ["0"])[0] in ("1", "true", "yes")
        svc = _service_by_id(service_id)
        if svc is None:
            self._send_json(404, {"ok": False, "error": "unknown service"})
            return
        started, msg = start_reindex(svc, full=full)
        self._send_json(200, {"ok": started, "status": msg, "service": svc.id, "full": full})

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
