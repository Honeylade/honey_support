#!/usr/bin/env python3
from __future__ import annotations
import os
import re
import sys
import time
import json
import math
import threading
import traceback
from typing import List, Dict, Any, Optional, Tuple
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
from concurrent.futures import ThreadPoolExecutor, as_completed
 
# Optional boto3 for S3 checkpointing
try:
    import boto3  # type: ignore
except Exception:
    boto3 = None
 
# -----------------------------
# CONFIG (must be defined before functions)
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
XML_URL = os.getenv("XML_URL")
API_VERSION = os.getenv("API_VERSION", "2024-01")
LIMIT = int(os.getenv("LIMIT", "6"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "25"))
WORKERS = int(os.getenv("WORKERS", "4"))
LOCAL_CHECKPOINT = os.getenv("LOCAL_CHECKPOINT", "checkpoint.json")
CHECKPOINT_S3_BUCKET = os.getenv("CHECKPOINT_S3_BUCKET")
CHECKPOINT_S3_KEY = os.getenv("CHECKPOINT_S3_KEY")
AWS_REGION = os.getenv("AWS_REGION", "eu-west-2")  # optional
 
HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN or "",
    "Content-Type": "application/json",
    "Accept": "application/json"
}
 
TAGS_TO_INCLUDE = ["football accessories", "themed gifts", "Honeylade", "Honey", "sports gifts", "fan accessories", "novelty gifts", "sports fans"]
 
