import requests
import xml.etree.ElementTree as ET
import re
import os
import sys

# -----------------------------
# CONFIG
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
XML_URL = os.getenv("XML_URL")

API_VERSION = "2024-01"
LIMIT = 40

HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json"
}

TAGS_TO_INCLUDE = ["football accessories", "Honeylade", "Honey"]

# -----------------------------
# 🔐 TOKEN VALIDATION
# -----------------------------
def validate_token():
    print("🔐 Validating Shopify token...")

    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/shop.json"
    r = requests.get(url, headers=HEADERS)

    if r.status_code == 200:
        shop_name = r.json().get("shop", {}).get("name")
        print(f"✅ Token valid. Connected to: {shop_name}")
        return True
    else:
        print("❌ INVALID TOKEN")
        print("Status:", r.status_code)
        print("Response:", r.text)
        print("\n👉 Fix this:")
        print("- Use Admin API access token (not Storefront)")
        print("- Or ensure your atkn_ token has Admin API scopes")
        print("- Required scopes: write_products, read_products")
        sys.exit(1)

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
    FEES = 0.029 + 0.090
    FIXED_COSTS = 0.30 + 0.50

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
    return val.split(">")[-1].strip().replace("&", "and")

def split_tags(val):
    if not val:
        return []
    return [t.strip() for t in re.split(r"[>\|,;/\s]+", val) if t.strip()]

def sanitize_tags(tags):
    seen = set()
    result = []
    for t in tags:
        t = t.replace("&", "and").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            result.append(t[:255])
    return result

def build_description(p):
    bullets = [
        f"<li>{p.get(f'desc_{i}')}</li>"
        for i in range(1, 11)
        if p.get(f"desc_{i}")
    ]
    return f"<ul>{''.join(bullets)}</ul><p>{p.get('desc_standard','')}</p>"

# -----------------------------
# IMAGE VALIDATION
# -----------------------------
def valid_image(url):
    if not url:
        return False
    url = url.strip().lower()
    return (
        url.startswith("http")
        and " " not in url
        and any(ext in url for ext in [".jpg", ".jpeg", ".png", ".webp"])
    )

# -----------------------------
# SHOPIFY WRAPPERS
# -----------------------------
def shopify_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    if r.status_code == 401:
        print("❌ AUTH ERROR DURING GET")
        sys.exit(1)
    return r.json() if r.ok else {}

def shopify_post(url, data):
    r = requests.post(url, headers=HEADERS, json=data)
    if r.status_code == 401:
        print("❌ AUTH ERROR DURING POST")
        sys.exit(1)
    if not r.ok:
        print("❌ POST ERROR:", r.status_code, r.text)
    return r.json() if r.text else {}

def shopify_put(url, data):
    r = requests.put(url, headers=HEADERS, json=data)
    if r.status_code == 401:
        print("❌ AUTH ERROR DURING PUT")
        sys.exit(1)
    if not r.ok:
        print("❌ PUT ERROR:", r.status_code, r.text)
    return r.json() if r.text else {}

# -----------------------------
# FIND PRODUCT
# -----------------------------
def find_product_by_handle(handle):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"handle": handle})
    return (res.get("products") or [None])[0]

# -----------------------------
# BUILD PRODUCT
# -----------------------------
def build_product(p):
    title = p.get("title") or f"Product-{p.get('sku')}"
    handle = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')

    cost = float(p.get("costprice") or 0)
    weight = float(p.get("weight") or 0)

    price = calc_price(cost, weight)

    tags = sanitize_tags(
        split_tags(p.get("productbrand")) +
        split_tags(p.get("productrange")) +
        TAGS_TO_INCLUDE +
        split_tags(title)
    )

    images = [
        {"src": img.strip()}
        for img in re.split(r"[|,]+", p.get("imageoffloads") or "")
        if valid_image(img)
    ]

    variant = {
        "price": price,
        "sku": p.get("sku"),
        "inventory_quantity": int(p.get("stock") or 0),
        "inventory_management": "shopify",
        "weight": weight,
        "weight_unit": "g"
    }

    return {
        "title": title,
        "body_html": build_description(p),
        "tags": ", ".join(tags),
        "handle": handle,
        "status": "active",
        "variants": [variant],
        **({"images": images} if images else {})
    }

# -----------------------------
# LOAD XML
# -----------------------------
def load_xml():
    print("📥 Downloading XML...")
    r = requests.get(XML_URL)
    r.raise_for_status()

    root = ET.fromstring(r.content)
    items = root.findall(".//post")

    print(f"🔎 Found {len(items)} items")

    return [
        {c.tag.lower(): c.text for c in item}
        for item in items[:LIMIT]
    ]

# -----------------------------
# SYNC
# -----------------------------
def run_sync():
    print("🚀 START SYNC")

    validate_token()   # ✅ NEW

    items = load_xml()

    created = 0

    for p in items:
        product_payload = build_product(p)

        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        res = shopify_post(url, {"product": product_payload})

        if res.get("product"):
            created += 1
            print(f"🆕 Created: {product_payload['title']}")
        else:
            print(f"❌ Failed: {product_payload['title']}")

    print("✅ DONE")
    print(f"Created: {created}")

# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_sync()
