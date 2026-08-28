import os
import requests
import xml.etree.ElementTree as ET
import re
import html
import time
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
 
# ============================================================
# CONFIGURATION
# ============================================================
 
SHOP_URL = os.getenv("SHOP_URL")
XML_URL = os.getenv("XML_URL")
 
# Keep this on a currently supported Shopify Admin API version.
API_VERSION = os.getenv("API_VERSION", "2026-07")
 
# Number of products processed concurrently.
WORKERS = int(os.getenv("WORKERS", "5"))
 
# Retry configuration.
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "2"))
 
# HTTP timeout.
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
 
# Optional product limit.
LIMIT_ENV = os.getenv("LIMIT")
 
if LIMIT_ENV in (None, "", "None", "0"):
    LIMIT = None
else:
    LIMIT = int(LIMIT_ENV)
 
# Shopify inventory location.
# IMPORTANT: Set this to your actual Shopify location GID,
# for example: LOCATION_ID=gid://shopify/Location/123456789
LOCATION_ID = os.getenv("LOCATION_ID")
 
# OAuth credentials for client-credentials access token.
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
 
# Tags added to every product.
TAGS_TO_INCLUDE = [
    "football accessories",
    "football",
    "rugby",
    "entertainment",
    "themed gifts",
    "Honeylade",
    "flags",
    "sports gifts",
    "fan accessories",
    "novelty gifts",
    "sports fans",
]
 
# ============================================================
# VALIDATION
# ============================================================
 
def validate_config():
    missing = []
 
    if not SHOP_URL:
        missing.append("SHOP_URL")
    if not XML_URL:
        missing.append("XML_URL")
    if not CLIENT_ID:
        missing.append("CLIENT_ID")
    if not CLIENT_SECRET:
        missing.append("CLIENT_SECRET")
 
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
        )
 
    print("✅ Configuration validated")
    print(f"   Shopify: {SHOP_URL}")
    print(f"   API: {API_VERSION}")
    print(f"   Workers: {WORKERS}")
    print(f"   Limit: {LIMIT if LIMIT is not None else 'ALL'}")
 
    if LOCATION_ID:
        print(f"   Location: {LOCATION_ID}")
    else:
        print("   Location: AUTO")
 
# ============================================================
# ACCESS TOKEN
# ============================================================
 
_token_cache = {
    "access_token": None,
    "expires_at": 0,
}
 
def get_access_token() -> str:
    """
    Fetch and cache Shopify client-credentials access token.
    """
    if (_token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60):
        return _token_cache["access_token"]
 
    url = f"https://{SHOP_URL}/admin/oauth/access_token"
 
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
 
    response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
 
    if response.status_code >= 400:
        raise RuntimeError(
            f"Shopify token request failed: "
            f"HTTP {response.status_code}: {response.text}"
        )
 
    data = response.json()
    token = data.get("access_token")
 
    if not token:
        raise RuntimeError(f"Shopify did not return an access token: {data}")
 
    expires_in = int(data.get("expires_in", 86400))
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = time.time() + expires_in
 
    return token
 
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
 
        if not tag:
            continue
 
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
    standard = clean_text(product.get("desc_standard"))
    paragraph = f"<p>{standard}</p>"
 
    return bullet_html + paragraph
 
def slugify(value: str) -> str:
    value = clean_text(value)
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
 
