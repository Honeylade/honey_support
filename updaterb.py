import os
import requests
import xml.etree.ElementTree as ET
import re
import html
import time
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================

# CONFIG

# ============================================================

SHOP_URL = os.getenv("SHOP_URL")
XML_URL = os.getenv("XML_URL")

# Shopify API version.

# 2026-04 requires changeFromQuantity on inventorySetQuantities.

API_VERSION = "2026-04"

# None = sync ALL products.

# Example: LIMIT = 10 for testing.

LIMIT = None

# Start conservatively. Increase only after confirming the sync

# works reliably without Shopify throttling.

MAX_WORKERS = 5

MAX_RETRIES = 5
RETRY_DELAY = 2
REQUEST_TIMEOUT = 60

# Set this as a GitHub Actions secret/variable.

# Example:

# LOCATION_ID=gid://shopify/Location/123456789

LOCATION_ID = os.getenv("LOCATION_ID")

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

# ACCESS TOKEN

# ============================================================

_token_cache = {
"access_token": None,
"expires_at": 0,
}

def get_access_token() -> str:
"""
Get Shopify access token using client credentials.

```
IMPORTANT:
Do not print the client secret or access token.
"""

cached = _token_cache["access_token"]

if cached and time.time() < (_token_cache["expires_at"] - 60):
    return cached

client_id = os.getenv("CLIENT_ID")
client_secret = os.getenv("CLIENT_SECRET")

if not SHOP_URL:
    raise RuntimeError("SHOP_URL is not configured.")

if not client_id:
    raise RuntimeError("CLIENT_ID is not configured.")

if not client_secret:
    raise RuntimeError("CLIENT_SECRET is not configured.")

url = f"https://{SHOP_URL}/admin/oauth/access_token"

payload = {
    "client_id": client_id,
    "client_secret": client_secret,
    "grant_type": "client_credentials",
}

response = requests.post(
    url,
    json=payload,
    timeout=REQUEST_TIMEOUT,
)

if response.status_code >= 400:
    raise RuntimeError(
        f"Unable to obtain Shopify access token. "
        f"HTTP {response.status_code}: {response.text[:500]}"
    )

data = response.json()

token = data.get("access_token")

if not token:
    raise RuntimeError(
        f"Shopify did not return an access token: {data}"
    )

_token_cache["access_token"] = token
_token_cache["expires_at"] = (
    time.time() + int(data.get("expires_in", 86400))
)

return token
```

# ============================================================

# PRICE LOGIC

# ============================================================

def calc_price(cost, weight):
cost = float(cost or 0)
weight = float(weight or 0)

```
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

final_price = (
    price_after_fees
    + shipping
    + FIXED_COSTS
)

return round(final_price, 2)
```

# ============================================================

# HELPERS

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

    tag = (
        html.unescape(str(tag))
        .replace("&", "and")
        .strip()
    )

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

def build_description(product: Dict[str, Any]) -> str:
bullets = []

```
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

standard = clean_text(
    product.get("desc_standard")
)

return (
    bullet_html
    + f"<p>{standard}</p>"
)
```

def slugify(value: str) -> str:
return re.sub(
r"[^a-z0-9]+",
"-",
clean_text(value).lower()
).strip("-")

def valid_image(url: str) -> bool:
if not url:
return False

```
url = url.strip()

if not (
    url.startswith("http://")
    or url.startswith("https://")
):
    return False

if " " in url:
    return False

return any(
    ext in url.lower()
    for ext in [
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    ]
)
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

# ============================================================

# XML

# ============================================================

def load_xml() -> List[Dict[str, Any]]:
print("📥 Downloading XML feed...")

```
if not XML_URL:
    raise RuntimeError(
        "XML_URL is not configured."
    )

response = requests.get(
    XML_URL,
    timeout=REQUEST_TIMEOUT,
)

response.raise_for_status()

root = ET.fromstring(response.content)

items = root.findall(".//post")

print(
    f"🔎 Found {len(items)} supplier products"
)

if LIMIT is None:
    selected_items = items
else:
    selected_items = items[:LIMIT]

products = []

for item in selected_items:
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
retries: int = MAX_RETRIES,
) -> Dict[str, Any]:

```
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

