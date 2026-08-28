import os
import requests
import xml.etree.ElementTree as ET
import re
import html
import time
import uuid
import random
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
 
# ============================================================
# CONFIG
# ============================================================
 
SHOP_URL = os.getenv("SHOP_URL")
XML_URL = os.getenv("XML_URL")
 
# Current Shopify Admin GraphQL API version.
API_VERSION = os.getenv("API_VERSION", "2026-07")
 
# ------------------------------------------------------------
# TESTING
# ------------------------------------------------------------
# Set LIMIT=10 while testing.
# Set LIMIT=0 or LIMIT=None for all products.
LIMIT_RAW = os.getenv("LIMIT", "0")
 
if LIMIT_RAW in ("", "0", "None", "none", "ALL", "all"):
    LIMIT = None
else:
    LIMIT = int(LIMIT_RAW)
 
# ------------------------------------------------------------
# WORKERS
# ------------------------------------------------------------
# Start with 5 for a 4,938-product catalogue.
# Increase only after confirming the sync is stable.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "5"))
 
# ------------------------------------------------------------
# REQUEST / RETRY SETTINGS
# ------------------------------------------------------------
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "2.0"))
 
# ------------------------------------------------------------
# SHOPIFY AUTH
# ------------------------------------------------------------
# Preferred:
# SHOPIFY_ACCESS_TOKEN=shpat_...
# OR, if using Shopify's client-credentials flow:
# CLIENT_ID=...
# CLIENT_SECRET=...
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
 
# ------------------------------------------------------------
# INVENTORY LOCATION
# ------------------------------------------------------------
# Example:
# LOCATION_ID=gid://shopify/Location/123456789
# If omitted and there is exactly one active location,
# the script will automatically use it.
LOCATION_ID = os.getenv("LOCATION_ID")
 
# ------------------------------------------------------------
# PRODUCT TAGS
# ------------------------------------------------------------
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
 
if not SHOP_URL:
    raise RuntimeError("SHOP_URL environment variable is not configured.")
 
if not XML_URL:
    raise RuntimeError("XML_URL environment variable is not configured.")
 
SHOP_URL = SHOP_URL.strip().replace("https://", "").replace("http://", "").rstrip("/")
 
# ============================================================
# ACCESS TOKEN MANAGEMENT
# ============================================================
 
_token_cache = {
    "access_token": None,
    "expires_at": 0,
}
 
