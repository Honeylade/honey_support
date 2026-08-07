#!/usr/bin/env python3
import os
import re
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
import asyncio
import aiohttp
import time
 
# -----------------------------
# CONFIG (must be defined before functions)
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
XML_URL = os.getenv("XML_URL")
API_VERSION = "2024-01"
 
# Interpret LIMIT env var: treat 0/"0"/""/"none"/"None" as no limit (None)
_raw_limit = os.getenv("LIMIT", "").strip()
if _raw_limit == "" or _raw_limit.lower() in ("0", "none", "null"):
    LIMIT = None
else:
    try:
        LIMIT = int(_raw_limit)
    except Exception:
        LIMIT = None
 
# Batch size for optional grouped work (not Store bulk API; just how we iterate)
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "250"))
 
# Maximum simultaneous product-processing tasks (tune as needed)
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "12"))
 
HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json"
}
 
TAGS_TO_INCLUDE = [
    "football accessories", "themed gifts", "Honeylade", "Honey",
    "sports gifts", "fan accessories", "novelty gifts", "sports fans"
]
 
# -----------------------------
# PRICE LOGIC
# -----------------------------
def calc_price(cost, weight):
    cost = float(cost or 0)
    weight = float(weight or 0)
    shipping = 3.99 if weight < 300 else 4.99 if weight < 2000 else 18
    margin = 0.30 if cost < 5 else 0.25 if cost < 10 else 0.20
    TAX = 0.20
    FEES = 0.029 + 0.090
    FIXED_COSTS = 0.30 + 0.50
    base_price = cost * (1 + margin)
    taxed_price = base_price * (1 + TAX)
    price_after_fees = taxed_price / (1 - FEES) if (1 - FEES) != 0 else taxed_price
    final_price = price_after_fees + shipping + FIXED_COSTS
    return round(final_price, 2)
 
# -----------------------------
# HELPERS
# -----------------------------
def last_value(val):
    if not val:
        return ""
    v = val.split(">")[-1].strip()
    return v.replace("&amp;", "and").replace("&", "and")
 
def split_tags(val):
    if not val:
        return []
    return [t.strip() for t in re.split(r"[>\|,;/\s]+", val) if t.strip()]
 
def sanitize_tags(tags):
    sanitized = []
    seen = set()
    for tag in tags:
        if not tag:
            continue
        t = tag.replace('&', 'and').strip()
        t = t[:255] if len(t) > 255 else t
        if t.lower() not in seen:
            seen.add(t.lower())
            sanitized.append(t)
    return sanitized
 
def build_description(p):
    bullets = []
    for i in range(1, 11):
        v = p.get(f"desc_{i}")
        if v:
            bullets.append(f"<li>{v}</li>")
    bullet_html = f"<ul>{''.join(bullets)}</ul>" if bullets else ""
    paragraph = f"<p>{p.get('desc_standard','') or ''}</p>"
    return bullet_html + paragraph
 
