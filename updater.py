import requests
import xml.etree.ElementTree as ET
import re
import os
import sys
from xml.etree.ElementTree import ParseError
 
# Optional: use BeautifulSoup as a last-resort fixer if installed
try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False
 
# -----------------------------
# CONFIG
# -----------------------------
SHOP_URL = os.getenv("SHOP_URL")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
XML_URL = os.getenv("XML_URL")
API_VERSION = "2024-01"
 
LIMIT = 4
 
HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json"
}
 
TAGS_TO_INCLUDE = ["football accessories", "themed gifts", "Honeylade", "sports gifts", "fan accessories", "novelty gifts", "sports fans"]
 
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
# (unchanged)
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
 
def shopify_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    try:
        return r.json() if r.status_code == 200 else {}
    except ValueError:
        return {}
 
def shopify_post(url, data):
    r = requests.post(url, headers=HEADERS, json=data)
    if r.status_code not in [200, 201]:
        print("❌ POST ERROR:", r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {}
 
def shopify_put(url, data):
    r = requests.put(url, headers=HEADERS, json=data)
    if r.status_code not in [200, 201]:
        print("❌ PUT ERROR:", r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {}
 
def find_product_by_handle(handle):
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    res = shopify_get(url, {"handle": handle})
    products = res.get("products") or []
    return products[0] if products else None
 
def find_product_by_sku_or_barcode(sku=None, barcode=None, title=None):
    if title:
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        res = shopify_get(url, {"title": title})
        for p in res.get("products", []) or []:
            for v in p.get("variants", []):
                if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                    return p
 
    url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
    params = {"limit": 250}
    page = shopify_get(url, params=params) or {}
    for p in page.get("products", []) or []:
        for v in p.get("variants", []):
            if (sku and str(v.get("sku")) == str(sku)) or (barcode and v.get("barcode") and v.get("barcode") == barcode):
                return p
    return None
 
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
# LOAD XML (robust parser + auto-fix)
# -----------------------------
def _escape_unescaped_amp(text):
    # replace '&' that are not part of an entity (amp;, lt;, gt;, quot;, apos;)
    return re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;)', '&amp;', text)
 
def load_xml():
    print("📥 Downloading XML...")
    r = requests.get(XML_URL)
    r.raise_for_status()
    raw_bytes = r.content
    raw = raw_bytes.decode('utf-8', errors='replace')
 
    # Save raw feed for inspection
    try:
        with open("feed_debug.xml", "wb") as f:
            f.write(raw_bytes)
        print("ℹ️ Saved raw feed to feed_debug.xml")
    except Exception as e:
        print("⚠️ Couldn't save raw feed:", e)
 
    # Try parse, on failure give helpful debug info and attempts to repair
    try:
        root = ET.fromstring(raw)
    except ParseError as e:
        print("⚠️ XML ParseError:", e)
        # extract line/col if available
        msg = str(e)
        m = re.search(r"line (\d+), column (\d+)", msg)
        if m:
            err_line = int(m.group(1))
            err_col = int(m.group(2))
            print(f"🔎 Error at line {err_line}, column {err_col}. Showing context:")
            lines = raw.splitlines()
            start = max(0, err_line - 6)
            end = min(len(lines), err_line + 4)
            for i in range(start, end):
                indicator = ">>" if (i + 1) == err_line else "  "
                print(f"{indicator} {i+1:04d}: {lines[i]}")
        else:
            print("🔎 Could not extract line/column from error message.")
 
        # Attempt 1: escape unescaped ampersands (already in your script)
        fixed = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;)', '&amp;', raw)
 
        # Attempt 2: common stray tag fixes: convert lone <br> to <br/> and close obvious opened tags like <p>, <div>, <span>
        fixed = re.sub(r"<br\s*>", "<br/>", fixed, flags=re.IGNORECASE)
        # heuristic: close unclosed simple tags if they appear to be left open at the same line (best-effort)
        # Note: this is intentionally conservative and won't attempt deep HTML repairs
        fixed = re.sub(r"<(/?)(div|span|p)[^>]*>(?![\s\S]*</\2>)", lambda m: m.group(0) if m.group(1) == "/" else m.group(0) + f"</{m.group(2)}>", fixed, flags=re.IGNORECASE)
 
        # Try parse the fixed text
        try:
            root = ET.fromstring(fixed)
            print("✅ Parsed after heuristic fixes (ampersand/BR/tag).")
            raw = fixed
        except ParseError as e2:
            print("⚠️ Still ParseError after heuristic fixes:", e2)
            # Try BeautifulSoup if available
            if _HAS_BS4:
                print("ℹ️ Attempting repair with BeautifulSoup (xml parser)...")
                soup = BeautifulSoup(raw, "xml")
                repaired = str(soup)
                try:
                    root = ET.fromstring(repaired)
                    raw = repaired
                    print("✅ Parsed after BeautifulSoup repair.")
                except ParseError as e3:
                    print("❌ Failed to parse even after BeautifulSoup:", e3)
                    raise
            else:
                print("❌ No BeautifulSoup available to attempt repair. Install with: pip install beautifulsoup4")
                raise
 
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
# SYNC (unchanged)
# -----------------------------
def run_sync():
    print("🚀 START SYNC")
 
    items = load_xml()
 
    created = 0
    updated = 0
 
    for p in items:
        product_payload = build_product(p)
        handle = product_payload.get("handle")
        sku = p.get("sku")
        barcode = p.get("barcode")
        title = product_payload.get("title")
 
        existing = find_product_by_handle(handle) or find_product_by_sku_or_barcode(sku=sku, barcode=barcode, title=title)
 
        if existing:
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json"
            shopify_put(url, {"product": product_payload})
            updated += 1
            print(f"🔄 Updated: {product_payload['title']} (ID: {existing['id']})")
        else:
            url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
            res = shopify_post(url, {"product": product_payload})
            product_id = res.get("product", {}).get("id")
            if product_id:
                created += 1
                print(f"🆕 Created: {product_payload['title']} (ID: {product_id})")
            else:
                print(f"❌ Failed to create: {product_payload['title']}")
 
    print("✅ DONE")
    print(f"Created: {created}, Updated: {updated}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_sync()