def get_access_token() -> str:
    """
    Return a cached Shopify access token.
 
    If SHOPIFY_ACCESS_TOKEN is supplied, use it directly.
    Otherwise use Shopify client-credentials flow.
    """
    # Direct token takes priority.
    if SHOPIFY_ACCESS_TOKEN:
        return SHOPIFY_ACCESS_TOKEN.strip()
 
    # Cached client-credentials token.
    if (
        _token_cache["access_token"]
        and time.time() < _token_cache["expires_at"] - 60
    ):
        return _token_cache["access_token"]
 
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "No Shopify authentication configured. "
            "Set SHOPIFY_ACCESS_TOKEN or CLIENT_ID + CLIENT_SECRET."
        )
 
    url = f"https://{SHOP_URL}/admin/oauth/access_token"
 
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
 
    response = requests.post(
        url,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
 
    if response.status_code >= 400:
        raise RuntimeError(
            f"Shopify access-token request failed: "
            f"HTTP {response.status_code}: {response.text[:1000]}"
        )
 
    data = response.json()
    token = data.get("access_token")
 
    if not token:
        raise RuntimeError(
            f"Shopify did not return an access token: {data}"
        )
 
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
            bullets.append(f"<li>{html.escape(value)}</li>")
    bullet_html = "<ul>" + "".join(bullets) + "</ul>" if bullets else ""
    standard_description = clean_text(product.get("desc_standard"))
    return bullet_html + f"<p>{standard_description}</p>"
 
def slugify(value: str) -> str:
    value = clean_text(value).lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")
 
def valid_image(url: str) -> bool:
    if not url:
        return False
    url = url.strip()
    return (
        (url.startswith("http://") or url.startswith("https://")) and
        (" " not in url) and
        any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"])
    )
 
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
    print(f"🔎 Found {len(items)} supplier products")
 
    selected_items = items if LIMIT is None else items[:LIMIT]
    products = [{child.tag.lower(): clean_text(child.text) for child in item} for item in selected_items]
 
    print(f"📦 Products selected for sync: {len(products)}")
    return products
 
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
# GRAPHQL CLIENT
# ============================================================
 
def graphql(query: str, variables: Optional[Dict[str, Any]] = None, operation_name: str = "GraphQL") -> Dict[str, Any]:
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
            # HTTP RETRYABLE ERRORS
            # ------------------------------------------------
            if response.status_code in (429, 500, 502, 503, 504):
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else (RETRY_DELAY * attempt + random.uniform(0, 1))
                print(f"⚠️ {operation_name}: Shopify HTTP {response.status_code}. Retry {attempt}/{MAX_RETRIES} in {delay:.1f}s")
                time.sleep(delay)
                continue
 
            # ------------------------------------------------
            # PERMANENT HTTP ERRORS
            # ------------------------------------------------
            if response.status_code >= 400:
                raise RuntimeError(f"{operation_name}: Shopify HTTP {response.status_code}: {response.text[:1000]}")
 
            result = response.json()
 
            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------
            graphql_errors = result.get("errors") or []
            if graphql_errors:
                retryable = any(error.get("extensions", {}).get("code") in ("THROTTLED", "INTERNAL_SERVER_ERROR") for error in graphql_errors)
                if retryable:
                    delay = RETRY_DELAY * attempt + random.uniform(0, 1)
                    print(f"⚠️ {operation_name}: temporary GraphQL error. Retry {attempt}/{MAX_RETRIES} in {delay:.1f}s")
                    time.sleep(delay)
                    continue
 
                messages = [error.get("message", "Unknown Shopify error") for error in graphql_errors]
                raise RuntimeError(f"{operation_name}: GraphQL error: " + " | ".join(messages))
 
            data = result.get("data")
            if data is None:
                raise RuntimeError(f"{operation_name}: Shopify returned no GraphQL data.")
 
            return data
 
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                break
            delay = RETRY_DELAY * attempt + random.uniform(0, 1)
            print(f"⚠️ {operation_name}: network error: {exc}. Retry {attempt}/{MAX_RETRIES} in {delay:.1f}s")
            time.sleep(delay)
 
        except RuntimeError:
            raise  # Permanent application/GraphQL error. Do NOT retry here.
 
        except Exception as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                break
            delay = RETRY_DELAY * attempt + random.uniform(0, 1)
            print(f"⚠️ {operation_name}: unexpected error: {exc}. Retry {attempt}/{MAX_RETRIES} in {delay:.1f}s")
            time.sleep(delay)
 
    raise RuntimeError(f"{operation_name}: Shopify request failed after {MAX_RETRIES} attempts: {last_error}")
 
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
    data = graphql(query, operation_name="Get locations")
    return data["locations"]["nodes"]
 
def resolve_location_id() -> str:
    locations = get_locations()
    active_locations = [location for location in locations if location.get("isActive")]
    
    if not active_locations:
        raise RuntimeError("❌ No active Shopify locations found.")
 
    print("\n📍 Shopify locations:")
    for location in active_locations:
        print(f"   {location['name']}: {location['id']} (online orders: {location.get('fulfillsOnlineOrders')})")
 
    # --------------------------------------------------------
    # Explicit location
    # --------------------------------------------------------
    if LOCATION_ID:
        for location in active_locations:
            if location["id"] == LOCATION_ID:
                print(f"\n📍 Using configured inventory location: {location['name']}")
                return LOCATION_ID
        raise RuntimeError(f"❌ LOCATION_ID was not found: {LOCATION_ID}")
 
    # --------------------------------------------------------
    # Automatically select if only one exists.
    # --------------------------------------------------------
    if len(active_locations) == 1:
        location = active_locations[0]
        print(f"\n📍 Automatically using Shopify location: {location['name']}")
        return location["id"]
 
    # --------------------------------------------------------
    # Prefer location fulfilling online orders if there is exactly one.
    # --------------------------------------------------------
    online_locations = [location for location in active_locations if location.get("fulfillsOnlineOrders")]
    
    if len(online_locations) == 1:
        location = online_locations[0]
        print(f"\n📍 Automatically using online-order location: {location['name']}")
        return location["id"]
 
    print("\n❌ Multiple active Shopify locations found.")
    print("Set LOCATION_ID to the location where the supplier stock should be written.")
    raise RuntimeError("Multiple Shopify locations exist. Set LOCATION_ID.")
 
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
                    selectedOptions {
                        name
                        value
                    }
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
    data = graphql(PRODUCT_QUERY, {"query": f'handle:"{handle}"'}, operation_name="Find product")
    products = data["products"]["nodes"]
    return products[0] if products else None
 
# ============================================================
# USER ERROR HELPER
# ============================================================
 
def raise_user_errors(operation: str, user_errors: List[Dict[str, Any]]):
    if not user_errors:
        return
 
    messages = []
    for error in user_errors:
        field = error.get("field")
        message = error.get("message", "Unknown Shopify error")
        if field:
            messages.append(f"{field}: {message}")
        else:
            messages.append(message)
 
    raise RuntimeError(f"{operation}: " + " | ".join(messages))
 
# ============================================================
# PRODUCT UPDATE
# ============================================================
 
PRODUCT_UPDATE_MUTATION = """
mutation ProductUpdate(
    $product: ProductUpdateInput!
) {
    productUpdate(product: $product) {
        product {
            id
            title
            handle
            status
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def update_product(source: Dict[str, Any], product_id: str):
    status = "ACTIVE" if source["stock"] > 0 else "DRAFT"
    product_input = {
        "id": product_id,
        "title": source["title"],
        "handle": source["handle"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "tags": source["tags"],
        "status": status,
    }
 
    data = graphql(PRODUCT_UPDATE_MUTATION, {"product": product_input}, operation_name="Product update")
    payload = data["productUpdate"]
    raise_user_errors("Product update", payload.get("userErrors") or [])
    return payload["product"]
 
# ============================================================
# VARIANT UPDATE
# ============================================================
 
VARIANT_UPDATE_MUTATION = """
mutation ProductVariantsBulkUpdate(
    $productId: ID!,
    $variants: [ProductVariantsBulkInput!]!
) {
    productVariantsBulkUpdate(
        productId: $productId,
        variants: $variants,
        allowPartialUpdates: false
    ) {
        product {
            id
        }
        productVariants {
            id
            title
            sku
            barcode
            price
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def update_variants(source: Dict[str, Any], product: Dict[str, Any]) -> List[Dict[str, Any]]:
    variants = product.get("variants", {}).get("nodes", [])
    if not variants:
        raise RuntimeError("Product has no Shopify variants.")
 
    source_sizes = source["sizes"]
    matched = []
 
    if source_sizes:
        for source_size in source_sizes:
            source_size_lower = source_size.strip().lower()
            found = None
 
            for variant in variants:
                selected_options = variant.get("selectedOptions") or []
                option_values = [clean_text(option.get("value")).lower() for option in selected_options]
                variant_title = clean_text(variant.get("title")).lower()
 
                if source_size_lower in option_values or source_size_lower == variant_title:
                    found = variant
                    break
 
            if found:
                matched.append(found)
 
    else:
        if source["sku"]:
            for variant in variants:
                if clean_text(variant.get("sku")) == source["sku"]:
                    matched.append(variant)
                    break
 
        if not matched and source["barcode"]:
            for variant in variants:
                if clean_text(variant.get("barcode")) == source["barcode"]:
                    matched.append(variant)
                    break
 
        if not matched and len(variants) == 1:
            matched.append(variants[0])
 
    if not matched:
        raise RuntimeError(f"Could not match Shopify variant for SKU={source['sku']} Barcode={source['barcode']} Sizes={source_sizes}")
 
    variant_inputs = []
    for variant in matched:
        inventory_item_input = {
            "tracked": True,
            "cost": source["cost"],
        }
 
        if source["sku"]:
            inventory_item_input["sku"] = source["sku"]
 
        if source["weight"] > 0:
            inventory_item_input["measurement"] = {
                "weight": {
                    "value": source["weight"],
                    "unit": "GRAMS",
                }
            }
 
        variant_input = {
            "id": variant["id"],
            "price": str(source["price"]),
            "inventoryItem": inventory_item_input,
        }
 
        if source["barcode"]:
            variant_input["barcode"] = source["barcode"]
 
        variant_inputs.append(variant_input)
 
    data = graphql(VARIANT_UPDATE_MUTATION, {
        "productId": product["id"],
        "variants": variant_inputs,
    }, operation_name="Variant update")
 
    payload = data["productVariantsBulkUpdate"]
    raise_user_errors("Variant update", payload.get("userErrors") or [])
    return payload.get("productVariants") or []
 
# ============================================================
# INVENTORY ACTIVATION
# ============================================================
 
INVENTORY_ACTIVATE_MUTATION = """
mutation InventoryActivate(
    $inventoryItemId: ID!,
    $locationId: ID!,
    $available: Int
) {
    inventoryActivate(
        inventoryItemId: $inventoryItemId,
        locationId: $locationId,
        available: $available
    ) {
        inventoryLevel {
            id
            item {
                id
            }
            location {
                id
            }
            quantities(names: ["available"]) {
                name
                quantity
            }
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def activate_inventory(inventory_item_id: str, location_id: str, quantity: int):
    data = graphql(INVENTORY_ACTIVATE_MUTATION, {
        "inventoryItemId": inventory_item_id,
        "locationId": location_id,
        "available": quantity,
    }, operation_name="Inventory activate")
 
    payload = data["inventoryActivate"]
    errors = payload.get("userErrors") or []
 
    if errors:
        messages = " ".join(e.get("message", "") for e in errors).lower()
        if not any(phrase in messages for phrase in ["already", "active", "exist"]):
            raise_user_errors("Inventory activation", errors)
 
    return payload.get("inventoryLevel")
 
# ============================================================
# INVENTORY SET QUANTITIES
# ============================================================
 
INVENTORY_SET_MUTATION = """
mutation InventorySet(
    $input: InventorySetQuantitiesInput!
) {
    inventorySetQuantities(
        input: $input
    ) {
        inventoryAdjustmentGroup {
            createdAt
            reason
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def set_inventory_quantity(inventory_item_id: str, location_id: str, quantity: int):
    variables = {
        "input": {
            "name": "available",
            "reason": "correction",
            "referenceDocumentUri": "supplier-feed://daily-sync",
            "quantities": [{
                "inventoryItemId": inventory_item_id,
                "locationId": location_id,
                "quantity": int(quantity),
            }],
        }
    }
 
    try:
        data = graphql(INVENTORY_SET_MUTATION, variables, operation_name="Inventory set quantity")
        payload = data["inventorySetQuantities"]
        raise_user_errors("Inventory set", payload.get("userErrors") or [])
        return
 
    except RuntimeError as exc:
        message = str(exc).lower()
        activation_required = any(phrase in message for phrase in ["not stocked", "inventory level", "does not exist", "not connected", "not active", "location"])
 
        if not activation_required:
            raise
 
        print("   🔌 Activating inventory item at location...")
        activate_inventory(inventory_item_id, location_id, quantity)
        time.sleep(0.25)  # Give Shopify a short moment to establish the inventory level.
        data = graphql(INVENTORY_SET_MUTATION, variables, operation_name="Inventory set quantity retry")
        payload = data["inventorySetQuantities"]
        raise_user_errors("Inventory set retry", payload.get("userErrors") or [])
 
# ============================================================
# CREATE PRODUCT
# ============================================================
 
PRODUCT_CREATE_MUTATION = """
mutation ProductCreate(
    $product: ProductCreateInput!
) {
    productCreate(product: $product) {
        product {
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
        }
    }
}
"""
 
def create_product(source: Dict[str, Any]) -> Dict[str, Any]:
    product_input = {
        "title": source["title"],
        "handle": source["handle"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "tags": source["tags"],
        "status": "ACTIVE" if source["stock"] > 0 else "DRAFT",
    }
 
    # Product options
    if source["sizes"]:
        product_input["productOptions"] = [{
            "name": "Size",
            "values": [{"name": size} for size in source["sizes"]],
        }]
 
    # Images
    media = [{"originalSource": image_url, "mediaContentType": "IMAGE", "alt": source["title"]} for image_url in source["images"]]
 
    variables = {"product": product_input}
    mutation = PRODUCT_CREATE_MUTATION
 
    if media:
        variables["media"] = media
        mutation = """
        mutation ProductCreate(
            $product: ProductCreateInput!,
            $media: [CreateMediaInput!]
        ) {
            productCreate(product: $product, media: $media) {
                product {
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
                }
            }
        }
        """
 
    data = graphql(mutation, variables, operation_name="Product create")
    payload = data["productCreate"]
    raise_user_errors("Product create", payload.get("userErrors") or [])
    product = payload.get("product")
 
    if not product:
        raise RuntimeError("Shopify productCreate returned no product.")
 
    return product
 
# ============================================================
# CREATE ADDITIONAL SIZE VARIANTS
# ============================================================
 
VARIANT_CREATE_MUTATION = """
mutation ProductVariantsBulkCreate(
    $productId: ID!,
    $variants: [ProductVariantsBulkInput!]!
) {
    productVariantsBulkCreate(
        productId: $productId,
        variants: $variants,
        strategy: REMOVE_STANDALONE_VARIANT
    ) {
        product {
            id
        }
        productVariants {
            id
            title
            selectedOptions {
                name
                value
            }
            inventoryItem {
                id
            }
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def create_missing_size_variants(source: Dict[str, Any], product: Dict[str, Any]):
    sizes = source["sizes"]
    if len(sizes) <= 1:
        return
 
    existing_variants = product.get("variants", {}).get("nodes", [])
    existing_sizes = {clean_text(option.get("value")).lower() for variant in existing_variants for option in (variant.get("selectedOptions") or []) if option.get("name", "").lower() == "size"}
 
    variants_to_create = [{
        "price": str(source["price"]),
        "barcode": source["barcode"],
        "inventoryItem": {
            "tracked": True,
            "sku": source["sku"],
            "cost": source["cost"],
            "measurement": {
                "weight": {
                    "value": source["weight"],
                    "unit": "GRAMS",
                }
            },
        },
        "optionValues": [{
            "optionName": "Size",
            "name": size,
        }] 
    } for size in sizes if size.lower() not in existing_sizes]
 
    if not variants_to_create:
        return
 
    data = graphql(VARIANT_CREATE_MUTATION, {
        "productId": product["id"],
        "variants": variants_to_create,
    }, operation_name="Create size variants")
 
    payload = data["productVariantsBulkCreate"]
    raise_user_errors("Create size variants", payload.get("userErrors") or [])
 
# ============================================================
# REFRESH PRODUCT AFTER CREATION
# ============================================================
 
def refresh_product(product_id: str) -> Dict[str, Any]:
    query = """
    query ProductById($id: ID!) {
        product(id: $id) {
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
                    inventoryItem {
                        id
                        tracked
                    }
                }
            }
        }
    }
    """
    data = graphql(query, {"id": product_id}, operation_name="Refresh product")
    product = data.get("product")
 
    if not product:
        raise RuntimeError(f"Could not refresh Shopify product {product_id}")
 
    return product
 
# ============================================================
# SYNC ONE PRODUCT
# ============================================================
 
def sync_product(source_item: Dict[str, Any], location_id: str, index: int, total: int) -> str:
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
 
    # --------------------------------------------------------
    # Find existing product
    # --------------------------------------------------------
 
    existing = find_product_by_handle(source["handle"])
    if existing:
        print(f"🔄 Existing product found: {existing['title']}")
        print(f"   Shopify ID: {existing['id']}")
 
        # Product-level update
        update_product(source, existing["id"])
 
        # Variant update
        update_variants(source, existing)
 
        # Inventory update
        variants = existing.get("variants", {}).get("nodes", [])
        matched_inventory_items = []
 
        if source["sizes"]:
            for source_size in source["sizes"]:
                source_size_lower = source_size.lower()
                for variant in variants:
                    options = variant.get("selectedOptions") or []
                    option_values = [clean_text(o.get("value")).lower() for o in options]
                    if source_size_lower in option_values:
                        matched_inventory_items.append(variant["inventoryItem"]["id"])
                        break
        else:
            variant = None
            if source["sku"]:
                for candidate in variants:
                    if clean_text(candidate.get("sku")) == source["sku"]:
                        variant = candidate
                        break
            if not variant and source["barcode"]:
                for candidate in variants:
                    if clean_text(candidate.get("barcode")) == source["barcode"]:
                        variant = candidate
                        break
            if not variant and len(variants) == 1:
                variant = variants[0]
            if variant:
                matched_inventory_items.append(variant["inventoryItem"]["id"])
 
        if not matched_inventory_items:
            raise RuntimeError("No inventory item could be matched.")
 
        matched_inventory_items = list(dict.fromkeys(matched_inventory_items))
 
        for inventory_item_id in matched_inventory_items:
            set_inventory_quantity(inventory_item_id, location_id, source["stock"])
 
        print(f"   ✅ Stock updated to {source['stock']}")
        print("   ✅ Product updated successfully")
        return "updated"
 
    # ========================================================
    # CREATE
    # ========================================================
    print("🆕 Product not found - creating...")
    product = create_product(source)
    print(f"   Created Shopify ID: {product['id']}")
 
    # Create any additional size variants.
    if len(source["sizes"]) > 1:
        create_missing_size_variants(source, product)
 
    # Refresh so we have all variant IDs.
    product = refresh_product(product["id"])
 
    # Update all variants.
    update_variants(source, product)
 
    # Refresh again to get inventory IDs.
    product = refresh_product(product["id"])
    variants = product.get("variants", {}).get("nodes", [])
 
    if not variants:
        raise RuntimeError("Created product has no variants.")
 
    # Inventory.
    inventory_items = []
    for variant in variants:
        inventory_item = variant.get("inventoryItem")
        if inventory_item:
            inventory_items.append(inventory_item["id"])
 
    inventory_items = list(dict.fromkeys(inventory_items))
 
    for inventory_item_id in inventory_items:
        set_inventory_quantity(inventory_item_id, location_id, source["stock"])
 
    print(f"   ✅ Stock set to {source['stock']}")
    print("   ✅ Product created successfully")
    return "created"
 
# ============================================================
# MAIN SYNC
# ============================================================
 
def run_sync():
    print("\n" + "=" * 70)
    print("🚀 SHOPIFY SUPPLIER SYNC")
    print("=" * 70)
 
    print(f"🏪 Shop: {SHOP_URL}")
    print(f"🔗 API: {API_VERSION}")
    print(f"👷 Workers: {MAX_WORKERS}")
 
    if LIMIT is None:
        print("📦 Limit: ALL PRODUCTS")
    else:
        print(f"📦 Limit: {LIMIT}")
 
    # --------------------------------------------------------
    # Authentication test
    # --------------------------------------------------------
    get_access_token()
    print("🔐 Shopify authentication: OK")
 
    # --------------------------------------------------------
    # Location
    # --------------------------------------------------------
    location_id = resolve_location_id()
    print(f"📍 Inventory location ID: {location_id}")
 
    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------
    items = load_xml()
    if not items:
        print("⚠️ XML feed contains no products.")
        return
 
    total = len(items)
    print("\n" + "=" * 70)
    print(f"🚀 Starting sync of {total} products...")
    print("=" * 70)
 
    created = 0
    updated = 0
    failed = 0
    failures = []
 
    # --------------------------------------------------------
    # Thread pool
    # --------------------------------------------------------
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_item = {}
 
        for index, item in enumerate(items, start=1):
            future = executor.submit(sync_product, item, location_id, index, total)
            future_to_item[future] = (index, item)
 
        # ----------------------------------------------------
        # Results
        # ----------------------------------------------------
        completed = 0
 
        for future in as_completed(future_to_item):
            index, item = future_to_item[future]
            completed += 1
            title = clean_text(item.get("title"))
 
            try:
                result = future.result()
                if result == "created":
                    created += 1
                elif result == "updated":
                    updated += 1
                else:
                    failed += 1
 
                print(f"📊 Progress: {completed}/{total} | Created: {created} | Updated: {updated} | Failed: {failed}")
 
            except Exception as exc:
                failed += 1
                error_message = str(exc)
                failures.append({
                    "index": index,
                    "title": title,
                    "sku": clean_text(item.get("sku")),
                    "error": error_message,
                })
 
                print(f"\n❌ ERROR syncing {title}")
                print(f"   SKU: {clean_text(item.get('sku'))}")
                print(f"   Error: {error_message}")
                print(f"📊 Progress: {completed}/{total} | Created: {created} | Updated: {updated} | Failed: {failed}")
 
    # ========================================================
    # FINAL REPORT
    # ========================================================
    print("\n" + "=" * 70)
    print("✅ SYNC COMPLETE")
    print("=" * 70)
    print(f"🆕 Created: {created}")
    print(f"🔄 Updated: {updated}")
    print(f"❌ Failed:  {failed}")
    print(f"📦 Total:   {total}")
 
    # --------------------------------------------------------
    # Failure report
    # --------------------------------------------------------
    if failures:
        print("\n" + "=" * 70)
        print("❌ FAILED PRODUCTS")
        print("=" * 70)
        for failure in failures:
            print(f"[{failure['index']}] {failure['
