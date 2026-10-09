#!/usr/bin/env python3
"""Regenera la sección de PRs merged del README y arma el resumen semanal por mail.

- Solo incluye PRs MERGED en la tabla del README (nunca abiertos/pendientes).
- Excluye repos propios (owner == username): cuentan solo contribuciones a otros.
- PRs merged = unión de la Search API y GraphQL (si una devuelve un resultado parcial,
  la otra lo cubre); config, umbrales y descripciones curadas viven en config.json.
- Reemplaza solo lo que está entre los marcadores PRS:START / PRS:END.
- El mail (asunto + HTML) sale por GITHUB_OUTPUT como email_subject / email_html y lo manda
  mailer.py en otro step, DESPUÉS del commit.
- Si algo es sospechoso (resultado parcial, dato de estrellas inventable) aborta con
  exit 1 y deja el motivo en GITHUB_OUTPUT (abort_reason) para el mail de alerta.

Uso local:  GITHUB_TOKEN=$(gh auth token) python3 scripts/update_readme_prs.py
En CI:      el workflow exporta GITHUB_TOKEN automáticamente.
"""

import html
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import ghapi

HERE = os.path.dirname(os.path.abspath(__file__))
README = os.path.join(HERE, "..", "README.md")
STATE_PATH = os.path.join(HERE, "weekly_state.json")
CONFIG_PATH = os.path.join(HERE, "config.json")
START = "<!-- PRS:START -->"
END = "<!-- PRS:END -->"
NBSP = chr(0xA0)  # por código, no literal: un NBSP crudo en el fuente es invisible y se pierde al editar

DEFAULT_CONFIG = {
    "username": "santichausis",
    "summary_suffix": "auto-updated weekly",
    "recent_closed_days": 10,
    "min_retention_ratio": 0.9,
    "stale_days": 14,
    "aged_days": 30,
    "top_star_changes": 5,
    "star_milestones": [1000, 5000, 10000, 50000, 100000],
    "pr_overrides": {},
}


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return {**DEFAULT_CONFIG, **json.load(f)}


CONFIG = load_config()
USERNAME = CONFIG["username"]
SUMMARY_SUFFIX = CONFIG["summary_suffix"]
RECENT_CLOSED_DAYS = CONFIG["recent_closed_days"]
MIN_RETENTION_RATIO = CONFIG["min_retention_ratio"]
STALE_DAYS = CONFIG["stale_days"]
AGED_DAYS = CONFIG["aged_days"]
TOP_STAR_CHANGES = CONFIG["top_star_changes"]
STAR_MILESTONES = CONFIG["star_milestones"]
PR_OVERRIDES = CONFIG["pr_overrides"]


# ---------------------------------------------------------------- utilidades

def utc_today():
    return datetime.now(timezone.utc).date()


