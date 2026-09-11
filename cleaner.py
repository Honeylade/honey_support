#!/usr/bin/env python3
"""
Honeylade Shopify Cleanup

Removes Shopify products that are managed by the Honeylade tag but whose
supplier Product ID/SKU no longer exists in the current supplier feed.

IMPORTANT:
- Only products with HONEYLADE_TAG are considered.
- DRY_RUN defaults to true.
- Deletion is permanent when DRY_RUN=false.
- The supplier feed may be CSV or XML; format is detected automatically.
- CSV feeds use Product ID (or SKU-like column) as the authoritative ID.
- XML feeds are parsed flexibly and common Product ID/SKU field names are
  recognised, including nested XML elements and attributes.
"""

import csv
import io
import os
import re
import sys
import time
import threading
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SHOP_URL = os.environ.get("SHOP_URL", "").strip().rstrip("/")
XML_URL = os.environ.get("XML_URL", "").strip()
CLIENT_ID = os.environ.get("CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "").strip()

API_VERSION = os.environ.get("API_VERSION", "2026-07").strip()
HONEYLADE_TAG = os.environ.get("HONEYLADE_TAG", "honeylade").strip()

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "6"))
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "5"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "60"))

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() not in {
    "false", "0", "no", "off"
}
MIN_FEED_PRODUCTS = int(os.environ.get("MIN_FEED_PRODUCTS", "100"))
MAX_DELETE_PERCENT = float(os.environ.get("MAX_DELETE_PERCENT", "25"))
MAX_DELETE_COUNT = int(os.environ.get("MAX_DELETE_COUNT", "100"))

GRAPHQL_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------
def log(message: str = "") -> None:
    print(message, flush=True)


def normalise_identifier(value: Any) -> str:
    """Normalise supplier/Shopify identifiers for reliable comparison."""
    if value is None:
        return ""

    text = str(value).strip()
    if not text:
        return ""

    # Remove a UTF-8 BOM and surrounding whitespace/quotes.
    text = text.lstrip("\ufeff").strip().strip('"').strip()

    # Excel/pandas-style numeric IDs sometimes become 12345.0.
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]

    return text.casefold()


def local_name(tag: str) -> str:
    """Return an XML tag without namespace."""
    if not tag:
        return ""
    return tag.rsplit("}", 1)[-1].strip().casefold()