for attempt in range(1, retries + 1):

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        # Shopify throttling / temporary server errors.
        if response.status_code in (
            429,
            500,
            502,
            503,
            504,
        ):
            wait = RETRY_DELAY * attempt

            print(
                f"⚠️ Shopify HTTP "
                f"{response.status_code}; "
                f"retry {attempt}/{retries} "
                f"in {wait}s"
            )

            time.sleep(wait)
            continue

        response.raise_for_status()

        result = response.json()

        if result.get("errors"):
            errors = result["errors"]

            error_text = str(errors)

            # GraphQL schema/validation errors are permanent.
            permanent_markers = [
                "undefinedField",
                "variableMismatch",
                "variableNotUsed",
                "INVALID_VARIABLE",
                "Field is not defined",
                "Type mismatch",
                "doesn't exist on type",
            ]

            if any(
                marker in error_text
                for marker in permanent_markers
            ):
                raise RuntimeError(
                    "GraphQL validation error: "
                    + error_text
                )

            last_error = RuntimeError(
                "GraphQL errors: "
                + error_text
            )

            if attempt < retries:
                wait = RETRY_DELAY * attempt

                print(
                    f"⚠️ Shopify request failed: "
                    f"{last_error}; "
                    f"retry {attempt}/{retries} "
                    f"in {wait}s"
                )

                time.sleep(wait)
                continue

            raise last_error

        return result.get("data", {})

    except requests.exceptions.RequestException as exc:
        last_error = exc

        if attempt < retries:
            wait = RETRY_DELAY * attempt

            print(
                f"⚠️ Shopify HTTP request failed: "
                f"{exc}; "
                f"retry {attempt}/{retries} "
                f"in {wait}s"
            )

            time.sleep(wait)
        else:
            break

    except RuntimeError:
        raise

    except Exception as exc:
        last_error = exc

        if attempt < retries:
            wait = RETRY_DELAY * attempt

            print(
                f"⚠️ Shopify request failed: "
                f"{exc}; "
                f"retry {attempt}/{retries} "
                f"in {wait}s"
            )

            time.sleep(wait)

raise RuntimeError(
    "Shopify GraphQL request failed after "
    f"{retries} attempts: {last_error}"
)
```

# ============================================================

# LOCATIONS

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

```
return data["locations"]["nodes"]
```

def resolve_location_id() -> str:

```
locations = get_locations()

active_locations = [
    x for x in locations
    if x.get("isActive")
]

if not active_locations:
    raise RuntimeError(
        "❌ No active Shopify locations found."
    )

print("\n📍 Shopify locations:")

for location in active_locations:
    print(
        f"   {location['name']}: "
        f"{location['id']} "
        f"(online="
        f"{location.get('fulfillsOnlineOrders')})"
    )

if LOCATION_ID:

    for location in active_locations:

        if location["id"] == LOCATION_ID:
            print(
                f"\n📍 Using Shopify location: "
                f"{location['name']}"
            )

            return LOCATION_ID

    raise RuntimeError(
        f"❌ LOCATION_ID was not found: "
        f"{LOCATION_ID}"
    )

online_locations = [
    x for x in active_locations
    if x.get("fulfillsOnlineOrders")
]

if len(online_locations) == 1:

    location = online_locations[0]

    print(
        f"\n📍 Automatically using Shopify "
        f"online location: {location['name']}"
    )

    return location["id"]

if len(active_locations) == 1:

    location = active_locations[0]

    print(
        f"\n📍 Automatically using Shopify "
        f"location: {location['name']}"
    )

    return location["id"]

raise RuntimeError(
    "\n❌ Multiple Shopify locations exist.\n"
    "Set LOCATION_ID in your environment variables."
)
```

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
price
inventoryQuantity
inventoryItem {
id
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

def find_product_by_handle(
handle: str,
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

return products[0] if products else None
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
```

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
    {
        "product": input_data
    },
)

result = data["productUpdate"]

errors = result.get("userErrors") or []

if errors:
    raise RuntimeError(
        "Product update errors: "
        + str(errors)
    )
```

# ============================================================

# VARIANT UPDATE

# ============================================================

VARIANT_UPDATE_MUTATION = """
mutation ProductVariantsBulkUpdate(
$productId: ID!,
$variants: [ProductVariantsBulkInput!]!
) {
productVariantsBulkUpdate(
productId: $productId
variants: $variants
) {
product {
id
}
productVariants {
id
price
barcode
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

def update_variant(
product_id: str,
variant_id: str,
source: Dict[str, Any],
) -> None:

```
variant_input = {
    "id": variant_id,
    "price": str(source["price"]),
}

