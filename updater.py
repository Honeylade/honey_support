import os
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import threading
 
# -----------------------------
# CONFIG
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
XML_URL = os.getenv("XML_URL")
API_VERSION = "2024-01"
 
LIMIT = os.getenv("LIMIT")  # Allow all products = None or 0
if LIMIT in ["None", "0", None]:
    LIMIT = None
else:
    LIMIT = int(LIMIT)
 
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json"
}
 
TAGS_TO_INCLUDE = [
    "football accessories", "football", "rugby", "entertainment",
    "themed gifts", "Honeylade", "flags", "sports gifts",
    "fan accessories", "novelty gifts", "sports fans"
]
 
# -----------------------------
# ACCESS TOKEN MANAGEMENT
# -----------------------------
_token_cache = {
    "access_token": None,
    "expires_at": 0
}
 
def get_access_token():
    """Fetches and caches the access token."""
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]
 
    url = f"https://{SHOP_URL}/admin/oauth/access_token"
    data = {
        "client_id": os.getenv("CLIENT_ID"),
        "client_secret": os.getenv("CLIENT_SECRET"),
        "grant_type": "client_credentials"
    }
 
    print(f"Requesting access token with {data}")  # Debug line
 
    try:
        response = requests.post(url, json=data)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error: {e}")
        print(f"Response content: {response.content.decode()}")
        raise
 
    token_data = response.json()
    _token_cache["access_token"] = token_data["access_token"]
    _token_cache["expires_at"] = time.time() + token_data.get("expires_in", 86400)
 
    return _token_cache["access_token"]
 
# -----------------------------
# API CALLS
# -----------------------------
def make_api_call():
    """Example function to demonstrate API call usage."""
    token = get_access_token()
    headers = {"X-Shopify-Access-Token": token}
    # ... your API call here
 
# -----------------------------
# PRICE LOGIC
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
    FEES = 0.029 + 0.09
    FIXED_COSTS = 0.30 + 0.5
 
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
    return v.replace("&amp;", "and").replace("&", "and")
 
def split_tags(val):
    if not val:
        return []
    return [t.strip() for t in re.split(r"[>\|,;/\s]+", val) if t.strip()]
 
def sanitize_title(title):
    if not title:
        return []
    title_cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", title)  # Remove special characters
    return [t.strip() for t in title_cleaned.split() if t.strip()]
 
# -----------------------------
# SANITIZE TAGS
# -----------------------------
def sanitize_tags(tags):
    sanitized = []
    seen = set()
    for tag in tags:
        if not tag:
            continue
        t = tag.replace("&amp;", "and").strip()
        t = ''.join(e for e in t if e.isalnum() or e.isspace())
        if len(t) > 255:
            t = t[:255]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            sanitized.append(t)
    return sanitized[:250]
 
# -----------------------------
# BUILD DESCRIPTION
# -----------------------------
def build_description(p):
    bullets = []
    for i in range(1, 11):
        v = p.get(f"desc_{i}")
        if v:
            bullets.append(f"<li>{v}</li>")
    bullet_html = f"<ul>{''.join(bullets)}</ul>" if bullets else ""
    paragraph = f"<p>{p.get('desc_standard','') or ''}</p>"
    return bullet_html + paragraph
 
# -----------------------------
# IMAGE VALIDATION
# -----------------------------
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
# SHOPIFY SIMPLE WRAPPERS
# -----------------------------
def shopify_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    try:
        return r.json() if r.status_code == 200 else {}
    except ValueError:
        return {}
 
