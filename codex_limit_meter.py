#!/usr/bin/env python3
"""codex-limit-meter: how much work moves your Codex weekly usage meter by 1%?

Reads local Codex CLI session logs (rollout-*.jsonl), prices the logged tokens at the
Codex rate card (in API-list dollars), and reports the median dollars of logged work per
1% of the weekly meter, by account and plan period.  Standard library only.

    codex_limit_meter.py scan   [--home DIR ...] [--since YYYY-MM-DD] [--out EVENTS.jsonl]
    codex_limit_meter.py report [--home DIR ...] [--since YYYY-MM-DD] [--split-date YYYY-MM-DD ...]
                                [--prices FILE] [--show-ids] [--json]
"""
import argparse
import bisect
import collections
import datetime as dt
import json
import os
import random
import statistics
import sys
from pathlib import Path

WEEK_MINUTES = 10080
DEFAULT_PRICES = Path(__file__).resolve().with_name("prices.json")

# --------------------------------------------------------------------------- parsing


def find_rollouts(homes, since=None):
    """Realpath-deduplicated rollout-*.jsonl paths under each home's sessions/ and
    archived_sessions/. Returns [(home, path)]. `since` (date) skips files last modified before it."""
    seen, out = set(), []
    cutoff = None
    if since:
        cutoff = dt.datetime.combine(since, dt.time.min, dt.timezone.utc).timestamp()
    for home in homes:
        for sub in ("sessions", "archived_sessions"):
            root = Path(home) / sub
            if not root.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(root):
                for name in sorted(files):
                    if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                        continue
                    real = os.path.realpath(os.path.join(dirpath, name))
                    if real in seen:
                        continue
                    seen.add(real)
                    if cutoff is not None:
                        try:
                            if os.path.getmtime(real) < cutoff:
                                continue
                        except OSError:
                            continue
                    out.append((str(home), real))
    return out


