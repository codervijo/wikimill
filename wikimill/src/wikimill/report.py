"""The report — one self-contained HTML file (v3.B/C).

Two jobs, and they are not the same job:

1. **The domains found**, filterable, so the operator can interrogate a result
   set of thousands without exporting it to a spreadsheet first.
2. **What the pipeline is doing right now**, from the heartbeat — including
   whether a long stage has stopped moving.

The second is why this file regenerates rather than being a one-shot artefact.
A crawl runs for hours; "is it stuck?" is a question asked *during* that, not
after, and answering it should not mean tailing a log and squinting at
timestamps.

## Constraints, all of them deliberate

* **No network at all.** No CDN, no webfont, no analytics, no external image.
  The file opens from `outputs/` on a laptop with the wifi off, and it must
  still work in five years when whatever CDN was fashionable has gone away.
* **Inline everything.** One file, copyable and mailable, with no sidecar
  assets to lose.
* **The data is deterministic.** Same database, same rows, same order — so two
  reports diff meaningfully. The generated-at stamp is rendered but is not part
  of what determinism is claimed about, exactly as with the CSV digest.
* **CC BY-SA attribution travels with it.** Anchor text, section names and
  article titles are Wikipedia excerpts here just as they are in the export
  (§17), so the same notice and the same per-row article links appear.
* **Filtering is client-side.** The page is a file, not a service; there is
  nothing to query. Everything needed is embedded, and the JavaScript only
  hides rows.

## On the visual language

It is built from this project's own `✓ ✗ ↷` markers, which the operator already
reads fluently from the terminal. Green is a settled good state, red is
operator-action-needed, amber is transient or soft-skipped — the same meanings
they carry in `logging.py`, so nothing new has to be learned to read the page.
Monospace throughout, because every value on it is an identifier, a count, or a
timestamp, and because this is the report of a command-line tool rather than a
brochure for one.
"""

from __future__ import annotations

import html
import json
import sqlite3
from dataclasses import dataclass, field

from . import progress as progress_mod
from .constants import CLASSIFIER_VERSION, DomainState
from .logging import utcnow

# Candidate rows embedded in the page. Filtering is client-side over this
# array, so it is bounded: past a few thousand the browser, not the crawler,
# becomes the slow part — and an operator scrolling 50,000 rows was going to
# filter them anyway.
MAX_ROWS = 3_000


@dataclass
class ReportData:
    generated_at: str = ""
    db_path: str = ""
    schema_version: int = 0
    policy_version: str = ""
    dump_runs: list[str] = field(default_factory=list)
    funnel: list[tuple[str, int, str]] = field(default_factory=list)
    states: list[tuple[str, int]] = field(default_factory=list)
    stages: list[progress_mod.StageView] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    total_candidates: int = 0
    truncated: bool = False

    @property
    def live(self) -> list[progress_mod.StageView]:
        return [s for s in self.stages if s.running]

    @property
    def stalled(self) -> list[progress_mod.StageView]:
        return [s for s in self.stages if s.stalled]


CANDIDATE_STATES = (
    DomainState.UNREGISTERED,
    DomainState.EXPIRING,
    DomainState.FOR_SALE,
    DomainState.PARKED,
    DomainState.NO_RDAP_FOR_TLD,
)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        return int(conn.execute(sql, params).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def collect(conn: sqlite3.Connection, cfg=None, states: list[str] | None = None,
            min_pages: int = 1, limit: int = MAX_ROWS) -> ReportData:
    """Everything the page shows, in one pass over the database."""
    wanted = list(states or CANDIDATE_STATES)
    data = ReportData(
        generated_at=utcnow(),
        db_path=str(cfg.db_path) if cfg else "",
        schema_version=_scalar(conn, "PRAGMA user_version"),
    )
    data.dump_runs = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT dump_run FROM wiki_pages ORDER BY dump_run"
        )
    ]

    # The funnel is the argument for the whole pipeline ordering, so it leads.
    links = _scalar(conn, "SELECT COUNT(*) FROM external_links")
    urls = _scalar(conn, "SELECT COUNT(*) FROM urls")
    domains = _scalar(conn, "SELECT COUNT(*) FROM domains")
    crawled = _scalar(conn, "SELECT COUNT(*) FROM urls WHERE last_checked IS NOT NULL")
    checked = _scalar(conn, "SELECT COUNT(*) FROM domains WHERE last_checked IS NOT NULL")
    candidates = _scalar(
        conn,
        "SELECT COUNT(*) FROM domains WHERE state IN "
        "(" + ",".join("?" * len(wanted)) + ") AND wiki_page_count >= ? "
        "AND registrable_domain != ''",
        (*wanted, min_pages),
    )
    pages = _scalar(conn, "SELECT COUNT(DISTINCT page_id) FROM wiki_pages")
    data.funnel = [
        ("Wikipedia pages ingested", pages, ""),
        ("external links", links, ""),
        ("distinct URLs", urls, _pct(urls, links)),
        ("registrable domains", domains, _pct(domains, urls)),
        ("URLs crawled", crawled, _pct(crawled, urls)),
        ("domains checked (DNS + RDAP)", checked, _pct(checked, domains)),
        ("candidates", candidates, _pct(candidates, domains)),
    ]
    data.total_candidates = candidates

    data.states = [
        (r["state"], r["n"]) for r in conn.execute(
            "SELECT state, COUNT(*) n FROM domains GROUP BY state ORDER BY n DESC"
        )
    ]
    data.stages = progress_mod.snapshot(conn, data.generated_at)

    try:
        from .policy import load as load_policy
        data.policy_version = (
            load_policy(cfg.root).effective_classifier_version if cfg
            else str(CLASSIFIER_VERSION)
        )
    except Exception:  # noqa: BLE001 — a bad config must not block the report
        data.policy_version = str(CLASSIFIER_VERSION)

    data.rows = _rows(conn, wanted, min_pages, limit)
    data.truncated = candidates > len(data.rows)
    return data


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.2f}%" if whole else ""


