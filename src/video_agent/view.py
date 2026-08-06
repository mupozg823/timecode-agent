"""Self-contained HTML views: per-workspace player + corpus browser.

No-excuse size marker: ``# noqa: SIZE_OK``. The renderer intentionally keeps
its inline HTML, CSS, and JavaScript together so each export stays standalone.

Derived presentation layer only — markdown/JSONL stay the source of truth
(md-first). Zero server, zero framework, zero network: one static file per
workspace that plays the local video with checkpoint/transcript
click-to-seek (the one interaction plain markdown cannot do), plus one
corpus page with a filter box, a sortable dense table, and a canvas force
graph of workspace↔entity co-occurrence (hand-rolled — vendoring d3 would
break the zero-framework commitment; the repulsion tick uses a uniform
300px grid so cost stays local-pair bounded as the corpus grows, and the
graph caps entities at the best-connected ``_GRAPH_ENTITY_MAX`` with a
visible notice — the table always lists every workspace). Regenerated on
demand by `va view`, never by daemons. Media purged by `va gc` degrades
to thumbnails-only with a notice.

Design contract (production-frontend, 2026-07): dark-first OKLCH tokens with
an automatic light theme, hierarchy from borders and an 8px rhythm (no glow,
no gradients), 14px data density with ~32px rows, Korean text scoped
``keep-all``/``overflow-wrap:anywhere``, motion limited to transform/opacity
micro-transitions honoring ``prefers-reduced-motion``.
"""

from __future__ import annotations

import base64
import html
import json
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from .corpus_projection import (
    READINESS_KO,
    STATUS_KO,
    WorkspaceMeta,
    checkpoint_anchor,
    human_reason,
    humanize_surface,
    ws_meta,
)
from .export import capture_reasons
from .fsio import write_text_atomic
from .image_provenance import resolve_image_path
from .projection_cache import (
    load_projection_cache,
    renderer_salt,
    save_projection_cache,
    workspace_fingerprint,
)
from .search import corpus_root
from .story_projection import StoryCheckpoint, build_story_map
from .timestamps import fmt_ts_compact, fmt_ts_label
from .transcript_segments import load_transcript_segments
from .view_sequences import render_sequence_section
from .view_story_map import STORY_MAP_CSS, STORY_MAP_JS, render_story_map
from .workspace import Workspace
from .workspace_discovery import find_workspaces

