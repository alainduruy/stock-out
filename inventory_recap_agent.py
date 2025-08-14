"""
Inventory Recap Agent for Shopify SKUs connected to a Google Sheet.

Features
- Pulls live data from a Google Sheet (header-based; resilient to column order changes)
- Computes projections (sell-out dates, days to stockout, reorder need, etc.)
- Groups reorder alerts by vendor (Supplier_Name)
- Posts a rich, daily recap to Slack with vendor sections + overall summary
- Designed for cron-based daily execution (e.g., GitHub Actions/Cron, Cloud Run + Cloud Scheduler)

Environment variables
- GOOGLE_APPLICATION_CREDENTIALS: Path to a service account JSON with Sheets API access (recommended)
  OR set GOOGLE_SERVICE_ACCOUNT_JSON: The JSON content itself (as a single env var string)
- SPREADSHEET_ID: Google Sheet ID (the long ID in the sheet URL)
- SHEET_NAME: Tab name in the spreadsheet to read
- SLACK_BOT_TOKEN: xoxb-... token for a Slack Bot with chat:write and files:write scopes
- SLACK_CHANNEL: Slack channel ID (e.g., C12345678) or name (#ops-inventory)
- TZ: Olson timezone, default "Europe/Paris"
- DATE_FORMAT: Optional date output format for Slack (default "%Y-%m-%d")

Sheet columns (header names must match — order doesn't matter):
SKU|Product_Name|Supplier_Name|Quantity_to_Reorder_Adjusted_by_MOQ|Quantity_to_Reorder_Raw|Current_Shopify_Stock|Incoming_PO|Incoming_PO_Quantity|Total_Available_Stock|Min_Quantity|MOQ|Lead_Time_Days|Safety_Stock_Days|Desired_Stock_Coverage_Days|Forecasted_Sales_During_Lead_Time|Forecasted_Sales_During_Coverage|Required_Stock_Level|Net_Stock_Position|Average_Daily_Sales|Last_Year_Daily_Sales|Days_To_Order|Real_Stock_Out|Stock_Out_Current_Stock_Only|Stock_Out_All_Stock|Next_PO

Notes
- If Average_Daily_Sales is missing/zero, the agent falls back to Last_Year_Daily_Sales. If both are zero, it uses a tiny epsilon to avoid division by zero.
- Dates in Next_PO are parsed leniently (YYYY-MM-DD recommended). Rows with invalid dates won't crash the run.
- The agent never writes to the sheet — it only reads it.

Scheduling (example)
- GitHub Actions: run daily at 06:30 Paris time
- Cloud Run + Cloud Scheduler: deploy this as a container and hit /run via a scheduled HTTP job

"""
from __future__ import annotations

import os
import io
import math
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytz
import pandas as pd
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Google Sheets
from google.oauth2 import service_account
from googleapiclient.discovery import build

# -----------------------------
# Config & Utilities
# -----------------------------
REQUIRED_COLUMNS = [
    "SKU",
    "Product_Name",
    "Supplier_Name",
    "Quantity_to_Reorder_Adjusted_by_MOQ",
    "Quantity_to_Reorder_Raw",
    "Current_Shopify_Stock",
    "Incoming_PO",
    "Incoming_PO_Quantity",
    "Total_Available_Stock",
    "Min_Quantity",
    "MOQ",
    "Lead_Time_Days",
    "Safety_Stock_Days",
    "Desired_Stock_Coverage_Days",
    "Forecasted_Sales_During_Lead_Time",
    "Forecasted_Sales_During_Coverage",
    "Required_Stock_Level",
    "Net_Stock_Position",
    "Average_Daily_Sales",
    "Last_Year_Daily_Sales",
    "Days_To_Order",
    "Real_Stock_Out",
    "Stock_Out_Current_Stock_Only",
    "Stock_Out_All_Stock",
    "Next_PO",
]

