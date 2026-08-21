import os
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import threading
import logging
 
# -----------------------------
# CONFIGURE LOGGING
# -----------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
 
# -----------------------------
# CONFIG
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
XML_URL = os.getenv("XML_URL")
API_VERSION = "2024-01"
 
LIMIT = os.getenv("LIMIT")
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
 
    logging.info(f"Requesting access token with {data}")
 
    try:
        response = requests.post(url, json=data, timeout=10)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP error: {e}")
        logging.error(f"Response content: {response.content.decode()}")
        raise
 
    token_data = response.json()
    _token_cache["access_token"] = token_data["access_token"]
    _token_cache["expires_at"] = time.time() + token_data.get("expires_in", 86400)
 
    return _token_cache["access_token"]
 
# -----------------------------
# API CALLS
# -----------------------------
def shopify_get(url, params=None):
    """Perform a GET request to Shopify API with rate limiting."""
    for attempt in range(5):  # Retry up to 5 times
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            if r.status_code == 429:  # Too many requests
                wait_time = int(r.headers.get("Retry-After", 1))  # Use Retry-After header if present
                logging.warning(f"Rate limit hit. Waiting for {wait_time} seconds.")
                time.sleep(wait_time)
            else:
                logging.error(f"GET request error: {e}")
                return {}
    return {}
 
def shopify_post(url, data):
    """Perform a POST request to Shopify API with retry logic."""
    for attempt in range(5):  # Retry up to 5 times
        try:
            r = requests.post(url, headers=HEADERS, json=data, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            if r.status_code == 429:  # Too many requests
                wait_time = int(r.headers.get("Retry-After", 1))  # Use Retry-After header if present
                logging.warning(f"Rate limit hit. Waiting for {wait_time} seconds.")
                time.sleep(wait_time)
            else:
                logging.error(f"POST request error: {e}")
                return {}
    return {}
 
def shopify_put(url, data, retries=3):
    """Perform a PUT request to Shopify API with retry logic."""
    for attempt in range(retries):
        try:
            r = requests.put(url, headers=HEADERS, json=data, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            if r.status_code == 429:  # Too many requests
                wait_time = int(r.headers.get("Retry-After", 1))  # Use Retry-After header if present
                logging.warning(f"Rate limit hit. Waiting for {wait_time} seconds.")
                time.sleep(wait_time)
            else:
                logging.error(f"PUT request attempt {attempt + 1} failed: {e}")
                if attempt == retries - 1:
                    return {}
            time.sleep(2)  # Wait before retrying
 
# -----------------------------
# PRICE LOGIC
# -----------------------------
def calc_price(cost, weight):
    """Calculate the final product price based on cost and weight."""
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
# HELPERS
# -----------------------------
def last_value(val):
    """Return the last value from a string separated by '>'."""
    if not val:
        return ""
    v = val.split(">")[-1].strip()
    return v.replace("&amp;", "and").replace("&", "and")
 
def split_tags(val):
    """Split tags from a string into a list."""
    if not val:
        return []
    return [t.strip() for t in re.split(r"[>\|,;/\s]+", val) if t.strip()]
 
def sanitize_title(title):
    """Sanitize product title by removing special characters."""
    if not title:
        return []
    title_cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", title)
    return [t.strip() for t in title_cleaned.split() if t.strip()]
 
def sanitize_tags(tags):
    """Sanitize tags for Shopify compatibility."""
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
    """Build product description from provided data."""
    bullets = []
    for i in range(1, 11):
        v = p.get(f"desc_{i}")
        if v:
            bullets.append(f"<li>{v}</li>")
    bullet_html = f"<ul>{''.join(bullets)}</ul>" if bullets else ""
    paragraph = f"<p>{p.get('desc_standard', '') or ''}</p>"
    return bullet_html + paragraph
 
# -----------------------------
# IMAGE VALIDATION
# -----------------------------
def valid_image(url):
    """Validate if the provided URL is a valid image URL."""
    if not url:
        return False
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    if " " in url:
        return False
    if not any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return False
    return True
 
# -----------------------------
# FIND PRODUCT
# -----------------------------
def find_product_by_handle(handle):
    """Find a product by its handle."""
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"handle": handle})
    products = res.get("products") or []
    return products[0] if products else None
 
def find_product_by_sku_or_barcode(sku=None, barcode=None, title=None):
    """Find a product by SKU or barcode."""
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
    """Construct the product payload for Shopify."""
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
    """Load XML data from the specified URL."""
    logging.info("📥 Downloading XML...")
    try:
        r = requests.get(XML_URL, timeout=10)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error downloading XML: {e}")
        return []
 
    try:
        root = ET.fromstring(r.content)
    except ParseError as e:
        logging.error(f"❌ XML Parse Error: {e}")
        return []
 
    items = root.findall(".//post")
    logging.info(f"🔎 Found {len(items)} items")
 
    products = []
    for item in items[:LIMIT]:
        data = {}
        for c in item:
            data[c.tag.lower()] = c.text
        products.append(data)
 
    return products
 