_CSS = """
:root{color-scheme:dark;
 --bg:oklch(0.17 0.005 255);--surface:oklch(0.205 0.006 255);
 --surface-2:oklch(0.24 0.007 255);--border:oklch(0.28 0.008 255);
 --border-strong:oklch(0.34 0.01 255);
 --text:oklch(0.88 0.005 255);--muted:oklch(0.63 0.012 255);
 --accent:oklch(0.72 0.11 245);--accent-ink:oklch(0.2 0.02 245);
 --ok:oklch(0.74 0.11 150);--ok-bg:oklch(0.28 0.04 150);
 --warn:oklch(0.8 0.1 85);--warn-bg:oklch(0.3 0.04 85)}
@media (prefers-color-scheme:light){:root{color-scheme:light;
 --bg:oklch(0.975 0.002 255);--surface:oklch(1 0 0);
 --surface-2:oklch(0.945 0.004 255);--border:oklch(0.89 0.006 255);
 --border-strong:oklch(0.8 0.01 255);
 --text:oklch(0.25 0.01 255);--muted:oklch(0.5 0.015 255);
 --accent:oklch(0.5 0.13 245);--accent-ink:oklch(0.98 0 0);
 --ok:oklch(0.5 0.12 150);--ok-bg:oklch(0.93 0.04 150);
 --warn:oklch(0.55 0.12 85);--warn-bg:oklch(0.95 0.05 85)}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.65 'Pretendard Variable',Pretendard,-apple-system,
 'Apple SD Gothic Neo','Noto Sans KR',sans-serif;
 background:var(--bg);color:var(--text);
 -webkit-font-smoothing:antialiased}
:lang(ko){word-break:keep-all;overflow-wrap:anywhere}
main{max-width:1200px;margin:0 auto;padding:24px 24px 64px}
h1{font-size:21px;line-height:1.35;letter-spacing:-.01em;margin:0 0 4px}
h2{font-size:15px;letter-spacing:-.005em;margin:40px 0 12px;
 padding-bottom:8px;border-bottom:1px solid var(--border)}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:3px}
a:focus-visible,button:focus-visible,input:focus-visible,
summary:focus-visible,[tabindex]:focus-visible{
 outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.meta{color:var(--muted);font-size:13px}
.meta a{color:var(--muted);text-decoration:underline;
 text-underline-offset:3px;text-decoration-color:var(--border-strong)}
.meta a:hover{color:var(--accent)}
.topbar{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 16px;
 margin-bottom:20px}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}
.stat{background:var(--surface);border:1px solid var(--border);
 border-radius:8px;padding:8px 14px;font-size:13px;color:var(--muted)}
.stat b{display:block;font-size:17px;color:var(--text);
 font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;
 margin:16px 0 12px}
input[type=search]{flex:1;min-width:220px;padding:8px 12px;border-radius:8px;
 border:1px solid var(--border);background:var(--surface);color:var(--text);
 font:inherit;font-size:14px}
input[type=search]::placeholder{color:var(--muted)}
input[type=search]:hover{border-color:var(--border-strong)}
.count{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums;
 white-space:nowrap}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:8px;
 overflow:hidden;background:var(--surface)}
.seg button{border:0;background:none;color:var(--muted);font:inherit;
 font-size:13px;padding:7px 14px;cursor:pointer;
 transition:background-color .12s ease-out,color .12s ease-out}
.seg button:hover{color:var(--text)}
.seg button[aria-pressed=true]{background:var(--surface-2);color:var(--text)}
.seg button+button{border-left:1px solid var(--border)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
.gchip{border:1px solid var(--border);background:var(--surface);
 color:var(--muted);font:inherit;font-size:12.5px;border-radius:999px;
 padding:3px 12px;cursor:pointer;
 transition:background-color .12s ease-out,color .12s ease-out,
 border-color .12s ease-out}
.gchip:hover{color:var(--text);border-color:var(--border-strong)}
.gchip[aria-pressed=true]{background:var(--accent);color:var(--accent-ink);
 border-color:var(--accent)}
.gchip b{font-variant-numeric:tabular-nums;font-weight:600;opacity:.75;
 margin-left:4px}
.table-scroll{overflow-x:auto;border:1px solid var(--border);
 border-radius:10px;background:var(--surface);scrollbar-width:thin}
.table-scroll table{min-width:640px}
.table-scroll td:nth-child(3){white-space:nowrap}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border-bottom:1px solid var(--border);padding:7px 12px;
 text-align:left;vertical-align:top}
tbody tr:last-child th,tbody tr:last-child td{border-bottom:0}
thead th{position:sticky;top:0;z-index:1;background:var(--surface);
 color:var(--muted);font-weight:600;font-size:12.5px;white-space:nowrap}
thead button{border:0;background:none;color:inherit;font:inherit;
 cursor:pointer;padding:0;display:inline-flex;align-items:center;gap:4px}
thead button:hover{color:var(--text)}
th[aria-sort=ascending] button:after{content:"↑"}
th[aria-sort=descending] button:after{content:"↓"}
tbody tr{transition:background-color .12s ease-out}
tbody tr:hover{background:var(--surface-2)}
td.num{text-align:right;font-variant-numeric:tabular-nums;
 white-space:nowrap;color:var(--muted)}
.clamp2{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
 overflow:hidden}
.empty{padding:36px 16px;text-align:center;color:var(--muted);font-size:14px}
.seqs{margin:0 0 18px}
.seq{border:1px solid var(--border);border-radius:10px;padding:12px;
 margin:0 0 10px;background:var(--surface)}
.seq-head{display:flex;gap:8px;align-items:center;margin-bottom:6px}
.seq .intent{margin:0 0 4px;font-weight:600}
.seq .brief,.seq .effect{margin:0 0 4px;color:var(--muted);font-size:13px}
.seq .cuts{margin:8px 0 0;padding-left:18px}
.seq .cut{margin:0 0 8px}
.seq .cut-span{font-variant-numeric:tabular-nums}
.seq .role{color:var(--muted);font-size:12px}
.seq .note{margin:2px 0 0;font-size:13px}
.seq .rejected{margin-top:8px;font-size:13px;color:var(--muted)}
.media-warn{margin:0 0 10px;padding:10px 12px;border-radius:8px;
 border:1px solid var(--border-strong);background:var(--surface);
 color:var(--text);font-size:13px;line-height:1.5}
video{width:100%;max-height:52vh;background:#000;border-radius:10px;
 border:1px solid var(--border);display:block}
.player-rail{position:sticky;top:0;z-index:5;background:var(--bg);
 padding:10px 0 6px;margin:0 -2px;}
.timeline{display:flex;gap:2px;height:14px;margin-top:8px;border-radius:7px;
 overflow:hidden;background:var(--surface)}
.timeline button{border:0;padding:0;cursor:pointer;background:var(--surface-2);
 min-width:3px;transition:opacity .12s ease-out;opacity:.85}
.timeline button:hover{opacity:1}
.timeline button.st-verified,.timeline button.st-corrected{
 background:var(--ok)}
.timeline button.st-hypothesized{background:var(--border-strong)}
.timeline button.active{outline:2px solid var(--text);outline-offset:-2px;
 opacity:1}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:12px;
 border-radius:999px;padding:1px 9px;background:var(--surface-2);
 color:var(--muted);white-space:nowrap}
.badge:before{content:"";width:6px;height:6px;border-radius:50%;
 background:var(--muted)}
.badge.verified,.badge.corrected,.badge.converged{
 background:var(--ok-bg);color:var(--ok)}
.badge.verified:before,.badge.corrected:before,.badge.converged:before{
 background:var(--ok)}
.badge.verify_more,.badge.cover_gaps,.badge.provisional{
 background:var(--warn-bg);color:var(--warn)}
.badge.verify_more:before,.badge.cover_gaps:before,
.badge.provisional:before{background:var(--warn)}
.cps{display:flex;flex-direction:column;gap:8px;margin-top:12px}
.cp{display:grid;grid-template-columns:150px 1fr;gap:4px 16px;
 background:var(--surface);border:1px solid var(--border);border-radius:10px;
 padding:10px 14px;transition:background-color .12s ease-out,
 border-color .12s ease-out;
 content-visibility:auto;contain-intrinsic-size:auto 110px}
.cp:hover{border-color:var(--border-strong)}
.cp.active{background:var(--surface-2);border-color:var(--accent)}
.cp .when{display:flex;flex-direction:column;align-items:flex-start;gap:6px}
.cp .situation{font-size:14px;margin:0}
.cp .who{font-size:12.5px;color:var(--muted)}
.cp .conf{font-variant-numeric:tabular-nums}
.seek{cursor:pointer;color:var(--accent);white-space:nowrap;border:0;
 background:none;padding:0;font:inherit;font-size:13px;
 font-variant-numeric:tabular-nums}
.seek:hover{text-decoration:underline;text-underline-offset:3px}
.follow{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;
 color:var(--muted);cursor:pointer;user-select:none}
.transcript p{margin:8px 0 0}
.transcript .seg-line{padding:2px 6px;margin:0 -6px;border-radius:6px}
.transcript .seg-line.active{background:var(--surface-2)}
.notice{background:var(--warn-bg);color:var(--warn);border-radius:8px;
 padding:10px 14px;font-size:13px}
.evidence{display:grid;gap:12px;
 grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr))}
.evidence figure{margin:0;background:var(--surface);
 border:1px solid var(--border);border-radius:10px;overflow:hidden}
.evidence img{display:block;width:100%;height:auto;max-height:340px;
 object-fit:contain;background:#000}
.evidence figcaption{font-size:12.5px;color:var(--muted);padding:8px 12px;
 border-top:1px solid var(--border)}
img{max-width:100%}
details{margin-top:8px}
summary{cursor:pointer;color:var(--muted);font-size:14px}
summary:hover{color:var(--text)}
.graph-wrap{position:relative;border:1px solid var(--border);
 border-radius:10px;background:var(--surface);overflow:hidden}
.graph-wrap canvas{display:block;width:100%;height:540px;cursor:grab;
 touch-action:none}
.graph-wrap canvas.dragging{cursor:grabbing}
.graph-hint{position:absolute;left:12px;bottom:10px;font-size:12px;
 color:var(--muted);pointer-events:none}
.graph-tip{position:absolute;padding:3px 10px;font-size:12.5px;
 background:var(--surface-2);border:1px solid var(--border-strong);
 border-radius:6px;pointer-events:none;transform:translate(-50%,-140%);
 white-space:nowrap;display:none}
.chip{display:inline-block;margin:2px 8px 2px 0;font-size:13px}
[hidden]{display:none!important}
@media (max-width:520px){
 main{padding:16px 14px 48px}
 .cp{grid-template-columns:1fr}
 .cp .when{flex-direction:row;flex-wrap:wrap;align-items:center}}
@media (min-width:1100px){
 .split{display:grid;grid-template-columns:minmax(0,58%) minmax(0,1fr);
  gap:0 28px;align-items:start}
 .split .player-rail{position:sticky;top:16px;padding:0}
 video{max-height:none;aspect-ratio:16/9;object-fit:contain}}
@media (min-width:1600px){main{max-width:1440px}}
@media (prefers-reduced-motion:reduce){
 *{transition-duration:.01ms!important;animation:none!important}}
""".strip()

