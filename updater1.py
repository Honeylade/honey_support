import os
import re
import html
import time
import uuid
import json
import random
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

# Shopify Admin GraphQL API
API_VERSION = os.getenv("API_VERSION", "2026-07").strip()

# ------------------------------------------------------------
# LIMIT
# ------------------------------------------------------------

LIMIT_RAW = os.getenv("LIMIT", "").strip()

if not LIMIT_RAW or LIMIT_RAW.lower() in ("none", "0", "all"):
    LIMIT = None
else:
    LIMIT = int(LIMIT_RAW)

# ------------------------------------------------------------
# WORKERS
# ------------------------------------------------------------

WORKERS = max(1, int(os.getenv("WORKERS", "4")))

# ------------------------------------------------------------
# RETRIES
# ------------------------------------------------------------

MAX_RETRIES = max(1, int(os.getenv("MAX_RETRIES", "4")))
RETRY_DELAY = max(0.1, float(os.getenv("RETRY_DELAY", "2")))
REQUEST_TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT", "60")))

# ------------------------------------------------------------
# LOCATION
# ------------------------------------------------------------

LOCATION_ID = os.getenv("LOCATION_ID", "").strip()

# ------------------------------------------------------------
# PRODUCT MATCHING
# ------------------------------------------------------------

MATCH_BY_SKU = os.getenv("MATCH_BY_SKU", "true").lower() in ("1", "true", "yes", "on")
MATCH_BY_BARCODE = os.getenv("MATCH_BY_BARCODE", "true").lower() in ("1", "true", "yes", "on")
MATCH_BY_HANDLE = os.getenv("MATCH_BY_HANDLE", "true").lower() in ("1", "true", "yes", "on")

# ------------------------------------------------------------
# SIZE VARIANTS
# ------------------------------------------------------------

CREATE_SIZE_VARIANTS_FOR_NEW_PRODUCTS = os.getenv(
    "CREATE_SIZE_VARIANTS_FOR_NEW_PRODUCTS", "true"
).lower() in ("1", "true", "yes", "on")

CREATE_MISSING_SIZE_VARIANTS_FOR_EXISTING_PRODUCTS = os.getenv(
    "CREATE_MISSING_SIZE_VARIANTS_FOR_EXISTING_PRODUCTS", "false"
).lower() in ("1", "true", "yes", "on")

SIZE_OPTION_NAME = os.getenv("SIZE_OPTION_NAME", "Size").strip() or "Size"

# ------------------------------------------------------------
# HANDLES
# ------------------------------------------------------------

UPDATE_EXISTING_HANDLES = os.getenv(
    "UPDATE_EXISTING_HANDLES", "false"
).lower() in ("1", "true", "yes", "on")

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

FILTER_BY_TAGS = os.getenv(
    "FILTER_BY_TAGS", "false"
).lower() in ("1", "true", "yes", "on")

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
# THREAD / STATE MANAGEMENT
# ============================================================

_token_cache = {
    "access_token": None,
    "expires_at": 0,
}

_token_lock = threading.Lock()

_product_identity_lock = threading.Lock()
_inventory_locks: Dict[str, threading.Lock] = {}
_inventory_locks_guard = threading.Lock()


def get_inventory_lock(inventory_item_id: str, location_id: str) -> threading.Lock:
    key = f"{inventory_item_id}|{location_id}"

    with _inventory_locks_guard:
        lock = _inventory_locks.get(key)

        if lock is None:
            lock = threading.Lock()
            _inventory_locks[key] = lock

        return lock


# ============================================================
# TOKEN MANAGEMENT
# ============================================================

def get_access_token() -> str:
    """
    Get a Shopify client-credentials access token and cache it.
    """
    with _token_lock:
        if (_token_cache["access_token"] and
                time.time() < _token_cache["expires_at"] - 60):
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

        response = requests.post(url, json=data, timeout=REQUEST_TIMEOUT)

        if not response.ok:
            raise RuntimeError(
                "Shopify token request failed: "
                f"HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )

        token_data = response.json()
        token = token_data.get("access_token")

        if not token:
            raise RuntimeError(
                "Shopify token response did not contain access_token: "
                f"{token_data}"
            )

        expires_in = int(token_data.get("expires_in", 86400))

        _token_cache["access_token"] = token
        _token_cache["expires_at"] = time.time() + expires_in

        return token


# ============================================================
# PRICE LOGIC
# ============================================================

TAX_RATE = 0.20
PAYMENT_FEE_RATE = 0.029
PLATFORM_FEE_RATE = 0.09
PAYMENT_FIXED_COST = 0.30
OTHER_FIXED_COST = 0.50


def calc_price(cost, weight):
    cost = max(0.0, float(cost or 0))
    weight = max(0.0, float(weight or 0))

    # Shipping
    if weight < 300:
        shipping = 3.99
    elif weight < 2000:
        shipping = 4.99
    else:
        shipping = 18.00

    # Markup
    if cost < 5:
        markup = 0.30
    elif cost < 10:
        markup = 0.25
    else:
        markup = 0.20

    fees = PAYMENT_FEE_RATE + PLATFORM_FEE_RATE

    base_price = cost * (1 + markup)
    taxed_price = base_price * (1 + TAX_RATE)
    price_after_percentage_fees = taxed_price / (1 - fees)

    final_price = (
        price_after_percentage_fees
        + shipping
        + PAYMENT_FIXED_COST
        + OTHER_FIXED_COST
    )

    return round(final_price, 2)


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value) -> str:
    if value is None:
        return ""

    return html.unescape(str(value).strip())


