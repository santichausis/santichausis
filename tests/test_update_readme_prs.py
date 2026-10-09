import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import update_readme_prs as u  # noqa: E402

TODAY = date(2026, 10, 9)  # viernes, semana ISO 2026-W41


def pr(full="a/b", num=1, merged_at="2026-10-01T10:00:00Z", **kw):
    owner, repo = full.split("/")
    base = {"owner": owner, "repo": repo, "full": full, "num": num, "title": f"Title {num}",
            "url": f"https://github.com/{full}/pull/{num}", "created_at": "2026-09-01T00:00:00Z",
            "merged_at": merged_at, "closed_at": "", "draft": False}
    base.update(kw)
    return base


def meta_of(**stars):
    return {k.replace("__", "/"): {"stars": v, "forks": 0, "stale": False} for k, v in stars.items()}


class AbortCapture:
    """Redirige GITHUB_OUTPUT a un archivo temporal (y silencia stderr) para poder leer
    abort_reason después de salir del bloque `with`."""

    def __enter__(self):
        self.tmp = tempfile.NamedTemporaryFile("w+", suffix=".out", delete=False)
        self.tmp.close()
        self.patch = mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.tmp.name})
        self.patch.start()
        self.stderr = contextlib.redirect_stderr(io.StringIO())
        self.stderr.__enter__()
        self._reason = None
        return self

    def __exit__(self, *exc):
        self.stderr.__exit__(*exc)
        self.patch.stop()
        with open(self.tmp.name, encoding="utf-8") as f:
            for line in f:
                if line.startswith("abort_reason="):
                    self._reason = line.split("=", 1)[1].strip()
        os.unlink(self.tmp.name)

    def reason(self):
        return self._reason


# ------------------------------------------------------------------ guardia de sanidad

class SanityCheckTests(unittest.TestCase):
    def state(self, n_prs=72, n_repos=14):
        return {"known_prs": {f"x/y#{i}" for i in range(n_prs)},
                "stars": {f"repo{i}": 1 for i in range(n_repos)}}

    def test_aborts_on_the_real_september_incident(self):
        """El 2026-09-15 la Search API devolvió 39 PRs / 3 repos habiendo 72 / 14 conocidos."""
        with AbortCapture() as cap, self.assertRaises(SystemExit) as ctx:
            u.sanity_check_or_abort([None] * 39, {f"repo{i}": {} for i in range(3)}, self.state())
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("39 merged PRs", cap.reason())

    def test_aborts_when_repos_vanish_even_if_pr_count_looks_fine(self):
        with AbortCapture() as cap, self.assertRaises(SystemExit):
            u.sanity_check_or_abort([None] * 72, {f"repo{i}": {} for i in range(5)}, self.state())
        self.assertIn("missing", cap.reason())

    def test_normal_growth_passes(self):
        u.sanity_check_or_abort([None] * 75, {f"repo{i}": {} for i in range(14)}, self.state())

    def test_first_run_has_no_baseline_so_it_passes(self):
        u.sanity_check_or_abort([None] * 3, {"r": {}}, {"known_prs": set(), "stars": {}})

    def test_boundary_exactly_at_the_minimum_passes_and_one_below_aborts(self):
        st = self.state(n_prs=100, n_repos=10)
        metas = {f"repo{i}": {} for i in range(10)}
        u.sanity_check_or_abort([None] * 90, metas, st)  # 90% justo: pasa
        with AbortCapture(), self.assertRaises(SystemExit):
            u.sanity_check_or_abort([None] * 89, metas, st)


# ------------------------------------------------------------------ stars=0 silencioso

class RepoMetaTests(unittest.TestCase):
    def test_success_reads_stars_and_forks(self):
        with mock.patch.object(u.ghapi, "api", return_value={"stargazers_count": 10, "forks_count": 2}):
            meta = u.fetch_repo_meta(["a/b"], {}, [])
        self.assertEqual(meta["a/b"], {"stars": 10, "forks": 2, "stale": False})

    def test_failure_falls_back_to_last_known_value_never_zero(self):
        warnings = []
        err = urllib.error.HTTPError("u", 404, "nf", None, None)
        with mock.patch.object(u.ghapi, "api", side_effect=err):
            meta = u.fetch_repo_meta(["a/b"], {"a/b": 73200}, warnings)
        self.assertEqual(meta["a/b"]["stars"], 73200)
        self.assertTrue(meta["a/b"]["stale"])
        self.assertIn("last known value (73200)", warnings[0])

    def test_failure_on_a_new_repo_aborts_instead_of_inventing_a_number(self):
        err = urllib.error.URLError("down")
        with AbortCapture() as cap, mock.patch.object(u.ghapi, "api", side_effect=err), \
                self.assertRaises(SystemExit):
            u.fetch_repo_meta(["new/repo"], {}, [])
        self.assertIn("new/repo", cap.reason())
        self.assertIn("made-up number", cap.reason())