def parse_rollout(path, home):
    """Yield one row per token_count event in a rollout file.

    Row keys: home, file, ts, account_id, session_id, model, effort, tier, plan, limit_id,
    p_pct/p_win/p_reset (primary meter), s_pct/s_win/s_reset (secondary meter), has_info,
    in, cached, out, reason (last_token_usage), cum_total (total_token_usage.total_tokens).
    """
    account_id = session_id = None
    model = effort = tier = None
    try:
        fh = open(path, errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            if not ('"session_meta"' in line or '"turn_context"' in line
                    or '"service_tier"' in line or '"token_count"' in line):
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            p = o.get("payload")
            if not isinstance(p, dict):
                continue
            kind = o.get("type")
            if kind == "session_meta":
                if session_id is None:
                    account_id = p.get("creator_account_id")
                    session_id = p.get("id") or p.get("session_id")
                continue
            if kind == "turn_context":
                model = p.get("model", model)
                effort = p.get("effort", effort)
                if "service_tier" in p:
                    tier = p.get("service_tier")
                continue
            settings = p.get("thread_settings")
            if isinstance(settings, dict) and "service_tier" in settings:
                tier = settings["service_tier"]
            if p.get("type") != "token_count":
                continue
            info = p.get("info") or {}
            rl = p.get("rate_limits") or {}
            pr = rl.get("primary") or {}
            se = rl.get("secondary") or {}
            last = info.get("last_token_usage") or {}
            total = info.get("total_token_usage") or {}
            yield {
                "home": home, "file": path, "ts": o.get("timestamp"),
                "account_id": account_id, "session_id": session_id,
                "model": model, "effort": effort, "tier": tier,
                "plan": rl.get("plan_type"), "limit_id": rl.get("limit_id"),
                "p_pct": pr.get("used_percent"), "p_win": pr.get("window_minutes"),
                "p_reset": pr.get("resets_at"),
                "s_pct": se.get("used_percent"), "s_win": se.get("window_minutes"),
                "s_reset": se.get("resets_at"),
                "has_info": bool(info),
                "in": last.get("input_tokens"), "cached": last.get("cached_input_tokens"),
                "out": last.get("output_tokens"), "reason": last.get("reasoning_output_tokens"),
                "cum_total": total.get("total_tokens"),
            }


def scan_homes(homes, since=None):
    rows, n_files = [], 0
    for home, path in find_rollouts(homes, since):
        n_files += 1
        rows.extend(parse_rollout(path, home))
    return rows, n_files


def read_events_jsonl(paths):
    rows = []
    for p in paths:
        with open(p) as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def derive_home(row):
    """Home directory of an event row (older event dumps only carry host + file)."""
    if row.get("home"):
        return row["home"]
    f = row.get("file") or ""
    return (row.get("host") or "") + ":" + f.split("/sessions/")[0].split("/archived_sessions/")[0]


def prepare(rows, since=None):
    """Normalize rows in place: epoch time, home, main-meter flag, weekly reading/reset."""
    keep = []
    for r in rows:
        ts = r.get("ts")
        if not ts:
            continue
        if since and ts[:10] < since.isoformat():
            continue
        try:
            r["t"] = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        r["home"] = derive_home(r)
        # Only the main meter (limit_id "codex" or absent). Others, e.g. "codex_bengalfox",
        # are separate model-specific meters.
        r["main"] = r.get("limit_id") in ("codex", None)
        r["w_pct"] = r["w_reset"] = None
        if r["main"]:
            if r.get("p_win") == WEEK_MINUTES:
                r["w_pct"], r["w_reset"] = r.get("p_pct"), r.get("p_reset")
            elif r.get("s_win") == WEEK_MINUTES:
                r["w_pct"], r["w_reset"] = r.get("s_pct"), r.get("s_reset")
        keep.append(r)
    return keep


# --------------------------------------------------------------------------- attribution


def attribute(rows):
    """Set r['acct'] on every row. Priority per file: explicit account id > a pinned home >
    weekly-reset-timestamp fingerprint. Returns a stats dict.

    A home is pinned to an account when every file in it that carries an id names that account
    AND the reset-timestamp fingerprint does not contradict it for the home's files without an
    id (a home shared by several logins, or whose login was switched, fails this check and its
    id-less files fall through to the fingerprint)."""
    by_file = collections.defaultdict(list)
    for r in rows:
        by_file[(r["home"], r["file"])].append(r)

    def fingerprint_table(known):
        votes = collections.defaultdict(collections.Counter)
        for r in rows:
            acct = known(r)
            if acct and r["w_reset"] and (r["w_pct"] or 0) > 0:
                votes[r["w_reset"]][acct] += 1
        return ({k: c.most_common(1)[0][0] for k, c in votes.items()},
                sum(1 for c in votes.values() if len(c) > 1))

    def lookup(table, r):
        if not r["w_reset"] or (r["w_pct"] or 0) == 0:
            return None
        for d in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5):  # resets_at jitters by seconds
            if r["w_reset"] + d in table:
                return table[r["w_reset"] + d]
        return None

    def file_ids(rs):
        return {r["account_id"] for r in rs if r.get("account_id")}

    # 1. Candidate pins from id-bearing files, checked against the id-only fingerprint table.
    id_table, _ = fingerprint_table(lambda r: r.get("account_id"))
    home_ids = collections.defaultdict(set)
    inconsistent = set()
    for (home, _f), rs in by_file.items():
        ids = file_ids(rs)
        if len(ids) > 1:
            inconsistent.add(home)
        home_ids[home] |= ids
    home_map = {}
    for home, ids in home_ids.items():
        if len(ids) != 1 or home in inconsistent:
            continue
        acct = next(iter(ids))
        agree = disagree = 0
        for (h, _f), rs in by_file.items():
            if h != home or file_ids(rs):
                continue
            for a in filter(None, (lookup(id_table, r) for r in rs)):
                agree, disagree = agree + (a == acct), disagree + (a != acct)
        if disagree <= 0.02 * (agree + disagree):
            home_map[home] = acct

    # 2. Fingerprints from everything attributed so far (explicit ids + pinned homes).
    fingerprints, conflicts = fingerprint_table(lambda r: r.get("account_id") or home_map.get(r["home"]))

    how_count = collections.Counter()
    for rs in by_file.values():
        ids = file_ids(rs)
        acct = how = None
        if len(ids) == 1:
            acct, how = next(iter(ids)), "explicit id"
        elif rs[0]["home"] in home_map:
            acct, how = home_map[rs[0]["home"]], "pinned home"
        else:
            v = collections.Counter(a for a in (lookup(fingerprints, r) for r in rs) if a)
            if v:
                acct = v.most_common(1)[0][0]
                how = "fingerprint" if len(v) == 1 else "fingerprint (mixed)"
        how_count[how or "unattributed"] += 1
        for r in rs:
            r["acct"] = acct
    return {"files_by_method": dict(how_count), "fingerprint_conflicts": conflicts,
            "pinned_homes": len(home_map),
            "unattributed_events": sum(1 for r in rows if not r["acct"])}


# --------------------------------------------------------------------------- pricing


def load_prices(path=None, long_context=False, overrides=()):
    """Load a price table. The long-context surcharge block is ignored unless `long_context`
    is set (the evidence in the README is that the meter does not apply it). `overrides` are
    'MODEL:field=VALUE' strings (field: in, cached or out) applied on top."""
    with open(path or DEFAULT_PRICES) as fh:
        prices = json.load(fh)
    if not long_context:
        prices["_long_context"] = None
    for spec in overrides:
        try:
            target, value = spec.rsplit("=", 1)
            model, field = target.rsplit(":", 1)
            if field not in ("in", "cached", "out") or model not in prices:
                raise ValueError
            prices[model] = {**prices[model], field: float(value)}
        except ValueError:
            raise SystemExit(f"bad --price-override {spec!r}: use MODEL:in|cached|out=PRICE for a model in the price table")
    return prices


