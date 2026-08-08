#!/usr/bin/env python3
import os
import re
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
import time
import concurrent.futures
from urllib.parse import unquote
from bs4 import BeautifulSoup
 
# -----------------------------
# CONFIG (must be defined before functions)
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
 
TAGS_TO_INCLUDE = ["football accessories", "themed gifts", "Honeylade", "Honey", "sports gifts", "fan accessories", "novelty gifts", "sports fans"]
 
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
        # Use BeautifulSoup to handle HTML entities and sanitize tags
        tag = BeautifulSoup(tag, "html.parser").text.strip()
        tag = tag[:255] if len(tag) > 255 else tag
        if tag.lower() not in seen:
            seen.add(tag.lower())
            sanitized.append(tag)
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
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    if " " in url:
        return False
    if not any(url.lower().endswith(ext) or ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return False
    return True
 
# -----------------------------
# SHOPIFY WRAPPERS
# -----------------------------
def shopify_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    try:
        return r.json() if r.status_code == 200 else {}
    except ValueError:
        print(f"⚠️ Error parsing response for GET request to {url}: {r.text}")
        return {}
 
def api_request(method, url, data=None):
    max_retries = 5
    retries = 0
 
    while retries < max_retries:
        try:
            if method == 'PUT':
                response = requests.put(url, headers=HEADERS, json=data)
            elif method == 'POST':
                response = requests.post(url, headers=HEADERS, json=data)
 
            if response.status_code in [200, 201]:
                return response.json()
            elif response.status_code == 429:
                print("❌ Rate limit exceeded. Retrying...")
                wait_time = int(response.headers.get('Retry-After', 1))
                time.sleep(wait_time)  # Wait based on Retry-After header
                retries += 1
                continue
            else:
                print(f"❌ Error: {response.status_code} - {response.text}")
                break
 
        except requests.exceptions.RequestException as e:
            print(f"⚠️ Request failed: {e}")
            break
 
    print("❌ Max retries reached. Aborting request.")
    return None
 
# -----------------------------
# FIND PRODUCT
# -----------------------------
def find_product_by_handle(handle):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"handle": handle})
    products = res.get("products") or []
    return products[0] if products else None
 
def find_product_by_sku_or_barcode(sku=None, barcode=None, title=None):
    if title:
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        res = shopify_get(url, {"title": title})
        for p in res.get("products", []):
            for v in p.get("variants", []):
                if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                    return p
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    params = {"limit": 250}
    page = shopify_get(url, params=params) or {}
    for p in page.get("products", []):
        for v in p.get("variants", []):
            if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                return p
    return None
 
def _existing_variant_map(existing_product):
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
def build_product(p):
    title = p.get("title") or f"Product-{p.get('sku')}"
    handle = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    cost = float(p.get("costprice") or 0)
    weight = float(p.get("weight") or 0)
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
    stock_qty = int(p.get("stock") or 0)
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
def load_xml():
    print("📥 Downloading XML...")
    r = requests.get(XML_URL)
    r.raise_for_status()
    raw_bytes = r.content
    raw = raw_bytes.decode('utf-8', errors='replace')
 
    # Save the raw feed for debugging
    try:
        with open("feed_debug.xml", "wb") as f:
            f.write(raw_bytes)
        print("ℹ️ Saved raw feed to feed_debug.xml")
    except Exception as e:
        print("⚠️ Couldn't save raw feed:", e)
 
    # Attempt to parse XML safely
    try:
        root = ET.fromstring(raw)
        items_elem = root.findall(".//post")
        print(f"🔎 Found {len(items_elem)} items (full parse).")
        products = []
        for item in items_elem[:LIMIT] if LIMIT else items_elem:
            data = {c.tag.lower(): c.text for c in item}
            products.append(data)
        if products:
            return products
    except ET.ParseError as e:
        print(f"⚠️ XML ParseError on full parse: {e}. Check the XML structure.")
        # Attempt to recover by processing fragments
        return load_xml_fragments(raw)
 
    print("❌ Could not parse feed into <post> items. Check feed_debug.xml for raw content.")
    return []
 
def load_xml_fragments(raw):
    posts = re.findall(r"(?is)<post\b[^>]*?>.*?</post>", raw)
    items = []
    for fragment in posts[:LIMIT] if LIMIT else posts:
        try:
            wrapped = f"<root>{fragment}</root>"
            root = ET.fromstring(wrapped)
            post_elem = root.find(".//post")
            if post_elem is None:
                print("⚠️ No <post> element found in fragment.")
                continue
            data = {c.tag.lower(): c.text for c in post_elem}
            items.append(data)
        except ET.ParseError as e:
            print(f"⚠️ Failed to parse a <post> fragment: {e}. Fragment: {fragment[:100]}...")  # Show part of the fragment for context
        except Exception as e:
            print(f"⚠️ Unexpected error: {e}")
    print(f"🔎 Parsed {len(items)} items from <post> fragments (limit {LIMIT if LIMIT else 'all'}).")
    return items
 
# -----------------------------
# PROCESS PRODUCT
# -----------------------------
def process_product(p):
    product_payload = build_product(p)
    handle = product_payload.get("handle")
    sku = p.get("sku")
    barcode = p.get("barcode")
    title = product_payload.get("title")
 
    # Log tags for debugging
    print(f"Tags being sent for {title}: {product_payload.get('tags')}")
 
    # Check for existing product using handle or SKU/barcode
    existing = find_product_by_handle(handle) or find_product_by_sku_or_barcode(sku=sku, barcode=barcode, title=title)
 
    if existing:
        # Update existing product
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
        response = api_request('PUT', url, {"product": product_payload})
        if response:
            print(f"🔄 Updated: {product_payload['title']} (ID: {existing['id']})")
            return "updated"
        else:
            print(f"❌ Failed to update: {product_payload['title']}")
            return "failed"
    else:
        # Create new product
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        response = api_request('POST', url, {"product": product_payload})
        product_id = response.get("product", {}).get("id")
        if product_id:
            print(f"🆕 Created: {product_payload['title']} (ID: {product_id})")
            return "created"
        else:
            print(f"❌ Failed to create: {product_payload['title']}")
            return "failed"
 
# -----------------------------
# SYNC
# -----------------------------
def run_sync():
    print("🚀 START SYNC")
 
    items = load_xml()
 
    created = 0
    updated = 0
    
    # Process products in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:  # Adjust to handle rate limits
        futures = {executor.submit(process_product, p): p for p in items}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result == "created":
                    created += 1
                elif result == "updated":
                    updated += 1
            except Exception as e:
                print(f"⚠️ Error processing product: {e}")
 
    print("✅ DONE")
    print(f"Created: {created}, Updated: {updated}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_sync()
