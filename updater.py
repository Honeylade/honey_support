#!/usr/bin/env python3
import os
import re
import time
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
from requests.adapters import HTTPAdapter, Retry
 
# -----------------------------
# CONFIG
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
XML_URL = os.getenv("XML_URL")
API_VERSION = os.getenv("API_VERSION", "2024-01")
 
_raw_limit = os.getenv("LIMIT", "").strip()
if _raw_limit == "" or _raw_limit.lower() in ("0", "none", "null"):
    LIMIT = None
else:
    try:
        LIMIT = int(_raw_limit)
    except Exception:
        LIMIT = None
 
HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json"
}
 
TAGS_TO_INCLUDE = [
    "football accessories", "themed gifts", "Honeylade", "Honey",
    "sports gifts", "fan accessories", "novelty gifts", "sports fans"
]
 
# Tune these
PAGE_LIMIT = 250          # Shopify max per page
HTTP_RETRIES = 3
RETRY_BACKOFF = 1.0       # seconds
 
# -----------------------------
# HTTP session with retries
# -----------------------------
session = requests.Session()
retries = Retry(total=HTTP_RETRIES, backoff_factor=RETRY_BACKOFF,
                status_forcelist=(429, 500, 502, 503, 504))
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update(HEADERS)
session.timeout = 60
 
def safe_get(url, params=None):
    r = session.get(url, params=params, timeout=60)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {}
 
def safe_post(url, json_data):
    r = session.post(url, json=json_data, timeout=60)
    try:
        r.raise_for_status()
        return r.json()
    except requests.HTTPError:
        print(f"❌ POST error {r.status_code} for {url}: {r.text[:400]}")
        return {}
 
def safe_put(url, json_data):
    r = session.put(url, json=json_data, timeout=60)
    try:
        r.raise_for_status()
        return r.json()
    except requests.HTTPError:
        print(f"❌ PUT error {r.status_code} for {url}: {r.text[:400]}")
        return {}
 
