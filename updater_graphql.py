#!/usr/bin/env python3
import os
import re
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
import time
import logging
 
# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
 
# -----------------------------
# CONFIG (must be defined before functions)
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
XML_URL = os.getenv("XML_URL")
API_VERSION = "2024-01"
LIMIT = 4
 
if not all([SHOP_URL, ACCESS_TOKEN, XML_URL]):
    logging.error("Environment variables SHOP_URL, ACCESS_TOKEN, and XML_URL must be set.")
    exit(1)
 
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
    return val.split(">")[-1].strip().replace("&amp;", "and").replace("&", "and") if val else ""
 
def split_tags(val):
    return [t.strip() for t in re.split(r"[>\|,;/\s]+", val) if t.strip()] if val else []
 
def sanitize_tags(tags):
    sanitized = []
    seen = set()
    for tag in tags:
        t = tag.replace('&', 'and').strip()
        if len(t) > 255:
            t = t[:255]
        if t.lower() not in seen:
            seen.add(t.lower())
            sanitized.append(t)
    return sanitized
 
def build_description(p):
    bullets = [f"<li>{p.get(f'desc_{i}')}</li>" for i in range(1, 11) if p.get(f'desc_{i}')]
    bullet_html = f"<ul>{''.join(bullets)}</ul>" if bullets else ""
    return bullet_html + f"<p>{p.get('desc_standard', '')}</p>"
 
def valid_image(url):
    if not url:
        return False
    url = url.strip()
    return url.startswith(("http://", "https://")) and not any(char in url for char in [" ", " "]) and any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"])
 
# -----------------------------
# SHOPIFY WRAPPERS
# -----------------------------
def shopify_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    if r.status_code != 200:
        logging.error(f"GET ERROR: {r.status_code} {r.text}")
    return r.json() if r.status_code == 200 else {}
 
def shopify_post(url, data):
    r = requests.post(url, headers=HEADERS, json=data)
    if r.status_code not in [200, 201]:
        logging.error(f"POST ERROR: {r.status_code} {r.text}")
    return r.json() if r.status_code in [200, 201] else {}
 
def shopify_put(url, data):
    r = requests.put(url, headers=HEADERS, json=data)
    if r.status_code not in [200, 201]:
        logging.error(f"PUT ERROR: {r.status_code} {r.text}")
    return r.json() if r.status_code in [200, 201] else {}
 
# -----------------------------
# FIND PRODUCT
# -----------------------------
def find_product_by_handle(handle):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"handle": handle})
    return res.get("products", [None])[0]
 
def find_product_by_sku_or_barcode(sku=None, barcode=None, title=None):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"title": title})
    for p in res.get("products", []):
        for v in p.get("variants", []):
            if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") == barcode):
                return p
    return None
 
def _existing_variant_map(existing_product):
    sku_map = {str(v.get("sku") or ""): v.get("id") for v in existing_product.get("variants", [])}
    option_map = {str(v.get("option1") or ""): v.get("id") for v in existing_product.get("variants", [])}
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
    tags = sanitize_tags(split_tags(p.get("productbrand")) + split_tags(p.get("productrange")) + TAGS_TO_INCLUDE + split_tags(title))
    description = build_description(p)
    
    raw_images = re.split(r"[|,]+", p.get("imageoffloads", ""))
    images = [{"src": img.strip()} for img in raw_images if valid_image(img)]
    
    variants = []
    sizes = [s.strip() for s in re.split(r"[|,]+", p.get("sizeattribute", "")) if s.strip()]
    stock_qty = int(p.get("stock") or 0)
    barcode = p.get("barcode") if p.get("barcode") else None
    
    base_variant = {
        "price": str(price),
        "sku": p.get("sku"),
        "inventory_quantity": stock_qty,
        "inventory_management": "shopify",
        "cost": cost,
        "barcode": barcode,
        "weight": weight,
        "weight_unit": "g"
    }
 
    if sizes:
        for s in sizes:
            variant = base_variant.copy()
            variant["option1"] = s
            variants.append(variant)
    else:
        variants.append(base_variant)
    
    product = {
        "title": title,
        "body_html": description,
        "vendor": vendor,
        "product_type": product_type,
        "tags": ", ".join(tags),
        "handle": handle,
        "status": "active" if stock_qty > 0 else "draft",
        "published": stock_qty > 0,
        "variants": variants
    }
    if images:
        product["images"] = images
    return product
 