# Retry config
REQUEST_RETRIES = int(os.getenv("REQUEST_RETRIES", "3"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
 
# -----------------------------
# PRICE LOGIC (unchanged)
# -----------------------------
def calc_price(cost, weight):
    cost = float(cost or 0)
    weight = float(weight or 0)
    shipping = 3.99 if weight < 300 else 4.99 if weight < 2000 else 18
    if cost < 5:
        margin = 0.30
    elif cost < 10:
        margin = 0.25
    else:
        margin = 0.20
    TAX = 0.20
    FEES = 0.029 + 0.090
    FIXED_COSTS = 0.30 + 0.50
    base_price = cost * (1 + margin)
    taxed_price = base_price * (1 + TAX)
    price_after_fees = taxed_price / (1 - FEES)
    final_price = price_after_fees + shipping + FIXED_COSTS
    return round(final_price, 2)
 
# -----------------------------
# HELPERS
# -----------------------------
def log(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()
 
def last_value(val):
    if not val:
        return ""
    v = val.split(">")[-1].strip()
    return v.replace("&amp;", "and").replace("&", "and")
 
def split_tags(val):
    if not val:
        return []
    return [t.strip() for t in re.split(r"[>\|,;/\s]+", val) if t.strip()]
 
def sanitize_tags(tags):
    sanitized = []
    seen = set()
    for tag in tags:
        if not tag:
            continue
        t = tag.replace('&', 'and').strip()
        if len(t) > 255:
            t = t[:255]
        if t.lower() not in seen:
            seen.add(t.lower())
            sanitized.append(t)
    return sanitized
 
def build_description(p):
    bullets = []
    for i in range(1, 11):
        v = p.get(f"desc_{i}")
        if v:
            bullets.append(f"<li>{v}</li>")
    bullet_html = f"<ul>{''.join(bullets)}</ul>" if bullets else ""
    paragraph = f"<p>{p.get('desc_standard','') or ''}</p>"
    return bullet_html + paragraph
 
def valid_image(url):
    if not url:
        return False
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    if " " in url:
        return False
    if not any(url.lower().endswith(ext) or ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return False
    return True
 
# -----------------------------
# HTTP with retry wrapper
# -----------------------------
def http_request(method: str, url: str, **kwargs):
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            r = requests.request(method, url, timeout=REQUEST_TIMEOUT, headers=HEADERS, **kwargs)
            # let caller inspect status / body
            return r
        except Exception as e:
            log(f"⚠️ HTTP {method} {url} attempt {attempt} failed: {e}")
            if attempt == REQUEST_RETRIES:
                raise
            time.sleep(1 + attempt)
 
# -----------------------------
# SHOPIFY WRAPPERS (REST)
# -----------------------------
def shopify_get(url, params=None):
    r = http_request("GET", url, params=params)
    try:
        return r.json() if r.status_code in (200, 201) else {}
    except ValueError:
        return {}
 
def shopify_post(url, data):
    r = http_request("POST", url, json=data)
    if r.status_code not in [200, 201]:
        log("❌ POST ERROR:", r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {}
 
def shopify_put(url, data):
    r = http_request("PUT", url, json=data)
    if r.status_code not in [200, 201]:
        log("❌ PUT ERROR:", r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {}
 
# -----------------------------
# FIND PRODUCT (with pagination)
# -----------------------------
def find_product_by_handle(handle: str) -> Optional[Dict[str, Any]]:
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"handle": handle})
    products = res.get("products") or []
    return products[0] if products else None
 
def find_product_by_sku_or_barcode(sku: Optional[str] = None, barcode: Optional[str] = None, title: Optional[str] = None) -> Optional[Dict[str, Any]]:
    # Try title search (cheap)
    if title:
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        res = shopify_get(url, {"title": title})
        for p in res.get("products", []) or []:
            for v in p.get("variants", []):
                if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                    return p
    # Full scan with pagination (limit 250 per page)
    base = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    params = {"limit": 250}
    page_info = None
    # Use since_id pagination as safe fallback: iterate until no more or found
    last_id = 0
    while True:
        params = {"limit": 250, "since_id": last_id}
        res = shopify_get(base, params=params) or {}
        prods = res.get("products", []) or []
        if not prods:
            break
        for p in prods:
            for v in p.get("variants", []) or []:
                if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                    return p
        # set last_id to max id on page
        try:
            last_id = max(int(p.get("id", 0)) for p in prods)
        except Exception:
            break
        # safety: avoid endless loop
        if last_id == 0:
            break
    return None
 
def _existing_variant_map(existing_product: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    sku_map = {}
    option_map = {}
    for v in existing_product.get("variants", []):
        vid = v.get("id")
        sku = str(v.get("sku") or "")
        opt1 = str(v.get("option1") or "")
        if sku:
            sku_map[sku] = vid
        if opt1:
            option_map[opt1] = vid
    return sku_map, option_map
 
# -----------------------------
# BUILD PRODUCT PAYLOAD
# -----------------------------
def build_product(p: Dict[str, Any]) -> Dict[str, Any]:
    title = p.get("title") or f"Product-{p.get('sku')}"
    handle = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    try:
        cost = float(p.get("costprice") or 0)
    except Exception:
        cost = 0.0
    try:
        weight = float(p.get("weight") or 0)
    except Exception:
        weight = 0.0
    price = calc_price(cost, weight)
    vendor = last_value(p.get("productbrand"))
    product_type = last_value(p.get("productrange"))
    tags = (
        split_tags(p.get("productbrand")) +
        split_tags(p.get("productrange")) +
        TAGS_TO_INCLUDE +
        split_tags(title)
    )
    tags = sanitize_tags(tags)
    description = build_description(p)
    raw_images = (p.get("imageoffloads") or "")
    raw_images = re.split(r"[|,]+", raw_images)
    images = [{"src": img.strip()} for img in raw_images if valid_image(img)]
    variants = []
    sizes_raw = (p.get("sizeattribute") or "")
    sizes = [s.strip() for s in re.split(r"[|,]+", sizes_raw) if s.strip()]
    try:
        stock_qty = int(p.get("stock") or 0)
    except Exception:
        stock_qty = 0
    barcode = p.get("barcode") if p.get("barcode") else None
    if sizes:
        for s in sizes:
            variants.append({
                "option1": s,
                "price": str(price),
                "sku": p.get("sku"),
                "inventory_quantity": int(stock_qty),
                "inventory_management": "shopify",
                "cost": cost,
                "barcode": barcode,
                "weight": weight,
                "weight_unit": "g"
            })
    else:
        variants.append({
            "price": str(price),
            "sku": p.get("sku"),
            "inventory_quantity": int(stock_qty),
            "inventory_management": "shopify",
            "cost": cost,
            "barcode": barcode,
            "weight": weight,
            "weight_unit": "g"
        })
    product = {
        "title": title,
        "body_html": description,
        "vendor": vendor,
        "product_type": product_type,
        "tags": ", ".join(tags),
        "handle": handle,
        "status": "active" if stock_qty > 0 else "draft",
        "published": True if stock_qty > 0 else False,
        "variants": variants,
        **({"images": images} if images else {})
    }
    if len(variants) > 1:
        product["options"] = [{"name": "Size", "values": sizes}]
    return product
 
# -----------------------------
# LOAD XML (fragment-safe, BeautifulSoup fallback)
# -----------------------------
def load_xml(limit: int) -> List[Dict[str, Any]]:
    log("📥 Downloading XML feed...")
    r = http_request("GET", XML_URL)
    r.raise_for_status()
    raw_bytes = r.content
    raw = raw_bytes.decode('utf-8', errors='replace')
    try:
        with open("feed_debug.xml", "wb") as f:
            f.write(raw_bytes)
        log("ℹ️ Saved raw feed to feed_debug.xml")
    except Exception as e:
        log("⚠️ Couldn't save raw feed:", e)
    posts = re.findall(r"(?is)<post\b[^>]*?>.*?</post>", raw)
    if posts:
        log(f"🔎 Extracted {len(posts)} <post> blocks from feed (regex fragment parsing).")
        items = []
        for fragment in posts[:limit]:
            try:
                wrapped = f"<root>{fragment}</root>"
                root = ET.fromstring(wrapped)
                post_elem = root.find(".//post")
                if post_elem is None:
                    continue
                data = {}
                for c in post_elem:
                    data[c.tag.lower()] = c.text
                items.append(data)
            except Exception as e:
                log("⚠️ Failed to parse a <post> fragment:", e)
        log(f"🔎 Parsed {len(items)} items from <post> fragments (limit {limit}).")
        if items:
            return items
    try:
        root = ET.fromstring(raw)
        items_elem = root.findall(".//post")
        log(f"🔎 Found {len(items_elem)} items (full parse).")
        products = []
        for item in items_elem[:limit]:
            data = {}
            for c in item:
                data[c.tag.lower()] = c.text
            products.append(data)
        if products:
            return products
    except ParseError as e:
        log("⚠️ XML ParseError on full parse:", e)
    try:
        from bs4 import BeautifulSoup
        log("🔧 Falling back to BeautifulSoup repair parsing...")
        soup = BeautifulSoup(raw, "html.parser")
        posts_bs = soup.find_all(lambda tag: tag.name and tag.name.lower() == "post")
        log(f"🔎 BeautifulSoup found {len(posts_bs)} post-like elements.")
        items = []
        for post in posts_bs[:limit]:
            data = {}
            for child in post.find_all(recursive=False):
                tagname = child.name.lower() if child.name else None
                data[tagname] = child.get_text() if child else None
            if not data and post.get_text(strip=True):
                data["content"] = post.get_text()
            items.append(data)
        if items:
            return items
    except Exception as e:
        log("⚠️ BeautifulSoup fallback failed or bs4 missing:", e)
    log("🔎 Final fallback: heuristic splitting by '<post'...")
    pieces = re.split(r"(?i)<post\b", raw)
    candidates = []
    for piece in pieces[1:limit+1]:
        snippet = "<post" + piece
        m = re.search(r"(?is)<post\b.*?</post>", snippet)
        if m:
            fragment = m.group(0)
            try:
                wrapped = f"<root>{fragment}</root>"
                root = ET.fromstring(wrapped)
                post_elem = root.find(".//post")
                if post_elem is None:
                    continue
                data = {}
                for c in post_elem:
                    data[c.tag.lower()] = c.text
                candidates.append(data)
            except Exception:
                continue
    if candidates:
        log(f"🔎 Heuristic parsed {len(candidates)} items.")
        return candidates
    log("❌ Could not parse feed into <post> items. Check feed_debug.xml for raw content.")
    return []
 
# -----------------------------
# Checkpointing (local + optional S3)
# -----------------------------
_checkpoint_lock = threading.Lock()
 
def load_checkpoint() -> Dict[str, Any]:
    state = {"processed_handles": [], "last_index": 0}
    # load local
    if os.path.exists(LOCAL_CHECKPOINT):
        try:
            with open(LOCAL_CHECKPOINT, "r") as f:
                state = json.load(f)
                log("🔁 Loaded local checkpoint:", LOCAL_CHECKPOINT)
        except Exception:
            log("⚠️ Failed reading local checkpoint; starting fresh")
    # try S3 override if present
    if CHECKPOINT_S3_BUCKET and CHECKPOINT_S3_KEY and boto3:
        try:
            s3 = boto3.client("s3", region_name=AWS_REGION)
            obj = s3.get_object(Bucket=CHECKPOINT_S3_BUCKET, Key=CHECKPOINT_S3_KEY)
            body = obj["Body"].read()
            s3_state = json.loads(body.decode("utf-8"))
            state = s3_state
            log("🔁 Loaded checkpoint from S3:", CHECKPOINT_S3_BUCKET, CHECKPOINT_S3_KEY)
        except Exception as e:
            log("⚠️ Could not load S3 checkpoint:", e)
    return state
 
def save_checkpoint(state: Dict[str, Any]) -> None:
    with _checkpoint_lock:
        try:
            with open(LOCAL_CHECKPOINT, "w") as f:
                json.dump(state, f)
            log("💾 Saved local checkpoint:", LOCAL_CHECKPOINT)
        except Exception as e:
            log("⚠️ Failed saving local checkpoint:", e)
        if CHECKPOINT_S3_BUCKET and CHECKPOINT_S3_KEY and boto3:
            try:
                s3 = boto3.client("s3", region_name=AWS_REGION)
                s3.put_object(Bucket=CHECKPOINT_S3_BUCKET, Key=CHECKPOINT_S3_KEY, Body=json.dumps(state).encode("utf-8"))
                log("💾 Saved checkpoint to S3:", CHECKPOINT_S3_BUCKET, CHECKPOINT_S3_KEY)
            except Exception as e:
                log("⚠️ Failed saving checkpoint to S3:", e)
 
# -----------------------------
# Worker: process one product payload (create/update)
# -----------------------------
def process_item(p: Dict[str, Any], checkpoint_state: Dict[str, Any]) -> Tuple[str, Optional[int], str]:
    """
    Returns tuple: (action, product_id_or_None, message)
    action: "created", "updated", "skipped", "failed"
    """
    try:
        product_payload = build_product(p)
        handle = product_payload.get("handle")
        sku = p.get("sku")
        barcode = p.get("barcode")
        title = product_payload.get("title")
        # skip if already processed in checkpoint
        if handle in checkpoint_state.get("processed_handles", []):
            return ("skipped", None, f"Already processed handle {handle}")
        existing = find_product_by_handle(handle) or find_product_by_sku_or_barcode(sku=sku, barcode=barcode, title=title)
        if existing:
            sku_map, option_map = _existing_variant_map(existing)
            updated_variants = []
            for v in product_payload["variants"]:
                vid = None
                v_sku = str(v.get("sku") or "")
                opt1 = str(v.get("option1") or "")
                if v_sku and v_sku in sku_map:
                    vid = sku_map[v_sku]
                elif opt1 and opt1 in option_map:
                    vid = option_map[opt1]
                v_copy = v.copy()
                if vid:
                    v_copy["id"] = vid
                v_copy["price"] = str(v_copy.get("price"))
                v_copy["inventory_quantity"] = int(v_copy.get("inventory_quantity", 0))
                updated_variants.append(v_copy)
            product_update_payload = product_payload.copy()
            product_update_payload["variants"] = updated_variants
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
            shopify_put(url, {"product": product_update_payload})
            # update checkpoint atomically
            with _checkpoint_lock:
                checkpoint_state.setdefault("processed_handles", []).append(handle)
                checkpoint_state["last_index"] = checkpoint_state.get("last_index", 0) + 1
                save_checkpoint(checkpoint_state)
            return ("updated", existing.get("id"), f"Updated: {title} (ID: {existing.get('id')})")
        else:
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
            res = shopify_post(url, {"product": product_payload})
            product_id = res.get("product", {}).get("id")
            if product_id:
                with _checkpoint_lock:
                    checkpoint_state.setdefault("processed_handles", []).append(handle)
                    checkpoint_state["last_index"] = checkpoint_state.get("last_index", 0) + 1
                    save_checkpoint(checkpoint_state)
                return ("created", product_id, f"Created: {title} (ID: {product_id})")
            else:
                return ("failed", None, f"Failed to create: {title} - response: {res}")
    except Exception as exc:
        log("❌ Exception processing item:", exc)
        traceback.print_exc()
        return ("failed", None, f"Exception: {exc}")
 
# -----------------------------
# Batching utilities
# -----------------------------
def chunked(it: List[Any], size: int):
    for i in range(0, len(it), size):
        yield it[i:i+size]
 
# -----------------------------
# MAIN SYNC LOOP (batched + threads)
# -----------------------------
def run_sync():
    if not SHOP_URL or not ACCESS_TOKEN or not XML_URL:
        log("❌ SHOP_URL, ACCESS_TOKEN and XML_URL must be set.")
        sys.exit(2)
    state = load_checkpoint()
    last_index = int(state.get("last_index", 0))
    log(f"🚀 START SYNC (LIMIT={LIMIT}, BATCH_SIZE={BATCH_SIZE}, WORKERS={WORKERS}) - resuming at index {last_index}")
    items = load_xml(LIMIT)
    if not items:
        log("❌ No items parsed from feed; aborting.")
        return
    total = len(items)
    log(f"🔁 Will process {total} items (limited to {LIMIT}).")
    created = 0
    updated = 0
    failed = 0
    skipped = 0
    # process in batches
    for batch_idx, batch in enumerate(chunked(items, BATCH_SIZE), start=1):
        log(f"🔸 Processing batch {batch_idx} - {len(batch)} items")
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(process_item, p, state): p for p in batch}
            for fut in as_completed(futures):
                action, pid, msg = fut.result()
                if action == "created":
                    created += 1
                elif action == "updated":
                    updated += 1
                elif action == "skipped":
                    skipped += 1
                else:
                    failed += 1
                log(f"  -> {action.upper()}: {msg}")
        # small throttle between batches
        time.sleep(0.5)
    log("✅ DONE")
    log(f"Created: {created}, Updated: {updated}, Skipped: {skipped}, Failed: {failed}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_sync()