if source["barcode"]:
    variant_input["barcode"] = (
        source["barcode"]
    )

# SKU is NOT a direct ProductVariantsBulkInput
# field. It belongs under inventoryItem.
inventory_item = {}

if source["sku"]:
    inventory_item["sku"] = source["sku"]

if source["weight"] > 0:
    inventory_item["measurement"] = {
        "weight": {
            "value": source["weight"],
            "unit": "GRAMS",
        }
    }

if inventory_item:
    variant_input[
        "inventoryItem"
    ] = inventory_item

data = graphql(
    VARIANT_UPDATE_MUTATION,
    {
        "productId": product_id,
        "variants": [variant_input],
    },
)

result = data[
    "productVariantsBulkUpdate"
]

errors = result.get(
    "userErrors"
) or []

if errors:
    raise RuntimeError(
        "Variant update errors: "
        + str(errors)
    )
```

# ============================================================

# INVENTORY

# ============================================================

INVENTORY_QUANTITY_QUERY = """
query InventoryQuantity(
$inventoryItemId: ID!,
$locationId: ID!
) {
inventoryItem(id: $inventoryItemId) {
id
tracked
inventoryLevel(
locationId: $locationId
) {
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

def get_current_inventory_quantity(
inventory_item_id: str,
location_id: str,
) -> Optional[int]:

```
data = graphql(
    INVENTORY_QUANTITY_QUERY,
    {
        "inventoryItemId":
            inventory_item_id,
        "locationId":
            location_id,
    },
)

inventory_item = (
    data.get("inventoryItem")
)

if not inventory_item:
    return None

if not inventory_item.get("tracked"):
    return None

level = inventory_item.get(
    "inventoryLevel"
)

if not level:
    return None

quantities = (
    level.get("quantities")
    or []
)

for quantity in quantities:

    if quantity.get("name") == "available":
        return quantity.get("quantity")

return None
```

INVENTORY_SET_MUTATION = """
mutation InventorySet(
$input: InventorySetQuantitiesInput!
) {
inventorySetQuantities(
input: $input
) {
inventoryAdjustmentGroup {
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

def set_inventory_quantity(
inventory_item_id: str,
location_id: str,
desired_quantity: int,
) -> None:

```
desired_quantity = max(
    0,
    int(desired_quantity)
)

# First read current quantity.
current_quantity = (
    get_current_inventory_quantity(
        inventory_item_id,
        location_id,
    )
)

# If Shopify does not have an inventory level
# at this location, report it instead of silently
# pretending the update succeeded.
if current_quantity is None:
    raise RuntimeError(
        "Inventory item is not tracked or "
        "is not stocked at this location: "
        f"{inventory_item_id}"
    )

# Already correct.
if current_quantity == desired_quantity:
    return

for attempt in range(
    1,
    MAX_RETRIES + 1
):

    variables = {
        "input": {
            "reason": "correction",
            "name": "available",
            "quantities": [
                {
                    "inventoryItemId":
                        inventory_item_id,
                    "locationId":
                        location_id,
                    "quantity":
                        desired_quantity,

                    # REQUIRED by current Shopify API.
                    "changeFromQuantity":
                        current_quantity,
                }
            ],
        }
    }

    try:
        data = graphql(
            INVENTORY_SET_MUTATION,
            variables,
        )

        result = data[
            "inventorySetQuantities"
        ]

        errors = result.get(
            "userErrors"
        ) or []

        if not errors:
            return

        error_text = str(errors)

        # Another process changed stock between
        # our read and write.
        if (
            "CHANGE_FROM_QUANTITY_STALE"
            in error_text
            or "changeFromQuantity"
            in error_text
        ):

            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    "Inventory changed while "
                    "updating after "
                    f"{MAX_RETRIES} attempts: "
                    f"{errors}"
                )

            print(
                "   ⚠️ Inventory changed "
                "during update; "
                "refreshing quantity..."
            )

            current_quantity = (
                get_current_inventory_quantity(
                    inventory_item_id,
                    location_id,
                )
            )

            if current_quantity is None:
                raise RuntimeError(
                    "Inventory level disappeared "
                    "while updating."
                )

            if current_quantity == desired_quantity:
                return

            time.sleep(
                RETRY_DELAY * attempt
            )

            continue

        raise RuntimeError(
            "Inventory update errors: "
            + error_text
        )

    except RuntimeError as exc:

        text = str(exc)

        if (
            "CHANGE_FROM_QUANTITY_STALE"
            not in text
            and "changeFromQuantity"
            not in text
        ):
            raise

        if attempt >= MAX_RETRIES:
            raise

        current_quantity = (
            get_current_inventory_quantity(
                inventory_item_id,
                location_id,
            )
        )

        if current_quantity is None:
            raise

        if current_quantity == desired_quantity:
            return

        time.sleep(
            RETRY_DELAY * attempt
        )