# ------------------------------------------------------------------ cap de 1 mail por semana

class DecideSendEmailTests(unittest.TestCase):
    MON, TUE, FRI = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 9)

    def test_news_on_monday_sends(self):
        self.assertEqual(u.decide_send_email(True, self.MON, "2026-W40"), (True, False))

    def test_no_news_on_monday_does_not_send(self):
        self.assertEqual(u.decide_send_email(False, self.MON, ""), (False, False))

    def test_no_news_from_tuesday_on_sends_the_weekly_pulse(self):
        self.assertTrue(u.decide_send_email(False, self.TUE, "2026-W40")[0])
        self.assertTrue(u.decide_send_email(False, self.FRI, "")[0])

    def test_second_news_in_the_same_week_is_suppressed(self):
        self.assertEqual(u.decide_send_email(True, self.TUE, "2026-W41"), (False, True))

    def test_pulse_is_suppressed_if_already_emailed_this_week(self):
        self.assertEqual(u.decide_send_email(False, self.FRI, "2026-W41"), (False, True))

    def test_new_week_resets(self):
        self.assertTrue(u.decide_send_email(True, date(2026, 10, 12), "2026-W41")[0])


# ------------------------------------------------------------------ streak / estrellas / milestones

class StreakTests(unittest.TestCase):
    def test_counts_consecutive_complete_weeks_ending_last_week(self):
        prs = [pr(merged_at=d) for d in ("2026-10-01T00:00:00Z", "2026-09-24T00:00:00Z", "2026-09-17T00:00:00Z")]
        self.assertEqual(u.compute_streak(prs, TODAY), 3)

    def test_gap_breaks_the_streak(self):
        prs = [pr(merged_at=d) for d in ("2026-10-01T00:00:00Z", "2026-09-17T00:00:00Z")]
        self.assertEqual(u.compute_streak(prs, TODAY), 1)

    def test_current_week_alone_does_not_count(self):
        self.assertEqual(u.compute_streak([pr(merged_at="2026-10-07T00:00:00Z")], TODAY), 0)

    def test_year_boundary(self):
        prs = [pr(merged_at="2025-12-31T00:00:00Z"), pr(merged_at="2025-12-24T00:00:00Z")]
        self.assertEqual(u.compute_streak(prs, date(2026, 1, 7)), 2)


class StarsTests(unittest.TestCase):
    def test_diff_is_sorted_by_magnitude_and_skips_unchanged_and_new(self):
        old = {"a/a": 100, "b/b": 100, "c/c": 100, "e/e": 100, "f/f": 100}
        meta = meta_of(a__a=101, b__b=40, c__c=100, d__d=999, e__e=150, f__f=97)
        # por |delta|: -60, +50, -3, +1  (ordenar por valor firmado daría -60, -3, +1, +50)
        self.assertEqual(u.compute_stars_diff(old, meta),
                         [("b/b", -60), ("e/e", 50), ("f/f", -3), ("a/a", 1)])

    def test_milestone_crossing(self):
        meta = meta_of(a__a=10050, b__b=900)
        self.assertEqual(u.star_milestones({"a/a": 9990, "b/b": 890}, meta), [("a/a", 10000)])

    def test_no_milestone_without_baseline(self):
        self.assertEqual(u.star_milestones({}, meta_of(a__a=100000)), [])


class NewSinceLastEmailTests(unittest.TestCase):
    def test_first_run_has_no_baseline_so_nothing_is_flagged_as_new(self):
        self.assertEqual(u.compute_new_prs([pr(num=1), pr(num=2)], set()), [])
        self.assertEqual(u.compute_new_repos(meta_of(a__a=5), {}), [])

    def test_new_prs_are_those_not_in_the_baseline_most_recent_first(self):
        prs = [pr("a/b", 1, merged_at="2026-09-01T00:00:00Z"), pr("a/b", 2, merged_at="2026-10-02T00:00:00Z"),
               pr("a/b", 3, merged_at="2026-10-05T00:00:00Z")]
        self.assertEqual([p["num"] for p in u.compute_new_prs(prs, {"a/b#1"})], [3, 2])

    def test_new_repos_sorted_by_stars(self):
        meta = meta_of(old__r=1, small__r=10, big__r=500)
        self.assertEqual(u.compute_new_repos(meta, {"old/r": 1}), [("big/r", 500), ("small/r", 10)])


