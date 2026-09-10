import csv
import io
import os
import sys
import time
import uuid
import requests

from typing import Dict, List, Set, Optional


# ============================================================
# CONFIGURATION
# ============================================================

SHOP_URL = os.getenv("SHOP_URL", "").strip()
XML_URL = os.getenv("XML_URL", "").strip()

CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip()

API_VERSION = os.getenv(
    "API_VERSION",
    "2026-07",
).strip()

# Product tag that identifies products managed by Honeylade.
HONEYLADE_TAG = os.getenv(
    "HONEYLADE_TAG",
    "honeylade",
).strip()

# Number of products Shopify returns per page.
PAGE_SIZE = int(
    os.getenv("PAGE_SIZE", "100")
)

# Retry settings.
MAX_RETRIES = int(
    os.getenv("MAX_RETRIES", "6")
)

RETRY_DELAY = float(
    os.getenv("RETRY_DELAY", "5")
)

REQUEST_TIMEOUT = int(
    os.getenv("REQUEST_TIMEOUT", "60")
)

# ------------------------------------------------------------
# SAFETY
# ------------------------------------------------------------

# IMPORTANT:
# Set DRY_RUN=true first.
#
# When true, the script reports what it WOULD delete,
# but does not delete anything.
DRY_RUN = os.getenv(
    "DRY_RUN",
    "true",
).strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)

# Prevent an unexpectedly small/broken feed from deleting
# a large portion of the Honeylade catalogue.
#
# Example:
# If Shopify has 2,000 Honeylade products and the feed only
# contains 10 products, the script stops instead of deleting
# 1,990 products.
MIN_FEED_PRODUCTS = int(
    os.getenv(
        "MIN_FEED_PRODUCTS",
        "10",
    )
)

# Maximum percentage of Honeylade products that may be
# deleted in one run.
#
# 25 means the script refuses to delete more than 25%.
MAX_DELETE_PERCENT = float(
    os.getenv(
        "MAX_DELETE_PERCENT",
        "25",
    )
)

# Optional absolute maximum.
#
# Example:
# MAX_DELETE_COUNT=100 means never delete more than 100
# products in a single run.
MAX_DELETE_COUNT = int(
    os.getenv(
        "MAX_DELETE_COUNT",
        "100",
    )
)


# ============================================================
# TOKEN CACHE
# ============================================================

_token_cache = {
    "access_token": None,
    "expires_at": 0,
}


# ============================================================
# HELPERS
# ============================================================

def require_environment() -> None:

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


# ============================================================
# SHOPIFY ACCESS TOKEN
# ============================================================