def event_cost(r, prices):
    """API-list dollars for one token_count event, or None if the model has no price.

    input_tokens INCLUDES cached_input_tokens (uncached = input - cached);
    output_tokens INCLUDES reasoning_output_tokens (billed as output)."""
    model = r.get("model")
    pr = prices.get(model) if model and not model.startswith("_") else None
    if not isinstance(pr, dict):
        return None
    inp, cached, out = r.get("in") or 0, r.get("cached") or 0, r.get("out") or 0
    in_rate, cached_rate, out_rate = pr["in"], pr["cached"], pr["out"]
    lc = prices.get("_long_context")
    if lc and lc.get("threshold_input_tokens") and inp > lc["threshold_input_tokens"]:
        in_rate *= lc.get("input_mult", 1.0)
        cached_rate *= lc.get("input_mult", 1.0)
        out_rate *= lc.get("output_mult", 1.0)
    tier = r.get("tier")
    mult = 1.0
    if tier in ("fast", "priority"):
        mult = prices.get("_fast_mult", 2.5)
    elif tier == "ultrafast":
        mult = prices.get("_ultrafast_mult", 8.0)
    return mult * ((inp - cached) * in_rate + cached * cached_rate + out * out_rate) / 1e6


def token_events(rows, prices):
    """Priced, deduplicated main-meter token events.

    Codex re-logs the same token_count (same cumulative total, later timestamp) several times,
    so an event is keyed by (session, cumulative total). A second key, (timestamp, totals),
    drops copies of a session that appear under another session id or file (forks, backups)."""
    seen_total, seen_stamp, events, unknown = set(), set(), [], collections.Counter()
    for r in rows:
        if not r.get("has_info") or r.get("cum_total") is None or not r["main"]:
            continue
        k1 = (r.get("session_id") or r["file"], r["cum_total"])
        k2 = (r["ts"], r["cum_total"], r.get("in"), r.get("cached"), r.get("out"))
        if k1 in seen_total or k2 in seen_stamp:
            continue
        seen_total.add(k1)
        seen_stamp.add(k2)
        if not (r.get("in") or 0) and not (r.get("out") or 0):
            continue
        r["cost"] = event_cost(r, prices)
        if r["cost"] is None:
            unknown[r.get("model") or "(no model logged)"] += 1
        events.append(r)
    return events, unknown


# --------------------------------------------------------------------------- ticks


def split_segments(readings):
    """Split time-sorted weekly readings into weekly segments at meter resets: the percent
    falls more than 2 points below the segment's running max (2 points tolerates slightly
    stale readings from concurrent sessions), or the weekly resets_at moves ahead by 1h+."""
    segs, cur, mx, last_reset = [], [], None, None
    for r in readings:
        reset = r["w_reset"]
        rolled = bool(cur) and reset and last_reset and reset - last_reset >= 3600
        if cur and (r["w_pct"] < mx - 2 or rolled):
            segs.append(cur)
            cur, mx = [], None
        cur.append(r)
        mx = r["w_pct"] if mx is None else max(mx, r["w_pct"])
        if reset:
            last_reset = reset
    if cur:
        segs.append(cur)
    return segs


def segment_ticks(seg):
    """Ticks: first observation of each new higher percent level after the segment's first
    reading. Returns [(t, ts, level, step, plan)]."""
    ticks, mx = [], seg[0]["w_pct"]
    for r in seg[1:]:
        if r["w_pct"] > mx:
            ticks.append((r["t"], r["ts"], r["w_pct"], r["w_pct"] - mx, r.get("plan")))
            mx = r["w_pct"]
    return ticks


def parse_date(s):
    return dt.date.fromisoformat(s)


def period_label(plan, ts, splits):
    """Plan_type plus the date range between --split-date boundaries."""
    plan = plan or "unknown"
    if not splits:
        return plan, 0
    i = bisect.bisect_right(splits, ts[:10])
    lo = splits[i - 1] if i > 0 else None
    hi = (parse_date(splits[i]) - dt.timedelta(days=1)).isoformat() if i < len(splits) else None
    if lo and hi:
        span = f"{lo} to {hi}"
    elif lo:
        span = f"from {lo}"
    else:
        span = f"through {hi}"
    return f"{plan}, {span}", i


def quartiles(v):
    v = sorted(v)

    def q(f):
        pos = f * (len(v) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(v) - 1)
        return v[lo] + (v[hi] - v[lo]) * (pos - lo)
    return q(0.25), q(0.75)


def summarize(values):
    q1, q3 = quartiles(values)
    med = statistics.median(values)
    return {"busy_ticks": len(values), "median_per_pct": med, "q1": q1, "q3": q3,
            "implied_week": med * 100}


def account_list(rows):
    return sorted({r["acct"] for r in rows if r["acct"]},
                  key=lambda a: min(r["t"] for r in rows if r["acct"] == a))


