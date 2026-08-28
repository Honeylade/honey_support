import os
import re
import html
import time
import requests
import xml.etree.ElementTree as ET

from typing import Optional, Dict, List, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

# ============================================================

# CONFIGURATION

# ============================================================

SHOP_URL = (os.getenv("SHOP_URL") or "").strip().replace("https://", "").rstrip("/")
XML_URL = (os.getenv("XML_URL") or "").strip()

API_VERSION = os.getenv("API_VERSION", "2024-01").strip()

# Number of products to process.

# Empty / None / 0 = ALL

LIMIT_RAW = os.getenv("LIMIT", "").strip()

if not LIMIT_RAW or LIMIT_RAW.lower() in ("none", "0", "all"):
LIMIT = None
else:
try:
LIMIT = int(LIMIT_RAW)
except ValueError:
raise RuntimeError("LIMIT must be a number, 0, None, or all.")

# Worker count.

# 5 is a sensible starting point for ~5,000 products.

try:
WORKERS = int(os.getenv("WORKERS", "5"))
except ValueError:
WORKERS = 5

WORKERS = max(1, min(WORKERS, 10))

# Shopify location.

# Can be:

# numeric ID: 123456789

# GID: gid://shopify/Location/123456789

LOCATION_ID = (os.getenv("LOCATION_ID") or "").strip()

# Retry configuration.

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "2"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))

# Whether to update product images.

UPDATE_IMAGES = os.getenv("UPDATE_IMAGES", "false").lower() == "true"

# Whether to update descriptions, vendor, type and tags.

UPDATE_PRODUCT_DATA = os.getenv(
"UPDATE_PRODUCT_DATA",
"true"
).lower() == "true"

# ============================================================

# REQUIRED ENVIRONMENT VARIABLES

# ============================================================

if not SHOP_URL:
raise RuntimeError("SHOP_URL is not configured.")

if not XML_URL:
raise RuntimeError("XML_URL is not configured.")

CLIENT_ID = (os.getenv("CLIENT_ID") or "").strip()
CLIENT_SECRET = (os.getenv("CLIENT_SECRET") or "").strip()

if not CLIENT_ID:
raise RuntimeError("CLIENT_ID is not configured.")

if not CLIENT_SECRET:
raise RuntimeError("CLIENT_SECRET is not configured.")

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

# HTTP SESSION

# ============================================================

SESSION = requests.Session()

adapter = HTTPAdapter(
pool_connections=max(WORKERS * 2, 10),
pool_maxsize=max(WORKERS * 2, 10),
)

SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

BASE_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}"

# ============================================================

# ACCESS TOKEN MANAGEMENT

# ============================================================

_token_cache = {
"access_token": None,
"expires_at": 0,
}

def get_access_token() -> str:
"""
Obtain Shopify client-credentials access token.

```
The token is cached until shortly before expiry.
"""

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

response = SESSION.post(
    url,
    json=payload,
    timeout=REQUEST_TIMEOUT,
)