# ------------------------------------------------------------------ fuentes de PRs

class MergeSourcesTests(unittest.TestCase):
    def test_union_and_differences(self):
        s = [pr("a/b", 1), pr("a/b", 2)]
        g = [pr("a/b", 2), pr("a/b", 3)]
        merged, only_s, only_g = u.merge_sources(s, g)
        self.assertEqual({p["num"] for p in merged}, {1, 2, 3})
        self.assertEqual((only_s, only_g), (["a/b#1"], ["a/b#3"]))

    def test_case_insensitive_dedupe_prefers_search_record(self):
        merged, _, _ = u.merge_sources([pr("PokeAPI/pokeapi", 1)], [pr("pokeapi/pokeapi", 1)])
        self.assertEqual([p["full"] for p in merged], ["PokeAPI/pokeapi"])


class FetchMergedTests(unittest.TestCase):
    def test_one_source_failing_is_covered_by_the_other(self):
        warnings = []
        with mock.patch.object(u, "fetch_prs", side_effect=urllib.error.URLError("down")), \
                mock.patch.object(u, "fetch_merged_prs_graphql", return_value=[pr("a/b", 1)]):
            out = u.fetch_merged_prs(warnings)
        self.assertEqual([p["num"] for p in out], [1])
        self.assertIn("Search API failed", warnings[0])

    def test_both_failing_aborts(self):
        with AbortCapture(), \
                mock.patch.object(u, "fetch_prs", side_effect=urllib.error.URLError("down")), \
                mock.patch.object(u, "fetch_merged_prs_graphql", side_effect=RuntimeError("no token")), \
                self.assertRaises(SystemExit):
            u.fetch_merged_prs([])

    def test_disagreement_is_reported_and_union_is_used(self):
        warnings = []
        with mock.patch.object(u, "fetch_prs", return_value=[pr("a/b", 1)]), \
                mock.patch.object(u, "fetch_merged_prs_graphql", return_value=[pr("a/b", 1), pr("a/b", 2)]):
            out = u.fetch_merged_prs(warnings)
        self.assertEqual(len(out), 2)
        self.assertIn("disagree", warnings[0])

    def test_graphql_pagination_and_own_repo_exclusion(self):
        node = lambda n, repo: {"number": n, "title": f"Fix: t{n}", "url": f"u{n}", "createdAt": "c",  # noqa: E731
                                "mergedAt": "m", "closedAt": "m", "repository": {"nameWithOwner": repo}}
        pages = [
            {"user": {"pullRequests": {"pageInfo": {"hasNextPage": True, "endCursor": "X"},
                                       "nodes": [node(1, "a/b"), node(2, "santichausis/mine")]}}},
            {"user": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                       "nodes": [node(3, "c/d")]}}},
        ]
        with mock.patch.object(u.ghapi, "graphql", side_effect=pages) as gq:
            out = u.fetch_merged_prs_graphql()
        self.assertEqual([p["full"] for p in out], ["a/b", "c/d"])
        self.assertEqual(out[0]["title"], "T1")  # clean_title también aplica acá
        self.assertEqual(gq.call_args_list[1].args[1]["cursor"], "X")


# ------------------------------------------------------------------ actividad de PRs abiertos

def user(login, kind="User"):
    return {"login": login, "type": kind}


def review(login, state, at):
    return {"user": user(login), "state": state, "submitted_at": at}


def comment(login, at, kind="User"):
    return {"user": user(login, kind), "created_at": at}


def commit(login, at):
    return {"author": user(login) if login else None, "commit": {"committer": {"date": at}}}


# Forma real de la API para PokeAPI/pokeapi#1690 (30-sep-2026): un reviewer pidió cambios y
# vos respondiste con un commit y un review 4 horas después. `created_at` es aproximado.
PR_1690 = pr("PokeAPI/pokeapi", 1690, created_at="2026-09-30T05:00:00Z", merged_at="")
REVIEWS_1690 = [review("FallenDeity", "CHANGES_REQUESTED", "2026-09-30T07:44:01Z"),
                review("santichausis", "COMMENTED", "2026-09-30T11:53:33Z")]
