import os
import re
import html
import time
import uuid
import json
import threading
import requests
import xml.etree.ElementTree as ET

from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONFIGURATION
# ============================================================

SHOP_URL = os.getenv("SHOP_URL", "").strip()
XML_URL = os.getenv("XML_URL", "").strip()

# Current Shopify Admin GraphQL API
API_VERSION = os.getenv("API_VERSION", "2026-07").strip()

# ------------------------------------------------------------
# LIMIT
# ------------------------------------------------------------

# LIMIT=10 -> test first 10
# LIMIT=0 / blank / None -> all products

LIMIT_RAW = os.getenv("LIMIT", "").strip()

if not LIMIT_RAW or LIMIT_RAW.lower() in ("none", "0", "all"):
    LIMIT = None
else:
    LIMIT = int(LIMIT_RAW)

# ------------------------------------------------------------
# WORKERS
# ------------------------------------------------------------
# Increase to up to 6

WORKERS = int(os.getenv("WORKERS", "4"))

# ------------------------------------------------------------
# RETRIES
# ------------------------------------------------------------

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "2"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))

# ------------------------------------------------------------
# LOCATION
# ------------------------------------------------------------

# IMPORTANT:
# Set LOCATION_ID to the Shopify Location GID, for example:
# LOCATION_ID=gid://shopify/Location/123456789
# If only one active location exists, the script can select it automatically.

LOCATION_ID = os.getenv("LOCATION_ID", "").strip()

# ------------------------------------------------------------
# TAGS
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

# ------------------------------------------------------------
# OUTPUT
# ------------------------------------------------------------

FAILURE_FILE = os.getenv("FAILURE_FILE", "sync_failures.json")
SUMMARY_FILE = os.getenv("SUMMARY_FILE", "sync_summary.json")

# ============================================================
# VALIDATE CONFIG
# ============================================================

if not SHOP_URL:
    raise RuntimeError("SHOP_URL is not configured.")

if not XML_URL:
    raise RuntimeError("XML_URL is not configured.")

# ============================================================
# TOKEN MANAGEMENT
# ============================================================

_token_cache = {
    "access_token": None,
    "expires_at": 0,
}

_token_lock = threading.Lock()

def get_access_token() -> str:
    """
    Gets a Shopify client-credentials access token and caches it.
    """
    with _token_lock:
        if (
            _token_cache["access_token"]
            and time.time() < _token_cache["expires_at"] - 60
        ):
            return _token_cache["access_token"]

        client_id = os.getenv("CLIENT_ID")
        client_secret = os.getenv("CLIENT_SECRET")

        if not client_id:
            raise RuntimeError("CLIENT_ID is not configured.")

        if not client_secret:
            raise RuntimeError("CLIENT_SECRET is not configured.")

        url = f"https://{SHOP_URL}/admin/oauth/access_token"

        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }

        response = requests.post(
            url,
            json=data,
            timeout=REQUEST_TIMEOUT,
        )

        if not response.ok:
            raise RuntimeError(
                f"Shopify token request failed: "
                f"HTTP {response.status_code}: {response.text[:1000]}"
            )

        token_data = response.json()
        token = token_data.get("access_token")

        if not token:
            raise RuntimeError(
                f"Shopify token response did not contain access_token: "
                f"{token_data}"
            )

        expires_in = int(token_data.get("expires_in", 86400))

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
    if not value:
        return ""
    return value.split(">")[-1].strip()

def split_tags(value) -> List[str]:
    value = clean_text(value)
    if not value:
        return []
    return [
        t.strip()
        for t in re.split(r"[>\|,;/\s]+", value)
        if t.strip()
    ]

def sanitize_tags(tags: List[str]) -> List[str]:
    result = []
    seen = set()

    for tag in tags:
        tag = clean_text(tag)
        if not tag:
            continue
        tag = tag.replace("&", "and").strip()
        if len(tag) > 255:
            tag = tag[:255]
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            result.append(tag)

    return result

