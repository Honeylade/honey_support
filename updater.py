#!/usr/bin/env python3
"""
Improved Shopify sync:
- Builds a local index of existing store products (handle, SKU, barcode → product)
- Avoids recreating already-existing products
- Updates existing products’ variants preserving variant IDs
- Retries on transient HTTP errors
"""
 
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
SHOP_URL     = os.getenv("SHOP_URL")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
XML_URL      = os.getenv("XML_URL")
API_VERSION  = os.getenv("API_VERSION", "2024-01")
 
_raw_limit = os.getenv("LIMIT", "").strip()
if _raw_limit == "" or _raw_limit.lower() in ("0", "none", "null"):
    LIMIT = None
else:
    try:
        LIMIT = int(_raw_limit)
    except ValueError:
        LIMIT = None
 
HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type":        "application/json",
    "Accept":              "application/json",
}
 
TAGS_TO_INCLUDE = [
    "football accessories", "themed gifts", "Honeylade", "Honey",
    "sports gifts", "fan accessories", "novelty gifts", "sports fans",
]
 
# Shopify pagination / retry params
PAGE_LIMIT   = 250
HTTP_RETRIES = 3
BACKOFF      = 1.0  # seconds
 
# -----------------------------
# SESSION WITH RETRIES
# -----------------------------
session = requests.Session()
retries = Retry(
    total=HTTP_RETRIES,
    backoff_factor=BACKOFF,
    status_forcelist=(429, 500, 502, 503, 504)
)
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update(HEADERS)
 
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
        print(f"❌ POST {r.status_code} {r.text[:300]}")
        return {}
 
def safe_put(url, json_data):
    r = session.put(url, json=json_data, timeout=60)
    try:
        r.raise_for_status()
        return r.json()
    except requests.HTTPError:
        print(f"❌ PUT {r.status_code} {r.text[:300]}")
        return {}
 
# -----------------------------
# HELPERS
# -----------------------------
def calc_price(cost, weight):
    cost   = float(cost or 0)
    weight = float(weight or 0)
    shipping = 3.99 if weight < 300 else 4.99 if weight < 2000 else 18
    margin   = 0.30 if cost < 5 else 0.25 if cost < 10 else 0.20
    TAX       = 0.20
    FEES      = 0.029 + 0.090
    FIXED     = 0.30 + 0.50
    base    = cost * (1 + margin)
    taxed   = base * (1 + TAX)
    denom   = (1 - FEES) if (1 - FEES) != 0 else 1
    priced  = taxed / denom
    return round(priced + shipping + FIXED, 2)
 
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
    seen = set()
    out  = []
    for t in tags:
        if not t:
            continue
        norm = t.replace("&", "and").strip()[:255]
        if norm.lower() not in seen:
            seen.add(norm.lower())
            out.append(norm)
    return out
 
def build_description(p):
    bullets = []
    for i in range(1, 11):
        v = p.get(f"desc_{i}")
        if v:
            bullets.append(f"<li>{v}</li>")
    html_list = f"<ul>{''.join(bullets)}</ul>" if bullets else ""
    para      = f"<p>{p.get('desc_standard','') or ''}</p>"
    return html_list + para
 
def valid_image(url):
    if not url: return False
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")): return False
    if " " in u: return False
    for ext in (".jpg",".jpeg",".png",".webp"):
        if u.lower().endswith(ext) or ext in u.lower():
            return True
    return False
 