def _parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def write_github_output(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            if "\n" in str(value):
                # Salida multilínea: sintaxis heredoc de GitHub Actions.
                f.write(f"{key}<<GHA_EOF\n{value}\nGHA_EOF\n")
            else:
                f.write(f"{key}={value}\n")


def abort(reason):
    """Corta el run sin tocar nada y deja el motivo para el mail de alerta."""
    reason = " ".join(str(reason).split())
    print(f"ABORT: {reason}", file=sys.stderr)
    write_github_output({"abort_reason": reason})
    sys.exit(1)


def safe(fn, default, label, warnings):
    """Para datos que solo alimentan el mail: si fallan, el run sigue y el mail lo avisa."""
    try:
        return fn()
    except ghapi.API_ERRORS as e:
        warnings.append(f"{label} unavailable ({e}).")
        return default


# ---------------------------------------------------------------- fetch

def search_prs(query):
    """Pagina /search/issues con la query dada, devuelve los items crudos."""
    items = []
    page = 1
    while True:
        data = ghapi.api("/search/issues", {"q": query, "per_page": 100, "page": page})
        batch = data.get("items", [])
        items.extend(batch)
        if len(items) >= data.get("total_count", 0) or not batch:
            break
        page += 1
    return items


def parse_pr_url(html_url):
    # https://github.com/OWNER/REPO/pull/NUM
    parts = html_url.split("github.com/", 1)[1].split("/")
    return parts[0], parts[1], parts[3]


def _pr_dict(owner, repo, num, title, url, created_at, merged_at, closed_at, draft=False):
    return {
        "owner": owner,
        "repo": repo,
        "full": f"{owner}/{repo}",
        "num": int(num),
        "title": clean_title(title),
        "url": url,
        "created_at": created_at or "",
        "merged_at": merged_at or "",
        "closed_at": closed_at or "",
        "draft": bool(draft),
    }


def to_pr(it):
    owner, repo, num = parse_pr_url(it["html_url"])
    return _pr_dict(
        owner, repo, num, it["title"], it["html_url"], it.get("created_at"),
        (it.get("pull_request") or {}).get("merged_at"), it.get("closed_at"), it.get("draft"),
    )


def _not_own(p):
    return p["owner"].lower() != USERNAME.lower()


def fetch_prs(query_suffix):
    """PRs del usuario que matchean la query, excluyendo repos propios."""
    items = search_prs(f"author:{USERNAME} type:pr {query_suffix}")
    return [p for p in map(to_pr, items) if _not_own(p)]


MERGED_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    pullRequests(states: MERGED, first: 100, after: $cursor,
                 orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url createdAt mergedAt closedAt repository { nameWithOwner } }
    }
  }
}
"""


def fetch_merged_prs_graphql():
    prs, cursor = [], None
    while True:
        data = ghapi.graphql(MERGED_QUERY, {"login": USERNAME, "cursor": cursor})
        conn = data["user"]["pullRequests"]
        for n in conn["nodes"]:
            owner, repo = n["repository"]["nameWithOwner"].split("/", 1)
            prs.append(_pr_dict(owner, repo, n["number"], n["title"], n["url"],
                                n["createdAt"], n["mergedAt"], n["closedAt"]))
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return [p for p in prs if _not_own(p)]


def _pr_key(p):
    return f'{p["full"]}#{p["num"]}'.lower()


def merge_sources(search_list, graphql_list):
    """Unión por owner/repo#num. Devuelve (lista, solo_en_search, solo_en_graphql)."""
    by_key = {_pr_key(p): p for p in search_list}
    for p in graphql_list:
        by_key.setdefault(_pr_key(p), p)
    s = {_pr_key(p) for p in search_list}
    g = {_pr_key(p) for p in graphql_list}
    return list(by_key.values()), sorted(s - g), sorted(g - s)


def fetch_merged_prs(warnings):
    """Search + GraphQL, unidas: merged es monótono, así que la unión no puede 'inventar'
    PRs y sí cubre una fuente que devuelva un resultado parcial."""
    search_list = graphql_list = None
    try:
        search_list = fetch_prs("is:merged")
    except ghapi.API_ERRORS as e:
        warnings.append(f"Search API failed for merged PRs ({e}).")
    try:
        graphql_list = fetch_merged_prs_graphql()
    except (*ghapi.API_ERRORS, RuntimeError, KeyError) as e:
        warnings.append(f"GraphQL unavailable for merged PRs ({e}).")
    if search_list is None and graphql_list is None:
        abort("Both Search and GraphQL failed while fetching merged PRs.")

    merged, only_s, only_g = merge_sources(search_list or [], graphql_list or [])
    n_s = "fail" if search_list is None else len(search_list)
    n_g = "fail" if graphql_list is None else len(graphql_list)
    print(f"Fuentes de PRs merged — Search: {n_s} · GraphQL: {n_g} · unión: {len(merged)}")
    if search_list is not None and graphql_list is not None and (only_s or only_g):
        warnings.append(
            f"Search and GraphQL disagree (only Search: {only_s or 'none'}; "
            f"only GraphQL: {only_g or 'none'}) — using the union."
        )
    return merged