def build_description(product: Dict[str, Any]) -> str:
    bullets = []
    for i in range(1, 11):
        value = clean_text(product.get(f"desc_{i}"))
        if value:
            escaped = html.escape(value)
            bullets.append(f"<li>{escaped}</li>")
    bullet_html = ""
    if bullets:
        bullet_html = (
            "<ul>"
            + "".join(bullets)
            + "</ul>"
        )
    standard = clean_text(product.get("desc_standard"))
    standard = html.escape(standard)

    return (
        bullet_html
        + f"<p>{standard}</p>"
    )

def slugify(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")

def valid_image(url: str) -> bool:
    if not url:
        return False
    url = url.strip()
    return url.startswith("http://") or url.startswith("https://")

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

def gid_numeric(gid: str) -> Optional[str]:
    if not gid:
        return None
    match = re.search(r"/(\d+)(?:\?|$)", gid)
    if match:
        return match.group(1)
    return None

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

    selected_items = (
        items[:LIMIT] if LIMIT is not None else items
    )

    products = []
    for item in selected_items:
        data = {}
        for child in item:
            data[child.tag.lower()] = clean_text(child.text)
        products.append(data)

    return products

# ============================================================
# GRAPHQL CLIENT
# ============================================================

def graphql(query: str, variables: Optional[Dict[str, Any]] = None, retry_transient: bool = True) -> Dict[str, Any]:
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
    attempts = MAX_RETRIES if retry_transient else 1

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)

            # ------------------------------------------------
            # TRANSIENT HTTP ERRORS
            # ------------------------------------------------

            if response.status_code in (429, 500, 502, 503, 504):
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = RETRY_DELAY * attempt
                else:
                    delay = RETRY_DELAY * attempt

                if attempt < attempts:
                    print(f"⚠️ Shopify HTTP {response.status_code}; retry {attempt}/{attempts} in {delay:.1f}s")
                    time.sleep(delay)
                    continue

                raise RuntimeError(f"Shopify HTTP {response.status_code}: {response.text[:1000]}")

            response.raise_for_status()
            result = response.json()

            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------

            errors = result.get("errors")
            if errors:
                error_text = str(errors)

                # GraphQL validation/schema errors are permanent.
                permanent_keywords = (
                    "doesn't exist",
                    "not defined",
                    "Type mismatch",
                    "variableMismatch",
                    "variableNotUsed",
                    "INVALID_VARIABLE",
                    "Field is not defined",
                    "required argument",
                )

                is_permanent = any(keyword in error_text for keyword in permanent_keywords)

                if is_permanent:
                    raise RuntimeError("GraphQL validation error: " + error_text)

                if retry_transient and attempt < attempts:
                    delay = RETRY_DELAY * attempt
                    print(f"⚠️ Shopify GraphQL error; retry {attempt}/{attempts} in {delay:.1f}s")
                    time.sleep(delay)
                    continue

                raise RuntimeError("GraphQL errors: " + error_text)

            return result.get("data", {})

        except requests.RequestException as exc:
            last_error = exc
            if retry_transient and attempt < attempts:
                delay = RETRY_DELAY * attempt
                print(f"⚠️ Network error: {exc}; retry {attempt}/{attempts} in {delay:.1f}s")
                time.sleep(delay)
                continue
            break

        except RuntimeError:
            raise

        except Exception as exc:
            last_error = exc
            if retry_transient and attempt < attempts:
                delay = RETRY_DELAY * attempt
                time.sleep(delay)
                continue
            break

    raise RuntimeError(f"Shopify GraphQL request failed after {attempts} attempts: {last_error}")

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

    # --------------------------------------------------------
    # Explicit LOCATION_ID
    # --------------------------------------------------------

    if LOCATION_ID:
        for location in active_locations:
            if location["id"] == LOCATION_ID:
                print(f"📍 Inventory location: {location['name']} ({location['id']})")
                return LOCATION_ID

        print("\n📍 Shopify locations:")
        for location in active_locations:
            print(f"   {location['name']}: {location['id']}")
        raise RuntimeError(f"❌ LOCATION_ID was not found: {LOCATION_ID}")

    # --------------------------------------------------------
    # Automatically use single active location
    # --------------------------------------------------------

    if len(active_locations) == 1:
        location = active_locations[0]
        print(f"📍 Automatically using Shopify location: {location['name']} ({location['id']})")
        return location["id"]

    # --------------------------------------------------------
    # Prefer online fulfillment location
    # --------------------------------------------------------

    online_locations = [location for location in active_locations if location.get("fulfillsOnlineOrders")]

    if len(online_locations) == 1:
        location = online_locations[0]
        print(f"📍 Automatically using online fulfillment location: {location['name']} ({location['id']})")
        return location["id"]

    # --------------------------------------------------------
    # Multiple locations
    # --------------------------------------------------------

    print("\n📍 Shopify locations:")
    for location in active_locations:
        print(f"   {location['name']}: {location['id']} (online={location.get('fulfillsOnlineOrders')})")

    raise RuntimeError("❌ Multiple Shopify locations exist. Set LOCATION_ID to the location that should receive supplier stock.")

