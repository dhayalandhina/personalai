---
name: data-analysis
description: Analyse CSV/Excel/JSON data with pandas and draw charts with matplotlib (free, local) — summaries, group-bys, trends, comparisons, PNG charts for Telegram. Use when Dhina gives numbers/data or asks "compare", "trend", "total", "chart", "graph", "how much per month".
triggers: [analyse, analyze, chart, graph, plot, trend, compare, total, average, sum, group by, csv data, excel data, statistics, dashboard]
metadata:
  version: "1.0"
  owner: dhina
---

# data-analysis

Install once: `pip install pandas matplotlib openpyxl`.

## Rules
1. Numbers are computed by code, never estimated in prose. Every figure in the reply comes from a script output this turn.
2. Load → inspect (`describe`) → compute → chart → **re-check one headline number by a second route** (e.g. sum of groups == grand total) before replying (`verification-protocol` §4).
3. Reply: line 1 = the headline number/insight, then 1-2 lines, then the chart path (send as photo via `telegram-interface`).
4. Big files (> 50 MB) → read in chunks or `usecols`; don't page rows through the chat.
5. Charts: one message, one chart; title + units on axes; no 3D, no pie for > 4 categories; IST timestamps if time-based.

## Commands
```bash
python3 scripts/analyze.py describe data.csv [--sheet Sheet1]                    # shape, dtypes, nulls, numeric summary, head
python3 scripts/analyze.py group data.csv --by region --agg amount:sum,orders:count [--sort -amount] [--top 10]
python3 scripts/analyze.py trend data.csv --date date --value amount --freq M     # resample by D/W/M/Q
python3 scripts/analyze.py chart data.csv --x month --y sales --kind line|bar --out chart.png [--title "..."]
python3 scripts/analyze.py chart data.csv --by region --agg amount:sum --kind bar --out byregion.png
python3 scripts/analyze.py query data.csv --expr "amount > 1000 and region == 'TN'" [--cols a,b]
python3 scripts/analyze.py sql data.csv --q "SELECT region, SUM(amount) a FROM t GROUP BY 1 ORDER BY a DESC"
```
All commands accept `.csv .tsv .xlsx .json` and print JSON (tables as records) so the reply can quote exact values.
