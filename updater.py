import os
import requests
import xml.etree.ElementTree as ET
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
 
# -----------------------------
# CONFIGURATION
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
XML_URL = os.getenv("XML_URL")
API_VERSION = "2024-01"
 
# Limit for product synchronization
LIMIT = os.getenv("LIMIT")
LIMIT = None if LIMIT in ["None", "0", None] else int(LIMIT)
 
# Initial headers for API requests
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json"
}
 
# Tags to include in products
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
 
    response = requests.post(url, json=data)
    response.raise_for_status()
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
# PRICE CALCULATION
# -----------------------------
def calc_price(cost, weight):
    """Calculates the final price based on cost and weight."""
    cost = float(cost or 0)
    weight = float(weight or 0)
 
    shipping = 3.99 if weight < 300 else 4.99 if weight < 2000 else 18
    margin = 0.30 if cost < 5 else 0.25 if cost < 10 else 0.20
 
    TAX = 0.20
    FEES = 0.029 + 0.09
    FIXED_COSTS = 0.30 + 0.5
 
    base_price = cost * (1 + margin)
    taxed_price = base_price * (1 + TAX)
    price_after_fees = taxed_price / (1 - FEES)
 
    final_price = price_after_fees + shipping + FIXED_COSTS
 
    return round(final_price, 2)
 
# -----------------------------
# DATA SANITIZATION
# -----------------------------
def last_value(val):
    """Returns the last value from a delimited string."""
    if not val:
        return ""
    return val.split(">")[-1].strip().replace("&amp;", "and").replace("&", "and")
 
def split_tags(val):
    """Splits a string into a list of tags."""
    if not val:
        return []
    return [t.strip() for t in re.split(r"[>\|,;/\s]+", val) if t.strip()]
 
def sanitize_title(title):
    """Cleans and splits the title into a list."""
    if not title:
        return []
    title_cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", title)  # Remove special characters
    return [t.strip() for t in title_cleaned.split() if t.strip()]
 
def sanitize_tags(tags):
    """Sanitizes and limits the number of tags."""
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
# DESCRIPTION BUILDING
# -----------------------------
def build_description(p):
    """Constructs the product description from the provided data."""
    bullets = [f"<li>{p.get(f'desc_{i}')}</li>" for i in range(1, 11) if p.get(f'desc_{i}')]
    bullet_html = f"<ul>{''.join(bullets)}</ul>" if bullets else ""
    paragraph = f"<p>{p.get('desc_standard', '') or ''}</p>"
    return bullet_html + paragraph
 
# -----------------------------
# IMAGE VALIDATION
# -----------------------------
def valid_image(url):
    """Validates the image URL."""
    if not url:
        return False
    url = url.strip()
    return url.startswith(("http://", "https://")) and " " not in url and \
           any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"])
 
# -----------------------------
# SHOPIFY API WRAPPERS
# -----------------------------
def shopify_get(url, params=None):
    """Wrapper for GET requests to Shopify API."""
    response = requests.get(url, headers=HEADERS, params=params)
    return response.json() if response.status_code == 200 else {}
 
def shopify_post(url, data):
    """Wrapper for POST requests to Shopify API."""
    response = requests.post(url, headers=HEADERS, json=data)
    if response.status_code not in [200, 201]:
        print("❌ POST ERROR:", response.status_code, response.text)
    return response.json()
 
def shopify_put(url, data):
    """Wrapper for PUT requests to Shopify API."""
    response = requests.put(url, headers=HEADERS, json=data)
    if response.status_code not in [200, 201]:
        print("❌ PUT ERROR:", response.status_code, response.text)
    return response.json()
 
# -----------------------------
# PRODUCT FINDING
# -----------------------------
def find_product_by_handle(handle):
    """Finds a product by its handle."""
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"handle": handle})
    return res.get("products", [None])[0]
 
def find_product_by_sku_or_barcode(sku=None, barcode=None, title=None):
    """Finds a product by SKU or barcode."""
    if title:
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        res = shopify_get(url, {"title": title})
        for p in res.get("products", []):
            for v in p.get("variants", []):
                if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") == barcode):
                    return p
 
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    params = {"limit": 250}
    page = shopify_get(url, params=params) or {}
    for p in page.get("products", []):
        for v in p.get("variants", []):
            if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") == barcode):
                return p
    return None
 