# -----------------------------
# PARSE INVENTORY QUANTITY
# -----------------------------
def parse_inventory_quantity(quantity):
    """Safely parse inventory quantity to int, handling floats represented as strings."""
    try:
        return int(float(quantity))  # Convert to float first and then to int
    except ValueError as e:
        logging.error(f"Error converting inventory quantity: {quantity} - {e}")
        return 0  # or some other default value or handling logic
 
# -----------------------------
# SYNC PRODUCT
# -----------------------------
def sync_product(p, counters, resync_quantity_counter):
    """Sync product with Shopify."""
    product_payload = build_product(p)
    handle = product_payload.get("handle")
    sku = p.get("sku")
    barcode = p.get("barcode")
 
    existing = find_product_by_handle(handle) or find_product_by_sku_or_barcode(sku=sku, barcode=barcode)
 
    if existing:
        needs_update = False
 
        # Get existing values with robust parsing
        existing_price = existing['variants'][0]['price']
        existing_inventory_quantity = parse_inventory_quantity(existing['variants'][0]['inventory_quantity'])
        existing_status = existing['status']
 
        # New values
        new_price = product_payload['variants'][0]['price']
        new_inventory_quantity = parse_inventory_quantity(product_payload['variants'][0]['inventory_quantity'])
        new_status = product_payload['status']
 
        # Check for price change (with rounding)
        if round(float(existing_price), 2) != round(float(new_price), 2):
            needs_update = True
 
        # Check for inventory change
        if existing_inventory_quantity != new_inventory_quantity:
            needs_update = True
 
        # Check for status change
        if existing_status != new_status:
            needs_update = True
 
        if needs_update:
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
            response = shopify_put(url, {"product": product_payload})
 
            if response:
                logging.info(f"🔄 Updated: {product_payload['title']} (ID: {existing['id']})")
                counters['updated'] += 1
                resync_quantity_counter += new_inventory_quantity
            else:
                logging.error(f"❌ Failed to update: {product_payload['title']}")
        else:
            counters['no_updates_needed'] += 1  # Increment the counter for no updates
    else:
        # Handle creation of new products
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        response = shopify_post(url, {"product": product_payload})
 
        if response:
            logging.info(f"➕ Created: {product_payload['title']} (ID: {response['product']['id']})")
            counters['created'] += 1
        else:
            logging.error(f"❌ Failed to create: {product_payload['title']}")
 
# -----------------------------
# CHECK FOR STOCK CHANGES
# -----------------------------
def check_for_changes(counters, resync_quantity_counter):
    """Run in a background thread to check for stock changes."""
    while True:
        time.sleep(900)  # Check every 15 minutes
        # Logic to check for stock changes
        # This is a placeholder; you would need to implement the actual API call to get updated products.
        updated_products = []  # Assume this is filled with updated products
        for product in updated_products:
            sync_product(product, counters, resync_quantity_counter)  # Re-sync the updated product
            # Log the total quantity resynced
            logging.info(f"Total Quantity Resynced: {resync_quantity_counter}")
 
# -----------------------------
# RUN SYNC
# -----------------------------
def run_sync():
    """Main function to run the sync process."""
    logging.info("🚀 START SYNC")
 
    # Refresh the access token at the start
    token = get_access_token()
    HEADERS["X-Shopify-Access-Token"] = token  # Update headers with the new token
 
    items = load_xml()
    counters = {'updated': 0, 'created': 0, 'no_updates_needed': 0}  # Add new counter
    resync_quantity_counter = 0  # Initialize resync quantity counter
 
    # Start the background thread for checking stock changes
    change_checker_thread = threading.Thread(target=check_for_changes, args=(counters, resync_quantity_counter))
    change_checker_thread.daemon = True
    change_checker_thread.start()
 
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(sync_product, p, counters, resync_quantity_counter): p for p in items}
 
        for future in as_completed(futures):
            p = futures[future]
            try:
                future.result()
            except Exception as e:
                logging.error(f"❌ Error syncing product {p.get('title')}: {e}")
 
    logging.info(f"✅ DONE: Total Updated: {counters['updated']}, Total Created: {counters['created']}, Total No Updates Needed: {counters['no_updates_needed']}, Total Quantity Resynced: {resync_quantity_counter}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_sync()
