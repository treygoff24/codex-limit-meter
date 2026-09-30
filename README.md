# codex-limit-meter

How much Codex work does 1% of your weekly limit buy? This script answers that from the logs Codex already keeps on your computer.

Codex records every turn you run: the model, the tokens in and out, and the reading of your weekly usage meter at that moment. The script prices each turn at OpenAI's published Codex rate card, converted to dollars, and measures how many dollars of work it takes to move your meter by one percentage point. Multiply by 100 and you have the size of your weekly allowance, in API dollars.

One Python file. No installs, no accounts, no network. Nothing leaves your machine.

## Why I built it

On 2026-09-29 OpenAI launched a $500 Pro plan with 25× Plus usage in Codex and lowered the $200 Pro plan to 10× Plus for new subscribers. The $200 plan used to be 20× Plus, so on paper the $500 plan gives 25% more than the old $200 plan did.

I had a month of logs from two accounts on the old $200 plan and one night on the $500 plan, so I measured it. The old $200 plan bought a steady $13 of Codex work per 1% of the weekly meter, about $1,300 a week. The $500 plan came out at 0.77× to 1.05× of that, not 1.25×; the range depends on one price I can't pin down, explained under "Compare two plans or periods." That is one night of data (17 meter steps), so treat it as a first reading, and run it on your own logs.

## Run it

You need Python 3.10 or newer.

Clone or download this repo, then from its folder:

```
python3 codex_limit_meter.py report
```

By default it reads `~/.codex` (or `$CODEX_HOME` if you set it). Before you trust the number, check the next section: if some of your Codex logs live elsewhere, the default run sees only part of your work and understates your allowance.

`scan` shows what the script found (files, accounts, plans, models, date range) without the analysis. Add `--since 2026-09-01` to either command to skip older logs.

Read any `WARNING` lines it prints. The most common one names a model that isn't in `prices.json`; the script skips every stretch of work that includes it, so your table comes out thin or empty. Add the model to `prices.json` in the same form as the others, in dollars per million tokens: `"model-name": {"in": 2.0, "cached": 0.2, "out": 10.0}`.

## Point it at all your logs

**The meter counts all your Codex usage, and the script only counts the logs you give it.** If half your work is logged somewhere the script never looks, the meter moves twice as far as the logged work explains, and your allowance looks half its real size. On my Mac, the default folder alone gave $7.94 per 1%; with every folder included it was $13.

Most people have one folder, `~/.codex`, and can skip this. You probably have more than one if you set `CODEX_HOME`, use a profile or account switcher, run Codex inside other tools, or use Codex on more than one computer.

To list every folder on a computer that holds Codex logs, with a count of log files in each:

```
find ~ \( -path '*/sessions/*' -o -path '*/archived_sessions/*' \) -name 'rollout-*.jsonl' 2>/dev/null \
  | sed -E 's#/(archived_)?sessions/.*##' | sort | uniq -c | sort -rn
```

It searches your whole home folder, so give it a moment. Then pass each folder with `--home`. Once you give any `--home`, the script stops reading `~/.codex` on its own, so list it too:

```
python3 codex_limit_meter.py report --home ~/.codex --home ~/profiles/work
```

Backups and copies are safe to include. A session that appears in two folders is counted once. As a check, the `rollout_files` count from `scan` with the same `--home` flags should roughly match the total from `find`.

For a second computer, run `scan` there and save the result, copy the file over, and combine. Keep `--home ~/.codex` in the last command; `--events-jsonl` on its own also skips the default folder.

```
# on the other computer
python3 codex_limit_meter.py scan --home ~/.codex --out laptop-events.jsonl

# back on this one
python3 codex_limit_meter.py report --home ~/.codex --events-jsonl laptop-events.jsonl
```

Usage that never reaches a local log still moves the meter: the ChatGPT app, Codex cloud tasks, code review, and anything else signed in to the same account. The script can't see that work. It limits the damage by measuring only the stretches when you were actively running Codex (more on that under "How it works").

## Read the result

Here is my report, across all my folders on two computers:

```
account    period  busy ticks  median $/1%     IQR $/1%  full week $ (x100)
---------  ------  ----------  -----------  -----------  ------------------
account A  pro            486        13.13  10.37-15.34               1,313
account A  promax          17         9.35   8.65-10.16                 935
account B  pro            476        13.31  12.21-14.65               1,331
```

- **account.** Accounts are labeled A, B, and so on. Add `--show-ids` to see the real account IDs.
- **period.** The plan as Codex logs it: `pro` is a Pro plan at either price, `promax` the $500 plan. `--split-date YYYY-MM-DD` splits a row at a date (UTC).
- **busy ticks.** How many measurements the row rests on, each one busy stretch between two meter steps. Under about 20 is thin.
- **median $/1%.** The answer: dollars of Codex work, at rate-card prices, per 1% of your weekly meter. Higher means a bigger allowance.
- **IQR.** The middle half of the individual measurements. A wide range means a noisy row.
- **full week $.** The median times 100, the implied weekly allowance.

Dividing 9.35 by 13.13 gives 0.71, which is not the 0.77 I quoted: that raw division ignores that I ran different models on the two plans.

Below that table come two more. The per-model table repeats the measurement using only stretches where one model did at least 90% of the work. The whole-week table is a rough cross-check over entire weeks. It can't filter out work the logs never saw, so it usually reads low.

