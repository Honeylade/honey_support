#!/usr/bin/env python3
"""
updater_graphql.py
Single-file Shopify updater using Admin GraphQL API, streaming XML feed,
batching, variant-id preservation, and S3 checkpointing for resumable runs.
 
Requirements:
- Python 3.9+
- requests
- boto3 (if using S3 checkpointing)
- lxml
- beautifulsoup4 (optional fallback)
"""
 
from __future__ import annotations
import os
import re
import sys
import time
import json
import math
import uuid
import logging
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
 
import requests
from lxml import etree
 
# Optional imports
try:
    from bs4 import BeautifulSoup  # fallback repair
except Exception:
    BeautifulSoup = None
 
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except Exception:
    boto3 = None
 
# -----------------------------
# CONFIG (edit or use env vars)
# -----------------------------
SHOP = os.environ.get("SHOP_URL", "your-shop.myshopify.com")  # without https
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "shpat_xxx")
API_VERSION = os.environ.get("API_VERSION", "2024-01")
GRAPHQL_ENDPOINT = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
XML_URL = os.environ.get("XML_URL", "https://example.com/feed.xml")
# LIMIT: None means process ALL products
LIMIT = None if os.environ.get("LIMIT") in (None, "", "None") else int(os.environ.get("LIMIT"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "25"))
WORKERS = int(os.environ.get("WORKERS", "2"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))
CHECKPOINT_S3_BUCKET = os.environ.get("CHECKPOINT_S3_BUCKET")  # optional
CHECKPOINT_S3_KEY = os.environ.get("CHECKPOINT_S3_KEY", "shopify_updater_checkpoint.json")
LOCAL_CHECKPOINT = os.environ.get("LOCAL_CHECKPOINT", "checkpoint.json")
USER_AGENT = "updater_graphql/1.0"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
 
# Pricing constants (example; keep your existing logic)
FIXED_FEE = 0.80
FEE_RATE = 0.029
FEE_FIXED = 0.09
VAT_RATE = 0.20
 
HEADERS = {
    "Content-Type": "application/json",
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "User-Agent": USER_AGENT,
}
 
# Tags that SmartCollections use (example)
TAGS_TO_INCLUDE = ["Honey", "Honey Spray", "Honeycomb"]
 
# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("updater")
 
# -----------------------------
# Utilities: retries/backoff
# -----------------------------
def backoff_sleep(attempt: int, base: float = 2.0, jitter: float = 0.1):
    sleep = (base ** attempt) + (jitter * (2 * (os.urandom(1)[0] / 255.0) - 1))
    time.sleep(max(0.5, sleep))
 
# -----------------------------
# Checkpoint helpers (S3 or local)
# -----------------------------
class CheckpointStore:
    def __init__(self, s3_bucket: Optional[str] = None, s3_key: Optional[str] = None, local_path: str = "checkpoint.json"):
        self.s3_bucket = s3_bucket
        self.s3_key = s3_key
        self.local_path = local_path
        self.lock = threading.Lock()
        self.state = {"last_index": -1, "processed": 0, "errors": []}
        if s3_bucket and boto3 is None:
            log.warning("boto3 not installed; S3 checkpointing disabled.")
            self.s3_bucket = None
 
    def load(self):
        with self.lock:
            if self.s3_bucket:
                try:
                    s3 = boto3.client("s3")
                    obj = s3.get_object(Bucket=self.s3_bucket, Key=self.s3_key)
                    buf = obj["Body"].read()
                    self.state = json.loads(buf)
                    log.info("Loaded checkpoint from S3.")
                    return
                except Exception as e:
                    log.info("No S3 checkpoint or failed to load: %s", e)
            if os.path.exists(self.local_path):
                try:
                    with open(self.local_path, "r") as f:
                        self.state = json.load(f)
                    log.info("Loaded local checkpoint.")
                except Exception as e:
                    log.warning("Failed to load local checkpoint: %s", e)
 
    def save(self):
        with self.lock:
            payload = json.dumps(self.state)
            if self.s3_bucket:
                try:
                    s3 = boto3.client("s3")
                    s3.put_object(Bucket=self.s3_bucket, Key=self.s3_key, Body=payload.encode("utf-8"))
                    log.info("Saved checkpoint to S3.")
                except Exception as e:
                    log.warning("Failed to save checkpoint to S3: %s", e)
            try:
                with open(self.local_path, "w") as f:
                    f.write(payload)
                log.info("Saved local checkpoint.")
            except Exception as e:
                log.warning("Failed to save local checkpoint: %s", e)
 
    def update_progress(self, last_index: int, processed_inc: int = 1, errors: Optional[List[dict]] = None):
        with self.lock:
            self.state["last_index"] = last_index
            self.state["processed"] = self.state.get("processed", 0) + processed_inc
            if errors:
                self.state.setdefault("errors", []).extend(errors)
 
