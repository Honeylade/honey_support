import os
import requests
import xml.etree.ElementTree as ET
import re
import html
import time
import uuid
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
 
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
 
# ============================================================
# PRICE LOGIC
# ============================================================
 
def calc_price(cost, weight):
    cost = float(cost or 0)
    weight = float(weight or 0)
 
    shipping = (
        3.99 if weight < 300
        else 4.99 if weight < 2000
        else 18.00
    )
 
    if cost < 5:
        margin = 0.30
    elif cost < 10:
        margin = 0.25
    else:
        margin = 0.20
 
    TAX = 0.20
    FEES = 0.029 + 0.09
    FIXED_COSTS = 0.30 + 0.50
 
    base_price = cost * (1 + margin)
    taxed_price = base_price * (1 + TAX)
    price_after_fees = taxed_price / (1 - FEES)
    final_price = price_after_fees + shipping + FIXED_COSTS
 
    return round(final_price, 2)
 
# ============================================================
# GENERAL HELPERS
# ============================================================
 
def clean_text(value) -> str:
    if value is None:
        return ""
    return html.unescape(str(value).strip())
 
def last_value(value) -> str:
    value = clean_text(value)
    return value.split(">")[-1].strip() if value else ""
 
def split_tags(value) -> List[str]:
    value = clean_text(value)
    return [t.strip() for t in re.split(r"[>\|,;/\s]+", value) if t.strip()]
 
def sanitize_tags(tags: List[str]) -> List[str]:
    sanitized = []
    seen = set()
    for tag in tags:
        if not tag:
            continue
        tag = html.unescape(str(tag)).replace("&", "and").strip()
        if len(tag) > 255:
            tag = tag[:255]
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            sanitized.append(tag)
    return sanitized
 
def build_description(product: Dict[str, Any]) -> str:
    bullets = []
    for i in range(1, 11):
        value = clean_text(product.get(f"desc_{i}"))
        if value:
            bullets.append(f"<li>{value}</li>")
    bullet_html = "<ul>" + "".join(bullets) + "</ul>" if bullets else ""
    standard_description = clean_text(product.get("desc_standard"))
    return bullet_html + f"<p>{standard_description}</p>"
 
def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", clean_text(value).lower()).strip("-")
 
def valid_image(url: str) -> bool:
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return False
    return any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"])
 
def to_int(value, default=0) -> int:
    try:
        return int(float(value or default))
    except (ValueError, TypeError):
        return default
 
def to_float(value, default=0.0) -> float:
    try:
        return float(value or default)
    except (ValueError, TypeError):
        return default
 
# ============================================================
# XML
# ============================================================
 
