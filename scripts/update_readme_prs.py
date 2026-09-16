#!/usr/bin/env python3
"""Regenera la sección de PRs merged del README y arma el resumen semanal por mail.

- Solo incluye PRs MERGED en la tabla del README (nunca abiertos/pendientes).
- Excluye repos propios (owner == USERNAME): cuentan solo contribuciones a otros.
- Mantiene descripciones curadas (PR_OVERRIDES); para lo nuevo usa el título del PR.
- Reemplaza solo lo que está entre los marcadores PRS:START / PRS:END.
- El mail (asunto + HTML) sale por GITHUB_OUTPUT como email_subject / email_html,
  para que el workflow solo tenga que mandarlo, no armarlo.

Uso local:  GITHUB_TOKEN=$(gh auth token) python3 scripts/update_readme_prs.py
En CI:      el workflow exporta GITHUB_TOKEN automáticamente.
"""

import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

USERNAME = "santichausis"
README = os.path.join(os.path.dirname(__file__), "..", "README.md")
STATE_PATH = os.path.join(os.path.dirname(__file__), "weekly_state.json")
START = "<!-- PRS:START -->"
END = "<!-- PRS:END -->"

RECENT_CLOSED_DAYS = 10  # ventana para "cerrados sin merge recientes" (cubre semanas con cron salteado)
STAR_MILESTONES = [100, 500, 1000, 2500, 5000, 10000, 25000, 50000, 75000,
                    100000, 150000, 200000, 250000, 500000, 1000000]

# Descripciones curadas a mano por PR ("owner/repo#num"). Lo que no esté acá
# usa el título crudo del PR. Agregá entradas para pulir PRs nuevos.
PR_OVERRIDES = {
    "PokeAPI/pokeapi#1487": "Rename `region_id` and `base_form_id` to `region` / `base_form` in evolution data",
    "PokeAPI/pokeapi#1486": "Mark `Region.main_generation` as nullable in the OpenAPI spec",
    "PokeAPI/pokeapi#1482": "Fix `gender_rate` for DLC Pokémon (Dipplin, Loyal Three, Ogerpon, Terapagos)",
    "pschlan/cron-job.org#421": 'Add "Import from cURL" to the job editor (frontend feature)',
    "Tadreeb-LMS/tadreeblms#799": "Add final feedback text field for course completion",
    "Tadreeb-LMS/tadreeblms#800": "Move Categories into the Courses Management dropdown",
    "Tadreeb-LMS/tadreeblms#456": "Add download button for the base English language file",
    "Tadreeb-LMS/tadreeblms#445": "Regenerate session ID on login + enable `AuthenticateSession` middleware",
    "Tadreeb-LMS/tadreeblms#795": "Resolve employee edit 404 by reordering routes",
    "Tadreeb-LMS/tadreeblms#373": "Fix 500 error in the Send Email Notification module",
    "tcgdex/cards-database#1371": "Correct Drampa holo variants for McDonald's 2022/2024 promos",
    "freeCodeCamp/contribute#1283": "Update Twitter icon to X logo in the navbar",
    "shevabam/breaking-bad-quotes#7": "Add Gus Fring and Saul Goodman quotes",
}


def api(path, params=None):
    url = "https://api.github.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def search_prs(query):
    """Pagina /search/issues con la query dada, devuelve los items crudos."""
    items = []
    page = 1
    while True:
        data = api("/search/issues", {"q": query, "per_page": 100, "page": page})
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


def to_pr(it):
    owner, repo, num = parse_pr_url(it["html_url"])
    return {
        "owner": owner,
        "repo": repo,
        "full": f"{owner}/{repo}",
        "num": int(num),
        "title": clean_title(it["title"]),
        "url": it["html_url"],
        "created_at": it.get("created_at", ""),
        "merged_at": (it.get("pull_request") or {}).get("merged_at") or "",
        "closed_at": it.get("closed_at") or "",
    }


def fetch_prs(query_suffix):
    """PRs del usuario que matchean la query, excluyendo repos propios."""
    items = search_prs(f"author:{USERNAME} type:pr {query_suffix}")
    prs = [to_pr(it) for it in items]
    return [p for p in prs if p["owner"].lower() != USERNAME.lower()]


def fetch_merged_prs():
    return fetch_prs("is:merged")