def fetch_open_prs():
    return fetch_prs("is:open")


def fetch_recently_closed_unmerged(days=None, today=None):
    days = RECENT_CLOSED_DAYS if days is None else days
    since = ((today or utc_today()) - timedelta(days=days)).isoformat()
    return fetch_prs(f"is:closed is:unmerged closed:>={since}")


def fetch_follower_count():
    return ghapi.api(f"/users/{USERNAME}").get("followers") or 0


def fetch_repo_meta(repos, known_stars, warnings):
    """Estrellas/forks por repo. Si un repo falla se usa el último valor conocido; si es un
    repo nuevo y falla, se aborta: nunca se publica (ni se guarda como base) un número
    inventado — antes un error caía a 0 estrellas en silencio."""
    meta = {}
    for r in repos:
        try:
            data = ghapi.api(f"/repos/{r}")
        except ghapi.API_ERRORS as e:
            known = known_stars.get(r)
            if known is None:
                abort(f"Could not fetch stars for new repo {r} ({e}) and there is no "
                      f"previous value — refusing to publish a made-up number.")
            warnings.append(f"Stars for {r} could not be fetched ({e}); showing last known value ({known}).")
            meta[r] = {"stars": known, "forks": 0, "stale": True}
            continue
        meta[r] = {
            "stars": data.get("stargazers_count") or 0,
            "forks": data.get("forks_count") or 0,
            "stale": False,
        }
    return meta


# ---------------------------------------------------------------- actividad de PRs abiertos

def _user_fields(user):
    user = user or {}
    login = user.get("login") or ""
    return login, user.get("type") == "Bot" or login.endswith("[bot]")


def pr_events(comments, reviews, review_comments, commits):
    """Normaliza la actividad de un PR a eventos {who, bot, at, kind, state}."""
    events = []
    for c in comments:
        who, bot = _user_fields(c.get("user"))
        events.append({"who": who, "bot": bot, "at": _parse_ts(c["created_at"]),
                       "kind": "comment", "state": ""})
    for r in reviews:
        if r.get("state") == "PENDING" or not r.get("submitted_at"):
            continue
        who, bot = _user_fields(r.get("user"))
        events.append({"who": who, "bot": bot, "at": _parse_ts(r["submitted_at"]),
                       "kind": "review", "state": r.get("state", "")})
    for c in review_comments:
        who, bot = _user_fields(c.get("user"))
        events.append({"who": who, "bot": bot, "at": _parse_ts(c["created_at"]),
                       "kind": "review_comment", "state": ""})
    for c in commits:
        who, bot = _user_fields(c.get("author"))
        events.append({"who": who, "bot": bot, "at": _parse_ts(c["commit"]["committer"]["date"]),
                       "kind": "commit", "state": ""})
    return events


def fetch_pr_events(pr):
    base = f"/repos/{pr['full']}"
    n = pr["num"]
    return pr_events(
        ghapi.paginate(f"{base}/issues/{n}/comments"),
        ghapi.paginate(f"{base}/pulls/{n}/reviews"),
        ghapi.paginate(f"{base}/pulls/{n}/comments"),
        ghapi.paginate(f"{base}/pulls/{n}/commits"),
    )