NUMBER_COLUMNS = [
    "Quantity_to_Reorder_Adjusted_by_MOQ",
    "Quantity_to_Reorder_Raw",
    "Current_Shopify_Stock",
    "Incoming_PO_Quantity",
    "Total_Available_Stock",
    "Min_Quantity",
    "MOQ",
    "Lead_Time_Days",
    "Safety_Stock_Days",
    "Desired_Stock_Coverage_Days",
    "Forecasted_Sales_During_Lead_Time",
    "Forecasted_Sales_During_Coverage",
    "Required_Stock_Level",
    "Net_Stock_Position",
    "Average_Daily_Sales",
    "Last_Year_Daily_Sales",
    "Days_To_Order",
    "Stock_Out_Current_Stock_Only",
    "Stock_Out_All_Stock",
]

TZ = os.getenv("TZ", "Europe/Paris")
DATE_FORMAT = os.getenv("DATE_FORMAT", "%Y-%m-%d")

tz = pytz.timezone(TZ)


def now_tz() -> datetime:
    return datetime.now(tz)


# -----------------------------
# Google Sheets fetch
# -----------------------------

def get_sheets_service():
    """Authenticate and return a Google Sheets API service object."""
    creds = None
    # Two ways: path or raw JSON in env var
    service_json_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    service_json_blob = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    
    # Debug: Check what environment variables are available
    print(f"DEBUG: GOOGLE_APPLICATION_CREDENTIALS is {'set' if service_json_path else 'not set'}")
    print(f"DEBUG: GOOGLE_SERVICE_ACCOUNT_JSON is {'set' if service_json_blob else 'not set'}")

    if service_json_blob:
        # Validate that the JSON blob is not empty
        if not service_json_blob.strip():
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON environment variable is empty"
            )
        
        # Debug: log the first 100 characters to help diagnose issues
        print(f"DEBUG: GOOGLE_SERVICE_ACCOUNT_JSON length: {len(service_json_blob)}")
        print(f"DEBUG: GOOGLE_SERVICE_ACCOUNT_JSON preview: {service_json_blob[:100]}...")
        
        try:
            info = json.loads(service_json_blob)
        except json.JSONDecodeError as e:
            # Check if the content starts with *** which indicates GitHub secret masking
            if service_json_blob.strip().startswith('***'):
                raise RuntimeError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON appears to contain masked content (starts with ***). "
                    "This suggests the GitHub secret may not be properly configured. "
                    "Ensure the secret contains valid JSON content from your Google Service Account key file, "
                    "not a masked or placeholder value."
                )
            else:
                raise RuntimeError(
                    f"Invalid JSON in GOOGLE_SERVICE_ACCOUNT_JSON: {e}. Content preview: {service_json_blob[:200]}"
                )
        
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
        )
    elif service_json_path and os.path.exists(service_json_path):
        creds = service_account.Credentials.from_service_account_file(
            service_json_path,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
        )
    else:
        raise RuntimeError(
            "Missing credentials: set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS. "
            "For GitHub Actions, ensure the GOOGLE_SERVICE_ACCOUNT_JSON secret is properly configured "
            "and contains valid JSON content from your Google Service Account key file."
        )

    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def fetch_sheet_as_df(spreadsheet_id: str, sheet_name: str) -> pd.DataFrame:
    service = get_sheets_service()
    sheet = service.spreadsheets()
    range_name = f"{sheet_name}!A:ZZ"  # generous range
    resp = sheet.values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
    values = resp.get("values", [])

    if not values:
        raise RuntimeError("Sheet has no data.")

    headers = values[0]
    rows = values[1:]
    df = pd.DataFrame(rows, columns=headers)

    return process_dataframe(df)


def fetch_csv_as_df(csv_path: str) -> pd.DataFrame:
    """Load and process CSV file with proper data type handling."""
    df = pd.read_csv(csv_path)
    return process_dataframe(df)