def fetch_open_prs():
    return fetch_prs("is:open")


def fetch_needs_action_prs():
    return fetch_prs("is:open review:changes_requested")


def fetch_recently_closed_unmerged(days=RECENT_CLOSED_DAYS):
    since = (date.today() - timedelta(days=days)).isoformat()
    return fetch_prs(f"is:closed is:unmerged closed:>={since}")


def clean_title(t):
    # Saca prefijos tipo "Fix:", "feat:", "Refactor:" para que quede más limpio.
    for sep in (": ", "/ "):
        head = t.split(sep, 1)[0].lower()
        if head in {"fix", "feat", "feature", "refactor", "chore", "docs", "frontend", "fix"} and sep in t:
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
    val = n / 1000
    s = f"{val:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return f"{s}k"


def days_since(iso_date):
    if not iso_date:
        return None
    return (date.today() - date.fromisoformat(iso_date[:10])).days


def build_section(prs):
    by_repo = defaultdict(list)
    for p in prs:
        by_repo[p["full"]].append(p)
    for repo_prs in by_repo.values():
        repo_prs.sort(key=lambda p: p["merged_at"], reverse=True)

    # Metadata de cada repo (estrellas, forks) para ordenar.
    meta = {}
    for r in by_repo:
        try:
            data = api(f"/repos/{r}")
            meta[r] = {
                "stars": data.get("stargazers_count") or 0,
                "forks": data.get("forks_count") or 0,
            }
        except urllib.error.HTTPError:
            meta[r] = {"stars": 0, "forks": 0}

    # Orden de repos por popularidad: estrellas y, a igualdad, forks (desc).
    repos = sorted(by_repo.keys(), key=lambda r: (meta[r]["stars"], meta[r]["forks"]), reverse=True)

    out = [
        f"**{len(prs)} PRs merged · {len(repos)} repos** · auto-updated every Monday",
        "",
        "| Repo | Stars | Merged PRs | Latest merge |",
        "|---|---|:---:|---|",
    ]
    for full in repos:
        owner = full.split("/")[0]
        rprs = by_repo[full]
        repo_cell = f"{logo(owner)} [{full}](https://github.com/{full})"
        stars_cell = f"⭐ {format_stars(meta[full]['stars'])}"  # NBSP: evita que el navegador parta "⭐ 72.7k" en dos renglones
        q = f"https://github.com/{full}/pulls?q=is%3Apr+author%3A{USERNAME}+is%3Amerged"
        count_cell = f"[{len(rprs)}]({q})"
        latest = rprs[0]
        latest_cell = f'[#{latest["num"]}]({latest["url"]}) {md_table_cell(desc_for(latest))}'
        out.append(f"| {repo_cell} | {stars_cell} | {count_cell} | {latest_cell} |")
    out.append("")

    return "\n".join(out).rstrip() + "\n", meta


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


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {"stars": data.get("stars", {}), "known_prs": set(data.get("known_prs", []))}


def save_state(meta, all_pr_ids):
    state = {
        "stars": {full: m["stars"] for full, m in meta.items()},
        "known_prs": sorted(all_pr_ids),
    }
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def compute_streak(prs):
    """Semanas ISO consecutivas *completas* (sin contar la actual, recién arrancando)
    con al menos 1 PR merged. Ancla en la semana pasada para no castigar un mail que
    sale el lunes a la mañana, cuando la semana en curso todavía no tuvo chance de nada."""
    weeks = set()
    for p in prs:
        if not p["merged_at"]:
            continue
        d = date.fromisoformat(p["merged_at"][:10])
        weeks.add(d.isocalendar()[:2])

    streak = 0
    year, week, _ = (date.today() - timedelta(days=7)).isocalendar()
    while (year, week) in weeks:
        streak += 1
        monday = date.fromisocalendar(year, week, 1) - timedelta(days=7)
        year, week, _ = monday.isocalendar()
    return streak


MIN_RETENTION_RATIO = 0.9  # si el fetch trae menos del 90% de los PRs/repos que ya conocíamos,
# es casi seguro un resultado parcial de la Search API de GitHub (no es fuertemente consistente,
# a veces devuelve un total_count parcial/desactualizado) y NO un caso real de "se desmergearon
# PRs". Pasó de verdad el 2026-09-15: un run devolvió 39 PRs/3 repos habiendo 72/14 conocidos,
# y el script sin este chequeo lo tomó como bueno y pisó el README. Mejor abortar (el run queda
# en rojo, GitHub avisa por mail) que confirmar un dato que puede ser basura.