def normalise_key(value) -> str:
    return clean_text(value).strip().lower()


def last_value(val):
    if not val:
        return ""

    value = val.split(">")[-1].strip()

    return (
        value
        .replace("&", "and")
        .replace("&", "and")
    )


def split_tags(val):
    if not val:
        return []

    return [
        t.strip()
        for t in re.split(r"[>\|,;/\s]+", val)
        if t.strip()
    ]


def split_values(val):
    if not val:
        return []

    values = []

    for value in re.split(r"[|,;]+", val):
        value = clean_text(value)

        if value:
            values.append(value)

    return values


def sanitize_tags(tags):
    sanitized = []
    seen = set()

    for tag in tags:
        if not tag:
            continue

        tag = clean_text(tag)

        # Preserve spaces but remove HTML/special characters.
        tag = "".join(
            char
            for char in tag
            if char.isalnum() or char.isspace()
        )

        tag = re.sub(r"\s+", " ", tag).strip()

        if len(tag) > 255:
            tag = tag[:255]

        key = tag.lower()

        if tag and key not in seen:
            seen.add(key)
            sanitized.append(tag)

    return sanitized[:250]


def build_description(product: Dict[str, Any]) -> str:
    bullets = []

    for i in range(1, 11):
        value = clean_text(product.get(f"desc_{i}"))

        if value:
            escaped = html.escape(value)
            bullets.append(f"<li>{escaped}</li>")

    bullet_html = ""

    if bullets:
        bullet_html = "<ul>" + "".join(bullets) + "</ul>"

    standard = clean_text(product.get("desc_standard"))

    if standard:
        standard_html = f"<p>{html.escape(standard)}</p>"
    else:
        standard_html = ""

    return bullet_html + standard_html


def slugify(value: str) -> str:
    value = clean_text(value).lower()

    value = re.sub(
        r"[^a-z0-9]+",
        "-",
        value,
    )

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

    match = re.search(
        r"/(\d+)(?:\?|$)",
        gid,
    )

    if match:
        return match.group(1)

    return None


def dedupe_preserve_order(values):
    result = []
    seen = set()

    for value in values:
        key = normalise_key(value)

        if key and key not in seen:
            seen.add(key)
            result.append(value)

    return result


# ============================================================
# XML
# ============================================================