def valid_image(url: str) -> bool:
    if not url:
        return False
 
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
 
    if " " in url:
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
    print("\n📥 Downloading XML feed...")
    response = requests.get(XML_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = root.findall(".//post")
 
    print(f"🔎 Found {len(items)} supplier products")
 
    if LIMIT is not None:
        items = items[:LIMIT]
 
    products = []
    for item in items:
        data = {}
        for child in item:
            data[child.tag.lower()] = clean_text(child.text)
        products.append(data)
 
    print(f"📦 Products selected for sync: {len(products)}")
    return products
 
# ============================================================
# GRAPHQL REQUEST
# ============================================================
 
def graphql(query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    token = get_access_token()
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Shopify-Access-Token": token,
    }
 
    payload = {
        "query": query,
        "variables": variables or {},
    }
 
    last_error = None
 
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
 
            # Rate limited / temporary Shopify errors.
            if response.status_code in (429, 500, 502, 503, 504):
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else RETRY_DELAY * attempt
 
                print(f"⚠️ Shopify HTTP {response.status_code}; retrying in {delay:.1f}s ({attempt}/{MAX_RETRIES})")
                time.sleep(delay)
                continue
 
            response.raise_for_status()
            result = response.json()
 
            # GraphQL-level errors.
            if result.get("errors"):
                raise RuntimeError("GraphQL errors: " + str(result["errors"]))
 
            return result.get("data", {})
 
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY * attempt
                print(f"⚠️ Shopify request failed: {exc}; Retry {attempt}/{MAX_RETRIES} in {delay:.1f}s")
                time.sleep(delay)
            else:
                break
 
    raise RuntimeError(f"Shopify GraphQL request failed after {MAX_RETRIES} attempts: {last_error}")
 
# ============================================================
# SHOPIFY LOCATIONS
# ============================================================
 
LOCATIONS_QUERY = """
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
 
def get_locations() -> List[Dict[str, Any]]:
    data = graphql(LOCATIONS_QUERY)
    return data.get("locations", {}).get("nodes", [])
 
def resolve_location_id() -> str:
    locations = get_locations()
    active_locations = [location for location in locations if location.get("isActive")]
 
    if not active_locations:
        raise RuntimeError("❌ No active Shopify locations found.")
 
    print("\n📍 Shopify locations:")
    for location in active_locations:
        print(f"   {location['name']} -> {location['id']} (online orders: {location.get('fulfillsOnlineOrders')})")
 
    # Explicit location supplied.
    if LOCATION_ID:
        for location in active_locations:
            if location["id"] == LOCATION_ID:
                print(f"\n✅ Using configured location: {location['name']}")
                return LOCATION_ID
 
        raise RuntimeError(f"❌ LOCATION_ID was not found: {LOCATION_ID}")
 
    # Automatically use a single location.
    if len(active_locations) == 1:
        location = active_locations[0]
        print(f"\n✅ Automatically using location: {location['name']} ({location['id']})")
        return location["id"]
 
    raise RuntimeError("\n❌ Multiple active Shopify locations exist.\nSet LOCATION_ID to the correct location GID.")
 
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
                        sku
                        tracked
                        measurement {
                            weight {
                                value
                                unit
                            }
                        }
                    }
                }
            }
        }
    }
}
"""
 
def find_product_by_handle(handle: str) -> Optional[Dict[str, Any]]:
    data = graphql(PRODUCT_QUERY, {"query": f"handle:{handle}"})
    products = data.get("products", {}).get("nodes", [])
    return products[0] if products else None
 
# ============================================================
# SOURCE PRODUCT
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
 
    tags = sanitize_tags(
        split_tags(p.get("productbrand")) +
        split_tags(p.get("productrange")) +
        TAGS_TO_INCLUDE +
        split_tags(title)
    )
 
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
# PRODUCT UPDATE
# ============================================================
 
PRODUCT_UPDATE_MUTATION = """
mutation ProductUpdate($product: ProductUpdateInput!) {
    productUpdate(product: $product) {
        product {
            id
            title
            handle
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def update_product_details(product_id: str, source: Dict[str, Any]):
    payload = {
        "id": product_id,
        "title": source["title"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "tags": source["tags"],
        "status": "ACTIVE" if source["stock"] > 0 else "DRAFT",
    }
 
    data = graphql(PRODUCT_UPDATE_MUTATION, {"product": payload})
    result = data["productUpdate"]
    errors = result.get("userErrors", [])
 
    if errors:
        raise RuntimeError("Product update errors: " + str(errors))
 
# ============================================================
# VARIANT UPDATE
# ============================================================
 
VARIANT_BULK_UPDATE_MUTATION = """
mutation ProductVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
    productVariantsBulkUpdate(productId: $productId, variants: $variants, allowPartialUpdates: false) {
        product {
            id
        }
        productVariants {
            id
            price
            barcode
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def update_variant_price_barcode(product_id: str, variant_id: str, price: float, barcode: Optional[str]):
    variant_input = {
        "id": variant_id,
        "price": f"{price:.2f}",
    }
 
    if barcode:
        variant_input["barcode"] = barcode
 
    data = graphql(VARIANT_BULK_UPDATE_MUTATION, {
        "productId": product_id,
        "variants": [variant_input],
    })
 
    result = data["productVariantsBulkUpdate"]
    errors = result.get("userErrors", [])
 
    if errors:
        raise RuntimeError("Variant update errors: " + str(errors))
 
# ============================================================
# INVENTORY ITEM UPDATE
# ============================================================
 
INVENTORY_ITEM_UPDATE_MUTATION = """
mutation InventoryItemUpdate($id: ID!, $input: InventoryItemInput!) {
    inventoryItemUpdate(id: $id, input: $input) {
        inventoryItem {
            id
            sku
            tracked
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def update_inventory_item(inventory_item_id: str, sku: Optional[str], cost: float, weight: float):
    inventory_input = {
        "tracked": True,
        "cost": cost,
        "measurement": {
            "weight": {
                "value": weight,
                "unit": "GRAMS",
            }
        },
    }
 
    if sku:
        inventory_input["sku"] = sku
 
    data = graphql(INVENTORY_ITEM_UPDATE_MUTATION, {
        "id": inventory_item_id,
        "input": inventory_input,
    })
 
    result = data["inventoryItemUpdate"]
    errors = result.get("userErrors", [])
 
    if errors:
        raise RuntimeError("Inventory item update errors: " + str(errors))
 
# ============================================================
# INVENTORY QUANTITY
# ============================================================
 
INVENTORY_SET_MUTATION = """
mutation InventorySet($input: InventorySetQuantitiesInput!) {
    inventorySetQuantities(input: $input) {
        inventoryAdjustmentGroup {
            createdAt
            reason
            changes {
                name
                delta
                quantityAfterChange
            }
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def set_inventory_quantity(inventory_item_id: str, location_id: str, quantity: int):
    input_data = {
        "name": "available",
        "reason": "correction",
        "referenceDocumentUri": "honey-support://supplier-sync",
        "quantities": [{
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
            "quantity": int(quantity),
        }]
    }
 
    data = graphql(INVENTORY_SET_MUTATION, {"input": input_data})
    result = data["inventorySetQuantities"]
    errors = result.get("userErrors", [])
 
    if errors:
        raise RuntimeError("Inventory update errors: " + str(errors))
 
# ============================================================
# MATCH VARIANT
# ============================================================
 
def match_variant(existing_variants: List[Dict[str, Any]], source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sku = source["sku"]
    barcode = source["barcode"]
 
    # 1. SKU match.
    if sku:
        for variant in existing_variants:
            variant_sku = clean_text(variant.get("sku")) or clean_text(variant.get("inventoryItem", {}).get("sku"))
            if variant_sku and variant_sku == sku:
                return variant
 
    # 2. Barcode match.
    if barcode:
        for variant in existing_variants:
            if clean_text(variant.get("barcode")) == barcode:
                return variant
 
    # 3. If there is only one variant, use it as a fallback.
    if len(existing_variants) == 1:
        return existing_variants[0]
 
    return None
 
# ============================================================
# SYNC ONE PRODUCT
# ============================================================
 
def sync_product(index: int, total: int, source_item: Dict[str, Any], location_id: str) -> str:
    source = build_source_product(source_item)
    title = source["title"]
 
    print("\n" + "=" * 70)
    print(f"[{index}/{total}]")
    print("=" * 70)
    print(f"📦 {title}")
    print(f"   SKU: {source['sku']}")
    print(f"   Barcode: {source['barcode']}")
    print(f"   Cost: £{source['cost']:.2f}")
    print(f"   Price: £{source['price']:.2f}")
    print(f"   Weight: {source['weight']}g")
    print(f"   Stock: {source['stock']}")
 
    existing = find_product_by_handle(source["handle"])
 
    if existing:
        print(f"🔄 Existing product found: {existing['title']}")
        print(f"   Shopify ID: {existing['id']}")
 
        # ----------------------------------------------------
        # Product details.
        # ----------------------------------------------------
        update_product_details(existing["id"], source)
 
        # ----------------------------------------------------
        # Find the correct variant.
        # ----------------------------------------------------
        variants = existing.get("variants", {}).get("nodes", [])
        variant = match_variant(variants, source)
 
        if not variant:
            raise RuntimeError("Could not match supplier SKU/barcode to a Shopify variant.")
 
        variant_id = variant["id"]
        inventory_item = variant.get("inventoryItem") or {}
        inventory_item_id = inventory_item.get("id")
 
        if not inventory_item_id:
            raise RuntimeError("Shopify variant has no inventoryItem ID.")
 
        # ----------------------------------------------------
        # Price + barcode.
        # ----------------------------------------------------
        update_variant_price_barcode(existing["id"], variant_id, source["price"], source["barcode"])
 
        # ----------------------------------------------------
        # SKU + cost + weight + tracking.
        # ----------------------------------------------------
        update_inventory_item(inventory_item_id, source["sku"], source["cost"], source["weight"])
 
        # ----------------------------------------------------
        # EXACT INVENTORY QUANTITY.
        # ----------------------------------------------------
        set_inventory_quantity(inventory_item_id, location_id, source["stock"])
 
        print(f"✅ Updated successfully: {title}")
        print(f"   💷 Price: £{source['price']:.2f}")
        print(f"   📦 Stock set to: {source['stock']}")
        return "updated"
 
    # ========================================================
    # PRODUCT DOES NOT EXIST
    # For safety, do not automatically create products here
    # unless you specifically want creation enabled.
    # ========================================================
    print("🆕 Product not found.")
    print("⚠️ Creation is disabled in this version to protect the existing Shopify catalogue.")
    return "missing"
 
# ============================================================
# MAIN SYNC
# ============================================================
 
def run_sync():
    print("\n" + "=" * 70)
    print("🚀 SHOPIFY SUPPLIER SYNC")
    print("=" * 70)
 
    validate_config()
 
    # Get token once before workers start.
    get_access_token()
 
    # Resolve location once.
    location_id = resolve_location_id()
    print(f"\n📍 Inventory location ID:\n   {location_id}")
 
    # Load supplier feed.
    items = load_xml()
    if not items:
        print("⚠️ XML feed contains no products.")
        return
 
    total = len(items)
    print("\n" + "=" * 70)
    print(f"🚀 Starting sync of {total} products...")
    print(f"⚙️ Workers: {WORKERS}")
    print("=" * 70)
 
    created = 0
    updated = 0
    missing = 0
    failed = 0
 
    # ThreadPoolExecutor is used for concurrent Shopify requests.
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_to_index = {}
 
        for index, item in enumerate(items, start=1):
            future = executor.submit(sync_product, index, total, item, location_id)
            future_to_index[future] = (index, item)
 
        completed = 0
 
        for future in as_completed(future_to_index):
            index, item = future_to_index[future]
            completed += 1
 
            try:
                result = future.result()
 
                if result == "created":
                    created += 1
                elif result == "updated":
                    updated += 1
                elif result == "missing":
                    missing += 1
                else:
                    failed += 1
 
            except Exception as exc:
                failed += 1
                title = clean_text(item.get("title"))
                print("\n" + "!" * 70)
                print(f"❌ ERROR syncing {title}")
                print(f"   {exc}")
                print("!" * 70)
 
            # Progress summary every 25 products.
            if completed % 25 == 0 or completed == total:
                print("\n📊 PROGRESS: {completed}/{total} ({completed / total * 100:.1f}%)")
                print(f"   Updated: {updated}")
                print(f"   Created: {created}")
                print(f"   Missing: {missing}")
                print(f"   Failed:  {failed}")
 
    print("\n" + "=" * 70)
    print("✅ SYNC COMPLETE")
    print("=" * 70)
    print(f"🆕 Created: {created}")
    print(f"🔄 Updated: {updated}")
    print(f"⚠️ Missing: {missing}")
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