def classify_pr(pr, events, today, stale_days=None):
    """Estado de un PR abierto según el ORDEN de los eventos humanos (los bots no cuentan):

    - changes_requested / awaiting_reply: alguien contestó DESPUÉS de tu última actividad
      (comentario, review o commit) y falta tu respuesta. Un "changes requested" que ya
      respondiste NO cuenta (la búsqueda `review:changes_requested` sí lo marca).
    - stale: la pelota está del lado de los maintainers y pasaron >= stale_days sin nada.
    - draft / ok.
    Aprobaciones y commits ajenos no piden respuesta tuya."""
    stale_days = STALE_DAYS if stale_days is None else stale_days
    me = USERNAME.lower()
    created = _parse_ts(pr["created_at"])
    mine, theirs = [], []
    for e in events:
        if e["bot"]:
            continue
        is_mine = e["who"].lower() == me or (e["kind"] == "commit" and not e["who"])
        (mine if is_mine else theirs).append(e)

    my_last = max([created] + [e["at"] for e in mine])
    asks = [e for e in theirs
            if e["kind"] != "commit" and e["state"] not in ("APPROVED", "DISMISSED")]
    ask_last = max(asks, key=lambda e: e["at"], default=None)
    last_any = max([my_last] + [e["at"] for e in theirs])

    latest_review = {}
    for e in sorted((e for e in theirs if e["kind"] == "review"), key=lambda e: e["at"]):
        latest_review[e["who"]] = e["state"]
    approved = "APPROVED" in latest_review.values() and "CHANGES_REQUESTED" not in latest_review.values()

    who, since = "", last_any
    if pr.get("draft"):
        state = "draft"
    elif ask_last and ask_last["at"] > my_last:
        cr = max((e for e in theirs if e["kind"] == "review"
                  and e["state"] == "CHANGES_REQUESTED" and e["at"] > my_last),
                 key=lambda e: e["at"], default=None)
        state = "changes_requested" if cr else "awaiting_reply"
        who = (cr or ask_last)["who"]
        since = (cr or ask_last)["at"]
    elif (today - last_any.date()).days >= stale_days:
        state = "stale"
    else:
        state = "ok"
    return {"state": state, "idle_days": (today - since.date()).days, "who": who, "approved": approved}


def fetch_open_pr_status(open_prs, today):
    """Cada PR abierto con su 'status'. Si falla la lectura de uno, queda 'unknown' (no se
    inventa un estado) y se avisa en el mail."""
    out, warnings = [], []
    for p in open_prs:
        try:
            status = classify_pr(p, fetch_pr_events(p), today)
        except (*ghapi.API_ERRORS, KeyError, ValueError, TypeError) as e:
            status = {"state": "unknown", "idle_days": None, "who": "", "approved": False}
            warnings.append(f"Could not read activity for {p['full']}#{p['num']} ({e}).")
        out.append({**p, "status": status})
    return out, warnings


# ---------------------------------------------------------------- formato

def clean_title(t):
    # Saca prefijos tipo "Fix:", "feat:", "Refactor:" para que quede más limpio.
    for sep in (": ", "/ "):
        head = t.split(sep, 1)[0].lower()
        if head in {"fix", "feat", "feature", "refactor", "chore", "docs", "frontend"} and sep in t:
            t = t.split(sep, 1)[1]
            break
    return t[:1].upper() + t[1:] if t else t


def desc_for(pr):
    return PR_OVERRIDES.get(f'{pr["full"]}#{pr["num"]}', pr["title"])


def md_table_cell(s):
    # Escapa el pipe: dentro de una tabla markdown "|" sin escapar corta la celda.
    return s.replace("|", "\\|")


def logo(owner, width=20):
    return f'<img src="https://github.com/{owner}.png?size=40" width="{width}" align="top"/>'


def format_stars(n):
    if n < 1000:
        return str(n)
    s = f"{n / 1000:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return f"{s}k"


def days_since(iso_date, today=None):
    if not iso_date:
        return None
    return ((today or utc_today()) - date.fromisoformat(iso_date[:10])).days