_SEEK_JS = """
const v=document.querySelector('video');let stop=null;
// 로컬 파일로 ingest한 워크스페이스는 원본을 file:// 절대 경로로 가리킨다.
// 그 페이지를 로컬 서버로 열면 브라우저가 file:// 리소스를 막아 재생이
// 조용히 검은 화면이 된다(2026-07-27 실측). 침묵 대신 말한다.
if(v){const _src=v.getAttribute('src')||'';
 if(_src.indexOf('file:')===0&&location.protocol!=='file:'){
  const _b=document.createElement('p');_b.className='media-warn';
  _b.textContent='원본이 이 워크스페이스 밖 로컬 경로에 있어 서버로 열면 '
   +'재생되지 않습니다 — 이 페이지를 파일로 직접 열거나(file://), '
   +'원본을 워크스페이스 안으로 옮긴 뒤 va view를 다시 실행하십시오.';
  v.parentNode.insertBefore(_b,v);}}
const spans=[...document.querySelectorAll('[data-span-start]')];
const marks=[...document.querySelectorAll('.timeline button,.story-span.seek')];
function seekTo(t){if(!v||!isFinite(t))return;
 if(v.readyState>=1)v.currentTime=t;
 else v.addEventListener('loadedmetadata',()=>{v.currentTime=t;},
  {once:true});}
document.querySelectorAll('.seek').forEach(el=>el.addEventListener('click',()=>{
 if(!v)return;const t=+el.dataset.start;
 stop=el.dataset.end?+el.dataset.end:null;
 seekTo(t);v.play();
 const card=el.closest('.cp');
 if(card&&card.id)
  history.replaceState(null,'','#'+encodeURIComponent(card.id));}));
let h=location.hash.slice(1);
try{h=decodeURIComponent(h);}catch(e){}
if(h.startsWith('t=')){seekTo(+h.slice(2));}
else if(h){const el=document.getElementById(h);
 if(el&&el.classList.contains('cp')){
  el.scrollIntoView({block:'center'});el.classList.add('active');
  seekTo(+el.dataset.spanStart);}}
const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
const follow=document.getElementById('follow');
let raf=0,activeEl=null;
function track(t){
 let cur=null;
 for(const el of spans){
  const on=t>=+el.dataset.spanStart&&t<+el.dataset.spanEnd;
  el.classList.toggle('active',on);if(on&&!cur)cur=el;}
 for(const m of marks)
  m.classList.toggle('active',t>=+m.dataset.start&&t<+(m.dataset.end||-1));
 if(cur&&cur!==activeEl){activeEl=cur;
  if(follow&&follow.checked)cur.scrollIntoView(
   {block:'nearest',behavior:reduced?'auto':'smooth'});}}
// 경계 정지는 rAF 밖에서 — 탭이 가려지면 rAF가 멈춰 정지가 영영 안
// 걸린다(창 가림 실측). 정지는 기능이고 rAF는 시각 추적이다.
if(v){v.addEventListener('timeupdate',()=>{
 if(stop!==null&&v.currentTime>=stop){v.pause();stop=null;}
 if(raf)return;
 raf=requestAnimationFrame(()=>{raf=0;track(v.currentTime);});});}
""".strip()

