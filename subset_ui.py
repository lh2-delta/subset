"""Browser UI for subset (stdlib http.server; no framework)."""

from __future__ import annotations

import json
import os
import secrets
import threading
import webbrowser
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import repo_discovery
import sampler
import subset

CSRF_TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
STATE: dict = {"running": False, "progress": {}, "error": None,
               "detected": {}, "detect_tokens": {}, "detect_insecure": {}}


def default_local_dir() -> str:
    return os.environ.get("DEFAULT_LOCAL_REPOS_DIR", "").strip() or str(subset.HERE / "repos")


def tokens_file_path() -> Path:
    return Path(os.environ.get("DEFAULT_TOKENS_FILE", "") or subset.HERE / "tokens")


def is_docker() -> bool:
    return os.environ.get("SUBSET_DOCKER") == "1"


def snapshot() -> dict:
    with LOCK:
        progress = dict(STATE["progress"])
        out = {"running": STATE["running"], "error": STATE["error"]}
    out["progress"] = {
        **{k: v for k, v in progress.items() if k not in {"recent", "current", "report"}},
        "recent": list(progress.get("recent", []))[-60:],
        "current": list(progress.get("current", [])),
    }
    report = progress.get("report")
    if report:
        out["progress"]["skipped_list"] = report["skipped"]
    return out


def start_job(targets: list[subset.Target], tokens: dict[str, str], insecure_tls: bool = False) -> None:
    progress: dict = {}

    def runner() -> None:
        try:
            subset.run_job(targets, output_dir=subset.DEFAULT_OUTPUT, tokens=tokens, insecure_tls=insecure_tls,
                           progress=progress)
        except Exception as exc:  # surfaced in the UI; the server keeps running
            with LOCK:
                STATE["error"] = subset.redact(str(exc), tokens)[:500]
        finally:
            with LOCK:
                STATE["running"] = False

    with LOCK:
        STATE.update(running=True, progress=progress, error=None)
    threading.Thread(target=runner, daemon=True).start()


