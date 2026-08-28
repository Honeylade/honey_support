import os
import requests
import xml.etree.ElementTree as ET
import re
import html
import time
import threading
from typing import Optional, Dict, List, Any
 
# ============================================================
# CONFIG
# ============================================================
 
SHOP_URL = os.getenv("SHOP_URL", "").strip()
XML_URL = os.getenv("XML_URL", "").strip()
 
# Use a current Shopify Admin GraphQL API version supported
# by your app.
API_VERSION = os.getenv("API_VERSION", "2026-07").strip()
 
CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip()
 
# Optional:
# If blank, the script will automatically use the only active
# Shopify location. If multiple active locations exist, it will
# print them and stop so you can choose one.
LOCATION_ID = os.getenv("LOCATION_ID", "").strip()
 
# Test with e.g. LIMIT=5.
# Set LIMIT=0 or LIMIT=None for all products.
LIMIT_RAW = os.getenv("LIMIT", "").strip()
 
if LIMIT_RAW in ("", "0", "None", "none", "NONE"):
    LIMIT = None
else:
    try:
        LIMIT = int(LIMIT_RAW)
    except ValueError:
        raise RuntimeError(f"Invalid LIMIT value: {LIMIT_RAW}")
 
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "2"))
 
# Start with 1 while testing.
# Increase to 2 after confirming everything works.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "1"))
 
# ============================================================
# TAGS
# ============================================================
 
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
# ACCESS TOKEN CACHE
# ============================================================
 
_token_cache = {
    "access_token": None,
    "expires_at": 0,
}
 
_token_lock = threading.Lock()
 
# ============================================================
# VALIDATE CONFIGURATION
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
            "Missing required environment variables: " + ", ".join(missing)
        )
 
# ============================================================
# SHOPIFY ACCESS TOKEN
# ============================================================
 
def get_access_token():
    with _token_lock:
        if (
            _token_cache["access_token"]
            and time.time() < _token_cache["expires_at"] - 60
        ):
            return _token_cache["access_token"]
 
        url = f"https://{SHOP_URL}/admin/oauth/access_token"
        payload = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        }
 
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
 
            if not response.ok:
                raise RuntimeError(
                    "Shopify access-token request failed "
                    f"({response.status_code}): {response.text[:500]}"
                )
 
            token_data = response.json()
 
        except requests.RequestException as exc:
            raise RuntimeError(f"Unable to obtain Shopify access token: {exc}")
 
        access_token = token_data.get("access_token")
 
        if not access_token:
            raise RuntimeError("Shopify did not return an access token.")
 
        expires_in = int(token_data.get("expires_in", 86400))
        _token_cache["access_token"] = access_token
        _token_cache["expires_at"] = time.time() + expires_in
 
        print("🔐 Shopify access token obtained.")
        return access_token
 
# ============================================================
# GRAPHQL
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
 
            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------
 
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else RETRY_DELAY * attempt
                print(f"⚠️ Shopify rate limit. Waiting {wait:.1f}s...")
                time.sleep(wait)
                continue
 
            # ------------------------------------------------
            # TEMPORARY SERVER ERRORS
            # ------------------------------------------------
 
            if response.status_code in (500, 502, 503, 504):
                wait = RETRY_DELAY * attempt
                print(f"⚠️ Shopify HTTP {response.status_code}. Retrying in {wait:.1f}s...")
                time.sleep(wait)
                continue
 
            response.raise_for_status()
            result = response.json()
 
            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------
 
            if result.get("errors"):
                raise RuntimeError("GraphQL errors: " + str(result["errors"]))
 
            return result.get("data", {})
        
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY * attempt
                print(f"⚠️ Shopify request failed: {exc}")
                time.sleep(wait)
            else:
                break
 
    raise RuntimeError("Shopify GraphQL request failed: " + str(last_error))
 
# ============================================================
# GENERAL HELPERS
# ============================================================
 
def clean_text(value) -> str:
    if value is None:
        return ""
    return html.unescape(str(value).strip())
 
def last_value(value) -> str:
    value = clean_text(value)
    if not value:
        return ""
    return value.split(">")[-1].strip()
 
def split_tags(value) -> List[str]:
    value = clean_text(value)
    if not value:
        return []
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
    paragraph = f"<p>{standard_description}</p>" if standard_description else ""
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
    return any(ext in url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))
 
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
# PRICE LOGIC
# ============================================================
 
