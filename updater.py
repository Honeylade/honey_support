#!/usr/bin/env python3
import os
import re
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
import time
import threading
from urllib.parse import unquote
import concurrent.futures  # Import concurrent futures
import logging
 
# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
 
# -----------------------------
# CONFIG
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
XML_URL = os.getenv("XML_URL")
API_VERSION = "2024-01"
LIMIT = os.getenv("LIMIT")  # Allow all products = None or 0
if LIMIT in ["None", "0", None]:
    LIMIT = None
else:
    LIMIT = int(LIMIT)
 
HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json"
}
 
TAGS_TO_INCLUDE = ["football accessories", "football", "rugby", "entertainment", "themed gifts", "Honeylade", "flags", "sports gifts", "fan accessories", "novelty gifts", "sports fans"]
 
# Lock for thread safety
lock = threading.Lock()
 
# -----------------------------
# PRICE LOGIC
# -----------------------------
def calc_price(cost, weight):
    cost = float(cost or 0)
    weight = float(weight or 0)
    shipping = 3.99 if weight < 300 else 4.99 if weight < 2000 else 18
    margin = 0.30 if cost < 5 else 0.25 if cost < 10 else 0.20
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
def last_value(val):
    if not val:
        return ""
    v = val.split(">")[-1].strip()
    return unquote(v.replace("&amp;", "and"))
 
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
        t = unquote(tag.replace('&amp;', 'and')).strip()
        t = t[:255] if len(t) > 255 else t
        if t.lower() not in seen and all(c.isalnum() or c in [' ', '-', '_'] for c in t):
            seen.add(t.lower())
            sanitized.append(t)
    return sanitized
 
def build_description(p):
    bullets = []
    for i in range(1, 11):
        v = p.get(f"desc_{i}")
        if v:
            bullets.append(f"<li>{unquote(v)}</li>")
    bullet_html = f"<ul>{''.join(bullets)}</ul>" if bullets else ""
    paragraph = f"<p>{unquote(p.get('desc_standard','') or '')}</p>"
    return bullet_html + paragraph
 
def valid_image(url):
    if not url:
        return False
    url = url.strip()
    return url.startswith(("http://", "https://")) and not any(x in url for x in [" ", " "]) and any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"])
 
# -----------------------------
# SHOPIFY WRAPPERS
# -----------------------------
def shopify_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    try:
        return r.json() if r.status_code == 200 else {}
    except ValueError:
        logging.warning(f"Error parsing response for GET request to {url}: {r.text}")
        return {}
 
def api_request(method, url, data=None):
    max_retries = 5
    retries = 0
 
    while retries < max_retries:
        try:
            response = requests.request(method, url, headers=HEADERS, json=data)
 
            if response.status_code in [200, 201]:
                return response.json()
            elif response.status_code == 429:
                logging.warning("Rate limit exceeded. Retrying...")
                wait_time = int(response.headers.get('Retry-After', 1))
                time.sleep(wait_time)
                retries += 1
                continue
            else:
                logging.error(f"Error: {response.status_code} - {response.text}")
                break
 
        except requests.exceptions.RequestException as e:
            logging.warning(f"Request failed: {e}")
            break
 
    logging.error("Max retries reached. Aborting request.")
    return None
 
# -----------------------------
# FIND PRODUCT
# -----------------------------
def find_product_by_handle(handle):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"handle": handle})
    return res.get("products", [None])[0]
 
def find_product_by_sku_or_barcode(sku=None, barcode=None):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    params = {"limit": 250}
    res = shopify_get(url, params=params)
    for p in res.get("products", []):
        for v in p.get("variants", []):
            if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") == barcode):
                return p
    return None
 