def process_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Common data processing for both CSV and Google Sheets data."""
    # Ensure all required columns exist (even if empty)
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = None

    # Store original 'No restock' flags before conversion
    df['_is_no_restock'] = df['Quantity_to_Reorder_Adjusted_by_MOQ'].astype(str).str.lower() == 'no restock'

    # Handle special values in the CSV
    # Replace 'No restock' with 0 for quantity columns
    quantity_cols = ["Quantity_to_Reorder_Adjusted_by_MOQ", "Quantity_to_Reorder_Raw"]
    for col in quantity_cols:
        if col in df.columns:
            df[col] = df[col].replace('No restock', 0)
    
    # Handle 'X' in MOQ and Lead_Time_Days columns
    if 'MOQ' in df.columns:
        df['MOQ'] = df['MOQ'].replace('X', 0)
    if 'Lead_Time_Days' in df.columns:
        df['Lead_Time_Days'] = df['Lead_Time_Days'].replace('X', 0)

    # Coerce numeric columns
    for col in NUMBER_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Normalize blanks
    df = df.fillna({c: 0 for c in NUMBER_COLUMNS})

    return df


# -----------------------------
# Computations
# -----------------------------

def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def parse_date(s: Any) -> Optional[datetime]:
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return None
    if isinstance(s, datetime):
        return s
    st = str(s).strip()
    if not st:
        return None
    # Try multiple formats
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d/%m/%y", "%m/%d/%y"):
        try:
            return tz.localize(datetime.strptime(st, fmt))
        except Exception:
            continue
    # ISO-ish fallback
    try:
        dt = datetime.fromisoformat(st)
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        return dt
    except Exception:
        return None


def compute_row_metrics(row: pd.Series, today: datetime) -> Dict[str, Any]:
    """Compute projections for a single SKU row and return a dict of calculated fields."""
    current_stock = safe_float(row.get("Current_Shopify_Stock"))
    total_available = safe_float(row.get("Total_Available_Stock"))

    ads = safe_float(row.get("Average_Daily_Sales"))
    if ads <= 0:
        ads = safe_float(row.get("Last_Year_Daily_Sales"))
    if ads <= 0:
        ads = 1e-6  # epsilon to avoid div-by-zero

    days_to_so_current = current_stock / ads
    days_to_so_all = total_available / ads

    # Cap the days to prevent date overflow (max ~27 years from today)
    MAX_DAYS = 10000
    days_to_so_current = min(days_to_so_current, MAX_DAYS)
    days_to_so_all = min(days_to_so_all, MAX_DAYS)

    sellout_current_date = today + timedelta(days=math.ceil(days_to_so_current))
    sellout_all_date = today + timedelta(days=math.ceil(days_to_so_all))

    # Reorder recommendation
    qty_adj_moq = safe_float(row.get("Quantity_to_Reorder_Adjusted_by_MOQ"))
    qty_raw = safe_float(row.get("Quantity_to_Reorder_Raw"))
    
    # Check if this is a 'No restock' item using the stored flag
    is_no_restock = bool(row.get("_is_no_restock", False))
    
    days_to_order = safe_float(row.get("Days_To_Order"), default=0.0)
    
    # Only flag as needing reorder if:
    # 1. Not a 'No restock' item AND
    # 2. Has positive reorder quantity OR (days to order <= 0 AND has some stock movement)
    needs_reorder = not is_no_restock and (qty_adj_moq > 0)

    next_po_dt = parse_date(row.get("Next_PO"))

    return {
        "ads": ads,
        "days_to_so_current": days_to_so_current,
        "days_to_so_all": days_to_so_all,
        "sellout_current_date": sellout_current_date,
        "sellout_all_date": sellout_all_date,
        "needs_reorder": needs_reorder,
        "qty_to_reorder": qty_adj_moq if qty_adj_moq > 0 else max(0.0, qty_raw),
        "next_po_dt": next_po_dt,
    }


def enrich_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    today = now_tz()
    calcs: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        calcs.append(compute_row_metrics(row, today))

    calc_df = pd.DataFrame(calcs)
    out = pd.concat([df.reset_index(drop=True), calc_df], axis=1)

    # Friendly date strings
    out["Sellout_Current_Stock_Date"] = out["sellout_current_date"].dt.strftime(DATE_FORMAT)
    out["Sellout_All_Stock_Date"] = out["sellout_all_date"].dt.strftime(DATE_FORMAT)
    
    # Handle Next_PO_Date which may contain None values
    out["Next_PO_Date"] = out["next_po_dt"].apply(
        lambda x: x.strftime(DATE_FORMAT) if x is not None and pd.notna(x) else ""
    )

    # Short vendor summary flags
    out["Urgency"] = pd.cut(
        out["days_to_so_all"],
        bins=[-float("inf"), 0, 3, 7, 14, 30, float("inf")],
        labels=["out", "≤3d", "≤7d", "≤14d", "≤30d", ">30d"],
        include_lowest=True,
    )

    return out


# -----------------------------
# Slack formatting
# -----------------------------

def build_vendor_buckets(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Return skus that need reorder grouped by Supplier_Name."""
    needing = df[df["needs_reorder"] == True].copy()
    buckets: Dict[str, pd.DataFrame] = {}
    for vendor, g in needing.groupby("Supplier_Name"):
        # sort high urgency first
        g = g.sort_values(by=["days_to_so_all", "qty_to_reorder"], ascending=[True, False])
        buckets[str(vendor) if vendor is not None else "(Unknown Vendor)"] = g
    return buckets