def valid_image(url):
    if not url:
        return False
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    if " " in url:
        return False
    if not any(url.lower().endswith(ext) or ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return False
    return True
 
# -----------------------------
# SHOPIFY WRAPPERS (async)
# -----------------------------
async def shopify_get(session, url, params=None):
    async with session.get(url, headers=HEADERS, params=params) as response:
        try:
            return await response.json()
        except Exception:
            text = await response.text()
            print(f"⚠️ GET parse error {response.status} for {url}: {text[:300]}")
            return {}
 
async def shopify_post(session, url, data):
    async with session.post(url, headers=HEADERS, json=data) as response:
        text = await response.text()
        if response.status not in (200, 201):
            print(f"❌ POST ERROR {response.status} - {text[:500]}")
            return {}
        try:
            return await response.json()
        except Exception:
            print(f"⚠️ POST parse error for {url}: {text[:300]}")
            return {}
 
async def shopify_put(session, url, data):
    async with session.put(url, headers=HEADERS, json=data) as response:
        text = await response.text()
        if response.status not in (200, 201):
            print(f"❌ PUT ERROR {response.status} - {text[:500]}")
            return {}
        try:
            return await response.json()
        except Exception:
            print(f"⚠️ PUT parse error for {url}: {text[:300]}")
            return {}
 
# -----------------------------
# FIND PRODUCT
# -----------------------------
async def find_product_by_handle(session, handle):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = await shopify_get(session, url, {"handle": handle})
    products = res.get("products") or []
    return products[0] if products else None
 
async def find_product_by_sku_or_barcode(session, sku=None, barcode=None, title=None):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    # Try title-based search first (cheap if useful)
    if title:
        res = await shopify_get(session, url, {"title": title})
        for p in res.get("products", []) or []:
            for v in p.get("variants", []) or []:
                if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                    return p
    # Fallback: scan some products (note: scanning entire store via this endpoint is not ideal)
    params = {"limit": 250}
    page = await shopify_get(session, url, params=params)
    for p in page.get("products", []) or []:
        for v in p.get("variants", []) or []:
            if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                return p
    return None
 
def _existing_variant_map(existing_product):
    sku_map = {}
    option_map = {}
    for v in existing_product.get("variants", []) or []:
        vid = v.get("id")
        sku = str(v.get("sku") or "")
        opt1 = str(v.get("option1") or "")
        if sku:
            sku_map[sku] = vid
        if opt1:
            option_map[opt1] = vid
    return sku_map, option_map
 
# -----------------------------
# BUILD PRODUCT PAYLOAD
# -----------------------------
def build_product(p):
    title = p.get("title") or f"Product-{p.get('sku')}"
    handle = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    try:
        cost = float(p.get("costprice") or 0)
    except Exception:
        cost = 0.0
    try:
        weight = float(p.get("weight") or 0)
    except Exception:
        weight = 0.0
    price = calc_price(cost, weight)
    vendor = last_value(p.get("productbrand"))
    product_type = last_value(p.get("productrange"))
    tags = (
        split_tags(p.get("productbrand")) +
        split_tags(p.get("productrange")) +
        TAGS_TO_INCLUDE +
        split_tags(title)
    )
    tags = sanitize_tags(tags)
    description = build_description(p)
    raw_images = (p.get("imageoffloads") or "")
    raw_images = re.split(r"[|,]+", raw_images)
    images = [{"src": img.strip()} for img in raw_images if valid_image(img)]
    variants = []
    sizes_raw = (p.get("sizeattribute") or "")
    sizes = [s.strip() for s in re.split(r"[|,]+", sizes_raw) if s.strip()]
    try:
        stock_qty = int(p.get("stock") or 0)
    except Exception:
        stock_qty = 0
    barcode = p.get("barcode") if p.get("barcode") else None
    if sizes:
        for s in sizes:
            variants.append({
                "option1": s,
                "price": str(price),
                "sku": p.get("sku"),
                "inventory_quantity": int(stock_qty),
                "inventory_management": "shopify",
                "cost": cost,
                "barcode": barcode,
                "weight": weight,
                "weight_unit": "g"
            })
    else:
        variants.append({
            "price": str(price),
            "sku": p.get("sku"),
            "inventory_quantity": int(stock_qty),
            "inventory_management": "shopify",
            "cost": cost,
            "barcode": barcode,
            "weight": weight,
            "weight_unit": "g"
        })
    product = {
        "title": title,
        "body_html": description,
        "vendor": vendor,
        "product_type": product_type,
        "tags": ", ".join(tags),
        "handle": handle,
        "status": "active" if stock_qty > 0 else "draft",
        "published": True if stock_qty > 0 else False,
        "variants": variants,
        **({"images": images} if images else {})
    }
    if len(variants) > 1:
        product["options"] = [{"name": "Size", "values": sizes}]
    return product
 
# -----------------------------
# LOAD XML (limit-aware)
# -----------------------------
def load_xml():
    print("📥 Downloading XML...")
    r = requests.get(XML_URL)
    r.raise_for_status()
    raw_bytes = r.content
    raw = raw_bytes.decode('utf-8', errors='replace')
    try:
        with open("feed_debug.xml", "wb") as f:
            f.write(raw_bytes)
        print("ℹ️ Saved raw feed to feed_debug.xml")
    except Exception as e:
        print("⚠️ Couldn't save raw feed:", e)
 
    # Try extracting <post> fragments first
    posts = re.findall(r"(?is)<post\b[^>]*?>.*?</post>", raw)
    max_take = LIMIT if LIMIT is not None else None
    if posts:
        count = len(posts)
        print(f"🔎 Extracted {count} <post> blocks from feed (regex fragment parsing).")
        items = []
        for fragment in posts[:max_take]:
            try:
                wrapped = f"<root>{fragment}</root>"
                root = ET.fromstring(wrapped)
                post_elem = root.find(".//post")
                if post_elem is None:
                    continue
                data = {}
                for c in post_elem:
                    data[c.tag.lower()] = c.text
                items.append(data)
            except Exception as e:
                print("⚠️ Failed to parse a <post> fragment:", e)
        if items:
            print(f"🔎 Parsed {len(items)} items from <post> fragments (limit {LIMIT}).")
            return items
 
    # Full XML parse
    try:
        root = ET.fromstring(raw)
        items_elem = root.findall(".//post")
        print(f"🔎 Found {len(items_elem)} items (full parse).")
        products = []
        for item in (items_elem if max_take is None else items_elem[:max_take]):
            data = {}
            for c in item:
                data[c.tag.lower()] = c.text
            products.append(data)
        if products:
            return products
    except ParseError as e:
        print("⚠️ XML ParseError on full parse:", e)
 
    # BeautifulSoup fallback
    try:
        from bs4 import BeautifulSoup
        print("🔧 Falling back to BeautifulSoup repair parsing...")
        soup = BeautifulSoup(raw, "html.parser")
        posts_bs = soup.find_all(lambda tag: tag.name and tag.name.lower() == "post")
        print(f"🔎 BeautifulSoup found {len(posts_bs)} post-like elements.")
        items = []
        for post in (posts_bs if max_take is None else posts_bs[:max_take]):
            data = {}
            for child in post.find_all(recursive=False):
                tagname = child.name.lower() if child.name else None
                data[tagname] = child.get_text() if child else None
            if not data and post.get_text(strip=True):
                data["content"] = post.get_text()
            items.append(data)
        if items:
            return items
    except Exception as e:
        print("⚠️ BeautifulSoup fallback failed or bs4 missing:", e)
 
    # Heuristic split fallback
    print("🔎 Final fallback: heuristic splitting by '<post'...")
    pieces = re.split(r"(?i)<post\b", raw)
    candidates = []
    for piece in pieces[1:(max_take + 1) if max_take is not None else None]:
        snippet = "<post" + piece
        m = re.search(r"(?is)<post\b.*?</post>", snippet)
        if m:
            fragment = m.group(0)
            try:
                wrapped = f"<root>{fragment}</root>"
                root = ET.fromstring(wrapped)
                post_elem = root.find(".//post")
                if post_elem is None:
                    continue
                data = {}
                for c in post_elem:
                    data[c.tag.lower()] = c.text
                candidates.append(data)
            except Exception:
                continue
    if candidates:
        print(f"🔎 Heuristic parsed {len(candidates)} items.")
        return candidates
 
    print("❌ Could not parse feed into <post> items. Check feed_debug.xml for raw content.")
    return []
 
# -----------------------------
# PRODUCT PROCESSOR (async)
# -----------------------------
async def process_product(session, semaphore, p, stats):
    """
    Build payload, find existing product, update or create.
    stats is a dict-like object to track created/updated counts.
    """
    async with semaphore:
        try:
            product_payload = build_product(p)
            handle = product_payload.get("handle")
            sku = p.get("sku")
            barcode = p.get("barcode")
            title = product_payload.get("title")
 
            # Query existing product by handle and sku/barcode in parallel
            find_tasks = [
                find_product_by_handle(session, handle),
                find_product_by_sku_or_barcode(session, sku=sku, barcode=barcode, title=title)
            ]
            existing_handle, existing_sku = await asyncio.gather(*find_tasks)
            existing = existing_handle or existing_sku
 
            if existing:
                sku_map, option_map = _existing_variant_map(existing)
                updated_variants = []
                for v in product_payload["variants"]:
                    vid = None
                    v_sku = str(v.get("sku") or "")
                    opt1 = str(v.get("option1") or "")
                    if v_sku and v_sku in sku_map:
                        vid = sku_map[v_sku]
                    elif opt1 and opt1 in option_map:
                        vid = option_map[opt1]
                    v_copy = v.copy()
                    if vid:
                        v_copy["id"] = vid
                    v_copy["price"] = str(v_copy.get("price"))
                    v_copy["inventory_quantity"] = int(v_copy.get("inventory_quantity", 0))
                    updated_variants.append(v_copy)
                product_update_payload = product_payload.copy()
                product_update_payload["variants"] = updated_variants
                url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
                await shopify_put(session, url, {"product": product_update_payload})
                stats["updated"] += 1
                print(f"🔄 Updated: {product_payload['title']} (ID: {existing['id']})")
            else:
                url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
                res = await shopify_post(session, url, {"product": product_payload})
                product_id = res.get("product", {}).get("id")
                if product_id:
                    stats["created"] += 1
                    print(f"🆕 Created: {product_payload['title']} (ID: {product_id})")
                else:
                    print(f"❌ Failed to create: {product_payload['title']}")
        except Exception as e:
            print(f"❌ Error processing SKU {p.get('sku')} / title {p.get('title')}: {e}")
 
# -----------------------------
# SYNC
# -----------------------------
async def run_sync():
    print("🚀 START SYNC")
    items = load_xml()
    total_items = len(items)
    print(f"🔢 Items to process: {total_items} (LIMIT={LIMIT})")
    stats = {"created": 0, "updated": 0}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
 
    connector = aiohttp.TCPConnector(limit_per_host=MAX_CONCURRENT)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=60)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Process in batches (to avoid building huge task lists)
        for start in range(0, total_items, BATCH_SIZE):
            batch = items[start:start + BATCH_SIZE]
            tasks = [process_product(session, semaphore, p, stats) for p in batch]
            # run the batch concurrently
            await asyncio.gather(*tasks)
            # small pause between batches to be gentle to the API (tune as needed)
            await asyncio.sleep(0.2)
 
    print("✅ DONE")
    print(f"Created: {stats['created']}, Updated: {stats['updated']}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    # Quick validation of critical envs
    if not SHOP_URL or not ACCESS_TOKEN or not XML_URL:
        print("❌ Missing required environment variables: SHOP_URL, ACCESS_TOKEN, and XML_URL must be set.")
        raise SystemExit(1)
 
    start_ts = time.time()
    asyncio.run(run_sync())
    elapsed = time.time() - start_ts
    print(f"⏱ Finished in {elapsed:.1f} seconds.")
