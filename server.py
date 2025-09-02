import os
import re
import json
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, timezone
import hmac
import hashlib
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Form
from pydantic import BaseModel, Field, validator


# Load environment variables from .env if present
load_dotenv()


APP_NAME = "kakeibo-mcp-server"


def try_get_base_dir() -> Optional[Path]:
    base = os.getenv("KAKEIBO_DIR", "").strip()
    if not base:
        return None
    p = Path(base).expanduser()
    if not p.exists() or not p.is_dir():
        return None
    return p.resolve()

def require_base_dir() -> Path:
    base = try_get_base_dir()
    if base is None:
        # 503 to indicate service not configured/ready
        raise HTTPException(status_code=503, detail="KAKEIBO_DIR is not configured or invalid")
    return base


BASE_DIR = try_get_base_dir()


# Security: Only allow Slack webhook URLs that exactly match env and use Slack domain
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
if SLACK_WEBHOOK_URL and not SLACK_WEBHOOK_URL.startswith("https://hooks.slack.com/services/"):
    # If misconfigured, ignore to avoid accidental exfiltration to arbitrary hosts
    SLACK_WEBHOOK_URL = ""

# Optional: Slack signing secret and legacy verification token for inbound commands
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").strip()
SLACK_VERIFICATION_TOKEN = os.getenv("SLACK_VERIFICATION_TOKEN", "").strip()


CSV_GLOB = "*.csv"


def list_csv_files() -> List[Path]:
    base = try_get_base_dir()
    if base is None:
        return []
    return sorted([p for p in base.glob(CSV_GLOB) if p.is_file()])


def safe_join_csv(filename: str) -> Path:
    # Normalize and restrict to CSV extension
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="filename must end with .csv")
    base = require_base_dir()
    # Prevent path traversal by resolving and ensuring under base
    target = (base / filename).resolve()
    if base not in target.parents and target != base:
        raise HTTPException(status_code=400, detail="Access denied: path outside KAKEIBO_DIR")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="CSV file not found")
    return target


CSV_COLUMNS = [
    "計算対象",
    "日付",
    "内容",
    "金額（円）",
    "保有金融機関",
    "大項目",
    "中項目",
    "メモ",
    "振替",
    "ID",
]


def read_csv(path: Path) -> pd.DataFrame:
    # Try utf-8-sig first, fallback to cp932 for Japanese Windows CSVs
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, encoding="cp932")
    # Keep only expected columns if present
    existing = [c for c in CSV_COLUMNS if c in df.columns]
    if existing:
        df = df[existing]
    return df


def load_all_csvs() -> pd.DataFrame:
    frames = []
    for f in list_csv_files():
        try:
            frames.append(read_csv(f))
        except Exception as e:
            # Skip unreadable files gracefully
            print(f"[warn] Failed to read {f.name}: {e}")
    if not frames:
        return pd.DataFrame(columns=CSV_COLUMNS)
    return pd.concat(frames, ignore_index=True)


MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ReadCsvRequest(BaseModel):
    filename: Optional[str] = Field(None, description="CSV filename under KAKEIBO_DIR")
    limit: int = Field(10, ge=1, le=200, description="Preview row limit when returning data")


class SummarizeRequest(BaseModel):
    month: str = Field(..., description="Target month in YYYY-MM")
    filename: Optional[str] = Field(None, description="If set, restrict to a single CSV file")

    @validator("month")
    def validate_month(cls, v: str) -> str:
        if not MONTH_RE.match(v):
            raise ValueError("month must be in YYYY-MM format")
        return v


class ReportRequest(SummarizeRequest):
    post_to_slack: bool = Field(False, description="Whether to post summary to Slack")


