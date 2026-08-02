import requests
from lxml import etree  # ✅ FIX: robust XML parser
import re
import os
import sys

# -----------------------------
# CONFIG
# -----------------------------
SHOP_URL = (os.getenv("SHOP_URL") or "").strip()
ACCESS_TOKEN = (os.getenv("ACCESS_TOKEN") or "").strip()
XML_URL = (os.getenv("XML_URL") or "").strip()
API_VERSION = "2024-01"

LIMIT = 40

HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json"
}

TAGS_TO_INCLUDE = ["football accessories", "Honeylade", "Honey"]

# -----------------------------
# VALIDATION (NEW)
# -----------------------------
def validate_connection():
    print("🔐 Validating Shopify connection...")
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/shop.json"

    r = requests.get(url, headers=HEADERS)

    if r.status_code == 200:
        print("✅ Connected to Shopify store")
    elif r.status_code == 401:
        print("❌ Invalid token (401)")
        sys.exit(1)
    elif r.status_code == 403:
        print("❌ Missing permissions (403)")
        sys.exit(1)
    else:
        print(f"❌ Shopify error: {r.status_code} {r.text}")
        sys.exit(1)

# -----------------------------
# PRICE LOGIC (unchanged)
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
    FEES = 0.029 + 0.090
    FIXED_COSTS = 0.30 + 0.50

    base_price = cost * (1 + margin)
    taxed_price = base_price * (1 + TAX)
    price_after_fees = taxed_price / (1 - FEES)

    final_price = price_after_fees + shipping + FIXED_COSTS
    return round(final_price, 2)

# -----------------------------
# HELPERS (unchanged)
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
        if len(t) > 255:
            t = t[:255]
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

# -----------------------------
# IMAGE VALIDATION (unchanged)
# -----------------------------
def valid_image(url):
    if not url:
        return False
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    if " " in url:
        return False
    if not any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return False
    return True

# -----------------------------
# SHOPIFY WRAPPERS (unchanged)
# -----------------------------
def shopify_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    return r.json() if r.status_code == 200 else {}

def shopify_post(url, data):
    r = requests.post(url, headers=HEADERS, json=data)
    if r.status_code not in [200, 201]:
        print("❌ POST ERROR:", r.status_code, r.text)
    return r.json() if r.text else {}

def shopify_put(url, data):
    r = requests.put(url, headers=HEADERS, json=data)
    if r.status_code not in [200, 201]:
        print("❌ PUT ERROR:", r.status_code, r.text)
    return r.json() if r.text else {}

# -----------------------------
# SAFE FIND (unchanged)
# -----------------------------
def find_product_by_handle(handle):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"handle": handle})
    return (res.get("products") or [None])[0]

# -----------------------------
# BUILD PRODUCT (FIX: title fallback)
# -----------------------------
def build_product(p):
    title = p.get("title")

    # 🚨 FIX: ensure title is never empty
    if not title:
        title = f"Product-{p.get('sku')}"

    handle = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')

    cost = float(p.get("costprice") or 0)
    weight = float(p.get("weight") or 0)
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

    images = []
    for img in re.split(r"[|,]+", (p.get("imageoffloads") or "")):
        if valid_image(img):
            images.append({"src": img.strip()})

    return {
        "title": title,
        "body_html": description,
        "vendor": vendor,
        "product_type": product_type,
        "tags": ", ".join(tags),
        "handle": handle,
        "status": "active",
        "published": True,
        "variants": [{
            "price": price,
            "sku": p.get("sku"),
            "inventory_management": "shopify"
        }],
        **({"images": images} if images else {})
    }

# -----------------------------
# LOAD XML (FIXED)
# -----------------------------
def load_xml():
    print("📥 Downloading XML...")
    r = requests.get(XML_URL)
    r.raise_for_status()

    parser = etree.XMLParser(recover=True)  # ✅ FIX
    root = etree.fromstring(r.content, parser)

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
# SYNC
# -----------------------------
def run_sync():
    print("🚀 START SYNC")

    validate_connection()  # ✅ FIX

    items = load_xml()

    for p in items:
        payload = build_product(p)
        handle = payload["handle"]

        existing = find_product_by_handle(handle)

        if existing:
            shopify_put(
                f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json",
                {"product": payload}
            )
            print(f"🔄 Updated: {payload['title']}")
        else:
            res = shopify_post(
                f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json",
                {"product": payload}
            )
            if res.get("product"):
                print(f"🆕 Created: {payload['title']}")
            else:
                print(f"❌ Failed: {payload['title']}")

    print("✅ DONE")

# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_sync()