def build_section(prs, meta):
    by_repo = defaultdict(list)
    for p in prs:
        by_repo[p["full"]].append(p)
    for repo_prs in by_repo.values():
        repo_prs.sort(key=lambda p: p["merged_at"], reverse=True)

    # Orden de repos por popularidad: estrellas y, a igualdad, forks (desc).
    repos = sorted(by_repo, key=lambda r: (meta[r]["stars"], meta[r]["forks"]), reverse=True)

    out = [
        f"**{len(prs)} PRs merged · {len(repos)} repos** · {SUMMARY_SUFFIX}",
        "",
        "| Repo | Stars | Merged PRs | Latest merge |",
        "|---|---|:---:|---|",
    ]
    for full in repos:
        owner = full.split("/")[0]
        rprs = by_repo[full]
        repo_cell = f"{logo(owner)} [{full}](https://github.com/{full})"
        # NBSP (chr(0xA0)) y no un espacio normal: evita que el navegador parta "⭐ 72.7k" en dos renglones.
        stars_cell = f"⭐{NBSP}{format_stars(meta[full]['stars'])}"
        q = f"https://github.com/{full}/pulls?q=is%3Apr+author%3A{USERNAME}+is%3Amerged"
        count_cell = f"[{len(rprs)}]({q})"
        latest = rprs[0]
        latest_cell = f'[#{latest["num"]}]({latest["url"]}) {md_table_cell(desc_for(latest))}'
        out.append(f"| {repo_cell} | {stars_cell} | {count_cell} | {latest_cell} |")
    out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- estado

