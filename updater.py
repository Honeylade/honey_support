import os
import re
import html
import time
import uuid
import requests
import xml.etree.ElementTree as ET
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
 
# ============================================================
# CONFIGURATION
# ============================================================
 
SHOP_URL = os.getenv("SHOP_URL")
XML_URL = os.getenv("XML_URL")
 
# Current Shopify Admin GraphQL API version
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-07")
 
# ------------------------------------------------------------
# IMPORTANT:
# Set this to the Shopify Location ID where supplier stock
# should be written.
# Example: LOCATION_ID=gid://shopify/Location/123456789
# If left empty and there is exactly one active Shopify
# location, the script will automatically use it.
# ------------------------------------------------------------
LOCATION_ID = os.getenv("LOCATION_ID")
 
# ------------------------------------------------------------
# Number of simultaneous products.
# For approximately 5,000 products: 5 is a safe starting point.
# If Shopify throttles heavily, reduce to 3.
# ------------------------------------------------------------
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "5"))
 
# ------------------------------------------------------------
# Product limit
# LIMIT=10      -> test first 10 products
# LIMIT=100     -> test first 100
# LIMIT=0       -> all
# LIMIT=None    -> all
# ------------------------------------------------------------
LIMIT_RAW = os.getenv("LIMIT", "0")
if LIMIT_RAW.lower() in ("none", "", "0"):
    LIMIT = None
else:
    LIMIT = int(LIMIT_RAW)
 
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "2"))
 
# ------------------------------------------------------------
# CREATE PRODUCTS
# TRUE  = create missing supplier products
# FALSE = don't create missing products
# ------------------------------------------------------------
CREATE_MISSING_PRODUCTS = os.getenv("CREATE_MISSING_PRODUCTS", "true").lower() == "true"
 
# ------------------------------------------------------------
# UPDATE PRODUCT DESCRIPTION / TAGS ETC.
# ------------------------------------------------------------
UPDATE_PRODUCT_DATA = os.getenv("UPDATE_PRODUCT_DATA", "true").lower() == "true"
 
# ------------------------------------------------------------
# UPDATE PRICES
# ------------------------------------------------------------
UPDATE_PRICES = os.getenv("UPDATE_PRICES", "true").lower() == "true"
 
# ------------------------------------------------------------
# UPDATE SKU / COST / WEIGHT
# ------------------------------------------------------------
UPDATE_INVENTORY_ITEM = os.getenv("UPDATE_INVENTORY_ITEM", "true").lower() == "true"
 
# ------------------------------------------------------------
# UPDATE STOCK
# ------------------------------------------------------------
UPDATE_INVENTORY = os.getenv("UPDATE_INVENTORY", "true").lower() == "true"
 
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
 
def validate_config():
    missing = []
    if not SHOP_URL:
        missing.append("SHOP_URL")
    if not XML_URL:
        missing.append("XML_URL")
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
        )
 
    if not SHOP_URL.startswith("http"):
        # Accept: mystore.myshopify.com but remove accidental protocol.
        global SHOP_URL
        SHOP_URL = SHOP_URL.replace("https://", "").replace("http://", "")
        SHOP_URL = SHOP_URL.rstrip("/")
 
# ============================================================
# PRICE LOGIC
# ============================================================
 
def calc_price(cost, weight):
    cost = to_float(cost)
    weight = to_float(weight)
 
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
    return html.unescape(str(value).strip()) if value else ""
 
def last_value(value) -> str:
    value = clean_text(value)
    return value.split(">")[-1].strip() if value else ""
 
def split_tags(value) -> List[str]:
    value = clean_text(value)
    return [t.strip() for t in re.split(r"[>\|,;/\s]+", value) if t.strip()]
 
def sanitize_tags(tags: List[str]) -> List[str]:
    result = []
    seen = set()
 
    for tag in tags:
        if not tag:
            continue
        tag = html.unescape(str(tag)).replace("&", "and").strip()
        if tag and len(tag) <= 255 and tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
 
    return result
 
def build_description(product: Dict[str, Any]) -> str:
    bullets = []
    for i in range(1, 11):
        value = clean_text(product.get(f"desc_{i}"))
        if value:
            bullets.append(f"<li>{value}</li>")
 
    bullet_html = "<ul>" + "".join(bullets) + "</ul>" if bullets else ""
    standard = clean_text(product.get("desc_standard"))
    paragraph = f"<p>{standard}</p>" if standard else ""
 
    return bullet_html + paragraph
 