def shopify_post(url, data):
    r = requests.post(url, headers=HEADERS, json=data)
    if r.status_code not in [200, 201]:
        print("❌ POST ERROR:", r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {}
 
def shopify_put(url, data):
    r = requests.put(url, headers=HEADERS, json=data)
    if r.status_code not in [200, 201]:
        print("❌ PUT ERROR:", r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {}
 
# -----------------------------
# FIND PRODUCT (safe handling for empty lists)
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
        for p in res.get("products", []) or []:
            for v in p.get("variants", []):
                if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                    return p
 
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    params = {"limit": 250}
    page = shopify_get(url, params=params) or {}
    for p in page.get("products", []) or []:
        for v in p.get("variants", []):
            if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                return p
    return None
 
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
        sanitize_title(title)
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
                "price": price,
                "sku": p.get("sku"),
                "inventory_quantity": stock_qty,
                "inventory_management": "shopify",
                "cost": cost,
                "barcode": barcode,
                "weight": weight,
                "weight_unit": "g"
            })
    else:
        variants.append({
            "price": price,
            "sku": p.get("sku"),
            "inventory_quantity": stock_qty,
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
        "status": "active" if stock_qty >= 1 else "draft",
        "published": True if stock_qty >= 1 else False,
        "variants": variants,
        **({"images": images} if images else {})
    }
 
    if len(variants) > 1:
        product["options"] = [{"name": "Size", "values": sizes}]
 
    return product
 
# -----------------------------
# LOAD XML
# -----------------------------
def load_xml():
    print("📥 Downloading XML...")
    r = requests.get(XML_URL)
    r.raise_for_status()
    
    try:
        root = ET.fromstring(r.content)
    except ParseError as e:
        print(f"❌ XML Parse Error: {e}")
        return []
 
    items = root.findall(".//post")
    print(f"🔎 Found {len(items)} items")
 
    products = []
    for item in items[:LIMIT]:
        data = {}
        for c in item:
            data[c.tag.lower()] = c.text
        products.append(data)
 
    return products
 
# -----------------------------
# SYNC PRODUCT
# -----------------------------
def sync_product(p, counters):
    product_payload = build_product(p)
    handle = product_payload.get("handle")
    sku = p.get("sku")
    barcode = p.get("barcode")
    title = product_payload.get("title")
 
    existing = find_product_by_handle(handle) or find_product_by_sku_or_barcode(sku=sku, barcode=barcode)
 
    if existing:
        # Check if we need to update the product
        needs_update = (
            existing['variants'][0]['price'] != product_payload['variants'][0]['price'] or
            existing['variants'][0]['inventory_quantity'] != product_payload['variants'][0]['inventory_quantity']
        )
 
        # Check if the product is currently in draft and should be made active
        if existing['status'] == "draft" and product_payload['variants'][0]['inventory_quantity'] >= 1:
            product_payload['status'] = "active"
            product_payload['published'] = True  # Ensure it's published as well
 
        if needs_update or existing['status'] == "draft":
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
            response = shopify_put(url, {"product": product_payload})
 
            if response:
                print(f"🔄 Updated: {product_payload['title']} (ID: {existing['id']})")
                counters['updated'] += 1
            else:
                print(f"❌ Failed to update: {product_payload['title']}")
        else:
            print(f"✅ No changes for: {product_payload['title']} (ID: {existing['id']})")
    else:
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        response = shopify_post(url, {"product": product_payload})
 
        if response:
            print(f"➕ Created: {product_payload['title']} (ID: {response['product']['id']})")
            counters['created'] += 1
        else:
            print(f"❌ Failed to create: {product_payload['title']}")
 
# -----------------------------
# CHECK FOR STOCK CHANGES
# -----------------------------
def check_for_changes(counters):
    while True:
        time.sleep(900)  # Check every 15 minutes
        # Logic to check for stock changes
        # This is a placeholder; you would need to implement the actual API call to get updated products.
        updated_products = []  # Assume this is filled with updated products
        for product in updated_products:
            sync_product(product, counters)  # Re-sync the updated product
 
# -----------------------------
# RUN SYNC
# -----------------------------
def run_sync():
    print("🚀 START SYNC")
 
    # Refresh the access token at the start
    token = get_access_token()
    HEADERS["X-Shopify-Access-Token"] = token  # Update headers with the new token
 
    items = load_xml()
    counters = {'updated': 0, 'created': 0}
 
    # Start the background thread for checking stock changes
    change_checker_thread = threading.Thread(target=check_for_changes, args=(counters,))
    change_checker_thread.daemon = True
    change_checker_thread.start()
 
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(sync_product, p, counters): p for p in items}
 
        for future in as_completed(futures):
            p = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"❌ Error syncing product {p.get('title')}: {e}")
 
    print(f"✅ DONE: Total Updated: {counters['updated']}, Total Created: {counters['created']}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_sync()