def _rows(conn: sqlite3.Connection, states: list[str], min_pages: int,
          limit: int) -> list[dict]:
    """Candidate rows with their evidence. Same ordering as the CSV export, so
    the page and the file agree about what "the top candidate" means."""
    from . import diff as diff_mod
    from . import verify as verify_mod

    sql = (
        "SELECT domain_id, registrable_domain, state, candidate_score, "
        " score_explanation, wiki_page_count, wiki_link_count, last_checked, "
        " public_suffix, is_private_suffix "
        "FROM domains WHERE state IN (" + ",".join("?" * len(states)) + ") "
        "AND wiki_page_count >= ? AND registrable_domain != '' "
        "ORDER BY candidate_score DESC, wiki_page_count DESC, registrable_domain "
        "LIMIT ?"
    )
    out: list[dict] = []
    for row in conn.execute(sql, (*states, min_pages, limit)):
        did = row["domain_id"]
        check = conn.execute(
            "SELECT registrar, registration_expiry FROM domain_checks "
            "WHERE domain_id=? ORDER BY id DESC LIMIT 1", (did,)
        ).fetchone()
        cite = conn.execute(
            "SELECT p.title, e.section, e.anchor_text FROM external_links e "
            "JOIN urls u ON u.url_hash = e.url_hash "
            "JOIN wiki_pages p ON p.page_id = e.page_id "
            "WHERE u.domain_id = ? ORDER BY e.enrich_status = 'done' DESC, e.id "
            "LIMIT 1", (did,)
        ).fetchone()
        usage = verify_mod.latest(conn, did)

        title = cite["title"] if cite else ""
        section = (cite["section"] if cite else None) or ""
        article_url = ""
        if title:
            anchor = title.replace(" ", "_")
            article_url = f"https://en.wikipedia.org/wiki/{anchor}"
            if section:
                article_url += "#" + section.replace(" ", "_")

        reasons = []
        if row["score_explanation"]:
            try:
                reasons = [
                    f"{c['name']} {c['points']:+d} — {c['detail']}"
                    for c in json.loads(row["score_explanation"])["components"]
                ]
            except (ValueError, KeyError, TypeError):
                reasons = []

        live_count = ""
        if usage is not None and usage["live_page_count"] is not None:
            live_count = (f"{usage['live_page_count']}+" if usage["truncated"]
                          else usage["live_page_count"])

        out.append({
            "domain": row["registrable_domain"],
            "state": row["state"],
            "score": round(row["candidate_score"] or 0, 1),
            "pages": row["wiki_page_count"] or 0,
            "links": row["wiki_link_count"] or 0,
            "suffix": row["public_suffix"] or "",
            "private": bool(row["is_private_suffix"]),
            "registrar": (check["registrar"] if check else "") or "",
            "expiry": (check["registration_expiry"] if check else "") or "",
            "checked": row["last_checked"] or "",
            "article": title,
            "article_url": article_url,
            "section": section,
            "anchor": (cite["anchor_text"] if cite else "") or "",
            "removed": diff_mod.removal_counts(conn, did) or "",
            "live": live_count,
            "why": reasons,
        })
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