# Corpus page: filter + sort + group chips + list/graph toggle + force graph.
_CORPUS_JS = """
const q=document.querySelector('input[type=search]');
const rows=[...document.querySelectorAll('tbody tr[data-title]')];
const cnt=document.getElementById('cnt');
const emptyRow=document.getElementById('empty-row');
let group='';
function syncHash(){
 const p=new URLSearchParams();
 const graphView=document.getElementById('graph-view');
 if(graphView&&!graphView.hidden)p.set('view','graph');
 if(q.value)p.set('q',q.value);
 if(group)p.set('g',group);
 const s=p.toString();
 history.replaceState(null,'',
  s?'#'+s:location.pathname+location.search);}
function apply(){
 const t=q.value.toLowerCase();let n=0;
 for(const r of rows){
  const hay=(r.textContent+' '+(r.dataset.labels||'')).toLowerCase();
  const hit=(!group||r.dataset.group===group)&&hay.includes(t);
  r.hidden=!hit;if(hit)n++;}
 if(cnt)cnt.textContent=n===rows.length?
  rows.length+'편 모두 표시':rows.length+'편 중 '+n+'편';
 if(emptyRow)emptyRow.hidden=n>0;
 // 목록과 그래프가 같은 필터를 본다. 예전에는 "39편 중 3편"이라 적힌
 // 화면에서 그래프만 39편을 계속 그렸다.
 window.__wsFilter=(t||group)?
  new Set(rows.filter(r=>!r.hidden).map(r=>'ws:'+r.dataset.rel)):null;
 if(window.__graphRedraw)window.__graphRedraw();
 syncHash();}
let deb=0;
q.addEventListener('input',()=>{clearTimeout(deb);deb=setTimeout(apply,80);});
addEventListener('keydown',e=>{
 if(e.key==='/'&&document.activeElement!==q){e.preventDefault();q.focus();}
 if(e.key==='Escape'&&document.activeElement===q){q.value='';apply();}});
document.querySelectorAll('.gchip').forEach(c=>c.addEventListener('click',()=>{
 group=c.dataset.group===group?'':c.dataset.group;
 document.querySelectorAll('.gchip').forEach(x=>
  x.setAttribute('aria-pressed',String(x.dataset.group===group&&!!group)));
 apply();}));
const tbody=document.querySelector('tbody');
document.querySelectorAll('thead th[data-key]').forEach(th=>{
 th.querySelector('button').addEventListener('click',()=>{
  const key=th.dataset.key,num=th.dataset.num==='1';
  const dir=th.getAttribute('aria-sort')==='ascending'?-1:1;
  document.querySelectorAll('thead th').forEach(x=>
   x.removeAttribute('aria-sort'));
  th.setAttribute('aria-sort',dir>0?'ascending':'descending');
  rows.sort((a,b)=>{const x=a.dataset[key],y=b.dataset[key];
   return dir*(num?(+x)-(+y):x.localeCompare(y,'ko'));});
  rows.forEach(r=>tbody.insertBefore(r,emptyRow));});});
// ---- list/graph toggle ----
const listBtn=document.getElementById('show-list');
const graphBtn=document.getElementById('show-graph');
const listView=document.getElementById('list-view');
const graphView=document.getElementById('graph-view');
let graphStarted=false;
function show(graph){
 listView.hidden=graph;graphView.hidden=!graph;
 listBtn.setAttribute('aria-pressed',String(!graph));
 graphBtn.setAttribute('aria-pressed',String(graph));
 if(graph&&!graphStarted){graphStarted=true;initGraph();}
 syncHash();}
listBtn.addEventListener('click',()=>show(false));
graphBtn.addEventListener('click',()=>show(true));
const hp=new URLSearchParams(location.hash.slice(1));
if(hp.get('q'))q.value=hp.get('q');
if(hp.get('g')){group=hp.get('g');
 document.querySelectorAll('.gchip').forEach(x=>
  x.setAttribute('aria-pressed',String(x.dataset.group===group)));}
if(location.hash==='#graph'||hp.get('view')==='graph')show(true);
// ---- hand-rolled force graph (no d3: zero-framework contract) ----
function initGraph(){
 const data=JSON.parse(document.getElementById('graph-data').textContent);
 const wrap=document.querySelector('.graph-wrap');
 const canvas=wrap.querySelector('canvas');
 const tip=wrap.querySelector('.graph-tip');
 const ctx=canvas.getContext('2d');
 const css=getComputedStyle(document.documentElement);
 const C={halo:css.getPropertyValue('--surface'),
  edge:css.getPropertyValue('--border-strong'),
  ws:css.getPropertyValue('--accent'),
  ent:css.getPropertyValue('--muted'),
  text:css.getPropertyValue('--text'),dim:.12};
 const N=data.nodes,L=data.links;
 const deg=new Map();
 for(const l of L){deg.set(l.s,(deg.get(l.s)||0)+1);
  deg.set(l.t,(deg.get(l.t)||0)+1);}
 // 초기 산포를 sqrt(N)로 키워 셀 점유 밀도를 상수로 유지 — 고정 환형은
 // 노드가 늘수록 같은 셀에 몰려 그리드 지역성이 초반 틱에서 무력했다.
 const spread=Math.max(1,Math.sqrt(N.length/150));
 N.forEach((n,i)=>{const a=i/N.length*Math.PI*2,
  r=(160+40*(i%5))*spread;
  n.x=Math.cos(a)*r;n.y=Math.sin(a)*r;n.vx=0;n.vy=0;
  n.r=n.type==='ws'?7:Math.min(4+(deg.get(n.id)||1)*.7,9);});
 const byId=new Map(N.map(n=>[n.id,n]));
 const E=L.map(l=>({a:byId.get(l.s),b:byId.get(l.t)})).filter(e=>e.a&&e.b);
 const nbr=new Map();
 for(const e of E){
  (nbr.get(e.a)||nbr.set(e.a,new Set()).get(e.a)).add(e.b);
  (nbr.get(e.b)||nbr.set(e.b,new Set()).get(e.b)).add(e.a);}
 let W,H,dpr;let scale=1,ox=0,oy=0;
 function resize(){dpr=devicePixelRatio||1;
  W=wrap.clientWidth;H=canvas.clientHeight;
  canvas.width=W*dpr;canvas.height=H*dpr;}
 resize();addEventListener('resize',()=>{resize();draw();});
 let alpha=1,hover=null,drag=null,panning=false,px=0,py=0;
 let downX=0,downY=0,movedFar=false;
 function tick(){
  // 반발력은 300px 컷오프 밖에서 0 — 균일 그리드(300px 셀)의 이웃 셀만
  // 보면 전 쌍 O(n²)이 지역 쌍으로 준다(같은 컷오프라 물리 동일 · 각
  // 쌍을 양쪽에서 한 번씩 방문하니 힘은 자기 노드에만 더한다).
  const CELL=300,grid=new Map();
  const cellOf=v=>Math.floor(v/CELL);
  for(const n of N){const k=cellOf(n.x)+':'+cellOf(n.y);
   const c=grid.get(k);if(c)c.push(n);else grid.set(k,[n]);}
  for(const a of N){
   const cx=cellOf(a.x),cy=cellOf(a.y);
   for(let gx=cx-1;gx<=cx+1;gx++)for(let gy=cy-1;gy<=cy+1;gy++){
    const cell=grid.get(gx+':'+gy);if(!cell)continue;
    for(const b of cell){if(b===a)continue;
     const dx=a.x-b.x,dy=a.y-b.y;const d2=dx*dx+dy*dy||1;
     if(d2<90000){const f=900/d2;const d=Math.sqrt(d2);
      a.vx+=dx/d*f;a.vy+=dy/d*f;}}}}
  for(const e of E){let dx=e.b.x-e.a.x,dy=e.b.y-e.a.y;
   const d=Math.sqrt(dx*dx+dy*dy)||1;const f=(d-70)*.012;
   dx/=d;dy/=d;e.a.vx+=dx*f*d*.02;e.a.vy+=dy*f*d*.02;
   e.b.vx-=dx*f*d*.02;e.b.vy-=dy*f*d*.02;}
  for(const n of N){n.vx-=n.x*.004;n.vy-=n.y*.004;
   n.vx*=.86;n.vy*=.86;
   const sp=Math.hypot(n.vx,n.vy);
   if(sp>24){n.vx*=24/sp;n.vy*=24/sp;}
   if(n!==drag){n.x+=n.vx;n.y+=n.vy;}}}
 // 필터가 켜지면 적중 워크스페이스와 그 이웃 엔티티만 살아난다.
 // 나머지는 지우지 않고 흐리게 남긴다 — 문맥이 사라지면 그래프가 아니다.
 function inFilter(n){
  const f=window.__wsFilter;
  if(!f)return true;
  if(n.type==='ws')return f.has(n.id);
  const near=nbr.get(n);
  if(!near)return false;
  for(const m of near)if(m.type==='ws'&&f.has(m.id))return true;
  return false;}
 function draw(){
  ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
  ctx.translate(W/2+ox,H/2+oy);ctx.scale(scale,scale);
  const hi=hover?nbr.get(hover)||new Set():null;
  for(const e of E){
   const dim=(hover&&e.a!==hover&&e.b!==hover)
    ||!(inFilter(e.a)&&inFilter(e.b));
   ctx.globalAlpha=dim?C.dim:.45;
   ctx.strokeStyle=C.edge;ctx.lineWidth=1/scale;
   ctx.beginPath();ctx.moveTo(e.a.x,e.a.y);ctx.lineTo(e.b.x,e.b.y);
   ctx.stroke();}
  for(const n of N){
   const dim=(hover&&n!==hover&&!(hi&&hi.has(n)))||!inFilter(n);
   ctx.globalAlpha=dim?C.dim:1;
   ctx.fillStyle=n.type==='ws'?C.ws:C.ent;
   ctx.beginPath();ctx.arc(n.x,n.y,n.r,0,7);ctx.fill();}
  ctx.font=(12/scale)+'px Pretendard,-apple-system,sans-serif';
  ctx.textAlign='center';
  ctx.lineWidth=3/scale;ctx.strokeStyle=C.halo;ctx.lineJoin='round';
  // 라벨은 그린 순서대로 자리를 잡고, 이미 놓인 라벨과 겹치면 건너뛴다.
  // 39편 실코퍼스에서 워크스페이스 라벨이 전부 그려져 서로 뭉갰다.
  const placed=[];
  const ordered=N.filter(n=>{
   const focus=n===hover||(hi&&hi.has(n));
   return focus||(n.type==='ws'&&scale>=.7)||scale>1.5;})
   .sort((a,b)=>{
    const fa=(a===hover||(hi&&hi.has(a)))?0:(inFilter(a)?1:2);
    const fb=(b===hover||(hi&&hi.has(b)))?0:(inFilter(b)?1:2);
    return fa-fb||(deg.get(b.id)||0)-(deg.get(a.id)||0);});
  for(const n of ordered){
   const focus=n===hover||(hi&&hi.has(n));
   const y=n.y-n.r-5/scale;
   const w=ctx.measureText(n.label).width,h=13/scale;
   const box=[n.x-w/2,y-h,n.x+w/2,y];
   let hit=false;
   for(const p of placed){
    if(box[0]<p[2]&&box[2]>p[0]&&box[1]<p[3]&&box[3]>p[1]){hit=true;break;}}
   if(hit&&!focus)continue;
   placed.push(box);
   ctx.globalAlpha=focus?1:
    (inFilter(n)?Math.min(1,(scale-.55)*2):C.dim);
   ctx.strokeText(n.label,n.x,y);ctx.fillStyle=C.text;
   ctx.fillText(n.label,n.x,y);}
  ctx.globalAlpha=1;}
 window.__graphRedraw=()=>{if(graphStarted)draw();};
 function fit(){
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  for(const n of N){x0=Math.min(x0,n.x);y0=Math.min(y0,n.y);
   x1=Math.max(x1,n.x);y1=Math.max(y1,n.y);}
  const bw=x1-x0+120,bh=y1-y0+120;
  scale=Math.min(1.2,Math.max(.3,Math.min(W/bw,H/bh)));
  ox=-(x0+x1)/2*scale;oy=-(y0+y1)/2*scale;}
 let userNav=false;
 const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
 if(reduced){const t0=performance.now();
  for(let i=0;i<400&&performance.now()-t0<1500;i++)tick();
  fit();draw();}
 else{(function loop(){if(alpha>0.012||drag){alpha*=.995;tick();
   if(!userNav)fit();
   draw();requestAnimationFrame(loop);}})();}
 function pos(e){const r=canvas.getBoundingClientRect();
  return{x:(e.clientX-r.left-W/2-ox)/scale,
         y:(e.clientY-r.top-H/2-oy)/scale};}
 function nodeAt(p){let best=null,bd=144;
  for(const n of N){const dx=n.x-p.x,dy=n.y-p.y,d=dx*dx+dy*dy;
   if(d<bd){bd=d;best=n;}}
  return best;}
 canvas.addEventListener('pointermove',e=>{
  const p=pos(e);
  if(Math.hypot(e.clientX-downX,e.clientY-downY)>5)movedFar=true;
  if(drag){drag.x=p.x;drag.y=p.y;alpha=Math.max(alpha,.4);
   if(reduced)draw();return;}
  if(panning){ox+=e.clientX-px;oy+=e.clientY-py;px=e.clientX;py=e.clientY;
   draw();return;}
  const n=nodeAt(p);
  if(n!==hover){hover=n;canvas.style.cursor=n?'pointer':'grab';
   if(n){const r=canvas.getBoundingClientRect();
    tip.style.display='block';
    tip.style.left=(n.x*scale+W/2+ox)+'px';
    tip.style.top=(n.y*scale+H/2+oy)+'px';
    tip.textContent=n.label;}
   else tip.style.display='none';
   draw();}});
 canvas.addEventListener('pointerdown',e=>{userNav=true;
  downX=e.clientX;downY=e.clientY;movedFar=false;
  const n=nodeAt(pos(e));canvas.setPointerCapture(e.pointerId);
  if(n){drag=n;alpha=Math.max(alpha,.4);if(!reduced){(function loop(){
   if(drag){tick();draw();requestAnimationFrame(loop);}})();}}
  else{panning=true;px=e.clientX;py=e.clientY;
   canvas.classList.add('dragging');}});
 canvas.addEventListener('pointerup',e=>{
  const wasDrag=drag;drag=null;panning=false;
  canvas.classList.remove('dragging');
  const n=nodeAt(pos(e));
  if(wasDrag&&n===wasDrag&&n&&!movedFar){
   if(n.type==='ws'&&n.href)location.href=n.href;
   else if(n.type==='ent'){show(false);q.value=n.label;apply();}}});
 canvas.addEventListener('pointerleave',()=>{hover=null;
  tip.style.display='none';draw();});
 canvas.addEventListener('wheel',e=>{e.preventDefault();userNav=true;
  scale=Math.min(2.5,Math.max(.35,scale*(e.deltaY<0?1.1:.9)));draw();},
  {passive:false});
 function showTip(n){tip.style.display='block';
  tip.style.left=(n.x*scale+W/2+ox)+'px';
  tip.style.top=(n.y*scale+H/2+oy)+'px';
  tip.textContent=n.label;}
 let kb=-1;
 canvas.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&hover){
   if(hover.type==='ws'&&hover.href)location.href=hover.href;
   else if(hover.type==='ent'){show(false);q.value=hover.label;apply();
    q.focus();}
   return;}
  const step=e.key==='ArrowRight'||e.key==='ArrowDown'?1:
   e.key==='ArrowLeft'||e.key==='ArrowUp'?-1:0;
  if(!step)return;
  e.preventDefault();userNav=true;
  kb=(kb+step+N.length)%N.length;hover=N[kb];showTip(hover);draw();});
 canvas.addEventListener('blur',()=>{if(kb>=0){hover=null;kb=-1;
  tip.style.display='none';draw();}});}
apply();
""".strip()


