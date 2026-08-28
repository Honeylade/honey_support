```python
import os
import requests
import xml.etree.ElementTree as ET
import re
import html
import time
import threading
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# CONFIG
# ============================================================

SHOP_URL = os.getenv("SHOP_URL")
XML_URL = os.getenv("XML_URL")

# Recommended current Shopify API version.
# You can override it with SHOPIFY_API_VERSION.
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-07")

# Number of simultaneous product sync jobs.
# Start with 5. Increase to 8-10 only if the shop handles it well.
WORKERS = int(os.getenv("WORKERS", "5"))

# Retry configuration
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "2"))

# HTTP timeout
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))

# Product limit
# LIMIT=None or LIMIT=0 means all products.
LIMIT_ENV = os.getenv("LIMIT")

if LIMIT_ENV in (None, "", "None", "0"):
    LIMIT = None
else:
    LIMIT = int(LIMIT_ENV)

# Optional specific Shopify location ID.
#
# Example:
# LOCATION_ID=gid://shopify/Location/123456789
#
# If blank and there is only one active location, the script
# automatically uses that location.
LOCATION_ID = os.getenv("LOCATION_ID", "").strip()

# Access token options:
#
# 1. Recommended:
#    SHOPIFY_ACCESS_TOKEN
#
# 2. Client credentials:
#    CLIENT_ID
#    CLIENT_SECRET
#
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()

CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip()


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
# GLOBAL TOKEN CACHE
# ============================================================

_token_cache = {
    "access_token": SHOPIFY_ACCESS_TOKEN or None,
    "expires_at": time.time() + 86400 if SHOPIFY_ACCESS_TOKEN else 0,
}

_token_lock = threading.Lock()


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
        if not tag:
            continue

        tag = html.unescape(str(tag))
        tag = tag.replace("&", "and").strip()

        if not tag:
            continue

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
            bullets.append(
                f"<li>{value}</li>"
            )

    bullet_html = ""

    if bullets:
        bullet_html = (
            "<ul>"
            + "".join(bullets)
            + "</ul>"
        )

    standard_description = clean_text(
        product.get("desc_standard")
    )

    paragraph = ""

    if standard_description:
        paragraph = f"<p>{standard_description}</p>"

    return bullet_html + paragraph


def slugify(value: str) -> str:
    value = clean_text(value)

    value = value.lower()

    value = re.sub(
        r"[^a-z0-9]+",
        "-",
        value
    )

    return value.strip("-")


def valid_image(url: str) -> bool:
    if not url:
        return False

    url = url.strip()

    if not (
        url.startswith("http://")
        or url.startswith("https://")
    ):
        return False

    if " " in url:
        return False

    extensions = [
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    ]

    url_lower = url.lower()

    return any(
        ext in url_lower
        for ext in extensions
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
# PRICE LOGIC
# ============================================================

def calc_price(cost, weight):
    cost = float(cost or 0)
    weight = float(weight or 0)

    shipping = (
        3.99
        if weight < 300
        else 4.99
        if weight < 2000
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

    price_after_fees = (
        taxed_price / (1 - FEES)
    )

    final_price = (
        price_after_fees
        + shipping
        + FIXED_COSTS
    )

    return round(final_price, 2)


# ============================================================
# ACCESS TOKEN
# ============================================================

def get_access_token():

    # Static access token
    if SHOPIFY_ACCESS_TOKEN:
        return SHOPIFY_ACCESS_TOKEN

    with _token_lock:

        if (
            _token_cache["access_token"]
            and time.time()
            < _token_cache["expires_at"] - 60
        ):
            return _token_cache["access_token"]

        if not SHOP_URL:
            raise RuntimeError(
                "SHOP_URL is not configured."
            )

        if not CLIENT_ID or not CLIENT_SECRET:
            raise RuntimeError(
                "Neither SHOPIFY_ACCESS_TOKEN nor "
                "CLIENT_ID/CLIENT_SECRET are configured."
            )

        url = (
            f"https://{SHOP_URL}"
            "/admin/oauth/access_token"
        )

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

        if not response.ok:
            raise RuntimeError(
                "Shopify token request failed: "
                f"{response.status_code} "
                f"{response.text[:1000]}"
            )

        data = response.json()

        token = data.get("access_token")

        if not token:
            raise RuntimeError(
                "Shopify did not return an access token."
            )

        expires_in = int(
            data.get("expires_in", 86400)
        )

        _token_cache["access_token"] = token

        _token_cache["expires_at"] = (
            time.time() + expires_in
        )

        return token


# ============================================================
# GRAPHQL REQUEST
# ============================================================

def graphql(
    query: str,
    variables: Optional[Dict[str, Any]] = None,
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

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            # Shopify throttling / temporary errors
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
                        sleep_time = float(
                            retry_after
                        )
                    except ValueError:
                        sleep_time = (
                            RETRY_DELAY * attempt
                        )
                else:
                    sleep_time = (
                        RETRY_DELAY * attempt
                    )

                print(
                    f"⚠️ Shopify HTTP "
                    f"{response.status_code}; "
                    f"waiting {sleep_time:.1f}s "
                    f"(attempt "
                    f"{attempt}/{MAX_RETRIES})"
                )

                time.sleep(sleep_time)

                continue

            response.raise_for_status()

            result = response.json()

            if result.get("errors"):

                errors = result["errors"]

                raise RuntimeError(
                    "GraphQL errors: "
                    + str(errors)
                )

            return result.get("data", {})

        except Exception as exc:

            last_error = exc

            if attempt < MAX_RETRIES:

                sleep_time = (
                    RETRY_DELAY * attempt
                )

                print(
                    f"⚠️ Shopify request failed: "
                    f"{exc}"
                )

                print(
                    f"   Retry in "
                    f"{sleep_time:.1f}s "
                    f"({attempt}/{MAX_RETRIES})"
                )

                time.sleep(sleep_time)

            else:
                break

    raise RuntimeError(
        "Shopify GraphQL request failed "
        f"after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


# ============================================================
# LOAD XML
# ============================================================

def load_xml() -> List[Dict[str, Any]]:

    if not XML_URL:
        raise RuntimeError(
            "XML_URL is not configured."
        )

    print("📥 Downloading XML feed...")

    response = requests.get(
        XML_URL,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    root = ET.fromstring(
        response.content
    )

    items = root.findall(".//post")

    print(
        f"🔎 Found {len(items)} supplier items"
    )

    if LIMIT:
        selected_items = items[:LIMIT]
    else:
        selected_items = items

    products = []

    for item in selected_items:

        data = {}

        for child in item:
            data[
                child.tag.lower()
            ] = clean_text(child.text)

        products.append(data)

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

    data = graphql(
        LOCATIONS_QUERY
    )

    return data["locations"]["nodes"]


def resolve_location_id():

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

    # Explicit LOCATION_ID
    if LOCATION_ID:

        for location in active_locations:

            if location["id"] == LOCATION_ID:

                print(
                    "📍 Inventory location: "
                    f"{location['name']} "
                    f"({location['id']})"
                )

                return location["id"]

        print("\n📍 Shopify locations:")

        for location in active_locations:
            print(
                f"   {location['name']}: "
                f"{location['id']}"
            )

        raise RuntimeError(
            "❌ LOCATION_ID was not found: "
            f"{LOCATION_ID}"
        )

    # Automatically use one location
    if len(active_locations) == 1:

        location = active_locations[0]

        print(
            "📍 Automatically using Shopify "
            f"location: {location['name']} "
            f"({location['id']})"
        )

        return location["id"]

    # Multiple locations
    print(
        "\n📍 Shopify locations:"
    )

    for location in active_locations:

        print(
            f"   {location['name']}: "
            f"{location['id']} "
            f"| Online fulfilment: "
            f"{location.get('fulfillsOnlineOrders')}"
        )

    raise RuntimeError(
        "❌ Multiple Shopify locations exist. "
        "Set LOCATION_ID."
    )


# ============================================================
# PRODUCT LOOKUP
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

                    inventoryQuantity
                }
            }
        }
    }
}
"""


def find_product_by_handle(
    handle: str,
) -> Optional[Dict[str, Any]]:

    data = graphql(
        PRODUCT_QUERY,
        {
            "query": f"handle:{handle}"
        },
    )

    products = (
        data
        .get("products", {})
        .get("nodes", [])
    )

    return (
        products[0]
        if products
        else None
    )


# ============================================================
# PRODUCT DATA
# ============================================================

def build_source_product(
    p: Dict[str, Any]
) -> Dict[str, Any]:

    title = (
        clean_text(p.get("title"))
        or
        f"Product-{clean_text(p.get('sku'))}"
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
        to_int(p.get("stock"))
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
        weight
    )

    vendor = last_value(
        p.get("productbrand")
    )

    product_type = last_value(
        p.get("productrange")
    )

    tags = sanitize_tags(
        split_tags(
            p.get("productbrand")
        )
        +
        split_tags(
            p.get("productrange")
        )
        +
        TAGS_TO_INCLUDE
        +
        split_tags(title)
    )

    description = build_description(p)

    raw_images = clean_text(
        p.get("imageoffloads")
    )

    images = [
        img.strip()
        for img in re.split(
            r"[|,]+",
            raw_images
        )
        if valid_image(img)
    ]

    sizes = [
        s.strip()
        for s in re.split(
            r"[|,]+",
            clean_text(
                p.get("sizeattribute")
            )
        )
        if s.strip()
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
# UPDATE PRODUCT INFORMATION
#
# IMPORTANT:
# We use ProductUpdateInput here.
#
# We DO NOT use productSet.
# This avoids the:
#
# "Product options input is required when updating variants"
#
# error.
# ============================================================

PRODUCT_UPDATE_MUTATION = """
mutation ProductUpdate(
    $productId: ID!,
    $product: ProductUpdateInput!
) {
    productUpdate(
        product: $product,
        identifier: {
            id: $productId
        }
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
    product_id: str,
    source: Dict[str, Any],
):

    # Only product-level fields here.
    #
    # Variants are handled separately.
    product_input = {
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

    data = graphql(
        PRODUCT_UPDATE_MUTATION,
        {
            "productId": product_id,
            "product": product_input,
        },
    )

    result = data["productUpdate"]

    errors = result.get(
        "userErrors",
        []
    )

    if errors:

        messages = [
            (
                f"{e.get('field')}: "
                f"{e.get('message')}"
            )
            for e in errors
        ]

        raise RuntimeError(
            "Product update errors: "
            + "; ".join(messages)
        )

    return result["product"]


# ============================================================
# UPDATE VARIANTS
#
# Shopify recommends productVariantsBulkUpdate
# for updating variants.
# ============================================================

VARIANTS_UPDATE_MUTATION = """
mutation ProductVariantsBulkUpdate(
    $productId: ID!,
    $variants: [ProductVariantsBulkInput!]!
) {
    productVariantsBulkUpdate(
        productId: $productId,
        variants: $variants,
        allowPartialUpdates: true
    ) {
        product {
            id
        }

        productVariants {
            id
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


def update_variants(
    product: Dict[str, Any],
    source: Dict[str, Any],
):

    variants = product.get(
        "variants",
        {}
    ).get(
        "nodes",
        []
    )

    if not variants:
        raise RuntimeError(
            "Shopify product has no variants."
        )

    supplier_sku = source["sku"]
    supplier_barcode = source["barcode"]

    variant_inputs = []

    # --------------------------------------------------------
    # BEST MATCH:
    # SKU first, barcode second.
    # --------------------------------------------------------

    matched_variant = None

    if supplier_sku:

        for variant in variants:

            if (
                variant.get("sku")
                and str(
                    variant["sku"]
                ).strip()
                == str(
                    supplier_sku
                ).strip()
            ):
                matched_variant = variant
                break

    if (
        matched_variant is None
        and supplier_barcode
    ):

        for variant in variants:

            if (
                variant.get("barcode")
                and str(
                    variant["barcode"]
                ).strip()
                == str(
                    supplier_barcode
                ).strip()
            ):
                matched_variant = variant
                break

    # --------------------------------------------------------
    # If only one Shopify variant exists,
    # safely use it.
    # --------------------------------------------------------

    if (
        matched_variant is None
        and len(variants) == 1
    ):
        matched_variant = variants[0]

    if matched_variant is None:

        raise RuntimeError(
            "Could not match supplier SKU/barcode "
            "to a Shopify variant."
        )

    variant_input = {
        "id": matched_variant["id"],
        "price": str(
            source["price"]
        ),
        "sku": supplier_sku,
        "barcode": supplier_barcode,
    }

    # Shopify expects weight as value/unit
    # on ProductVariantsBulkInput in supported API versions.
    #
    # If your store/API rejects these fields, remove
    # the following block; inventory syncing will still work.
    if source["weight"] > 0:

        variant_input["inventoryItem"] = {
            "measurement": {
                "weight": {
                    "value": source["weight"],
                    "unit": "GRAMS",
                }
            }
        }

    variant_inputs.append(
        variant_input
    )

    data = graphql(
        VARIANTS_UPDATE_MUTATION,
        {
            "productId": product["id"],
            "variants": variant_inputs,
        },
    )

    result = data[
        "productVariantsBulkUpdate"
    ]

    errors = result.get(
        "userErrors",
        []
    )

    if errors:

        messages = [
            (
                f"{e.get('field')}: "
                f"{e.get('message')}"
            )
            for e in errors
        ]

        raise RuntimeError(
            "Variant update errors: "
            + "; ".join(messages)
        )

    return matched_variant


# ============================================================
# INVENTORY
#
# This is the important part for stock.
#
# Shopify inventory is NOT updated by changing
# inventoryQuantity on Product/Variant.
#
# We update the InventoryLevel at the selected location.
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
            referenceDocumentUri

            changes {
                name
                delta
            }
        }

        userErrors {
            field
            message
        }
    }
}
"""


def update_inventory(
    inventory_item_id: str,
    location_id: str,
    quantity: int,
):

    input_data = {
        "reason": "correction",
        "name": "available",
        "quantities": [
            {
                "inventoryItemId": inventory_item_id,
                "locationId": location_id,
                "quantity": int(quantity),
            }
        ],
    }

    data = graphql(
        INVENTORY_SET_MUTATION,
        {
            "input": input_data
        },
    )

    result = data[
        "inventorySetQuantities"
    ]

    errors = result.get(
        "userErrors",
        []
    )

    if errors:

        messages = [
            (
                f"{e.get('field')}: "
                f"{e.get('message')}"
            )
            for e in errors
        ]

        raise RuntimeError(
            "Inventory update errors: "
            + "; ".join(messages)
        )

    return True


# ============================================================
# CREATE PRODUCT
# ============================================================

PRODUCT_CREATE_MUTATION = """
mutation ProductCreate(
    $product: ProductCreateInput!
) {
    productCreate(
        product: $product
    ) {
        product {
            id
            title
            handle

            variants(first: 10) {
                nodes {
                    id
                    sku
                    barcode
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


def create_product(
    source: Dict[str, Any]
):

    # --------------------------------------------------------
    # Product creation
    # --------------------------------------------------------

    product_input = {
        "title": source["title"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "handle": source["handle"],
        "tags": source["tags"],
        "status": (
            "ACTIVE"
            if source["stock"] > 0
            else "DRAFT"
        ),
    }

    # --------------------------------------------------------
    # Product options
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

    data = graphql(
        PRODUCT_CREATE_MUTATION,
        {
            "product": product_input
        },
    )

    result = data["productCreate"]

    errors = result.get(
        "userErrors",
        []
    )

    if errors:

        messages = [
            (
                f"{e.get('field')}: "
                f"{e.get('message')}"
            )
            for e in errors
        ]

        raise RuntimeError(
            "Product creation errors: "
            + "; ".join(messages)
        )

    return result["product"]


# ============================================================
# FIND INVENTORY ITEM FOR VARIANT
# ============================================================

VARIANT_LOOKUP_QUERY = """
query VariantLookup(
    $id: ID!
) {
    productVariant(id: $id) {
        id
        sku
        barcode
        inventoryItem {
            id
            tracked
        }
    }
}
"""


def get_inventory_item_id(
    variant_id: str
):

    data = graphql(
        VARIANT_LOOKUP_QUERY,
        {
            "id": variant_id
        },
    )

    variant = data.get(
        "productVariant"
    )

    if not variant:
        return None

    inventory_item = variant.get(
        "inventoryItem"
    )

    if not inventory_item:
        return None

    return inventory_item.get("id")


# ============================================================
# SYNC EXISTING PRODUCT
# ============================================================

def sync_existing_product(
    product: Dict[str, Any],
    source: Dict[str, Any],
    location_id: str,
):

    # 1. Product-level information
    update_product(
        product["id"],
        source,
    )

    # 2. Price/SKU/barcode/variant information
    matched_variant = update_variants(
        product,
        source,
    )

    # 3. Inventory
    inventory_item_id = (
        matched_variant
        .get("inventoryItem", {})
        .get("id")
    )

    if not inventory_item_id:

        inventory_item_id = (
            get_inventory_item_id(
                matched_variant["id"]
            )
        )

    if not inventory_item_id:

        raise RuntimeError(
            "Could not determine Shopify "
            "inventory item ID."
        )

    update_inventory(
        inventory_item_id,
        location_id,
        source["stock"],
    )


# ============================================================
# SYNC ONE PRODUCT
# ============================================================

def sync_product(
    source_item: Dict[str, Any],
    location_id: str,
    index: int,
    total: int,
):

    source = build_source_product(
        source_item
    )

    title = source["title"]

    print(
        "\n"
        + "=" * 70
    )

    print(
        f"[{index}/{total}]"
    )

    print(
        "=" * 70
    )

    print(
        f"📦 {title}"
    )

    print(
        f"   SKU: {source['sku']}"
    )

    print(
        f"   Barcode: {source['barcode']}"
    )

    print(
        f"   Cost: £{source['cost']:.2f}"
    )

    print(
        f"   Price: £{source['price']:.2f}"
    )

    print(
        f"   Weight: {source['weight']}g"
    )

    print(
        f"   Stock: {source['stock']}"
    )

    try:

        existing = find_product_by_handle(
            source["handle"]
        )

        if existing:

            print(
                f"🔄 Existing product found: "
                f"{existing['title']}"
            )

            print(
                f"   Shopify ID: "
                f"{existing['id']}"
            )

            sync_existing_product(
                existing,
                source,
                location_id,
            )

            print(
                "✅ Product, variant and "
                "inventory updated."
            )

            return {
                "status": "updated",
                "title": title,
                "index": index,
            }

        # ----------------------------------------------------
        # CREATE
        # ----------------------------------------------------

        print(
            "🆕 Product not found - creating..."
        )

        created_product = create_product(
            source
        )

        print(
            f"   Created Shopify ID: "
            f"{created_product['id']}"
        )

        # ----------------------------------------------------
        # New product creation returns variants.
        # ----------------------------------------------------

        variants = (
            created_product
            .get("variants", {})
            .get("nodes", [])
        )

        if not variants:

            raise RuntimeError(
                "Product was created but "
                "no variant was returned."
            )

        # Match the first/appropriate variant.
        matched_variant = None

        if source["sku"]:

            for variant in variants:

                if (
                    variant.get("sku")
                    and str(
                        variant["sku"]
                    ).strip()
                    == str(
                        source["sku"]
                    ).strip()
                ):
                    matched_variant = variant
                    break

        if (
            matched_variant is None
            and len(variants) == 1
        ):
            matched_variant = variants[0]

        if matched_variant:

            inventory_item_id = (
                matched_variant
                .get("inventoryItem", {})
                .get("id")
            )

            if inventory_item_id:

                update_inventory(
                    inventory_item_id,
                    location_id,
                    source["stock"],
                )

        print(
            "✅ Product created and inventory set."
        )

        return {
            "status": "created",
            "title": title,
            "index": index,
        }

    except Exception as exc:

        print(
            f"❌ ERROR syncing {title}: {exc}"
        )

        return {
            "status": "failed",
            "title": title,
            "index": index,
            "error": str(exc),
        }


# ============================================================
# MAIN SYNC
# ============================================================

def run_sync():

    print(
        "\n"
        + "=" * 70
    )

    print(
        "🚀 SHOPIFY SUPPLIER SYNC"
    )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # Validate config
    # --------------------------------------------------------

    if not SHOP_URL:

        raise RuntimeError(
            "SHOP_URL is not configured."
        )

    if not XML_URL:

        raise RuntimeError(
            "XML_URL is not configured."
        )

    print(
        f"🏪 Store: {SHOP_URL}"
    )

    print(
        f"🔌 API version: {API_VERSION}"
    )

    print(
        f"👷 Workers: {WORKERS}"
    )

    print(
        f"🔁 Max retries: {MAX_RETRIES}"
    )

    # --------------------------------------------------------
    # Token
    # --------------------------------------------------------

    get_access_token()

    print(
        "🔐 Shopify authentication OK"
    )

    # --------------------------------------------------------
    # Location
    # --------------------------------------------------------

    location_id = resolve_location_id()

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    items = load_xml()

    if not items:

        print(
            "⚠️ XML feed contains no products."
        )

        return

    total = len(items)

    print(
        "\n"
        + "=" * 70
    )

    print(
        f"🚀 Starting sync of {total} products..."
    )

    print(
        f"👷 Using {WORKERS} workers"
    )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # Counters
    # --------------------------------------------------------

    created = 0
    updated = 0
    failed = 0

    failures = []

    # --------------------------------------------------------
    # Thread pool
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {}

        for index, item in enumerate(
            items,
            start=1
        ):

            future = executor.submit(
                sync_product,
                item,
                location_id,
                index,
                total,
            )

            futures[future] = item

        # ----------------------------------------------------
        # Results
        # ----------------------------------------------------

        for future in as_completed(
            futures
        ):

            item = futures[future]

            try:

                result = future.result()

                status = result.get(
                    "status"
                )

                if status == "created":

                    created += 1

                elif status == "updated":

                    updated += 1

                else:

                    failed += 1

                    failures.append(
                        result
                    )

            except Exception as exc:

                failed += 1

                title = clean_text(
                    item.get("title")
                )

                failure = {
                    "status": "failed",
                    "title": title,
                    "index": 0,
                    "error": str(exc),
                }

                failures.append(
                    failure
                )

                print(
                    f"❌ Worker failure "
                    f"for {title}: "
                    f"{exc}"
                )

    # ========================================================
    # FINAL REPORT
    # ========================================================

    print(
        "\n"
        + "=" * 70
    )

    print(
        "✅ SYNC COMPLETE"
    )

    print(
        "=" * 70
    )

    print(
        f"🆕 Created: {created}"
    )

    print(
        f"🔄 Updated: {updated}"
    )

    print(
        f"❌ Failed:  {failed}"
    )

    print(
        f"📦 Total:   {total}"
    )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # Failure report
    # --------------------------------------------------------

    if failures:

        print(
            "\n"
            + "=" * 70
        )

        print(
            "❌ FAILED PRODUCTS"
        )

        print(
            "=" * 70
        )

        for failure in failures:

            print(
                f"[{failure.get('index', '?')}] "
                f"{failure.get('title', 'Unknown')}"
            )

            print(
                f"   Error: "
                f"{failure.get('error', 'Unknown error')}"
            )

            print(
                "-" * 70
            )

        print(
            f"\n⚠️ {len(failures)} "
            "products failed."
        )

    else:

        print(
            "\n🎉 No failed products."
        )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        run_sync()

    except KeyboardInterrupt:

        print(
            "\n🛑 Sync cancelled by user."
        )

    except Exception as exc:

        print(
            "\n❌ SYNC STOPPED:"
        )

        print(
            str(exc)
        )
```