Different models don't use the meter at the same rate per dollar. On the old $200 plan, a dollar of GPT-5.6 Sol moved my meter noticeably less than a dollar of GPT-6 Sol. So a plain before/after comparison mixes up "the plan changed" with "I switched models." The next command corrects for that.

## Compare two plans or periods

```
python3 codex_limit_meter.py compare \
  --baseline plan=pro,before=2026-09-29 \
  --target plan=promax \
  --map gpt-6.1-sol=gpt-6-sol
```

It learns, from the baseline period, how much each model moves the meter per dollar of work. Then it asks how far your target-period work would have moved the baseline meter, and compares that with how far the meter actually moved. The answer is the line that begins `Target allowance`:

```
Target allowance / baseline allowance = 0.76  [90% CI 0.68 .. 0.82]
```

1.00 means the same allowance. 0.76 means the target period allows about 24% less work. The bracket is a 90% confidence interval from resampling your data.

`--baseline` and `--target` pick measurements by `plan=`, `after=YYYY-MM-DD` (inclusive), and `before=YYYY-MM-DD` (exclusive), joined with commas; dates are UTC. Plan names are the ones in the report's period column. If either side has too few busy measurements, the script says so and stops, so you need some use on both plans.

If the target period used a model the baseline never saw, the script stops and asks you to map it to one it has, as in `--map gpt-6.1-sol=gpt-6-sol`. The mapping is your assumption; here, that GPT-6.1 Sol uses the meter the way GPT-6 Sol did.

`--price-override MODEL:FIELD=PRICE` changes one price for a what-if run. GPT-6.1 Sol's cached input is half the price of GPT-6 Sol's, and my data can't tell which price the meter uses, so I ran both:

```
python3 codex_limit_meter.py compare --baseline plan=pro,before=2026-09-29 --target plan=promax \
  --map gpt-6.1-sol=gpt-6-sol --price-override gpt-6.1-sol:cached=0.20
```

Run on my logs today, the first command prints 0.76 (GPT-6.1 Sol cached input at its list price, $0.10 per million) and the second prints 1.02 (at GPT-6 Sol's $0.20). The 0.77 and 1.05 I quoted came from an earlier run with no date cutoff on the baseline and a slightly different set of log folders. Choices like that move the result by a few hundredths.

## Share your numbers

The most useful numbers to share are from new $200 Pro subscribers (existing ones keep the old allowance until 2026-10-29) and anyone on the $500 plan with more than one night of use. Post your `report` table and your `compare` line, and say which models you mostly used. The tables show plan names and dollar figures, not your account ID, unless you add `--show-ids`.

## How it works

1. **Read the logs.** Codex writes a `token_count` event after each model request. It carries that request's tokens and the current state of your usage meters. Only the main weekly Codex meter is used; some models have separate meters of their own, which are ignored.
2. **Price each request.** Uncached input, cached input, and output tokens are each multiplied by the model's rate from OpenAI's Codex rate card, which is quoted in credits and converted at 25 credits per dollar. Output includes reasoning tokens. Fast mode is priced at 2.5× and Ultrafast at 8×. The prices, with sources, are in `prices.json`. A model missing from that file is reported, and any stretch that contains it is skipped rather than guessed.
3. **Find the meter steps.** The meter reports whole percents. Each time it climbs to a new level, that's a tick. The meter resets weekly, and each week is handled separately.
4. **Keep only busy stretches.** For each pair of consecutive ticks, the dollars of work logged between them, divided by the points the meter moved, is one measurement. It counts only if the stretch lasted at most 90 minutes, the meter moved at most 3 points, at least one request was logged per 10 minutes, and the meter was below 100%. That keeps out stretches where you were away, or using Codex somewhere the logs can't see. The report gives the median of what's left. The thresholds are the flags `--max-minutes`, `--max-step`, and `--event-every-min` on `report` and `compare`.
5. **Work out the account.** Each log file is assigned to an account from the account ID in the file, or, failing that, from other files in the same folder, or from the time your weekly meter resets, which is unique to each account. Files that fit none of these are left out, and `scan` says how many.

## Limitations

The meter reports whole percents, so any single measurement is coarse. The median over many ticks carries the information, and a row with few ticks deserves little trust.

The meter doesn't charge every model exactly at rate-card price. Compare periods with similar model mixes, or use `compare`.

Work outside your local logs moves the meter without showing up. The busy-stretch filter reduces this but can't remove it, so your true allowance is, if anything, higher than reported.

OpenAI says it changed how usage is metered around 2026-09-28. A comparison across that date measures the new metering as well as any plan change.

Prices are the rate card as of 2026-09-30; if OpenAI changes it, edit `prices.json` or pass `--prices`. OpenAI says credit prices alone don't set how much a plan includes, so treat the dollar figures as a consistent yardstick, not a bill.

The API charges extra for requests over 272K input tokens. In my data those requests moved the meter no faster per dollar than normal ones, so the surcharge is off by default; `--long-context-surcharge` turns it on.

Ultrafast's 8× rate is OpenAI's stated figure. My logs have too little Ultrafast use to check it against the meter.

## Tests

```
python3 -m unittest
```

## License

MIT. See `LICENSE`.
