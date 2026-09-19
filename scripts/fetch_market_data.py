"""
fetch_market_data.py  (v1.0.0)
--------------------------------------------------------------------
ดึง OHLCV จาก Yahoo -> เขียนเป็น Parquet long-format -> (optional) push ขึ้น GitHub

รันได้ 2 โหมด:
  1) Colab      : python fetch_market_data.py --push        (ใช้ GH_TOKEN จาก Colab secrets)
  2) GH Actions : python fetch_market_data.py               (เขียนไฟล์เฉยๆ ให้ workflow commit)

Schema (long format, เหมือนกันทุก market):
    date (datetime64[ns]) | symbol (str) | open | high | low | close | adj_close | volume

หมายเหตุ: auto_adjust=False โดยตั้งใจ — เก็บ raw close + adj_close แยกกัน
เพื่อให้ฝั่ง analysis เลือกเองได้ (VP/NVDR ต้องใช้ raw price, momentum ใช้ adj ได้)
"""

import argparse
import base64
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

# ==================== CONFIG ====================

REPO   = os.environ.get("GH_REPO", "babaros2555-del/market-data")
BRANCH = os.environ.get("GH_BRANCH", "main")
OUTDIR = Path(os.environ.get("OUTDIR", "data"))

LOOKBACK_YEARS = 3          # ใช้ตอน bootstrap ครั้งแรกเท่านั้น
REFRESH_DAYS   = 10         # รอบถัดไปดึงแค่ 10 วันล่าสุดแล้ว merge (กัน late adjustment)

UNIVERSE = {
    "us": [
        "AMZN", "GOOGL", "NVDA", "ADI", "TXN", "ENPH", "DIOD", "KLAC", "RUN",
        "SPY", "QQQ",                      # ใช้กับ Market Regime Gate (f-series)
    ],
    "set": [
        "ADVANC.BK", "PTTEP.BK", "PTT.BK", "AOT.BK", "CPALL.BK",
        "KBANK.BK", "SCB.BK", "BDMS.BK", "GULF.BK", "DELTA.BK",
    ],
}

COLS = ["open", "high", "low", "close", "adj_close", "volume"]

# ==================== FETCH ====================