def page() -> str:
    docker_note = (
        '<p class="note"><b>Docker:</b> local clones are read from <code>/data/repos</code> '
        "(the <code>LOCAL_REPOS_DIR</code> folder on your computer). Results land in <code>./outputs</code>.</p>"
        if is_docker() else ""
    )
    return (PAGE.replace("__CSRF__", CSRF_TOKEN).replace("__DC_URL__", escape(subset.dc_url_from_env())).replace("__DOCKER_NOTE__", docker_note)
            .replace("__LOCAL_DIR__", escape(default_local_dir()))
            .replace("__KEEP__", f"{subset.SAMPLE_PCT:g}").replace("__MIN__", str(subset.MIN_COMMITS)))


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:
        pass

    def respond(self, status: int, ctype: str, body: str | bytes) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self.respond(HTTPStatus.OK, "text/html", page())
        elif path == "/status":
            self.respond(HTTPStatus.OK, "application/json", json.dumps(snapshot()))
        elif path == "/logo.svg":
            logo = subset.HERE / "LH2-DataLabs.svg"
            if logo.is_file():
                self.respond(HTTPStatus.OK, "image/svg+xml", logo.read_bytes())
            else:
                self.respond(HTTPStatus.NOT_FOUND, "text/plain", "no logo")
        elif path == "/download":
            zip_path = snapshot()["progress"].get("zip")
            if not zip_path or not Path(zip_path).is_file():
                self.respond(HTTPStatus.NOT_FOUND, "text/plain", "No finished run yet.")
                return
            data = Path(zip_path).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{Path(zip_path).name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.respond(HTTPStatus.NOT_FOUND, "text/plain", "Not found")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        if not secrets.compare_digest(fields.get("csrf_token", [""])[0], CSRF_TOKEN):
            self.respond(HTTPStatus.FORBIDDEN, "text/plain", "Bad CSRF token; reload the page.")
            return
        path = urlparse(self.path).path
        one = lambda k, d="": fields.get(k, [d])[0].strip()  # noqa: E731

        if path == "/discover":
            root = Path(one("local_dir")).expanduser()
            if not root.is_dir():
                self.respond(HTTPStatus.BAD_REQUEST, "application/json",
                             json.dumps({"error": f"Not a folder: {root}"}))
                return
            items = [t.full_name for t in subset.discover_local_repos(root)]
            self.respond(HTTPStatus.OK, "application/json", json.dumps({"items": items}))
            return

        if path == "/detect":
            platform = one("platform", "github")
            if platform not in repo_discovery.PLATFORMS:
                self.respond(HTTPStatus.BAD_REQUEST, "application/json", json.dumps({"error": "Unknown platform."}))
                return
            token = one("token") or subset.parse_tokens_file(tokens_file_path()).get(platform, "")
            host = one("host") or (subset.dc_url_from_env() if platform == "bitbucket_dc" else "")
            insecure = one("insecure") == "on"
            try:
                found = repo_discovery.discover_remote(
                    platform, token, host=host, workspace=one("workspace"), insecure=insecure)
            except repo_discovery.DiscoveryError as exc:
                self.respond(HTTPStatus.BAD_REQUEST, "application/json",
                             json.dumps({"error": subset.redact(str(exc), {platform: token})}))
                return
            with LOCK:
                STATE["detected"] = {k: v for k, v in STATE["detected"].items() if v.platform != platform}
                STATE["detected"].update({t.key: t for t in found})
                STATE["detect_tokens"][platform] = token
                STATE["detect_insecure"][platform] = insecure
                items = [{"key": t.key, "name": t.full_name, "platform": t.platform}
                         for t in STATE["detected"].values()]
            items.sort(key=lambda i: (i["platform"], i["name"].lower()))
            self.respond(HTTPStatus.OK, "application/json", json.dumps({"items": items, "found": len(found)}))
            return

        if path != "/start":
            self.respond(HTTPStatus.NOT_FOUND, "text/plain", "Not found")
            return

        def bad(msg: str) -> None:
            self.respond(HTTPStatus.BAD_REQUEST, "text/plain", msg)

        targets: list[subset.Target] = []
        if one("local_dir"):
            root = Path(one("local_dir")).expanduser()
            if not root.is_dir():
                return bad(f"Not a folder: {root}")
            found = subset.discover_local_repos(root)
            ticked = {v.strip() for v in fields.get("local_repos", []) if v.strip()}
            targets += [t for t in found if not ticked or t.full_name in ticked]
        with LOCK:
            detected = dict(STATE["detected"])
            tokens = {**subset.parse_tokens_file(tokens_file_path()), **STATE["detect_tokens"]}
            insecure_by_platform = dict(STATE["detect_insecure"])
        remote = [detected[k] for k in fields.get("remote_repos", []) if k in detected]
        targets += remote
        if not targets:
            return bad("Nothing to sample: pick a local folder with repos, or detect and tick remote repos.")
        insecure_tls = any(insecure_by_platform.get(t.platform) for t in remote)

        with LOCK:
            if STATE["running"]:
                self.respond(HTTPStatus.CONFLICT, "text/plain", "A run is already in progress.")
                return
        start_job(targets, tokens, insecure_tls)
        self.respond(HTTPStatus.OK, "application/json", json.dumps({"ok": True, "repos": len(targets)}))


def serve(host: str = "127.0.0.1", port: int = 8768) -> None:
    allowed = {"127.0.0.1", "localhost", "::1"} | ({"0.0.0.0"} if is_docker() else set())
    if host not in allowed:
        raise ValueError("The UI may only bind to localhost (tokens can be pasted into it).")
    server = LocalServer((host, port), Handler)
    url = f"http://{'localhost' if host == '0.0.0.0' else host}:{port}"
    print(f"subset UI: {url}", flush=True)
    if not is_docker():
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nUI stopped.")
    finally:
        server.server_close()


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>subset | LH2 DataLabs</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;800&family=Instrument+Serif:ital@0;1&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* Visual language follows lh2.ai: black canvas, hairline borders, mono eyebrows, serif italic accent, white pill buttons. */
:root{color-scheme:dark;--bg:#000;--card:#0a0a0a;--surface:#171717;--fg:#fafafa;--muted:#a1a1a1;--hair:rgba(255,255,255,.16);--border:rgba(255,255,255,.1);--accent:#3d96ff;--bad:#ff6568;--ok:#00bb7f;
--mono:"IBM Plex Mono",ui-monospace,monospace;--disp:"Inter","Helvetica Neue",Arial,sans-serif;--serif:"Instrument Serif",Georgia,serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 var(--disp);-webkit-font-smoothing:antialiased}
main{max-width:900px;margin:0 auto;padding:0 max(20px,4vw) 80px}
.top{display:flex;align-items:center;justify-content:space-between;height:74px;border-bottom:1px solid var(--border);margin-bottom:44px}
img.logo{height:26px;filter:invert(1) hue-rotate(180deg)}.tag{font:500 10.5px var(--mono);letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
.eyebrow,h2{font:500 10.5px var(--mono);letter-spacing:.18em;text-transform:uppercase;color:var(--accent);margin:0 0 16px}
h1{font-weight:800;font-size:max(34px,min(5vw,54px));line-height:1.04;letter-spacing:-.02em;margin:0 0 14px;max-width:760px}
h1 em{font:italic 400 1.08em var(--serif);letter-spacing:0}
.sub{color:var(--muted);font-size:17px;max-width:620px;margin:0 0 36px}
.card{background:var(--card);border:1px solid var(--hair);border-radius:14px;padding:24px;margin-bottom:18px}
label.f{display:block;font-weight:600;font-size:13.5px;margin:16px 0 6px}
input[type=text],input[type=number],input[type=password],select,textarea{width:100%;padding:11px 14px;border:1px solid var(--hair);border-radius:10px;background:var(--surface);color:var(--fg);font:inherit;font-size:14px;transition:border-color .2s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}input::placeholder{color:#6b6b6b}
input[type=checkbox]{accent-color:var(--accent);width:15px;height:15px;margin:0}
.row{display:flex;gap:14px;flex-wrap:wrap}.row>div{flex:1;min-width:150px}
button{font:600 13.5px var(--disp);letter-spacing:.005em;padding:12px 24px;border-radius:100px;border:1px solid #fff;background:#fff;color:#000;cursor:pointer;transition:all .2s}
button:hover{background:#e6e6e6}button.sec{background:transparent;color:#fff;border-color:rgba(255,255,255,.35)}button.sec:hover{border-color:#fff;background:rgba(255,255,255,.06)}
button:disabled{opacity:.45;cursor:default}
.small,.note{color:var(--muted);font-size:13px;margin:6px 0}.note{border-left:2px solid var(--accent);padding-left:12px}
.picks,#picker{max-height:260px;overflow:auto;border:1px solid var(--hair);border-radius:10px;padding:6px 14px;margin-top:10px;font:13px var(--mono);background:var(--surface)}
.err{color:var(--bad);font-weight:600}.ok{color:var(--ok);font-weight:600}
.bar{height:6px;background:var(--surface);border-radius:100px;overflow:hidden;margin:12px 0}.bar>i{display:block;height:100%;background:var(--accent);width:0;transition:width .4s}
pre{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px;max-height:240px;overflow:auto;font:12px var(--mono);white-space:pre-wrap;color:#d4d4d4}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:8px 8px;border-bottom:1px solid var(--border);vertical-align:top}
th{font:500 10.5px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.picks details{border-bottom:1px solid var(--border)}.picks summary{cursor:pointer;padding:7px 0;list-style-position:inside}.picks details label{display:flex;gap:10px;padding:3px 0 3px 28px}.picks .gc{margin-left:6px}
hr{border:0;border-top:1px solid var(--border);margin:22px 0}details>summary.small{cursor:pointer}
#msg{margin-left:14px}.hidden{display:none}code{font:12.5px var(--mono);background:var(--surface);padding:1px 6px;border-radius:5px}
@media (max-width:600px){.card{padding:18px}.top{margin-bottom:28px}}
</style></head><body><main>
<div class="top"><img class="logo" src="/logo.svg" alt="LH2 DataLabs" onerror="this.remove()"><span class="tag">DataLabs / subset</span></div>
<div class="eyebrow">Commit-history sampling</div>
<h1>Keep the first <em>__KEEP__%</em> of every repo.</h1>
<p class="sub">Pick repos from a folder or straight from your access token, and download a zip of history samples. Each sample is the first __KEEP__% of commits; repos with fewer than __MIN__ commits are skipped and listed with the reason. Sampling only: no metrics, no profiling.</p>
__DOCKER_NOTE__
<form id="f" onsubmit="return false">
<input type="hidden" name="csrf_token" value="__CSRF__">
<div class="card"><h2>1. Which repos</h2>
<label class="f">Local folder of git clones</label>
<div class="row"><div style="flex:3"><input type="text" name="local_dir" id="local_dir" value="__LOCAL_DIR__"></div><div style="flex:0"><button class="sec" id="load" type="button">Load repos</button></div></div>
<p class="small">Repos are found up to 3 folders deep. They are copied, never modified. Leave the folder empty to use only the remote list.</p>
<div id="pickwrap" class="hidden"><div class="row"><button class="sec" type="button" id="all">Select all</button><button class="sec" type="button" id="none">Clear</button><span class="small" id="count"></span></div>
<div id="picker" class="picks"></div><p class="small">Tick the repos to sample. Nothing ticked = every repo found.</p></div>
<hr>
<label class="f">Or detect remote repos with an access token</label>
<div class="row"><div><label class="f">Platform</label><select name="platform" id="platform"><option value="github">GitHub</option><option value="gitlab">GitLab</option><option value="bitbucket">Bitbucket Cloud</option><option value="bitbucket_dc">Bitbucket Data Center</option></select></div>
<div id="hostbox" class="hidden"><label class="f" id="hostlabel">Server URL</label><input type="text" name="host" id="host" value="__DC_URL__"></div>
<div id="wsbox" class="hidden"><label class="f">Workspace (optional)</label><input type="text" name="workspace" placeholder="blank = repos you are a member of"></div></div>
<div class="row"><div style="flex:3"><label class="f">Access token</label><input type="password" name="token" autocomplete="off" placeholder="blank = use GH_TOKEN / GITLAB_TOKEN / BITBUCKET_TOKEN / BITBUCKET_DC_TOKEN or the tokens file"></div>
<div style="flex:0;align-self:flex-end"><button class="sec" id="detect" type="button">Detect repos</button></div></div>
<label class="small" id="tlsbox" style="display:none"><input type="checkbox" name="insecure"> Skip TLS verification (self-signed server only; this disables certificate checks)</label>
<p class="small">The token is kept in memory for this session only. Detection lists every repo the token can see. Run it again for another platform; results are combined below.</p>
<div id="rpickwrap" class="hidden"><div class="row"><input type="text" id="rfilter" placeholder="Filter by name" style="flex:2"><button class="sec" type="button" id="rall">Tick shown</button><button class="sec" type="button" id="rnone">Clear</button><span class="small" id="rcount"></span></div>
<div id="rpicker" class="picks"></div>
<p class="small">Unlike local repos, <b>nothing ticked here means none</b>: detected lists can hold thousands of repos.</p></div></div>
<button id="go" type="button">Create samples</button> <span id="msg"></span></form>
<div class="card hidden" id="runcard" style="margin-top:16px"><h2>Progress</h2><div class="bar"><i id="bar"></i></div>
<div id="stats" class="small"></div><pre id="log"></pre>
<div id="done" class="hidden"><p class="ok">Finished.</p><button id="dl" type="button">Download zip</button>
<h2 style="margin-top:16px">Repos not sampled</h2><table id="skips"></table></div></div>
<script>
const $=s=>document.querySelector(s), csrf=$('[name=csrf_token]').value;
const post=(p,d)=>fetch(p,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:d});
const esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const openSets={};
function syncGroup(d){ const boxes=[...d.querySelectorAll('label input')], n=boxes.filter(e=>e.checked).length, g=d.querySelector('.grp');
  g.checked=n>0&&n===boxes.length; g.indeterminate=n>0&&n<boxes.length; d.querySelector('.gc').textContent=n+' / '+boxes.length; }
function syncAll(box){ box.querySelectorAll('details').forEach(syncGroup); }
function renderGroups(box,items,name,q,emptyMsg){
  const keep=new Set([...box.querySelectorAll('input[name='+name+']:checked')].map(e=>e.value)), open=openSets[name]||(openSets[name]=new Set());
  // Groups a filter force-opened are not the user's choice: don't remember them.
  if(!box.dataset.forced) box.querySelectorAll('details').forEach(d=>{ if(d.open) open.add(d.dataset.g); else open.delete(d.dataset.g); });
  box.dataset.forced=q?'1':'';
  const groups=new Map();
  items.filter(i=>!q||i.label.toLowerCase().includes(q)||keep.has(i.value)).forEach(i=>{ if(!groups.has(i.group)) groups.set(i.group,[]); groups.get(i.group).push(i); });
  if(!groups.size){ box.innerHTML='<span class="small">'+esc(emptyMsg)+'</span>'; return; }
  box.innerHTML=[...groups].map(([g,list])=>'<details data-g="'+esc(g)+'"'+((q||open.has(g))?' open':'')+'><summary><input type="checkbox" class="grp" title="Tick all in this group"> <b>'+esc(g)+'</b> <span class="small gc"></span></summary>'+list.map(i=>'<label><input type="checkbox" name="'+name+'" value="'+esc(i.value)+'"'+(keep.has(i.value)?' checked':'')+'>'+esc(i.display)+(i.tag?' <span class="small">'+esc(i.tag)+'</span>':'')+'</label>').join('')+'</details>').join('');
  syncAll(box); }
function wireGroups(box,name,after){
  box.addEventListener('change',e=>{ const t=e.target, d=t.closest('details'); if(!d) return;
    if(t.classList.contains('grp')) d.querySelectorAll('label input').forEach(x=>x.checked=t.checked);
    syncGroup(d); after(); });
  box.addEventListener('click',e=>{ if(e.target.classList.contains('grp')) e.stopPropagation(); }); }
function setAll(box,name,val){ box.querySelectorAll('input[name='+name+']').forEach(e=>e.checked=val); syncAll(box); }
function splitName(n,fallback){ const i=n.indexOf('/'); return i<0?[fallback,n]:[n.slice(0,i),n.slice(i+1)]; }
let localItems=[];
function count(){ const n=document.querySelectorAll('#picker input[name=local_repos]:checked').length; $('#count').textContent=n?n+' ticked':'none ticked: all will be sampled'; }
function lrender(){ renderGroups($('#picker'),localItems,'local_repos','','No git repos found in that folder.'); count(); }
$('#load').onclick=async()=>{
  $('#msg').textContent=''; const r=await post('/discover',new URLSearchParams({csrf_token:csrf,local_dir:$('#local_dir').value}));
  const j=await r.json(); if(!r.ok){$('#msg').innerHTML='<span class="err">'+esc(j.error)+'</span>';return;}
  localItems=j.items.map(n=>{ const [g,d]=splitName(n,'(top level)'); return {value:n,label:n,group:g,display:d}; });
  $('#pickwrap').classList.remove('hidden'); lrender(); };
wireGroups($('#picker'),'local_repos',count);
$('#all').onclick=()=>{setAll($('#picker'),'local_repos',true);count();};
$('#none').onclick=()=>{setAll($('#picker'),'local_repos',false);count();};
$('#go').onclick=async()=>{
  $('#msg').textContent='';
  const r=await post('/start',new URLSearchParams(new FormData($('#f'))));
  const t=await r.text(); if(!r.ok){$('#msg').innerHTML='<span class="err">'+esc(t)+'</span>';return;}
  $('#runcard').classList.remove('hidden'); $('#done').classList.add('hidden'); poll(); };
$('#dl').onclick=()=>{location.href='/download';};
const PLAT={github:['',''],gitlab:['Host (blank = gitlab.com)','https://gitlab.example.com'],bitbucket:['',''],bitbucket_dc:['Server URL','https://bitbucket.example.com']};
function platUi(){ const p=$('#platform').value;
  $('#hostbox').classList.toggle('hidden',!(p==='gitlab'||p==='bitbucket_dc')); $('#wsbox').classList.toggle('hidden',p!=='bitbucket');
  $('#hostlabel').textContent=PLAT[p][0]||'Host'; $('#host').placeholder=PLAT[p][1];
  $('#tlsbox').style.display=(p==='gitlab'||p==='bitbucket_dc')?'block':'none'; }
$('#platform').onchange=platUi; platUi();
let detected=[];
function rcount(){ const n=document.querySelectorAll('#rpicker input[name=remote_repos]:checked').length; $('#rcount').textContent=n+' ticked'; }
function rrender(){
  const items=detected.map(i=>{ const [o,d]=splitName(i.name,'(none)'); return {value:i.key,label:i.name,group:i.platform+' / '+o,display:d}; });
  renderGroups($('#rpicker'),items,'remote_repos',$('#rfilter').value.toLowerCase(),'No repos match.'); rcount(); }
wireGroups($('#rpicker'),'remote_repos',rcount);
$('#detect').onclick=async()=>{
  $('#msg').textContent=''; $('#detect').disabled=true; $('#detect').textContent='Detecting...';
  try{ const d=new URLSearchParams({csrf_token:csrf}); for(const n of ['platform','host','workspace','token']){ const el=document.querySelector('[name='+n+']'); if(el&&el.value) d.set(n,el.value); }
    if(document.querySelector('[name=insecure]').checked) d.set('insecure','on');
    const r=await post('/detect',d); const j=await r.json();
    if(!r.ok){ $('#msg').innerHTML='<span class="err">'+esc(j.error)+'</span>'; return; }
    detected=j.items; $('#rpickwrap').classList.remove('hidden'); rrender();
    $('#msg').innerHTML='<span class="ok">Found '+j.found+' repo(s).</span>';
  } finally { $('#detect').disabled=false; $('#detect').textContent='Detect repos'; } };
$('#rfilter').oninput=rrender;
$('#rall').onclick=()=>{setAll($('#rpicker'),'remote_repos',true);rcount();};
$('#rnone').onclick=()=>{setAll($('#rpicker'),'remote_repos',false);rcount();};
let timer=null;
async function poll(){
  clearTimeout(timer); const s=await (await fetch('/status')).json(), p=s.progress||{};
  const total=p.total||0, done=p.done||0;
  $('#bar').style.width=(total?100*done/total:0)+'%';
  $('#stats').textContent=total?(done+' / '+total+' done, '+(p.sampled||0)+' sampled, '+(p.skipped||0)+' not sampled'+((p.current||[]).length?'. Working on: '+p.current.join(', '):'')):'Starting...';
  $('#log').textContent=(p.recent||[]).join('\\n'); $('#log').scrollTop=1e9;
  $('#go').disabled=s.running;
  if(s.error){$('#msg').innerHTML='<span class="err">'+esc(s.error)+'</span>';}
  if(p.finished&&!s.running){ $('#done').classList.remove('hidden');
    $('#skips').innerHTML=(p.skipped_list||[]).length?'<tr><th>Repo</th><th>Reason</th></tr>'+p.skipped_list.map(e=>'<tr><td>'+esc(e.repo)+'</td><td>'+esc(e.reason)+'</td></tr>').join(''):'<tr><td>All repos were sampled.</td></tr>'; return; }
  if(s.running||!p.finished) timer=setTimeout(poll,1200); }
fetch('/status').then(r=>r.json()).then(s=>{ if(s.running||(s.progress&&s.progress.total)){ $('#runcard').classList.remove('hidden'); poll(); } });
</script></main></body></html>"""
