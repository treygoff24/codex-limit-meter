"""Tests use synthetic rollout logs generated here; nothing is read from a real Codex home."""
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import codex_limit_meter as m

BASE = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)
RESET = 1790000000
PRICES = {  # in = uncached input, cached, out; per 1M tokens
    "_fast_mult": 2.5, "_ultrafast_mult": 8.0,
    "_long_context": {"threshold_input_tokens": 272000, "input_mult": 2.0, "output_mult": 1.5},
    "test-model": {"in": 2.0, "cached": 0.5, "out": 10.0},
    "other-model": {"in": 1.0, "cached": 0.1, "out": 5.0},
}
OPTS = dict(max_minutes=90, max_step=3, event_every_min=10, model_share=0.9, min_model_ticks=1,
            min_segment_points=0, split_dates=[])
# 100k input (50k cached) + 25k output at test-model = 0.1 + 0.025 + 0.25 = $0.375. Doubled it
# is still under the 272k long-context threshold.
UNIT = dict(inp=100_000, cached=50_000, out=25_000)
UNIT_COST = 0.375


def stamp(minutes):
    return (BASE + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ev(minute, pct, scale=1, model="test-model", plan="pro", limit_id="codex", reset=RESET,
       tier=None, cum=None, inp=None, cached=None, out=None):
    return dict(minute=minute, pct=pct, scale=scale, model=model, plan=plan, limit_id=limit_id,
                reset=reset, tier=tier, cum=cum, inp=inp, cached=cached, out=out)


def write_rollout(home, name, events, account_id=None, session_id=None, day="2026/09/10"):
    """Write a rollout file. Events with the same model share a turn_context line."""
    d = Path(home) / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rollout-{name}.jsonl"
    lines = [{"timestamp": stamp(0), "type": "session_meta",
              "payload": {"id": session_id or name, "creator_account_id": account_id}}]
    model = tier = None
    total = 0
    for i, e in enumerate(events):
        if e["model"] != model or e["tier"] != tier:
            model, tier = e["model"], e["tier"]
            ctx = {"model": model, "effort": "high"}
            if tier:
                ctx["service_tier"] = tier
            lines.append({"timestamp": stamp(e["minute"]), "type": "turn_context", "payload": ctx})
        inp = e["inp"] if e["inp"] is not None else UNIT["inp"] * e["scale"]
        cached = e["cached"] if e["cached"] is not None else UNIT["cached"] * e["scale"]
        out = e["out"] if e["out"] is not None else UNIT["out"] * e["scale"]
        total += inp + out
        window = {"used_percent": e["pct"], "window_minutes": 10080, "resets_at": e["reset"]}
        lines.append({"timestamp": stamp(e["minute"]), "type": "event_msg", "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                           "output_tokens": out, "reasoning_output_tokens": 0},
                     "total_token_usage": {"total_tokens": e["cum"] if e["cum"] is not None else total}},
            "rate_limits": {"limit_id": e["limit_id"], "plan_type": e["plan"], "primary": window,
                            "secondary": None}}})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def run_analysis(homes, prices=PRICES, **over):
    rows, _ = m.scan_homes(homes)
    rows = m.prepare(rows)
    return m.analyze(rows, prices, {**OPTS, **over})


def busy_events():
    """Levels tick 11 (m10), 12 (m20), 13 (m30), 14 (m40): three intervals of known cost, then a
    2h gap tick and a 5-point jump that must both be rejected as not busy."""
    return [
        ev(0, 10), ev(5, 10), ev(10, 11),
        ev(15, 11), ev(20, 12),                 # interval (10,20]: 2 units
        ev(25, 12), ev(30, 13, scale=2),        # interval (20,30]: 1 + 2 units
        ev(35, 13), ev(40, 14),                 # interval (30,40]: 2 units
        ev(200, 15),                            # 160 minutes since last tick: not busy
        ev(205, 20),                            # +5 points in one tick: not busy
    ]