if response.status_code >= 400:
    raise RuntimeError(
        f"Shopify access-token request failed "
        f"({response.status_code}): {response.text[:1000]}"
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
```

# ============================================================

# PRICE LOGIC

# ============================================================

def calc_price(cost, weight):
cost = float(cost or 0)
weight = float(weight or 0)

```
# Shipping
if weight < 300:
    shipping = 3.99
elif weight < 2000:
    shipping = 4.99
else:
    shipping = 18.00

# Profit margin
if cost < 5:
    margin = 0.30
elif cost < 10:
    margin = 0.25
else:
    margin = 0.20

TAX = 0.20

# Shopify + TikTok
FEES = 0.029 + 0.09

# Shopify flat fee + platform/order fee
FIXED_COSTS = 0.30 + 0.50

base_price = cost * (1 + margin)

taxed_price = base_price * (1 + TAX)

price_after_fees = taxed_price / (1 - FEES)

final_price = price_after_fees + shipping + FIXED_COSTS

return round(final_price, 2)
```

# ============================================================

# GENERAL HELPERS

# ============================================================

def clean_text(value) -> str:
if value is None:
return ""

```
return html.unescape(str(value).strip())
```

def last_value(value) -> str:
value = clean_text(value)

```
if not value:
    return ""

return value.split(">")[-1].strip()
```

def split_tags(value) -> List[str]:
value = clean_text(value)

```
if not value:
    return []

return [
    t.strip()
    for t in re.split(r"[>\|,;/\s]+", value)
    if t.strip()
]
```

def sanitize_tags(tags: List[str]) -> List[str]:
sanitized = []
seen = set()

```
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
        sanitized.append(tag)

return sanitized
```

def slugify(value: str) -> str:
value = clean_text(value)

```
value = re.sub(
    r"[^a-z0-9]+",
    "-",
    value.lower(),
)

return value.strip("-")
```

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

def valid_image(url: str) -> bool:

```
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

return any(
    ext in url.lower()
    for ext in extensions
)
```

def build_description(product: Dict[str, Any]) -> str:

```
bullets = []

for i in range(1, 11):

    value = clean_text(
        product.get(f"desc_{i}")
    )

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

standard = clean_text(
    product.get("desc_standard")
)

paragraph = f"<p>{standard}</p>"

return bullet_html + paragraph
```

def normalize_gid(value: str, resource: str) -> str:

```
value = str(value).strip()

if value.startswith("gid://"):
    return value

return f"gid://shopify/{resource}/{value}"
```

# ============================================================

# XML

# ============================================================

def load_xml() -> List[Dict[str, Any]]:

```
print("📥 Downloading XML feed...")

response = SESSION.get(
    XML_URL,
    timeout=REQUEST_TIMEOUT,
)

response.raise_for_status()

root = ET.fromstring(
    response.content
)

items = root.findall(".//post")

print(
    f"🔎 Found {len(items)} supplier products"
)

if LIMIT is not None:
    items = items[:LIMIT]

products = []

for item in items:

    data = {}

    for child in item:

        data[
            child.tag.lower()
        ] = clean_text(child.text)

    products.append(data)

return products
```

# ============================================================

# GRAPHQL

# ============================================================

def graphql(
query: str,
variables: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:

```
token = get_access_token()

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

        response = SESSION.post(
            f"{BASE_URL}/graphql.json",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        # Rate limiting / temporary Shopify errors
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

            print(
                f"⚠️ Shopify HTTP "
                f"{response.status_code}; "
                f"retrying in {delay:.1f}s "
                f"({attempt}/{MAX_RETRIES})"
            )

            time.sleep(delay)

            continue

        response.raise_for_status()

        result = response.json()

        errors = result.get("errors")

        if errors:

            error_text = str(errors)

            # These are permanent schema/validation errors.
            permanent_markers = (
                "INVALID_VARIABLE",
                "variableMismatch",
                "Field is not defined",
                "doesn't exist on type",
                "Type mismatch",
                "Unknown argument",
                "Unknown field",
            )

            if any(
                marker in error_text
                for marker in permanent_markers
            ):
                raise RuntimeError(
                    "GraphQL validation error: "
                    + error_text
                )

            raise RuntimeError(
                "GraphQL errors: "
                + error_text
            )

        return result.get("data") or {}

    except RuntimeError:
        raise

    except Exception as exc:

        last_error = exc

        if attempt >= MAX_RETRIES:
            break

        delay = RETRY_DELAY * attempt

        print(
            f"⚠️ Shopify request failed: "
            f"{exc}; retrying in {delay:.1f}s "
            f"({attempt}/{MAX_RETRIES})"
        )

        time.sleep(delay)

raise RuntimeError(
    f"Shopify GraphQL request failed after "
    f"{MAX_RETRIES} attempts: {last_error}"
)
```

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

```
data = graphql(LOCATIONS_QUERY)

return data.get(
    "locations",
    {}
).get(
    "nodes",
    []
)
```

def resolve_location_id() -> str:

```
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

    wanted = LOCATION_ID

    if not wanted.startswith("gid://"):
        wanted = normalize_gid(
            wanted,
            "Location",
        )

    for location in active_locations:

        if location["id"] == wanted:

            print(
                f"📍 Inventory location: "
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
        f"❌ LOCATION_ID was not found: "
        f"{LOCATION_ID}"
    )

# Automatically use only active location
if len(active_locations) == 1:

    location = active_locations[0]

    print(
        f"📍 Automatically using Shopify "
        f"location: {location['name']} "
        f"({location['id']})"
    )

    return location["id"]

# Prefer location that fulfills online orders
online_locations = [
    location
    for location in active_locations
    if location.get("fulfillsOnlineOrders")
]

if len(online_locations) == 1:

    location = online_locations[0]

    print(
        f"📍 Automatically using online "
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
        f"(online={location.get('fulfillsOnlineOrders')})"
    )

raise RuntimeError(
    "❌ Multiple Shopify locations found. "
    "Set LOCATION_ID."
)
```

# ============================================================

# SOURCE PRODUCT

# ============================================================

def build_source_product(
p: Dict[str, Any]
) -> Dict[str, Any]:

```
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

stock = max(
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
    + split_tags(
        p.get("productrange")
    )
    + TAGS_TO_INCLUDE
    + split_tags(title)
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
    "stock": stock,
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
```

# ============================================================

# FIND PRODUCT

# ============================================================

PRODUCT_QUERY = """
query ProductByHandle($query: String!) {

```
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
```

}
"""

def find_product_by_handle(
handle: str
) -> Optional[Dict[str, Any]]:

```
data = graphql(
    PRODUCT_QUERY,
    {
        "query": f"handle:{handle}"
    },
)

products = (
    data.get("products", {})
    .get("nodes", [])
)

return (
    products[0]
    if products
    else None
)
```

def find_product_by_sku_or_barcode(
sku: Optional[str],
barcode: Optional[str],
) -> Optional[Dict[str, Any]]:

```
if not sku and not barcode:
    return None

search_terms = []

if sku:
    search_terms.append(
        f"sku:{sku}"
    )

if barcode:
    search_terms.append(
        f"barcode:{barcode}"
    )

for query_text in search_terms:

    query = """
    query FindProduct($query: String!) {

        products(first: 10, query: $query) {

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

    data = graphql(
        query,
        {"query": query_text},
    )

    products = (
        data.get("products", {})
        .get("nodes", [])
    )

    for product in products:

        for variant in product.get(
            "variants",
            {}
        ).get(
            "nodes",
            []
        ):

            variant_sku = (
                str(variant.get("sku"))
                if variant.get("sku")
                else None
            )

            variant_barcode = (
                str(variant.get("barcode"))
                if variant.get("barcode")
                else None
            )

            if sku and variant_sku == str(sku):
                return product

            if (
                barcode
                and variant_barcode == str(barcode)
            ):
                return product

return None
```

# ============================================================

# PRODUCT UPDATE

# ============================================================

PRODUCT_UPDATE_MUTATION = """
mutation ProductUpdate(
$input: ProductUpdateInput!
) {

```
productUpdate(product: $input) {

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
```

}
"""

def update_product_data(
product_id: str,
source: Dict[str, Any],
) -> None:

```
input_data = {
    "id": product_id,
    "title": source["title"],
    "descriptionHtml": source["description"],
    "vendor": source["vendor"],
    "productType": source["product_type"],
    "tags": source["tags"],
}

data = graphql(
    PRODUCT_UPDATE_MUTATION,
    {"input": input_data},
)

result = data.get(
    "productUpdate",
    {}
)

errors = result.get(
    "userErrors",
    []
)

if errors:
    raise RuntimeError(
        "Shopify productUpdate errors: "
        + str(errors)
    )
```

# ============================================================

# PRODUCT VARIANT UPDATE

# ============================================================

VARIANT_UPDATE_MUTATION = """
mutation ProductVariantUpdate(
$input: ProductVariantInput!
) {

```
productVariantUpdate(
    input: $input
) {

    productVariant {

        id
        sku
        barcode
        price

        inventoryItem {
            id
        }
    }

    userErrors {
        field
        message
    }
}
```

}
"""

def update_variant(
variant_id: str,
source: Dict[str, Any],
) -> Dict[str, Any]:

```
# IMPORTANT:
#
# SKU is updated here using ProductVariantInput.
#
# We do NOT put SKU into ProductVariantsBulkInput.
# That was the cause of:
#
# "Field is not defined on ProductVariantsBulkInput"
#
input_data = {
    "id": variant_id,
    "price": str(source["price"]),
    "barcode": source["barcode"],
    "weight": source["weight"],
    "weightUnit": "GRAMS",
}

if source["sku"]:
    input_data["sku"] = source["sku"]

data = graphql(
    VARIANT_UPDATE_MUTATION,
    {"input": input_data},
)

result = data.get(
    "productVariantUpdate",
    {}
)

errors = result.get(
    "userErrors",
    []
)

if errors:
    raise RuntimeError(
        "Shopify productVariantUpdate errors: "
        + str(errors)
    )

variant = result.get(
    "productVariant"
)

if not variant:
    raise RuntimeError(
        "Shopify did not return updated variant."
    )

return variant
```

# ============================================================

# INVENTORY

# ============================================================

INVENTORY_SET_MUTATION = """
mutation InventorySetQuantities(
$input: InventorySetQuantitiesInput!
) {

```
inventorySetQuantities(
    input: $input
) {

    inventoryAdjustmentGroup {
        createdAt
        reason
        referenceDocumentUri
    }

    userErrors {
        field
        message
        code
    }
}
```

}
"""

def set_inventory_quantity(
inventory_item_id: str,
location_id: str,
quantity: int,
) -> None:

```
quantity = max(
    0,
    int(quantity)
)

input_data = {
    "name": "available",
    "reason": "correction",
    "ignoreCompareQuantity": True,
    "quantities": [
        {
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
            "quantity": quantity,
        }
    ],
}

data = graphql(
    INVENTORY_SET_MUTATION,
    {"input": input_data},
)

result = data.get(
    "inventorySetQuantities",
    {}
)

errors = result.get(
    "userErrors",
    []
)

if errors:
    raise RuntimeError(
        "Shopify inventory errors: "
        + str(errors)
    )
```

# ============================================================

# PRODUCT OPTIONS

# ============================================================

PRODUCT_OPTIONS_QUERY = """
query ProductOptions($id: ID!) {

```
product(id: $id) {

    id

    options {
        id
        name
        position
        values
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
                tracked
            }
        }
    }
}
```

}
"""

def get_product_details(
product_id: str,
) -> Dict[str, Any]:

```
data = graphql(
    PRODUCT_OPTIONS_QUERY,
    {"id": product_id},
)

product = data.get("product")

if not product:
    raise RuntimeError(
        f"Shopify product not found: {product_id}"
    )

return product
```

# ============================================================

# VARIANT MATCHING

# ============================================================

def find_matching_variant(
product: Dict[str, Any],
source: Dict[str, Any],
size: Optional[str] = None,
) -> Optional[Dict[str, Any]]:

```
variants = (
    product.get("variants", {})
    .get("nodes", [])
)

# 1. SKU match
if source["sku"]:

    for variant in variants:

        if str(
            variant.get("sku") or ""
        ) == str(source["sku"]):

            return variant

# 2. Barcode match
if source["barcode"]:

    for variant in variants:

        if str(
            variant.get("barcode") or ""
        ) == str(source["barcode"]):

            return variant

# 3. Size / option match
if size:

    size_lower = size.strip().lower()

    for variant in variants:

        selected_options = variant.get(
            "selectedOptions",
            []
        )

        for option in selected_options:

            if (
                str(option.get("value", ""))
                .strip()
                .lower()
                == size_lower
            ):
                return variant

# 4. Single variant fallback
if len(variants) == 1:
    return variants[0]

return None
```

# ============================================================

# TRACK INVENTORY

# ============================================================

INVENTORY_ITEM_UPDATE_MUTATION = """
mutation InventoryItemUpdate(
$id: ID!,
$input: InventoryItemInput!
) {

```
inventoryItemUpdate(
    id: $id,
    input: $input
) {

    inventoryItem {
        id
        tracked
    }

    userErrors {
        field
        message
    }
}
```

}
"""

def ensure_inventory_tracked(
inventory_item_id: str,
) -> None:

```
data = graphql(
    INVENTORY_ITEM_UPDATE_MUTATION,
    {
        "id": inventory_item_id,
        "input": {
            "tracked": True
        },
    },
)

result = data.get(
    "inventoryItemUpdate",
    {}
)

errors = result.get(
    "userErrors",
    []
)

if errors:
    raise RuntimeError(
        "Shopify inventoryItemUpdate errors: "
        + str(errors)
    )
```

# ============================================================

# IMAGES

# ============================================================

IMAGE_CREATE_MUTATION = """
mutation ProductCreateMedia(
$productId: ID!,
$media: [CreateMediaInput!]!
) {

```
productCreateMedia(
    productId: $productId,
    media: $media
) {

    media {
        id
    }

    mediaUserErrors {
        field
        message
    }
}
```

}
"""

def update_images(
product_id: str,
image_urls: List[str],
) -> None:

```
if not image_urls:
    return

media = []

for url in image_urls:

    media.append(
        {
            "originalSource": url,
            "mediaContentType": "IMAGE",
        }
    )

# Shopify limits mutation payload size.
# Process images in small batches.
for start in range(
    0,
    len(media),
    10,
):

    batch = media[
        start:start + 10
    ]

    data = graphql(
        IMAGE_CREATE_MUTATION,
        {
            "productId": product_id,
            "media": batch,
        },
    )

    result = data.get(
        "productCreateMedia",
        {}
    )

    errors = result.get(
        "mediaUserErrors",
        []
    )

    if errors:
        raise RuntimeError(
            "Shopify image errors: "
            + str(errors)
        )
```

# ============================================================

# STATUS

# ============================================================

def update_product_status(
product_id: str,
stock: int,
) -> None:

```
status = (
    "ACTIVE"
    if stock > 0
    else "DRAFT"
)

mutation = """
mutation ProductUpdate(
    $input: ProductUpdateInput!
) {

    productUpdate(
        product: $input
    ) {

        product {
            id
            status
        }

        userErrors {
            field
            message
        }
    }
}
"""

data = graphql(
    mutation,
    {
        "input": {
            "id": product_id,
            "status": status,
        }
    },
)

result = data.get(
    "productUpdate",
    {}
)

errors = result.get(
    "userErrors",
    []
)

if errors:
    raise RuntimeError(
        "Shopify status update errors: "
        + str(errors)
    )
```

# ============================================================

# SYNC EXISTING PRODUCT

# ============================================================

def sync_existing_product(
product: Dict[str, Any],
source: Dict[str, Any],
location_id: str,
) -> None:

```
product_id = product["id"]

details = get_product_details(
    product_id
)

variants = (
    details.get("variants", {})
    .get("nodes", [])
)

if not variants:
    raise RuntimeError(
        "Existing product has no variants."
    )

# --------------------------------------------------------
# Determine variant
# --------------------------------------------------------

sizes = source["sizes"]

if sizes:

    for size in sizes:

        variant = find_matching_variant(
            details,
            source,
            size,
        )

        if not variant:
            raise RuntimeError(
                f"Could not match Shopify "
                f"variant for size '{size}'."
            )

        updated_variant = update_variant(
            variant["id"],
            source,
        )

        inventory_item_id = (
            updated_variant
            .get("inventoryItem", {})
            .get("id")
        )

        if not inventory_item_id:
            inventory_item_id = (
                variant
                .get("inventoryItem", {})
                .get("id")
            )

        if not inventory_item_id:
            raise RuntimeError(
                "Variant has no inventory item."
            )

        ensure_inventory_tracked(
            inventory_item_id
        )

        set_inventory_quantity(
            inventory_item_id,
            location_id,
            source["stock"],
        )

else:

    variant = find_matching_variant(
        details,
        source,
    )

    if not variant:
        raise RuntimeError(
            "Could not match existing Shopify "
            "variant by SKU, barcode or single "
            "variant fallback."
        )

    updated_variant = update_variant(
        variant["id"],
        source,
    )

    inventory_item_id = (
        updated_variant
        .get("inventoryItem", {})
        .get("id")
    )

    if not inventory_item_id:
        inventory_item_id = (
            variant
            .get("inventoryItem", {})
            .get("id")
        )

    if not inventory_item_id:
        raise RuntimeError(
            "Variant has no inventory item."
        )

    ensure_inventory_tracked(
        inventory_item_id
    )

    set_inventory_quantity(
        inventory_item_id,
        location_id,
        source["stock"],
    )

# --------------------------------------------------------
# Product information
# --------------------------------------------------------

if UPDATE_PRODUCT_DATA:

    update_product_data(
        product_id,
        source,
    )

# --------------------------------------------------------
# Product status
# --------------------------------------------------------

update_product_status(
    product_id,
    source["stock"],
)

# --------------------------------------------------------
# Images
# --------------------------------------------------------

if UPDATE_IMAGES and source["images"]:

    update_images(
        product_id,
        source["images"],
    )
```

# ============================================================

# CREATE PRODUCT

# ============================================================

PRODUCT_CREATE_MUTATION = """
mutation ProductCreate(
$input: ProductInput!
) {

```
productCreate(
    input: $input
) {

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
```

}
"""

def create_product(
source: Dict[str, Any],
) -> str:

```
input_data = {
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

# Only send product options when the source
# actually has sizes.
if source["sizes"]:

    input_data["productOptions"] = [
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
    {"input": input_data},
)

result = data.get(
    "productCreate",
    {}
)

errors = result.get(
    "userErrors",
    []
)

if errors:
    raise RuntimeError(
        "Shopify productCreate errors: "
        + str(errors)
    )

product = result.get("product")

if not product:
    raise RuntimeError(
        "Shopify did not return created product."
    )

return product["id"]
```

# ============================================================

# CREATE VARIANTS

# ============================================================

PRODUCT_VARIANTS_BULK_CREATE = """
mutation ProductVariantsBulkCreate(
$productId: ID!,
$variants: [ProductVariantsBulkInput!]!
) {

```
productVariantsBulkCreate(
    product: $productId,
    variants: $variants
) {

    productVariants {
        id
        title
        inventoryItem {
            id
        }
    }

    userErrors {
        field
        message
    }
}
```

}
"""

def create_variants(
product_id: str,
source: Dict[str, Any],
) -> List[Dict[str, Any]]:

```
variants_input = []

sizes = source["sizes"]

if not sizes:
    sizes = [None]

for size in sizes:

    item = {
        "price": str(source["price"]),
        "barcode": source["barcode"],
    }

    # IMPORTANT:
    #
    # Do NOT put SKU here.
    #
    # ProductVariantsBulkInput does not accept sku
    # on the Shopify API version/schema producing
    # the error you received.

    if size:

        item["optionValues"] = [
            {
                "optionName": "Size",
                "name": size,
            }
        ]

    variants_input.append(item)

data = graphql(
    PRODUCT_VARIANTS_BULK_CREATE,
    {
        "productId": product_id,
        "variants": variants_input,
    },
)

result = data.get(
    "productVariantsBulkCreate",
    {}
)

errors = result.get(
    "userErrors",
    []
)

if errors:
    raise RuntimeError(
        "Shopify productVariantsBulkCreate errors: "
        + str(errors)
    )

return result.get(
    "productVariants",
    []
)
```

# ============================================================

# CREATE PRODUCT COMPLETE

# ============================================================

def create_complete_product(
source: Dict[str, Any],
location_id: str,
) -> None:

```
product_id = create_product(
    source
)

variants = create_variants(
    product_id,
    source,
)

if not variants:
    raise RuntimeError(
        "Product was created but no variants "
        "were returned."
    )

# Update each newly created variant.
#
# This is intentionally done through
# productVariantUpdate because SKU/weight
# are not reliably supported by the bulk input
# schema used by this API version.

for variant in variants:

    variant_input = {
        "id": variant["id"],
        "price": str(source["price"]),
        "barcode": source["barcode"],
        "weight": source["weight"],
        "weightUnit": "GRAMS",
    }

    if source["sku"]:
        variant_input["sku"] = source["sku"]

    data = graphql(
        VARIANT_UPDATE_MUTATION,
        {"input": variant_input},
    )

    result = data.get(
        "productVariantUpdate",
        {}
    )

    errors = result.get(
        "userErrors",
        []
    )

    if errors:
        raise RuntimeError(
            "Shopify variant creation/update errors: "
            + str(errors)
        )

    updated = result.get(
        "productVariant"
    )

    if not updated:
        raise RuntimeError(
            "Shopify did not return created variant."
        )

    inventory_item_id = (
        updated
        .get("inventoryItem", {})
        .get("id")
    )

    if not inventory_item_id:

        inventory_item_id = (
            variant
            .get("inventoryItem", {})
            .get("id")
        )

    if not inventory_item_id:
        raise RuntimeError(
            "Created variant has no inventory item."
        )

    ensure_inventory_tracked(
        inventory_item_id
    )

    set_inventory_quantity(
        inventory_item_id,
        location_id,
        source["stock"],
    )

if UPDATE_IMAGES and source["images"]:

    update_images(
        product_id,
        source["images"],
    )
```

# ============================================================

# SYNC ONE PRODUCT

# ============================================================

def sync_product(
source_item: Dict[str, Any],
location_id: str,
index: int,
total: int,
) -> Tuple[str, str]:

```
source = build_source_product(
    source_item
)

title = source["title"]

print("\n" + "=" * 70)
print(
    f"[{index}/{total}]"
)
print("=" * 70)
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

# --------------------------------------------------------
# Find by handle
# --------------------------------------------------------

existing = find_product_by_handle(
    source["handle"]
)

# --------------------------------------------------------
# Fallback by SKU / barcode
# --------------------------------------------------------

if not existing:

    existing = find_product_by_sku_or_barcode(
        source["sku"],
        source["barcode"],
    )

# --------------------------------------------------------
# Existing
# --------------------------------------------------------

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
        f"✅ Updated: {title}"
    )

    return (
        "updated",
        title,
    )

# --------------------------------------------------------
# New
# --------------------------------------------------------

print(
    "🆕 Product not found - creating..."
)

create_complete_product(
    source,
    location_id,
)

print(
    f"✅ Created: {title}"
)

return (
    "created",
    title,
)
```

# ============================================================

# MAIN SYNC

# ============================================================

def run_sync():

```
print("\n" + "=" * 70)
print("🚀 SHOPIFY SUPPLIER SYNC")
print("=" * 70)

print(
    f"🏪 Shop: {SHOP_URL}"
)

print(
    f"🔢 API version: {API_VERSION}"
)

print(
    f"👷 Workers: {WORKERS}"
)

print(
    f"📦 Limit: "
    f"{LIMIT if LIMIT is not None else 'ALL'}"
)

print(
    f"⏱️ Timeout: {REQUEST_TIMEOUT}s"
)

print("=" * 70)

# --------------------------------------------------------
# Token
# --------------------------------------------------------

get_access_token()

print(
    "🔐 Shopify authentication: OK"
)

# --------------------------------------------------------
# Location
# --------------------------------------------------------

location_id = resolve_location_id()

print(
    f"📍 Using location: {location_id}"
)

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

print("\n" + "=" * 70)

print(
    f"🚀 Starting sync of {total} products..."
)

print("=" * 70)

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

    future_to_meta = {}

    for index, item in enumerate(
        items,
        start=1,
    ):

        future = executor.submit(
            sync_product,
            item,
            location_id,
            index,
            total,
        )

        future_to_meta[
            future
        ] = (
            index,
            item,
        )

    for future in as_completed(
        future_to_meta
    ):

        index, item = future_to_meta[
            future
        ]

        title = clean_text(
            item.get("title")
        )

        try:

            result, result_title = (
                future.result()
            )

            if result == "created":
                created += 1

            elif result == "updated":
                updated += 1

            else:
                failed += 1

        except Exception as exc:

            failed += 1

            error_text = str(exc)

            failures.append(
                {
                    "index": index,
                    "title": title,
                    "error": error_text,
                }
            )

            print("\n" + "!" * 70)

            print(
                f"❌ ERROR syncing "
                f"{title}"
            )

            print(
                f"   {error_text}"
            )

            print("!" * 70)

# --------------------------------------------------------
# Summary
# --------------------------------------------------------

print("\n" + "=" * 70)
print("✅ SYNC COMPLETE")
print("=" * 70)

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

print("=" * 70)

# --------------------------------------------------------
# Failure report
# --------------------------------------------------------

if failures:

    print("\n" + "=" * 70)

    print(
        f"❌ FAILURE SUMMARY "
        f"({len(failures)} products)"
    )

    print("=" * 70)

    for failure in failures:

        print(
            f"[{failure['index']}] "
            f"{failure['title']}"
        )

        print(
            f"    {failure['error']}"
        )

    print("=" * 70)

else:

    print(
        "\n🎉 No products failed."
    )
```

# ============================================================

# ENTRY POINT

# ============================================================

if **name** == "**main**":

```
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