def _page(title: str, body: list[str], js: str, *, extra_css: str = "") -> str:
    style = _CSS + ("\n" + extra_css if extra_css else "")
    return "\n".join([
        "<!doctype html>", '<html lang="ko"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta name="color-scheme" content="dark light">',
        f"<title>{html.escape(title)}</title>",
        f"<style>{style}</style></head><body><main>",
        *body,
        f"</main><script>{js}</script></body></html>",
    ]) + "\n"


def _video_src(ws: Workspace) -> str | None:
    """Relative URL when the source lives inside the workspace (URL ingests),
    absolute file:// URI otherwise; None when the media is gone (gc'd)."""
    video = ws.video
    if not video.is_file():
        return None
    try:
        return quote(str(video.relative_to(ws.root)))
    except ValueError:
        return video.resolve().as_uri()


def _seek(
    s: float,
    e: float | None,
    label: str,
    *,
    enabled: bool,
) -> str:
    """미디어가 없으면 시간을 텍스트로만 남긴다 — 죽은 seek 버튼 금지."""
    if not enabled:
        return f'<span class="timecode">{html.escape(label)}</span>'
    end_attr = f' data-end="{e:.2f}"' if e is not None else ""
    return (f'<button type="button" class="seek" data-start="{s:.2f}"'
            f"{end_attr}>{html.escape(label)}</button>")


