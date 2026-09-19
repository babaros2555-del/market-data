# market-data

Daily OHLCV mirror for quant toolkit — ดึงจาก Yahoo Finance ผ่าน GitHub Actions
อัปเดตอัตโนมัติทุกวันทำการ 21:30 UTC (04:30 ICT ของเช้าวันถัดไป)

เหตุผลที่มี repo นี้: sandbox ของ AI assistant เข้า `query1.finance.yahoo.com` ไม่ได้
(egress proxy ตอบ 403 `host_not_allowed`) แต่เข้า `raw.githubusercontent.com` ได้
repo นี้จึงทำหน้าที่เป็นสะพานส่งข้อมูล

## วิธีโหลดข้อมูล

```python
import pandas as pd

U = "https://raw.githubusercontent.com/babaros2555-del/market-data/main/data"
us  = pd.read_parquet(f"{U}/us/ohlcv.parquet")
th  = pd.read_parquet(f"{U}/set/ohlcv.parquet")
```

ต้องมี `pyarrow` ก่อน: `pip install pyarrow`

ถ้า parquet มีปัญหา ใช้ `manifest.json` เช็คสถานะข้อมูลก่อนได้:

```python
import json, urllib.request
m = json.load(urllib.request.urlopen(f"{U}/manifest.json"))
print(m["updated_at"], m["markets"]["set"]["end"])
```

## Schema

long format เหมือนกันทั้งสอง market:

`date | symbol | open | high | low | close | adj_close | volume`

- `auto_adjust=False` โดยตั้งใจ — เก็บ raw close กับ adj_close แยกกัน
  (Volume Profile / NVDR ต้องใช้ raw price, momentum study ใช้ adj ได้)
- `date` เป็น datetime ไม่มี timezone, normalize เป็นเที่ยงคืน
- 1 แถว = 1 ticker 1 วัน

filter ด้วย `df[df.symbol == "ADVANC.BK"]` หรือ pivot ด้วย
`df.pivot(index="date", columns="symbol", values="close")`

## Universe

**`data/us/ohlcv.parquet`**
AMZN, GOOGL, NVDA, ADI, TXN, ENPH, DIOD, KLAC, RUN + SPY, QQQ

SPY/QQQ มีไว้สำหรับ Market Regime Gate (EMA50/200)

**`data/set/ohlcv.parquet`**
ADVANC, AOT, BDMS, CPALL, DELTA, GULF, KBANK, PTT, PTTEP, SCB (suffix `.BK`)

**`data/manifest.json`**
วันที่อัปเดตล่าสุด, จำนวนแถว, รายชื่อ symbol, ช่วงวันที่ของแต่ละ market

### ข้อควรระวัง

`GULF.BK` มีประวัติราคาตั้งแต่ 2025-04 เท่านั้น (ผลจากการควบรวม GULF–INTUCH)
ไม่พอสำหรับ EMA200 หรือ Wyckoff structure ระยะยาว

## แก้ universe

1. แก้ dict `UNIVERSE` ใน `scripts/fetch_market_data.py`
2. commit
3. Actions → update-market-data → **Run workflow** (ปุ่มในแถบฟ้า ไม่ใช่ Re-run jobs)
4. **ติ๊ก `ดึงย้อนหลังเต็ม (bootstrap)`** — ถ้าไม่ติ๊ก ticker ใหม่จะได้แค่ 10 วัน

## โครงสร้าง

```
scripts/fetch_market_data.py    ดึง Yahoo -> parquet (รันได้ทั้ง Actions และ Colab)
.github/workflows/update-data.yml   cron + manual trigger
data/                            ผลลัพธ์ (เขียนโดย bot ห้ามแก้มือ)
```

`fetch_market_data.py` รันจาก Colab ได้ด้วย (`--push` ใช้ GitHub Contents API,
ต้องมี fine-grained PAT ที่ Contents: Read and write เก็บใน Colab Secrets ชื่อ `GH_TOKEN`)
ใช้ตอนอยาก push ข้อมูลที่ประมวลผลเองแล้ว เช่น NVDR master ที่ merge จากไฟล์ xlsx

การ merge เป็นแบบ incremental: key `(date, symbol)` keep last —
ข้อมูลใหม่ทับเก่าเสมอ เพื่อรองรับกรณี Yahoo ปรับ adjusted close ย้อนหลัง