_CSS = """
:root{
  --ink:#15171c; --ink-2:#3f4550; --ink-3:#6b7280;
  --paper:#f7f6f3; --card:#ffffff; --line:#dedbd4;
  --ok:#1a7f52; --bad:#b3261e; --warn:#9a6200;
  --ok-bg:#e6f2ec; --bad-bg:#fbeae9; --warn-bg:#fbf1e0;
  --accent:#1f4d7a;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --ink:#e7e5e0; --ink-2:#b3b0a9; --ink-3:#83807a;
    --paper:#14161a; --card:#1b1e24; --line:#2c3037;
    --ok:#5fd39b; --bad:#ff8a80; --warn:#e8b45c;
    --ok-bg:#123227; --bad-bg:#3a1d1b; --warn-bg:#33280f;
    --accent:#7fb0e0;
  }
}
:root[data-theme="dark"]{
  --ink:#e7e5e0; --ink-2:#b3b0a9; --ink-3:#83807a;
  --paper:#14161a; --card:#1b1e24; --line:#2c3037;
  --ok:#5fd39b; --bad:#ff8a80; --warn:#e8b45c;
  --ok-bg:#123227; --bad-bg:#3a1d1b; --warn-bg:#33280f;
  --accent:#7fb0e0;
}
:root[data-theme="light"]{
  --ink:#15171c; --ink-2:#3f4550; --ink-3:#6b7280;
  --paper:#f7f6f3; --card:#ffffff; --line:#dedbd4;
  --ok:#1a7f52; --bad:#b3261e; --warn:#9a6200;
  --ok-bg:#e6f2ec; --bad-bg:#fbeae9; --warn-bg:#fbf1e0;
  --accent:#1f4d7a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--mono);font-size:13px;line-height:1.5;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 72px}
h1{font-size:19px;margin:0;letter-spacing:-.01em}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.12em;
  color:var(--ink-3);margin:32px 0 10px;font-weight:600}
a{color:var(--accent)}
.sub{color:var(--ink-3);font-size:12px;margin-top:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px}
.pad{padding:14px 16px}
.grid{display:grid;gap:10px}
.marker{display:inline-block;width:1.1em;font-weight:700}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
  border:1px solid transparent;white-space:nowrap}
.pill.ok{background:var(--ok-bg);border-color:var(--ok);color:var(--ok)}
.pill.bad{background:var(--bad-bg);border-color:var(--bad);color:var(--bad)}
.pill.warn{background:var(--warn-bg);border-color:var(--warn);color:var(--warn)}
.bar{height:6px;background:var(--line);border-radius:99px;overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;background:var(--ok);transition:width .3s}
.bar.stalled>i{background:var(--bad)}
.stage{display:flex;justify-content:space-between;gap:16px;align-items:baseline;
  flex-wrap:wrap}
.stage .what{color:var(--ink-3);font-size:12px;
  overflow-wrap:anywhere;max-width:100%}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);
  vertical-align:top}
th{color:var(--ink-3);font-weight:600;font-size:11px;text-transform:uppercase;
  letter-spacing:.08em;cursor:pointer;user-select:none;white-space:nowrap;
  position:sticky;top:0;background:var(--card)}
th[data-sort]:after{content:" \\2195";opacity:.3}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:hover{background:var(--paper)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px;
  background:var(--card);max-height:76vh;overflow-y:auto}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
input[type=search],select{font-family:var(--mono);font-size:12px;padding:6px 9px;
  border:1px solid var(--line);border-radius:5px;background:var(--card);
  color:var(--ink)}
input[type=search]{min-width:220px;flex:1}
button{font-family:var(--mono);font-size:11px;padding:5px 10px;cursor:pointer;
  border:1px solid var(--line);border-radius:99px;background:var(--card);
  color:var(--ink-2)}
button[aria-pressed=true]{border-color:var(--accent);color:var(--accent);
  background:var(--card);font-weight:700}
.count{color:var(--ink-3);font-size:12px;margin-left:auto}
.why{color:var(--ink-3);font-size:11px}
details summary{cursor:pointer;color:var(--ink-3)}
.foot{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--ink-3);font-size:11px}
.foot a{color:var(--ink-3)}
.empty{padding:40px 16px;text-align:center;color:var(--ink-3)}
@media(max-width:640px){.wrap{padding:18px 12px 56px}h1{font-size:17px}}
"""

