# ruff: noqa: E501  (the embedded HTML/JS page has long lines)
"""Build a self-contained HTML viewer from exported Orchestrate traces (local, offline).

    make wxo-traces-view            # export our traces from the last LAST and open the viewer

Reads ``build/traces/*.json`` (written by ``wxo_traces.py``, which only exports Andes Travel
traces) and writes ``build/traces/index.html``. The page has no external requests: traces
can contain what travelers typed, so they stay on this machine. ``build/`` is git-ignored.

Observations carry no parent id, so the span tree is inferred from time containment.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACES = ROOT / "build" / "traces"


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _is_error(obs: dict) -> bool:
    meta = obs.get("metadata") or {}
    flat = " ".join(f"{k}={v}" for k, v in meta.items()).lower()
    return "level=error" in flat or "status_code=error" in flat or "error" in meta


def summarize(data: dict) -> dict:
    """One trace: spans with inferred depth and offsets in ms, plus totals for the list."""
    spans = []
    for obs in data.get("observations", []):
        start = _ts(obs.get("startTime"))
        if start is None:
            continue
        spans.append({**obs, "_s": start, "_e": _ts(obs.get("endTime")) or start})
    spans.sort(key=lambda o: (o["_s"], -o["_e"]))
    t0 = spans[0]["_s"] if spans else 0.0
    t1 = max((o["_e"] for o in spans), default=t0)
    stack: list[dict] = []
    out = []
    for o in spans:
        while stack and o["_s"] >= stack[-1]["_e"] and not (o["_s"] == o["_e"] == stack[-1]["_s"]):
            stack.pop()
        out.append(
            {
                "id": o.get("id"),
                "name": o.get("name", "?"),
                "type": o.get("type", "SPAN"),
                "depth": len(stack),
                "start_ms": round((o["_s"] - t0) * 1000, 1),
                "dur_ms": round((o["_e"] - o["_s"]) * 1000, 1),
                "model": o.get("model"),
                "usage": o.get("usage"),
                "input": o.get("input"),
                "output": o.get("output"),
                "metadata": o.get("metadata"),
                "error": _is_error(o),
            }
        )
        stack.append(o)
    tokens = 0
    for o in out:
        usage = o["usage"] or {}
        tokens += int(usage.get("totalTokens") or usage.get("total") or 0)
    return {
        "trace_id": data.get("trace_id", ""),
        "started": datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M:%S") if spans else "",
        "total_ms": round((t1 - t0) * 1000, 1),
        "spans": out,
        "generations": sum(o["type"] == "GENERATION" for o in out),
        "tokens": tokens,
        "errors": sum(o["error"] for o in out),
    }


def load(directory: Path) -> list[dict]:
    traces = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"skipping unreadable {path.name}", file=sys.stderr)
            continue
        if data.get("observations"):
            traces.append(summarize(data))
    traces.sort(key=lambda t: t["started"], reverse=True)
    return traces


def render(traces: list[dict]) -> str:
    # "</" would end the script block early: escape it inside the JSON payload.
    payload = json.dumps(traces, ensure_ascii=False, default=str).replace("</", "<\\/")
    return PAGE.replace("__TITLE__", html.escape("Andes Travel · trazas")).replace(
        "__DATA__", payload
    )


PAGE = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fafaf8;--panel:#fff;--ink:#1c1c1a;--mute:#6b6b66;--line:#e4e3de;--sel:#eef3ff;
--span:#8a8a84;--chain:#6c5ce7;--gen:#0a7d5a;--tool:#c26a00;--err:#c0392b;--bar:#d9d8d2}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1e1e1c;--ink:#ecebe6;--mute:#9a9992;
--line:#33332f;--sel:#232a3a;--span:#9a9992;--chain:#a29bfe;--gen:#3ccf9e;--tool:#f0a040;--err:#ff6b5b;--bar:#3a3a36}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,sans-serif}header{padding:12px 16px;border-bottom:1px solid var(--line);
display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}h1{font-size:16px;margin:0}
.mute{color:var(--mute)}main{display:grid;grid-template-columns:320px 1fr;height:calc(100vh - 49px)}
@media (max-width:800px){main{grid-template-columns:1fr;height:auto}}
#list{overflow:auto;border-right:1px solid var(--line)}
.row{padding:10px 16px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:var(--sel)}.row.sel{background:var(--sel);box-shadow:inset 3px 0 var(--chain)}
.row b{font-variant-numeric:tabular-nums}.pill{font-size:12px;padding:1px 7px;border-radius:9px;
border:1px solid var(--line);margin-right:4px;color:var(--mute)}.pill.err{color:var(--err);border-color:var(--err)}
#detail{overflow:auto;padding:16px}.wf{background:var(--panel);border:1px solid var(--line);border-radius:8px}
.sp{display:grid;grid-template-columns:minmax(180px,34%) 1fr 70px;align-items:center;gap:8px;
padding:3px 10px;cursor:pointer;border-bottom:1px solid var(--line)}.sp:last-child{border:0}
.sp:hover,.sp.sel{background:var(--sel)}.nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track{position:relative;height:12px}.bar{position:absolute;top:2px;height:8px;min-width:2px;border-radius:2px;
background:var(--span)}.t-CHAIN .bar{background:var(--chain)}.t-GENERATION .bar{background:var(--gen)}
.t-TOOL .bar{background:var(--tool)}.sp.err .bar,.sp.err .nm{color:var(--err);background:var(--err)}
.ms{text-align:right;color:var(--mute);font-variant-numeric:tabular-nums}
#io{margin-top:14px;display:grid;gap:10px}.blk{background:var(--panel);border:1px solid var(--line);border-radius:8px}
.blk h3{margin:0;padding:6px 10px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;
color:var(--mute);border-bottom:1px solid var(--line)}pre{margin:0;padding:10px;white-space:pre-wrap;
word-break:break-word;font:12px/1.5 ui-monospace,Menlo,monospace;max-height:340px;overflow:auto}
input{padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
.empty{padding:40px;text-align:center;color:var(--mute)}
</style></head><body>
<header><h1>Andes Travel · trazas de Orchestrate</h1><span class="mute" id="count"></span>
<input id="q" placeholder="Filtrar por texto o ID" aria-label="Filtrar"></header>
<main><div id="list"></div><div id="detail"><div class="empty">Elige una traza.</div></div></main>
<script>
const DATA = __DATA__;
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = ms => ms >= 1000 ? (ms/1000).toFixed(2)+' s' : Math.round(ms)+' ms';
const pretty = v => v == null ? '—' : typeof v === 'string' ? v : JSON.stringify(v, null, 2);
let cur = null;

function list(){
  const q = $('#q').value.toLowerCase();
  const rows = DATA.filter(t => !q || t.trace_id.includes(q) ||
    t.spans.some(s => (s.name+JSON.stringify(s.input)+JSON.stringify(s.output)).toLowerCase().includes(q)));
  $('#count').textContent = rows.length + ' de ' + DATA.length + ' trazas';
  $('#list').innerHTML = rows.length ? rows.map(t => `<div class="row${cur===t.trace_id?' sel':''}" data-id="${t.trace_id}">
    <b>${esc(t.started)}</b> <span class="mute">${fmt(t.total_ms)}</span><br>
    <span class="pill">${t.spans.length} pasos</span><span class="pill">${t.generations} LLM</span>
    ${t.tokens?`<span class="pill">${t.tokens} tokens</span>`:''}
    ${t.errors?`<span class="pill err">${t.errors} errores</span>`:''}
    <div class="mute" style="font-size:12px">${t.trace_id.slice(0,12)}…</div></div>`).join('')
    : '<div class="empty">Sin trazas. Corre <code>make wxo-traces-view</code> tras una conversación.</div>';
  document.querySelectorAll('.row').forEach(r => r.onclick = () => show(r.dataset.id));
}

function show(id){
  cur = id; list();
  const t = DATA.find(x => x.trace_id === id), total = Math.max(t.total_ms, 1);
  $('#detail').innerHTML = `<div class="mute" style="margin-bottom:8px">Traza ${t.trace_id} · ${fmt(t.total_ms)}</div>
  <div class="wf">${t.spans.map((s,i) => `<div class="sp t-${esc(s.type)}${s.error?' err':''}" data-i="${i}">
    <div class="nm" style="padding-left:${s.depth*14}px" title="${esc(s.name)}">${esc(s.name)}
      <span class="mute">${esc(s.type.toLowerCase())}</span></div>
    <div class="track"><div class="bar" style="left:${s.start_ms/total*100}%;width:${s.dur_ms/total*100}%"></div></div>
    <div class="ms">${fmt(s.dur_ms)}</div></div>`).join('')}</div><div id="io"></div>`;
  document.querySelectorAll('.sp').forEach(r => r.onclick = () => span(t, +r.dataset.i));
}

function span(t, i){
  const s = t.spans[i];
  document.querySelectorAll('.sp').forEach((r,j) => r.classList.toggle('sel', j===i));
  const blk = (h, v) => `<div class="blk"><h3>${h}</h3><pre>${esc(pretty(v))}</pre></div>`;
  $('#io').innerHTML = `<div><b>${esc(s.name)}</b> <span class="mute">${esc(s.type)} · ${fmt(s.dur_ms)}
    ${s.model?' · '+esc(s.model):''}</span></div>` + blk('Entrada', s.input) + blk('Salida', s.output)
    + (s.usage ? blk('Uso', s.usage) : '') + (s.metadata ? blk('Metadatos', s.metadata) : '');
  $('#io').scrollIntoView({behavior:'smooth', block:'nearest'});
}

$('#q').oninput = list; list(); if (DATA.length) show(DATA[0].trace_id);
</script></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", type=Path, default=TRACES)
    args = parser.parse_args()
    traces = load(args.dir)
    out = args.dir / "index.html"
    args.dir.mkdir(parents=True, exist_ok=True)
    out.write_text(render(traces), encoding="utf-8")
    print(f"{len(traces)} traces -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