REVIEW_COMMENTS_1690 = [comment("FallenDeity", "2026-09-30T07:43:08Z"),
                        comment("santichausis", "2026-09-30T11:53:33Z")]
COMMITS_1690 = [commit("santichausis", "2026-09-30T11:53:16Z")]


def classify(pr_, comments=(), reviews=(), review_comments=(), commits=(), today=TODAY):
    events = u.pr_events(list(comments), list(reviews), list(review_comments), list(commits))
    return u.classify_pr(pr_, events, today, stale_days=14)


class ClassifyTests(unittest.TestCase):
    def test_changes_requested_that_you_already_answered_is_not_needs_action(self):
        """Regresión: la búsqueda `review:changes_requested` marcaba este PR aunque ya respondiste."""
        st = classify(PR_1690, reviews=REVIEWS_1690, review_comments=REVIEW_COMMENTS_1690, commits=COMMITS_1690)
        self.assertEqual(st["state"], "ok")
        self.assertEqual(st["idle_days"], 9)

    def test_same_pr_becomes_stale_after_the_threshold(self):
        st = classify(PR_1690, reviews=REVIEWS_1690, review_comments=REVIEW_COMMENTS_1690,
                      commits=COMMITS_1690, today=date(2026, 10, 20))
        self.assertEqual(st["state"], "stale")
        self.assertEqual(st["idle_days"], 20)

    def test_changes_requested_after_your_last_activity_needs_action(self):
        st = classify(PR_1690, reviews=REVIEWS_1690 + [review("FallenDeity", "CHANGES_REQUESTED", "2026-10-03T09:00:00Z")],
                      commits=COMMITS_1690)
        self.assertEqual((st["state"], st["who"], st["idle_days"]), ("changes_requested", "FallenDeity", 6))

    def test_plain_reply_after_your_last_activity_is_awaiting_reply(self):
        st = classify(PR_1690, comments=[comment("maint", "2026-10-02T09:00:00Z")], commits=COMMITS_1690)
        self.assertEqual((st["state"], st["who"], st["idle_days"]), ("awaiting_reply", "maint", 7))

    def test_bots_are_ignored(self):
        st = classify(PR_1690, comments=[comment("vercel[bot]", "2026-10-08T09:00:00Z"),
                                         comment("coderabbit", "2026-10-08T09:00:00Z", kind="Bot")],
                      commits=COMMITS_1690)
        self.assertEqual(st["state"], "ok")

    def test_approval_does_not_ask_for_a_reply_and_is_flagged(self):
        st = classify(PR_1690, reviews=[review("maint", "APPROVED", "2026-10-05T09:00:00Z")], commits=COMMITS_1690)
        self.assertEqual(st["state"], "ok")
        self.assertTrue(st["approved"])

    def test_maintainer_commit_on_your_branch_does_not_ask_for_a_reply(self):
        st = classify(PR_1690, commits=COMMITS_1690 + [commit("maint", "2026-10-08T09:00:00Z")])
        self.assertEqual(st["state"], "ok")

    def test_commit_with_unlinked_author_counts_as_yours(self):
        st = classify(PR_1690, comments=[comment("maint", "2026-10-02T09:00:00Z")],
                      commits=[commit(None, "2026-10-03T09:00:00Z")])
        self.assertEqual(st["state"], "ok")

    def test_pr_with_no_activity_at_all_goes_stale_from_creation(self):
        old = pr("a/b", 5, created_at="2026-08-01T00:00:00Z", merged_at="")
        st = classify(old)
        self.assertEqual((st["state"], st["idle_days"]), ("stale", 69))

    def test_draft_is_never_nudged(self):
        old = pr("a/b", 5, created_at="2026-01-01T00:00:00Z", merged_at="", draft=True)
        self.assertEqual(classify(old)["state"], "draft")

    def test_pending_reviews_are_ignored(self):
        st = classify(PR_1690, reviews=[{"user": user("maint"), "state": "PENDING", "submitted_at": None}],
                      commits=COMMITS_1690)
        self.assertEqual(st["state"], "ok")


