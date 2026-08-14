import os
import asyncio
import aiohttp
import xml.etree.ElementTree as ET
import re
import random
 
# -----------------------------
# CONFIG
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
XML_URL = os.getenv("XML_URL")
API_VERSION = "2024-01"
 
LIMIT = os.getenv("LIMIT")  # Allow all products = None or 0
if LIMIT in ["None", "0", None]:
    LIMIT = None
else:
    LIMIT = int(LIMIT)
 
HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json"
}
 
TAGS_TO_INCLUDE = [
    "football accessories", "football", "rugby", "entertainment",
    "themed gifts", "Honeylade", "flags", "sports gifts",
    "fan accessories", "novelty gifts", "sports fans"
]
 
# -----------------------------
# PRICE LOGIC
# -----------------------------
def calc_price(cost, weight):
    cost = float(cost or 0)
    weight = float(weight or 0)
 
    shipping = 3.99 if weight < 300 else 4.99 if weight < 2000 else 18
 
    if cost < 5:
        margin = 0.30
    elif cost < 10:
        margin = 0.25
    else:
        margin = 0.20
 
    TAX = 0.20
    FEES = 0.029 + 0.09
    FIXED_COSTS = 0.30 + 0.5
 
    base_price = cost * (1 + margin)
    taxed_price = base_price * (1 + TAX)
    price_after_fees = taxed_price / (1 - FEES)
 
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
 
# -----------------------------
# SANITIZE TAGS
# -----------------------------
def sanitize_tags(tags):
    sanitized = []
    seen = set()
    for tag in tags:
        if not tag:
            continue
        t = tag.replace("&amp;", "and").strip()
        t = ''.join(e for e in t if e.isalnum() or e.isspace() or e == '-')
        if len(t) > 255:
            t = t[:255]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            sanitized.append(t)
    return sanitized[:250]
 
# -----------------------------
# BUILD DESCRIPTION
# -----------------------------
def build_description(p):
    bullets = []
    for i in range(1, 11):
        v = p.get(f"desc_{i}")
        if v:
            bullets.append(f"<li>{v}</li>")
    bullet_html = f"<ul>{''.join(bullets)}</ul>" if bullets else ""
    paragraph = f"<p>{p.get('desc_standard','') or ''}</p>"
    return bullet_html + paragraph
 
# -----------------------------
# IMAGE VALIDATION
# -----------------------------
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
# SHOPIFY SIMPLE WRAPPERS
# -----------------------------
async def shopify_get(session, url, params=None):
    async with session.get(url, headers=HEADERS, params=params) as r:
        return await r.json() if r.status == 200 else {}
 
async def shopify_post(session, url, data):
    async with session.post(url, headers=HEADERS, json=data) as r:
        if r.status not in [200, 201]:
            print("❌ POST ERROR:", r.status, await r.text())
        return await r.json()
 
async def shopify_put(session, url, data):
    async with session.put(url, headers=HEADERS, json=data) as r:
        if r.status not in [200, 201]:
            print("❌ PUT ERROR:", r.status, await r.text())
        return await r.json()
 
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
    
    if title:
        res = await shopify_get(session, url, {"title": title})
        for p in res.get("products", []) or []:
            for v in p.get("variants", []):
                if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                    return p
 
    params = {"limit": 250}
    page = await shopify_get(session, url, params=params) or {}
    for p in page.get("products", []) or []:
        for v in p.get("variants", []):
            if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                return p
    return None
 
# -----------------------------
# BUILD PRODUCT PAYLOAD
# -----------------------------
def build_product(p):
    title = p.get("title") or f"Product-{p.get('sku')}"
    handle = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
 
    cost = float(p.get("costprice") or 0)
    weight = float(p.get("weight") or 0)
 
    price = calc_price(cost, weight)
 
    vendor = last_value(p.get("productbrand"))
    product_type = last_value(p.get("productrange"))
 
    tags = (
        split_tags(p.get("productbrand")) +
        [p.get("productrange")] +  # Add the whole product range directly
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
    stock_qty = int(p.get("stock") or 0)
    barcode = p.get("barcode") if p.get("barcode") else None
 
    if sizes:
        for s in sizes:
            variants.append({
                "option1": s,
                "price": price,
                "sku": p.get("sku"),
                "inventory_quantity": stock_qty,
                "inventory_management": "shopify",
                "cost": cost,
                "barcode": barcode,
                "weight": weight,
                "weight_unit": "g"
            })
    else:
        variants.append({
            "price": price,
            "sku": p.get("sku"),
            "inventory_quantity": stock_qty,
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
# LOAD XML
# -----------------------------
async def load_xml():
    print("📥 Downloading XML...")
    async with aiohttp.ClientSession() as session:
        async with session.get(XML_URL) as r:
            r.raise_for_status()
            root = ET.fromstring(await r.text())
 
            items = root.findall(".//post")
            print(f"🔎 Found {len(items)} items")
 
            products = []
            for item in items[:LIMIT]:
                data = {}
                for c in item:
                    data[c.tag.lower()] = c.text
                products.append(data)
 
            return products
 
# -----------------------------
# SYNC PRODUCT
# -----------------------------
async def sync_product(session, p, counters, backoff=1):
    product_payload = build_product(p)
    handle = product_payload.get("handle")
    sku = p.get("sku")
    barcode = p.get("barcode")
    title = product_payload.get("title")
 
    print(f"🔍 Checking for product: Handle: {handle}, SKU: {sku}, Barcode: {barcode}")
 
    existing = await find_product_by_handle(session, handle) or await find_product_by_sku_or_barcode(session, sku=sku, barcode=barcode)
 
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/"
    response = None
 
    if existing:
        url += f"{existing['id']}.json"
        response = await shopify_put(session, url, {"product": product_payload})
    else:
        url += "json"
        response = await shopify_post(session, url, {"product": product_payload})
 
    if response:
        if existing:
            print(f"🔄 Updated: {product_payload['title']} (ID: {existing['id']})")
            counters['updated'] += 1
        else:
            print(f"➕ Created: {product_payload['title']} (ID: {response['product']['id']})")
            counters['created'] += 1
    else:
        if response and 'errors' in response:
            if 'Exceeded 2 calls per second' in response['errors']:
                print(f"💔 Rate limit hit. Waiting for {backoff} seconds before retrying...")
                await asyncio.sleep(backoff)
                # Exponential backoff
                new_backoff = min(backoff * 2, 30) + random.uniform(0, 1)  # Adding some randomness to avoid thundering herd problem
                return await sync_product(session, p, counters, new_backoff)  # Retry the sync
        print(f"❌ Failed to process: {product_payload['title']}")
 
    await asyncio.sleep(1)  # Basic rate limiting for other requests
 
# -----------------------------
# RUN SYNC
# -----------------------------
async def run_sync():
    print("🚀 START SYNC")
    items = await load_xml()
    counters = {'updated': 0, 'created': 0}
 
    async with aiohttp.ClientSession() as session:
        tasks = [sync_product(session, p, counters) for p in items]
        await asyncio.gather(*tasks)
 
    print(f"✅ DONE: Total Updated: {counters['updated']}, Total Created: {counters['created']}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    asyncio.run(run_sync())