def get_access_token() -> str:

    cached_token = _token_cache.get(
        "access_token"
    )

    expires_at = _token_cache.get(
        "expires_at",
        0,
    )

    if (
        cached_token
        and time.time() < expires_at - 60
    ):
        return cached_token

    url = (
        f"https://{SHOP_URL}"
        "/admin/oauth/access_token"
    )

    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }

    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        try:

            print(
                f"🔐 Requesting Shopify access token "
                f"(attempt {attempt}/{MAX_RETRIES})..."
            )

            response = requests.post(
                url,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            # ------------------------------------------------
            # TEMPORARY HTTP ERRORS
            # ------------------------------------------------

            if response.status_code in (
                429,
                500,
                502,
                503,
                504,
            ):

                if attempt < MAX_RETRIES:

                    retry_after = response.headers.get(
                        "Retry-After"
                    )

                    if retry_after:
                        try:
                            delay = float(
                                retry_after
                            )
                        except (
                            ValueError,
                            TypeError,
                        ):
                            delay = (
                                RETRY_DELAY
                                * attempt
                            )
                    else:
                        delay = (
                            RETRY_DELAY
                            * attempt
                        )

                    print(
                        f"⚠️ Shopify token request "
                        f"returned HTTP "
                        f"{response.status_code}"
                    )

                    print(
                        f"   Retrying in "
                        f"{delay:.1f}s..."
                    )

                    time.sleep(delay)
                    continue

                raise RuntimeError(
                    "Shopify access token failed "
                    f"after {MAX_RETRIES} attempts: "
                    f"HTTP {response.status_code}"
                )

            # ------------------------------------------------
            # NON-RETRYABLE ERROR
            # ------------------------------------------------

            if not response.ok:

                raise RuntimeError(
                    "Shopify access token failed: "
                    f"HTTP {response.status_code}: "
                    f"{response.text[:1000]}"
                )

            data = response.json()

            token = data.get(
                "access_token"
            )

            if not token:
                raise RuntimeError(
                    "Shopify token response did not "
                    f"contain access_token: {data}"
                )

            expires_in = int(
                data.get(
                    "expires_in",
                    86400,
                )
            )

            _token_cache[
                "access_token"
            ] = token

            _token_cache[
                "expires_at"
            ] = (
                time.time()
                + expires_in
            )

            print(
                "✅ Shopify access token obtained."
            )

            return token

        except requests.RequestException as exc:

            last_error = exc

            if attempt < MAX_RETRIES:

                delay = (
                    RETRY_DELAY
                    * attempt
                )

                print(
                    f"⚠️ Network error obtaining "
                    f"Shopify token: {exc}"
                )

                print(
                    f"   Retrying in "
                    f"{delay:.1f}s..."
                )

                time.sleep(delay)

            else:
                break

    raise RuntimeError(
        "Shopify access token failed after "
        f"{MAX_RETRIES} attempts: "
        f"{last_error}"
    )


# ============================================================
# SHOPIFY GRAPHQL
# ============================================================

def graphql(
    query: str,
    variables: Optional[Dict] = None,
    retry_auth: bool = True,
) -> Dict:

    url = (
        f"https://{SHOP_URL}"
        f"/admin/api/{API_VERSION}"
        "/graphql.json"
    )

    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        token = get_access_token()

        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        }

        try:

            response = requests.post(
                url,
                headers=headers,
                json={
                    "query": query,
                    "variables": variables or {},
                },
                timeout=REQUEST_TIMEOUT,
            )

            # ------------------------------------------------
            # TOKEN EXPIRED / INVALID
            # ------------------------------------------------

            if response.status_code == 401:

                if retry_auth:

                    print(
                        "⚠️ Shopify returned 401."
                    )

                    print(
                        "   Clearing cached token "
                        "and requesting a new one..."
                    )

                    _token_cache[
                        "access_token"
                    ] = None

                    _token_cache[
                        "expires_at"
                    ] = 0

                    return graphql(
                        query,
                        variables,
                        retry_auth=False,
                    )

                raise RuntimeError(
                    "Shopify authentication failed "
                    "after refreshing the token."
                )

            # ------------------------------------------------
            # TEMPORARY HTTP ERRORS
            # ------------------------------------------------

            if response.status_code in (
                429,
                500,
                502,
                503,
                504,
            ):

                if attempt < MAX_RETRIES:

                    delay = (
                        RETRY_DELAY
                        * attempt
                    )

                    print(
                        f"⚠️ Shopify HTTP "
                        f"{response.status_code}"
                    )

                    print(
                        f"   Retrying in "
                        f"{delay:.1f}s..."
                    )

                    time.sleep(delay)
                    continue

            response.raise_for_status()

            result = response.json()

            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------

            if result.get("errors"):

                errors = result["errors"]

                # Retry throttling / transient errors.
                error_text = str(
                    errors
                ).lower()

                transient = any(
                    phrase in error_text
                    for phrase in (
                        "throttled",
                        "timeout",
                        "temporarily",
                        "internal",
                        "service unavailable",
                    )
                )

                if (
                    transient
                    and attempt < MAX_RETRIES
                ):

                    delay = (
                        RETRY_DELAY
                        * attempt
                    )

                    print(
                        f"⚠️ Shopify GraphQL "
                        f"temporary error"
                    )

                    print(
                        f"   Retrying in "
                        f"{delay:.1f}s..."
                    )

                    time.sleep(delay)
                    continue

                raise RuntimeError(
                    "Shopify GraphQL errors: "
                    + str(errors)
                )

            return result.get(
                "data",
                {},
            )

        except requests.RequestException as exc:

            last_error = exc

            if attempt < MAX_RETRIES:

                delay = (
                    RETRY_DELAY
                    * attempt
                )

                print(
                    f"⚠️ Shopify network error: "
                    f"{exc}"
                )

                print(
                    f"   Retrying in "
                    f"{delay:.1f}s..."
                )

                time.sleep(delay)
                continue

            break

    raise RuntimeError(
        "Shopify GraphQL request failed "
        f"after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


# ============================================================
# DOWNLOAD SUPPLIER CSV
# ============================================================

def download_feed() -> bytes:

    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        try:

            print(
                f"📥 Downloading supplier feed "
                f"(attempt {attempt}/{MAX_RETRIES})..."
            )

            response = requests.get(
                XML_URL,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            content = response.content

            if not content:
                raise RuntimeError(
                    "Supplier feed is empty."
                )

            print(
                f"✅ Feed downloaded "
                f"({len(content):,} bytes)"
            )

            return content

        except requests.RequestException as exc:

            last_error = exc

            if attempt < MAX_RETRIES:

                delay = (
                    RETRY_DELAY
                    * attempt
                )

                print(
                    f"⚠️ Feed download failed: "
                    f"{exc}"
                )

                print(
                    f"   Retrying in "
                    f"{delay:.1f}s..."
                )

                time.sleep(delay)

            else:
                break

    raise RuntimeError(
        "Supplier feed download failed "
        f"after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


# ============================================================
# PARSE CSV FEED
# ============================================================

def parse_feed(
    content: bytes,
) -> Set[str]:

    # --------------------------------------------------------
    # UTF-8 BOM
    # --------------------------------------------------------

    try:
        text = content.decode(
            "utf-8-sig"
        )

    except UnicodeDecodeError:

        print(
            "⚠️ Feed is not valid UTF-8."
        )

        print(
            "   Trying Windows-1252..."
        )

        text = content.decode(
            "cp1252",
            errors="replace",
        )

    stream = io.StringIO(
        text,
        newline="",
    )

    reader = csv.DictReader(
        stream
    )

    if not reader.fieldnames:
        raise RuntimeError(
            "CSV feed has no headers."
        )

    headers = {
        h.strip()
        for h in reader.fieldnames
        if h
    }

    print(
        "📋 Feed columns detected:"
    )

    print(
        "   "
        + ", ".join(
            sorted(headers)
        )
    )

    # --------------------------------------------------------
    # PRODUCT ID IS THE AUTHORITATIVE MATCH KEY
    # --------------------------------------------------------

    product_id_column = None

    for candidate in (
        "Product ID",
        "ProductID",
        "SKU",
        "Sku",
        "sku",
    ):

        if candidate in headers:
            product_id_column = candidate
            break

    if not product_id_column:

        raise RuntimeError(
            "Could not find Product ID/SKU "
            "column in supplier feed."
        )

    feed_ids = set()

    row_count = 0

    for row in reader:

        row_count += 1

        raw_id = row.get(
            product_id_column,
            "",
        )

        if raw_id is None:
            continue

        product_id = str(
            raw_id
        ).strip()

        if product_id:
            feed_ids.add(
                product_id
            )

    print(
        f"📊 Feed rows: {row_count:,}"
    )

    print(
        f"📊 Unique product IDs: "
        f"{len(feed_ids):,}"
    )

    if len(feed_ids) < MIN_FEED_PRODUCTS:

        raise RuntimeError(
            "SAFETY STOP: Supplier feed contains "
            f"only {len(feed_ids)} product IDs. "
            f"Minimum required is "
            f"{MIN_FEED_PRODUCTS}."
        )

    return feed_ids


# ============================================================
# SHOPIFY: GET HONEYLADE PRODUCTS
# ============================================================

PRODUCTS_QUERY = """
query GetHoneyladeProducts(
    $first: Int!
    $after: String
    $query: String!
) {
    products(
        first: $first
        after: $after
        query: $query
    ) {
        nodes {
            id
            title
            handle
            tags

            variants(first: 100) {
                nodes {
                    id
                    sku
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


def get_honeylade_products() -> List[Dict]:

    products = []

    cursor = None

    search_query = (
        f'tag:"{HONEYLADE_TAG}"'
    )

    while True:

        data = graphql(
            PRODUCTS_QUERY,
            {
                "first": PAGE_SIZE,
                "after": cursor,
                "query": search_query,
            },
        )

        connection = data.get(
            "products"
        )

        if not connection:
            raise RuntimeError(
                "Shopify returned no products "
                "connection."
            )

        nodes = connection.get(
            "nodes",
            [],
        )

        products.extend(nodes)

        page_info = connection.get(
            "pageInfo",
            {},
        )

        if not page_info.get(
            "hasNextPage"
        ):
            break

        cursor = page_info.get(
            "endCursor"
        )

        if not cursor:
            raise RuntimeError(
                "Shopify reported another page "
                "but did not provide a cursor."
            )

    return products


# ============================================================
# DELETE PRODUCT
# ============================================================

PRODUCT_DELETE_MUTATION = """
mutation ProductDelete(
    $input: ProductDeleteInput!
) {
    productDelete(
        input: $input
        synchronous: true
    ) {
        deletedProductId

        userErrors {
            field
            message
        }
    }
}
"""


def delete_product(
    product_id: str,
) -> None:

    data = graphql(
        PRODUCT_DELETE_MUTATION,
        {
            "input": {
                "id": product_id,
            }
        },
    )

    payload = data.get(
        "productDelete"
    )

    if not payload:
        raise RuntimeError(
            "Shopify returned no "
            "productDelete payload."
        )

    errors = payload.get(
        "userErrors",
        [],
    )

    if errors:

        raise RuntimeError(
            "Shopify product deletion failed: "
            + str(errors)
        )

    deleted_id = payload.get(
        "deletedProductId"
    )

    if deleted_id != product_id:

        raise RuntimeError(
            "Shopify did not confirm deletion "
            f"of {product_id}."
        )


# ============================================================
# DETERMINE PRODUCTS TO DELETE
# ============================================================

def find_products_to_delete(
    products: List[Dict],
    feed_ids: Set[str],
) -> List[Dict]:

    candidates = []

    for product in products:

        product_id = product.get(
            "id"
        )

        title = product.get(
            "title",
            "",
        )

        tags = {
            str(tag).strip().lower()
            for tag in product.get(
                "tags",
                [],
            )
        }

        # ----------------------------------------------------
        # SECONDARY SAFETY CHECK:
        # Even though the Shopify query uses the tag,
        # verify the tag ourselves.
        # ----------------------------------------------------

        if HONEYLADE_TAG.lower() not in tags:

            print(
                f"⚠️ SKIP {title} "
                f"({product_id}) - "
                f"honeylade tag not confirmed."
            )

            continue

        variants = (
            product.get(
                "variants",
                {},
            )
            .get(
                "nodes",
                [],
            )
        )

        skus = set()

        for variant in variants:

            sku = variant.get(
                "sku"
            )

            if sku is not None:

                sku = str(
                    sku
                ).strip()

                if sku:
                    skus.add(
                        sku
                    )

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # A product is considered present if ANY of its
        # variants has a SKU in the current supplier feed.
        #
        # Therefore it will only be deleted when NONE of its
        # SKUs exist in the feed.
        # ----------------------------------------------------

        matching_skus = (
            skus.intersection(
                feed_ids
            )
        )

        if matching_skus:

            continue

        candidates.append(
            {
                "id": product_id,
                "title": title,
                "handle": product.get(
                    "handle",
                    "",
                ),
                "skus": sorted(
                    skus
                ),
            }
        )

    return candidates


# ============================================================
# SAFETY CHECK
# ============================================================

def validate_deletion_count(
    honeylade_count: int,
    deletion_count: int,
) -> None:

    if deletion_count == 0:
        return

    if honeylade_count <= 0:
        raise RuntimeError(
            "SAFETY STOP: No Honeylade products "
            "were found in Shopify."
        )

    percentage = (
        deletion_count
        / honeylade_count
        * 100
    )

    print(
        f"🛡️ Deletion safety check:"
    )

    print(
        f"   Honeylade products: "
        f"{honeylade_count:,}"
    )

    print(
        f"   Products to delete: "
        f"{deletion_count:,}"
    )

    print(
        f"   Deletion percentage: "
        f"{percentage:.2f}%"
    )

    if (
        percentage
        > MAX_DELETE_PERCENT
    ):

        raise RuntimeError(
            "SAFETY STOP: This run would delete "
            f"{percentage:.2f}% of Honeylade products. "
            f"Maximum allowed is "
            f"{MAX_DELETE_PERCENT:.2f}%."
        )

    if (
        MAX_DELETE_COUNT > 0
        and deletion_count
        > MAX_DELETE_COUNT
    ):

        raise RuntimeError(
            "SAFETY STOP: This run would delete "
            f"{deletion_count:,} products. "
            f"Maximum allowed is "
            f"{MAX_DELETE_COUNT:,}."
        )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    print("=" * 70)
    print("🧹 HONEYLADE SHOPIFY CLEANUP")
    print("=" * 70)

    print(
        f"🏷️ Managed tag: {HONEYLADE_TAG}"
    )

    print(
        f"🔧 API version: {API_VERSION}"
    )

    print(
        f"🧪 Dry run: {DRY_RUN}"
    )

    print()

    require_environment()

    # --------------------------------------------------------
    # AUTHENTICATE
    # --------------------------------------------------------

    get_access_token()

    print(
        "🔐 Shopify authentication: OK"
    )

    print()

    # --------------------------------------------------------
    # DOWNLOAD FEED
    # --------------------------------------------------------

    feed_content = download_feed()

    print()

    # --------------------------------------------------------
    # PARSE FEED
    # --------------------------------------------------------

    feed_ids = parse_feed(
        feed_content
    )

    print()

    # --------------------------------------------------------
    # GET SHOPIFY HONEYLADE PRODUCTS
    # --------------------------------------------------------

    print(
        f"🔎 Finding Shopify products "
        f"tagged '{HONEYLADE_TAG}'..."
    )

    products = (
        get_honeylade_products()
    )

    print(
        f"📦 Honeylade products found: "
        f"{len(products):,}"
    )

    print()

    # --------------------------------------------------------
    # FIND REMOVED PRODUCTS
    # --------------------------------------------------------

    to_delete = (
        find_products_to_delete(
            products,
            feed_ids,
        )
    )

    print(
        f"🗑️ Products no longer in feed: "
        f"{len(to_delete):,}"
    )

    print()

    # --------------------------------------------------------
    # SAFETY CHECK
    # --------------------------------------------------------

    validate_deletion_count(
        len(products),
        len(to_delete),
    )

    # --------------------------------------------------------
    # NOTHING TO DELETE
    # --------------------------------------------------------

    if not to_delete:

        print(
            "✅ Nothing to remove."
        )

        print("=" * 70)

        return

    # --------------------------------------------------------
    # SHOW CANDIDATES
    # --------------------------------------------------------

    print(
        "Products identified for deletion:"
    )

    print()

    for index, product in enumerate(
        to_delete,
        1,
    ):

        print(
            f"{index:>5}. "
            f"{product['title']}"
        )

        print(
            f"       ID: "
            f"{product['id']}"
        )

        print(
            f"       SKU(s): "
            f"{', '.join(product['skus']) or '(none)'}"
        )

        print(
            f"       Handle: "
            f"{product['handle']}"
        )

    print()

    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if DRY_RUN:

        print(
            "🧪 DRY RUN ENABLED"
        )

        print(
            "   No products were deleted."
        )

        print()

        print(
            "   To actually delete them, "
            "set:"
        )

        print(
            "   DRY_RUN=false"
        )

        print("=" * 70)

        return

    # --------------------------------------------------------
    # CONFIRM REAL DELETION
    # --------------------------------------------------------

    print(
        "⚠️ REAL DELETION MODE"
    )

    print(
        f"⚠️ {len(to_delete):,} Shopify "
        "products will be permanently deleted."
    )

    print()

    # --------------------------------------------------------
    # DELETE
    # --------------------------------------------------------

    deleted = 0
    failed = 0

    for index, product in enumerate(
        to_delete,
        1,
    ):

        product_id = product[
            "id"
        ]

        title = product[
            "title"
        ]

        print(
            f"[{index}/{len(to_delete)}] "
            f"🗑️ Deleting {title}"
        )

        print(
            f"      ID: {product_id}"
        )

        try:

            delete_product(
                product_id
            )

            deleted += 1

            print(
                "      ✅ Deleted"
            )

        except Exception as exc:

            failed += 1

            print(
                f"      ❌ FAILED: {exc}"
            )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("🧹 CLEANUP COMPLETE")
    print("=" * 70)

    print(
        f"📊 Honeylade products checked: "
        f"{len(products):,}"
    )

    print(
        f"📊 Feed product IDs: "
        f"{len(feed_ids):,}"
    )

    print(
        f"🗑️ Products deleted: "
        f"{deleted:,}"
    )

    print(
        f"❌ Failed deletions: "
        f"{failed:,}"
    )

    print("=" * 70)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:

        print()
        print(
            "⚠️ Cleanup interrupted."
        )

        sys.exit(130)

    except Exception as exc:

        print()
        print(
            f"❌ CLEANUP STOPPED: {exc}"
        )

        sys.exit(1)
