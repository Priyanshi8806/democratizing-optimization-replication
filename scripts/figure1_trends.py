"""Reproduce Figure 1: US Google Trends interest, Aug 2020 - Aug 2025, five terms.

Google Trends normalises *jointly* within one request (max 5 terms), so all five
terms must be fetched together for the 0-100 scale to match the paper.

Option A (pytrends, may hit HTTP 429 - just retry later or use option B):
    python scripts/figure1_trends.py --fetch
Option B (manual, always works):
    1. Open https://trends.google.com/trends/explore?date=2020-08-01%202025-08-31&geo=US&q=artificial%20intelligence,LLMs,linear%20programming,integer%20programming,optimization
    2. "Interest over time" -> download CSV -> save as data/trends_multiTimeline.csv
    3. python scripts/figure1_trends.py --csv data/trends_multiTimeline.csv

Note: Trends data are re-sampled per request, so values differ slightly between pulls.
The paper does not state the exact end date; we use 2020-08-01 .. 2025-08-31 (weekly).
"""
import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

TERMS = ["artificial intelligence", "LLMs", "linear programming", "integer programming", "optimization"]
TIMEFRAME = "2020-08-01 2025-08-31"
STYLE = {  # mimic the paper's line styles
    "artificial intelligence": dict(color="#E69F00", ls="-", lw=2),
    "LLMs": dict(color="#56B4E9", ls="--", lw=2),
    "linear programming": dict(color="#009E73", ls="-.", lw=2),
    "integer programming": dict(color="#F0E442", ls=":", lw=2),
    "optimization": dict(color="#0072B2", ls=(0, (3, 1, 1, 1)), lw=2),
}


def fetch() -> pd.DataFrame:
    from pytrends.request import TrendReq
    py = TrendReq(hl="en-US", tz=0, retries=3, backoff_factor=1.0)
    py.build_payload(TERMS, timeframe=TIMEFRAME, geo="US")
    df = py.interest_over_time().drop(columns=["isPartial"], errors="ignore")
    return df


def read_csv(path) -> pd.DataFrame:
    # older exports start with a 'Category: All categories' line + blank line; newer ones don't
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    skip = next(i for i, ln in enumerate(lines) if ln.count(",") >= len(TERMS))
    df = pd.read_csv(path, skiprows=skip, encoding="utf-8-sig")
    df = df.rename(columns={df.columns[0]: "date"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    df.columns = [c.split(":")[0].strip() for c in df.columns]
    return df.replace("<1", 0.5).astype(float)


def plot(df: pd.DataFrame, out: Path):
    fig, ax = plt.subplots(figsize=(5.2, 3.6))
    for t in TERMS:
        col = next((c for c in df.columns if c.lower() == t.lower()), None)
        if col is not None:
            ax.plot(df.index, df[col], label=t, **STYLE[t])
    for y in range(2021, 2026):
        ax.axvline(pd.Timestamp(f"{y}-01-01"), color="grey", ls="--", lw=0.6, alpha=0.6)
    ax.set_ylim(0, 100)
    ax.set_xlabel("Date")
    ax.set_ylabel("Relative search interest (0-100)")
    ax.legend(frameon=False, loc="upper left")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=200)
    print(f"wrote {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--csv", default=None)
    a = ap.parse_args()
    Path("data").mkdir(exist_ok=True)
    Path("results").mkdir(exist_ok=True)
    if a.fetch:
        df = fetch()
        df.to_csv("data/trends_pytrends.csv")
    elif a.csv:
        df = read_csv(a.csv)
    else:
        ap.error("use --fetch or --csv")
    # descriptive stats behind the paper's claims
    s = df.agg(["mean", "max"]).T.round(1)
    s["mean_2025"] = df[df.index >= "2025-01-01"].mean().round(1)
    print(s)
    s.to_csv("results/figure1_stats.csv")
    plot(df, Path("results/figure1_trends.png"))


if __name__ == "__main__":
    main()