def sanity_check_or_abort(prs, meta, state):
    known_prs, known_repos = state["known_prs"], set(state["stars"])
    if not known_prs:
        return  # primera corrida, sin base para comparar
    if len(prs) < len(known_prs) * MIN_RETENTION_RATIO:
        print(
            f"ABORT: el fetch trajo {len(prs)} PRs merged, pero ya conocíamos {len(known_prs)} "
            f"(retención {len(prs) / len(known_prs):.0%}, mínimo {MIN_RETENTION_RATIO:.0%}). "
            f"Huele a respuesta parcial de la Search API de GitHub, no a PRs perdidos de verdad. "
            f"No se toca el README ni el estado — reintentar en la próxima corrida.",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(meta) < len(known_repos) * MIN_RETENTION_RATIO:
        missing = sorted(known_repos - set(meta))
        print(
            f"ABORT: el fetch trajo {len(meta)} repos, pero ya conocíamos {len(known_repos)} "
            f"(faltan: {', '.join(missing)}). Misma causa probable que el chequeo de PRs — "
            f"no se toca el README ni el estado.",
            file=sys.stderr,
        )
        sys.exit(1)


def star_milestones(old_stars, meta):
    """Repos que cruzaron un umbral redondo de estrellas desde el último mail."""
    hits = []
    for full, m in meta.items():
        old = old_stars.get(full)
        if old is None:
            continue
        new = m["stars"]
        for t in STAR_MILESTONES:
            if old < t <= new:
                hits.append((full, t))
    return hits


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


def build_email(*, content_changed, new_pr_list, new_repo_list, stars_diff,
                 milestones, streak, needs_action, open_prs, recently_closed,
                 total_prs, total_repos):
    parts = []

    if new_pr_list:
        subject = f"✅ Your GitHub profile updated (+{len(new_pr_list)} PR{'s' if len(new_pr_list) != 1 else ''})"
    elif content_changed:
        subject = "✅ Your GitHub profile updated"
    else:
        subject = "📋 Weekly OSS digest — no new merges this week"

    parts.append(
        f'<p style="font-size:14px;color:#555;margin:0 0 4px">'
        f'<strong>{total_prs} PRs merged · {total_repos} repos</strong> total'
        f'</p>'
    )

    if new_pr_list:
        rows = [
            f'<li><a href="{esc(p["url"])}">{esc(p["full"])}#{p["num"]}</a> — {esc(desc_for(p))}</li>'
            for p in new_pr_list
        ]
        parts.append(html_section("🆕 New merged PRs", rows))

    if new_repo_list:
        rows = [
            f'<li><a href="https://github.com/{esc(full)}">{esc(full)}</a> '
            f'— ⭐ {format_stars(stars)}</li>'
            for full, stars in new_repo_list
        ]
        parts.append(html_section("📦 New repos", rows))

    if stars_diff:
        rows = [
            f'<li>{esc(full)}: <strong style="color:{"#1a7f37" if d > 0 else "#cf222e"}">'
            f'{"+" if d > 0 else ""}{d} ⭐</strong></li>'
            for full, d in stars_diff
        ]
        parts.append(html_section("⭐ Star changes since last update", rows))

    if milestones:
        rows = [
            f'<li>🎉 <a href="https://github.com/{esc(full)}">{esc(full)}</a> crossed '
            f'<strong>{format_stars(t)}</strong> stars!</li>'
            for full, t in milestones
        ]
        parts.append(html_section("🏆 Milestones", rows))

    if streak >= 1:
        parts.append(
            f'<p style="font-size:14px;color:#555;margin:16px 0 4px">'
            f'🔥 <strong>{streak}</strong> consecutive week{"s" if streak != 1 else ""} '
            f'with a merged PR.</p>'
        )

    if needs_action:
        rows = [
            f'<li><a href="{esc(p["url"])}">{esc(p["full"])}#{p["num"]}</a> — {esc(p["title"])}</li>'
            for p in needs_action
        ]
        parts.append(html_section("⚠️ Needs your action (changes requested)", rows))

    if open_prs:
        rows = []
        for p in sorted(open_prs, key=lambda p: p["created_at"]):
            d = days_since(p["created_at"])
            age = f" · open {d}d" if d is not None else ""
            rows.append(
                f'<li><a href="{esc(p["url"])}">{esc(p["full"])}#{p["num"]}</a> '
                f'— {esc(p["title"])}{age}</li>'
            )
        parts.append(html_section(f"⏳ Pending PRs ({len(open_prs)})", rows))

    if recently_closed:
        rows = [
            f'<li><a href="{esc(p["url"])}">{esc(p["full"])}#{p["num"]}</a> — {esc(p["title"])}</li>'
            for p in recently_closed
        ]
        parts.append(html_section("🗑️ Recently closed without merging", rows))

    parts.append(
        f'<p style="margin-top:24px;font-size:12px;color:#888">'
        f'<a href="https://github.com/{USERNAME}">Profile</a>'
        f'</p>'
    )

    return subject, "\n".join(p for p in parts if p)


def main():
    prs = fetch_merged_prs()
    if not prs:
        print("No merged PRs found — abort para no vaciar el README.", file=sys.stderr)
        sys.exit(1)
    section, meta = build_section(prs)

    state = load_state()
    sanity_check_or_abort(prs, meta, state)  # antes de tocar el README o el estado guardado

    with open(README, encoding="utf-8") as f:
        content = f.read()
    if START not in content or END not in content:
        print(f"Faltan los marcadores {START} / {END} en el README.", file=sys.stderr)
        sys.exit(1)

    pre = content.split(START)[0]
    post = content.split(END)[1]
    new = f"{pre}{START}\n\n{section}\n{END}{post}"
    content_changed = new != content

    if content_changed:
        with open(README, "w", encoding="utf-8") as f:
            f.write(new)

    all_pr_ids = {f'{p["full"]}#{p["num"]}' for p in prs}
    new_pr_ids = all_pr_ids - state["known_prs"] if state["known_prs"] else set()
    new_pr_list = sorted(
        [p for p in prs if f'{p["full"]}#{p["num"]}' in new_pr_ids],
        key=lambda p: p["merged_at"], reverse=True,
    ) if state["known_prs"] else []  # primera corrida: sin base, no listar "todo como nuevo"

    new_repo_list = sorted(
        [(full, m["stars"]) for full, m in meta.items() if full not in state["stars"]],
        key=lambda x: x[1], reverse=True,
    ) if state["stars"] else []

    stars_diff = []
    for full in sorted(meta, key=lambda r: meta[r]["stars"], reverse=True):
        old = state["stars"].get(full)
        if old is None:
            continue
        delta = meta[full]["stars"] - old
        if delta:
            stars_diff.append((full, delta))

    milestones = star_milestones(state["stars"], meta)
    streak = compute_streak(prs)

    # Snapshots en vivo, no dependen de si hubo update del README esta corrida.
    open_prs = fetch_open_prs()
    needs_action = fetch_needs_action_prs()
    recently_closed = fetch_recently_closed_unmerged()

    if content_changed:
        save_state(meta, all_pr_ids)

    total_prs, total_repos = len(prs), len(meta)
    subject, body_html = build_email(
        content_changed=content_changed,
        new_pr_list=new_pr_list,
        new_repo_list=new_repo_list,
        stars_diff=stars_diff,
        milestones=milestones,
        streak=streak,
        needs_action=needs_action,
        open_prs=open_prs,
        recently_closed=recently_closed,
        total_prs=total_prs,
        total_repos=total_repos,
    )

    if content_changed:
        print(f"README actualizado: {total_prs} merged PRs en {total_repos} repos. "
              f"(+{len(new_pr_list)} PRs nuevos, +{len(new_repo_list)} repos nuevos)")
    else:
        print("Sin cambios en el README.")
    print(f"Open PRs: {len(open_prs)} · Needs action: {len(needs_action)} · "
          f"Closed unmerged (últimos {RECENT_CLOSED_DAYS}d): {len(recently_closed)} · Streak: {streak}")

    write_github_output({
        "changed": "true" if content_changed else "false",
        "email_subject": subject,
        "email_html": body_html,
    })


if __name__ == "__main__":
    main()