def blocks_header() -> List[Dict[str, Any]]:
    date_str = now_tz().strftime('%Y-%m-%d')
    header_text = f"📦 *Daily Inventory & Purchase Order Report*\nDate: {date_str}\n⸻"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": header_text}}]


def blocks_consolidated_kpis(df: pd.DataFrame) -> List[Dict[str, Any]]:
    # Filter to only SKUs that need reordering
    reorder_df = df[df["needs_reorder"] == True]
    
    # Calculate consolidated KPIs
    suppliers_to_order = len(reorder_df["Supplier_Name"].unique())
    total_skus = len(reorder_df)
    total_qty = int(reorder_df["qty_to_reorder"].sum())
    
    kpi_text = (
        f"1️⃣ *Consolidated KPIs*\n"
        f"\t•\tSuppliers to Order From: {suppliers_to_order}\n"
        f"\t•\tTotal SKUs to Order: {total_skus}\n"
        f"\t•\tTotal Quantity to Order: {total_qty}\n"
    )
    
    return [{"type": "section", "text": {"type": "mrkdwn", "text": kpi_text}}]


def format_sku_line(row: pd.Series) -> str:
    sku = str(row.get("SKU", ""))
    name = str(row.get("Product_Name", "")).strip()
    ads = row.get("ads", 0.0)
    lt = safe_float(row.get("Lead_Time_Days"))
    moq = int(safe_float(row.get("MOQ")))
    qty_to_reorder = int(round(safe_float(row.get("qty_to_reorder"))))

    days_all = row.get("days_to_so_all", 0.0)
    so_date_all = row.get("Sellout_All_Stock_Date", "-")

    next_po = row.get("Next_PO_Date", "-")

    return (
        f"*{sku}* – {name}\n"
        f"ADS: {ads:.2f} | LT: {int(lt)}d | MOQ: {moq}\n"
        f"Reorder: *{qty_to_reorder}* | Sellout(all): *{so_date_all}* ({days_all:.1f}d) | Next PO: {next_po}"
    )


def blocks_suppliers_requiring_pos(buckets: Dict[str, pd.DataFrame]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    
    section_text = "2️⃣ *Suppliers Requiring Purchase Orders*\n"
    
    if not buckets:
        section_text += "No purchase orders needed today. ✅\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": section_text}})
        return blocks
    
    for vendor, g in buckets.items():
        total_qty = int(g["qty_to_reorder"].sum())
        sku_count = len(g)
        
        section_text += f"*{vendor}*\n"
        
        # Simplified format for all brands
        section_text += f"  • *{sku_count} SKUs* ({total_qty} units) to Order\n"
        
        section_text += "\n"
    
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": section_text}})
    return blocks