class OpenStatusTests(unittest.TestCase):
    def test_unreadable_pr_becomes_unknown_and_warns_instead_of_crashing(self):
        for exc in (urllib.error.URLError("down"), KeyError("commit")):
            with mock.patch.object(u, "fetch_pr_events", side_effect=exc):
                out, warnings = u.fetch_open_pr_status([PR_1690], TODAY)
            self.assertEqual(out[0]["status"]["state"], "unknown")
            self.assertIn("PokeAPI/pokeapi#1690", warnings[0])


# ------------------------------------------------------------------ README

class BuildSectionTests(unittest.TestCase):
    def setUp(self):
        self.prs = [pr("small/repo", 1, merged_at="2026-09-01T00:00:00Z"),
                    pr("big/repo", 2, merged_at="2026-09-02T00:00:00Z"),
                    pr("big/repo", 3, merged_at="2026-09-05T00:00:00Z", title="Has | pipe")]
        self.meta = meta_of(small__repo=10, big__repo=72700)

    def test_stars_use_a_non_breaking_space(self):
        """Regresión: un espacio normal deja que el navegador parta '⭐ 72.7k' en dos líneas."""
        section = u.build_section(self.prs, self.meta)
        self.assertIn("⭐" + chr(0xA0) + "72.7k", section)
        self.assertNotIn("⭐ 72.7k", section)

    def test_sorted_by_stars_with_counts_and_latest_merge(self):
        section = u.build_section(self.prs, self.meta)
        self.assertLess(section.index("big/repo"), section.index("small/repo"))
        self.assertIn("**3 PRs merged · 2 repos**", section)
        self.assertIn("[#3](https://github.com/big/repo/pull/3)", section)  # el más reciente

    def test_pipe_in_a_title_is_escaped_so_the_table_does_not_break(self):
        self.assertIn("Has \\| pipe", u.build_section(self.prs, self.meta))

    def test_summary_text_comes_from_config(self):
        self.assertIn(f"· {u.SUMMARY_SUFFIX}", u.build_section(self.prs, self.meta))
        self.assertNotIn("every Monday", u.SUMMARY_SUFFIX)

    def test_curated_override_wins_over_the_raw_title(self):
        with mock.patch.dict(u.PR_OVERRIDES, {"a/b#9": "Curated text"}):
            self.assertEqual(u.desc_for(pr("a/b", 9)), "Curated text")


class FormatTests(unittest.TestCase):
    def test_format_stars(self):
        self.assertEqual([u.format_stars(n) for n in (0, 999, 1000, 1049, 2000, 72700)],
                         ["0", "999", "1k", "1k", "2k", "72.7k"])

    def test_clean_title_strips_conventional_prefixes_only(self):
        self.assertEqual(u.clean_title("fix: handle null"), "Handle null")
        self.assertEqual(u.clean_title("Fix(content-manager): validate"), "Fix(content-manager): validate")
        self.assertEqual(u.clean_title("Update docs: usage"), "Update docs: usage")

    def test_iso_week_label_pads_and_uses_iso_year(self):
        self.assertEqual(u.iso_week_label(date(2026, 1, 5)), "2026-W02")
        self.assertEqual(u.iso_week_label(date(2024, 12, 30)), "2025-W01")


class ConfigTests(unittest.TestCase):
    def test_config_file_is_valid_and_complete(self):
        cfg = u.load_config()
        for key in u.DEFAULT_CONFIG:
            self.assertIn(key, cfg)
        for k, v in cfg["pr_overrides"].items():
            self.assertRegex(k, r"^[^/\s]+/[^#\s]+#\d+$")
            self.assertTrue(v.strip())

    def test_missing_keys_fall_back_to_defaults(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"stale_days": 3}, f)
        try:
            cfg = u.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(cfg["stale_days"], 3)
        self.assertEqual(cfg["min_retention_ratio"], u.DEFAULT_CONFIG["min_retention_ratio"])