# -----------------------------
# Utilities
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
    denom = (1 - FEES) if (1 - FEES) != 0 else 1
    price_after_fees = taxed_price / denom
    final_price = price_after_fees + shipping + FIXED_COSTS
    return round(final_price, 2)
 
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
# Build product payload (same logic as original)
# -----------------------------
def build_product_payload(p):
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
        # create variants for each size; keep sku base but real SKU matching may vary by feed
        for s in sizes:
            variants.append({
                "option1": s,
                "price": str(price),
                "sku": p.get("sku") or "",
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
            "sku": p.get("sku") or "",
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
# Load XML feed (original robust parsing)
# -----------------------------
def load_xml():
    print("📥 Downloading XML...")
    r = requests.get(XML_URL, timeout=60)
    r.raise_for_status()
    raw_bytes = r.content
    raw = raw_bytes.decode('utf-8', errors='replace')
    try:
        with open("feed_debug.xml", "wb") as f:
            f.write(raw_bytes)
        print("ℹ️ Saved raw feed to feed_debug.xml")
    except Exception as e:
        print("⚠️ Couldn't save raw feed:", e)
 
    posts = re.findall(r"(?is)<post\b[^>]*?>.*?</post>", raw)
    max_take = LIMIT if LIMIT is not None else None
    if posts:
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
            print(f"🔎 Parsed {len(items)} items from fragment parsing.")
            return items
 
    try:
        root = ET.fromstring(raw)
        items_elem = root.findall(".//post")
        products = []
        for item in (items_elem if max_take is None else items_elem[:max_take]):
            data = {}
            for c in item:
                data[c.tag.lower()] = c.text
            products.append(data)
        if products:
            print(f"🔎 Parsed {len(products)} items from full XML.")
            return products
    except ParseError as e:
        print("⚠️ XML ParseError on full parse:", e)
 
    try:
        from bs4 import BeautifulSoup
        print("🔧 Falling back to BeautifulSoup repair parsing...")
        soup = BeautifulSoup(raw, "html.parser")
        posts_bs = soup.find_all(lambda tag: tag.name and tag.name.lower() == "post")
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
    except Exception:
        pass
 
    print("❌ Could not parse feed into <post> items. Check feed_debug.xml for raw content.")
    return []
 
# -----------------------------
# Build store index (pagination) - maps handle, sku, barcode to product
# -----------------------------
def build_store_index():
    print("📚 Building store product index (paginated)...")
    handle_map = {}
    sku_map = {}
    barcode_map = {}
    page = 1
    params = {"limit": PAGE_LIMIT}
    while True:
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        params['page'] = page  # older page param; if store uses cursor-based pagination it's still helpful to paginate by page for legacy
        # Use 'since_id' style if many items — but we'll try simple approach first.
        res = safe_get(url, params={"limit": PAGE_LIMIT, "page": page})
        products = res.get("products") or []
        if not products:
            break
        for p in products:
            pid = p.get("id")
            handle = p.get("handle")
            title = p.get("title")
            if handle:
                handle_map[handle] = p
            # collect variants
            for v in p.get("variants", []) or []:
                sku = str(v.get("sku") or "")
                barcode = v.get("barcode")
                if sku:
                    sku_map[sku] = p
                if barcode:
                    barcode_map[barcode] = p
        # stop if fewer than page limit or LIMIT reached
        if len(products) < PAGE_LIMIT:
            break
        page += 1
        # small pause to be polite
        time.sleep(0.2)
    print(f"Indexed {len(handle_map)} handles, {len(sku_map)} skus, {len(barcode_map)} barcodes.")
    return handle_map, sku_map, barcode_map
 
# -----------------------------
# Variant helper: map existing variant ids by sku and option1
# -----------------------------
def existing_variant_map(product):
    sku_map = {}
    opt_map = {}
    for v in product.get("variants", []) or []:
        vid = v.get("id")
        sku = str(v.get("sku") or "")
        opt1 = str(v.get("option1") or "")
        if sku:
            sku_map[sku] = vid
        if opt1:
            opt_map[opt1] = vid
    return sku_map, opt_map
 
# -----------------------------
# Sync loop
# -----------------------------
def run_sync():
    if not SHOP_URL or not ACCESS_TOKEN or not XML_URL:
        print("❌ Missing SHOP_URL, ACCESS_TOKEN, or XML_URL env vars.")
        return
    items = load_xml()
    if not items:
        print("❌ No items to process.")
        return
 
    handle_map, sku_map, barcode_map = build_store_index()
 
    created = 0
    updated = 0
 
    for p in items:
        product_payload = build_product_payload(p)
        handle = product_payload.get("handle")
        sku = (p.get("sku") or "").strip()
        barcode = p.get("barcode") if p.get("barcode") else None
        title = product_payload.get("title")
 
        # Lookup strategy: SKU -> barcode -> handle -> title fallback
        existing = None
        if sku and sku in sku_map:
            existing = sku_map[sku]
        elif barcode and barcode in barcode_map:
            existing = barcode_map[barcode]
        elif handle and handle in handle_map:
            existing = handle_map[handle]
        else:
            # try title search (API supports title param)
            try:
                url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
                res = safe_get(url, params={"title": title})
                for cand in res.get("products", []) or []:
                    # double-check variants for SKU/barcode
                    matched = False
                    for v in cand.get("variants", []) or []:
                        if sku and str(v.get("sku") or "") == sku:
                            matched = True
                            break
                        if barcode and v.get("barcode") and v.get("barcode") == barcode:
                            matched = True
                            break
                    if matched or cand.get("title") == title:
                        existing = cand
                        break
            except Exception:
                existing = None
 
        if existing:
            # update existing product
            sku_map_existing, opt_map_existing = existing_variant_map(existing)
            updated_variants = []
            for v in product_payload["variants"]:
                v_sku = str(v.get("sku") or "")
                opt1 = str(v.get("option1") or "")
                vid = None
                if v_sku and v_sku in sku_map_existing:
                    vid = sku_map_existing[v_sku]
                elif opt1 and opt1 in opt_map_existing:
                    vid = opt_map_existing[opt1]
                v_copy = {
                    # include fields Shopify expects for variant update:
                    **({ "id": vid } if vid else {}),
                    "price": str(v.get("price")),
                    "sku": v.get("sku") or "",
                    "inventory_quantity": int(v.get("inventory_quantity", 0)),
                    "inventory_management": v.get("inventory_management"),
                    "barcode": v.get("barcode"),
                    "weight": v.get("weight"),
                    "weight_unit": v.get("weight_unit")
                }
                # remove keys with None to avoid API complaints
                v_copy = {k: v for k, v in v_copy.items() if v is not None}
                updated_variants.append(v_copy)
 
            product_update_payload = {
                "title": product_payload.get("title"),
                "body_html": product_payload.get("body_html"),
                "vendor": product_payload.get("vendor"),
                "product_type": product_payload.get("product_type"),
                "tags": product_payload.get("tags"),
                "variants": updated_variants,
            }
            # include images only if present (update may add images)
            if product_payload.get("images"):
                product_update_payload["images"] = product_payload.get("images")
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
            res = safe_put(url, {"product": product_update_payload})
            # small heuristic for success
            if res.get("product"):
                updated += 1
                print(f"🔄 Updated: {product_payload['title']} (ID: {existing['id']})")
            else:
                print(f"❌ Failed to update: {product_payload['title']}")
            # keep maps in sync: update sku_map/barcode_map if necessary
            for v in res.get("product", {}).get("