def iter_segments(rows, events, accounts):
    """Yield (account, weekly segment, its ticks, the account's time-sorted events, their times)."""
    for acct in accounts:
        readings = sorted((r for r in rows if r["acct"] == acct and r["w_pct"] is not None),
                          key=lambda r: r["t"])
        evs = sorted((e for e in events if e["acct"] == acct), key=lambda e: e["t"])
        times = [e["t"] for e in evs]
        for seg in split_segments(readings):
            yield acct, seg, segment_ticks(seg), evs, times


def interval(prev, cur, evs, times, opts):
    """The interval between two consecutive ticks: its events, and whether it is 'busy'
    (short, small step, steady logged activity, level below 100)."""
    minutes = (cur[0] - prev[0]) / 60
    i0, i1 = bisect.bisect_right(times, prev[0]), bisect.bisect_right(times, cur[0])
    busy = (0 < minutes <= opts["max_minutes"] and cur[3] <= opts["max_step"] and cur[2] < 100
            and (i1 - i0) >= max(1, minutes / opts["event_every_min"]))
    return {"ts": cur[1], "level": cur[2], "step": cur[3], "plan": cur[4], "minutes": minutes,
            "events": evs[i0:i1], "busy": busy}


def analyze(rows, prices, opts):
    """Run attribution, pricing and tick analysis. Returns a result dict."""
    attribution = attribute(rows)
    events, unknown = token_events(rows, prices)
    splits = sorted(opts.get("split_dates") or [])
    pure_share, min_model_ticks = opts["model_share"], opts["min_model_ticks"]
    min_seg_points = opts.get("min_segment_points", 5)

    accounts = account_list(rows)
    tick_vals = collections.defaultdict(list)     # (acct, period) -> [$/1%]
    model_vals = collections.defaultdict(list)    # (acct, period, model) -> [$/1%]
    considered = collections.Counter()            # (acct, period) -> intervals seen
    skipped_unpriced = collections.Counter()
    segments_out = []
    order = {}

    for acct, seg, ticks, evs, times in iter_segments(rows, events, accounts):
        # Busy intervals between consecutive ticks (the first tick is only an anchor).
        for prev, cur in zip(ticks, ticks[1:]):
            iv = interval(prev, cur, evs, times, opts)
            step, span = iv["step"], iv["events"]
            label, idx = period_label(iv["plan"], iv["ts"], splits)
            order[label] = (idx, iv["plan"] or "")
            considered[(acct, label)] += 1
            if not iv["busy"]:
                continue
            if any(e["cost"] is None for e in span):
                skipped_unpriced[(acct, label)] += 1
                continue
            total = sum(e["cost"] for e in span)
            tick_vals[(acct, label)].append(total / step)
            by_model = collections.Counter()
            for e in span:
                by_model[e["model"]] += e["cost"]
            if total > 0:
                top, top_cost = by_model.most_common(1)[0]
                if top_cost / total >= pure_share:
                    model_vals[(acct, label, top)].append(total / step)
        # Whole segment: first tick to last tick (includes unlogged usage).
        ticks100 = [t for t in ticks if t[2] < 100]
        if len(ticks100) >= 2 and ticks100[-1][2] - ticks100[0][2] >= min_seg_points:
            t0, t1 = ticks100[0][0], ticks100[-1][0]
            i0, i1 = bisect.bisect_right(times, t0), bisect.bisect_right(times, t1)
            span = evs[i0:i1]
            total = sum(e["cost"] for e in span if e["cost"] is not None)
            dp = ticks100[-1][2] - ticks100[0][2]
            plans = collections.Counter(r.get("plan") for r in seg if t0 <= r["t"] <= t1)
            segments_out.append({
                "account": acct, "plan": plans.most_common(1)[0][0] if plans else None,
                "first_tick": ticks100[0][1], "last_tick": ticks100[-1][1],
                "from_percent": ticks100[0][2], "to_percent": ticks100[-1][2],
                "percent_moved": dp, "token_events": len(span),
                "unpriced_events": sum(1 for e in span if e["cost"] is None),
                "cost": total, "per_pct": total / dp, "implied_week": total / dp * 100})

    ticks_out = []
    for (acct, label), vals in tick_vals.items():
        ticks_out.append({"account": acct, "period": label, **summarize(vals),
                          "intervals_seen": considered[(acct, label)],
                          "skipped_unpriced": skipped_unpriced[(acct, label)]})
    models_out = []
    for (acct, label, model), vals in model_vals.items():
        if len(vals) >= min_model_ticks:
            models_out.append({"account": acct, "period": label, "model": model, **summarize(vals)})
    acct_order = {a: i for i, a in enumerate(accounts)}
    ticks_out.sort(key=lambda x: (acct_order[x["account"]], order[x["period"]], x["period"]))
    models_out.sort(key=lambda x: (acct_order[x["account"]], order[x["period"]], x["period"], x["model"]))
    segments_out.sort(key=lambda x: (acct_order[x["account"]], x["first_tick"]))

    return {"accounts": accounts, "ticks": ticks_out, "models": models_out,
            "segments": segments_out, "attribution": attribution,
            "unknown_models": dict(unknown),
            "tick_skips_unpriced": sum(skipped_unpriced.values())}