def slugify(value: str) -> str:
    value = clean_text(value)
    value = value.lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")
 
def valid_image(url: str) -> bool:
    if not url:
        return False
 
    url = url.strip()
    return (
        url.startswith("http://") or url.startswith("https://") and
        " " not in url and
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
    print(f"🔎 Found {len(items)} supplier items")
 
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
# SHOPIFY GRAPHQL
# ============================================================
 
def graphql_request(query: str, variables: Optional[Dict[str, Any]] = None, operation_name: str = "") -> Dict[str, Any]:
    token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("SHOPIFY_ACCESS_TOKEN is not configured.")
 
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
    if operation_name:
        payload["operationName"] = operation_name
 
    last_exception = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
 
            # ------------------------------------------------
            # RATE LIMIT / TEMPORARY SHOPIFY FAILURE
            # ------------------------------------------------
            if response.status_code in (429, 500, 502, 503, 504):
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else RETRY_BASE_DELAY * attempt
                print(f"⚠️ Shopify HTTP {response.status_code}; waiting {delay:.1f}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(delay)
                continue
 
            response.raise_for_status()
            result = response.json()
 
            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------
            errors = result.get("errors")
            if errors:
                error_text = str(errors)
                permanent_markers = [
                    "INVALID_VARIABLE", "variableMismatch", "Field is not defined",
                    "Unknown argument", "Unknown field", "Cannot query field",
                    "Type mismatch", "Parse error", "Syntax Error",
                ]
                is_permanent = any(marker in error_text for marker in permanent_markers)
                if is_permanent:
                    raise RuntimeError("Permanent Shopify GraphQL error: " + error_text)
 
                last_exception = RuntimeError("GraphQL errors: " + error_text)
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * attempt
                    print(f"⚠️ Shopify GraphQL error; retrying in {delay:.1f}s ({attempt}/{MAX_RETRIES})")
                    time.sleep(delay)
                    continue
 
                raise last_exception
 
            return result.get("data", {})
 
        except requests.exceptions.RequestException as exc:
            last_exception = exc
            if attempt >= MAX_RETRIES:
                break
            delay = RETRY_BASE_DELAY * attempt
            print(f"⚠️ Network error: {exc}; retrying in {delay:.1f}s ({attempt}/{MAX_RETRIES})")
            time.sleep(delay)
 
        except RuntimeError:
            raise
 
        except Exception as exc:
            last_exception = exc
            if attempt >= MAX_RETRIES:
                break
            delay = RETRY_BASE_DELAY * attempt
            time.sleep(delay)
 
    raise RuntimeError("Shopify GraphQL request failed after " f"{MAX_RETRIES} attempts: " f"{last_exception}")
 
# ============================================================
# SHOPIFY USER ERROR HANDLER
# ============================================================
 
def raise_user_errors(user_errors, operation):
    if not user_errors:
        return
 
    formatted = []
    for error in user_errors:
        field = error.get("field")
        message = error.get("message", "Unknown Shopify error")
        formatted.append(f"{field}: {message}" if field else message)
 
    raise RuntimeError(f"{operation} failed: " + " | ".join(formatted))
 
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
    data = graphql_request(LOCATIONS_QUERY, operation_name="GetLocations")
    return data["locations"]["nodes"]
 
def resolve_location_id():
    locations = get_locations()
    active_locations = [location for location in locations if location.get("isActive")]
 
    if not active_locations:
        raise RuntimeError("❌ No active Shopify locations found.")
 
    # --------------------------------------------------------
    # Explicit LOCATION_ID
    # --------------------------------------------------------
    if LOCATION_ID:
        for location in locations:
            if location["id"] == LOCATION_ID:
                print(f"📍 Inventory location: {location['name']} ({location['id']})")
                return location["id"]
 
        print("\n📍 Shopify locations:")
        for location in locations:
            print(f"   {location['name']}: {location['id']} (active={location['isActive']})")
        raise RuntimeError(f"❌ LOCATION_ID was not found: {LOCATION_ID}")
 
    # --------------------------------------------------------
    # Automatically use only active location
    # --------------------------------------------------------
    if len(active_locations) == 1:
        location = active_locations[0]
        print(f"📍 Automatically using Shopify location: {location['name']} ({location['id']})")
        return location["id"]
 
    # --------------------------------------------------------
    # Multiple locations
    # --------------------------------------------------------
    print("\n📍 Shopify locations:")
    for location in locations:
        print(f"   {location['name']}: {location['id']} (active={location['isActive']}, online={location['fulfillsOnlineOrders']})")
 
    raise RuntimeError("❌ Multiple active Shopify locations found. Set LOCATION_ID in your environment.")
 
# ============================================================
# FIND PRODUCT
# ============================================================
 
PRODUCT_BY_HANDLE_QUERY = """
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
                        unitCost {
                            amount
                        }
                        measurement {
                            weight {
                                value
                                unit
                            }
                        }
                    }
                    selectedOptions {
                        name
                        value
                    }
                }
            }
        }
    }
}
"""
 
def find_product_by_handle(handle: str) -> Optional[Dict[str, Any]]:
    data = graphql_request(PRODUCT_BY_HANDLE_QUERY, {"query": f"handle:{handle}"}, operation_name="ProductByHandle")
    products = data["products"]["nodes"]
    return products[0] if products else None
 
# ============================================================
# FIND VARIANT
# ============================================================
 
def find_matching_variant(product, source):
    variants = product.get("variants", {}).get("nodes", [])
    source_sku = clean_text(source.get("sku"))
    source_barcode = clean_text(source.get("barcode"))
 
    # --------------------------------------------------------
    # 1. Barcode match
    # --------------------------------------------------------
    if source_barcode:
        for variant in variants:
            if clean_text(variant.get("barcode")) == source_barcode:
                return variant
 
    # --------------------------------------------------------
    # 2. SKU match
    # --------------------------------------------------------
    if source_sku:
        for variant in variants:
            variant_sku = clean_text(variant.get("sku"))
            inventory_sku = clean_text(variant.get("inventoryItem", {}).get("sku"))
            if variant_sku == source_sku or inventory_sku == source_sku:
                return variant
 
    # --------------------------------------------------------
    # 3. If only one variant exists
    # --------------------------------------------------------
    if len(variants) == 1:
        return variants[0]
 
    # --------------------------------------------------------
    # 4. Try size option
    # --------------------------------------------------------
    sizes = source.get("sizes") or []
    if sizes:
        wanted = clean_text(sizes[0]).lower()
        for variant in variants:
            selected_options = variant.get("selectedOptions", [])
            for option in selected_options:
                if option.get("name", "").lower() == "size" and option.get("value", "").lower() == wanted:
                    return variant
 
    return None
 
# ============================================================
# BUILD SOURCE PRODUCT
# ============================================================
 
def build_source_product(p: Dict[str, Any]):
    title = clean_text(p.get("title")) or f"Product-{clean_text(p.get('sku'))}"
    handle = slugify(title)
    cost = to_float(p.get("costprice"))
    weight = to_float(p.get("weight"))
    stock = max(0, to_int(p.get("stock")))
    sku = clean_text(p.get("sku")) or None
    barcode = clean_text(p.get("barcode")) or None
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
        "stock": stock,
        "sku": sku,
        "barcode": barcode,
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
mutation ProductUpdate($input: ProductUpdateInput!) {
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
}
"""
 
def update_product_data(product_id, source):
    status = "ACTIVE" if source["stock"] > 0 else "DRAFT"
    input_data = {
        "id": product_id,
        "title": source["title"],
        "descriptionHtml": source["description"],
        "vendor": source["vendor"],
        "productType": source["product_type"],
        "tags": source["tags"],
        "status": status,
    }
 
    data = graphql_request(PRODUCT_UPDATE_MUTATION, {"input": input_data}, operation_name="ProductUpdate")
    payload = data["productUpdate"]
    raise_user_errors(payload.get("userErrors"), "Product update")
 
# ============================================================
# VARIANT BULK UPDATE
# ============================================================
 
VARIANT_BULK_UPDATE_MUTATION = """
mutation ProductVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
    productVariantsBulkUpdate(productId: $productId, variants: $variants) {
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
 
def update_variant(product_id, variant, source):
    variant_input = {"id": variant["id"]}
 
    if UPDATE_PRICES:
        variant_input["price"] = str(source["price"])
 
    if source["barcode"]:
        variant_input["barcode"] = source["barcode"]
 
    # --------------------------------------------------------
    # Weight belongs inside inventoryItem.measurement
    # --------------------------------------------------------
    if source["weight"] > 0:
        variant_input["inventoryItem"] = {
            "measurement": {
                "weight": {
                    "value": source["weight"],
                    "unit": "GRAMS",
                }
            }
        }
 
    # Don't send an empty update.
    if len(variant_input) == 1:
        return
 
    data = graphql_request(VARIANT_BULK_UPDATE_MUTATION, {
        "productId": product_id,
        "variants": [variant_input],
    }, operation_name="ProductVariantsBulkUpdate")
 
    payload = data["productVariantsBulkUpdate"]
    raise_user_errors(payload.get("userErrors"), "Variant update")
 
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
            unitCost {
                amount
            }
        }
        userErrors {
            field
            message
        }
    }
}
"""
 
def update_inventory_item(variant, source):
    inventory_item = variant.get("inventoryItem")
    if not inventory_item:
        raise RuntimeError("Variant has no inventoryItem.")
 
    inventory_item_id = inventory_item["id"]
    input_data = {
        "tracked": True,
        "sku": source.get("sku"),
        "cost": source["cost"],
    }
 
    data = graphql_request(INVENTORY_ITEM_UPDATE_MUTATION, {
        "id": inventory_item_id,
        "input": input_data,
    }, operation_name="InventoryItemUpdate")
 
    payload = data["inventoryItemUpdate"]
    raise_user_errors(payload.get("userErrors"), "Inventory item update")
    return inventory_item_id
 
# ============================================================
# ACTIVATE INVENTORY AT LOCATION
# ============================================================
 
ACTIVATE_INVENTORY_MUTATION = """
mutation InventoryBulkToggleActivation($inventoryItemId: ID!, $inventoryItemUpdates: [InventoryBulkToggleActivationInput!]!) {
    inventoryBulkToggleActivation(inventoryItemId: $inventoryItemId, inventoryItemUpdates: $inventoryItemUpdates) {
        inventoryItem {
            id
        }
        inventoryLevels {
            id
            quantities(names: ["available"]) {
                name
                quantity
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
 
def activate_inventory_at_location(inventory_item_id, location_id):
    data = graphql_request(ACTIVATE_INVENTORY_MUTATION, {
        "inventoryItemId": inventory_item_id,
        "inventoryItemUpdates": [{
            "locationId": location_id,
            "activate": True,
        }],
    }, operation_name="InventoryBulkToggleActivation")
 
    payload = data["inventoryBulkToggleActivation"]
    raise_user_errors(payload.get("userErrors"), "Inventory location activation")
 
# ============================================================
# SET INVENTORY QUANTITY
# ============================================================
 
INVENTORY_SET_MUTATION = """
mutation InventorySetQuantities($input: InventorySetQuantitiesInput!) {
    inventorySetQuantities(input: $input) {
        inventoryAdjustmentGroup {
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
 
def set_inventory_quantity(inventory_item_id, location_id, quantity):
    quantity = max(0, int(quantity))
    data = graphql_request(INVENTORY_SET_MUTATION, {
        "input": {
            "name": "available",
            "reason": "correction",
            "referenceDocumentUri": "supplier-feed://" + str(uuid.uuid4()),
            "ignoreCompareQuantity": True,
            "quantities": [{
                "inventoryItemId": inventory_item_id,
                "locationId": location_id,
                "quantity": quantity,
            }],
        }
    }, operation_name="InventorySetQuantities")
 
    payload = data["inventorySetQuantities"]
    raise_user_errors(payload.get("userErrors"), "Inventory quantity update")
 
# ============================================================
# CREATE PRODUCT
# ============================================================
 
PRODUCT_SET_CREATE_MUTATION = """
mutation ProductSetCreate($input: ProductSetInput!, $synchronous: Boolean!) {
    productSet(input: $input, synchronous: $synchronous) {
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
                        sku
                    }
                    selectedOptions {
                        name
                        value
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
 
def create_product(source):
    # --------------------------------------------------------
    # PRODUCT DATA
    # --------------------------------------------------------
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
    # VARIANTS
    # --------------------------------------------------------
    sizes = source["sizes"] or ["Default Title"]
 
    # --------------------------------------------------------
    # PRODUCT OPTION
    # --------------------------------------------------------
    product_input["productOptions"] = [{
        "name": "Size",
        "position": 1,
        "values": [{"name": size} for size in sizes],
    }]
 
    variants = []
    for size in sizes:
        variant = {
            "optionValues": [{"optionName": "Size", "name": size}],
            "price": str(source["price"]),
            "barcode": source["barcode"] or None,
            "sku": source["sku"] or None,
            "inventoryItem": {
                "cost": source["cost"],
                "tracked": True,
                "sku": source["sku"] or None,
                "measurement": {
                    "weight": {
                        "value": source["weight"],
                        "unit": "GRAMS",
                    }
                },
            },
        }
 
        # Remove None values.
        variant = {key: value for key, value in variant.items() if value is not None}
        if variant.get("inventoryItem"):
            variant["inventoryItem"] = {key: value for key, value in variant["inventoryItem"].items() if value is not None}
 
        variants.append(variant)
 
    product_input["variants"] = variants
    data = graphql_request(PRODUCT_SET_CREATE_MUTATION, {
        "input": product_input,
        "synchronous": True,
    }, operation_name="ProductSetCreate")
 
    payload = data["productSet"]
    raise_user_errors(payload.get("userErrors"), "Product creation")
 
    product = payload.get("product")
    if not product:
        raise RuntimeError("Shopify returned no product after creation.")
 
    return product
 
# ============================================================
# SYNC EXISTING PRODUCT
# ============================================================
 
def sync_existing_product(product, source, location_id):
    product_id = product["id"]
 
    # --------------------------------------------------------
    # PRODUCT INFORMATION
    # --------------------------------------------------------
    if UPDATE_PRODUCT_DATA:
        update_product_data(product_id, source)
 
    # --------------------------------------------------------
    # MATCH VARIANT
    # --------------------------------------------------------
    variant = find_matching_variant(product, source)
    if not variant:
        raise RuntimeError(
            "Could not match supplier item to "
            f"an existing Shopify variant. SKU={source['sku']} "
            f"Barcode={source['barcode']}"
        )
 
    # --------------------------------------------------------
    # VARIANT
    # --------------------------------------------------------
    update_variant(product_id, variant, source)
 
    # --------------------------------------------------------
    # INVENTORY ITEM
    # --------------------------------------------------------
    inventory_item_id = variant["inventoryItem"]["id"]
    if UPDATE_INVENTORY_ITEM:
        update_inventory_item(variant, source)
 
    # --------------------------------------------------------
    # LOCATION
    # --------------------------------------------------------
    if UPDATE_INVENTORY:
        activate_inventory_at_location(inventory_item_id, location_id)
        set_inventory_quantity(inventory_item_id, location_id, source["stock"])
 
# ============================================================
# SYNC NEW PRODUCT
# ============================================================
 
def sync_new_product(source, location_id):
    product = create_product(source)
    product_id = product["id"]
 
    print(f"🆕 Created: {source['title']}")
 
    # --------------------------------------------------------
    # Refresh product so we have inventory item IDs.
    # --------------------------------------------------------
    refreshed = find_product_by_handle(source["handle"])
    if not refreshed:
        raise RuntimeError("Product was created but could not be found again by handle.")
 
    variants = refreshed.get("variants", {}).get("nodes", [])
    if not variants:
        raise RuntimeError("Created product has no variants.")
 
    # --------------------------------------------------------
    # Update every created variant's inventory.
    # --------------------------------------------------------
    for variant in variants:
        inventory_item_id = variant.get("inventoryItem", {}).get("id")
        if not inventory_item_id:
            continue
 
        if UPDATE_INVENTORY_ITEM:
            update_inventory_item(variant, source)
 
        if UPDATE_INVENTORY:
            activate_inventory_at_location(inventory_item_id, location_id)
            set_inventory_quantity(inventory_item_id, location_id, source["stock"])
 
    return product_id
 
# ============================================================
# SYNC ONE PRODUCT
# ============================================================
 
def sync_product(index, total, source_item, location_id):
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
    # FIND EXISTING PRODUCT
    # --------------------------------------------------------
    existing = find_product_by_handle(source["handle"])
 
    # --------------------------------------------------------
    # UPDATE
    # --------------------------------------------------------
    if existing:
        print(f"🔄 Existing product found: {existing['title']}")
        print(f"   Shopify ID: {existing['id']}")
        sync_existing_product(existing, source, location_id)
        print(f"✅ Updated successfully: {title}")
        return "updated"
 
    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------
    if not CREATE_MISSING_PRODUCTS:
        print("⚠️ Product not found and CREATE_MISSING_PRODUCTS=false")
        return "skipped"
 
    sync_new_product(source, location_id)
    print(f"✅ Created successfully: {title}")
    return "created"
 
# ============================================================
# MAIN SYNC
# ============================================================
 
def run_sync():
    global SHOP_URL
    validate_config()
 
    print("\n" + "=" * 70)
    print("🚀 SHOPIFY SUPPLIER SYNC")
    print("=" * 70)
 
    print(f"🏪 Shop: {SHOP_URL}")
    print(f"🔌 Shopify API: {API_VERSION}")
    print(f"👷 Workers: {MAX_WORKERS}")
    print(f"📋 Limit: {LIMIT if LIMIT is not None else 'ALL'}")
    print(f"📦 Inventory updates: {UPDATE_INVENTORY}")
    print(f"💷 Price updates: {UPDATE_PRICES}")
    print("=" * 70)
 
    # --------------------------------------------------------
    # LOCATION
    # --------------------------------------------------------
    location_id = resolve_location_id()
    print(f"📍 Using location ID: {location_id}")
 
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
    skipped = 0
    failed = 0
    failures = []
 
    # --------------------------------------------------------
    # THREAD POOL
    # --------------------------------------------------------
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_index = {}
        for index, item in enumerate(items, start=1):
            future = executor.submit(sync_product, index, total, item, location_id)
            future_to_index[future] = (index, item)
 
        # ----------------------------------------------------
        # COLLECT RESULTS
        # ----------------------------------------------------
        completed = 0
        for future in as_completed(future_to_index):
            index, item = future_to_index[future]
            completed += 1
            title = clean_text(item.get("title"))
 
            try:
                result = future.result()
                if result == "created":
                    created += 1
                elif result == "updated":
                    updated += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failed += 1
 
            except Exception as exc:
                failed += 1
                print("\n" + "!" * 70)
                print(f"❌ ERROR syncing {title}")
                print(f"   Product #{index}")
                print(f"   Error: {exc}")
                print("!" * 70)
                failures.append({
                    "index": index,
                    "title": title,
                    "sku": clean_text(item.get("sku")),
                    "barcode": clean_text(item.get("barcode")),
                    "error": str(exc),
                })
 
            # ------------------------------------------------
            # PROGRESS
            # ------------------------------------------------
            if completed % 25 == 0 or completed == total:
                print("\n" + "-" * 70)
                print(f"📊 Progress: {completed}/{total} ({completed / total * 100:.1f}%)")
                print(f"   Created: {created}")
                print(f"   Updated: {updated}")
                print(f"   Skipped: {skipped}")
                print(f"   Failed:  {failed}")
                print("-" * 70)
 
    # ========================================================
    # SUMMARY
    # ========================================================
    print("\n" + "=" * 70)
    print("✅ SYNC COMPLETE")
    print("=" * 70)
    print(f"🆕 Created: {created}")
    print(f"🔄 Updated: {updated}")
    print(f"⏭️ Skipped: {skipped}")
    print(f"❌ Failed:  {failed}")
    print(f"📦 Total:   {total}")
    print("=" * 70)
 
    # --------------------------------------------------------
    # FAILURE REPORT
    # --------------------------------------------------------
    if failures:
        print("\n" + "=" * 70)
        print("❌ FAILED PRODUCTS")
        print("=" * 70)
        for failure in failures:
            print(f"[{failure['index']}] {failure['title']}")
            print(f"   SKU: {failure['sku']}")
            print(f"   Barcode: {failure['barcode']}")
            print(f"   Error: {failure['error']}")
            print("-" * 70)
 
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