app = FastAPI(title=APP_NAME)


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    # 計算対象 & 振替 as numeric 0/1
    for col in ["計算対象", "振替"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    # 金額（円） as numeric
    if "金額（円）" in df.columns:
        df["金額（円）"] = pd.to_numeric(df["金額（円）"], errors="coerce").fillna(0)
    # 日付 to datetime
    if "日付" in df.columns:
        # Try common JP formats
        df["日付"] = pd.to_datetime(
            df["日付"], errors="coerce", format=None, infer_datetime_format=True
        )
    return df


def filter_month(df: pd.DataFrame, month: str) -> pd.DataFrame:
    if "日付" not in df.columns:
        return df.iloc[0:0]
    mask = df["日付"].dt.strftime("%Y-%m") == month
    return df[mask]


def summarize_df(df: pd.DataFrame, month: str) -> Dict[str, Any]:
    if df.empty:
        return {
            "month": month,
            "rows_total": 0,
            "rows_used": 0,
            "total_income": 0,
            "total_expense": 0,
            "net": 0,
            "by_category": [],
            "top_expenses": [],
        }

    df = _coerce_types(df)
    # Only rows marked 計算対象 == 1
    if "計算対象" in df.columns:
        df = df[df["計算対象"] == 1]
    # Filter by month
    df = filter_month(df, month)

    rows_used = len(df)
    if rows_used == 0:
        return {
            "month": month,
            "rows_total": 0,
            "rows_used": 0,
            "total_income": 0,
            "total_expense": 0,
            "net": 0,
            "by_category": [],
            "top_expenses": [],
        }

    amounts = df["金額（円）"] if "金額（円）" in df.columns else pd.Series([], dtype=float)
    total_income = float(amounts[amounts > 0].sum()) if not amounts.empty else 0.0
    total_expense = float(-amounts[amounts < 0].sum()) if not amounts.empty else 0.0
    net = float(amounts.sum()) if not amounts.empty else 0.0

    # Group by 大項目
    by_category = []
    if "大項目" in df.columns and "金額（円）" in df.columns:
        grp = df.groupby("大項目")["金額（円）"].sum().sort_values()
        # Present totals as signed; also provide expense magnitude for sorting
        for cat, total in grp.items():
            by_category.append({
                "category": cat if pd.notna(cat) else "(不明)",
                "total": float(total),
            })

    # Top 5 expense items (most negative amounts)
    top_expenses = []
    if "金額（円）" in df.columns:
        df_exp = df.sort_values("金額（円）").head(5)
        for _, row in df_exp.iterrows():
            top_expenses.append({
                "date": row.get("日付").strftime("%Y-%m-%d") if pd.notna(row.get("日付")) else None,
                "title": row.get("内容"),
                "amount": float(row.get("金額（円）", 0)),
                "category": row.get("大項目"),
                "subcategory": row.get("中項目"),
            })

    return {
        "month": month,
        "rows_total": int(rows_used),
        "rows_used": int(rows_used),
        "total_income": round(total_income, 2),
        "total_expense": round(total_expense, 2),
        "net": round(net, 2),
        "by_category": by_category,
        "top_expenses": top_expenses,
    }


def format_slack_message(summary: Dict[str, Any]) -> str:
    month = summary.get("month")
    total_income = summary.get("total_income", 0)
    total_expense = summary.get("total_expense", 0)
    net = summary.get("net", 0)
    lines = [
        f"{APP_NAME} 月次レポート: {month}",
        f"収入: {int(total_income):,} 円",
        f"支出: {int(total_expense):,} 円",
        f"収支: {int(net):,} 円",
        "カテゴリ内訳:",
    ]
    by_cat = summary.get("by_category", [])
    # Show top 6 categories by absolute spend
    by_cat_sorted = sorted(by_cat, key=lambda x: abs(x.get("total", 0)), reverse=True)[:6]
    for item in by_cat_sorted:
        lines.append(f"・{item['category']}: {int(item['total']):,} 円")
    return "\n".join(lines)


@app.get("/health")
def health() -> Dict[str, Any]:
    base = try_get_base_dir()
    status = "ok" if base else "unconfigured"
    reason = None
    if not base:
        raw = os.getenv("KAKEIBO_DIR")
        if not raw:
            reason = "KAKEIBO_DIR not set"
        else:
            reason = "KAKEIBO_DIR path invalid or not a directory"
    return {
        "status": status,
        "app": APP_NAME,
        "base_dir": str(base) if base else None,
        "csv_files": [f.name for f in list_csv_files()],
        "reason": reason,
    }


# Slack Slash Command Support
JST = timezone(timedelta(hours=9))


def parse_month_from_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    now = datetime.now(JST)
    if "今月" in t:
        return now.strftime("%Y-%m")
    if "先月" in t:
        first = now.replace(day=1)
        prev_last = first - timedelta(days=1)
        return prev_last.strftime("%Y-%m")
    m = re.search(r"(\d{4})[-/]?(0?[1-9]|1[0-2])(?:月)?", t)
    if m:
        y = int(m.group(1))
        mm = int(m.group(2))
        return f"{y:04d}-{mm:02d}"
    m2 = re.search(r"(0?[1-9]|1[0-2])月", t)
    if m2:
        mm = int(m2.group(1))
        y = now.year
        if mm > now.month:
            y -= 1
        return f"{y:04d}-{mm:02d}"
    m3 = re.search(r"(\d{4})-(\d{2})", t)
    if m3:
        return f"{int(m3.group(1)):04d}-{int(m3.group(2)):02d}"
    return None


def verify_slack_request(sig_header: str, ts_header: str, body: bytes) -> bool:
    if not SLACK_SIGNING_SECRET:
        return False
    try:
        timestamp = int(ts_header)
    except Exception:
        return False
    if abs(int(time.time()) - timestamp) > 60 * 5:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    digest = hmac.new(SLACK_SIGNING_SECRET.encode("utf-8"), basestring.encode("utf-8"), hashlib.sha256).hexdigest()
    my_sig = f"v0={digest}"
    return hmac.compare_digest(my_sig, sig_header)


@app.post("/slack/command")
async def slack_command(
    request: Request,
    token: Optional[str] = Form(None),
    command: Optional[str] = Form(None),
    text: Optional[str] = Form(None),
    user_name: Optional[str] = Form(None),
    user_id: Optional[str] = Form(None),
    response_url: Optional[str] = Form(None),
    channel_id: Optional[str] = Form(None),
):
    body = await request.body()
    sig = request.headers.get("X-Slack-Signature", "")
    ts = request.headers.get("X-Slack-Request-Timestamp", "0")
    if SLACK_SIGNING_SECRET:
        if not verify_slack_request(sig, ts, body):
            raise HTTPException(status_code=401, detail="Invalid Slack signature")
    elif SLACK_VERIFICATION_TOKEN:
        if not token or token != SLACK_VERIFICATION_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid Slack token")
    else:
        raise HTTPException(status_code=503, detail="Slack verification not configured")

    month = parse_month_from_text(text) or datetime.now(JST).strftime("%Y-%m")
    df = load_all_csvs()
    summary = summarize_df(df, month)
    msg = (
        f"{month}の収支\n"
        f"収入: {int(summary.get('total_income', 0)):,} 円\n"
        f"支出: {int(summary.get('total_expense', 0)):,} 円\n"
        f"収支: {int(summary.get('net', 0)):,} 円"
    )
    return {"response_type": "ephemeral", "text": msg}


@app.post("/read_csv")
def read_csv_endpoint(req: ReadCsvRequest) -> Dict[str, Any]:
    if not req.filename:
        files = [f.name for f in list_csv_files()]
        return {"files": files, "count": len(files)}
    path = safe_join_csv(req.filename)
    df = read_csv(path)
    preview = df.head(req.limit).to_dict(orient="records")
    return {
        "filename": path.name,
        "rows": int(len(df)),
        "preview": preview,
    }


@app.post("/summarize")
def summarize_endpoint(req: SummarizeRequest) -> Dict[str, Any]:
    if req.filename:
        df = read_csv(safe_join_csv(req.filename))
        used_files = [req.filename]
    else:
        df = load_all_csvs()
        used_files = [f.name for f in list_csv_files()]

    summary = summarize_df(df, req.month)
    summary.update({"files_used": used_files})
    return summary


@app.post("/report")
def report_endpoint(req: ReportRequest) -> Dict[str, Any]:
    summary = summarize_endpoint(req)  # type: ignore[arg-type]
    result: Dict[str, Any] = {"summary": summary, "slack_posted": False}

    if req.post_to_slack:
        if not SLACK_WEBHOOK_URL:
            raise HTTPException(status_code=400, detail="SLACK_WEBHOOK_URL is not configured or invalid")
        payload = {"text": format_slack_message(summary)}
        try:
            resp = requests.post(SLACK_WEBHOOK_URL, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=10)
            if 200 <= resp.status_code < 300:
                result["slack_posted"] = True
            result["slack_status_code"] = resp.status_code
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"Failed to post to Slack: {e}")

    return result