# --------------------------------------------------------------------------- rendering


def label_for(i):
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return f"account {s}"


def table(headers, rows, align=None):
    align = align or ["<"] * len(headers)
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h)
              for i, h in enumerate(headers)]
    fmt = lambda r: "  ".join(f"{c:{a}{w}}" for c, a, w in zip(r, align, widths)).rstrip()
    return "\n".join([fmt(headers), fmt(["-" * w for w in widths])] + [fmt(r) for r in cells])


def render_text(res, name, params):
    out = []
    o = out.append
    o(f"Busy-tick $ per 1% of the weekly Codex meter "
      f"(busy: interval <= {params['max_minutes']:g} min, step <= {params['max_step']} pt, "
      f">= 1 logged event per {params['event_every_min']:g} min)\n")
    if res["ticks"]:
        o(table(["account", "period", "busy ticks", "median $/1%", "IQR $/1%", "full week $ (x100)"],
                [[name(x["account"]), x["period"], x["busy_ticks"], f"{x['median_per_pct']:.2f}",
                  f"{x['q1']:.2f}-{x['q3']:.2f}", f"{x['implied_week']:,.0f}"] for x in res["ticks"]],
                ["<", "<", ">", ">", ">", ">"]))
    else:
        o("(no busy ticks found)")
    o("\nPer model (busy ticks where one model is >= "
      f"{params['model_share'] * 100:.0f}% of priced cost; needs >= {params['min_model_ticks']} ticks)\n")
    if res["models"]:
        o(table(["account", "period", "model", "busy ticks", "median $/1%", "IQR $/1%"],
                [[name(x["account"]), x["period"], x["model"], x["busy_ticks"],
                  f"{x['median_per_pct']:.2f}", f"{x['q1']:.2f}-{x['q3']:.2f}"] for x in res["models"]],
                ["<", "<", "<", ">", ">", ">"]))
    else:
        o("(none)")
    o("\nWhole weekly segments, first tick to last tick.")
    o("WARNING: includes usage that never reaches local logs (ChatGPT app, cloud tasks, other tools "
      "on the same login), so it usually understates $/1%. Prefer the busy-tick tables above.\n")
    if res["segments"]:
        o(table(["account", "plan", "first tick (UTC)", "last tick (UTC)", "pct", "events",
                 "logged $", "$/1%", "full week $ (x100)"],
                [[name(x["account"]), x["plan"], x["first_tick"][:16], x["last_tick"][:16],
                  f"{x['from_percent']:g}-{x['to_percent']:g}", x["token_events"], f"{x['cost']:,.0f}",
                  f"{x['per_pct']:.2f}", f"{x['implied_week']:,.0f}"] for x in res["segments"]],
                ["<", "<", "<", "<", ">", ">", ">", ">", ">"]))
    else:
        o("(none)")
    return "\n".join(out)


def warnings_for(res, name):
    w = []
    if res["unknown_models"]:
        listed = ", ".join(f"{m} ({n} events)" for m, n in sorted(res["unknown_models"].items()))
        w.append(f"models with no price, excluded from cost: {listed}. Intervals containing them "
                 f"({res['tick_skips_unpriced']}) were skipped. Add them with --prices.")
    att = res["attribution"]
    if att["unattributed_events"]:
        w.append(f"{att['unattributed_events']} events could not be attributed to an account and were ignored.")
    if att["fingerprint_conflicts"]:
        w.append(f"{att['fingerprint_conflicts']} weekly reset timestamps were seen under more than "
                 "one account; attribution may be unreliable.")
    return w


# --------------------------------------------------------------------------- compare


def parse_selector(spec):
    """'plan=pro,before=2026-09-29' -> predicate on (plan_type, tick timestamp). `after` is
    inclusive, `before` exclusive, both UTC dates."""
    sel = {}
    for part in spec.split(","):
        key, _, value = part.partition("=")
        if key not in ("plan", "after", "before") or not value:
            raise SystemExit(f"bad selector {spec!r}: use plan=NAME and/or after=YYYY-MM-DD, before=YYYY-MM-DD")
        if key != "plan":
            parse_date(value)
        sel[key] = value

    def match(plan, ts):
        return ((sel.get("plan") is None or plan == sel["plan"])
                and (sel.get("after") is None or ts[:10] >= sel["after"])
                and (sel.get("before") is None or ts[:10] < sel["before"]))
    return match


def parse_map(specs):
    mapping = {}
    for spec in specs or ():
        new, sep, old = spec.partition("=")
        if not sep or not new or not old:
            raise SystemExit(f"bad --map {spec!r}: use NEW=OLD, e.g. gpt-6.1-sol=gpt-6-sol")
        mapping[new] = old
    return mapping