def load_xml() -> List[Dict[str, Any]]:
    print("📥 Downloading XML feed...")

    response = requests.get(
        XML_URL,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    root = ET.fromstring(response.content)

    items = root.findall(".//post")

    print(f"🔎 Found {len(items)} supplier items")

    products = []

    for item in items:
        data = {}

        for child in item:
            data[child.tag.lower()] = clean_text(child.text)

        products.append(data)

    # Optional supplier tag filtering.
    if FILTER_BY_TAGS:
        wanted = {
            normalise_key(tag)
            for tag in TAGS_TO_INCLUDE
        }

        filtered = []

        for product in products:
            supplier_values = []

            for field in (
                "productbrand",
                "productrange",
                "title",
                "tags",
                "category",
            ):
                supplier_values.extend(
                    split_tags(product.get(field))
                )

            supplier_keys = {
                normalise_key(value)
                for value in supplier_values
            }

            if wanted.intersection(supplier_keys):
                filtered.append(product)

        products = filtered

        print(
            f"🏷️ Tag filtering enabled: "
            f"{len(products)} products selected"
        )

    if LIMIT is not None:
        products = products[:LIMIT]

    print(f"📦 Products selected for sync: {len(products)}")

    return products


# ============================================================
# GRAPHQL CLIENT
# ============================================================

def graphql(
    query: str,
    variables: Optional[Dict[str, Any]] = None,
    retry_transient: bool = True,
) -> Dict[str, Any]:

    token = get_access_token()

    url = (
        f"https://{SHOP_URL}"
        f"/admin/api/{API_VERSION}/graphql.json"
    )

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

    attempts = (
        MAX_RETRIES
        if retry_transient
        else 1
    )

    permanent_keywords = (
        "doesn't exist",
        "not defined",
        "type mismatch",
        "variableMismatch",
        "variableNotUsed",
        "INVALID_VARIABLE",
        "field is not defined",
        "required argument",
        "unknown argument",
        "cannot query field",
        "syntax error",
    )

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            # ------------------------------------------------
            # TRANSIENT HTTP ERRORS
            # ------------------------------------------------

            if response.status_code in (
                429,
                500,
                502,
                503,
                504,
            ):
                retry_after = response.headers.get(
                    "Retry-After"
                )

                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = RETRY_DELAY * attempt
                else:
                    delay = RETRY_DELAY * attempt

                delay += random.uniform(0, 0.5)

                if attempt < attempts:
                    print(
                        f"⚠️ Shopify HTTP "
                        f"{response.status_code}; "
                        f"retry {attempt}/{attempts} "
                        f"in {delay:.1f}s"
                    )

                    time.sleep(delay)
                    continue

                raise RuntimeError(
                    f"Shopify HTTP "
                    f"{response.status_code}: "
                    f"{response.text[:1000]}"
                )

            response.raise_for_status()

            result = response.json()

            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------

            errors = result.get("errors")

            if errors:
                error_text = str(errors)

                is_permanent = any(
                    keyword in error_text.lower()
                    for keyword in permanent_keywords
                )

                if is_permanent:
                    raise RuntimeError(
                        "GraphQL validation error: "
                        + error_text
                    )

                if (
                    retry_transient
                    and attempt < attempts
                ):
                    delay = (
                        RETRY_DELAY * attempt
                        + random.uniform(0, 0.5)
                    )

                    print(
                        "⚠️ Shopify GraphQL error; "
                        f"retry {attempt}/{attempts} "
                        f"in {delay:.1f}s"
                    )

                    time.sleep(delay)
                    continue

                raise RuntimeError(
                    "GraphQL errors: "
                    + error_text
                )

            return result.get("data", {})

        except requests.RequestException as exc:
            last_error = exc

            if (
                retry_transient
                and attempt < attempts
            ):
                delay = (
                    RETRY_DELAY * attempt
                    + random.uniform(0, 0.5)
                )

                print(
                    f"⚠️ Network error: {exc}; "
                    f"retry {attempt}/{attempts} "
                    f"in {delay:.1f}s"
                )

                time.sleep(delay)
                continue

            break

        except RuntimeError:
            raise

        except Exception as exc:
            last_error = exc

            if (
                retry_transient
                and attempt < attempts
            ):
                delay = (
                    RETRY_DELAY * attempt
                    + random.uniform(0, 0.5)
                )

                time.sleep(delay)
                continue

            break

    raise RuntimeError(
        "Shopify GraphQL request failed after "
        f"{attempts} attempts: {last_error}"
    )


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

    return data.get(
        "locations",
        {},
    ).get(
        "nodes",
        [],
    )


def resolve_location_id() -> str:
    locations = get_locations()

    active_locations = [
        location
        for location in locations
        if location.get("isActive")
    ]

    if not active_locations:
        raise RuntimeError(
            "❌ No active Shopify locations found."
        )

    if LOCATION_ID:
        for location in active_locations:
            if location["id"] == LOCATION_ID:
                print(
                    f"📍 Inventory location: "
                    f"{location['name']} "
                    f"({location['id']})"
                )
                return LOCATION_ID

        print("\n📍 Shopify locations:")

        for location in active_locations:
            print(
                f"   {location['name']}: "
                f"{location['id']}"
            )

        raise RuntimeError(
            f"❌ LOCATION_ID was not found: "
            f"{LOCATION_ID}"
        )

    if len(active_locations) == 1:
        location = active_locations[0]

        print(
            "📍 Automatically using Shopify "
            f"location: {location['name']} "
            f"({location['id']})"
        )
        return location["id"]

    online_locations = [
        location
        for location in active_locations
        if location.get("fulfillsOnlineOrders")
    ]

    if len(online_locations) == 1:
        location = online_locations[0]

        print(
            "📍 Automatically using online "
            f"fulfillment location: "
            f"{location['name']} "
            f"({location['id']})"
        )
        return location["id"]

    print("\n📍 Shopify locations:")

    for location in active_locations:
        print(
            f"   {location['name']}: "
            f"{location['id']} "
            f"(online="
            f"{location.get('fulfillsOnlineOrders')})"
        )

    raise RuntimeError(
        "❌ Multiple Shopify locations exist. "
        "Set LOCATION_ID explicitly."
    )


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


def load_shopify_catalog() -> Dict[str, Any]:
    print("\n📚 Loading Shopify product catalog...")

    catalog = {
        "products": {},
        "by_sku": {},
        "by_barcode": {},
        "by_handle": {},
    }

    after = None
    page = 0

    while True:
        page += 1

        data = graphql(
            PRODUCTS_QUERY,
            {"after": after},
        )

        products_data = data.get(
            "products",
            {},
        )

        nodes = products_data.get(
            "nodes",
            [],
        )

        for product in nodes:
            product_id = product.get("id")

            if not product_id:
                continue

            catalog["products"][product_id] = product

            handle = product.get("handle")

            if handle:
                catalog["by_handle"][
                    normalise_key(handle)
                ] = product

            variants = (
                product
                .get("variants", {})
                .get("nodes", [])
            )

            for variant in variants:
                sku = (
                    variant.get("sku")
                    or variant.get(
                        "inventoryItem",
                        {},
                    ).get("sku")
                )

                barcode = variant.get("barcode")

                if sku:
                    catalog["by_sku"][
                        normalise_key(sku)
                    ] = product

                if barcode:
                    catalog["by_barcode"][
                        normalise_key(barcode)
                    ] = product

        page_info = products_data.get(
            "pageInfo",
            {},
        )

        if not page_info.get("hasNextPage"):
            break

        after = page_info.get("endCursor")

        if not after:
            break

        print(
            f"   Shopify catalog page "
            f"{page} loaded..."
        )

    print(
        f"✅ Shopify catalog loaded: "
        f"{len(catalog['products'])} products"
    )

    print(
        f"   SKU index: "
        f"{len(catalog['by_sku'])}"
    )

    print(
        f"   Barcode index: "
        f"{len(catalog['by_barcode'])}"
    )

    return catalog


# ============================================================
# BUILD SOURCE PRODUCT
# ============================================================

def build_source_product(
    p: Dict[str, Any],
) -> Dict[str, Any]:

    title = (
        clean_text(p.get("title"))
        or f"Product-{clean_text(p.get('sku'))}"
    )

    handle = slugify(title)

    cost = to_float(
        p.get("costprice")
    )

    weight = to_float(
        p.get("weight")
    )

    stock_qty = max(
        0,
        to_int(p.get("stock")),
    )

    barcode = (
        clean_text(p.get("barcode"))
        or None
    )

    sku = (
        clean_text(p.get("sku"))
        or None
    )

    price = calc_price(
        cost,
        weight,
    )

    vendor = last_value(
        p.get("productbrand")
    )

    product_type = last_value(
        p.get("productrange")
    )

    tags = sanitize_tags(
        split_tags(p.get("productbrand"))
        + split_tags(p.get("productrange"))
        + TAGS_TO_INCLUDE
    )

    description = build_description(p)

    raw_images = clean_text(
        p.get("imageoffloads")
    )

    images = [
        image.strip()
        for image in re.split(
            r"[|,]+",
            raw_images,
        )
        if valid_image(image)
    ]

    sizes = dedupe_preserve_order(
        split_values(
            p.get("sizeattribute")
        )
    )

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
# PRODUCT MATCHING
# ============================================================

def find_existing_product(
    source: Dict[str, Any],
    catalog: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

    sku = normalise_key(
        source.get("sku")
    )

    barcode = normalise_key(
        source.get("barcode")
    )

    handle = normalise_key(
        source.get("handle")
    )

    if MATCH_BY_SKU and sku:
        product = catalog["by_sku"].get(sku)

        if product:
            return product

    if MATCH_BY_BARCODE and barcode:
        product = catalog["by_barcode"].get(
            barcode
        )

        if product:
            return product

    if MATCH_BY_HANDLE and handle:
        product = catalog["by_handle"].get(
            handle
        )

        if product:
            return product

    return None


# ============================================================
# PRODUCT UPDATE
# ============================================================

PRODUCT_UPDATE_MUTATION = """
mutation ProductUpdate(
    $product: ProductUpdateInput!
) {
    productUpdate(
        product: $product
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
        }
    }
}
"""


def update_product(
    source: Dict[str, Any],
    product: Dict[str, Any],
) -> None:

    input_data = {
        "id": product["id"],
        "title": source["title"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "tags": source["tags"],
        "status": (
            "ACTIVE"
            if source["stock"] > 0
            else "DRAFT"
        ),
    }

    if UPDATE_EXISTING_HANDLES:
        input_data["handle"] = source["handle"]

    data = graphql(
        PRODUCT_UPDATE_MUTATION,
        {
            "product": input_data,
        },
    )

    payload = data.get(
        "productUpdate"
    )

    if not payload:
        raise RuntimeError(
            "Shopify returned no "
            "productUpdate payload."
        )

    errors = payload.get(
        "userErrors",
        [],
    )

    if errors:
        raise RuntimeError(
            "Shopify productUpdate errors: "
            + str(errors)
        )


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

        userErrors {
            field
            message
        }
    }
}
"""


def update_variant(
    product_id: str,
    variant_id: str,
    source: Dict[str, Any],
) -> Dict[str, Any]:

    inventory_item = {
        "tracked": True,
        "cost": str(source["cost"]),
        "measurement": {
            "weight": {
                "value": source["weight"],
                "unit": "GRAMS",
            }
        },
    }

    if source.get("sku"):
        inventory_item["sku"] = source["sku"]

    variant_input = {
        "id": variant_id,
        "price": str(source["price"]),
        "inventoryItem": inventory_item,
    }

    if source.get("barcode"):
        variant_input["barcode"] = (
            source["barcode"]
        )

    data = graphql(
        VARIANTS_UPDATE_MUTATION,
        {
            "productId": product_id,
            "variants": [variant_input],
        },
    )

    payload = data.get(
        "productVariantsBulkUpdate"
    )

    if not payload:
        raise RuntimeError(
            "Shopify returned no "
            "productVariantsBulkUpdate payload."
        )

    errors = payload.get(
        "userErrors",
        [],
    )

    if errors:
        raise RuntimeError(
            "Shopify variant update errors: "
            + str(errors)
        )

    variants = payload.get(
        "productVariants",
        [],
    )

    return (
        variants[0]
        if variants
        else {}
    )


# ============================================================
# INVENTORY
# ============================================================

INVENTORY_LEVEL_QUERY = """
query InventoryLevel(
    $inventoryItemId: ID!,
    $locationId: ID!
) {
    inventoryItem(id: $inventoryItemId) {
        id
        tracked

        inventoryLevel(
            locationId: $locationId
        ) {
            id

            quantities(
                names: ["available"]
            ) {
                name
                quantity
            }
        }
    }
}
"""


def get_inventory_level(
    inventory_item_id: str,
    location_id: str,
) -> Optional[int]:

    data = graphql(
        INVENTORY_LEVEL_QUERY,
        {
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
        },
        retry_transient=False,
    )

    item = data.get(
        "inventoryItem"
    )

    if not item:
        return None

    level = item.get(
        "inventoryLevel"
    )

    if not level:
        return None

    quantities = level.get(
        "quantities",
        [],
    )

    for quantity in quantities:
        if quantity.get("name") == "available":
            return quantity.get("quantity")

    return None


# ------------------------------------------------------------
# INVENTORY ACTIVATION
# ------------------------------------------------------------

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

            quantities(
                names: ["available"]
            ) {
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


def activate_inventory(
    inventory_item_id: str,
    location_id: str,
    initial_quantity: int = 0,
) -> None:

    data = graphql(
        INVENTORY_ACTIVATE_MUTATION,
        {
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
            "available": initial_quantity,
        },
        retry_transient=False,
    )

    payload = data.get(
        "inventoryActivate"
    )

    if not payload:
        raise RuntimeError(
            "Shopify returned no "
            "inventoryActivate payload."
        )

    errors = payload.get(
        "userErrors",
        [],
    )

    if errors:
        error_text = str(errors)

        # Another worker/process may have activated it.
        if "already" in error_text.lower():
            return

        raise RuntimeError(
            "Inventory activation errors: "
            + error_text
        )


# ------------------------------------------------------------
# INVENTORY SET
# ------------------------------------------------------------

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
            code
            field
            message
        }
    }
}
"""


def set_inventory(
    inventory_item_id: str,
    location_id: str,
    quantity: int,
    compare_quantity: Optional[int],
) -> None:

    quantity = max(
        0,
        int(quantity),
    )

    quantities = {
        "inventoryItemId": inventory_item_id,
        "locationId": location_id,
        "quantity": quantity,
    }

    if compare_quantity is not None:
        quantities["compareQuantity"] = int(
            compare_quantity
        )

    idempotency_key = str(
        uuid.uuid4()
    )

    input_data = {
        "name": "available",
        "reason": "correction",
        "referenceDocumentUri": (
            "https://honeylade-sync.local/"
            + idempotency_key
        ),
        "quantities": [
            quantities
        ],
    }

    data = graphql(
        INVENTORY_SET_MUTATION,
        {
            "input": input_data,
            "idempotencyKey": idempotency_key,
        },
    )

    payload = data.get(
        "inventorySetQuantities"
    )

    if not payload:
        raise RuntimeError(
            "Shopify returned no "
            "inventorySetQuantities payload."
        )

    errors = payload.get(
        "userErrors",
        [],
    )

    if errors:
        error_text = str(errors)

        if (
            "compare" in error_text.lower()
            or "quantity" in error_text.lower()
        ):
            raise RuntimeError(
                "INVENTORY_COMPARE_FAILED: "
                + error_text
            )

        raise RuntimeError(
            "Inventory update errors: "
            + error_text
        )


def ensure_inventory(
    inventory_item_id: str,
    location_id: str,
    quantity: int,
) -> None:

    lock = get_inventory_lock(
        inventory_item_id,
        location_id,
    )

    with lock:
        current_quantity = get_inventory_level(
            inventory_item_id,
            location_id,
        )

        if current_quantity is None:
            print(
                "   📍 Activating inventory "
                "at selected location..."
            )

            activate_inventory(
                inventory_item_id,
                location_id,
                0,
            )

            current_quantity = get_inventory_level(
                inventory_item_id,
                location_id,
            )

        try:
            set_inventory(
                inventory_item_id,
                location_id,
                quantity,
                current_quantity,
            )

        except RuntimeError as exc:
            if not str(exc).startswith(
                "INVENTORY_COMPARE_FAILED:"
            ):
                raise

            print(
                "   🔁 Inventory changed "
                "during update; refreshing..."
            )

            latest_quantity = get_inventory_level(
                inventory_item_id,
                location_id,
            )

            set_inventory(
                inventory_item_id,
                location_id,
                quantity,
                latest_quantity,
            )


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
                    hasVariants
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

        userErrors {
            field
            message
        }
    }
}
"""


def create_product(
    source: Dict[str, Any],
) -> Dict[str, Any]:

    product_input = {
        "title": source["title"],
        "handle": source["handle"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "tags": source["tags"],
        "status": (
            "ACTIVE"
            if source["stock"] > 0
            else "DRAFT"
        ),
    }

    if (
        CREATE_SIZE_VARIANTS_FOR_NEW_PRODUCTS
        and source["sizes"]
    ):
        product_input["productOptions"] = [
            {
                "name": SIZE_OPTION_NAME,
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
        },
    )

    payload = data.get(
        "productCreate"
    )

    if not payload:
        raise RuntimeError(
            "Shopify returned no "
            "productCreate payload."
        )

    errors = payload.get(
        "userErrors",
        [],
    )

    if errors:
        raise RuntimeError(
            "Shopify productCreate errors: "
            + str(errors)
        )

    product = payload.get(
        "product"
    )

    if not product:
        raise RuntimeError(
            "Shopify created no product."
        )

    return product


# ============================================================
# CREATE MISSING VARIANTS
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

            options {
                id
                name
                position

                optionValues {
                    id
                    name
                    hasVariants
                }
            }
        }

        productVariants {
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

        userErrors {
            field
            message
        }
    }
}
"""