def load_state(path=None):
    try:
        with open(path or STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {
        "stars": data.get("stars", {}),
        "known_prs": set(data.get("known_prs", [])),
        "last_emailed_week": data.get("last_emailed_week", ""),
        "followers": data.get("followers"),  # None = todavía no hay base para comparar
    }


def save_state(stars, known_prs, last_emailed_week, followers, path=None):
    state = {
        "stars": stars,
        "known_prs": sorted(known_prs),
        "last_emailed_week": last_emailed_week,
        "followers": followers,
    }
    with open(path or STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def iso_week_label(d):
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


# ---------------------------------------------------------------- lógica de negocio

def compute_streak(prs, today=None):
    """Semanas ISO consecutivas *completas* (sin contar la actual, recién arrancando)
    con al menos 1 PR merged. Ancla en la semana pasada para no castigar un mail que
    sale el lunes a la mañana, cuando la semana en curso todavía no tuvo chance de nada."""
    today = today or utc_today()
    weeks = set()
    for p in prs:
        if p["merged_at"]:
            weeks.add(date.fromisoformat(p["merged_at"][:10]).isocalendar()[:2])

    streak = 0
    year, week, _ = (today - timedelta(days=7)).isocalendar()
    while (year, week) in weeks:
        streak += 1
        monday = date.fromisocalendar(year, week, 1) - timedelta(days=7)
        year, week, _ = monday.isocalendar()
    return streak


def sanity_check_or_abort(prs, meta, state, min_ratio=None):
    """Si el fetch trae menos del `min_ratio` de los PRs/repos que ya conocíamos, es casi
    seguro un resultado parcial de la API (la Search API no es fuertemente consistente) y
    NO PRs perdidos de verdad. Pasó el 2026-09-15: un run devolvió 39 PRs/3 repos habiendo
    72/14 conocidos y el script, sin este chequeo, pisó el README. Mejor abortar."""
    min_ratio = MIN_RETENTION_RATIO if min_ratio is None else min_ratio
    known_prs, known_repos = state["known_prs"], set(state["stars"])
    if not known_prs:
        return  # primera corrida, sin base para comparar
    if len(prs) < len(known_prs) * min_ratio:
        abort(f"Fetched {len(prs)} merged PRs but {len(known_prs)} were already known "
              f"({len(prs) / len(known_prs):.0%} retained, minimum {min_ratio:.0%}). Looks like a "
              f"partial API response, not real data loss. README and state left untouched.")
    if len(meta) < len(known_repos) * min_ratio:
        missing = sorted(known_repos - set(meta))
        abort(f"Fetched {len(meta)} repos but {len(known_repos)} were already known "
              f"(missing: {', '.join(missing)}). README and state left untouched.")


def compute_new_prs(prs, known_prs):
    """PRs que no estaban en el último estado comunicado, el más reciente primero.
    Sin base (primera corrida) no lista nada: sería marcar *todo* como nuevo."""
    if not known_prs:
        return []
    return sorted((p for p in prs if f'{p["full"]}#{p["num"]}' not in known_prs),
                  key=lambda p: p["merged_at"], reverse=True)


def compute_new_repos(meta, known_stars):
    """[(repo, estrellas)] de repos que no estaban en el último estado, los más grandes primero."""
    if not known_stars:
        return []
    return sorted(((full, m["stars"]) for full, m in meta.items() if full not in known_stars),
                  key=lambda x: x[1], reverse=True)


def compute_stars_diff(old_stars, meta):
    """[(repo, delta)] solo con cambios y dato previo, ordenado por magnitud del cambio."""
    diff = []
    for full, m in meta.items():
        old = old_stars.get(full)
        if old is None:
            continue
        delta = m["stars"] - old
        if delta:
            diff.append((full, delta))
    return sorted(diff, key=lambda x: (-abs(x[1]), x[0]))


def star_milestones(old_stars, meta):
    """Repos que cruzaron un umbral redondo de estrellas desde el último mail."""
    hits = []
    for full, m in meta.items():
        old = old_stars.get(full)
        if old is None:
            continue
        for t in STAR_MILESTONES:
            if old < t <= m["stars"]:
                hits.append((full, t))
    return hits


def decide_send_email(content_changed, today, last_emailed_week):
    """Máximo 1 mail por semana ISO, aunque el cron dispare 3 veces: si ya se mandó uno,
    una segunda novedad actualiza el README pero NO manda otro mail (se acumula para el
    próximo). El "sin novedades" solo sale de martes en adelante (no solo el martes puntual,
    por si ese intento también se saltea) y solo si esa semana no se avisó nada todavía."""
    already = last_emailed_week == iso_week_label(today)
    closing = today.weekday() >= 1
    send = (content_changed or closing) and not already
    return send, already


# ---------------------------------------------------------------- mail

def esc(s):
    return html.escape(str(s), quote=False)


def html_section(title, rows_html):
    if not rows_html:
        return ""
    return (
        f'<h3 style="margin:20px 0 8px;font-size:15px;color:#1a1a1a">{title}</h3>'
        f'<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.6;color:#333">'
        f'{"".join(rows_html)}</ul>'
    )


def _pr_link(p):
    return f'<a href="{esc(p["url"])}">{esc(p["full"])}#{p["num"]}</a>'


def build_email(*, content_changed, new_pr_list, new_repo_list, stars_diff, milestones, streak,
                open_status, recently_closed, total_prs, total_repos, followers, followers_delta,
                warnings=(), today=None, top_star_changes=None, aged_days=None):
    today = today or utc_today()
    top_star_changes = TOP_STAR_CHANGES if top_star_changes is None else top_star_changes
    aged_days = AGED_DAYS if aged_days is None else aged_days
    parts = []

    if new_pr_list:
        subject = f"✅ Your GitHub profile updated (+{len(new_pr_list)} PR{'s' if len(new_pr_list) != 1 else ''})"
    elif content_changed:
        subject = "✅ Your GitHub profile updated"
    else:
        subject = "📋 Weekly OSS digest — no new merges this week"

    parts.append(
        f'<p style="font-size:14px;color:#555;margin:0 0 4px">'
        f'<strong>{total_prs} PRs merged · {total_repos} repos</strong> total</p>'
    )

    if followers is not None:
        followers_line = f"👥 <strong>{followers} followers</strong>"
        if followers_delta:
            color = "#1a7f37" if followers_delta > 0 else "#cf222e"
            followers_line += f' · <span style="color:{color}">{followers_delta:+d} since last update</span>'
        parts.append(f'<p style="font-size:14px;color:#555;margin:0 0 16px">{followers_line}</p>')

    if new_pr_list:
        parts.append(html_section("🆕 New merged PRs", [
            f'<li>{_pr_link(p)} — {esc(desc_for(p))}</li>' for p in new_pr_list
        ]))

    if new_repo_list:
        parts.append(html_section("📦 New repos", [
            f'<li><a href="https://github.com/{esc(full)}">{esc(full)}</a> — ⭐ {format_stars(stars)}</li>'
            for full, stars in new_repo_list
        ]))

    if stars_diff:
        shown, rest = stars_diff[:top_star_changes], stars_diff[top_star_changes:]
        rows = [
            f'<li>{esc(full)}: <strong style="color:{"#1a7f37" if d > 0 else "#cf222e"}">{d:+d} ⭐</strong></li>'
            for full, d in shown
        ]
        if rest:
            rows.append(f'<li style="color:#888">+{len(rest)} more repos with smaller changes '
                        f'(net {sum(d for _, d in rest):+d} ⭐)</li>')
        parts.append(html_section("⭐ Star changes since last update", rows))

    if milestones:
        parts.append(html_section("🏆 Milestones", [
            f'<li>🎉 <a href="https://github.com/{esc(full)}">{esc(full)}</a> crossed '
            f'<strong>{format_stars(t)}</strong> stars!</li>' for full, t in milestones
        ]))

    if streak >= 1:
        parts.append(
            f'<p style="font-size:14px;color:#555;margin:16px 0 4px">'
            f'🔥 <strong>{streak}</strong> consecutive week{"s" if streak != 1 else ""} with a merged PR.</p>'
        )

    needs_reply = [p for p in open_status if p["status"]["state"] in ("changes_requested", "awaiting_reply")]
    if needs_reply:
        rows = []
        for p in sorted(needs_reply, key=lambda p: -p["status"]["idle_days"]):
            st = p["status"]
            what = (f'changes requested by {esc(st["who"])}' if st["state"] == "changes_requested"
                    else f'{esc(st["who"])} replied')
            rows.append(f'<li>{_pr_link(p)} — {esc(p["title"])} · {what}, unanswered {st["idle_days"]}d</li>')
        parts.append(html_section("⚠️ Needs your reply", rows))

    nudge = [p for p in open_status if p["status"]["state"] == "stale"]
    if nudge:
        rows = []
        for p in sorted(nudge, key=lambda p: -p["status"]["idle_days"]):
            tag = " · ✅ approved" if p["status"]["approved"] else ""
            rows.append(f'<li>{_pr_link(p)} — {esc(p["title"])} · no activity for {p["status"]["idle_days"]}d{tag}</li>')
        parts.append(html_section("🔔 Worth a nudge to the maintainers", rows))

    others = [p for p in open_status if p["status"]["state"] in ("ok", "draft", "unknown")]
    if others:
        rows = []
        for p in sorted(others, key=lambda p: p["created_at"]):
            age = days_since(p["created_at"], today)
            aged = "🕰 " if age is not None and age >= aged_days else ""
            tags = {"draft": " · draft", "unknown": " · status unknown"}.get(p["status"]["state"], "")
            age_txt = f" · open {age}d" if age is not None else ""
            rows.append(f'<li>{aged}{_pr_link(p)} — {esc(p["title"])}{age_txt}{tags}</li>')
        parts.append(html_section(f"⏳ Other open PRs ({len(others)})", rows))

    if recently_closed:
        parts.append(html_section("🗑️ Recently closed without merging", [
            f'<li>{_pr_link(p)} — {esc(p["title"])}</li>' for p in recently_closed
        ]))

    if warnings:
        parts.append(
            '<p style="margin-top:20px;font-size:12px;color:#b35900">⚠️ Data notes:<br>'
            + "<br>".join(esc(w) for w in warnings) + "</p>"
        )

    parts.append(
        f'<p style="margin-top:24px;font-size:12px;color:#888">'
        f'<a href="https://github.com/{USERNAME}">Profile</a></p>'
    )
    return subject, "\n".join(p for p in parts if p)


# ---------------------------------------------------------------- main

def main():
    warnings = []
    today = utc_today()
    state = load_state()

    prs = fetch_merged_prs(warnings)
    if not prs:
        abort("No merged PRs found — refusing to empty the README.")
    meta = fetch_repo_meta(sorted({p["full"] for p in prs}), state["stars"], warnings)
    sanity_check_or_abort(prs, meta, state)  # antes de tocar el README o el estado guardado
    section = build_section(prs, meta)

    with open(README, encoding="utf-8") as f:
        content = f.read()
    if START not in content or END not in content:
        abort(f"Missing {START} / {END} markers in README.")

    pre = content.split(START)[0]
    post = content.split(END)[1]
    new = f"{pre}{START}\n\n{section}\n{END}{post}"
    content_changed = new != content
    if content_changed:
        with open(README, "w", encoding="utf-8") as f:
            f.write(new)

    all_pr_ids = {f'{p["full"]}#{p["num"]}' for p in prs}
    new_pr_list = compute_new_prs(prs, state["known_prs"])
    new_repo_list = compute_new_repos(meta, state["stars"])
    stars_diff = compute_stars_diff(state["stars"], meta)
    milestones = star_milestones(state["stars"], meta)
    streak = compute_streak(prs, today)

    # Datos que solo alimentan el mail: si fallan, el run sigue y el mail lo avisa.
    open_prs = safe(fetch_open_prs, None, "Open PRs", warnings)
    open_status = []
    if open_prs is not None:
        open_status, status_warnings = fetch_open_pr_status(open_prs, today)
        warnings.extend(status_warnings)
    recently_closed = safe(lambda: fetch_recently_closed_unmerged(today=today), [],
                           "Recently closed PRs", warnings)
    followers = safe(fetch_follower_count, None, "Follower count", warnings)
    followers_delta = None if followers is None or state["followers"] is None else followers - state["followers"]

    send_email, already_emailed = decide_send_email(content_changed, today, state["last_emailed_week"])
    if send_email:
        save_state({full: m["stars"] for full, m in meta.items()}, all_pr_ids,
                   iso_week_label(today), state["followers"] if followers is None else followers)
    else:
        # Reescribe lo mismo: si hubo novedad pero se suprime el mail, el próximo mail compara
        # contra esta MISMA base vieja y muestra el delta acumulado completo.
        save_state(state["stars"], state["known_prs"], state["last_emailed_week"], state["followers"])

    subject, body_html = build_email(
        content_changed=content_changed, new_pr_list=new_pr_list, new_repo_list=new_repo_list,
        stars_diff=stars_diff, milestones=milestones, streak=streak, open_status=open_status,
        recently_closed=recently_closed, total_prs=len(prs), total_repos=len(meta),
        followers=followers, followers_delta=followers_delta, warnings=warnings, today=today,
    )

    if content_changed:
        print(f"README actualizado: {len(prs)} merged PRs en {len(meta)} repos. "
              f"(+{len(new_pr_list)} PRs nuevos, +{len(new_repo_list)} repos nuevos)")
    else:
        print("Sin cambios en el README.")
    counts = defaultdict(int)
    for p in open_status:
        counts[p["status"]["state"]] += 1
    followers_log = ("n/d" if followers is None else
                     f"{followers} ({'sin base' if followers_delta is None else f'{followers_delta:+d} desde el último mail'})")
    print(f"Open PRs: {len(open_status)} {dict(counts)} · Closed unmerged ({RECENT_CLOSED_DAYS}d): "
          f"{len(recently_closed)} · Streak: {streak} · Followers: {followers_log} · "
          f"Send email: {send_email} (ya mandado esta semana: {already_emailed})")
    for w in warnings:
        print(f"WARNING: {w}")

    write_github_output({
        "changed": "true" if content_changed else "false",
        "send_email": "true" if send_email else "false",
        "email_subject": subject,
        "email_html": body_html,
    })


if __name__ == "__main__":
    main()