def blocks_skus_selling_out_this_month(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Create section for SKUs selling out in less than 1 month based on Real_Stock_Out column."""
    blocks: List[Dict[str, Any]] = []
    today = now_tz()
    
    # Calculate days until stock out for each row
    selling_out_rows = []
    sold_out_rows = []
    
    for _, row in df.iterrows():
        # First check for sold-out items (zero stock) for Proclub and Camber
        current_stock = safe_float(row.get("Current_Shopify_Stock", 0))
        supplier = str(row.get("Supplier_Name", ""))
        
        if current_stock <= 0 and ("proclub" in supplier.lower() or "camber" in supplier.lower()):
            row_dict = row.to_dict()
            row_dict['days_until_stock_out'] = 0  # Already sold out
            row_dict['stock_out_date_formatted'] = "Already sold out"
            sold_out_rows.append(row_dict)
            continue
        
        # Then process items with Real_Stock_Out dates
        real_stock_out_str = row.get("Real_Stock_Out")
        if pd.isna(real_stock_out_str) or str(real_stock_out_str).strip() == "":
            continue
            
        # Parse the Real_Stock_Out date
        stock_out_date = parse_date(real_stock_out_str)
        if stock_out_date is None:
            continue
            
        # Calculate days until stock out
        days_until_stock_out = (stock_out_date - today).days
        
        # Include SKUs selling out in less than 30 days and in the future
        if 0 <= days_until_stock_out < 30:
            row_dict = row.to_dict()
            row_dict['days_until_stock_out'] = days_until_stock_out
            row_dict['stock_out_date_formatted'] = stock_out_date.strftime(DATE_FORMAT)
            selling_out_rows.append(row_dict)
    
    section_text = "3️⃣ *SKUs Selling Out This Month*\n"
    
    if not selling_out_rows and not sold_out_rows:
        section_text += "No SKUs selling out in the next 30 days. ✅\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": section_text}})
        return blocks
    
    # Sort by days until stock out (most urgent first)
    selling_out_rows.sort(key=lambda x: x['days_until_stock_out'])
    sold_out_rows.sort(key=lambda x: x['days_until_stock_out'], reverse=True)  # Most recently sold out first
    
    total_skus = len(selling_out_rows)
    if total_skus > 0:
        section_text += f"*{total_skus} SKUs* selling out in less than 30 days:\n\n"
    
    # Helper function to normalize supplier names
    def normalize_supplier_name(supplier):
        supplier_str = str(supplier) if supplier is not None else "(Unknown Supplier)"
        # Group all Proclub variants under "Proclub"
        if "proclub" in supplier_str.lower():
            return "Proclub"
        return supplier_str
    
    # Group by supplier for better organization
    from collections import defaultdict
    supplier_groups = defaultdict(list)
    sold_out_groups = defaultdict(list)
    
    for row in selling_out_rows:
        supplier_name = normalize_supplier_name(row.get("Supplier_Name"))
        supplier_groups[supplier_name].append(row)
    
    for row in sold_out_rows:
        supplier_name = normalize_supplier_name(row.get("Supplier_Name"))
        sold_out_groups[supplier_name].append(row)
    
    # Display selling out SKUs grouped by date, then alphabetically
    for supplier_name, group in supplier_groups.items():
        section_text += f"*{supplier_name}*:\n"
        
        # Group by days until stock out, then sort alphabetically within each group
        from collections import defaultdict
        date_groups = defaultdict(list)
        
        for row in group:
            days_until_stock_out = row['days_until_stock_out']
            stock_out_date = row['stock_out_date_formatted']
            date_groups[(days_until_stock_out, stock_out_date)].append(row)
        
        # Sort by days until stock out (most urgent first)
        sorted_dates = sorted(date_groups.keys(), key=lambda x: x[0])
        
        for days_until_stock_out, stock_out_date in sorted_dates:
            section_text += f" Sold-out in *{days_until_stock_out} days* ({stock_out_date}):\n"
            
            # Sort SKUs alphabetically within this date group
            date_group_sorted = sorted(date_groups[(days_until_stock_out, stock_out_date)], 
                                     key=lambda x: str(x.get("SKU", "")))
            
            for row in date_group_sorted:
                sku = str(row.get("SKU", ""))
                current_stock = int(safe_float(row.get("Current_Shopify_Stock")))
                section_text += f"  • *{sku}* – {current_stock} left\n"
        
        # Add sold-out SKUs for this supplier if any
        if supplier_name in sold_out_groups:
            section_text += f" Already *sold-out*:\n"
            sold_out_sorted = sorted(sold_out_groups[supplier_name], key=lambda x: str(x.get("SKU", "")))
            for row in sold_out_sorted:
                sku = str(row.get("SKU", ""))
                section_text += f"  • *{sku}* – 0 left\n"
        
        section_text += "\n"
    
    # Display sold-out only suppliers (those not in selling_out_rows)
    for supplier_name, group in sold_out_groups.items():
        if supplier_name not in supplier_groups:
            section_text += f"*{supplier_name}*:\n"
            section_text += f" Already *sold-out*:\n"
            sold_out_sorted = sorted(group, key=lambda x: str(x.get("SKU", "")))
            for row in sold_out_sorted:
                sku = str(row.get("SKU", ""))
                section_text += f"  • *{sku}* – 0 left\n"
            section_text += "\n"

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": section_text}})
    return blocks









def build_slack_blocks(df: pd.DataFrame) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    
    # Build all sections
    blocks += blocks_header()
    blocks += blocks_consolidated_kpis(df)
    
    buckets = build_vendor_buckets(df)
    blocks += blocks_suppliers_requiring_pos(buckets)
    blocks += blocks_skus_selling_out_this_month(df)
    
    return blocks


# -----------------------------
# Slack client
# -----------------------------

def send_slack_report(blocks: List[Dict[str, Any]], client: WebClient, channel: str) -> None:
    try:
        client.chat_postMessage(channel=channel, blocks=blocks, text="Daily Inventory Recap")
    except SlackApiError as e:
        if e.response['error'] in ['msg_too_large', 'invalid_blocks']:
            # Split the message into chunks
            send_chunked_slack_report(blocks, client, channel)
        else:
            raise RuntimeError(f"Slack error: {e.response['error']}")


def split_large_text_block(block: Dict[str, Any], max_chars: int = 2800) -> List[Dict[str, Any]]:
    """Split a text block that exceeds character limits into multiple blocks."""
    if block.get("type") != "section" or "text" not in block:
        return [block]
    
    text_content = block["text"].get("text", "")
    if len(text_content) <= max_chars:
        return [block]
    
    # Split by sections (marked by ⸻ or vendor names starting with *)
    lines = text_content.split('\n')
    chunks = []
    current_chunk = []
    current_length = 0
    
    for line in lines:
        line_length = len(line) + 1  # +1 for newline
        
        # If adding this line would exceed limit, start new chunk
        if current_length + line_length > max_chars and current_chunk:
            chunk_text = '\n'.join(current_chunk)
            chunks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk_text}
            })
            current_chunk = []
            current_length = 0
        
        current_chunk.append(line)
        current_length += line_length
    
    # Add remaining chunk
    if current_chunk:
        chunk_text = '\n'.join(current_chunk)
        chunks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": chunk_text}
        })
    
    return chunks if chunks else [block]


def send_chunked_slack_report(blocks: List[Dict[str, Any]], client: WebClient, channel: str) -> None:
    """Send large reports in multiple messages to avoid Slack size limits."""
    # First, split any oversized text blocks
    processed_blocks = []
    for block in blocks:
        processed_blocks.extend(split_large_text_block(block))
    
    # Now chunk by number of blocks
    chunk_size = 10  # Smaller chunks to be safe
    chunk_num = 1
    total_chunks = (len(processed_blocks) + chunk_size - 1) // chunk_size
    
    for i in range(0, len(processed_blocks), chunk_size):
        chunk = processed_blocks[i:i + chunk_size]
        
        try:
            client.chat_postMessage(
                channel=channel,
                blocks=chunk,
                text=f"Daily Inventory Recap ({chunk_num}/{total_chunks})"
            )
            chunk_num += 1
        except SlackApiError as e:
            if e.response['error'] in ['msg_too_large', 'invalid_blocks']:
                # Try sending each block individually
                for j, single_block in enumerate(chunk):
                    try:
                        client.chat_postMessage(
                            channel=channel,
                            blocks=[single_block],
                            text=f"Daily Inventory Recap ({chunk_num}/{total_chunks}) - Part {j+1}"
                        )
                    except SlackApiError:
                        # If even a single block fails, send as plain text
                        if single_block.get("text", {}).get("text"):
                            try:
                                client.chat_postMessage(
                                    channel=channel,
                                    text=f"Daily Inventory Recap ({chunk_num}/{total_chunks}) - Part {j+1}\n\n{single_block['text']['text']}"
                                )
                            except SlackApiError:
                                pass  # Skip this block entirely
                chunk_num += 1
            else:
                # Different error, re-raise
                raise RuntimeError(f"Slack error in chunk {chunk_num}: {e.response['error']}")


def optionally_upload_csv(df: pd.DataFrame, client: WebClient, channel: str) -> None:
    """Uploads a CSV snapshot for auditability (optional)."""
    try:
        buf = io.StringIO()
        export_cols = [
            "SKU",
            "Product_Name",
            "Supplier_Name",
            "Current_Shopify_Stock",
            "Incoming_PO_Quantity",
            "Total_Available_Stock",
            "Average_Daily_Sales",
            "Last_Year_Daily_Sales",
            "Sellout_Current_Stock_Date",
            "Sellout_All_Stock_Date",
            "Next_PO_Date",
            "qty_to_reorder",
            "needs_reorder",
        ]
        for c in export_cols:
            if c not in df.columns:
                df[c] = None
        df[export_cols].to_csv(buf, index=False)
        buf.seek(0)

        client.files_upload_v2(
            channel=channel,
            filename=f"inventory_recap_{now_tz().strftime('%Y%m%d_%H%M')}.csv",
            title="Inventory Recap – Full Export",
            filetype="csv",
            content=buf.getvalue(),
        )
    except Exception:
        # Non-fatal – the main message has already been sent
        traceback.print_exc()


# -----------------------------
# Main
# -----------------------------

def main() -> int:
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    sheet_name = os.getenv("SHEET_NAME")
    csv_path = os.getenv("CSV_PATH")
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    slack_channel = os.getenv("SLACK_CHANNEL")

    # Check data source - either Google Sheets or CSV
    if csv_path:
        if not os.path.exists(csv_path):
            print(f"CSV file not found: {csv_path}", file=sys.stderr)
            return 2
    elif not spreadsheet_id or not sheet_name:
        print("Missing data source: provide either CSV_PATH or both SPREADSHEET_ID and SHEET_NAME", file=sys.stderr)
        return 2

    if not slack_token or not slack_channel:
        print("Missing SLACK_BOT_TOKEN or SLACK_CHANNEL", file=sys.stderr)
        return 2

    try:
        if csv_path:
            df_raw = fetch_csv_as_df(csv_path)
        else:
            df_raw = fetch_sheet_as_df(spreadsheet_id, sheet_name)
        df = enrich_dataframe(df_raw)

        blocks = build_slack_blocks(df)

        client = WebClient(token=slack_token)
        send_slack_report(blocks, client, slack_channel)

        # Optional CSV (doesn't block success)
        optionally_upload_csv(df, client, slack_channel)

        print("Report sent.")
        return 0

    except Exception as e:
        traceback.print_exc()
        # Best-effort Slack error ping if config allows
        try:
            if slack_token and slack_channel:
                client = WebClient(token=slack_token)
                err_text = f":warning: Inventory agent failed: {e}\n```${traceback.format_exc()[:2500]}```"
                client.chat_postMessage(channel=slack_channel, text=err_text)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
