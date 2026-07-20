#!/usr/bin/env python3
"""Regenera la sección de PRs merged del README a partir de la API de GitHub.

- Solo incluye PRs MERGED (nunca abiertos/pendientes).
- Excluye repos propios (owner == USERNAME): cuentan solo contribuciones a otros.
- Mantiene descripciones curadas (PR_OVERRIDES / REPO_TAGLINES); para lo nuevo
  usa el título del PR / la descripción del repo en GitHub.
- Reemplaza solo lo que está entre los marcadores PRS:START / PRS:END.

Uso local:  GITHUB_TOKEN=$(gh auth token) python3 scripts/update_readme_prs.py
En CI:      el workflow exporta GITHUB_TOKEN automáticamente.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

USERNAME = "santichausis"
README = os.path.join(os.path.dirname(__file__), "..", "README.md")
START = "<!-- PRS:START -->"
END = "<!-- PRS:END -->"

# Un repo necesita al menos esta cantidad de PRs merged para tener sección propia.
FEATURED_THRESHOLD = 3
# Máximo de PRs mostrados por repo; si hay más, se muestra un "N merged PRs" + link.
MAX_ROWS = 3

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

# Tagline corta por repo (al lado del título). Si no está, usa la descripción de GitHub.
REPO_TAGLINES = {
    "PokeAPI/pokeapi": "The RESTful Pokémon API",
    "pschlan/cron-job.org": "Open source cron job scheduling service",
    "Tadreeb-LMS/tadreeblms": "Open source learning management system",
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


def fetch_merged_prs():
    """Todos los PRs merged del usuario, excluyendo repos propios."""
    items = []
    page = 1
    while True:
        data = api("/search/issues", {
            "q": f"author:{USERNAME} type:pr is:merged",
            "per_page": 100,
            "page": page,
        })
        batch = data.get("items", [])
        items.extend(batch)
        if len(items) >= data.get("total_count", 0) or not batch:
            break
        page += 1

    prs = []
    for it in items:
        owner, repo, num = parse_pr_url(it["html_url"])
        if owner.lower() == USERNAME.lower():
            continue  # excluir repos propios
        prs.append({
            "owner": owner,
            "repo": repo,
            "full": f"{owner}/{repo}",
            "num": int(num),
            "title": clean_title(it["title"]),
            "url": it["html_url"],
            "merged_at": (it.get("pull_request") or {}).get("merged_at") or it.get("closed_at") or "",
        })
    return prs


def parse_pr_url(html_url):
    # https://github.com/OWNER/REPO/pull/NUM
    parts = html_url.split("github.com/", 1)[1].split("/")
    return parts[0], parts[1], parts[3]


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


def tagline_for(full, repo_desc):
    if full in REPO_TAGLINES:
        return REPO_TAGLINES[full]
    d = (repo_desc or "").strip()
    d = d.split(". ")[0].split(". ")[0]  # primera oración
    return d[:80].rstrip(" .") if d else ""


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


def stars_badge(full, stars):
    label = format_stars(stars)
    return f'[![Stars](https://img.shields.io/badge/⭐-{label}-blue)](https://github.com/{full}/stargazers)'


def build_section(prs):
    by_repo = defaultdict(list)
    for p in prs:
        by_repo[p["full"]].append(p)
    for repo_prs in by_repo.values():
        repo_prs.sort(key=lambda p: p["merged_at"], reverse=True)

    # Metadata de cada repo (descripción, estrellas, forks) para ordenar y describir.
    meta = {}
    for r in by_repo:
        try:
            data = api(f"/repos/{r}")
            meta[r] = {
                "desc": data.get("description") or "",
                "stars": data.get("stargazers_count") or 0,
                "forks": data.get("forks_count") or 0,
            }
        except urllib.error.HTTPError:
            meta[r] = {"desc": "", "stars": 0, "forks": 0}
    repo_desc = {r: m["desc"] for r, m in meta.items()}

    # Orden de repos por popularidad: estrellas y, a igualdad, forks (desc).
    repos = sorted(by_repo.keys(), key=lambda r: (meta[r]["stars"], meta[r]["forks"]), reverse=True)
    featured = [r for r in repos if len(by_repo[r]) >= FEATURED_THRESHOLD]
    others = [r for r in repos if len(by_repo[r]) < FEATURED_THRESHOLD]

    out = []
    for full in featured:
        owner = full.split("/")[0]
        rprs = by_repo[full]
        tag = tagline_for(full, repo_desc.get(full))
        title = f"### {logo(owner)} [{full}](https://github.com/{full})"
        if tag:
            title += f" — {tag}"
        out.append(title)
        out.append("")
        if len(rprs) > MAX_ROWS:
            out.append(f"**{len(rprs)} merged PRs** across features, bug fixes and refactors. Highlights:")
            out.append("")
        badge = stars_badge(full, meta[full]["stars"])
        out.append("| PR | What it does | Stars |")
        out.append("|---|---|---|")
        for p in rprs[:MAX_ROWS]:
            out.append(f'| [#{p["num"]}]({p["url"]}) | {desc_for(p)} | {badge} |')
        if len(rprs) > MAX_ROWS:
            q = f"https://github.com/{full}/pulls?q=is%3Apr+author%3A{USERNAME}+is%3Amerged"
            out.append("")
            out.append(f"→ [All my PRs in this repo]({q})")
        out.append("")

    if others:
        out.append("### Other merged contributions")
        out.append("")
        out.append("| Repository | PR | What it does | Stars |")
        out.append("|---|---|---|---|")
        for full in others:
            owner = full.split("/")[0]
            p = by_repo[full][0]
            badge = stars_badge(full, meta[full]["stars"])
            out.append(
                f'| {logo(owner, 16)} [{full}](https://github.com/{full}) '
                f'| [#{p["num"]}]({p["url"]}) | {desc_for(p)} | {badge} |'
            )
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def main():
    prs = fetch_merged_prs()
    if not prs:
        print("No merged PRs found — abort para no vaciar el README.", file=sys.stderr)
        sys.exit(1)
    section = build_section(prs)

    with open(README, encoding="utf-8") as f:
        content = f.read()
    if START not in content or END not in content:
        print(f"Faltan los marcadores {START} / {END} en el README.", file=sys.stderr)
        sys.exit(1)

    pre = content.split(START)[0]
    post = content.split(END)[1]
    new = f"{pre}{START}\n\n{section}\n{END}{post}"

    if new == content:
        print("Sin cambios.")
        return
    with open(README, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"README actualizado: {len(prs)} merged PRs en {len(set(p['full'] for p in prs))} repos.")


if __name__ == "__main__":
    main()