```

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
variants(first: 250) {
nodes {
id
title
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
) -> Dict[str, Any]:

```
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

# Only add options if the source has sizes.
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
    "userErrors"
) or []

if errors:
    raise RuntimeError(
        "Product creation errors: "
        + str(errors)
    )

product = result.get("product")

if not product:
    raise RuntimeError(
        "Shopify productCreate returned "
        "no product."
    )

return product
```

# ============================================================

# VARIANT MATCHING

# ============================================================

def find_matching_variant(
product: Dict[str, Any],
source: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

```
variants = (
    product.get("variants", {})
    .get("nodes", [])
)

source_sku = source.get("sku")
source_barcode = source.get(
    "barcode"
)

# First match SKU.
if source_sku:

    for variant in variants:

        if (
            variant.get("sku")
            and str(
                variant["sku"]
            )
            == str(source_sku)
        ):
            return variant

# Then barcode.
if source_barcode:

    for variant in variants:

        if (
            variant.get("barcode")
            and str(
                variant["barcode"]
            )
            == str(source_barcode)
        ):
            return variant

# Single-variant product.
if len(variants) == 1:
    return variants[0]

return None
```

# ============================================================

# SYNC EXISTING PRODUCT

# ============================================================

def sync_existing_product(
existing: Dict[str, Any],
source: Dict[str, Any],
location_id: str,
) -> None:

```
product_id = existing["id"]

# Product information.
update_product(
    product_id,
    source,
)

variants = (
    existing.get("variants", {})
    .get("nodes", [])
)

if not variants:
    raise RuntimeError(
        "Product has no variants."
    )

# --------------------------------------------------------
# Match/update variants.
# --------------------------------------------------------

if source["sizes"] and len(variants) > 1:

    # For multi-variant products, match by
    # SKU/barcode where possible.
    for variant in variants:

        variant_source = dict(source)

        matched = False

        if (
            source["sku"]
            and variant.get("sku")
            and str(
                variant["sku"]
            )
            == str(source["sku"])
        ):
            matched = True

        if (
            source["barcode"]
            and variant.get("barcode")
            and str(
                variant["barcode"]
            )
            == str(source["barcode"])
        ):
            matched = True

        if matched:

            update_variant(
                product_id,
                variant["id"],
                variant_source,
            )

            inventory_item = (
                variant
                .get("inventoryItem")
                or {}
            )

            inventory_item_id = (
                inventory_item.get("id")
            )

            if inventory_item_id:

                set_inventory_quantity(
                    inventory_item_id,
                    location_id,
                    source["stock"],
                )

            return

    # If no SKU/barcode match, do not
    # accidentally overwrite a random size.
    raise RuntimeError(
        "Could not safely match source "
        "SKU/barcode to a Shopify variant."
    )

# --------------------------------------------------------
# Single variant.
# --------------------------------------------------------

variant = find_matching_variant(
    existing,
    source,
)

if not variant:
    raise RuntimeError(
        "Could not match Shopify variant "
        "by SKU, barcode or single-variant "
        "fallback."
    )

update_variant(
    product_id,
    variant["id"],
    source,
)

inventory_item = (
    variant.get("inventoryItem")
    or {}
)

inventory_item_id = (
    inventory_item.get("id")
)

if not inventory_item_id:
    raise RuntimeError(
        "Shopify variant has no inventory item."
    )

set_inventory_quantity(
    inventory_item_id,
    location_id,
    source["stock"],
)
```

# ============================================================

# SYNC ONE PRODUCT

# ============================================================

def sync_product(
index: int,
total: int,
source_item: Dict[str, Any],
location_id: str,
) -> str:

```
source = build_source_product(
    source_item
)

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
        f"✅ Updated successfully: "
        f"{title}"
    )

    return "updated"