# -----------------------------
# BUILD PAYLOAD
# -----------------------------
def build_product_payload(p):
    title = p.get("title") or f"Product-{p.get('sku')}"
    handle = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    try:
        cost   = float(p.get("costprice") or 0)
    except:
        cost = 0
    try:
        weight = float(p.get("weight") or 0)
    except:
        weight = 0
 
    price = calc_price(cost, weight)
    vendor       = last_value(p.get("productbrand"))
    product_type = last_value(p.get("productrange"))
    tags = (
        split_tags(p.get("productbrand")) +
        split_tags(p.get("productrange")) +
        TAGS_TO_INCLUDE +
        split_tags(title)
    )
    tags        = sanitize_tags(tags)
    description = build_description(p)
 
    # Images
    imgs = []
    for src in re.split(r"[|,]+", p.get("imageoffloads") or ""):
        if valid_image(src):
            imgs.append({"src": src.strip()})
 
    # Variants
    variants = []
    sizes = [s.strip() for s in re.split(r"[|,]+", p.get("sizeattribute") or "") if s.strip()]
    try:
        stock = int(p.get("stock") or 0)
    except:
        stock = 0
    barcode = p.get("barcode") or None
 
    if sizes:
        for s in sizes:
            variants.append({
                "option1": s,
                "price": str(price),
                "sku": p.get("sku",""),
                "inventory_quantity": stock,
                "inventory_management": "shopify",
                "cost": cost,
                "barcode": barcode,
                "weight": weight,
                "weight_unit": "g"
            })
    else:
        variants.append({
            "price": str(price),
            "sku": p.get("sku",""),
            "inventory_quantity": stock,
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
        "status": "active" if stock > 0 else "draft",
        "published": True if stock > 0 else False,
        "variants": variants,
        **({"images": imgs} if imgs else {})
    }
    if len(variants) > 1:
        product["options"] = [{"name": "Size", "values": sizes}]
    return product
 
# -----------------------------
# LOAD XML
# -----------------------------
def load_xml():
    print("📥 Downloading feed…")
    r = requests.get(XML_URL, timeout=60)
    r.raise_for_status()
    raw = r.content.decode("utf-8", errors="replace")
    # save debug
    try:
        with open("feed_debug.xml", "wb") as f:
            f.write(r.content)
        print("ℹ️ feed_debug.xml saved.")
    except:
        pass
 
    posts = re.findall(r"(?is)<post\b[^>]*?>.*?</post>", raw)
    max_take = LIMIT if LIMIT is not None else None
    if posts:
        items = []
        for frag in posts[:max_take]:
            try:
                root = ET.fromstring(f"<root>{frag}</root>")
                post = root.find(".//post")
                if post is None: continue
                data = {c.tag.lower(): c.text for c in post}
                items.append(data)
            except Exception as e:
                print("⚠️ fragment parse:", e)
        if items:
            print(f"🔎 Parsed {len(items)} items (fragment).")
            return items
 
    # Full parse
    try:
        root = ET.fromstring(raw)
        posts = root.findall(".//post")
        items = []
        for post in posts[:max_take] if max_take else posts:
            data = {c.tag.lower(): c.text for c in post}
            items.append(data)
        if items:
            print(f"🔎 Parsed {len(items)} items (full).")
            return items
    except ParseError as e:
        print("⚠️ full XML parse error:", e)
 
    # BeautifulSoup fallback
    try:
        from bs4 import BeautifulSoup
        print("🔧 BS4 fallback…")
        soup = BeautifulSoup(raw, "html.parser")
        posts = soup.find_all(lambda t: t.name and t.name.lower()=="post")
        items = []
        for post in posts[:max_take] if max_take else posts:
            data = {child.name.lower(): child.get_text() for child in post.find_all(recursive=False)}
            items.append(data)
        if items:
            return items
    except:
        pass
 
    print("❌ Could not parse feed.")
    return []
 
# -----------------------------
# INDEX STORE PRODUCTS
# -----------------------------
def build_store_index():
    print("📚 Indexing store products…")
    handle_map, sku_map, barcode_map = {}, {}, {}
    page = 1
 
    while True:
        url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json"
        res = safe_get(url, params={"limit": PAGE_LIMIT, "page": page})
        prods = res.get("products", [])
        if not prods:
            break
        for p in prods:
            pid = p.get("id")
            if p.get("handle"):
                handle_map[p["handle"]] = p
            for v in p.get("variants", []):
                sku = str(v.get("sku") or "")
                bc  = v.get("barcode")
                if sku:   sku_map[sku]      = p
                if bc:    barcode_map[bc]    = p
        if len(prods) < PAGE_LIMIT:
            break
        page += 1
        time.sleep(0.2)
 
    print(f"Indexed {len(handle_map)} handles, {len(sku_map)} SKUs, {len(barcode_map)} barcodes.")
    return handle_map, sku_map, barcode_map
 
# -----------------------------
# VARIANT ID MAP
# -----------------------------
def existing_variant_map(prod):
    sku_map, opt_map = {}, {}
    for v in prod.get("variants", []):
        vid  = v.get("id")
        sku  = str(v.get("sku") or "")
        opt1 = str(v.get("option1") or "")
        if sku:   sku_map[sku] = vid
        if opt1:  opt_map[opt1] = vid
    return sku_map, opt_map
 
# -----------------------------
# MAIN SYNC
# -----------------------------
def run_sync():
    if not SHOP_URL or not ACCESS_TOKEN or not XML_URL:
        print("❌ Missing SHOP_URL/ACCESS_TOKEN/XML_URL")
        return
 
    items = load_xml()
    if not items:
        print("❌ No items.")
        return
 
    handle_map, sku_map, barcode_map = build_store_index()
    created, updated = 0, 0
 
    for p in items:
        payload = build_product_payload(p)
        handle  = payload["handle"]
        sku     = (p.get("sku") or "").strip()
        bc      = p.get("barcode") or None
 
        # match existing
        existing = (
            sku_map.get(sku) or
            barcode_map.get(bc)  or
            handle_map.get(handle)
        )
        if not existing:
            # last resort: title search
            res = safe_get(
                f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json",
                params={"title": payload["title"]}
            )
            for cand in res.get("products", []):
                # check its variants
                for v in cand.get("variants", []):
                    if (sku and str(v.get("sku"))==sku) or (bc and v.get("barcode")==bc):
                        existing = cand
                        break
                if existing:
                    break
 
        if existing:
            # UPDATE
            vid_map, opt_map = existing_variant_map(existing)
            upd_vars = []
            for v in payload["variants"]:
                key_sku = v.get("sku","")
                key_opt = v.get("option1","")
                vid = vid_map.get(key_sku) or opt_map.get(key_opt)
                var = {
                    **({"id": vid} if vid else {}),
                    "price":                v["price"],
                    "sku":                  v.get("sku",""),
                    "inventory_quantity":   int(v.get("inventory_quantity",0)),
                    "inventory_management": v.get("inventory_management"),
                    "barcode":              v.get("barcode"),
                    "weight":               v.get("weight"),
                    "weight_unit":          v.get("weight_unit"),
                }
                # drop Nones
                var = {k:vv for k,vv in var.items() if vv is not None}
                upd_vars.append(var)
 
            body = {
                "title":       payload["title"],
                "body_html":   payload["body_html"],
                "vendor":      payload["vendor"],
                "product_type":payload["product_type"],
                "tags":        payload["tags"],
                "variants":    upd_vars,
            }
            if payload.get("images"):
                body["images"] = payload["images"]
 
            res = safe_put(
                f"https://{SHOP_URL}/admin/api/{API_VERSION}/products/{existing['id']}.json",
                {"product": body}
            )
            if res.get("product"):
                updated += 1
                print(f"🔄 Updated {payload['title']} (ID {existing['id']})")
                # refresh maps
                prod = res["product"]
                handle_map[prod["handle"]] = prod
                for v in prod.get("variants", []):
                    sku_map[str(v.get("sku") or "")] = prod
                    bc2 = v.get("barcode")
                    if bc2:
                        barcode_map[bc2] = prod
            else:
                print(f"❌ Update failed: {payload['title']}")
        else:
            # CREATE
            res = safe_post(
                f"https://{SHOP_URL}/admin/api/{API_VERSION}/products.json",
                {"product": payload}
            )
            prod = res.get("product") or {}
            pid = prod.get("id")
            if pid:
                created += 1
                print(f"🆕 Created {payload['title']} (ID {pid})")
                handle_map[prod["handle"]] = prod
                for v in prod.get("variants", []):
                    sku_map[str(v.get("sku") or "")] = prod
                    bc2 = v.get("barcode")
                    if bc2:
                        barcode_map[bc2] = prod
            else:
                print(f"❌ Create failed: {payload['title']}")
 
        # throttle
        time.sleep(0.5)
 
    print("✅ DONE")
    print(f"Created: {created}, Updated: {updated}")
 
# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    start = time.time()
    try:
        run_sync()
    except Exception as e:
        print("Fatal:", e)
    print(f"⏱ Elapsed: {time.time()-start:.1f}s")