def _timeline(cps: list[StoryCheckpoint], duration: float) -> str:
    """Span strip under the player — each checkpoint as a seekable block."""
    if not cps or duration <= 0:
        return ""
    blocks, cursor = [], 0.0
    # 투영 계약: checkpoints는 이미 (start, end, id) 정렬로 온다 — 재정렬 불요.
    for c in cps:
        e = min(max(c["end"], c["start"]), duration)
        s = max(c["start"], 0.0, cursor)   # 겹친 앞부분은 이미 그려진 폭
        if e <= s and cursor > 0.0:
            continue                        # 완전히 덮인 스팬은 생략
        if s > cursor:
            blocks.append(
                f'<span style="flex:{(s - cursor) / duration:.4f}"></span>')
        share = max((e - s) / duration, 0.003)
        label = (f"{c['id']} · "
                 f"{fmt_ts_label(s)}–{fmt_ts_label(e)}")
        blocks.append(
            f'<button type="button" class="seek st-{html.escape(c["status"])}"'
            f' style="flex:{share:.4f}" data-start="{s:.2f}"'
            f' data-end="{e:.2f}" title="{html.escape(label)}"'
            f' aria-label="{html.escape(label)}"></button>')
        cursor = max(cursor, e)
    if cursor < duration:
        blocks.append(
            f'<span style="flex:{(duration - cursor) / duration:.4f}"></span>')
    return ('<div class="timeline" role="group" aria-label="장면 타임라인">'
            + "".join(blocks) + "</div>")


# 단일 파일로 인라인할 수 있는 캡처 포맷 — 그 외는 생략으로 센다
# (조용히 상대참조를 남기면 받은 쪽에서 깨진 이미지가 된다).
_INLINE_IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".png": "image/png", ".webp": "image/webp",
                      ".gif": "image/gif"}


def _inline_image_src(resolved: Path) -> str | None:
    mime = _INLINE_IMAGE_MIME.get(resolved.suffix.lower())
    if mime is None:
        return None
    try:
        payload = resolved.read_bytes()
    except OSError:
        return None
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def export_html(
    ws: Workspace, *, corpus_backlink: str = "../view.html",
    standalone: bool = False,
) -> str:
    """One static page: story map + player + checkpoint cards + transcript.

    ``standalone``은 파일 1개 전달용(#45): 비디오·상대참조를 빼고 캡처를
    data URI로 인라인한다 — 정상 뷰의 상대참조·플레이어는 그대로 둔다.
    """
    meta = ws_meta(ws)
    src = None if standalone else _video_src(ws)
    story = build_story_map(ws)
    cps = story["checkpoints"]
    media_available = src is not None
    links = ("" if standalone else
             f' · <a href="{ws.doc_stem}.md">글로 보기</a> · '
             f'<a href="{html.escape(corpus_backlink, quote=True)}">'
             "← 라이브러리</a>")
    body = [
        '<div class="topbar"><div>',
        f"<h1>{html.escape(meta['title'])}</h1>",
        f'<p class="meta">{fmt_ts_compact(meta["duration"])} · '
        f'{html.escape(meta["mode_head"])} · '
        f'<span class="badge {meta["readiness"]}">'
        f'{READINESS_KO.get(meta["readiness"], meta["readiness"])}</span> '
        f'· 기록된 장면 {meta["cps"]}{links}</p>',
        "</div></div>",
    ]
    # 스토리 맵은 스플릿 위 — 두 원장을 한 시간축으로 조망한 뒤 상세로 간다.
    story_html = render_story_map(story, media_available=media_available)
    if story_html:
        body.append(story_html)
    body.append('<div class="split"><div>')
    if src:
        body.append('<div class="player-rail">'
                    f'<video controls preload="metadata" src="{src}"></video>'
                    + _timeline(cps, float(meta["duration"] or 0))
                    + "</div>")
    elif standalone:
        body.append('<p class="notice">단일 파일 보기 — 영상 재생 없이 '
                    "장면 캡처와 기록으로 봅니다</p>")
    else:
        body.append('<p class="notice">영상 파일이 없습니다(원본이 이동됐거나 정리됨) '
                    "— 장면 캡처로만 볼 수 있습니다</p>")
    body.append("</div><div>")
    # 편집 결정을 장면 기록 위에 둔다 — 컷에서 근거 체크포인트로 내려가는
    # 앵커가 아래를 향해야 스크롤 방향이 읽는 순서와 맞는다.
    body.append(render_sequence_section(story))

    if cps:
        body += ['<h2 style="margin-top:0">장면 기록 '
                 '<label class="follow" style="float:right">'
                 '<input type="checkbox" id="follow">재생 위치 따라가기</label>'
                 "</h2>",
                 '<div class="cps">']
        for c in cps:
            s, e = c["start"], c["end"]
            card_id = checkpoint_anchor(c["id"])
            speakers = " · ".join(c["speakers"])
            who = (f'<span class="who">{html.escape(speakers)}</span>'
                   if speakers else "")
            confidence = (
                f'{c["confidence"]:g}' if c["confidence"] is not None else "-"
            )
            situation = humanize_surface(c["situation"])
            body.append(
                f'<article class="cp story-ground" id="{card_id}"'
                f' data-checkpoint-id="{card_id}"'
                f' data-span-start="{s:.2f}"'
                f' data-span-end="{e:.2f}">'
                '<div class="when">'
                + _seek(s, e, f"{fmt_ts_label(s)}–{fmt_ts_label(e)}",
                        enabled=media_available)
                + f'<span class="badge {html.escape(c["status"])}">'
                f'{html.escape(STATUS_KO.get(c["status"], c["status"]))}'
                "</span></div>"
                "<div>"
                f'<p class="situation">{html.escape(situation)}</p>'
                f'<p class="meta">{html.escape(c["id"])}'
                f' · 확신 <span class="conf">{confidence}</span>'
                f"{(' · ' + who) if who else ''}</p>"
                "</div></article>")
        body.append("</div>")

    segs = load_transcript_segments(ws.transcript_path)
    if segs:
        body.append('<details class="transcript">'
                    f"<summary>받아쓰기 전체 {len(segs)}구간</summary><p>")
        body += [
            f'<span class="seg-line" data-span-start="{s["start"]:.2f}"'
            f' data-span-end="{s["end"]:.2f}">'
            + _seek(s["start"], None, fmt_ts_label(s["start"]),
                    enabled=media_available)
            + f" {html.escape(s['text'])}</span><br>"
            for s in segs
        ]
        body.append("</p></details>")

    reasons = capture_reasons(ws)
    evidence, gone = [], 0
    for c in cps:
        for rel in c["visual_evidence"]:
            try:
                image_path, resolved = resolve_image_path(ws, rel)
            except ValueError:
                gone += 1
                continue
            if not resolved.is_file():          # gc'd frame — md keeps the
                gone += 1                       # record, the view skips it
                continue
            if standalone:
                img_src = _inline_image_src(resolved)
                if img_src is None:
                    gone += 1
                    continue
            else:
                img_src = quote(image_path, safe="/-._~")
            reason = human_reason(reasons.get(Path(image_path).name, ""))
            evidence.append(
                f'<figure><img loading="lazy" decoding="async" '
                f'src="{img_src}" '
                f'alt="{html.escape(c["id"])}">'
                "<figcaption>"
                + _seek(c["start"], c["end"], fmt_ts_label(c["start"]),
                        enabled=media_available)
                + f" {html.escape(c['id'])} — "
                f"{html.escape(humanize_surface(c['situation']))}"
                f"{(' · ' + html.escape(reason)) if reason else ''}"
                "</figcaption></figure>")
    if evidence or gone:
        body.append("<h2>장면 캡처</h2>")
        if gone:
            body.append(f'<p class="meta">정리된 캡처 {gone}장 생략 '
                        "(글 기록에는 남아 있습니다)</p>")
        body.append('<div class="evidence">')
        body += evidence
        body.append("</div>")

    body.append("</div></div>")  # close .split
    return _page(
        meta["title"],
        body,
        _SEEK_JS + "\n" + STORY_MAP_JS,
        extra_css=STORY_MAP_CSS,
    )