# ============================================================
# SHOPIFY PRODUCT PRELOAD
# ============================================================

PRODUCTS_QUERY = """
query ProductsPage($after: String) {
    products(first: 250, after: $after) {
        pageInfo {
            hasNextPage
            endCursor
        }
        nodes {
            id
            title
            handle
            status
            options {
                id
                name
                position
                optionValues {
                    id
                    name
                }
            }
            variants(first: 250) {
                nodes {
                    id
                    title
                    sku
                    barcode
                    price
                    inventoryQuantity
                    selectedOptions {
                        name
                        value
                    }
                    inventoryItem {
                        id
                        sku
                        tracked
                    }
                }
            }
        }
    }
}
"""

def load_shopify_catalog() -> Dict[str, Dict[str, Any]]:
    print("\n📚 Loading Shopify product catalog...")
    catalog = {}
    after = None
    page = 0

    while True:
        page += 1
        data = graphql(PRODUCTS_QUERY, {"after": after})

        products_data = data.get("products", {})
        nodes = products_data.get("nodes", [])

        for product in nodes:
            handle = product.get("handle")
            if handle:
                catalog[handle.lower()] = product

        page_info = products_data.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break

        after = page_info.get("endCursor")
        if not after:
            break

        print(f"   Shopify catalog page {page} loaded...")

    print(f"✅ Shopify catalog loaded: {len(catalog)} products")
    return catalog

# ============================================================
# BUILD SOURCE PRODUCT
# ============================================================

def build_source_product(p: Dict[str, Any]) -> Dict[str, Any]:
    title = (
        clean_text(p.get("title"))
        or f"Product-{clean_text(p.get('sku'))}"
    )

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
        split_tags(p.get("productbrand"))
        + split_tags(p.get("productrange"))
        + TAGS_TO_INCLUDE
        + split_tags(title)
    )

    description = build_description(p)
    raw_images = clean_text(p.get("imageoffloads"))
    images = [
        image.strip()
        for image in re.split(r"[|,]+", raw_images)
        if valid_image(image)
    ]

    sizes = [
        size.strip()
        for size in re.split(r"[|,]+", clean_text(p.get("sizeattribute")))
        if size.strip()
    ]

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