# -----------------------------
# XML streaming/fragment parser
# -----------------------------
POST_RE = re.compile(r"<post\b.*?>.*?</post>", re.DOTALL | re.IGNORECASE)
 
def stream_feed_fragments(xml_text: str):
    """Yield XML fragments for each <post>...</post> block. Falls back to full parse if needed."""
    for m in POST_RE.finditer(xml_text):
        yield m.group(0)
 
def fetch_feed_stream(url: str):
    """Fetch feed and yield fragments; keeps memory low."""
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    buf = []
    for chunk in r.iter_content(chunk_size=65536, decode_unicode=True):
        if not chunk:
            continue
        buf.append(chunk)
        text = "".join(buf)
        # yield all complete post fragments currently in text
        matches = list(POST_RE.finditer(text))
        last_end = 0
        for m in matches:
            fragment = m.group(0)
            yield fragment
            last_end = m.end()
        if last_end:
            buf = [text[last_end:]]
        # keep only a modest buffer
        if sum(len(x) for x in buf) > 1_000_000:
            # fallback: attempt to salvage by yielding and clearing buffer
            yield "".join(buf)
            buf = []
    # leftover
    if buf:
        leftover = "".join(buf)
        # try to extract remaining posts
        for m in POST_RE.finditer(leftover):
            yield m.group(0)
 
def parse_post_fragment(fragment: str) -> Dict[str, Any]:
    """Parse required fields from a <post> fragment. Returns dict with keys used by workflow."""
    # Use lxml fragment parsing
    try:
        root = etree.fromstring(fragment.encode("utf-8"))
    except Exception:
        # try simple BeautifulSoup fallback
        if BeautifulSoup:
            soup = BeautifulSoup(fragment, "xml")
            def txt(tag):
                t = soup.find(tag)
                return t.get_text(strip=True) if t else ""
            item = {
                "id": txt("id") or str(uuid.uuid4()),
                "title": txt("title"),
                "sku": txt("sku"),
                "price_cost": float(txt("cost") or 0.0),
                "size": txt("size"),
                "weight": txt("weight") or "",  # weight expected in grams (string)
                "image": txt("image"),
                "tags": [t.strip() for t in (txt("tags") or "").split(",") if t.strip()],
                "raw": fragment,
            }
            return item
        else:
            # last resort: regex extracts
            def find(tag):
                m = re.search(rf"<{tag}>(.*?)</{tag}>", fragment, re.DOTALL|re.IGNORECASE)
                return m.group(1).strip() if m else ""
            item = {
                "id": find("id") or str(uuid.uuid4()),
                "title": find("title"),
                "sku": find("sku"),
                "price_cost": float(find("cost") or 0.0),
                "size": find("size"),
                "weight": find("weight") or "",
                "image": find("image"),
                "tags": [t.strip() for t in (find("tags") or "").split(",") if t.strip()],
                "raw": fragment,
            }
            return item
    # if lxml succeeded:
    def g(tag):
        el = root.find(".//" + tag)
        return el.text.strip() if el is not None and el.text else ""
    item = {
        "id": g("id") or str(uuid.uuid4()),
        "title": g("title"),
        "sku": g("sku"),
        "price_cost": float(g("cost") or 0.0),
        "size": g("size"),
        "weight": g("weight") or "",  # weight expected in grams (string)
        "image": g("image"),
        "tags": [t.strip() for t in (g("tags") or "").split(",") if t.strip()],
        "raw": fragment,
    }
    return item
 