def calc_price(cost, weight):
    cost = to_float(cost)
    weight = to_float(weight)
    shipping = 3.99 if weight < 300 else 4.99 if weight < 2000 else 18.00
 
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
# XML
# ============================================================
 
def load_xml() -> List[Dict[str, Any]]:
    print("📥 Downloading XML feed...")
    response = requests.get(XML_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = root.findall(".//post")
    print(f"🔎 Found {len(items)} supplier items")
 
    selected_items = items[:LIMIT] if LIMIT is not None else items
    products = []
 
    for item in selected_items:
        data = {}
        for child in item:
            data[child.tag.lower()] = clean_text(child.text)
        products.append(data)
 
    print(f"📦 Selected {len(products)} products for sync")
    return products
 
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
 
def get_locations():
    data = graphql(LOCATIONS_QUERY)
    return data["locations"]["nodes"]
 
def resolve_location_id():
    locations = get_locations()
 
    if not locations:
        raise RuntimeError("❌ No Shopify locations were returned.")
 
    print("\n📍 Shopify locations:")
    for location in locations:
        print(f"   {location['name']} | Active: {location['isActive']} | Online: {location['fulfillsOnlineOrders']} | ID: {location['id']}")
 
    active_locations = [location for location in locations if location.get("isActive")]
    if not active_locations:
        raise RuntimeError("❌ No active Shopify locations found.")
 
    # --------------------------------------------------------
    # Explicit LOCATION_ID
    # --------------------------------------------------------
 
    if LOCATION_ID:
        for location in active_locations:
            if location["id"] == LOCATION_ID:
                print(f"\n📍 Using configured location: {location['name']}")
                print(f"   Location ID: {location['id']}")
                return location["id"]
 
        raise RuntimeError("\n❌ LOCATION_ID was not found among the active Shopify locations.\nConfigured value: {LOCATION_ID}")
 
    # --------------------------------------------------------
    # One active location
    # --------------------------------------------------------
 
    if len(active_locations) == 1:
        location = active_locations[0]
        print(f"\n📍 Automatically using Shopify location: {location['name']}")
        print(f"   Location ID: {location['id']}")
        return location["id"]
 
    # --------------------------------------------------------
    # Prefer online-fulfilling location
    # --------------------------------------------------------
 
    online_locations = [location for location in active_locations if location.get("fulfillsOnlineOrders")]
    if len(online_locations) == 1:
        location = online_locations[0]
        print(f"\n📍 Automatically using online fulfillment location: {location['name']}")
        print(f"   Location ID: {location['id']}")
        return location["id"]
 
    # --------------------------------------------------------
    # Multiple locations
    # --------------------------------------------------------
 
    raise RuntimeError("\n❌ Multiple active Shopify locations found.\nSet LOCATION_ID to the location where supplier inventory should be managed.")
 
# ============================================================
# PRODUCT QUERY
# ============================================================
 
PRODUCT_QUERY = """
query ProductSearch(
    $query: String!
    $locationId: ID!
) {
    products(
        first: 10
        query: $query
    ) {
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
                    selectedOptions {
                        name
                        value
                    }
                    price
                    inventoryItem {
                        id
                        tracked
                        inventoryLevel(locationId: $locationId) {
                            id
                            quantities(names: ["available"]) {
                                name
                                quantity
                            }
                        }
                    }
                }
            }
        }
    }
}
"""
 
def search_products(query_string: str, location_id: str):
    data = graphql(PRODUCT_QUERY, {
        "query": query_string,
        "locationId": location_id,
    })
    return data["products"]["nodes"]
 
# ============================================================
# FIND PRODUCT
# ============================================================
 
def find_product(handle: str, sku: Optional[str], barcode: Optional[str], location_id: str):
    # --------------------------------------------------------
    # 1. HANDLE
    # --------------------------------------------------------
 
    if handle:
        products = search_products(f"handle:{handle}", location_id)
        if products:
            return products[0]
 
    # --------------------------------------------------------
    # 2. SKU
    # --------------------------------------------------------
 
    if sku:
        products = search_products(f"sku:{sku}", location_id)
        for product in products:
            for variant in product["variants"]["nodes"]:
                if variant.get("sku") and str(variant["sku"]) == str(sku):
                    return product
 
    # --------------------------------------------------------
    # 3. BARCODE
    # --------------------------------------------------------
 
    if barcode:
        products = search_products(f"barcode:{barcode}", location_id)
        for product in products:
            for variant in product["variants"]["nodes"]:
                if variant.get("barcode") and str(variant["barcode"]) == str(barcode):
                    return product
 
    return None
 
# ============================================================
# BUILD SOURCE PRODUCT
# ============================================================
 
def build_source_product(p: Dict[str, Any]):
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
# PRODUCT UPDATE
# ============================================================
 
PRODUCT_UPDATE_MUTATION = """
mutation ProductUpdate(
    $input: ProductInput!
) {
    productUpdate(
        product: $input
    ) {
        product {
            id
            title
            handle
            status
        }
        userErrors {
            field
            message
            code
        }
    }
}
"""
 
def update_product_details(product, source):
    input_data = {
        "id": product["id"],
        "title": source["title"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "tags": source["tags"],
        "status": "ACTIVE" if source["stock"] > 0 else "DRAFT",
    }
 
    # Only change handle when necessary.
    # This avoids unnecessary handle changes.
    if product.get("handle") != source["handle"]:
        input_data["handle"] = source["handle"]
 
    data = graphql(PRODUCT_UPDATE_MUTATION, {"input": input_data})
    result = data["productUpdate"]
 
    errors = result.get("userErrors") or []
    if errors:
        raise RuntimeError("Product update errors: " + str(errors))
 
    return result["product"]
 
# ============================================================
# VARIANT UPDATE
# ============================================================
 
VARIANTS_BULK_UPDATE_MUTATION = """
mutation ProductVariantsBulkUpdate(
    $productId: ID!
    $variants: [ProductVariantsBulkInput!]!
) {
    productVariantsBulkUpdate(
        productId: $productId
        variants: $variants
    ) {
        product {
            id
            title
        }
        productVariants {
            id
            title
            sku
            barcode
            price
            inventoryItem {
                id
                tracked
            }
        }
        userErrors {
            field
            message
            code
        }
    }
}
"""
 
def find_matching_variant(product, source):
    variants = product["variants"]["nodes"]
    sku = source["sku"]
    barcode = source["barcode"]
 
    # --------------------------------------------------------
    # SKU
    # --------------------------------------------------------
 
    if sku:
        for variant in variants:
            if variant.get("sku") and str(variant["sku"]) == str(sku):
                return variant
 
    # --------------------------------------------------------
    # Barcode
    # --------------------------------------------------------
 
    if barcode:
        for variant in variants:
            if variant.get("barcode") and str(variant["barcode"]) == str(barcode):
                return variant
 
    # --------------------------------------------------------
    # Single variant product
    # --------------------------------------------------------
 
    if len(variants) == 1:
        return variants[0]
 
    return None
 
def update_variant(product, variant, source):
    variant_input = {
        "id": variant["id"],
        "price": str(source["price"]),
    }
 
    # --------------------------------------------------------
    # SKU
    # --------------------------------------------------------
 
    if source["sku"]:
        variant_input["inventoryItem"] = {"sku": source["sku"]}
 
    # --------------------------------------------------------
    # Barcode
    # --------------------------------------------------------
 
    if source["barcode"]:
        variant_input["barcode"] = source["barcode"]
 
    # --------------------------------------------------------
    # Weight
    # --------------------------------------------------------
 
    variant_input["inventoryItem"] = {
        **variant_input.get("inventoryItem", {}),
        "measurement": {
            "weight": {
                "value": source["weight"],
                "unit": "GRAMS",
            }
        },
    }
 
    data = graphql(VARIANTS_BULK_UPDATE_MUTATION, {
        "productId": product["id"],
        "variants": [variant_input],
    })
 
    result = data["productVariantsBulkUpdate"]
 
    errors = result.get("userErrors") or []
    if errors:
        raise RuntimeError("Variant update errors: " + str(errors))
 
    return result["productVariants"]
 
# ============================================================
# INVENTORY SET
# ============================================================
 
INVENTORY_SET_MUTATION = """
mutation InventorySetQuantities(
    $input: InventorySetQuantitiesInput!
) {
    inventorySetQuantities(
        input: $input
    ) {
        inventoryAdjustmentGroup {
            createdAt
            reason
            changes {
                name
                delta
                item {
                    id
                }
            }
        }
        userErrors {
            field
            message
            code
        }
    }
}
"""
 
def set_inventory_quantity(inventory_item_id: str, location_id: str, quantity: int):
    quantity = max(0, int(quantity))
    input_data = {
        "name": "available",
        "reason": "correction",
        "ignoreCompareQuantity": True,
        "quantities": [{
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
            "quantity": quantity,
        }],
    }
 
    data = graphql(INVENTORY_SET_MUTATION, {"input": input_data})
    result = data["inventorySetQuantities"]
 
    errors = result.get("userErrors") or []
    if errors:
        raise RuntimeError("Inventory update errors: " + str(errors))
 
    return result["inventoryAdjustmentGroup"]
 
# ============================================================
# ACTIVATE INVENTORY AT LOCATION
# ============================================================
 
INVENTORY_ACTIVATE_MUTATION = """
mutation InventoryActivate(
    $inventoryItemId: ID!
    $locationId: ID!
    $available: Int
) {
    inventoryActivate(
        inventoryItemId: $inventoryItemId
        locationId: $locationId
        available: $available
    ) {
        inventoryLevel {
            id
        }
        userErrors {
            field
            message
            code
        }
    }
}
"""
 
def activate_inventory(inventory_item_id, location_id, quantity):
    data = graphql(INVENTORY_ACTIVATE_MUTATION, {
        "inventoryItemId": inventory_item_id,
        "locationId": location_id,
        "available": max(0, int(quantity)),
    })
 
    result = data["inventoryActivate"]
    errors = result.get("userErrors") or []
 
    if errors:
        # If already connected to the location,
        # Shopify may return an error. We handle this
        # by checking the inventory level afterwards.
        error_text = str(errors).lower()
        if "already" not in error_text and "exists" not in error_text and "active" not in error_text:
            raise RuntimeError("Inventory activation errors: " + str(errors))
 
    return result.get("inventoryLevel")
 
# ============================================================
# INVENTORY VERIFICATION
# ============================================================
 
INVENTORY_CHECK_QUERY = """
query InventoryCheck(
    $inventoryItemId: ID!
    $locationId: ID!
) {
    inventoryItem(id: $inventoryItemId) {
        id
        tracked
        inventoryLevel(locationId: $locationId) {
            id
            quantities(names: ["available"]) {
                name
                quantity
            }
        }
    }
}
"""
 
def get_inventory_quantity(inventory_item_id, location_id):
    data = graphql(INVENTORY_CHECK_QUERY, {
        "inventoryItemId": inventory_item_id,
        "locationId": location_id,
    })
 
    item = data.get("inventoryItem")
    if not item:
        return None
 
    level = item.get("inventoryLevel")
    if not level:
        return None
 
    quantities = level.get("quantities") or []
    for quantity in quantities:
        if quantity.get("name") == "available":
            return quantity.get("quantity")
 
    return None
 
# ============================================================
# NEW PRODUCT CREATION
# ============================================================
 
PRODUCT_CREATE_MUTATION = """
mutation ProductCreate(
    $input: ProductInput!
) {
    productCreate(input: $input) {
        product {
            id
            title
            handle
            variants(first: 250) {
                nodes {
                    id
                    sku
                    barcode
                    title
                    inventoryItem {
                        id
                        tracked
                    }
                }
            }
        }
        userErrors {
            field
            message
            code
        }
    }
}
"""
 
def create_product(source):
    # --------------------------------------------------------
    # IMPORTANT:
    #
    # We create the product with ONE default variant.
    # We do NOT use productSet.
    #
    # This avoids the "productOptions input is required"
    # error that you encountered.
    # --------------------------------------------------------
 
    input_data = {
        "title": source["title"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "tags": source["tags"],
        "status": "ACTIVE" if source["stock"] > 0 else "DRAFT",
    }
 
    data = graphql(PRODUCT_CREATE_MUTATION, {"input": input_data})
    result = data["productCreate"]
 
    errors = result.get("userErrors") or []
    if errors:
        raise RuntimeError("Product creation errors: " + str(errors))
 
    return result["product"]
 
# ============================================================
# GET PRODUCT AFTER CREATION
# ============================================================
 
def get_product_by_id(product_id, location_id):
    data = graphql(PRODUCT_QUERY, {
        "query": f"id:{product_id.split('/')[-1]}",
        "locationId": location_id,
    })
 
    products = data["products"]["nodes"]
    for product in products:
        if product["id"] == product_id:
            return product
 
    return None
 
# ============================================================
# SYNC EXISTING PRODUCT
# ============================================================
 
def sync_existing_product(product, source, location_id):
    # --------------------------------------------------------
    # PRODUCT DETAILS
    # --------------------------------------------------------
 
    update_product_details(product, source)
    print("   ✅ Product details updated")
 
    # --------------------------------------------------------
    # FIND VARIANT
    # --------------------------------------------------------
 
    variant = find_matching_variant(product, source)
    if not variant:
        raise RuntimeError(
            "Could not match supplier item "
            "to a Shopify variant. "
            f"SKU={source['sku']}, "
            f"Barcode={source['barcode']}"
        )
 
    print(f"   🔎 Variant: {variant.get('title')}")
 
    # --------------------------------------------------------
    # UPDATE VARIANT
    # --------------------------------------------------------
 
    updated_variants = update_variant(product, variant, source)
    print("   ✅ Variant price/SKU/barcode/weight updated")
 
    # Find returned inventory item ID.
    updated_variant = None
    for v in updated_variants:
        if v["id"] == variant["id"]:
            updated_variant = v
            break
 
    if not updated_variant:
        updated_variant = variant
 
    inventory_item_id = updated_variant["inventoryItem"]["id"]
 
    # --------------------------------------------------------
    # TRACK INVENTORY
    # --------------------------------------------------------
 
    tracked = updated_variant["inventoryItem"].get("tracked")
    if tracked is False:
        print("   ℹ️ Inventory is not tracked. Attempting inventory update.")
 
    # --------------------------------------------------------
    # CHECK LOCATION
    # --------------------------------------------------------
 
    current_quantity = get_inventory_quantity(inventory_item_id, location_id)
 
    if current_quantity is None:
        print("   📍 Inventory item is not connected to this location.")
        print("   🔗 Activating inventory at location...")
 
        activate_inventory(inventory_item_id, location_id, source["stock"])
        time.sleep(0.5)  # Give Shopify a moment to establish the inventory level before setting it.
 
    else:
        print(f"   📦 Current Shopify stock: {current_quantity}")
 
    # --------------------------------------------------------
    # SET INVENTORY
    # --------------------------------------------------------
 
    print(f"   📦 Setting Shopify stock to {source['stock']}")
    set_inventory_quantity(inventory_item_id, location_id, source["stock"])
 
    # --------------------------------------------------------
    # VERIFY
    # --------------------------------------------------------
 
    verified_quantity = get_inventory_quantity(inventory_item_id, location_id)
 
    if verified_quantity is None:
        raise RuntimeError(
            "Inventory update was submitted "
            "but Shopify returned no inventory "
            "level for the selected location."
        )
 
    if int(verified_quantity) != int(source["stock"]):
        raise RuntimeError(
            "Inventory verification failed. "
            f"Expected {source['stock']}, "
            f"Shopify reports {verified_quantity}."
        )
 
    print(f"   ✅ Inventory verified: {verified_quantity}")
    return True
 
# ============================================================
# SYNC NEW PRODUCT
# ============================================================
 
def sync_new_product(source, location_id):
    print("   🆕 Creating Shopify product...")
    product = create_product(source)
    print(f"   ✅ Created Shopify product: {product['id']}")
 
    # --------------------------------------------------------
    # Shopify creates a default variant.
    # We now update that variant with the supplier SKU/barcode/price/weight.
    # --------------------------------------------------------
 
    variants = product["variants"]["nodes"]
    if not variants:
        raise RuntimeError("Shopify created the product but returned no variants.")
    
    variant = variants[0]
    updated_variants = update_variant(product, variant, source)
    print("   ✅ Initial variant configured")
 
    updated_variant = updated_variants[0] if updated_variants else variant
    inventory_item_id = updated_variant["inventoryItem"]["id"]
 
    # --------------------------------------------------------
    # Activate inventory at location.
    # --------------------------------------------------------
 
    activate_inventory(inventory_item_id, location_id, source["stock"])
    time.sleep(0.5)
 
    # --------------------------------------------------------
    # Set stock.
    # --------------------------------------------------------
 
    set_inventory_quantity(inventory_item_id, location_id, source["stock"])
 
    # --------------------------------------------------------
    # Verify.
    # --------------------------------------------------------
 
    verified_quantity = get_inventory_quantity(inventory_item_id, location_id)
 
    if verified_quantity is None or int(verified_quantity) != int(source["stock"]):
        raise RuntimeError(
            "New product inventory verification failed. "
            f"Expected {source['stock']}, "
            f"Shopify reports {verified_quantity}."
        )
 
    print(f"   ✅ New product inventory verified: {verified_quantity}")
    return True
 
# ============================================================
# SYNC ONE PRODUCT
# ============================================================
 
def sync_product(source_item, location_id):
    source = build_source_product(source_item)
    title = source["title"]
 
    print("\n" + "=" * 70)
    print(f"📦 {title}")
    print(f"   SKU: {source['sku']}")
    print(f"   Barcode: {source['barcode']}")
    print(f"   Cost: £{source['cost']:.2f}")
    print(f"   Price: £{source['price']:.2f}")
    print(f"   Weight: {source['weight']}g")
    print(f"   Stock: {source['stock']}")
 
    # --------------------------------------------------------
    # FIND EXISTING PRODUCT
    # --------------------------------------------------------
 
    existing = find_product(source["handle"], source["sku"], source["barcode"], location_id)
 
    # --------------------------------------------------------
    # EXISTING
    # --------------------------------------------------------
 
    if existing:
        print(f"🔄 Existing product found: {existing['title']}")
        print(f"   Shopify ID: {existing['id']}")
        sync_existing_product(existing, source, location_id)
        return "updated"
 
    # --------------------------------------------------------
    # NEW
    # --------------------------------------------------------
 
    print("🆕 Product not found.")
    sync_new_product(source, location_id)
    return "created"
 
# ============================================================
# MAIN SYNC
# ============================================================
 
def run_sync():
    print("\n" + "=" * 70)
    print("🚀 SHOPIFY SUPPLIER SYNC")
    print("=" * 70)
 
    validate_config()
    print(f"🏪 Shop: {SHOP_URL}")
    print(f"🔗 API: {API_VERSION}")
    print(f"⚙️ Workers: {MAX_WORKERS}")
 
    if LIMIT is None:
        print("📦 Limit: ALL PRODUCTS")
    else:
        print(f"📦 Limit: {LIMIT}")
 
    # --------------------------------------------------------
    # AUTHENTICATION
    # --------------------------------------------------------
 
    get_access_token()
 
    # --------------------------------------------------------
    # LOCATION
    # --------------------------------------------------------
 
    location_id = resolve_location_id()
 
    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------
 
    items = load_xml()
 
    if not items:
        print("⚠️ XML feed contains no products.")
        return
 
    created = 0
    updated = 0
    failed = 0
    total = len(items)
 
    print("\n" + "=" * 70)
    print(f"🚀 Starting sync of {total} products...")
    print("=" * 70)
 
    # --------------------------------------------------------
    # THREADING
    # --------------------------------------------------------
 
    # For inventory synchronization, sequential operation
    # is safer and easier to troubleshoot.
    #
    # If MAX_WORKERS > 1, use a ThreadPoolExecutor.
    # --------------------------------------------------------
 
    if MAX_WORKERS <= 1:
        for index, item in enumerate(items, start=1):
            print(f"\n[{index}/{total}]")
            try:
                result = sync_product(item, location_id)
                if result == "created":
                    created += 1
                elif result == "updated":
                    updated += 1
            except Exception as exc:
                failed += 1
                title = clean_text(item.get("title"))
                print(f"❌ ERROR syncing {title}: {exc}")
 
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(sync_product, item, location_id): item for item in items}
            completed = 0
 
            for future in as_completed(futures):
                item = futures[future]
                completed += 1
 
                try:
                    result = future.result()
                    if result == "created":
                        created += 1
                    elif result == "updated":
                        updated += 1
                except Exception as exc:
                    failed += 1
                    title = clean_text(item.get("title"))
                    print(f"❌ ERROR syncing {title}: {exc}")
 
                print(f"📊 Progress: {completed}/{total}")
 
    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------
 
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