def update_product(source: Dict[str, Any], product_id: str) -> None:
    input_data = {
        "id": product_id,
        "title": source["title"],
        "handle": source["handle"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "tags": source["tags"],
        "status": "ACTIVE" if source["stock"] > 0 else "DRAFT",
    }

    data = graphql(
        PRODUCT_UPDATE_MUTATION,
        {
            "product": input_data
        }
    )

    payload = data.get("productUpdate")
    if not payload:
        raise RuntimeError("Shopify returned no productUpdate payload.")

    errors = payload.get("userErrors", [])
    if errors:
        raise RuntimeError("Shopify productUpdate errors: " + str(errors))

# ============================================================
# PRODUCT VARIANT UPDATE
# ============================================================

VARIANTS_UPDATE_MUTATION = """
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
            price
            barcode
            inventoryItem {
                id
                sku
                tracked
            }
        }
        userErrors {
            field
            message
        }
    }
}
"""

def update_variant(product_id: str, variant_id: str, source: Dict[str, Any]) -> Dict[str, Any]:
    variants_input.append({
    "optionValues": [{"optionName": "Size", "name": size}],  # Ensure the optionName is set correctly
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
})
    
    data = graphql(
        VARIANTS_UPDATE_MUTATION,
        {
            "productId": product_id,
            "variants": [variant_input],
        }
    )

    payload = data.get("productVariantsBulkUpdate")
    if not payload:
        raise RuntimeError("Shopify returned no productVariantsBulkUpdate payload.")

    errors = payload.get("userErrors", [])
    if errors:
        raise RuntimeError("Shopify variant update errors: " + str(errors))

    variants = payload.get("productVariants", [])
    return variants[0] if variants else {}

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
            quantities(names: ["available"]) {
                name
                quantity
            }
            item {
                id
            }
            location {
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

def activate_inventory(inventory_item_id: str, location_id: str, initial_quantity: int = 0) -> None:
    data = graphql(
        INVENTORY_ACTIVATE_MUTATION,
        {
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
            "available": initial_quantity,
        },
        retry_transient=False,
    )

    payload = data.get("inventoryActivate")
    if not payload:
        raise RuntimeError("Shopify returned no inventoryActivate payload.")

    errors = payload.get("userErrors", [])
    if errors:
        error_text = str(errors)
        # Already active is harmless.
        if "already" in error_text.lower():
            return
        raise RuntimeError("Inventory activation errors: " + error_text)

# ============================================================
# CHECK INVENTORY LEVEL
# ============================================================

INVENTORY_LEVEL_QUERY = """
query InventoryLevel(
    $inventoryItemId: ID!,
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

def inventory_level_exists(inventory_item_id: str, location_id: str) -> bool:
    data = graphql(
        INVENTORY_LEVEL_QUERY,
        {
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
        },
        retry_transient=False,
    )

    item = data.get("inventoryItem")
    if not item:
        return False

    return bool(item.get("inventoryLevel"))

# ============================================================
# INVENTORY SET
# ============================================================

INVENTORY_SET_MUTATION = """
mutation InventorySet(
    $input: InventorySetQuantitiesInput!,
    $idempotencyKey: String!
) {
    inventorySetQuantities(
        input: $input
    ) @idempotent(key: $idempotencyKey) {
        inventoryAdjustmentGroup {
            reason
            referenceDocumentUri
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

def set_inventory(inventory_item_id: str, location_id: str, quantity: int) -> None:
    # --------------------------------------------------------
    # IMPORTANT:
    #
    # changeFromQuantity MUST be present.
    #
    # None tells Shop to skip the compare-and-swap
    # quantity check.
    # --------------------------------------------------------

    input_data = {
        "name": "available",
        "reason": "correction",
        "referenceDocumentUri": (
            "https://honeylade-sync.local/"
            f"{uuid.uuid4()}"
        ),
        "quantities": [
            {
                "inventoryItemId": inventory_item_id,
                "locationId": location_id,
                "quantity": int(quantity),
                "changeFromQuantity": None,
            }
        ],
    }

    data = graphql(
        INVENTORY_SET_MUTATION,
        {
            "input": input_data,
            "idempotencyKey": str(uuid.uuid4()),
        }
    )

    payload = data.get("inventorySetQuantities")
    if not payload:
        raise RuntimeError("Shopify returned no inventorySetQuantities payload.")

    errors = payload.get("userErrors", [])
    if errors:
        raise RuntimeError("Inventory update errors: " + str(errors))

# ============================================================
# CREATE PRODUCT
# ============================================================

PRODUCT_CREATE_MUTATION = """
mutation ProductCreate(
    $product: ProductCreateInput!,
    $media: [CreateMediaInput!]
) {
    productCreate(
        product: $product,
        media: $media
    ) {
        product {
            id
            title
            handle
            status
            options {
                id
                name
                position
                optionValues {
                    id
                    name
                }
            }
            variants(first: 10) {
                nodes {
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

    # --------------------------------------------------------
    # Create Size option if supplier provides sizes.
    # --------------------------------------------------------

    if source["sizes"]:
        product_input["productOptions"] = [
            {
                "name": "Size",
                "values": [
                    {
                        "name": size
                    }
                    for size in source["sizes"]
                ],
            }
        ]

    media = []
    for image_url in source["images"]:
        media.append(
            {
                "originalSource": image_url,
                "mediaContentType": "IMAGE",
                "alt": source["title"],
            }
        )

    data = graphql(
        PRODUCT_CREATE_MUTATION,
        {
            "product": product_input,
            "media": media or None,
        }
    )

    payload = data.get("productCreate")
    if not payload:
        raise RuntimeError("Shopify returned no productCreate payload.")

    errors = payload.get("userErrors", [])
    if errors:
        raise RuntimeError("Shopify productCreate errors: " + str(errors))

    product = payload.get("product")
    if not product:
        raise RuntimeError("Shopify created no product.")

    return product

# ============================================================
# CREATE VARIANTS
# ============================================================

VARIANTS_CREATE_MUTATION = """
mutation ProductVariantsBulkCreate(
    $productId: ID!,
    $variants: [ProductVariantsBulkInput!]!
) {
    productVariantsBulkCreate(
        productId: $productId,
        variants: $variants
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

def create_missing_variants(product: Dict[str, Any], source: Dict[str, Any]) -> List[Dict[str, Any]]:
    sizes = source["sizes"]
    if not sizes:
        return []

    existing_values = set()
    for variant in product.get("variants", {}).get("nodes", []):
        for option in variant.get("selectedOptions", []):
            if option.get("name") == "Size":
                existing_values.add(option.get("value"))

    missing_sizes = [size for size in sizes if size not in existing_values]
    if not missing_sizes:
        return []

    option_id = None
    for option in product.get("options", []):
        if option.get("name") == "Size":
            option_id = option.get("id")
            break

    variants_input = []
    for size in missing_sizes:
        option_value = {
            "name": size
        }
        if option_id:
            option_value["optionId"] = option_id

        variants_input.append(
            {
                "optionValues": [option_value],
                "price": str(source["price"]),
                "barcode": source["barcode"],
                "inventoryItem": {
                    "sku": source["sku"],
                    "cost": str(source["cost"]),
                    "tracked": True,
                    "measurement": {
                        "weight": {
                            "value": source["weight"],
                            "unit": "GRAMS",
                        }
                    },
                },
            }
        )

    data = graphql(
        VARIANTS_CREATE_MUTATION,
        {
            "productId": product["id"],
            "variants": variants_input,
        }
    )

    payload = data.get("productVariantsBulkCreate")
    if not payload:
        raise RuntimeError("Shopify returned no productVariantsBulkCreate payload.")

    errors = payload.get("userErrors", [])
    if errors:
        raise RuntimeError("Shopify variant creation errors: " + str(errors))

    return payload.get("productVariants", [])

# ============================================================
# MATCH EXISTING VARIANT
# ============================================================

def find_matching_variant(product: Dict[str, Any], source: Dict[str, Any], size: Optional[str] = None) -> Optional[Dict[str, Any]]:
    variants = product.get("variants", {}).get("nodes", [])

    # --------------------------------------------------------
    # If size is supplied, match Size option first.
    # --------------------------------------------------------

    if size:
        for variant in variants:
            selected_options = variant.get("selectedOptions", [])
            for option in selected_options:
                if option.get("name") == "Size" and option.get("value") == size:
                    return variant

    # --------------------------------------------------------
    # Match SKU.
    # --------------------------------------------------------

    if source["sku"]:
        for variant in variants:
            sku = variant.get("sku") or variant.get("inventoryItem", {}).get("sku")
            if sku and str(sku) == str(source["sku"]):
                return variant

    # --------------------------------------------------------
    # Match barcode.
    # --------------------------------------------------------

    if source["barcode"]:
        for variant in variants:
            if variant.get("barcode") and str(variant.get("barcode")) == str(source["barcode"]):
                return variant

    # --------------------------------------------------------
    # Single-variant product.
    # --------------------------------------------------------

    if len(variants) == 1:
        return variants[0]

    return None

# ============================================================
# SYNC ONE PRODUCT
# ============================================================

def sync_product(index: int, total: int, source_item: Dict[str, Any], catalog: Dict[str, Dict[str, Any]], location_id: str) -> Dict[str, Any]:
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

    handle_key = source["handle"].lower()
    product = catalog.get(handle_key)
    created = False

    # ========================================================
    # CREATE OR UPDATE PRODUCT
    # ========================================================

    if product:
        print(f"🔄 Existing product found: {product['title']}")
        print(f"   Shopify ID: {product['id']}")
        update_product(source, product["id"])
    else:
        print("🆕 Product not found - creating...")
        product = create_product(source)
        created = True
        print(f"✅ Product created: {product['title']}")
        print(f"   Shopify ID: {product['id']}")
        # Put it into local catalog.
        catalog[handle_key] = product

    # ========================================================
    # CREATE MISSING SIZE VARIANTS
    # ========================================================

    if source["sizes"]:
        try:
            created_variants = create_missing_variants(product, source)
            if created_variants:
                print(f"   ➕ Created {len(created_variants)} missing variant(s)")
                # Refresh product after variant creation.
                refreshed = find_product_after_change(product["id"])
                if refreshed:
                    product = refreshed
                    catalog[handle_key] = refreshed
        except Exception as exc:
            raise RuntimeError(f"Variant creation failed: {exc}")

    # ========================================================
    # MATCH VARIANTS
    # ========================================================

    variants = product.get("variants", {}).get("nodes", [])
    if not variants:
        raise RuntimeError("Product has no variants.")

    processed_variants = 0

    # --------------------------------------------------------
    # If source has sizes, update each matching size.
    # --------------------------------------------------------

    if source["sizes"]:
        for size in source["sizes"]:
            variant = find_matching_variant(product, source, size=size)
            if not variant:
                print(f"⚠️ Could not match variant size '{size}'")
                continue

            updated_variant = update_variant(product["id"], variant["id"], source)
            inventory_item_id = (
                updated_variant.get("inventoryItem", {}).get("id")
                or variant.get("inventoryItem", {}).get("id")
            )

            if not inventory_item_id:
                raise RuntimeError(f"No inventory item ID for variant {variant['id']}")

            # -----------------------------------------------
            # Ensure inventory is active at location.
            # -----------------------------------------------

            if not inventory_level_exists(inventory_item_id, location_id):
                print("   📍 Activating inventory at selected location...")
                activate_inventory(inventory_item_id, location_id, 0)

            # -----------------------------------------------
            # Set absolute supplier quantity.
            # -----------------------------------------------

            set_inventory(inventory_item_id, location_id, source["stock"])
            processed_variants += 1

    # --------------------------------------------------------
    # No sizes: update the single/default variant.
    # --------------------------------------------------------

    else:
        variant = find_matching_variant(product, source)
        if not variant:
            raise RuntimeError("Could not match product variant.")

        updated_variant = update_variant(product["id"], variant["id"], source)
        inventory_item_id = (
            updated_variant.get("inventoryItem", {}).get("id")
            or variant.get("inventoryItem", {}).get("id")
        )

        if not inventory_item_id:
            raise RuntimeError(f"No inventory item ID for variant {variant['id']}")

        if not inventory_level_exists(inventory_item_id, location_id):
            print("   📍 Activating inventory at selected location...")
            activate_inventory(inventory_item_id, location_id, 0)

        set_inventory(inventory_item_id, location_id, source["stock"])
        processed_variants += 1

    if processed_variants == 0:
        raise RuntimeError("No variants were successfully processed.")

    print(f"   ✅ Stock updated: {source['stock']} ({processed_variants} variant(s))")
    return {
        "status": "created" if created else "updated",
        "title": title,
        "handle": source["handle"],
        "sku": source["sku"],
        "stock": source["stock"],
        "variants": processed_variants,
    }

# ============================================================
# REFRESH PRODUCT
# ============================================================

PRODUCT_BY_ID_QUERY = """
query ProductById($id: ID!) {
    product(id: $id) {
        id
        title
        handle
        status
        options {
            id
            name
            position
            optionValues {
                id
                name
            }
        }
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
                    sku
                    tracked
                }
            }
        }
    }
}
"""

def find_product_after_change(product_id: str) -> Optional[Dict[str, Any]]:
    data = graphql(
        PRODUCT_BY_ID_QUERY,
        {
            "id": product_id
        }
    )
    return data.get("product")

# ============================================================
# FAILURE LOG
# ============================================================

def save_failures(failures: List[Dict[str, Any]]) -> None:
    try:
        with open(
            FAILURE_FILE,
            "w",
            encoding="utf-8"
        ) as file:
            json.dump(
                failures,
                file,
                indent=2,
                ensure_ascii=False
            )
        print(f"\n💾 Failure log written to {FAILURE_FILE}")
    except Exception as exc:
        print(f"⚠️ Could not write failure log: {exc}")

# ============================================================
# MAIN SYNC
# ============================================================
def run_sync():
    start_time = time.time()

    print("\n" + "=" * 70)
    print("🚀 SHOP SYNC")
    print("=" * 70)

    print(f"Shopify API: {API_VERSION}")
    print(f"Workers: {WORKERS}")
    print(f"Limit: {LIMIT if LIMIT is not None else 'ALL'}")
    print("=" * 70)

    # --------------------------------------------------------
    # Authenticate
    # --------------------------------------------------------

    get_access_token()
    print("🔐 Shopify authentication: OK")

    # --------------------------------------------------------
    # Location
    # --------------------------------------------------------

    location_id = resolve_location_id()

    # --------------------------------------------------------
    # Load supplier feed
    # --------------------------------------------------------

    items = load_xml()
    if not items:
        print("⚠️ XML feed contains no products.")
        return

    total = len(items)

    # --------------------------------------------------------
    # Load Shopify catalog once.
    # --------------------------------------------------------

    catalog = load_shopify_catalog()

    print("\n" + "=" * 70)
    print(f"🚀 Starting sync of {total} products...")
    print(f"👷 Workers: {WORKERS}")
    print(f"📍 Location: {location_id}")
    print("=" * 70)

    created = 0
    updated = 0
    failed = 0
    failures = []
    completed = 0

    # --------------------------------------------------------
    # Thread pool
    # --------------------------------------------------------

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_map = {}
        for index, item in enumerate(items, start=1):
            future = executor.submit(
                sync_product,
                index,
                total,
                item,
                catalog,
                location_id,
            )
            future_map[future] = item

        # ----------------------------------------------------
        # Process completed tasks.
        # ----------------------------------------------------

        for future in as_completed(future_map):
            item = future_map[future]

            try:
                result = future.result()
                completed += 1

                if result["status"] == "created":
                    created += 1
                else:
                    updated += 1

            except Exception as exc:
                failed += 1
                completed += 1

                title = clean_text(item.get("title"))
                sku = clean_text(item.get("sku"))

                print("\n" + "!" * 70)
                print(f"❌ ERROR syncing {title}")
                print(f"   SKU: {sku}")
                print(f"   Error: {exc}")
                print("!" * 70)

                failures.append({
                    "title": title,
                    "sku": sku,
                    "barcode": clean_text(item.get("barcode")),
                    "error": str(exc),
                })

    # --------------------------------------------------------
    # Save failure log
    # --------------------------------------------------------
    if failures:
        save_failures(failures)
    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    summary = {
        "total": total,
        "created": created,
        "updated": updated,
        "failed": failed,
        "workers": WORKERS,
        "api_version": API_VERSION,
        "location_id": location_id,
        "elapsed_seconds": round(elapsed, 2),
    }

    try:
        with open(SUMMARY_FILE, "w", encoding="utf-8") as file:
            json.dump(summary, file, indent=2)
    except Exception:
        pass

    print("\n" + "=" * 70)
    print("✅ SYNC COMPLETE")
    print("=" * 70)
    print(f"🆕 Created: {created}")
    print(f"🔄 Updated: {updated}")
    print(f"❌ Failed:  {failed}")
    print(f"📦 Total:   {total}")
    print(f"⏱️ Time:    {minutes}m {seconds}s")
    print("=" * 70)

    if failures:
        print(f"\n⚠️ {len(failures)} products failed.")
        print(f"See {FAILURE_FILE} for the failure list.")
    else:
        print("\n🎉 No product failures.")

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