def normalise_key(value: str) -> str:
    """Normalise a field name for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


# ---------------------------------------------------------------------------
# Shopify authentication / HTTP
# ---------------------------------------------------------------------------
_token_cache = {
    "access_token": None,
    "expires_at": 0,
}

_token_lock = threading.Lock()


def get_access_token() -> str:
    """
    Get a Shopify client-credentials access token using the same mechanism
    as the working Honeylade sync script, and cache it until shortly before
    expiry.
    """
    with _token_lock:
        if (
            _token_cache["access_token"]
            and time.time() < _token_cache["expires_at"] - 60
        ):
            return _token_cache["access_token"]

        if not SHOP_URL:
            raise RuntimeError("SHOP_URL is not configured.")
        if not CLIENT_ID:
            raise RuntimeError("CLIENT_ID is not configured.")
        if not CLIENT_SECRET:
            raise RuntimeError("CLIENT_SECRET is not configured.")

        # IMPORTANT: this intentionally matches the working sync script:
        #   https://{SHOP_URL}/admin/oauth/access_token
        #   POST JSON body containing client_id/client_secret/grant_type
        # SHOP_URL is expected to be the bare myshopify.com hostname.
        url = f"https://{SHOP_URL}/admin/oauth/access_token"
        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        }

        log("🔐 Requesting Shopify access token...")

        try:
            response = requests.post(
                url,
                json=data,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Shopify token request failed: {exc}") from exc

        if not response.ok:
            # Do not retry 400/401/403 here. These normally indicate a
            # configuration/authentication problem rather than a transient
            # Shopify failure. Retryable HTTP failures are handled explicitly
            # below for 429/5xx.
            if response.status_code in {429, 500, 502, 503, 504}:
                retry_after = response.headers.get("Retry-After")
                detail = response.text[:1000]
                raise RuntimeError(
                    f"Shopify token request returned retryable HTTP "
                    f"{response.status_code}: {detail}"
                )

            raise RuntimeError(
                f"Shopify token request failed: HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )

        try:
            token_data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "Shopify token response was not valid JSON: "
                f"{response.text[:1000]}"
            ) from exc

        token = token_data.get("access_token")
        if not token:
            raise RuntimeError(
                "Shopify token response did not contain access_token: "
                f"{token_data}"
            )

        expires_in = int(token_data.get("expires_in", 86400))
        _token_cache["access_token"] = token
        _token_cache["expires_at"] = time.time() + expires_in

        log("✅ Shopify access token obtained.")
        return token


def refresh_access_token() -> str:
    """Clear the cached token and obtain a fresh one."""
    with _token_lock:
        _token_cache["access_token"] = None
        _token_cache["expires_at"] = 0
    return get_access_token()


def graphql_request(
    query: str,
    variables: Optional[Dict[str, Any]],
    token: str,
) -> Tuple[Dict[str, Any], str]:
    """Run GraphQL, refreshing the token once if Shopify returns 401."""
    current_token = token

    for auth_attempt in range(2):
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": current_token,
        }

        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.post(
                    GRAPHQL_URL,
                    json={"query": query, "variables": variables or {}},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )

                if response.status_code == 401 and auth_attempt == 0:
                    log("🔄 Shopify returned 401; refreshing access token...")
                    current_token = refresh_access_token()
                    break

                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else RETRY_DELAY * attempt
                    log(
                        f"⚠️ Shopify GraphQL returned {response.status_code}; "
                        f"retrying in {delay:g}s..."
                    )
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                data = response.json()

                if data.get("errors"):
                    raise RuntimeError(f"Shopify GraphQL errors: {data['errors']}")

                return data.get("data", {}), current_token

            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt >= MAX_RETRIES:
                    break
                delay = RETRY_DELAY * attempt
                log(f"⚠️ GraphQL request error: {exc}; retrying in {delay:g}s...")
                time.sleep(delay)
        else:
            raise RuntimeError(f"Shopify GraphQL request failed: {last_error}")

        # A 401 caused a token refresh and needs to retry the request.
        if current_token != headers["X-Shopify-Access-Token"]:
            continue

        raise RuntimeError(f"Shopify GraphQL request failed: {last_error}")

    raise RuntimeError("Shopify authentication failed after token refresh.")


# ---------------------------------------------------------------------------
# Supplier feed download / format detection
# ---------------------------------------------------------------------------
def download_feed() -> bytes:
    if not XML_URL:
        raise RuntimeError("XML_URL is not configured.")

    last_error: Optional[Exception] = None

    headers = {
        "User-Agent": "Honeylade-Shopify-Cleanup/1.0",
        "Accept": "text/csv,application/csv,application/xml,text/xml;q=0.9,*/*;q=0.8",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"📥 Downloading supplier feed (attempt {attempt}/{MAX_RETRIES})...")
        try:
            response = requests.get(XML_URL, headers=headers, timeout=REQUEST_TIMEOUT)

            if response.status_code == 200:
                content = response.content
                log(f"✅ Feed downloaded ({len(content):,} bytes)")
                return content

            retryable = response.status_code in {429, 500, 502, 503, 504}
            error = RuntimeError(
                f"Feed download failed ({response.status_code}): {response.text[:500]}"
            )
            if not retryable:
                raise error

            last_error = error
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else RETRY_DELAY * attempt
            log(f"⚠️ Feed returned {response.status_code}; retrying in {delay:g}s...")
            time.sleep(delay)

        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                break
            delay = RETRY_DELAY * attempt
            log(f"⚠️ Feed download error: {exc}; retrying in {delay:g}s...")
            time.sleep(delay)

    raise RuntimeError(f"Could not download supplier feed: {last_error}")


def detect_feed_format(content: bytes, content_type: str = "") -> str:
    """Detect XML/CSV without trusting the URL extension or HTTP header."""
    sample = content.lstrip(b"\xef\xbb\xbf \t\r\n")
    lowered = sample[:1000].lower()
    header = content_type.casefold()

    if lowered.startswith(b"<?xml") or lowered.startswith(b"<"):
        return "xml"

    if "xml" in header:
        # Only call it XML when the payload actually looks XML-like.
        if b"<" in sample[:2000]:
            return "xml"

    if "csv" in header or "spreadsheet" in header:
        return "csv"

    # Sniff CSV by looking for a header row containing a likely ID column.
    try:
        text = content[:100_000].decode("utf-8-sig", errors="replace")
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if "," in first_line or "\t" in first_line or ";" in first_line:
            return "csv"
    except Exception:
        pass

    # Final fallback: XML if it parses as XML, otherwise CSV.
    try:
        ET.fromstring(content)
        return "xml"
    except ET.ParseError:
        return "csv"


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------
CSV_ID_COLUMNS = {
    "productid",
    "productsku",
    "sku",
    "productcode",
    "itemcode",
    "itemid",
    "stockcode",
    "stockid",
    "productnumber",
    "itemnumber",
}


def find_csv_id_column(fieldnames: Iterable[str]) -> Optional[str]:
    fields = list(fieldnames)
    normalised = {normalise_key(f): f for f in fields if f}

    # Exact preferred name first.
    for candidate in ("productid", "sku", "productsku", "productcode"):
        if candidate in normalised:
            return normalised[candidate]

    for key, original in normalised.items():
        if key in CSV_ID_COLUMNS:
            return original

    # Conservative fuzzy matching; avoid accidentally using Barcode as SKU.
    for key, original in normalised.items():
        if "product" in key and ("id" in key or "sku" in key or "code" in key):
            return original
        if key in {"id", "code"}:
            return original

    return None


def parse_csv_feed(content: bytes) -> Set[str]:
    text = content.decode("utf-8-sig", errors="replace")
    sample = text[:100_000]

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise RuntimeError("CSV feed has no header row.")

    log("📋 Feed columns detected:")
    for field in reader.fieldnames:
        log(f"   {field}")

    id_column = find_csv_id_column(reader.fieldnames)
    if not id_column:
        raise RuntimeError(
            "Could not find Product ID/SKU column in supplier CSV feed. "
            f"Columns: {reader.fieldnames}"
        )

    log(f"🔑 Supplier identifier column: {id_column}")

    ids: Set[str] = set()
    row_count = 0

    for row in reader:
        row_count += 1
        identifier = normalise_identifier(row.get(id_column))
        if identifier:
            ids.add(identifier)

    log(f"📦 CSV product rows: {row_count:,}")
    log(f"🔑 Unique supplier Product IDs: {len(ids):,}")

    if not ids:
        raise RuntimeError("CSV feed contained no usable Product IDs/SKUs.")

    return ids


# ---------------------------------------------------------------------------
# XML parser
# ---------------------------------------------------------------------------
XML_ID_NAMES = {
    "productid",
    "productsku",
    "sku",
    "productcode",
    "itemcode",
    "itemid",
    "stockcode",
    "stockid",
    "productnumber",
    "itemnumber",
}

XML_RECORD_HINTS = {
    "product",
    "item",
    "record",
    "productitem",
    "catalogitem",
    "productrecord",
}


def iter_xml_elements(root: ET.Element) -> Iterable[ET.Element]:
    for element in root.iter():
        yield element


def find_xml_identifier(element: ET.Element) -> str:
    """Find a likely product ID/SKU inside one XML record."""
    # 1. Attributes, preferring exact names.
    attributes = {normalise_key(local_name(k)): v for k, v in element.attrib.items()}
    for candidate in ("productid", "sku", "productsku", "productcode", "itemid", "itemcode"):
        value = attributes.get(candidate)
        if value:
            return normalise_identifier(value)

    # 2. Direct child fields.
    children = list(element)
    child_map: Dict[str, ET.Element] = {}
    for child in children:
        child_map[normalise_key(local_name(child.tag))] = child

    for candidate in ("productid", "sku", "productsku", "productcode", "itemid", "itemcode"):
        child = child_map.get(candidate)
        if child is not None and child.text:
            value = normalise_identifier(child.text)
            if value:
                return value

    # 3. Descendant fields, but only exact identifier names.
    for descendant in element.iter():
        if descendant is element:
            continue
        key = normalise_key(local_name(descendant.tag))
        if key in XML_ID_NAMES and descendant.text:
            value = normalise_identifier(descendant.text)
            if value:
                return value

    return ""


def parse_xml_feed(content: bytes) -> Set[str]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        # Provide a useful diagnostic without dumping the whole feed.
        preview = content[:300].decode("utf-8", errors="replace").replace("\n", " ")
        raise RuntimeError(
            f"Supplier feed was detected as XML but could not be parsed: {exc}. "
            f"Feed begins: {preview!r}"
        ) from exc

    log(f"📋 XML root element: {local_name(root.tag)}")

    # First identify plausible record elements. We prefer direct/repeated
    # product/item-like children of the document, then fall back to any
    # element containing an exact identifier field.
    records: List[ET.Element] = []

    for child in list(root):
        if local_name(child.tag) in XML_RECORD_HINTS:
            records.append(child)

    if not records:
        for element in root.iter():
            name = local_name(element.tag)
            if name in XML_RECORD_HINTS:
                records.append(element)

    ids: Set[str] = set()

    if records:
        for record in records:
            identifier = find_xml_identifier(record)
            if identifier:
                ids.add(identifier)
    else:
        # Generic fallback for supplier XMLs with unusual record names.
        for element in root.iter():
            identifier = find_xml_identifier(element)
            if identifier:
                ids.add(identifier)

    if not ids:
        # Last diagnostic: show the XML field names that look like IDs.
        candidates: Set[str] = set()
        for element in root.iter():
            name = normalise_key(local_name(element.tag))
            if "sku" in name or ("product" in name and "id" in name) or name in {
                "itemid",
                "itemcode",
                "productcode",
            }:
                candidates.add(local_name(element.tag))
        raise RuntimeError(
            "Could not find Product ID/SKU values in XML feed. "
            f"Potential identifier fields found: {sorted(candidates)}"
        )

    log(f"📦 XML product records detected: {len(records):,}")
    log(f"🔑 Unique supplier Product IDs/SKUs: {len(ids):,}")

    return ids


def parse_supplier_feed(content: bytes) -> Set[str]:
    """Automatically detect and parse CSV or XML."""
    feed_format = detect_feed_format(content)
    log(f"🔎 Supplier feed format detected: {feed_format.upper()}")

    if feed_format == "xml":
        return parse_xml_feed(content)
    return parse_csv_feed(content)


# ---------------------------------------------------------------------------
# Shopify product retrieval
# ---------------------------------------------------------------------------
PRODUCTS_QUERY = """
query HoneyladeProducts($cursor: String, $query: String!) {
  products(first: 250, after: $cursor, query: $query) {
    nodes {
      id
      title
      handle
      tags
      variants(first: 250) {
        nodes {
          id
          sku
          barcode
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def has_exact_tag(tags: List[str], wanted: str) -> bool:
    wanted_norm = wanted.casefold().strip()
    return any(str(tag).casefold().strip() == wanted_norm for tag in tags)


def fetch_honeylade_products(token: str) -> Tuple[List[Dict[str, Any]], str]:
    products: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    page = 0

    # Shopify search query narrows the server-side result set. We also verify
    # the exact tag locally, so a search parser/escaping quirk cannot broaden
    # the deletion scope.
    query_text = f'tag:"{HONEYLADE_TAG.replace(chr(34), chr(92) + chr(34))}"'

    while True:
        page += 1
        log(f"🔎 Reading Shopify Honeylade products page {page}...")

        data, token = graphql_request(
            PRODUCTS_QUERY,
            {"cursor": cursor, "query": query_text},
            token,
        )

        connection = data.get("products")
        if not connection:
            raise RuntimeError("Shopify did not return a products connection.")

        for product in connection.get("nodes", []):
            tags = product.get("tags") or []
            if has_exact_tag(tags, HONEYLADE_TAG):
                products.append(product)

        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise RuntimeError("Shopify indicated another page but returned no cursor.")

    log(f"🏷️ Honeylade-tagged Shopify products found: {len(products):,}")
    return products, token


# ---------------------------------------------------------------------------
# Deletion logic
# ---------------------------------------------------------------------------
PRODUCT_DELETE_MUTATION = """
mutation HoneyladeDeleteProduct($input: ProductDeleteInput!) {
  productDelete(input: $input, synchronous: true) {
    deletedProductId
    userErrors {
      field
      message
    }
  }
}
"""


def product_matches_feed(product: Dict[str, Any], supplier_ids: Set[str]) -> bool:
    """Return true if any Shopify variant identifier still exists in feed."""
    variants = (product.get("variants") or {}).get("nodes") or []

    for variant in variants:
        sku = normalise_identifier(variant.get("sku"))
        if sku and sku in supplier_ids:
            return True

    return False


def describe_product(product: Dict[str, Any]) -> str:
    """Return a compact one-line description for normal deletion logging."""
    variants = (product.get("variants") or {}).get("nodes") or []
    skus = [normalise_identifier(v.get("sku")) for v in variants]
    skus = [s for s in skus if s]
    sku_text = ", ".join(skus) if skus else "no SKU"
    return f"{product.get('title') or '(untitled)'} | {sku_text} | {product.get('id')}"


def print_stale_products(stale_products: List[Dict[str, Any]]) -> None:
    """Print every stale product and every variant SKU before any safety stop."""
    log("📋 PRODUCTS THAT WOULD BE DELETED:")
    log("=" * 70)

    for index, product in enumerate(stale_products, start=1):
        variants = (product.get("variants") or {}).get("nodes") or []
        log(f"{index}. {product.get('title') or '(untitled)'}")
        log(f"   Product ID: {product.get('id') or '(unknown)'}")
        log(f"   Handle: {product.get('handle') or '(none)'}")

        if variants:
            log("   Variant SKUs:")
            for variant in variants:
                sku = normalise_identifier(variant.get("sku"))
                log(f"      - {sku if sku else '(no SKU)'}")
        else:
            log("   Variant SKUs: (no variants returned)")

        log()

    log("=" * 70)


def delete_product(product: Dict[str, Any], token: str) -> str:
    data, token = graphql_request(
        PRODUCT_DELETE_MUTATION,
        {"input": {"id": product["id"]}},
        token,
    )

    payload = data.get("productDelete") or {}
    errors = payload.get("userErrors") or []
    if errors:
        raise RuntimeError(f"Shopify productDelete errors: {errors}")

    deleted_id = payload.get("deletedProductId")
    if not deleted_id:
        raise RuntimeError("Shopify productDelete returned no deletedProductId.")

    return token


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def validate_config() -> None:
    missing = [
        name
        for name, value in {
            "SHOP_URL": SHOP_URL,
            "XML_URL": XML_URL,
            "CLIENT_ID": CLIENT_ID,
            "CLIENT_SECRET": CLIENT_SECRET,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

    if not HONEYLADE_TAG:
        raise RuntimeError("HONEYLADE_TAG cannot be empty.")

    if MIN_FEED_PRODUCTS < 1:
        raise RuntimeError("MIN_FEED_PRODUCTS must be at least 1.")
    if MAX_DELETE_PERCENT < 0:
        raise RuntimeError("MAX_DELETE_PERCENT cannot be negative.")
    if MAX_DELETE_COUNT < 0:
        raise RuntimeError("MAX_DELETE_COUNT cannot be negative.")


def main() -> int:
    log("=" * 70)
    log("🧹 HONEYLADE SHOPIFY CLEANUP")
    log("=" * 70)
    log(f"🏷️ Managed tag: {HONEYLADE_TAG}")
    log(f"🔧 API version: {API_VERSION}")
    log(f"🧪 Dry run: {DRY_RUN}")
    log()

    validate_config()

    token = get_access_token()
    log("🔐 Shopify authentication: OK")
    log()

    feed_content = download_feed()
    supplier_ids = parse_supplier_feed(feed_content)

    if len(supplier_ids) < MIN_FEED_PRODUCTS:
        raise RuntimeError(
            f"Safety stop: supplier feed contains only {len(supplier_ids):,} unique "
            f"Product IDs, below MIN_FEED_PRODUCTS={MIN_FEED_PRODUCTS:,}. "
            "No Shopify products will be deleted."
        )

    log(f"🛡️ Feed safety check passed: {len(supplier_ids):,} products")
    log()

    products, token = fetch_honeylade_products(token)

    if not products:
        log("ℹ️ No Shopify products with the managed Honeylade tag were found.")
        log("✅ CLEANUP COMPLETE")
        return 0

    stale_products = [
        product
        for product in products
        if not product_matches_feed(product, supplier_ids)
    ]

    stale_count = len(stale_products)
    managed_count = len(products)
    stale_percent = (stale_count / managed_count * 100) if managed_count else 0.0

    log(f"📊 Managed Shopify products: {managed_count:,}")
    log(f"📊 Products still present in feed: {managed_count - stale_count:,}")
    log(f"🗑️ Products no longer in feed: {stale_count:,} ({stale_percent:.2f}%)")
    log()

    if stale_count == 0:
        log("✅ Nothing to remove. All Honeylade-tagged products still exist in the feed.")
        log("✅ CLEANUP COMPLETE")
        return 0

    # Always print the complete stale-product list before either safety stop.
    # This is informational only; nothing is deleted by this function.
    print_stale_products(stale_products)

    if stale_count > MAX_DELETE_COUNT:
        raise RuntimeError(
            f"Safety stop: {stale_count:,} products would be deleted, exceeding "
            f"MAX_DELETE_COUNT={MAX_DELETE_COUNT:,}. No deletions performed."
        )

    if stale_percent > MAX_DELETE_PERCENT:
        raise RuntimeError(
            f"Safety stop: {stale_percent:.2f}% of Honeylade-tagged products would be "
            f"deleted, exceeding MAX_DELETE_PERCENT={MAX_DELETE_PERCENT:.2f}%. "
            "No deletions performed."
        )

    if DRY_RUN:
        log("🧪 DRY RUN enabled — NO PRODUCTS WERE DELETED.")
        log("➡️ Set DRY_RUN=false only after reviewing the list above.")
        log("✅ CLEANUP COMPLETE (DRY RUN)")
        return 0

    log("⚠️ DRY_RUN=false — permanent Shopify product deletion is starting.")

    deleted = 0
    failed = 0

    for product in stale_products:
        description = describe_product(product)
        log(f"🗑️ Deleting: {description}")
        try:
            token = delete_product(product, token)
            deleted += 1
            log("   ✅ Deleted")
        except Exception as exc:
            failed += 1
            log(f"   ❌ Delete failed: {exc}")

    log()
    log("=" * 70)
    log("🧹 CLEANUP SUMMARY")
    log("=" * 70)
    log(f"🏷️ Honeylade-tagged products checked: {managed_count:,}")
    log(f"🗑️ Products selected: {stale_count:,}")
    log(f"✅ Successfully deleted: {deleted:,}")
    log(f"❌ Failed deletions: {failed:,}")

    if failed:
        log("❌ CLEANUP FINISHED WITH ERRORS")
        return 1

    log("✅ CLEANUP COMPLETE")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("\n❌ CLEANUP INTERRUPTED")
        raise SystemExit(130)
    except Exception as exc:
        log()
        log(f"❌ CLEANUP STOPPED: {exc}")
        raise SystemExit(1)