_JS = """
(function(){
  var rows=Array.prototype.slice.call(document.querySelectorAll('#rows tr'));
  var q=document.getElementById('q'), out=document.getElementById('count');
  var active=new Set(), dir={}, tbody=document.getElementById('rows');
  function apply(){
    var text=(q.value||'').toLowerCase(), shown=0;
    rows.forEach(function(tr){
      var okState=active.size===0||active.has(tr.dataset.state);
      var okText=!text||tr.dataset.search.indexOf(text)>=0;
      var vis=okState&&okText;
      tr.hidden=!vis; if(vis)shown++;
    });
    out.textContent=shown.toLocaleString()+' of '+rows.length.toLocaleString()+' shown';
  }
  q.addEventListener('input',apply);
  document.querySelectorAll('[data-state-filter]').forEach(function(b){
    b.addEventListener('click',function(){
      var s=b.dataset.stateFilter;
      if(active.has(s)){active.delete(s);b.setAttribute('aria-pressed','false');}
      else{active.add(s);b.setAttribute('aria-pressed','true');}
      apply();
    });
  });
  document.querySelectorAll('th[data-sort]').forEach(function(th){
    th.addEventListener('click',function(){
      var k=th.dataset.sort, num=th.classList.contains('num');
      dir[k]=dir[k]==='asc'?'desc':'asc';
      var s=dir[k]==='asc'?1:-1;
      rows.sort(function(a,b){
        var x=a.dataset[k]||'', y=b.dataset[k]||'';
        if(num){return (parseFloat(x)||0)-(parseFloat(y)||0)>0?s:-s;}
        return x.localeCompare(y)*s;
      });
      rows.forEach(function(r){tbody.appendChild(r);});
    });
  });
  apply();
})();
"""