class CostTests(unittest.TestCase):
    def row(self, **kw):
        return {"model": "test-model", "in": UNIT["inp"], "cached": UNIT["cached"], "out": UNIT["out"], **kw}

    def test_cached_input_is_subtracted_from_input(self):
        # uncached 50k * $2 + cached 50k * $0.5 + out 25k * $10 = 0.1 + 0.025 + 0.25
        self.assertAlmostEqual(m.event_cost(self.row(), PRICES), UNIT_COST)

    def test_output_includes_reasoning_so_it_is_not_added_twice(self):
        r = self.row(reason=20_000)
        self.assertAlmostEqual(m.event_cost(r, PRICES), UNIT_COST)

    def test_fast_and_ultrafast_multipliers(self):
        self.assertAlmostEqual(m.event_cost(self.row(tier="fast"), PRICES), UNIT_COST * 2.5)
        self.assertAlmostEqual(m.event_cost(self.row(tier="priority"), PRICES), UNIT_COST * 2.5)
        self.assertAlmostEqual(m.event_cost(self.row(tier="ultrafast"), PRICES), UNIT_COST * 8)
        self.assertAlmostEqual(m.event_cost(self.row(tier="default"), PRICES), UNIT_COST)

    def test_long_context_doubles_input_rates_and_scales_output(self):
        r = self.row(**{"in": 300_000, "cached": 100_000, "out": 10_000})
        # (200k*2 + 100k*0.5)*2 + 10k*10*1.5 = 0.9 + 0.15
        self.assertAlmostEqual(m.event_cost(r, PRICES), 1.05)
        at_limit = self.row(**{"in": 272_000, "cached": 0, "out": 0})
        self.assertAlmostEqual(m.event_cost(at_limit, PRICES), 0.544)

    def test_unknown_model_has_no_price(self):
        self.assertIsNone(m.event_cost(self.row(model="mystery"), PRICES))
        self.assertIsNone(m.event_cost(self.row(model=None), PRICES))

    def test_bundled_prices_are_well_formed(self):
        p = m.load_prices()
        self.assertEqual(p["_fetched"], "2026-09-30")
        self.assertTrue(all(u.startswith("https://") for u in p["_sources"].values()))
        for name, rate in p.items():
            if not name.startswith("_"):
                self.assertEqual(set(rate), {"in", "cached", "out"}, name)


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def test_repeated_token_count_lines_count_once(self):
        # Codex re-logs the same cumulative total later; only the first is new work.
        write_rollout(self.home, "a", [ev(0, 10, cum=100), ev(1, 10, cum=100), ev(2, 10, cum=200)])
        rows = m.prepare(m.scan_homes([self.home])[0])
        events, _ = m.token_events(rows, PRICES)
        self.assertEqual(len(rows), 3)
        self.assertEqual([e["cum_total"] for e in events], [100, 200])

    def test_copy_of_a_session_in_another_home_counts_once(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        evs = [ev(0, 10), ev(5, 10)]
        write_rollout(self.home, "a", evs, session_id="s1")
        write_rollout(other.name, "a-copy", evs, session_id="s1")
        rows = m.prepare(m.scan_homes([self.home, other.name])[0])
        self.assertEqual(len(m.token_events(rows, PRICES)[0]), 2)

    def test_realpath_dedupe_of_symlinked_home(self):
        write_rollout(self.home, "a", [ev(0, 10)])
        link = Path(self.tmp.name + "-link")
        os.symlink(self.home, link)
        self.addCleanup(link.unlink)
        _, n_files = m.scan_homes([self.home, str(link)])
        self.assertEqual(n_files, 1)

    def test_non_codex_limit_ids_are_excluded_from_readings_and_tokens(self):
        write_rollout(self.home, "a", [
            ev(0, 10), ev(5, 11, limit_id="codex_bengalfox", plan="pro"), ev(6, 90, limit_id="codex_bengalfox"),
            ev(10, 11, limit_id=None)])
        rows = m.prepare(m.scan_homes([self.home])[0])
        events, _ = m.token_events(rows, PRICES)
        self.assertEqual(len(events), 2)
        self.assertEqual([r["w_pct"] for r in rows], [10, None, None, 11])

    def test_since_filters_events_by_date(self):
        write_rollout(self.home, "a", [ev(0, 10), ev(5, 11)])
        rows, _ = m.scan_homes([self.home])
        self.assertEqual(len(m.prepare(list(rows), dt.date(2026, 9, 11))), 0)
        self.assertEqual(len(m.prepare(list(rows), dt.date(2026, 9, 10))), 2)


class SegmentTests(unittest.TestCase):
    @staticmethod
    def readings(pcts, resets=None):
        return [{"t": i * 60.0, "ts": stamp(i), "w_pct": p, "w_reset": (resets or [RESET] * len(pcts))[i]}
                for i, p in enumerate(pcts)]

    def test_meter_reset_starts_a_new_segment(self):
        segs = m.split_segments(self.readings([50, 51, 52, 1, 2, 3]))
        self.assertEqual([[r["w_pct"] for r in s] for s in segs], [[50, 51, 52], [1, 2, 3]])

    def test_slightly_stale_reading_does_not_split(self):
        segs = m.split_segments(self.readings([50, 52, 51, 53]))
        self.assertEqual(len(segs), 1)

    def test_resets_at_rolling_forward_starts_a_new_segment(self):
        segs = m.split_segments(self.readings([10, 11, 12, 12], resets=[RESET, RESET, RESET + 604800, RESET + 604800]))
        self.assertEqual([len(s) for s in segs], [2, 2])

    def test_ticks_are_first_sight_of_each_higher_level(self):
        seg = self.readings([10, 10, 11, 11, 13, 12, 13, 14])
        ticks = m.segment_ticks(seg)
        self.assertEqual([(t[2], t[3]) for t in ticks], [(11, 1), (13, 2), (14, 1)])

    def test_ticks_do_not_bridge_a_reset(self):
        segs = m.split_segments(self.readings([50, 51, 1, 2]))
        self.assertEqual([[t[2] for t in m.segment_ticks(s)] for s in segs], [[51], [2]])


class TickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def test_busy_ticks_and_dollars_per_percent_hand_computed(self):
        write_rollout(self.home, "a", busy_events(), account_id="acct-1")
        res = run_analysis([self.home])
        (row,) = res["ticks"]
        self.assertEqual(row["busy_ticks"], 3)
        vals = sorted([2 * UNIT_COST, 3 * UNIT_COST, 2 * UNIT_COST])
        self.assertAlmostEqual(row["median_per_pct"], vals[1])           # the median, not the mean
        self.assertAlmostEqual(row["implied_week"], vals[1] * 100)
        self.assertAlmostEqual(row["q3"], 3 * UNIT_COST * 0.5 + 2 * UNIT_COST * 0.5)  # midpoint of 2 and 3 units
        self.assertEqual(row["period"], "pro")

    def test_non_busy_intervals_are_rejected(self):
        # Only the gap tick and the 5-point tick, with logged events: neither qualifies.
        write_rollout(self.home, "a", [ev(0, 10), ev(10, 11), ev(200, 12), ev(205, 17)], account_id="acct-1")
        self.assertEqual(run_analysis([self.home])["ticks"], [])

    def test_sparse_events_inside_the_window_are_not_busy(self):
        # 60 minute interval with 2 logged events: needs >= 6.
        write_rollout(self.home, "a", [ev(0, 10), ev(5, 11), ev(35, 11), ev(65, 12)], account_id="acct-1")
        self.assertEqual(run_analysis([self.home])["ticks"], [])

    def test_other_meters_do_not_leak_into_ticks_or_cost(self):
        evs = busy_events()
        evs += [ev(m_, 99, limit_id="codex_bengalfox", scale=50) for m_ in (12, 14, 22, 33)]
        write_rollout(self.home, "a", sorted(evs, key=lambda e: e["minute"]), account_id="acct-1")
        (row,) = run_analysis([self.home])["ticks"]
        self.assertAlmostEqual(row["median_per_pct"], 2 * UNIT_COST)

    def test_periods_split_by_plan_and_split_date(self):
        evs = [ev(0, 10), ev(5, 10), ev(10, 11), ev(15, 11), ev(20, 12)]
        write_rollout(self.home, "a", evs, account_id="acct-1", day="2026/09/10")
        late = [ev(0, 10, plan="promax"), ev(10, 11, plan="promax"), ev(15, 11, plan="promax"),
                ev(20, 12, plan="promax"), ev(25, 12, plan="promax"), ev(30, 13, plan="promax")]
        # same clock, 12 days later (BASE + 17280 minutes), different reset -> its own weekly segment
        late = [{**e, "minute": e["minute"] + 17280, "reset": RESET + 1_000_000} for e in late]
        write_rollout(self.home, "b", late, account_id="acct-1", day="2026/09/22")
        res = run_analysis([self.home], split_dates=["2026-09-15"])
        self.assertEqual([r["period"] for r in res["ticks"]],
                         ["pro, through 2026-09-14", "promax, from 2026-09-15"])

    def test_pure_model_table_needs_dominant_model(self):
        evs = [ev(0, 10), ev(10, 11),
               ev(15, 11, model="other-model"), ev(20, 12, model="other-model"),      # all other-model
               ev(25, 12), ev(30, 13, model="other-model", scale=0)]                   # test-model dominates
        write_rollout(self.home, "a", evs, account_id="acct-1")
        res = run_analysis([self.home])
        by_model = {r["model"]: r for r in res["models"]}
        self.assertEqual(set(by_model), {"other-model", "test-model"})
        other_unit = (50_000 * 1.0 + 50_000 * 0.1 + 25_000 * 5.0) / 1e6
        self.assertAlmostEqual(by_model["other-model"]["median_per_pct"], 2 * other_unit)

    def test_unknown_model_is_reported_and_its_intervals_are_not_priced(self):
        evs = [ev(0, 10), ev(10, 11), ev(15, 11, model="mystery"), ev(20, 12, model="mystery"),
               ev(25, 12), ev(30, 13)]
        write_rollout(self.home, "a", evs, account_id="acct-1")
        res = run_analysis([self.home])
        self.assertEqual(res["unknown_models"], {"mystery": 2})
        (row,) = res["ticks"]
        self.assertEqual(row["busy_ticks"], 1)          # only the interval with known models
        self.assertEqual(row["skipped_unpriced"], 1)
        self.assertAlmostEqual(row["median_per_pct"], 2 * UNIT_COST)

    def test_whole_segment_table_spans_first_to_last_tick(self):
        write_rollout(self.home, "a", busy_events()[:9], account_id="acct-1")
        (seg,) = run_analysis([self.home])["segments"]
        self.assertEqual((seg["from_percent"], seg["to_percent"], seg["percent_moved"]), (11, 14, 3))
        self.assertAlmostEqual(seg["cost"], 7 * UNIT_COST)   # events after m10 up to m40: 2+3+2 units
        self.assertAlmostEqual(seg["per_pct"], 7 * UNIT_COST / 3)


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def home(self, name):
        p = self.root / name
        p.mkdir()
        return str(p)

    def attribute(self, homes):
        rows = m.prepare(m.scan_homes(homes)[0])
        stats = m.attribute(rows)
        return {Path(r["file"]).name: r["acct"] for r in rows}, stats

    def test_file_without_account_id_is_attributed_by_reset_fingerprint(self):
        h1, h2 = self.home("h1"), self.home("h2")
        write_rollout(h1, "with-id", [ev(0, 10, reset=RESET), ev(5, 11, reset=RESET + 1)], account_id="acct-A")
        write_rollout(h2, "no-id", [ev(30, 12, reset=RESET + 2), ev(35, 13, reset=RESET + 2)])
        write_rollout(h2, "other-week", [ev(30, 12, reset=RESET + 700000)], day="2026/09/11")
        got, stats = self.attribute([h1, h2])
        self.assertEqual(got["rollout-no-id.jsonl"], "acct-A")
        self.assertIsNone(got["rollout-other-week.jsonl"])          # unmatched fingerprint stays unattributed
        self.assertEqual(stats["files_by_method"]["fingerprint"], 1)

    def test_home_pinned_by_id_bearing_files_attributes_the_rest(self):
        h = self.home("solo")
        write_rollout(h, "with-id", [ev(0, 10)], account_id="acct-A")
        write_rollout(h, "no-id", [ev(0, 0, reset=RESET + 12345)], day="2026/09/11")  # no fingerprint possible
        got, stats = self.attribute([h])
        self.assertEqual(got["rollout-no-id.jsonl"], "acct-A")
        self.assertEqual(stats["files_by_method"]["pinned home"], 1)

    def test_shared_home_is_not_pinned_when_fingerprint_contradicts(self):
        # One home used by two logins: its id-less file belongs to B (weekly reset known from
        # B's id-bearing file elsewhere), so it must not be handed to A just because A has an id here.
        h1, h2 = self.home("shared"), self.home("b-home")
        write_rollout(h1, "a-file", [ev(0, 10, reset=RESET)], account_id="acct-A")
        write_rollout(h2, "b-file", [ev(0, 40, reset=RESET + 900000)], account_id="acct-B")
        write_rollout(h1, "b-noid", [ev(0, 41, reset=RESET + 900000)], day="2026/09/11")
        got, _ = self.attribute([h1, h2])
        self.assertEqual(got["rollout-b-noid.jsonl"], "acct-B")

    def test_explicit_id_wins(self):
        h = self.home("h")
        write_rollout(h, "x", [ev(0, 10, reset=RESET)], account_id="acct-A")
        write_rollout(h, "y", [ev(0, 10, reset=RESET)], account_id="acct-B", day="2026/09/11")
        got, _ = self.attribute([h])
        self.assertEqual((got["rollout-x.jsonl"], got["rollout-y.jsonl"]), ("acct-A", "acct-B"))


def tick_events(start_minute, start_pct, step, ticks, plan, reset, model_dollars):
    """`ticks` busy ticks of `step` points each; every tick has two events (5 and 10 minutes after the
    previous one) sharing `model_dollars` {model: card dollars} equally. Test models price output
    at $10 per 1M tokens, so dollars * 100_000 output tokens."""
    out, pct = [], start_pct
    for i in range(ticks):
        for half in (5, 10):
            if half == 10:
                pct += step
            for model, dollars in model_dollars.items():
                out.append(ev(start_minute + 10 * i + half, pct, model=model, plan=plan, reset=reset,
                              inp=0, cached=0, out=int(dollars / 2 * 100_000)))
    return out


COMPARE_PRICES = {**PRICES, "_long_context": None,
                  **{f"model-{x}": {"in": 0.0, "cached": 0.0, "out": 10.0} for x in "abc"}}


class CompareTests(unittest.TestCase):
    """Baseline weights: model-a moves the meter 1% per $10, model-b 1% per $20. The target plan
    moves it 2 points for the same work, i.e. exactly half the allowance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        evs = self.laid_end_to_end([{"model-a": 10}, {"model-b": 20}, {"model-a": 5, "model-b": 10}],
                                   step=1, ticks=12, plan="pro", reset=RESET)
        write_rollout(self.home, "base", evs, account_id="acct-1", day="2026/09/10")

    @staticmethod
    def laid_end_to_end(kinds, step, ticks, plan, reset):
        out, pct, minute = [], 10, 0
        for kind in kinds:
            part = tick_events(minute, pct, step, ticks, plan, reset, kind)
            out += part
            pct, minute = part[-1]["pct"], part[-1]["minute"]
        return out

    def write_target(self, kinds=({"model-a": 10}, {"model-b": 20}), step=2, plan="promax"):
        evs = self.laid_end_to_end(kinds, step, 6, plan, RESET + 5_000_000)
        write_rollout(self.home, "target", [{**e, "minute": e["minute"] + 20 * 1440} for e in evs],
                      account_id="acct-1", day="2026/09/30")

    def run_compare(self, **kw):
        rows = m.prepare(m.scan_homes([self.home])[0])
        args = dict(rows=rows, prices=COMPARE_PRICES, opts=OPTS, baseline="plan=pro", target="plan=promax")
        args.update(kw)
        return m.compare(**args)

    def test_recovers_half_allowance(self):
        self.write_target()
        res = self.run_compare()
        self.assertEqual((res["baseline_ticks"], res["target_ticks"]), (35, 11))  # first tick of a week only anchors
        self.assertAlmostEqual(res["ratio"], 0.5, places=6)
        lo, hi = res["ratio_ci90"]
        self.assertTrue(0.49 <= lo <= 0.5 <= hi <= 0.51, (lo, hi))
        weights = {g["group"]: g["card_dollars_per_pct"] for g in res["groups"]}
        self.assertAlmostEqual(weights["model-a"], 10.0, places=6)
        self.assertAlmostEqual(weights["model-b"], 20.0, places=6)

    def test_same_allowance_gives_ratio_one(self):
        self.write_target(step=1)
        self.assertAlmostEqual(self.run_compare()["ratio"], 1.0, places=6)

    def test_target_model_absent_from_baseline_needs_a_map(self):
        self.write_target(kinds=({"model-c": 20},))
        with self.assertRaises(SystemExit) as cm:
            self.run_compare()
        self.assertIn("--map model-c=", str(cm.exception))
        self.assertAlmostEqual(self.run_compare(mapping={"model-c": "model-b"})["ratio"], 0.5, places=6)

    def test_price_override_changes_the_target_cost(self):
        over = m.load_prices(None, overrides=["gpt-6.1-sol:cached=0.20"])
        self.assertEqual(over["gpt-6.1-sol"]["cached"], 0.2)
        self.assertEqual(m.load_prices()["gpt-6.1-sol"]["cached"], 0.1)
        # model-c work at twice the card price: the same meter movement now means the same allowance.
        self.write_target(kinds=({"model-c": 20},))
        doubled = {**COMPARE_PRICES, "model-c": {"in": 0.0, "cached": 0.0, "out": 20.0}}
        res = self.run_compare(prices=doubled, mapping={"model-c": "model-b"})
        self.assertAlmostEqual(res["ratio"], 1.0, places=6)

    def test_bad_override_and_selector_are_rejected(self):
        with self.assertRaises(SystemExit):
            m.load_prices(None, overrides=["no-such-model:cached=1"])
        with self.assertRaises(SystemExit):
            m.load_prices(None, overrides=["gpt-6-sol:cached"])
        with self.assertRaises(SystemExit):
            m.parse_selector("plane=pro")

    def test_selector_plan_and_date_bounds(self):
        match = m.parse_selector("plan=pro,after=2026-09-01,before=2026-09-29")
        self.assertTrue(match("pro", "2026-09-28T23:59:00.000Z"))
        self.assertTrue(match("pro", "2026-09-01T00:00:00.000Z"))
        self.assertFalse(match("pro", "2026-09-29T00:00:00.000Z"))
        self.assertFalse(match("promax", "2026-09-10T00:00:00.000Z"))

    def test_cli_compare_json(self):
        self.write_target()
        prices = Path(self.tmp.name) / "p.json"
        prices.write_text(json.dumps(COMPARE_PRICES))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.main(["compare", "--home", self.home, "--prices", str(prices), "--baseline", "plan=pro",
                    "--target", "plan=promax", "--json"])
        self.assertAlmostEqual(json.loads(out.getvalue())["ratio"], 0.5, places=6)


class PriceOptionTests(unittest.TestCase):
    def test_long_context_surcharge_is_off_unless_requested(self):
        self.assertIsNone(m.load_prices()["_long_context"])
        self.assertEqual(m.load_prices(long_context=True)["_long_context"]["threshold_input_tokens"], 272000)
        row = {"model": "gpt-6-astra", "in": 300_000, "cached": 0, "out": 0}
        self.assertAlmostEqual(m.event_cost(row, m.load_prices()), 3.0)
        self.assertAlmostEqual(m.event_cost(row, m.load_prices(long_context=True)), 6.0)

    def test_bundled_ultrafast_multiplier_is_8x(self):
        row = {"model": "gpt-6-astra", "in": 100_000, "cached": 0, "out": 0, "tier": "ultrafast"}
        self.assertAlmostEqual(m.event_cost(row, m.load_prices()), 1.0 * 8)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        write_rollout(self.home, "a", busy_events(), account_id="00000000-aaaa-bbbb-cccc-123456789abc")
        prices = {k: v for k, v in PRICES.items()}
        self.prices_path = Path(self.tmp.name) / "prices-test.json"
        self.prices_path.write_text(json.dumps(prices))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = m.main(list(argv))
        self.assertEqual(code, 0)
        return out.getvalue(), err.getvalue()

    def test_json_report_is_anonymized_and_show_ids_reveals(self):
        out, _ = self.run_cli("report", "--home", self.home, "--prices", str(self.prices_path), "--json")
        doc = json.loads(out)
        self.assertEqual(doc["ticks"][0]["account"], "account A")
        self.assertNotIn("123456789abc", out)
        self.assertAlmostEqual(doc["ticks"][0]["median_per_pct"], 2 * UNIT_COST)
        self.assertIn("segments_warning", doc)
        out, _ = self.run_cli("report", "--home", self.home, "--prices", str(self.prices_path),
                              "--json", "--show-ids")
        self.assertEqual(json.loads(out)["ticks"][0]["account"], "00000000-aaaa-bbbb-cccc-123456789abc")

    def test_text_report_warns_about_unlogged_usage_and_unknown_models(self):
        write_rollout(self.home, "z", [ev(300, 1, model="mystery")], day="2026/09/12")
        out, err = self.run_cli("report", "--home", self.home, "--prices", str(self.prices_path))
        self.assertIn("never reaches local logs", out)
        self.assertIn("mystery", err)

    def test_scan_out_then_events_jsonl_reproduces_the_report(self):
        cache = Path(self.tmp.name) / "events.jsonl"
        self.run_cli("scan", "--home", self.home, "--out", str(cache), "--prices", str(self.prices_path))
        direct, _ = self.run_cli("report", "--home", self.home, "--prices", str(self.prices_path), "--json")
        cached, _ = self.run_cli("report", "--events-jsonl", str(cache), "--prices", str(self.prices_path), "--json")
        self.assertEqual(json.loads(direct)["ticks"], json.loads(cached)["ticks"])


if __name__ == "__main__":
    unittest.main()