def load_xml() -> List[Dict[str, Any]]:
    print("📥 Downloading XML feed...")
    response = requests.get(XML_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = root.findall(".//post")
    print(f"🔎 Found {len(items)} supplier items")
    
    products = []
    selected_items = items[:LIMIT] if LIMIT else items
    for item in selected_items:
        data = {child.tag.lower(): clean_text(child.text) for child in item}
        products.append(data)
    return products
 
# ============================================================
# SHOPIFY GRAPHQL
# ============================================================
 
def graphql(query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    token = get_access_token()  # Get the access token
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"
    headers = {**HEADERS, "X-Shopify-Access-Token": token}
    payload = {"query": query, "variables": variables or {}}
    
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code in (429, 500, 502, 503, 504):
                print(f"⚠️ Shopify HTTP {response.status_code}; retry {attempt}/{MAX_RETRIES}")
                time.sleep(RETRY_DELAY * attempt)
                continue
            response.raise_for_status()
            result = response.json()
            if result.get("errors"):
                raise RuntimeError("GraphQL errors: " + str(result["errors"]))
            return result.get("data", {})
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                print(f"⚠️ Shopify request failed: {exc}; retry {attempt}/{MAX_RETRIES}")
                time.sleep(RETRY_DELAY * attempt)
            else:
                break
    raise RuntimeError(f"Shopify GraphQL request failed: {last_error}")
 
# ============================================================
# SHOPIFY LOCATIONS
# ============================================================
 
def get_locations() -> List[Dict[str, Any]]:
    query = """
    query GetLocations {
        locations(first: 250) {
            nodes {
                id
                name
                isActive
                fulfillsOnlineOrders
            }
        }
    }
    """
    data = graphql(query)
    return data["locations"]["nodes"]
 
def resolve_location_id() -> str:
    locations = get_locations()
    if not locations:
        raise RuntimeError("❌ No active Shopify locations found.")
    
    if LOCATION_ID:
        for location in locations:
            if location["id"] == LOCATION_ID:
                print(f"📍 Inventory location: {location['name']} ({location['id']})")
                return LOCATION_ID
        raise RuntimeError(f"❌ LOCATION_ID was not found: {LOCATION_ID}")
 
    if len(locations) == 1:
        location = locations[0]
        print(f"📍 Automatically using Shopify location: {location['name']} ({location['id']})")
        return location["id"]
 
    print("\n❌ Multiple Shopify locations found. Set LOCATION_ID in the CONFIG section before running the sync:\n")
    for location in locations:
        print(f"  {location['name']}: {location['id']}")
    raise RuntimeError("Multiple inventory locations exist. Set LOCATION_ID.")
 
# ============================================================
# FIND PRODUCT
# ============================================================
 
PRODUCT_QUERY = """
query ProductByHandle($query: String!) {
    products(first: 1, query: $query) {
        nodes {
            id
            title
            handle
            status
            variants(first: 250) {
                nodes {
                    id
                    title
                    sku
                    barcode
                    inventoryItem {
                        id
                        tracked
                    }
                }
            }
        }
    }
}
"""
 
def find_product_by_handle(handle: str) -> Optional[Dict[str, Any]]:
    data = graphql(PRODUCT_QUERY, {"query": f"handle:{handle}"})
    products = data["products"]["nodes"]
    return products[0] if products else None
 
# ============================================================
# BUILD SOURCE PRODUCT
# ============================================================
 
def build_source_product(p: Dict[str, Any]) -> Dict[str, Any]:
    title = clean_text(p.get("title")) or f"Product-{clean_text(p.get('sku'))}"
    handle = slugify(title)
    cost = to_float(p.get("costprice"))
    weight = to_float(p.get("weight"))
    stock_qty = max(0, to_int(p.get("stock")))
    barcode = clean_text(p.get("barcode")) or None
    sku = clean_text(p.get("sku")) or None
    price = calc_price(cost, weight)
    vendor = last_value(p.get("productbrand"))
    product_type = last_value(p.get("productrange"))
    tags = sanitize_tags(split_tags(p.get("productbrand")) + split_tags(p.get("productrange")) + TAGS_TO_INCLUDE + split_tags(title))
    description = build_description(p)
    raw_images = clean_text(p.get("imageoffloads"))
    images = [img.strip() for img in re.split(r"[|,]+", raw_images) if valid_image(img)]
    sizes = [s.strip() for s in re.split(r"[|,]+", clean_text(p.get("sizeattribute"))) if s.strip()]
 
    return {
        "title": title,
        "handle": handle,
        "cost": cost,
        "weight": weight,
        "stock": stock_qty,
        "barcode": barcode,
        "sku": sku,
        "price": price,
        "vendor": vendor,
        "product_type": product_type,
        "tags": tags,
        "description": description,
        "images": images,
        "sizes": sizes,
    }
 
# ============================================================
# SYNC ONE PRODUCT
# ============================================================
 
def sync_product(source_item: Dict[str, Any], location_id: str) -> str:
    source = build_source_product(source_item)
    title = source["title"]
    handle = source["handle"]
 
    print("\n" + "=" * 70)
    print(f"📦 {title}")
    print(f"   SKU: {source['sku']}")
    print(f"   Barcode: {source['barcode']}")
    print(f"   Cost: £{source['cost']:.2f}")
    print(f"   Price: £{source['price']:.2f}")
    print(f"   Weight: {source['weight']}g")
    print(f"   Stock: {source['stock']}")
 
    existing = find_product_by_handle(handle)
 
    if existing:
        print(f"🔄 Existing product found: {existing['title']}")
        print(f"   Shopify ID: {existing['id']}")
        # Update existing product logic here
        # Call product_set function
        return "updated"
    
    print("🆕 Product not found - creating...")
    # Create new product logic here
    # Call product_set function
    return "created"
 
# ============================================================
# MAIN SYNC
# ============================================================
 
def run_sync():
    print("\n" + "=" * 70)
    print("🚀 SHOPIFY SUPPLIER SYNC")
    print("=" * 70)
 
    if not SHOP_URL:
        raise RuntimeError("SHOP_URL is not configured.")
    if not ACCESS_TOKEN:
        raise RuntimeError("ACCESS_TOKEN is not configured.")
 
    location_id = resolve_location_id()
    items = load_xml()
 
    if not items:
        print("⚠️ XML feed contains no products.")
        return
 
    created = 0
    updated = 0
    failed = 0
    total = len(items)
 
    print(f"\n🚀 Starting sync of {total} products...")
 
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_item = {executor.submit(sync_product, item, location_id): item for item in items}
        
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            try:
                result = future.result()
                if result == "created":
                    created += 1
                elif result == "updated":
                    updated += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                title = clean_text(item.get("title"))
                print(f"❌ ERROR syncing {title}: {exc}")
 
    print("\n" + "=" * 70)
    print("✅ SYNC COMPLETE")
    print("=" * 70)
    print(f"🆕 Created: {created}")
    print(f"🔄 Updated: {updated}")
    print(f"❌ Failed:  {failed}")
    print(f"📦 Total:   {total}")
    print("=" * 70)
 
# ============================================================
# RUN
# ============================================================
 
if __name__ == "__main__":
    try:
        run_sync()
    except KeyboardInterrupt:
        print("\n🛑 Sync cancelled by user.")
    except Exception as exc:
        print("\n❌ SYNC STOPPED:")
        print(str(exc))