class StateTests(unittest.TestCase):
    def test_roundtrip_and_deterministic_serialization(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            u.save_state({"b/b": 2, "a/a": 1}, {"x#2", "x#1"}, "2026-W41", 39, path=path)
            with open(path, encoding="utf-8") as f:
                first = f.read()
            state = u.load_state(path)
            u.save_state(state["stars"], state["known_prs"], state["last_emailed_week"], state["followers"], path=path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(first, f.read())  # byte-idéntico → git no ve diff
        self.assertEqual(state["known_prs"], {"x#1", "x#2"})
        self.assertEqual((state["last_emailed_week"], state["followers"]), ("2026-W41", 39))

    def test_missing_file_gives_empty_baseline(self):
        state = u.load_state("/nonexistent/state.json")
        self.assertEqual((state["stars"], state["known_prs"], state["followers"]), ({}, set(), None))


# ------------------------------------------------------------------ mail

def open_pr(num, state, idle=3, who="", approved=False, created="2026-10-01T00:00:00Z", title=None):
    p = pr("o/r", num, created_at=created, merged_at="", title=title or f"Open {num}")
    p["status"] = {"state": state, "idle_days": idle, "who": who, "approved": approved}
    return p


def email(**overrides):
    args = dict(content_changed=False, new_pr_list=[], new_repo_list=[], stars_diff=[], milestones=[],
                streak=0, open_status=[], recently_closed=[], total_prs=10, total_repos=3,
                followers=41, followers_delta=None, warnings=[], today=TODAY)
    args.update(overrides)
    return u.build_email(**args)


class BuildEmailTests(unittest.TestCase):
    def test_subjects(self):
        self.assertIn("+2 PRs", email(new_pr_list=[pr(num=1), pr(num=2)])[0])
        self.assertIn("+1 PR)", email(new_pr_list=[pr(num=1)])[0])
        self.assertEqual(email(content_changed=True)[0], "✅ Your GitHub profile updated")
        self.assertIn("no new merges", email()[0])

    def test_followers_line_colors_and_absence(self):
        self.assertIn("#1a7f37", email(followers_delta=3)[1])
        self.assertIn("-2 since last update", email(followers_delta=-2)[1])
        self.assertNotIn("since last update", email(followers_delta=0)[1])
        self.assertNotIn("followers", email(followers=None)[1])

    def test_titles_are_html_escaped(self):
        body = email(open_status=[open_pr(1, "ok", title="<script>alert(1)</script> & co")])[1]
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_needs_reply_section_orders_by_waiting_time_and_names_who(self):
        body = email(open_status=[open_pr(1, "awaiting_reply", idle=2, who="alice"),
                                  open_pr(2, "changes_requested", idle=9, who="bob")])[1]
        self.assertIn("Needs your reply", body)
        self.assertLess(body.index("bob"), body.index("alice"))
        self.assertIn("changes requested by bob", body)
        self.assertIn("unanswered 9d", body)

    def test_nudge_section_flags_approved(self):
        body = email(open_status=[open_pr(1, "stale", idle=40, approved=True)])[1]
        self.assertIn("Worth a nudge", body)
        self.assertIn("approved", body)
        self.assertIn("no activity for 40d", body)

    def test_other_open_prs_mark_aged_and_draft(self):
        body = email(open_status=[open_pr(1, "ok", created="2026-08-01T00:00:00Z"),
                                  open_pr(2, "draft", created="2026-10-08T00:00:00Z")])[1]
        self.assertIn("Other open PRs (2)", body)
        self.assertEqual(body.count("🕰"), 1)
        self.assertIn("draft", body)

    def test_no_empty_sections(self):
        body = email()[1]
        for header in ("Needs your reply", "Worth a nudge", "Other open PRs", "New merged PRs", "Star changes"):
            self.assertNotIn(header, body)

    def test_star_changes_show_top_n_and_collapse_the_rest(self):
        diff = [(f"r/{i}", 100 - i) for i in range(7)]
        body = email(stars_diff=diff, top_star_changes=5)[1]
        self.assertIn("r/4", body)
        self.assertNotIn("r/5", body)
        self.assertIn("+2 more repos with smaller changes (net +189 ⭐)", body)

    def test_data_warnings_are_shown_escaped(self):
        body = email(warnings=["Stars for a/b could not be fetched (<urlopen error>)"])[1]
        self.assertIn("Data notes", body)
        self.assertIn("&lt;urlopen error&gt;", body)


class SafeTests(unittest.TestCase):
    def test_api_failure_returns_default_and_warns(self):
        warnings = []
        out = u.safe(lambda: (_ for _ in ()).throw(urllib.error.URLError("x")), [], "Thing", warnings)
        self.assertEqual(out, [])
        self.assertIn("Thing unavailable", warnings[0])

    def test_programming_errors_are_not_swallowed(self):
        with self.assertRaises(ZeroDivisionError):
            u.safe(lambda: 1 / 0, None, "Thing", [])


if __name__ == "__main__":
    unittest.main()