# -----------------------------
# Pricing with weight-based shipping
# -----------------------------
def compute_price_from_cost(cost: float, item: Optional[dict] = None) -> str:
    """
    Compute final price string (2 decimals) from cost and item fields.
    Uses weight-based shipping:
      weight = float(weight or 0)
      shipping = 3.99 if weight < 300 else 4.99 if weight < 2000 else 18
    weight is expected in grams; item['weight'] may be string or numeric.
    """
    # margin tiers (example)
    if cost < 5:
        margin = 0.30
    elif cost < 10:
        margin = 0.25
    else:
        margin = 0.20
    base = cost * (1 + margin)
    taxed = base * (1 + VAT_RATE)
 
    # determine weight (grams)
    weight_val = None
    if item:
        w = item.get("weight")
        try:
            weight_val = float(w) if w not in (None, "") else 0.0
        except Exception:
            # try to extract digits
            m = re.search(r"(\d+(\.\d+)?)", str(w or ""))
            weight_val = float(m.group(1)) if m else 0.0
    if weight_val is None:
        weight_val = 0.0
 
    # shipping rule (your snippet)
    if weight_val < 300:
        shipping = 3.99
    elif weight_val < 2000:
        shipping = 4.99
    else:
        shipping = 18.00
 
    fees_factor = FEE_RATE + FEE_FIXED
    if fees_factor >= 1.0:
        raise ValueError("fees factor invalid")
    final_after_fees = taxed / (1 - fees_factor)
    final = final_after_fees + shipping + FIXED_FEE
    return f"{round(final + 1e-9, 2):.2f}"  # price as string with 2 decimals
 
# -----------------------------
# GraphQL helpers
# -----------------------------
def graphql_request(query: str, variables: Optional[dict] = None, max_retry: int = MAX_RETRIES) -> dict:
    body = {"query": query}
    if variables is not None:
        body["variables"] = variables
    attempt = 0
    while True:
        attempt += 1
        try:
            r = requests.post(GRAPHQL_ENDPOINT, headers=HEADERS, json=body, timeout=60)
            if r.status_code == 200:
                data = r.json()
                if "errors" in data:
                    log.warning("GraphQL returned errors: %s", data["errors"])
                return data
            elif r.status_code in (429, 500, 502, 503, 504):
                if attempt > max_retry:
                    r.raise_for_status()
                log.warning("GraphQL transient error %s; backoff attempt %d", r.status_code, attempt)
                backoff_sleep(attempt)
                continue
            else:
                r.raise_for_status()
        except requests.RequestException as e:
            if attempt > max_retry:
                raise
            log.warning("GraphQL request exception %s; retrying attempt %d", e, attempt)
            backoff_sleep(attempt)
 
PRODUCT_BY_HANDLE_QUERY = """
query productByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    variants(first: 250) {
      edges { node {
        id
        sku
        price
        inventoryQuantity
        selectedOptions { name value }
      }}
    }
    images(first: 10) { edges { node { src } } }
    tags
  }
}
"""
 
PRODUCT_UPDATE_MUTATION = """
mutation productUpdate($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id, title }
    userErrors { field message }
  }
}
"""
 
def to_gid(type_name: str, id_value: str):
    if id_value.startswith("gid://"):
        return id_value
    return f"gid://shopify/{type_name}/{id_value}"
 
# -----------------------------
# Variant mapping helpers
# -----------------------------
def map_existing_variants(existing_product: dict) -> Tuple[Dict[str, str], Dict[str, str]]:
    sku_map = {}
    option_map = {}
    if not existing_product:
        return sku_map, option_map
    variants = existing_product.get("variants", {}).get("edges", [])
    for edge in variants:
        node = edge.get("node", {})
        vid = node.get("id")
        sku = str(node.get("sku") or "")
        opts = node.get("selectedOptions") or []
        opt1 = ""
        if opts:
            opt1 = str(opts[0].get("value") or "")
        if sku:
            sku_map[sku] = vid
        if opt1:
            option_map[opt1] = vid
    return sku_map, option_map
 