def get_option(
    product: Dict[str, Any],
    option_name: str,
) -> Optional[Dict[str, Any]]:
    wanted = normalise_key(option_name)

    for option in product.get("options", []):
        if normalise_key(option.get("name")) == wanted:
            return option

    return None


def create_missing_variants(
    product: Dict[str, Any],
    source: Dict[str, Any],
) -> List[Dict[str, Any]]:
    sizes = dedupe_preserve_order(source["sizes"])

    if not sizes:
        return []

    size_option = get_option(product, SIZE_OPTION_NAME)

    if not size_option:
        if CREATE_MISSING_SIZE_VARIANTS_FOR_EXISTING_PRODUCTS:
            raise RuntimeError(
                f"Product does not have a '{SIZE_OPTION_NAME}' option. "
                "Automatic option creation is disabled by this "
                "variant creation function."
            )

        print(
            f"   ℹ️ Product has no "
            f"'{SIZE_OPTION_NAME}' option; "
            "skipping size variant creation."
        )

        return []

    existing_values = set()

    for variant in (
        product
        .get("variants", {})
        .get("nodes", [])
    ):
        for option in variant.get("selectedOptions", []):
            if (
                normalise_key(option.get("name")) == normalise_key(SIZE_OPTION_NAME)
            ):
                existing_values.add(normalise_key(option.get("value")))

    missing_sizes = [
        size for size in sizes if normalise_key(size) not in existing_values
    ]

    if not missing_sizes:
        return []

    variants_input = []

    for size in missing_sizes:
        inventory_item = {
            "tracked": True,
            "cost": str(source["cost"]),
            "measurement": {
                "weight": {
                    "value": source["weight"],
                    "unit": "GRAMS",
                }
            },
        }

        if source.get("sku"):
            inventory_item["sku"] = source["sku"]

        variant_input = {
            "optionValues": [
                {
                    "name": size,
                    "optionName": SIZE_OPTION_NAME,
                }
            ],
            "price": str(source["price"]),
            "inventoryItem": inventory_item,
        }

        if source.get("barcode"):
            variant_input["barcode"] = source["barcode"]

        variants_input.append(variant_input)

    data = graphql(
        VARIANTS_CREATE_MUTATION,
        {
            "productId": product["id"],
            "variants": variants_input,
        },
    )

    payload = data.get("productVariantsBulkCreate")

    if not payload:
        raise RuntimeError(
            "Shopify returned no "
            "productVariantsBulkCreate payload."
        )

    errors = payload.get("userErrors", [])

    if errors:
        raise RuntimeError(
            "Shopify variant creation errors: "
            + str(errors)
        )

    return payload.get("productVariants", [])


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
                hasVariants
            }
        }

        variants(first: 250) {
            nodes {
                id
                title
                sku
                barcode
                price

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


def find_product_after_change(
    product_id: str,
) -> Optional[Dict[str, Any]]:
    data = graphql(
        PRODUCT_BY_ID_QUERY,
        {
            "id": product_id
        },
    )

    return data.get("product")


# ============================================================
# VARIANT MATCHING
# ============================================================

def find_matching_variant(
    product: Dict[str, Any],
    source: Dict[str, Any],
    size: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    variants = (
        product
        .get("variants", {})
        .get("nodes", [])
    )

    # Size match
    if size:
        wanted_size = normalise_key(size)

        for variant in variants:
            for option in variant.get("selectedOptions", []):
                if (
                    normalise_key(option.get("name")) == normalise_key(SIZE_OPTION_NAME)
                    and normalise_key(option.get("value")) == wanted_size
                ):
                    return variant

    # SKU match
    source_sku = normalise_key(source.get("sku"))

    if source_sku:
        for variant in variants:
            sku = (
                variant.get("sku")
                or variant.get("inventoryItem", {}).get("sku")
            )

            if sku and normalise_key(sku) == source_sku:
                return variant

    # Barcode match
    source_barcode = normalise_key(source.get("barcode"))

    if source_barcode:
        for variant in variants:
            barcode = variant.get("barcode")

            if barcode and normalise_key(barcode) == source_barcode:
                return variant

    # Single variant fallback
    if len(variants) == 1:
        return variants[0]

    return None


# ============================================================
# PRODUCT INDEX UPDATE
# ============================================================

def add_product_to_catalog(
    catalog: Dict[str, Any],
    product: Dict[str, Any],
) -> None:
    product_id = product.get("id")

    if not product_id:
        return

    catalog["products"][product_id] = product

    handle = product.get("handle")

    if handle:
        catalog["by_handle"][normalise_key(handle)] = product

    for variant in (
        product
        .get("variants", {})
        .get("nodes", [])
    ):
        sku = (
            variant.get("sku")
            or variant.get("inventoryItem", {}).get("sku")
        )

        barcode = variant.get("barcode")

        if sku:
            catalog["by_sku"][normalise_key(sku)] = product

        if barcode:
            catalog["by_barcode"][normalise_key(barcode)] = product


# ============================================================
# SYNC ONE PRODUCT
# ============================================================

def sync_product(
    index: int,
    total: int,
    source_item: Dict[str, Any],
    catalog: Dict[str, Any],
    location_id: str,
) -> Dict[str, Any]:
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

    with _product_identity_lock:
        product = find_existing_product(source, catalog)
        created = False

        if product:
            print(f"🔄 Existing product found: {product['title']}")
            print(f"   Shopify ID: {product['id']}")
            update_product(source, product)
        else:
            print("🆕 Product not found - creating...")
            product = create_product(source)
            created = True
            print(f"✅ Product created: {product['title']}")
            print(f"   Shopify ID: {product['id']}")

        add_product_to_catalog(catalog, product)

    refreshed = find_product_after_change(product["id"])

    if refreshed:
        product = refreshed
        add_product_to_catalog(catalog, product)

    if source["sizes"] and not created and CREATE_MISSING_SIZE_VARIANTS_FOR_EXISTING_PRODUCTS:
        created_variants = create_missing_variants(product, source)

        if created_variants:
            print(f"   ➕ Created {len(created_variants)} missing variant(s)")
            refreshed = find_product_after_change(product["id"])

            if refreshed:
                product = refreshed
                add_product_to_catalog(catalog, product)

    if source["sizes"] and created and CREATE_SIZE_VARIANTS_FOR_NEW_PRODUCTS:
        created_variants = create_missing_variants(product, source)

        if created_variants:
            print(f"   ➕ Created {len(created_variants)} additional size variant(s)")
            refreshed = find_product_after_change(product["id"])

            if refreshed:
                product = refreshed
                add_product_to_catalog(catalog, product)

    variants = product.get("variants", {}).get("nodes", [])

    if not variants:
        raise RuntimeError("Product has no variants.")

    processed_variants = 0
    has_size_option = get_option(product, SIZE_OPTION_NAME) is not None

    if source["sizes"] and has_size_option:
        for size in source["sizes"]:
            variant = find_matching_variant(product, source, size=size)

            if not variant:
                print(f"⚠️ Could not match variant size '{size}'")
                continue

            updated_variant = update_variant(product["id"], variant["id"], source)
            inventory_item_id = updated_variant.get("inventoryItem", {}).get("id") or variant.get("inventoryItem", {}).get("id")

            if not inventory_item_id:
                raise RuntimeError(f"No inventory item ID for variant {variant['id']}")

            ensure_inventory(inventory_item_id, location_id, source["stock"])
            processed_variants += 1
    else:
        variant = find_matching_variant(product, source)

        if not variant:
            raise RuntimeError("Could not match product variant.")

        updated_variant = update_variant(product["id"], variant["id"], source)
        inventory_item_id = updated_variant.get("inventoryItem", {}).get("id") or variant.get("inventoryItem", {}).get("id")

        if not inventory_item_id:
            raise RuntimeError(f"No inventory item ID for variant {variant['id']}")

        ensure_inventory(inventory_item_id, location_id, source["stock"])
        processed_variants += 1

    if processed_variants == 0:
        raise RuntimeError("No variants were successfully processed.")

    print(f"   ✅ Stock updated: {source['stock']} ({processed_variants} variant(s))")

    return {
        "status": "created" if created else "updated",
        "title": title,
        "handle": source["handle"],
        "sku": source["sku"],
        "barcode": source["barcode"],
        "stock": source["stock"],
        "variants": processed_variants,
    }


# ============================================================
# FAILURE LOG
# ============================================================

def save_failures(
    failures: List[Dict[str, Any]],
) -> None:
    try:
        with open(FAILURE_FILE, "w", encoding="utf-8") as file:
            json.dump(failures, file, indent=2, ensure_ascii=False)

        print(f"\n💾 Failure log written to {FAILURE_FILE}")

    except Exception as exc:
        print(f"⚠️ Could not write failure log: {exc}")


# ============================================================
# SUMMARY
# ============================================================

def save_summary(
    summary: Dict[str, Any],
) -> None:
    try:
        with open(SUMMARY_FILE, "w", encoding="utf-8") as file:
            json.dump(summary, file, indent=2, ensure_ascii=False)

    except Exception as exc:
        print(f"⚠️ Could not write summary: {exc}")


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
    print(f"Size variants for new products: {CREATE_SIZE_VARIANTS_FOR_NEW_PRODUCTS}")
    print(f"Size variants for existing products: {CREATE_MISSING_SIZE_VARIANTS_FOR_EXISTING_PRODUCTS}")
    print(f"Update existing handles: {UPDATE_EXISTING_HANDLES}")
    print(f"Filter by tags: {FILTER_BY_TAGS}")
    print("=" * 70)

    get_access_token()
    print("🔐 Shopify authentication: OK")

    location_id = resolve_location_id()
    items = load_xml()

    if not items:
        print("⚠️ XML feed contains no products.")
        return

    total = len(items)
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

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_map = {}

        for index, item in enumerate(items, start=1):
            future = executor.submit(sync_product, index, total, item, catalog, location_id)
            future_map[future] = item

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
                barcode = clean_text(item.get("barcode"))

                print("\n" + "!" * 70)
                print(f"❌ ERROR syncing {title}")
                print(f"   SKU: {sku}")
                print(f"   Barcode: {barcode}")
                print(f"   Error: {exc}")
                print("!" * 70)

                failures.append({
                    "title": title,
                    "sku": sku,
                    "barcode": barcode,
                    "error": str(exc),
                })

    if failures:
        save_failures(failures)

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    summary = {
        "total": total,
        "completed": completed,
        "created": created,
        "updated": updated,
        "failed": failed,
        "workers": WORKERS,
        "api_version": API_VERSION,
        "location_id": location_id,
        "elapsed_seconds": round(elapsed, 2),
    }

    save_summary(summary)

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