def _stage_html(stage: progress_mod.StageView) -> str:
    marker, cls = ("✓", "ok")
    if stage.stalled:
        marker, cls = "✗", "bad"
    elif stage.running:
        marker, cls = "↷", "warn"
    elif stage.outcome and stage.outcome not in ("ok",):
        marker, cls = "✗", "bad"

    bits = [f"{stage.done:,}"]
    if stage.total:
        bits.append(f"of {stage.total:,}")
        if stage.percent is not None:
            bits.append(f"({stage.percent:.1f}%)")
    if stage.rate_per_second:
        bits.append(f"· {stage.rate_per_second:.2f}/s")
    if stage.running and stage.eta_seconds:
        bits.append(f"· ~{_duration(stage.eta_seconds)} left")

    detail = ""
    if stage.stalled:
        # The headline this whole module exists for.
        detail = (f"<div class='what bad'>no progress for "
                  f"{_duration(stage.age_seconds)} — last item: "
                  f"{e(stage.current_item or 'unknown')}</div>")
    elif stage.running and stage.current_item:
        detail = f"<div class='what'>{e(stage.current_item)}</div>"
    elif stage.note:
        detail = f"<div class='what'>{e(stage.note)}</div>"

    bar = ""
    if stage.percent is not None:
        klass = "bar stalled" if stage.stalled else "bar"
        bar = f"<div class='{klass}'><i style='width:{stage.percent:.1f}%'></i></div>"

    return (
        f"<div class='card pad' style='margin-bottom:8px'>"
        f"<div class='stage'>"
        f"<div><span class='marker {cls}'>{marker}</span> <b>{e(stage.stage)}</b>"
        f" <span class='what'>{e(stage.phase or '')}</span></div>"
        f"<div><span class='pill {cls}'>{e(stage.status)}</span> "
        f"<span class='what'>{e(' '.join(bits))}</span></div>"
        f"</div>{bar}{detail}</div>"
    )


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def render(data: ReportData, refresh_seconds: int = 0) -> str:
    """The whole page, as one string. No external references of any kind."""
    # Only auto-refresh while something is actually running: a page that
    # reloads forever fights the operator who is trying to read it.
    refresh = ""
    if refresh_seconds and data.live:
        refresh = f"<meta http-equiv='refresh' content='{int(refresh_seconds)}'>"

    if data.stages:
        stages = "".join(_stage_html(s) for s in data.stages[:8])
    else:
        stages = ("<div class='card pad what'>No stage has reported progress yet. "
                  "Run <b>crawl</b> or <b>check</b> and reload.</div>")

    funnel = "".join(
        f"<tr><td>{e(label)}</td><td class='num'>{value:,}</td>"
        f"<td class='num what'>{e(pct)}</td></tr>"
        for label, value, pct in data.funnel
    )
    states = "".join(
        f"<tr><td>{e(name)}</td><td class='num'>{n:,}</td></tr>"
        for name, n in data.states
    )

    present = sorted({r["state"] for r in data.rows})
    filters = "".join(
        f"<button data-state-filter='{e(s)}' aria-pressed='false'>{e(s)}</button>"
        for s in present
    )

    body = []
    for r in data.rows:
        search = " ".join(str(r[k]) for k in
                          ("domain", "state", "suffix", "registrar", "article")).lower()
        article = (f"<a href='{e(r['article_url'])}' rel='noreferrer'>"
                   f"{e(r['article'])}</a>" if r["article_url"]
                   else e(r["article"]))
        if r["section"]:
            article += f" <span class='what'>§{e(r['section'])}</span>"
        why = ""
        if r["why"]:
            why = ("<details><summary>why</summary><div class='why'>"
                   + "<br>".join(e(x) for x in r["why"]) + "</div></details>")
        body.append(
            f"<tr data-state='{e(r['state'])}' data-domain='{e(r['domain'])}' "
            f"data-score='{r['score']}' data-pages='{r['pages']}' "
            f"data-search='{e(search)}'>"
            f"<td><b>{e(r['domain'])}</b>"
            f"{' <span class=\"pill warn\">private suffix</span>' if r['private'] else ''}"
            f"{why}</td>"
            f"<td><span class='pill {_state_class(r['state'])}'>{e(r['state'])}</span></td>"
            f"<td class='num'>{r['score']}</td>"
            f"<td class='num'>{r['pages']:,}</td>"
            f"<td class='num'>{e(r['live'])}</td>"
            f"<td class='num'>{e(r['removed'])}</td>"
            f"<td>{e(r['registrar'])}</td>"
            f"<td>{e(r['expiry'][:10])}</td>"
            f"<td>{article}</td></tr>"
        )
    rows_html = "".join(body) or ""

    empty = ("<div class='empty'>No candidates yet. Run "
             "<b>ingest</b> → <b>check</b> to populate this table.</div>"
             if not data.rows else "")

    truncated = ""
    if data.truncated:
        truncated = (f"<div class='what' style='margin-top:8px'>↷ showing the top "
                     f"{len(data.rows):,} of {data.total_candidates:,} — the rest are "
                     f"in the CSV export, not silently dropped.</div>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}
<title>wikimill — {data.total_candidates:,} candidates</title>
<style>{_CSS}</style></head><body><div class="wrap">

<h1>wikimill</h1>
<div class="sub">
  {data.total_candidates:,} candidate domain(s) ·
  dump run {e(', '.join(data.dump_runs) or 'none')} ·
  schema v{data.schema_version} · policy {e(data.policy_version)}<br>
  generated {e(data.generated_at)}
  {" · auto-refreshing while a stage is running" if refresh else ""}
</div>

<h2>Pipeline</h2>
{stages}

<h2>Corpus funnel</h2>
<div class="scroll"><table>
<thead><tr><th>stage</th><th class="num">count</th><th class="num">of previous</th></tr></thead>
<tbody>{funnel}</tbody></table></div>

<h2>Domains by state</h2>
<div class="scroll"><table>
<thead><tr><th>state</th><th class="num">domains</th></tr></thead>
<tbody>{states}</tbody></table></div>

<h2>Candidates</h2>
<div class="controls">
  <input type="search" id="q" placeholder="filter by domain, registrar, suffix, article…">
  {filters}
  <span class="count" id="count"></span>
</div>
<div class="scroll"><table>
<thead><tr>
  <th data-sort="domain">domain</th>
  <th data-sort="state">state</th>
  <th class="num" data-sort="score">score</th>
  <th class="num" data-sort="pages">wiki pages</th>
  <th class="num">live</th>
  <th class="num">removed</th>
  <th>registrar</th>
  <th>expiry</th>
  <th>example citation</th>
</tr></thead>
<tbody id="rows">{rows_html}</tbody></table>{empty}</div>
{truncated}

<div class="foot">
  Article titles, section names and anchor text above are excerpts of Wikipedia
  content, licensed <a href="https://creativecommons.org/licenses/by-sa/4.0/"
  rel="noreferrer">CC BY-SA 4.0</a> (also GFDL). Each row links to its source
  article for attribution; redistribution requires attribution and share-alike.
  <br><br>
  Generated offline by wikimill. This file makes no network requests — every
  style and script is inline, and the only links are to Wikipedia.
</div>

</div><script>{_JS}</script></body></html>"""


def _state_class(state: str) -> str:
    if state in (DomainState.UNREGISTERED, DomainState.EXPIRING):
        return "ok"
    if state == DomainState.ACTIVE:
        return "bad"
    return "warn"