# -----------------------------
# Product build & update
# -----------------------------
def build_product_input(item: dict, existing_variant_maps: Tuple[Dict[str,str], Dict[str,str]], existing_product_gid: Optional[str]):
    sku_map, option_map = existing_variant_maps
    variants = []
    # compute price using item (weight-aware)
    price_str = compute_price_from_cost(item.get("price_cost", 0.0), item)
    inventory_qty = item.get("inventory_quantity")
    try:
        inventory_qty = int(inventory_qty) if inventory_qty is not None else None
    except Exception:
        inventory_qty = None
 
    variant = {
        "sku": item.get("sku"),
        "price": price_str,
        "inventoryQuantity": inventory_qty,
        "option1": item.get("size") or "",
    }
    existing_vid = None
    if variant["sku"] and variant["sku"] in sku_map:
        existing_vid = sku_map[variant["sku"]]
    elif variant["option1"] and variant["option1"] in option_map:
        existing_vid = option_map[variant["option1"]]
    if existing_vid:
        variant["id"] = existing_vid
    if variant.get("inventoryQuantity") is None:
        variant.pop("inventoryQuantity", None)
    variants.append(variant)
 
    input_payload = {
        **({"id": existing_product_gid} if existing_product_gid else {}),
        "title": item.get("title") or f"Product {item.get('id')}",
        "tags": ",".join(item.get("tags", []) + TAGS_TO_INCLUDE),
        "variants": variants,
    }
    image_src = item.get("image")
    if image_src:
        input_payload["images"] = [{"src": image_src}]
    return input_payload
 
# -----------------------------
# Worker: process a batch of items
# -----------------------------
def process_batch(batch: List[dict], checkpoint: CheckpointStore, start_index: int):
    errors = []
    processed = 0
    for idx, item in enumerate(batch):
        global_index = start_index + idx
        try:
            handle = item.get("title", "").lower().replace(" ", "-")[:200] or f"p-{item.get('id')}"
            variables = {"handle": handle}
            resp = graphql_request(PRODUCT_BY_HANDLE_QUERY, variables=variables)
            data = resp.get("data", {})
            existing = data.get("productByHandle")
            existing_gid = existing.get("id") if existing else None
 
            existing_maps = map_existing_variants(existing) if existing else ({}, {})
            input_payload = build_product_input(item, existing_maps, existing_gid)
            mutation_vars = {"input": input_payload}
            resp2 = graphql_request(PRODUCT_UPDATE_MUTATION, variables=mutation_vars)
            errs = resp2.get("data", {}).get("productUpdate", {}).get("userErrors", [])
            if errs:
                log.error("User errors updating product %s: %s", handle, errs)
                errors.append({"index": global_index, "id": item.get("id"), "errors": errs})
            else:
                log.info("Updated/created product for item id=%s (handle=%s)", item.get("id"), handle)
            processed += 1
        except Exception as e:
            log.exception("Failed item index %d id %s: %s", global_index, item.get("id"), e)
            errors.append({"index": global_index, "id": item.get("id"), "exc": str(e)})
        checkpoint.update_progress(global_index, processed_inc=0, errors=None)
    checkpoint.update_progress(start_index + len(batch) - 1, processed_inc=processed, errors=errors)
    checkpoint.save()
    return processed, errors
 
# -----------------------------
# Main processing loop
# -----------------------------
def main():
    log.info("Starting updater script")
    checkpoint = CheckpointStore(CHECKPOINT_S3_BUCKET, CHECKPOINT_S3_KEY, LOCAL_CHECKPOINT)
    checkpoint.load()
    last_index = checkpoint.state.get("last_index", -1)
    processed_total = checkpoint.state.get("processed", 0)
 
    fragments = fetch_feed_stream(XML_URL)
    buffer_batch = []
    index = -1
 
    target_limit = LIMIT
 
    def submit_and_wait(batch_items, start_idx):
        return process_batch(batch_items, checkpoint, start_idx)
 
    with ThreadPoolExecutor(max_workers=WORKERS) as exe:
        for fragment in fragments:
            index += 1
            if target_limit and index >= target_limit:
                log.info("Reached LIMIT %d; breaking", target_limit)
                break
            if index <= last_index:
                continue
            try:
                item = parse_post_fragment(fragment)
            except Exception as e:
                log.exception("Failed to parse fragment at index %d: %s", index, e)
                checkpoint.update_progress(index, processed_inc=0, errors=[{"index": index, "parse_error": str(e)}])
                checkpoint.save()
                continue
            buffer_batch.append(item)
            if len(buffer_batch) >= BATCH_SIZE:
                start_idx = index - len(buffer_batch) + 1
                processed, errs = submit_and_wait(buffer_batch, start_idx)
                processed_total += processed
                buffer_batch = []
        if buffer_batch:
            start_idx = index - len(buffer_batch) + 1
            processed, errs = submit_and_wait(buffer_batch, start_idx)
            processed_total += processed
 
    log.info("Finished processing. Total processed this run: %d; last_index=%d", processed_total, checkpoint.state.get("last_index"))
 
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted by user; exiting.")
        sys.exit(1)