def solve_least_squares(points, groups):
    """Fit dp = sum_g k_g * cost_g (no intercept) by the normal equations. points: [(dp, {group: cost})].
    Returns [k_g] or None if the system is singular."""
    n = len(groups)
    a = [[0.0] * (n + 1) for _ in range(n)]
    for dp, cost in points:
        x = [cost.get(g, 0.0) for g in groups]
        for i in range(n):
            a[i][n] += x[i] * dp
            for j in range(n):
                a[i][j] += x[i] * x[j]
    scale = max((a[i][i] for i in range(n)), default=0.0)
    for i in range(n):
        pivot = max(range(i, n), key=lambda r: abs(a[r][i]))
        a[i], a[pivot] = a[pivot], a[i]
        if abs(a[i][i]) <= 1e-12 * scale:
            return None
        for r in range(n):
            if r != i:
                f = a[r][i] / a[i][i]
                a[r] = [u - f * v for u, v in zip(a[r], a[i])]
    return [a[i][n] / a[i][i] for i in range(n)]


def percentile(sorted_values, f):
    return sorted_values[int(f * len(sorted_values))]


def compare(rows, prices, opts, baseline, target, mapping=None, min_share=0.02, bootstrap=500, seed=2):
    """How far would the TARGET period's logged work have moved the BASELINE meter?

    Fit per-model meter weights on baseline busy ticks (dp = sum_g k_g * card_cost_g), predict the
    target ticks' movement with them, and return predicted/actual: target allowance divided by
    baseline allowance (below 1 = a smaller allowance), with a bootstrap 90% interval."""
    mapping = mapping or {}
    base_match, targ_match = parse_selector(baseline), parse_selector(target)
    attribute(rows)
    events, unknown = token_events(rows, prices)
    base, targ = [], []
    skipped = 0
    for acct, _seg, ticks, evs, times in iter_segments(rows, events, account_list(rows)):
        for prev, cur in zip(ticks, ticks[1:]):
            iv = interval(prev, cur, evs, times, opts)
            if not iv["busy"]:
                continue
            if any(e["cost"] is None for e in iv["events"]):
                skipped += 1
                continue
            cost = collections.Counter()
            for e in iv["events"]:
                cost[mapping.get(e["model"], e["model"])] += e["cost"]
            for match, out in ((base_match, base), (targ_match, targ)):
                if match(iv["plan"], iv["ts"]):
                    out.append((iv["step"], cost))
    if len(base) < 3 or not targ:
        raise SystemExit(f"not enough busy ticks: baseline {len(base)}, target {len(targ)}")

    base_cost = collections.Counter()
    for _dp, cost in base:
        base_cost.update(cost)
    total = sum(base_cost.values())
    kept = [g for g, c in base_cost.most_common() if c / total >= min_share]
    folded = {g for g in base_cost if g not in kept}
    groups = kept + (["other"] if folded else [])
    missing = sorted({g for _dp, cost in targ for g in cost if g not in base_cost and g not in kept})
    if missing:
        raise SystemExit("target uses models absent from the baseline: " + ", ".join(missing)
                         + ". Map each to a baseline model, e.g. --map " + missing[0] + "=<baseline model>")

    def regroup(points):
        out = []
        for dp, cost in points:
            c = collections.Counter()
            for g, v in cost.items():
                c[g if g in kept else "other"] += v
            out.append((dp, c))
        return out
    base_g, targ_g = regroup(base), regroup(targ)
    k = solve_least_squares(base_g, groups)
    if k is None:
        raise SystemExit("baseline fit is singular (too few ticks, or two model groups always used together)")

    def ratio(weights, points):
        predicted = sum(w * c.get(g, 0.0) for _dp, c in points for w, g in zip(weights, groups))
        return predicted / sum(dp for dp, _c in points)

    rng = random.Random(seed)
    ratios, ks = [], []
    for _ in range(bootstrap):
        kb = solve_least_squares([rng.choice(base_g) for _ in base_g], groups)
        if kb is None:
            continue
        ks.append(kb)
        ratios.append(ratio(kb, [rng.choice(targ_g) for _ in targ_g]))
    ratios.sort()
    if len(ratios) < 10:
        raise SystemExit("bootstrap failed: baseline fit was singular in nearly every resample")
    group_rows = []
    for i, g in enumerate(groups):
        per = sorted(1 / kb[i] if kb[i] > 0 else float("inf") for kb in ks)
        group_rows.append({
            "group": g, "baseline_cost_share": sum(c.get(g, 0.0) for _d, c in base_g) / total,
            "pct_per_card_dollar": k[i], "card_dollars_per_pct": 1 / k[i] if k[i] > 0 else None,
            "card_dollars_per_pct_ci90": [percentile(per, 0.05), percentile(per, 0.95)]})
    targ_cost = collections.Counter()
    for _dp, c in targ_g:
        targ_cost.update(c)
    return {"baseline": baseline, "target": target, "map": mapping,
            "baseline_ticks": len(base), "target_ticks": len(targ),
            "target_cost_share": {g: v / sum(targ_cost.values()) for g, v in targ_cost.most_common()},
            "groups": group_rows, "ratio": ratio(k, targ_g),
            "ratio_ci90": [percentile(ratios, 0.05), percentile(ratios, 0.95)],
            "bootstrap": {"resamples": len(ratios), "seed": seed},
            "intervals_skipped_unpriced": skipped, "unknown_models": dict(unknown)}