# 그래프 엔티티 노드 상한 — 코퍼스가 커져도 임베드 JSON과 시뮬레이션
# 비용을 유계로 유지한다. 워크스페이스 노드·목록 테이블은 전량이 계약이고,
# 엔티티만 연결수 상위로 자른다(발동 시 그래프 힌트에 명시 — 무언 캡 금지).
_GRAPH_ENTITY_MAX = 400


def _graph_payload(rows: list[tuple[WorkspaceMeta, str]]) -> tuple[str, int]:
    """workspace↔entity co-occurrence graph JSON과 잘린 엔티티 수."""
    appearances: dict[str, int] = {}
    for r, _rel in rows:
        for lb in set(r["labels"]):
            appearances[lb] = appearances.get(lb, 0) + 1
    kept = set(sorted(
        appearances, key=lambda lb: (-appearances[lb], lb)
    )[:_GRAPH_ENTITY_MAX])
    dropped = len(appearances) - len(kept)
    nodes: list[dict[str, object]] = []
    links: list[dict[str, str]] = []
    seen_entities: set[str] = set()
    for r, rel in rows:
        ws_id = f"ws:{rel}"
        nodes.append({"id": ws_id, "label": r["title"] or r["name"],
                      "type": "ws", "href": f"{quote(rel)}/view.html"})
        for lb in r["labels"]:
            if lb not in kept:
                continue
            ent_id = f"ent:{lb}"
            if lb not in seen_entities:
                seen_entities.add(lb)
                nodes.append({"id": ent_id, "label": lb, "type": "ent"})
            links.append({"s": ws_id, "t": ent_id})
    payload = json.dumps({"nodes": nodes, "links": links},
                         ensure_ascii=False, separators=(",", ":"))
    return payload.replace("</", "<\\/"), dropped


