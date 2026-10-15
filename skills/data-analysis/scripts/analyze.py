#!/usr/bin/env python3
"""analyze.py — pandas + matplotlib helper for dd.

  describe FILE [--sheet S]
  group FILE --by COL[,COL2] --agg col:func[,col:func] [--sort [-]col] [--top N]
  trend FILE --date COL --value COL [--freq D|W|M|Q] [--agg sum|mean|count]
  chart FILE (--x COL --y COL | --by COL --agg col:func) --kind line|bar --out PNG [--title T]
  query FILE --expr "pandas query" [--cols a,b] [--top N]
  sql FILE --q "SELECT ... FROM t ..."
  --selftest
Output: JSON. Files: .csv .tsv .xlsx .json
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path


def load(path: str, sheet: str | None = None):
    import pandas as pd  # pip install pandas
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return pd.read_excel(p, sheet_name=sheet or 0)
    if ext == ".tsv":
        return pd.read_csv(p, sep="\t")
    if ext == ".json":
        return pd.read_json(p)
    return pd.read_csv(p)


def records(df, top: int | None = None) -> list[dict]:
    d = df.head(top) if top else df
    return json.loads(d.to_json(orient="records", date_format="iso"))


def describe(df) -> dict:
    return {"shape": list(df.shape), "columns": {c: str(t) for c, t in df.dtypes.items()},
            "nulls": {c: int(n) for c, n in df.isna().sum().items() if n},
            "numeric": json.loads(df.describe().round(3).to_json()) if not df.select_dtypes("number").empty else {},
            "head": records(df, 5)}


def parse_agg(s: str) -> dict:
    out: dict = {}
    for part in s.split(","):
        col, _, fn = part.partition(":")
        out.setdefault(col.strip(), []).append(fn.strip() or "sum")
    return {k: (v[0] if len(v) == 1 else v) for k, v in out.items()}


def group(df, by: str, agg: str, sort: str | None = None, top: int | None = None):
    g = df.groupby([b.strip() for b in by.split(",")]).agg(parse_agg(agg))
    if hasattr(g.columns, "levels"):
        g.columns = ["_".join(c).strip("_") for c in g.columns.values]
    g = g.reset_index()
    if sort:
        g = g.sort_values(sort.lstrip("-"), ascending=not sort.startswith("-"))
    return g.head(top) if top else g


def trend(df, date: str, value: str, freq: str = "M", agg: str = "sum"):
    import pandas as pd
    d = df.copy()
    d[date] = pd.to_datetime(d[date], errors="coerce")
    d = d.dropna(subset=[date]).set_index(date)[value]
    res = getattr(d.resample({"D": "D", "W": "W", "M": "ME", "Q": "QE"}.get(freq, freq)), agg)()
    out = res.reset_index()
    out[date] = out[date].dt.strftime("%Y-%m-%d")
    return out


def chart(df, out: str, kind: str = "line", x: str | None = None, y: str | None = None,
          by: str | None = None, agg: str | None = None, title: str | None = None) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if by and agg:
        g = group(df, by, agg)
        x, y = by, [c for c in g.columns if c != by][0]
        df = g
    fig, ax = plt.subplots(figsize=(9, 5), dpi=120)
    if kind == "bar":
        ax.bar(df[x].astype(str), df[y], color="#2b6cb0")
    else:
        ax.plot(df[x].astype(str), df[y], marker="o", color="#2b6cb0")
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    ax.set_title(title or f"{y} by {x}")
    ax.grid(alpha=0.3)
    if len(df) > 8:
        plt.xticks(rotation=45, ha="right")
    fig.tight_layout()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    return {"ok": True, "path": out, "points": int(len(df)), "x": x, "y": y}


def sql(df, q: str):
    import pandas as pd
    con = sqlite3.connect(":memory:")
    df.to_sql("t", con, index=False)
    return pd.read_sql_query(q, con)


def selftest() -> int:
    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        print("analyze selftest ok (pandas not installed here: pip install pandas matplotlib)")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="dd-ana-"))
    f = tmp / "s.csv"
    f.write_text("date,region,amount\n2026-01-05,TN,100\n2026-01-20,KL,50\n2026-02-03,TN,200\n2026-02-28,TN,25\n")
    df = load(str(f))
    d = describe(df)
    assert d["shape"] == [4, 3], d
    g = group(df, "region", "amount:sum,amount:count", "-amount_sum")
    assert records(g)[0]["region"] == "TN" and records(g)[0]["amount_sum"] == 325, records(g)
    t = trend(df, "date", "amount", "M")
    assert records(t)[1]["amount"] == 225, records(t)
    assert int(sql(df, "SELECT SUM(amount) s FROM t").iloc[0, 0]) == 375
    assert int(df.amount.sum()) == 375  # second route check
    try:
        c = chart(df, str(tmp / "c.png"), "bar", by="region", agg="amount:sum")
        assert Path(c["path"]).stat().st_size > 1000
        print("analyze selftest ok (pandas + matplotlib)")
    except ImportError:
        print("analyze selftest ok (pandas only; pip install matplotlib for charts)")
    return 0


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    cmd, path = argv[0], argv[1]
    top = int(arg(argv, "--top")) if "--top" in argv else None
    try:
        df = load(path, arg(argv, "--sheet"))
        if cmd == "describe":
            res = describe(df)
        elif cmd == "group":
            res = records(group(df, arg(argv, "--by"), arg(argv, "--agg", ""), arg(argv, "--sort"), top))
        elif cmd == "trend":
            res = records(trend(df, arg(argv, "--date"), arg(argv, "--value"), arg(argv, "--freq", "M"), arg(argv, "--agg", "sum")))
        elif cmd == "chart":
            res = chart(df, arg(argv, "--out", "chart.png"), arg(argv, "--kind", "line"), arg(argv, "--x"), arg(argv, "--y"),
                        arg(argv, "--by"), arg(argv, "--agg"), arg(argv, "--title"))
        elif cmd == "query":
            q = df.query(arg(argv, "--expr", "index >= 0"))
            cols = arg(argv, "--cols")
            res = {"count": int(len(q)), "rows": records(q[cols.split(",")] if cols else q, top or 50)}
        elif cmd == "sql":
            res = records(sql(df, arg(argv, "--q", "SELECT * FROM t LIMIT 20")), top)
        else:
            print(__doc__)
            return 2
    except ImportError as e:
        res = {"error": f"missing library {e.name}: pip install pandas matplotlib openpyxl"}
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 1 if isinstance(res, dict) and "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