def render_compare(res):
    lo, hi = res["ratio_ci90"]
    out = [f"Baseline: {res['baseline']}  ({res['baseline_ticks']} busy ticks)",
           f"Target:   {res['target']}  ({res['target_ticks']} busy ticks)"]
    if res["map"]:
        out.append("Model map: " + ", ".join(f"{a} -> {b}" for a, b in res["map"].items()))
    out += ["", "Baseline meter weights (card dollars of logged work per 1% of the weekly meter, "
                "if a tick were all that group):",
            table(["group", "cost share", "card $/1%", "90% CI"],
                  [[g["group"], f"{g['baseline_cost_share']:.0%}",
                    f"{g['card_dollars_per_pct']:.2f}" if g["card_dollars_per_pct"] else "n/a",
                    f"{g['card_dollars_per_pct_ci90'][0]:.2f} .. {g['card_dollars_per_pct_ci90'][1]:.2f}"]
                   for g in res["groups"]], ["<", ">", ">", ">"]),
            "", "Target work by group: " + ", ".join(f"{g} {v:.0%}" for g, v in res["target_cost_share"].items()),
            "", f"Target allowance / baseline allowance = {res['ratio']:.2f}  [90% CI {lo:.2f} .. {hi:.2f}]",
            "(1.00 = the same work moves the meter equally; 0.78 = the target period's allowance is "
            "about 22% smaller. Assumes the baseline weights hold in the target period.)"]
    return "\n".join(out)


# --------------------------------------------------------------------------- CLI