def build_view(
    roots: list[str] | None = None,
    *,
    workspace_paths: Sequence[Path] | None = None,
    projection_root: Path | None = None,
    standalone: bool = False,
) -> tuple[Path, int]:
    """Regenerate every workspace's view.html + the corpus browser page.

    ``standalone``이면 워크스페이스마다 ``view-standalone.html``을 병행
    생성한다(파일 1개 전달용 — 일반 뷰·캐시는 그대로).
    """
    ws_dirs = (
        list(workspace_paths)
        if workspace_paths is not None
        else find_workspaces(roots)
    )
    if not ws_dirs:
        raise ValueError("no workspaces found (looked in: "
                         + ", ".join(roots or ["./va-out"]) + ")")
    root = (
        Path(projection_root).resolve()
        if projection_root is not None
        else corpus_root(roots)
    )
    # 포맷 솔트 — export_html/_page 렌더 포맷을 바꾸면 올려서 전량 무효화.
    # 렌더러 소스 digest에서 자동 유도 — 수동 범프를 잊으면 투영 캐시가
    # 기존 코퍼스의 플레이어 페이지를 낡은 렌더로 영구 스킵한다
    # (v1→v2 수동 시절의 사고를 기계로 봉합).
    view_salt = renderer_salt("view")
    cache = load_projection_cache(root)
    view_cache = cache.get("view")
    if not isinstance(view_cache, dict):
        view_cache = {}
    cache["view"] = view_cache

    rows: list[tuple[WorkspaceMeta, str]] = []
    for d in ws_dirs:
        ws = Workspace.load(d)
        meta = ws_meta(ws)
        try:
            rel = d.relative_to(root).as_posix()
        except ValueError:
            rel = meta["name"]
        depth = len(Path(rel).parts)
        corpus_backlink = "../" * depth + "view.html"
        # 소스 영상은 워크스페이스 밖에 있을 수 있고, 사라지면 렌더가
        # "영상 없음" 안내로 바뀐다 — 존재·상태를 지문에 포함해야 이동·삭제
        # 후의 스킵이 죽은 <video> 링크를 남기지 않는다.
        fingerprint = workspace_fingerprint(
            d, salt=view_salt, extra_paths=[ws.video]
        )
        dest_ws = ws.root / "view.html"
        if view_cache.get(rel) != fingerprint or not dest_ws.is_file():
            write_text_atomic(
                dest_ws,
                export_html(ws, corpus_backlink=corpus_backlink),
            )
            view_cache[rel] = fingerprint
        if standalone:
            # 캐시 없이 항상 재생성 — 내용이 결정적이고, 전달용 파일이
            # 낡은 채 남는 것이 재생성 비용보다 비싸다.
            write_text_atomic(
                ws.root / "view-standalone.html",
                export_html(ws, standalone=True),
            )
        rows.append((meta, rel))

    sorted_rows = sorted(rows, key=lambda item: item[1])
    total_secs = sum(float(r["duration"] or 0) for r, _ in sorted_rows)
    total_cps = sum(int(r["cps"] or 0) for r, _ in sorted_rows)
    converged = sum(1 for r, _ in sorted_rows if r["readiness"] == "converged")
    groups: dict[str, int] = {}
    for _, rel in sorted_rows:
        parts = Path(rel).parts
        if len(parts) > 1:
            groups[parts[0]] = groups.get(parts[0], 0) + 1

    body = [
        '<div class="topbar">',
        "<h1>영상 라이브러리</h1>",
        f'<p class="meta">분석을 마친 영상 {len(rows)}편 — 이 목록은 `va view`로 '
        '언제든 다시 만들어집니다. 글 원본은 <a href="INDEX.md">INDEX.md</a>.</p>',
        "</div>",
        '<div class="stats">'
        f'<span class="stat"><b>{len(rows)}</b>영상</span>'
        f'<span class="stat"><b>{total_secs / 3600:.1f}시간</b>총 길이</span>'
        f'<span class="stat"><b>{total_cps}</b>기록된 장면</span>'
        f'<span class="stat"><b>{converged}</b>분석 완료</span>'
        "</div>",
        '<div class="toolbar">',
        '<input type="search" aria-label="영상 검색" '
        'placeholder="검색: 제목·유형·장면·인물…  /키로 바로 입력">',
        f'<span class="count" id="cnt">{len(rows)}편 모두 표시</span>',
        '<div class="seg" role="group" aria-label="보기 전환">'
        '<button type="button" id="show-list" aria-pressed="true">목록'
        "</button>"
        '<button type="button" id="show-graph" aria-pressed="false">그래프'
        "</button></div>",
        "</div>",
    ]
    if groups:
        body.append('<div class="chips">' + "".join(
            f'<button type="button" class="gchip" aria-pressed="false" '
            f'data-group="{html.escape(g, quote=True)}">'
            f"{html.escape(g)}<b>{n}</b></button>"
            for g, n in sorted(groups.items())) + "</div>")

    body.append('<div id="list-view">')
    body.append(
        '<div class="table-scroll"><table><thead><tr>'
        '<th data-key="title" aria-sort="ascending">'
        "<button type=\"button\">영상</button></th>"
        '<th data-key="duration" data-num="1">'
        "<button type=\"button\">길이</button></th>"
        '<th data-key="mode"><button type="button">유형</button></th>'
        "<th>상태</th>"
        '<th data-key="cps" data-num="1">'
        "<button type=\"button\">장면</button></th>"
        "<th>요약</th></tr></thead><tbody>")
    for r, rel in sorted_rows:
        title = html.escape(r["title"] or r["name"])
        parts = Path(rel).parts
        group = parts[0] if len(parts) > 1 else ""
        crumb = (f'<span class="meta">{html.escape(group)} / </span>'
                 if group else "")
        body.append(
            f'<tr data-title="{html.escape(str(r["title"] or r["name"]), quote=True)}"'
            f' data-duration="{float(r["duration"] or 0):.0f}"'
            f' data-mode="{html.escape(str(r["mode_head"]), quote=True)}"'
            f' data-cps="{r["cps"]}"'
            f' data-group="{html.escape(group, quote=True)}"'
            # 그래프 노드(`ws:<rel>`)와 목록 행을 잇는 키 — 이게 없으면
            # 검색으로 목록만 좁혀지고 그래프는 전편을 계속 그린다.
            f' data-rel="{html.escape(rel, quote=True)}"'
            f' data-labels="{html.escape(" ".join(r["labels"]), quote=True)}">'
            f'<td>{crumb}<a href="{quote(rel)}/view.html">{title}</a></td>'
            f'<td class="num">{fmt_ts_compact(r["duration"])}</td>'
            f"<td>{html.escape(r['mode_head'])}</td>"
            f'<td><span class="badge {r["readiness"]}">'
            f'{READINESS_KO.get(r["readiness"], r["readiness"])}'
            "</span></td>"
            f'<td class="num">{r["cps"]}</td>'
            f'<td><span class="clamp2">'
            f'{html.escape(humanize_surface(r["head"]))}</span></td>'
            "</tr>")
    body.append('<tr id="empty-row" hidden><td colspan="6">'
                '<div class="empty">조건에 맞는 영상이 없습니다 — '
                "Esc 키를 누르면 검색어가 지워집니다</div></td></tr>")
    body.append("</tbody></table></div>")

    inverse: dict[str, list[tuple[str, str]]] = {}
    for r, rel in rows:
        for lb in r["labels"]:
            inverse.setdefault(lb, []).append((r["title"] or r["name"], rel))
    if inverse:
        body.append(f"<details><summary>인물·항목으로 찾기 {len(inverse)}건"
                    "</summary><p>")
        for lb in sorted(inverse):
            links = " · ".join(
                f'<a href="{quote(rel)}/view.html">{html.escape(title)}</a>'
                for title, rel in sorted(set(inverse[lb]), key=lambda item: item[1])
            )
            body.append(f'<span class="chip"><b>{html.escape(lb)}</b> → '
                        f"{links}</span><br>")
        body.append("</p></details>")
    body.append("</div>")  # close #list-view

    graph_payload, dropped_entities = _graph_payload(sorted_rows)
    truncation_note = (
        f" · 연결 많은 인물·항목 상위 {_GRAPH_ENTITY_MAX}개만 표시"
        f"(나머지 {dropped_entities}개는 목록·검색에서)"
        if dropped_entities else ""
    )
    body.append(
        '<div id="graph-view" hidden><div class="graph-wrap">'
        '<canvas tabindex="0" role="application" '
        'aria-label="영상과 인물의 연결 그래프 — 목록과 같은 내용입니다. '
        '화살표 키로 점 사이를 이동하고 Enter로 엽니다" '
        'aria-describedby="graph-hint"></canvas>'
        '<div class="graph-tip"></div>'
        '<p class="graph-hint" id="graph-hint">점 클릭: 영상 열기·인물 검색 · '
        '끌기: 자리 이동 · 휠: 확대 · 화살표 키: 점 이동'
        f"{truncation_note}</p>"
        "</div></div>")
    body.append('<script type="application/json" id="graph-data">'
                + graph_payload + "</script>")

    dest = root / "view.html"
    write_text_atomic(dest, _page("코퍼스 브라우저", body, _CORPUS_JS))
    save_projection_cache(root, cache, live_keys={rel for _, rel in rows})
    return dest, len(rows)