# -----------------------------
# LOAD XML
# -----------------------------
def load_xml():
    logging.info("📥 Downloading XML...")
    try:
        r = requests.get(XML_URL)
        r.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Failed to download XML: {e}")
        return []
 
    raw = r.content.decode('utf-8', errors='replace')
    try:
        with open("feed_debug.xml", "wb") as f:
            f.write(r.content)
        logging.info("ℹ️ Saved raw feed to feed_debug.xml")
    except Exception as e:
        logging.warning(f"⚠️ Couldn't save raw feed: {e}")
 
    posts = re.findall(r"(?is)<post\b[^>]*?>.*?</post>", raw)
    items = []
 
    if posts:
        logging.info(f"🔎 Extracted {len(posts)} <post> blocks from feed.")
        for fragment in posts[:LIMIT]:
            try:
                wrapped = f"<root>{fragment}</root>"
                root = ET.fromstring(wrapped)
                post_elem = root.find(".//post")
                if post_elem is not None:
                    data = {c.tag.lower(): c.text for c in post_elem}
                    items.append(data)
            except Exception as e:
                logging.warning(f"⚠️ Failed to parse a <post> fragment: {e}")
    else:
        try:
            root = ET.fromstring(raw)
            items_elem = root.findall(".//post")
            logging.info(f"🔎 Found {len(items_elem)} items (full parse).")
            items = [{c.tag.lower(): c.text for c in item} for item in items_elem[:LIMIT]]
        except ParseError as e:
            logging.error(f"⚠️ XML ParseError on full parse: {e}")
 
    if not items:
        logging.info("🔎 Final fallback: heuristic splitting by '<post'...")
        pieces = re.split(r"(?i)<post\b", raw)
        candidates = []
        for piece in pieces[1:LIMIT + 1]:
            snippet = "<post" + piece
            m = re.search(r"(?is)<post\b.*?</post>", snippet)
            if m:
                fragment = m.group(0)
                try:
                    wrapped = f"<root>{fragment}</root>"
                    root = ET.fromstring(wrapped)
                    post_elem = root.find(".//post")
                    if post_elem is not None:
                        data = {c.tag.lower(): c.text for c in post_elem}
                        candidates.append(data)
                except Exception:
                    continue
        items = candidates
        if candidates:
            logging.info(f"🔎 Heuristic parsed {len(candidates)} items.")
 
    if not items:
        logging.error("❌ Could not parse feed into <post> items. Check feed_debug.xml for raw content.")
    return items
 
# -----------------------------
# SYNC
# -----------------------------
def run_sync():
    logging.info("🚀 START SYNC")
    items = load_xml()
    created = 0
    updated = 0
 
    for p in items:
        product_payload = build_product(p)
        handle = product_payload.get("handle")
        sku = p.get("sku")
        barcode = p.get("barcode")
        title = product_payload.get("title")
 
        existing = find_product_by_handle(handle) or find_product_by_sku_or_barcode(sku=sku, barcode=barcode, title=title)
        if existing:
            sku_map, option_map = _existing_variant_map(existing)
            updated_variants = []
 
            for v in product_payload["variants"]:
                vid = sku_map.get(str(v.get("sku") or ""), option_map.get(str(v.get("option1") or "")))
                v_copy = v.copy()
                if vid:
                    v_copy["id"] = vid
                updated_variants.append(v_copy)
 
            product_update_payload = product_payload.copy()
            product_update_payload["variants"] = updated_variants
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
            shopify_put(url, {"product": product_update_payload})
            updated += 1
            logging.info(f"🔄 Updated: {product_payload['title']} (ID: {existing['id']})")
        else:
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
            res = shopify_post(url, {"product": product_payload})
            product_id = res.get("product", {}).get("id")
            if product_id:
                created += 1
                logging.info(f"🆕 Created: {product_payload['title']} (ID: {product_id})")
            else:
                logging.error(f"❌ Failed to create: {product_payload['title']}")
 
        time.sleep(1)  # Respect rate limits
 
    logging.info("✅ DONE")
    logging.info(f"Created: {created}, Updated: {updated}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_sync()