def default_homes():
    return [os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")]


def add_source_args(p):
    p.add_argument("--home", action="append", metavar="DIR",
                   help="Codex home to scan (repeatable). Default: $CODEX_HOME or ~/.codex")
    p.add_argument("--since", type=parse_date, metavar="YYYY-MM-DD", help="ignore logs before this date")
    p.add_argument("--events-jsonl", action="append", metavar="FILE", default=[],
                   help="read events written by `scan --out` (or a prototype extract) instead of "
                        "scanning; repeatable, can be combined with --home")


def add_price_args(p):
    p.add_argument("--prices", metavar="FILE", help="price table (default: bundled prices.json)")
    p.add_argument("--price-override", action="append", default=[], metavar="MODEL:FIELD=PRICE",
                   help="override one rate, e.g. gpt-6.1-sol:cached=0.20 (FIELD: in, cached, out); repeatable")
    p.add_argument("--long-context-surcharge", action="store_true",
                   help="apply the long-context rates (>272K input: 2x input, 1.5x output). Off by default")


def prices_from(args):
    return load_prices(args.prices, args.long_context_surcharge, args.price_override)


def load_rows(args, explicit_homes_only=False):
    rows, n_files = [], 0
    homes = args.home or ([] if args.events_jsonl else default_homes())
    if homes:
        rows, n_files = scan_homes(homes, args.since)
    rows += read_events_jsonl(args.events_jsonl)
    return prepare(rows, args.since), n_files, homes


def account_namer(res, show_ids):
    names = {a: (a if show_ids else label_for(i)) for i, a in enumerate(res["accounts"])}
    return lambda a: names[a]


def cmd_scan(args):
    rows, n_files, homes = load_rows(args)
    prices = prices_from(args)
    res = analyze(rows, prices, dict(max_minutes=90, max_step=3, event_every_min=10,
                                     model_share=0.9, min_model_ticks=3))
    name = account_namer(res, args.show_ids)
    events, _ = token_events(rows, prices)
    summary = {
        "homes": [str(h) for h in homes], "rollout_files": n_files, "rows": len(rows),
        "token_events_main_meter": len(events),
        "rows_other_meters": sum(1 for r in rows if not r["main"]),
        "attribution": res["attribution"],
        "accounts": {name(a): sum(1 for r in rows if r["acct"] == a) for a in res["accounts"]},
        "plans": dict(collections.Counter(r.get("plan") for r in rows if r["w_pct"] is not None)),
        "models": dict(collections.Counter(e.get("model") for e in events)),
        "unknown_models": res["unknown_models"],
        "first_event": min((r["ts"] for r in rows), default=None),
        "last_event": max((r["ts"] for r in rows), default=None),
    }
    if args.out:
        with open(args.out, "w") as fh:
            for r in rows:
                fh.write(json.dumps({k: v for k, v in r.items()
                                     if k not in ("t", "main", "w_pct", "w_reset", "acct", "cost")}) + "\n")
        summary["wrote"] = args.out
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        for k, v in summary.items():
            print(f"{k}: {v}")
        for w in warnings_for(res, name):
            print("WARNING:", w, file=sys.stderr)
    return 0


def cmd_report(args):
    rows, _n, _h = load_rows(args)
    prices = prices_from(args)
    params = dict(max_minutes=args.max_minutes, max_step=args.max_step, event_every_min=args.event_every_min,
                  model_share=args.model_share, min_model_ticks=args.min_model_ticks,
                  min_segment_points=args.min_segment_points,
                  split_dates=[d.isoformat() for d in (args.split_date or [])])
    res = analyze(rows, prices, params)
    name = account_namer(res, args.show_ids)
    warns = warnings_for(res, name)
    if args.json:
        doc = {"params": params, "prices_fetched": prices.get("_fetched"),
               "ticks": [{**x, "account": name(x["account"])} for x in res["ticks"]],
               "models": [{**x, "account": name(x["account"])} for x in res["models"]],
               "segments": [{**x, "account": name(x["account"])} for x in res["segments"]],
               "segments_warning": "Whole-segment figures include usage that never reaches local logs "
                                   "and usually understate $/1%.",
               "warnings": warns, "unknown_models": res["unknown_models"],
               "attribution": res["attribution"]}
        print(json.dumps(doc, indent=2))
    else:
        print(render_text(res, name, params))
        for w in warns:
            print("WARNING:", w, file=sys.stderr)
    return 0


def cmd_compare(args):
    rows, _n, _h = load_rows(args)
    prices = prices_from(args)
    opts = dict(max_minutes=args.max_minutes, max_step=args.max_step, event_every_min=args.event_every_min)
    res = compare(rows, prices, opts, args.baseline, args.target, parse_map(args.map),
                  args.min_share, args.bootstrap, args.seed)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(render_compare(res))
        if res["unknown_models"]:
            print("WARNING: models with no price were excluded and their intervals skipped: "
                  + ", ".join(sorted(res["unknown_models"])), file=sys.stderr)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="codex_limit_meter", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scan", help="find and parse rollout logs; print a summary")
    add_source_args(sc)
    sc.add_argument("--out", metavar="FILE", help="write parsed events (JSONL) for later --events-jsonl")
    add_price_args(sc)
    sc.add_argument("--show-ids", action="store_true", help="print full account IDs")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_scan)

    rp = sub.add_parser("report", help="$ of logged work per 1%% of the weekly meter")
    add_source_args(rp)
    rp.add_argument("--split-date", action="append", type=parse_date, metavar="YYYY-MM-DD",
                    help="extra period boundary (repeatable); ticks are grouped by plan_type and date")
    add_price_args(rp)
    rp.add_argument("--show-ids", action="store_true", help="print full account IDs instead of labels")
    rp.add_argument("--json", action="store_true", help="machine-readable output")
    rp.add_argument("--max-minutes", type=float, default=90, help="busy filter: max interval between ticks")
    rp.add_argument("--max-step", type=int, default=3, help="busy filter: max percent points per tick")
    rp.add_argument("--event-every-min", type=float, default=10,
                    help="busy filter: require >= 1 logged token event per this many minutes")
    rp.add_argument("--model-share", type=float, default=0.9,
                    help="per-model table: cost share one model must have in an interval")
    rp.add_argument("--min-model-ticks", type=int, default=3, help="per-model table: minimum ticks to list")
    rp.add_argument("--min-segment-points", type=float, default=5,
                    help="whole-segment table: hide segments where the meter moved fewer points")
    rp.set_defaults(func=cmd_report)
    cp = sub.add_parser("compare", help="how far would the target period's work have moved the baseline meter?")
    add_source_args(cp)
    add_price_args(cp)
    cp.add_argument("--baseline", required=True, metavar="SPEC",
                    help="baseline ticks, e.g. plan=pro,before=2026-09-29 (plan=, after= inclusive, before= exclusive)")
    cp.add_argument("--target", required=True, metavar="SPEC", help="target ticks, e.g. plan=promax")
    cp.add_argument("--map", action="append", default=[], metavar="NEW=OLD",
                    help="treat model NEW as baseline model OLD, e.g. gpt-6.1-sol=gpt-6-sol; repeatable. "
                         "Required for target models absent from the baseline")
    cp.add_argument("--min-share", type=float, default=0.02,
                    help="baseline models below this share of cost are folded into 'other'")
    cp.add_argument("--bootstrap", type=int, default=500, help="bootstrap resamples for the 90%% interval")
    cp.add_argument("--seed", type=int, default=2)
    cp.add_argument("--max-minutes", type=float, default=90)
    cp.add_argument("--max-step", type=int, default=3)
    cp.add_argument("--event-every-min", type=float, default=10)
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=cmd_compare)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