def fetch(tickers, period):
    """ดึงหลาย ticker พร้อมกัน -> คืน long-format DataFrame"""
    raw = yf.download(
        tickers,
        period=period,
        interval="1d",
        auto_adjust=False,
        group_by="ticker",
        threads=True,
        progress=False,
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["date", "symbol"] + COLS)

    # ticker เดียว yfinance จะคืน columns ชั้นเดียว -> ยัด MultiIndex ให้เหมือนกัน
    if not isinstance(raw.columns, pd.MultiIndex):
        raw.columns = pd.MultiIndex.from_product([[tickers[0]], raw.columns])

    try:
        df = raw.stack(level=0, future_stack=True)      # pandas >= 2.1
    except TypeError:
        df = raw.stack(level=0)                          # pandas เก่า

    df = df.rename_axis(["date", "symbol"]).reset_index()
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]

    for c in COLS:
        if c not in df.columns:
            df[c] = pd.NA

    df = df[["date", "symbol"] + COLS]
    df = df.dropna(subset=["close"])
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def merge_incremental(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """ซ้อนข้อมูลใหม่ทับเก่า: key = (date, symbol), keep=last"""
    if old is None or old.empty:
        out = new
    else:
        old["date"] = pd.to_datetime(old["date"])
        out = pd.concat([old, new], ignore_index=True)
    out = (
        out.drop_duplicates(subset=["date", "symbol"], keep="last")
           .sort_values(["symbol", "date"])
           .reset_index(drop=True)
    )
    return out

# ==================== GITHUB API ====================

API = "https://api.github.com"

def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def gh_get_sha(path, token):
    """คืน sha ของไฟล์เดิม (None ถ้ายังไม่มี). ไฟล์ >1MB ก็ยังได้ sha กลับมาปกติ"""
    import requests
    r = requests.get(
        f"{API}/repos/{REPO}/contents/{path}",
        headers=_headers(token), params={"ref": BRANCH}, timeout=30,
    )
    if r.status_code == 200:
        return r.json().get("sha")
    if r.status_code == 404:
        return None
    r.raise_for_status()

def gh_put(path, content_bytes, message, token):
    import requests
    body = {
        "message": message,
        "branch": BRANCH,
        "content": base64.b64encode(content_bytes).decode(),
    }
    sha = gh_get_sha(path, token)
    if sha:
        body["sha"] = sha
    r = requests.put(
        f"{API}/repos/{REPO}/contents/{path}",
        headers=_headers(token), json=body, timeout=120,
    )
    r.raise_for_status()
    size_kb = len(content_bytes) / 1024
    print(f"  pushed {path}  ({size_kb:,.0f} KB)")

def raw_url(path):
    return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{path}"

def read_existing(path, token=None):
    """อ่านไฟล์เดิมจาก raw URL (เร็วกว่า API และไม่ติดลิมิต 1MB)"""
    import requests
    try:
        r = requests.get(raw_url(path), timeout=60)
        if r.status_code == 200:
            return pd.read_parquet(io.BytesIO(r.content))
    except Exception as e:
        print(f"  [warn] read_existing({path}): {e}")
    return None

# ==================== MAIN ====================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="push ขึ้น GitHub ผ่าน API (โหมด Colab)")
    ap.add_argument("--full", action="store_true", help="ดึงย้อนหลังเต็ม (bootstrap ครั้งแรก)")
    ap.add_argument("--markets", default="us,set")
    args = ap.parse_args()

    token = None
    if args.push:
        token = os.environ.get("GH_TOKEN")
        if not token:
            try:
                from google.colab import userdata
                token = userdata.get("GH_TOKEN")
            except Exception:
                pass
        if not token:
            sys.exit("ERROR: ไม่พบ GH_TOKEN (ตั้งใน Colab Secrets หรือ env var)")

    OUTDIR.mkdir(parents=True, exist_ok=True)
    period = f"{LOOKBACK_YEARS}y" if args.full else f"{REFRESH_DAYS}d"
    manifest = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "markets": {}}

    for mkt in [m.strip() for m in args.markets.split(",")]:
        tickers = UNIVERSE[mkt]
        path = f"{OUTDIR.name}/{mkt}/ohlcv.parquet"
        print(f"\n[{mkt}] fetching {len(tickers)} tickers (period={period}) ...")

        new = fetch(tickers, period)
        print(f"  got {len(new):,} rows, {new['symbol'].nunique()} symbols")

        old = None if args.full else read_existing(path, token)
        df = merge_incremental(old, new)

        buf = io.BytesIO()
        df.to_parquet(buf, index=False, compression="snappy")
        data = buf.getvalue()

        local = OUTDIR / mkt / "ohlcv.parquet"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)

        manifest["markets"][mkt] = {
            "rows": int(len(df)),
            "symbols": sorted(df["symbol"].unique().tolist()),
            "start": str(df["date"].min().date()),
            "end": str(df["date"].max().date()),
            "bytes": len(data),
        }
        print(f"  merged -> {len(df):,} rows | {df['date'].min().date()} .. {df['date'].max().date()}")

        if args.push:
            gh_put(path, data, f"data({mkt}): update to {df['date'].max().date()}", token)

    mbytes = json.dumps(manifest, indent=2).encode()
    (OUTDIR / "manifest.json").write_bytes(mbytes)
    if args.push:
        gh_put(f"{OUTDIR.name}/manifest.json", mbytes, "meta: update manifest", token)

    print("\nDONE")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "symbols"}
                      for k, v in manifest["markets"].items()}, indent=2))


if __name__ == "__main__":
    main()