# -----------------------------
# PRODUCT PAYLOAD BUILDING
# -----------------------------
def build_product(p):
    """Builds the product payload for Shopify API."""
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
 
    raw_images = (p.get("imageoffloads") or "").split('|')
    images = [{"src": img.strip()} for img in raw_images if valid_image(img)]
 
    variants = []
    sizes = [s.strip() for s in re.split(r"[|,]+", p.get("sizeattribute", "") or "") if s.strip()]
    stock_qty = int(p.get("stock") or 0)
    barcode = p.get("barcode")
 
    if sizes:
        for s in sizes:
            variants.append(build_variant(p, price, stock_qty, barcode, weight, s))
    else:
        variants.append(build_variant(p, price, stock_qty, barcode, weight))
 
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
        **({"images": images} if images else {})
    }
 
    if len(variants) > 1:
        product["options"] = [{"name": "Size", "values": sizes}]
 
    return product
 
def build_variant(p, price, stock_qty, barcode, weight, size=None):
    """Builds a variant payload for the product."""
    variant_data = {
        "price": price,
        "sku": p.get("sku"),
        "inventory_quantity": stock_qty,
        "inventory_management": "shopify",
        "cost": float(p.get("costprice") or 0),
        "barcode": barcode,
        "weight": weight,
        "weight_unit": "g"
    }
    if size:
        variant_data["option1"] = size
    return variant_data
 
# -----------------------------
# XML LOADING
# -----------------------------
def load_xml():
    """Loads and parses the XML data."""
    print("📥 Downloading XML...")
    r = requests.get(XML_URL)
    r.raise_for_status()
    root = ET.fromstring(r.content)
 
    items = root.findall(".//post")
    print(f"🔎 Found {len(items)} items")
 
    return [{c.tag.lower(): c.text for c in item} for item in items[:LIMIT]]
 
# -----------------------------
# PRODUCT SYNC
# -----------------------------
def sync_product(p, counters):
    """Synchronizes a product with the Shopify store."""
    product_payload = build_product(p)
    handle = product_payload.get("handle")
    sku = p.get("sku")
    barcode = p.get("barcode")
 
    existing = find_product_by_handle(handle) or find_product_by_sku_or_barcode(sku=sku, barcode=barcode)
 
    if existing:
        update_existing_product(existing, product_payload, counters)
    else:
        create_new_product(product_payload, counters)
 
def update_existing_product(existing, product_payload, counters):
    """Updates an existing product in the Shopify store."""
    if existing['variants'][0]['price'] != product_payload['variants'][0]['price'] or \
       existing['variants'][0]['inventory_quantity'] != product_payload['variants'][0]['inventory_quantity']:
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
        response = shopify_put(url, {"product": product_payload})
 
        if response:
            print(f"🔄 Updated: {product_payload['title']} (ID: {existing['id']})")
            counters['updated'] += 1
        else:
            print(f"❌ Failed to update: {product_payload['title']}")
    else:
        print(f"✅ No changes for: {product_payload['title']} (ID: {existing['id']})")
 
def create_new_product(product_payload, counters):
    """Creates a new product in the Shopify store."""
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    response = shopify_post(url, {"product": product_payload})
 
    if response:
        print(f"➕ Created: {product_payload['title']} (ID: {response['product']['id']})")
        counters['created'] += 1
    else:
        print(f"❌ Failed to create: {product_payload['title']}")
 
# -----------------------------
# RUN SYNC
# -----------------------------
def run_sync():
    """Main function to run the synchronization process."""
    print("🚀 START SYNC")
    
    # Refresh the access token at the start
    new_token = get_access_token()
    HEADERS["X-Shopify-Access-Token"] = new_token  # Update headers with the new token
 
    items = load_xml()
    counters = {'updated': 0, 'created': 0}
 
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
# ENTRY POINT
# -----------------------------
if __name__ == "__main__":
    run_sync()