# -----------------------------
# BUILD PRODUCT PAYLOAD
# -----------------------------
def build_product(p):
    title = p.get("title") or f"Product-{p.get('sku')}"
    handle = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    
    costprice = float(p.get("costprice", 0) or 0)
    weight = float(p.get("weight", 0) or 0)
    price = calc_price(costprice, weight)
    
    vendor = last_value(p.get("productbrand"))
    product_type = last_value(p.get("productrange"))
    tags = sanitize_tags(split_tags(p.get("productbrand")) + split_tags(p.get("productrange")) + TAGS_TO_INCLUDE + split_tags(title))
 
    if not tags:
        logging.warning(f"No valid tags for product: {title}")
        
    description = build_description(p)
 
    images = [{"src": img.strip()} for img in re.split(r"[|,]+", p.get("imageoffloads", "")) if valid_image(img)]
    
    stock_qty = int(float(p.get("stock", 0)))  # Convert to int safely
    barcode = p.get("barcode")
 
    variants = [{"price": str(price), "sku": p.get("sku"), "inventory_quantity": stock_qty, "inventory_management": "shopify", "cost": costprice, "barcode": barcode, "weight": weight, "weight_unit": "g"}]
 
    product = {
        "title": title,
        "body_html": description,
        "vendor": vendor,
        "product_type": product_type,
        "tags": ", ".join(tags),
        "handle": handle,
        "status": "active" if stock_qty > 0 else "draft",
        "published": stock_qty > 0,
        "variants": variants,
        "images": images if images else []
    }
    return product
 
# -----------------------------
# LOAD XML
# -----------------------------
def load_xml():
    logging.info("Downloading XML...")
    headers = {
        "User-Agent": "Mozilla/5.0",  # Add User-Agent header
    }
    r = requests.get(XML_URL, headers=headers)
 
    if r.status_code != 200:
        logging.error(f"Error fetching XML: {r.status_code} - {r.text}")
        return []
 
    if 'xml' not in r.headers.get('Content-Type', '').lower():
        logging.error("Expected XML but received a different content type. Content received:")
        logging.error(r.text)
        return []
 
    raw = r.content.decode('utf-8', errors='replace')
 
    items = []
    try:
        root = ET.fromstring(raw)
        for item in root.findall(".//post")[:LIMIT] if LIMIT else root.findall(".//post"):
            items.append({c.tag.lower(): c.text for c in item})
        logging.info(f"Found {len(items)} items (full parse).")
    except ET.ParseError as e:
        logging.error(f"XML ParseError: {e}")
    
    return items
 
# -----------------------------
# PROCESS PRODUCT
# -----------------------------
def process_product(p):
    with lock:  # Ensure thread safety
        product_payload = build_product(p)
        handle = product_payload.get("handle")
        sku = p.get("sku")
        barcode = p.get("barcode")
        title = product_payload.get("title")
 
        existing = find_product_by_handle(handle) or find_product_by_sku_or_barcode(sku=sku, barcode=barcode)
 
        if existing:
            existing_variants = existing.get("variants", [])
            new_variants = product_payload.get("variants", [])
 
            # Compare all relevant fields for existing and new products
            if len(existing_variants) == len(new_variants) and all(
                existing_v['sku'] == new_v['sku'] and existing_v['price'] == new_v['price'] and existing_v['inventory_quantity'] == new_v['inventory_quantity']
                for existing_v, new_v in zip(existing_variants, new_variants)
            ):
                logging.info(f"Product already exists and is up-to-date: {title} (ID: {existing['id']})")
                return "exists"
 
            # Update existing product if variants differ
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
            response = api_request('PUT', url, {"product": product_payload})
            if response:
                logging.info(f"Updated: {product_payload['title']} (ID: {existing['id']})")
                return "updated"
            else:
                logging.error(f"Failed to update: {product_payload['title']}")
                return "failed"
        else:
            # Create new product if not found
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
            response = api_request('POST', url, {"product": product_payload})
            product_id = response.get("product", {}).get("id")
            if product_id:
                logging.info(f"Created: {product_payload['title']} (ID: {product_id})")
                return "created"
            else:
                logging.error(f"Failed to create: {product_payload['title']}")
                return "failed"
 
# -----------------------------
# SYNC
# -----------------------------
def run_sync():
    logging.info("START SYNC")
 
    items = load_xml()
 
    created = 0
    updated = 0
 
    # Process products with a maximum of 2 workers to prevent race conditions
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(process_product, p): p for p in items}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result == "created":
                    created += 1
                elif result == "updated":
                    updated += 1
                elif result == "exists":
                    continue
            except Exception as e:
                logging.error(f"Error processing product: {e}")
 
    logging.info("DONE")
    logging.info(f"Created: {created}, Updated: {updated}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_sync()