# --------------------------------------------------------
# CREATE
# --------------------------------------------------------

print(
    "🆕 Product not found - creating..."
)

created_product = create_product(
    source
)

print(
    f"   Created Shopify product: "
    f"{created_product['id']}"
)

# Shopify product creation may create a
# default variant. Retrieve the product
# again so we have the definitive variant
# and inventory item IDs.
created = find_product_by_handle(
    source["handle"]
)

if not created:
    raise RuntimeError(
        "Product was created but could "
        "not be found afterwards."
    )

variants = (
    created.get("variants", {})
    .get("nodes", [])
)

if not variants:
    raise RuntimeError(
        "Created product has no variant."
    )

# For a newly created single variant,
# update the variant.
if len(variants) == 1:

    variant = variants[0]

    update_variant(
        created["id"],
        variant["id"],
        source,
    )

    inventory_item = (
        variant.get("inventoryItem")
        or {}
    )

    inventory_item_id = (
        inventory_item.get("id")
    )

    if inventory_item_id:

        set_inventory_quantity(
            inventory_item_id,
            location_id,
            source["stock"],
        )

print(
    f"✅ Created successfully: "
    f"{title}"
)

return "created"
```

# ============================================================

# MAIN

# ============================================================

def run_sync():

```
print("\n" + "=" * 70)
print("🚀 SHOPIFY SUPPLIER SYNC")
print("=" * 70)

# --------------------------------------------------------
# Validate configuration.
# --------------------------------------------------------

if not SHOP_URL:
    raise RuntimeError(
        "SHOP_URL is not configured."
    )

if not XML_URL:
    raise RuntimeError(
        "XML_URL is not configured."
    )

# Validate token before starting workers.
get_access_token()

# --------------------------------------------------------
# Resolve location.
# --------------------------------------------------------

location_id = resolve_location_id()

print(
    f"\n📍 Inventory location ID: "
    f"{location_id}"
)

# --------------------------------------------------------
# Load XML.
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
    f"🚀 Starting sync of "
    f"{total} products"
)
print(
    f"👷 Workers: {MAX_WORKERS}"
)
print(
    f"🔁 Max retries: {MAX_RETRIES}"
)
print("=" * 70)

created = 0
updated = 0
failed = 0

failures = []

# --------------------------------------------------------
# Thread pool.
# --------------------------------------------------------

with ThreadPoolExecutor(
    max_workers=MAX_WORKERS
) as executor:

    future_to_data = {}

    for index, item in enumerate(
        items,
        start=1
    ):

        future = executor.submit(
            sync_product,
            index,
            total,
            item,
            location_id,
        )

        future_to_data[future] = (
            index,
            item,
        )

    completed = 0

    for future in as_completed(
        future_to_data
    ):

        index, item = (
            future_to_data[future]
        )

        completed += 1

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

            title = clean_text(
                item.get("title")
            )

            failure = {
                "index": index,
                "title": title,
                "error": str(exc),
            }

            failures.append(
                failure
            )

            print(
                "\n" + "!" * 70
            )

            print(
                f"❌ ERROR syncing "
                f"{title}"
            )

            print(
                f"   {exc}"
            )

            print(
                "!" * 70
            )

        # Progress summary every 25 products.
        if (
            completed % 25 == 0
            or completed == total
        ):

            print(
                "\n📊 PROGRESS: "
                f"{completed}/{total} "
                f"| Created: {created} "
                f"| Updated: {updated} "
                f"| Failed: {failed}"
            )

# --------------------------------------------------------
# Final report.
# --------------------------------------------------------

print("\n" + "=" * 70)
print("✅ SYNC COMPLETE")
print("=" * 70)

print(f"🆕 Created: {created}")
print(f"🔄 Updated: {updated}")
print(f"❌ Failed:  {failed}")
print(f"📦 Total:   {total}")

if failures:

    print("\n" + "=" * 70)
    print("❌ FAILED PRODUCTS")
    print("=" * 70)

    for failure in sorted(
        failures,
        key=lambda x: x["index"]
    ):

        print(
            f"[{failure['index']}] "
            f"{failure['title']}"
        )

        print(
            f"    {failure['error']}"
        )

print("=" * 70)
```

# ============================================================

# RUN

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
