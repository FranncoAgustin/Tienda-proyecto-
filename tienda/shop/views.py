from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import csv
import io
import json
import re
import os
from datetime import datetime
from pathlib import Path
from difflib import SequenceMatcher

# ---- ReportLab (import seguro) --------------------------------------
try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader
    # ImageReader lo importamos localmente dentro de las funciones
    REPORTLAB_OK = True
    REPORTLAB_ERR = ""
except Exception as e:
    REPORTLAB_OK = False
    REPORTLAB_ERR = str(e)
# --------------------------------------------------------------------

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import models, transaction
from django.db.models import Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_POST
from django.core.paginator import Paginator

from .forms import ProductForm, ProductImageFormSet, VariantFormSet
from .models import (
    Category,
    Order,
    OrderItem,
    Product,
    ProductImage,
    StockEntry,
    Variant,
    PriceChangeBatch, 
    PriceChangeItem,
)

# ---------------------------------------------------------------------
# Helpers / Utils
# ---------------------------------------------------------------------

def _price_for_variant(v: Variant) -> Decimal:
    return v.price_override if v.price_override is not None else v.product.base_price

def _cart_items_from_session(request):
    """
    Devuelve (items, subtotal). items: lista de dicts con variant, qty, unit_price, line_total
    Estructura de session: { "variant_id": qty, ... }
    """
    cart = _get_cart(request.session)  # dict
    if not cart:
        return [], Decimal('0.00')

    ids = [int(k) for k in cart.keys() if str(k).isdigit()]
    variants = (
        Variant.objects.filter(id__in=ids, active=True, product__active=True)
        .select_related("product")
    )
    by_id = {v.id: v for v in variants}

    items = []
    subtotal = Decimal('0.00')
    for k, qty in cart.items():
        try:
            vid = int(k)
            v = by_id.get(vid)
            if not v:
                continue
            q = max(1, int(qty))
            unit = _price_for_variant(v)
            line = (unit * q).quantize(Decimal('0.01'))
            items.append({"variant": v, "qty": q, "unit_price": unit, "line_total": line})
            subtotal += line
        except Exception:
            continue
    return items, subtotal

def _active_promotions_cached():
    from .models import Promotion  # import lazy
    now = timezone.now()
    promos = list(
        Promotion.objects.filter(
            active=True, start_at__lte=now, end_at__gte=now
        ).prefetch_related("products")
    )
    # cache pids en memoria
    for pr in promos:
        pr._pids = set(pr.products.values_list("id", flat=True))
    return promos

def _best_promo_for_product(promos, product):
    best = None
    best_pct = 0
    for pr in promos:
        applies = False
        if pr._pids:
            applies = (product.id in pr._pids)
        else:
            applies = (not pr.tech_filter) or (product.tech == pr.tech_filter)
        if applies and pr.percent > best_pct:
            best = pr
            best_pct = pr.percent
    return best  # o None

def _apply_percent(price: Decimal, percent: int) -> Decimal:
    q = (price * (Decimal(100 - percent) / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return q

def _parse_pct(s, default=None):
    """
    Convierte '10' -> Decimal('10'), '-5.5' -> Decimal('-5.5')
    Si viene vacío/None y hay default: devuelve default.
    """
    if s is None or str(s).strip() == "":
        return default
    return Decimal(str(s).replace(",", ".")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _round_to_500(value: Decimal, mode: str = "nearest") -> Decimal:
    """
    Redondea al entero en pesos más cercano que termine en 000 o 500.
    mode: 'nearest' (por defecto), 'up', 'down'
    """
    # Pasar a entero de pesos (primero redondeo a peso)
    n = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    rem = n % 1000

    if mode == "up":  # hacia arriba
        if rem == 0:
            out = n
        elif rem <= 500:
            out = n - rem + 500
        else:
            out = n - rem + 1000
    elif mode == "down":  # hacia abajo
        if rem < 500:
            out = n - rem
        else:
            out = n - rem + 500
    else:  # nearest: más cercano
        if rem < 250:
            out = n - rem
        elif rem < 750:
            out = n - rem + 500
        else:
            out = n - rem + 1000

    return Decimal(out).quantize(Decimal("1.00"))

def staff_required(view):
    return user_passes_test(
        lambda u: u.is_active and u.is_staff, login_url="/admin/login/"
    )(view)

def _get_cart(session):
    return session.get(settings.CART_SESSION_KEY, {})

def _save_cart(session, cart):
    session[settings.CART_SESSION_KEY] = cart
    session.modified = True

PLACEHOLDER = "https://via.placeholder.com/64?text=%E2%80%94"

def _product_thumb_or_placeholder(product: Product) -> str:
    img = product.images.order_by("order").first()
    if img:
        try:
            return img.image.url
        except Exception:
            pass
    v = product.variants.filter(active=True).exclude(image="").first()
    if v and getattr(v, "image", None):
        try:
            return v.image.url
        except Exception:
            pass
    return PLACEHOLDER

def _parse_decimal(s: str) -> Decimal:
    """
    Acepta "1.234,56", "1234.56", "$ 1.234,56", "U$S 5", etc.
    Devuelve Decimal o lanza InvalidOperation.
    """
    if s is None:
        raise InvalidOperation("empty")
    s = str(s).strip()
    s = re.sub(r"[^\d,.\-]", "", s)     # quita símbolos
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return Decimal(s)

def _normalize_tech(param: str) -> str | None:
    """
    Convierte parámetro libre a uno de SUB/LAS/3D/OTR o None.
    """
    if not param:
        return None
    s = (param or "").strip().lower()
    if s.startswith("sub"):
        return "SUB"
    if "laser" in s:
        return "LAS"
    if s in ("3d", "impresion 3d", "impresión 3d"):
        return "3D"
    if s.startswith("otr") or s == "otro":
        return "OTR"
    return None

def _normalize_tech_from_json(val: str) -> str:
    """
    Igual que arriba pero devuelve 'OTR' por defecto (útil al crear).
    """
    s = (val or "").strip().lower()
    if s.startswith("sub"):
        return "SUB"
    if "laser" in s:
        return "LAS"
    if s in ("3d", "impresion 3d", "impresión 3d"):
        return "3D"
    if s.startswith("otr") or s == "otro":
        return "OTR"
    return "OTR"

# ---------------------------------------------------------------------
# Público (Catálogo / Producto / Carrito / Checkout)
# ---------------------------------------------------------------------

def product_variants_by_id_json(request, pk):
    """
    Igual a product_variants_json pero resolviendo por PK estrictamente.
    """
    product = get_object_or_404(Product, pk=pk, active=True)

    vs = product.variants.filter(active=True).values(
        "id", "color", "size", "stock", "price_override", "image"
    )

    def abs_media_url(path_rel):
        if not path_rel:
            return None
        rel = settings.MEDIA_URL.rstrip("/") + "/" + str(path_rel).lstrip("/")
        return request.build_absolute_uri(rel)

    data = {
        "sku": product.sku,
        "public_name": product.public_name,
        "base_price": str(product.base_price or Decimal("0")),
        "variants": [
            {
                "id": v["id"],
                "label": (" ".join([v["color"] or "", v["size"] or ""]).strip() or "Única"),
                "stock": v["stock"] or 0,
                "price": (
                    str(v["price_override"]) if v["price_override"] is not None
                    else str(product.base_price or Decimal("0"))
                ),
                "image_url": abs_media_url(v.get("image")),
            }
            for v in vs
        ],
    }
    return JsonResponse(data)

def product_variants_json(request, sku):
    """
    Devuelve variantes activas del producto como JSON.
    Acepta SKU (string) o ID numérico (si 'sku' son dígitos).
    Corrige image_url para apuntar a MEDIA_URL.
    """
    # Buscar por SKU o por ID si es numérico
    product = Product.objects.filter(sku=sku, active=True).first()
    if not product and str(sku).isdigit():
        product = Product.objects.filter(pk=int(sku), active=True).first()
    if not product:
        raise Http404("Producto no encontrado")

    # Traer variantes activas
    vs = product.variants.filter(active=True).values(
        "id", "color", "size", "stock", "price_override", "image"
    )

    # Helper para URL absoluta de imagen de variante
    def abs_media_url(path_rel):
        if not path_rel:
            return None
        # path_rel suele ser "variants/archivo.jpg", lo preficamos con MEDIA_URL
        rel = settings.MEDIA_URL.rstrip("/") + "/" + str(path_rel).lstrip("/")
        return request.build_absolute_uri(rel)

    data = {
        "sku": product.sku,
        "public_name": product.public_name,
        "base_price": str(product.base_price or Decimal("0")),
        "variants": [
            {
                "id": v["id"],
                "label": (" ".join([v["color"] or "", v["size"] or ""]).strip() or "Única"),
                "stock": v["stock"] or 0,
                "price": (
                    str(v["price_override"]) if v["price_override"] is not None
                    else str(product.base_price or Decimal("0"))
                ),
                "image_url": abs_media_url(v.get("image")),
            }
            for v in vs
        ],
    }
    return JsonResponse(data)



def catalog(request):
    from decimal import Decimal
    q = (request.GET.get("q") or "").strip()

    # lee t= o tech=
    t_raw = (request.GET.get("t") or request.GET.get("tech") or "").strip()
    tech = _normalize_tech(t_raw)   # 'SUB'/'LAS'/'3D'/'OTR' o None

    qs = Product.objects.filter(active=True)

    if q:
        qs = qs.filter(
            Q(public_name__icontains=q)
            | Q(sku__icontains=q)
            | Q(description__icontains=q)
            | Q(variants__color__icontains=q)
            | Q(variants__size__icontains=q)
        ).distinct()

    if tech:
        qs = qs.filter(tech=tech)

    qs = qs.prefetch_related(
        Prefetch("variants", queryset=Variant.objects.filter(active=True)),
        Prefetch("images", queryset=ProductImage.objects.order_by("order")),
    ).order_by("public_name")

    paginator = Paginator(qs, 12)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # --- Adjuntamos campos "seguros para template" (sin "_") ---
    products = list(page_obj.object_list)
    now = timezone.now()

    for p in products:
        percent = None
        ends_at = None

        # Si tenés un modelo de ofertas/promos y un helper, intentamos usarlo.
        # Si no existe, cae al fallback sin romper.
        promo = None
        try:
            # Ejemplo: Offer.objects.active_for_product(p, now=now).first()
            from .models import Offer  # si no existe, entra al except
            if hasattr(Offer.objects, "active_for_product"):
                promo = Offer.objects.active_for_product(p, now=now).first()
        except Exception:
            promo = None

        if promo:
            try:
                percent = int(promo.percent or 0)
            except Exception:
                percent = None
            ends_at = getattr(promo, "ends_at", None)
        else:
            # Fallback: si guardaste algún percent/fecha en el Product
            for attr in ("current_discount_percent", "discount_percent", "promo_percent"):
                v = getattr(p, attr, None)
                if v:
                    try:
                        percent = int(v)
                        break
                    except Exception:
                        pass
            for attr in ("discount_ends_at", "promo_ends_at", "offer_ends_at"):
                v = getattr(p, attr, None)
                if v:
                    ends_at = v
                    break

        p.view_discount_percent = percent  # <- OK en templates
        p.view_promo_ends = ends_at        # <- OK en templates

        if percent and percent > 0:
            p.view_discounted_price = (p.base_price * (Decimal(100 - percent) / Decimal(100))).quantize(Decimal("0.01"))
        else:
            p.view_discounted_price = None

    return render(
        request,
        "shop/catalog.html",
        {
            "products": products,
            "page_obj": page_obj,
            "q": q,
            "t": t_raw,        # mantener en paginación / UI
            "tech_code": tech, # 'SUB'/'LAS'/'3D'/'OTR' o None
            "is_sub": tech == "SUB",
            "is_laser": tech == "LAS",
            "is_3d": tech == "3D",
        },
    )


def product_detail(request, pk=None, sku=None):
    if pk is not None:
        product = get_object_or_404(Product, pk=pk, active=True)
    elif sku is not None:
        product = Product.objects.filter(sku=sku, active=True).first()
        if not product and sku.isdigit():
            product = Product.objects.filter(pk=int(sku), active=True).first()
        if not product:
            raise Http404("Producto no encontrado")
    else:
        raise Http404("Producto no encontrado")

    variants = product.variants.filter(active=True).order_by("color", "size")
    return render(
        request,
        "shop/product_detail.html",
        {"product": product, "variants": variants},
    )

def _cart_qty(session) -> int:
    cart = _get_cart(session)
    if not isinstance(cart, dict):
        return 0
    total = 0
    for v in cart.values():
        try:
            total += int(v)
        except Exception:
            pass
    return total

@require_POST
def cart_add(request):
    # ¿viene JSON?
    if request.content_type and "application/json" in request.content_type:
        try:
            payload = json.loads((request.body or b"").decode("utf-8") or "{}")
        except Exception:
            return HttpResponseBadRequest("JSON inválido")
        variant_id = payload.get("variant_id")
        qty = payload.get("qty", 1)
    else:
        variant_id = request.POST.get("variant_id")
        qty = request.POST.get("qty", "1")

    # Parseo seguro
    try:
        variant_id = int(variant_id)
    except Exception:
        return HttpResponseBadRequest("variant_id inválido")

    if isinstance(qty, dict):
        qty = qty.get("qty", 1)
    try:
        qty = int(qty)
    except Exception:
        qty = 1
    qty = max(1, qty)

    v = get_object_or_404(Variant, pk=variant_id, active=True, product__active=True)

    # Stock
    if v.stock is not None and v.stock < qty:
        if "application/json" in (request.headers.get("Accept") or "") or \
           request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "stock", "message": "No hay stock suficiente"}, status=400)
        messages.error(request, "No hay stock suficiente para esa variante.")
        return redirect(request.META.get("HTTP_REFERER", "catalog"))

    # Carrito en sesión
    cart = _get_cart(request.session)
    if not isinstance(cart, dict):
        cart = {}

    prev = cart.get(str(v.id), 0)
    try:
        current = int(prev)
    except Exception:
        current = 0

    cart[str(v.id)] = current + qty
    _save_cart(request.session, cart)

    # Respuesta
    if "application/json" in (request.headers.get("Accept") or "") or \
       request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "cart_qty": _cart_qty(request.session)})
    else:
        messages.success(request, "Producto agregado al carrito.")
        return redirect(request.META.get("HTTP_REFERER", "catalog"))

def cart_remove(request):
    variant_id = request.POST.get("variant_id")
    cart = _get_cart(request.session)

    if not variant_id:
        messages.error(request, "No se indicó la variante a quitar.")
        return redirect("cart")

    # Aceptamos claves str o int
    removed = False
    if isinstance(cart, dict):
        if variant_id in cart:
            cart.pop(variant_id, None)
            removed = True
        else:
            # intentar con int->str / str->int
            try:
                cart.pop(str(int(variant_id)), None)
                removed = True
            except Exception:
                pass

    _save_cart(request.session, cart)

    if removed:
        messages.success(request, "Producto quitado del carrito.")
    else:
        messages.info(request, "Ese producto ya no estaba en el carrito.")

    return redirect("cart")

def cart_update(request):
    """
    Actualiza cantidades del carrito.
    Admite:
      - op=set  qty=N      → fija cantidad exacta
      - op=add  qty=N      → suma N a la cantidad actual
      - op=sub  qty=N      → resta N a la cantidad actual
    Si la cantidad final queda en 0, se elimina la línea.
    Responde JSON si el cliente lo pide, si no redirige a /cart/.
    """
    # Soportar JSON y form-url-encoded
    if request.content_type and "application/json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            return HttpResponseBadRequest("JSON inválido")
        variant_id = data.get("variant_id")
        op = (data.get("op") or "set").strip().lower()
        qty_in = data.get("qty", 1)
    else:
        variant_id = request.POST.get("variant_id")
        op = (request.POST.get("op") or "set").strip().lower()
        qty_in = request.POST.get("qty", "1")

    # Normalizar
    try:
        qty = max(0, int(qty_in))
    except Exception:
        qty = 0

    if not variant_id:
        return HttpResponseBadRequest("Falta variant_id")

    # Validamos la variante (y de paso conseguimos stock)
    v = get_object_or_404(Variant, pk=variant_id, active=True, product__active=True)

    cart = _get_cart(request.session)
    key = str(v.id)
    current = int(cart.get(key, 0))

    if op == "add":
        new_qty = current + qty
    elif op == "sub":
        new_qty = current - qty
    else:  # "set" (default)
        new_qty = qty

    # Clamp [0..stock] si tenés stock definido
    if v.stock is not None:
        new_qty = min(new_qty, max(0, int(v.stock or 0)))
    new_qty = max(0, new_qty)

    if new_qty <= 0:
        cart.pop(key, None)
    else:
        cart[key] = new_qty

    _save_cart(request.session, cart)

    # ¿JSON o redirección normal?
    wants_json = "application/json" in (request.headers.get("Accept") or "")
    if wants_json:
        cart_qty = sum(int(x) for x in cart.values())
        return JsonResponse({
            "ok": True,
            "variant_id": v.id,
            "line_qty": cart.get(key, 0),
            "cart_qty": cart_qty
        })

    # Mensajitos opcionales
    if op == "sub":
        messages.success(request, f"Se descontaron {qty} u. de {v.product.public_name or v.product.sku}.")
    elif op == "add":
        messages.success(request, f"Se agregaron {qty} u. de {v.product.public_name or v.product.sku}.")
    else:
        messages.success(request, f"Cantidad actualizada.")

    return redirect("cart")


from decimal import Decimal, ROUND_HALF_UP
from django.views.decorators.http import require_POST
from django.shortcuts import redirect
from django.http import JsonResponse, HttpResponseBadRequest

PLACEHOLDER = "https://via.placeholder.com/64?text=%E2%80%94"  # si no lo tenías ya arriba

def cart_view(request):
    # --- cupón (opcional) ---
    coupon_msg = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "apply_coupon":
            code = (request.POST.get("coupon") or "").strip().upper()
            COUPONS = {"DOMINGO": 10, "SUB10": 10, "LAS5": 5}
            pct = COUPONS.get(code)
            if pct:
                request.session["coupon"] = {"code": code, "percent": pct}
                request.session.modified = True
                coupon_msg = f"Cupón {code} aplicado ({pct}%)."
            else:
                request.session.pop("coupon", None)
                request.session.modified = True
                coupon_msg = "Cupón inválido."
        elif action == "remove_coupon":
            request.session.pop("coupon", None)
            request.session.modified = True
            coupon_msg = "Cupón quitado."

    applied_coupon = request.session.get("coupon")
    pct = Decimal(str(applied_coupon.get("percent", 0))) if applied_coupon else Decimal("0")

    # --- items desde la sesión ---
    items_raw, subtotal = _cart_items_from_session(request)

    view_items = []
    for it in items_raw:
        v = it["variant"]   # Variant (ya viene con product por select_related)
        p = v.product

        # etiqueta variante
        variant_label = f"{(v.color or '').strip()} {(v.size or '').strip()}".strip() or "Única"

        # imagen (variante > producto > placeholder)
        img_url = None
        try:
            if getattr(v, "image", None):
                img_url = v.image.url
        except Exception:
            img_url = None
        if not img_url:
            img = p.images.order_by("order").first()
            if img:
                try:
                    img_url = img.image.url
                except Exception:
                    img_url = None
        if not img_url:
            img_url = PLACEHOLDER

        unit = it["unit_price"]     # Decimal
        qty  = it["qty"]
        line = it["line_total"]

        # aplicar cupón (si hay)
        unit_now = unit
        line_now = line
        if pct and pct != 0:
            factor = (Decimal("100") - pct) / Decimal("100")
            unit_now = (unit * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            line_now = (unit_now * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        view_items.append({
            "variant": v,
            "product": p,
            "variant_label": variant_label,
            "qty": qty,
            "unit_price": unit,
            "line_total": line,
            "unit_price_now": unit_now,
            "line_total_now": line_now,
            "thumb": img_url,
        })

    # totales
    total = sum(x["line_total_now"] for x in view_items) if pct else subtotal

    return render(
        request,
        "shop/cart.html",
        {
            "items": view_items,
            "subtotal": subtotal,
            "total": total,
            "applied_coupon": applied_coupon,
            "coupon_msg": coupon_msg,
        },
    )

@require_POST
def cart_update(request):
    """
    Actualiza cantidades del carrito:
      - op=add  qty=K  -> suma K
      - op=sub  qty=K  -> resta K (si queda <=0, elimina)
      - op=set  qty=K  -> fija K (si K<=0, elimina)
    Soporta AJAX (Accept: application/json) devolviendo cart_qty.
    """
    variant_id = request.POST.get("variant_id")
    if not variant_id:
        return HttpResponseBadRequest("Falta variant_id")

    op = (request.POST.get("op") or "set").strip().lower()
    try:
        qty = int(request.POST.get("qty", "1"))
    except Exception:
        qty = 1
    qty = max(0, qty)

    # buscar variante
    try:
        v = Variant.objects.select_related("product").get(pk=variant_id, active=True, product__active=True)
    except Variant.DoesNotExist:
        return HttpResponseBadRequest("Variante inexistente")

    # carrito actual
    cart = _get_cart(request.session)
    cur = int(cart.get(str(v.id), 0))

    if op == "add":
        new_qty = cur + qty
    elif op == "sub":
        new_qty = cur - qty
    else:  # set (por default)
        new_qty = qty

    # respetar stock si existe
    if v.stock is not None:
        new_qty = min(new_qty, max(0, int(v.stock)))

    # aplicar cambio
    if new_qty <= 0:
        cart.pop(str(v.id), None)
    else:
        cart[str(v.id)] = new_qty

    _save_cart(request.session, cart)

    # respuesta AJAX opcional
    if "application/json" in (request.headers.get("Accept") or "") or \
       request.headers.get("X-Requested-With") == "XMLHttpRequest":
        cart_qty = sum(int(x) for x in cart.values()) if isinstance(cart, dict) else 0
        return JsonResponse({"ok": True, "cart_qty": cart_qty, "variant_id": v.id, "new_qty": new_qty})

    # navegación normal
    return redirect("cart")


def checkout(request):
    # Precios de envío (podés moverlos a settings)
    SHIPPING_PRICES = {
        "pickup": Decimal("0"),
        "standard": Decimal("3500"),  # ajustá a gusto
        "express": Decimal("6000"),
    }

    items, subtotal = _cart_items_from_session(request)
    if request.method == "GET":
        if not items:
            messages.warning(request, "Tu carrito está vacío.")
            return redirect("catalog")
        shipping_method = request.GET.get("shipping_method") or "pickup"
        shipping = SHIPPING_PRICES.get(shipping_method, SHIPPING_PRICES["pickup"])
        total = (subtotal + shipping).quantize(Decimal('0.01'))
        return render(request, "shop/checkout.html", {
            "items": items,
            "subtotal": subtotal,
            "shipping": shipping,
            "total": total,
            "shipping_method": shipping_method,
            "shipping_prices": SHIPPING_PRICES,
        })

    # POST -> validar y crear orden + items
    full_name = (request.POST.get("full_name") or "").strip()
    email = (request.POST.get("email") or "").strip()
    notes = (request.POST.get("notes") or "").strip()
    shipping_method = (request.POST.get("shipping_method") or "pickup").strip()

    if not full_name or not email:
        return render(request, "shop/checkout.html", {
            "error": "Completá tus datos.",
            "items": items,
            "subtotal": subtotal,
            "shipping": SHIPPING_PRICES.get(shipping_method, 0),
            "total": (subtotal + SHIPPING_PRICES.get(shipping_method, 0)).quantize(Decimal('0.01')),
            "shipping_method": shipping_method,
            "shipping_prices": SHIPPING_PRICES,
            "full_name": full_name,
            "email": email,
            "notes": notes,
        })

    if not items:
        messages.warning(request, "Tu carrito está vacío.")
        return redirect("catalog")

    shipping = SHIPPING_PRICES.get(shipping_method, SHIPPING_PRICES["pickup"])
    total = (subtotal + shipping).quantize(Decimal('0.01'))

    with transaction.atomic():
        order = Order.objects.create(
            email=email,
            full_name=full_name,
            total=total,
            status="pending",
        )
        # Guardar items
        for it in items:
            OrderItem.objects.create(
                order=order,
                variant=it["variant"],
                quantity=it["qty"],
                unit_price=it["unit_price"],
            )
        # (Opcional) guardar notas / método de envío si tu modelo tiene campos
        if hasattr(order, "notes"):
            order.notes = f"{notes} | envío: {shipping_method} (${shipping})"
            order.save(update_fields=["notes", "total"])

    # Vaciar carrito y redirigir a MP
    request.session[settings.CART_SESSION_KEY] = {}
    request.session.modified = True
    return redirect(f"/pay/mp/create/?order_id={order.id}")

def mp_create_preference(request):
    from mercadopago import SDK

    order_id = request.GET.get("order_id")
    if not order_id:
        return HttpResponseBadRequest("Falta order_id")
    order = get_object_or_404(Order, pk=order_id, status="pending")

    sdk = SDK(settings.MP_ACCESS_TOKEN or "")
    items = [
        {
            "title": str(it.variant),
            "quantity": int(it.quantity),
            "currency_id": "ARS",
            "unit_price": float(it.unit_price),
        }
        for it in order.items.all()
    ]
    pref = sdk.preference().create(
        {
            "items": items,
            "external_reference": str(order.id),
            "back_urls": {
                "success": settings.SITE_BASE_URL + "/pay/mp/success/",
                "failure": settings.SITE_BASE_URL + "/pay/mp/failure/",
                "pending": settings.SITE_BASE_URL + "/pay/mp/pending/",
            },
            "auto_return": "approved",
            "notification_url": settings.SITE_BASE_URL + "/pay/mp/webhook/",
        }
    )
    if pref.get("status") != 201:
        return HttpResponse(
            f"Error MP {pref.get('status')}: {pref.get('response')}", status=500
        )
    order.mp_preference_id = pref["response"]["id"]
    order.save(update_fields=["mp_preference_id"])
    init_point = pref["response"]["init_point"]
    request.session[settings.CART_SESSION_KEY] = {}
    request.session.modified = True
    return redirect(init_point)

@csrf_exempt
def mp_webhook(request):
    return HttpResponse("OK")

# ---------------------------------------------------------------------
# Owner / Admin simple
# ---------------------------------------------------------------------

@login_required(login_url="/admin/login/")
@staff_required
def owner_offers(request):
    """
    Crea y lista promociones simples.
    - Podés filtrar por técnica o pegar SKUs (uno por línea) para asociar productos.
    """
    from .models import Promotion, Product
    msg = ""
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        percent = int(request.POST.get("percent") or 0)
        start_at = request.POST.get("start_at")  # "YYYY-MM-DDTHH:MM"
        end_at = request.POST.get("end_at")
        tech = (request.POST.get("tech") or "").strip().upper() or None
        active = bool(request.POST.get("active"))

        if not name or not percent or not start_at or not end_at:
            messages.error(request, "Completá nombre, %, inicio y fin.")
        else:
            pr = Promotion.objects.create(
                name=name,
                percent=max(1, min(90, percent)),
                start_at=start_at,
                end_at=end_at,
                tech_filter=(tech if tech in dict(Product.TECH_CHOICES) else None),
                active=active,
            )
            # SKUs opcionales
            skus_raw = request.POST.get("skus", "")
            if skus_raw.strip():
                skus = [s.strip() for s in skus_raw.splitlines() if s.strip()]
                ps = list(Product.objects.filter(sku__in=skus))
                pr.products.add(*ps)
            messages.success(request, "Promoción creada.")
            return redirect("owner_offers")

    promos = (
        Promotion.objects.all()
        .prefetch_related("products")
        .order_by("-active", "-end_at", "name")
    )
    return render(request, "shop/owner/offers.html", {"promos": promos})


@login_required(login_url="/admin/login/")
@staff_required
def owner_coupons(request):
    """
    Crea y lista cupones.
    """
    from .models import Coupon, Product
    msg = ""
    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        percent = int(request.POST.get("percent") or 0)
        start_at = request.POST.get("start_at") or None
        end_at = request.POST.get("end_at") or None
        tech = (request.POST.get("tech") or "").strip().upper() or None
        active = bool(request.POST.get("active"))
        limit = request.POST.get("usage_limit") or None

        if not code or not percent:
            messages.error(request, "Completá código y %.")
        else:
            Coupon.objects.create(
                code=code,
                percent=max(1, min(90, percent)),
                start_at=start_at,
                end_at=end_at,
                tech_filter=(tech if tech in dict(Product.TECH_CHOICES) else None),
                active=active,
                usage_limit=(int(limit) if limit else None),
            )
            messages.success(request, "Cupón creado.")
            return redirect("owner_coupons")

    coupons = Coupon.objects.all().order_by("-active", "code")
    return render(request, "shop/owner/coupons.html", {"coupons": coupons})

@login_required(login_url="/admin/login/")
@staff_required
def owner_dashboard(request):
    q = (request.GET.get("q") or "").strip()

    qs = Product.objects.all()

    if q:
        qs = qs.filter(Q(public_name__icontains=q) | Q(sku__icontains=q))

    qs = (
        qs.annotate(total_stock=Coalesce(Sum("variants__stock"), 0))
        .prefetch_related(
            Prefetch("images", queryset=ProductImage.objects.order_by("order")),
            Prefetch("variants", queryset=Variant.objects.only("id", "stock")),
        )
        .order_by("public_name", "sku")
    )

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    products = page_obj.object_list

    return render(
        request,
        "shop/owner/dashboard.html",
        {"q": q, "products": products, "page_obj": page_obj},
    )

@login_required(login_url="/admin/login/")
@user_passes_test(lambda u: u.is_active and u.is_staff, login_url="/admin/login/")
@transaction.atomic
def owner_bulk_pricing_revert(request, batch_id: int):
    batch = get_object_or_404(PriceChangeBatch, pk=batch_id)
    if batch.is_reverted:
        messages.info(request, f"El lote #{batch.id} ya fue revertido.")
        return redirect("owner_bulk_pricing")

    items = list(batch.items.select_related("product"))
    for it in items:
        p = it.product
        p.base_price = it.old_price
        p.save(update_fields=["base_price"])

    batch.is_reverted = True
    batch.save(update_fields=["is_reverted"])
    messages.success(request, f"Lote #{batch.id} revertido. {len(items)} productos restaurados.")
    return redirect("owner_bulk_pricing")

@login_required(login_url="/admin/login/")
@user_passes_test(lambda u: u.is_active and u.is_staff, login_url="/admin/login/")
def owner_bulk_pricing(request):
    """
    Recalcula precios masivamente con:
      - Porcentaje global y/o por técnica (SUB/LAS/3D/OTR)
      - Modo de redondeo a 000/500: nearest / up / down
      - Permite porcentajes negativos (ej: -5)
      - Historial (PriceChangeBatch) y reversión por lote
    """
    # Defaults de UI
    ctx = {
        "pct_global": request.POST.get("pct_global", "0"),
        "pct_sub": request.POST.get("pct_sub", ""),
        "pct_las": request.POST.get("pct_las", ""),
        "pct_3d": request.POST.get("pct_3d", ""),
        "pct_otr": request.POST.get("pct_otr", ""),
        "round_mode": request.POST.get("round_mode", "nearest"),
        "note": request.POST.get("note", ""),
        "batches": PriceChangeBatch.objects.order_by("-created_at")[:20],
    }

    if request.method != "POST":
        return render(request, "shop/owner/bulk_pricing.html", ctx)

    # Parseo de porcentajes
    try:
        pct_global = _parse_pct(request.POST.get("pct_global", "0"), default=Decimal("0"))
        pct_sub = _parse_pct(request.POST.get("pct_sub"), default=None)
        pct_las = _parse_pct(request.POST.get("pct_las"), default=None)
        pct_3d  = _parse_pct(request.POST.get("pct_3d"),  default=None)
        pct_otr = _parse_pct(request.POST.get("pct_otr"), default=None)
    except Exception:
        messages.error(request, "Alguno de los porcentajes no es válido.")
        return render(request, "shop/owner/bulk_pricing.html", ctx)

    round_mode = (request.POST.get("round_mode") or "nearest").lower().strip()
    if round_mode not in ("nearest", "up", "down"):
        round_mode = "nearest"

    # Mapa por técnica (si una técnica viene vacía -> usa global)
    pct_by_tech = {
        "SUB": pct_sub,
        "LAS": pct_las,
        "3D":  pct_3d,
        "OTR": pct_otr,
    }

    note = (request.POST.get("note") or "").strip()

    qs = Product.objects.all().only("id", "tech", "base_price")

    updated_items = []
    with transaction.atomic():
        batch = PriceChangeBatch.objects.create(
            user=request.user,
            params={
                "pct_global": str(pct_global),
                "pct_by_tech": {k: (str(v) if v is not None else None) for k, v in pct_by_tech.items()},
                "round_mode": round_mode,
            },
            note=note,
        )

        for p in qs:
            old = p.base_price or Decimal("0")
            # Busca % específico por técnica; si no hay usa global
            pct = pct_by_tech.get(p.tech) if p.tech in pct_by_tech else None
            if pct is None:
                pct = pct_global

            # si todo dio 0% y global 0% => no tocar
            if pct == 0:
                continue

            # calcula nuevo precio
            factor = (Decimal("100") + pct) / Decimal("100")
            raw = (old * factor)
            new = _round_to_500(raw, mode=round_mode)
            # Evitar negativos o cero por si usan -100%
            if new < Decimal("1.00"):
                new = Decimal("1.00").quantize(Decimal("1.00"))

            if new != old:
                PriceChangeItem.objects.create(
                    batch=batch, product=p, old_price=old, new_price=new
                )
                p.base_price = new
                p.save(update_fields=["base_price"])
                updated_items.append((p.id, old, new))

        batch.updated_count = len(updated_items)
        batch.save(update_fields=["updated_count"])

    messages.success(
        request,
        f"Actualizados {len(updated_items)} productos. Lote #{batch.id} guardado en el historial."
    )
    ctx["batches"] = PriceChangeBatch.objects.order_by("-created_at")[:20]
    return render(request, "shop/owner/bulk_pricing.html", ctx)

@login_required(login_url="/admin/login/")
@staff_required
def owner_stock_intake(request):
    msg = ""
    if request.method == "POST":
        lines = request.POST.get("lines", "").strip()
        file = request.FILES.get("file")
        entries = []

        if lines:
            for raw in lines.splitlines():
                if not raw.strip():
                    continue
                parts = [p.strip() for p in raw.split(",")]
                if len(parts) < 5:
                    continue
                entries.append(
                    {
                        "date": parts[0],
                        "sku_or_interno": parts[1],
                        "color": parts[2] if len(parts) > 2 else "",
                        "size": parts[3] if len(parts) > 3 else "",
                        "qty": parts[4],
                        "unit_cost": parts[5] if len(parts) > 5 else "0",
                        "note": parts[6] if len(parts) > 6 else "",
                    }
                )
        elif file:
            decoded = file.read().decode("utf-8", errors="ignore")
            reader = csv.DictReader(io.StringIO(decoded))
            for row in reader:
                entries.append(
                    {
                        "date": row.get("date", ""),
                        "sku_or_interno": (row.get("sku", "") or row.get("interno", "") or row.get("Producto", "")),
                        "color": row.get("color", "") or row.get("colores o talles", ""),
                        "size": row.get("size", ""),
                        "qty": row.get("qty", "") or row.get("cantidad", ""),
                        "unit_cost": row.get("unit_cost", "") or row.get("costo", "") or "0",
                        "note": row.get("note", "") or row.get("nota", ""),
                    }
                )

        ok, fail = 0, 0

        with transaction.atomic():
            for e in entries:
                try:
                    date = datetime.strptime(e["date"], "%Y-%m-%d").date()
                    key = (e["sku_or_interno"] or "").strip()
                    color = (e["color"] or "").strip()[:64]
                    size = (e["size"] or "").strip()[:32]
                    qty = int(e["qty"])
                    unit_cost = Decimal(str(e["unit_cost"]).replace(",", "."))

                    if not key or not qty:
                        fail += 1
                        continue

                    p = Product.objects.filter(
                        Q(sku__iexact=key) | Q(public_name__iexact=key)
                    ).first()

                    if not p:
                        p = Product.objects.create(
                            sku=key,
                            public_name=key,
                            base_price=Decimal("0.00"),
                            active=True,
                        )

                    v, _ = Variant.objects.get_or_create(
                        product=p,
                        color=color,
                        size=size,
                        defaults={"stock": 0, "active": True},
                    )

                    v.stock = (v.stock or 0) + qty
                    v.save(update_fields=["stock"])

                    StockEntry.objects.create(
                        date=date,
                        product=p,
                        variant=v,
                        quantity=qty,
                        unit_cost=unit_cost,
                        note=e.get("note", ""),
                        source_name=key,
                    )

                    ok += 1
                except Exception:
                    fail += 1

        msg = f"Ingresos OK: {ok}, con error: {fail}."

    return render(request, "shop/owner/stock_intake.html", {"msg": msg})

@login_required(login_url="/admin/login/")
@staff_required
def owner_stock_intake_ui(request):
    """
    Pantalla UI guiada (JS) para ingresos: búsqueda, variantes, qty, costo y notas.
    """
    return render(request, "shop/owner/stock_intake_ui.html")

@login_required(login_url="/admin/login/")
@staff_required
def owner_bitacora(request):
    entries = (
        StockEntry.objects.select_related("product", "variant")
        .order_by("-date", "-id")[:500]
    )
    return render(request, "shop/owner/bitacora.html", {"entries": entries})

@transaction.atomic
def product_manage(request, sku=None, pk=None):
    # Obtener producto por pk o sku
    if pk:
        product = get_object_or_404(Product, pk=pk)
    else:
        product = get_object_or_404(Product, sku=sku)

    if request.method == "POST":
        form = ProductForm(request.POST, request.FILES, instance=product)
        imgformset = ProductImageFormSet(
            request.POST, request.FILES, instance=product, prefix="images"
        )
        vformset = VariantFormSet(
            request.POST, request.FILES, instance=product, prefix="variants"
        )
        if form.is_valid() and imgformset.is_valid() and vformset.is_valid():
            form.save()
            imgformset.save()
            vformset.save()
            messages.success(request, "Producto guardado correctamente.")
            if request.POST.get("stay"):
                return redirect("product_manage", sku=product.sku)
            return redirect("owner_dashboard")
    else:
        form = ProductForm(instance=product)
        imgformset = ProductImageFormSet(instance=product, prefix="images")
        vformset = VariantFormSet(instance=product, prefix="variants")

    return render(
        request,
        "shop/owner/product_manage.html",
        {"product": product, "form": form, "imgformset": imgformset, "vformset": vformset},
    )

# ---------------------------------------------------------------------
# APIs usadas por la UI de /owner
# ---------------------------------------------------------------------

@require_GET
@login_required(login_url="/admin/login/")
@staff_required
def owner_search_products_api(request):
    q = (request.GET.get("q") or "").strip()
    qs = Product.objects.all()
    if q:
        qs = qs.filter(
            models.Q(sku__icontains=q) |
            models.Q(public_name__icontains=q)
        )
    qs = qs.prefetch_related("images")[:30]

    def abs_url(rel):
        return request.build_absolute_uri(rel)

    results = []
    for p in qs:
        img = p.images.first()
        thumb = abs_url(img.image.url) if img else abs_url("/static/img/placeholder-64.png")
        results.append({
            "id": p.id,
            "sku": p.sku,
            "public_name": p.public_name or "",
            "base_price": float(p.base_price or 0),
            "thumb": thumb,
            "tech": p.tech,  # 👈 para precargar el select al elegir producto
        })
    return JsonResponse({"results": results})

@require_GET
@login_required(login_url="/admin/login/")
@staff_required
def owner_api_categories(request):
    q = (request.GET.get("q") or "").strip()
    qs = Category.objects.filter(active=True)
    if q:
        qs = qs.filter(name__icontains=q)
    data = [{"id": c.id, "name": c.name} for c in qs.order_by("name")[:20]]
    return JsonResponse({"results": data})

@require_POST
@login_required(login_url="/admin/login/")
@staff_required
@transaction.atomic
def owner_api_product_set_category(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("JSON inválido")

    pid = data.get("product_id")
    category_id = data.get("category_id")
    category_name = (data.get("category") or "").strip()

    if not pid:
        return HttpResponseBadRequest("Falta product_id")

    product = get_object_or_404(Product, pk=pid)

    category = None
    if isinstance(category_id, int):
        category = get_object_or_404(Category, pk=category_id, active=True)
    elif category_name:
        slug = slugify(category_name)[:140]
        category, _ = Category.objects.get_or_create(
            slug=slug, defaults={"name": category_name, "active": True}
        )

    product.category = category
    product.save(update_fields=["category"])

    return JsonResponse(
        {
            "ok": True,
            "category": {
                "id": category.id if category else None,
                "name": category.name if category else "",
            },
        }
    )

def _product_image_fs_path(product) -> str | None:
    """
    Devuelve la ruta en disco de la mejor imagen del producto
    (primero ProductImage.order, si no, imagen de alguna variante).
    """
    try:
        img = product.images.order_by("order").first()
        if img and img.image and hasattr(img.image, "path"):
            return img.image.path
    except Exception:
        pass
    try:
        v = product.variants.filter(active=True).exclude(image="").first()
        if v and v.image and hasattr(v.image, "path"):
            return v.image.path
    except Exception:
        pass
    return None

def _draw_image_fit(c: canvas.Canvas, path: str, x: float, y: float, max_w: float, max_h: float) -> bool:
    """
    Dibuja una imagen centrada dentro de (x,y,max_w,max_h),
    manteniendo proporción. Devuelve True si pudo dibujarla.
    """
    if not path:
        return False
    try:
        from reportlab.lib.utils import ImageReader  # import local
        img = ImageReader(path)
        iw, ih = img.getSize()
        ratio = min(max_w / iw, max_h / ih)
        w, h = iw * ratio, ih * ratio
        c.drawImage(img, x + (max_w - w) / 2, y + (max_h - h) / 2,
                    width=w, height=h, preserveAspectRatio=True, mask='auto')
        return True
    except Exception:
        return False

def _truncate_to_width(c: canvas.Canvas, text: str, font_name: str, font_size: int, max_w: float) -> str:
    """
    Corta un texto para que entre en max_w, agregando '…' si hace falta.
    """
    c.setFont(font_name, font_size)
    if c.stringWidth(text, font_name, font_size) <= max_w:
        return text
    ell = "…"
    w_ell = c.stringWidth(ell, font_name, font_size)
    out = ""
    for ch in text:
        if c.stringWidth(out + ch, font_name, font_size) + w_ell > max_w:
            break
        out += ch
    return out + ell

def _draw_link(c: canvas.Canvas, x: float, y: float, label: str, url: str, font=("Helvetica", 10)):
    """
    Dibuja texto y agrega anotación clicable a 'url'.
    Devuelve el ancho usado.
    """
    if not url:
        return 0
    c.setFont(*font)
    w = c.stringWidth(label, font[0], font[1])
    # color tipo “link”
    c.setFillColor(colors.HexColor("#0a58ca"))
    c.drawString(x, y, label)
    # área clicable (ligeramente mayor que el texto)
    c.linkURL(url, (x, y-2, x+w, y+font[1]+2), relative=0, thickness=0, color=None)
    c.setFillColor(colors.black)
    return w

def _draw_footer_links(c: canvas.Canvas, page_w: float, margin: float, wa_url: str, ig_url: str):
    """
    Dibuja los links de contacto en el pie de página (izquierda).
    """
    y = 0.65 * cm  # altura del pie (sobre el borde inferior)
    x = 5.0 * cm 
    used = 1
    if wa_url:
        used = _draw_link(c, x, y, "WhatsApp +54 11-5663-7260", wa_url, font=("Helvetica", 20))
        x += used + c.stringWidth("   ", "Helvetica", 20)
    if ig_url:
        _draw_link(c, x+5, y, "Instagram", ig_url, font=("Helvetica", 20))

@login_required(login_url="/admin/login/")
@staff_required
def owner_export_pdf_ui(request):
    """
    Pantalla con opciones para exportar PDF del catálogo.
    Envía GET a owner_export_pdf con los parámetros elegidos.
    """
    ctx = {
        "q": (request.GET.get("q") or "").strip(),
        "t": (request.GET.get("t") or "").strip().lower(),  # '', sub, laser, 3d, otr
        "show_sku": (request.GET.get("show_sku") in ("1", "on", "true")),
        "wmark": (request.GET.get("wmark") in ("1", "on", "true")),
    }
    return render(request, "shop/owner/export_pdf.html", ctx)

def _draw_watermark(c: canvas.Canvas, page_w: float, page_h: float, path: str, rel_width: float = 0.70):
    """
    Dibuja el watermark centrado. rel_width es el ancho relativo de la página (0.18 = 18%).
    No aplica alpha en ReportLab; usá un PNG ya esfumado.
    """
    try:
        from reportlab.lib.utils import ImageReader  # import local
        img = ImageReader(path)
        iw, ih = img.getSize()
        target_w = page_w * rel_width
        ratio = target_w / iw
        w = target_w
        h = ih * ratio
        x = (page_w - w) / 2
        y = (page_h - h) / 2
        c.drawImage(img, x, y, width=w, height=h, mask='auto', preserveAspectRatio=True)
    except Exception:
        pass

@login_required(login_url="/admin/login/")
@staff_required
def owner_export_pdf(request):
    """
    Exporta PDF del catálogo con:
      - Imagen o placeholder
      - Nombre público
      - (opcional) SKU
      - Precio y (si hay promo) badge y precio con descuento
    Admite ?q= y ?t=, y ?wmark=1 (watermark), ?show_sku=1.
    """
    import os
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader

    show_sku = (request.GET.get("show_sku") or "").lower() in ("1", "true", "on", "yes")
    draw_wm  = (request.GET.get("wmark") or "").lower()    in ("1", "true", "on", "yes")

    q     = (request.GET.get("q") or "").strip()
    t_raw = (request.GET.get("t") or request.GET.get("tech") or "").strip()
    tech  = _normalize_tech(t_raw)

    qs = Product.objects.filter(active=True)
    if q:
        qs = qs.filter(Q(public_name__icontains=q) | Q(sku__icontains=q) | Q(description__icontains=q))
    if tech:
        qs = qs.filter(tech=tech)

    qs = qs.prefetch_related(
        Prefetch("images", queryset=ProductImage.objects.order_by("order")),
        Prefetch("variants", queryset=Variant.objects.only("id", "image", "active")),
    ).order_by("public_name", "sku")

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="catalogo.pdf"'

    page_w, page_h = landscape(A4)
    c = pdfcanvas.Canvas(response, pagesize=(page_w, page_h))

    # Watermark
    wm_path = getattr(settings, "PDF_WATERMARK_IMAGE", None)
    if draw_wm and wm_path and os.path.exists(wm_path):
        try:
            img = ImageReader(wm_path)
            iw, ih = img.getSize()
            target_w = page_w * 0.18  # tamaño ajustable
            ratio = target_w / iw
            w = target_w
            h = ih * ratio
            x = (page_w - w) / 2
            y = (page_h - h) / 2
            c.drawImage(img, x, y, width=w, height=h, mask='auto', preserveAspectRatio=True)
        except Exception:
            pass

    # Layout
    margin   = 1.0 * cm
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin

    COLS   = 3
    CARD_H = 7.8 * cm
    IMG_H  = 4.2 * cm
    PAD    = 0.35 * cm

    NAME_FONT  = ("Helvetica-Bold", 12)
    SKU_FONT   = ("Helvetica", 9)
    PRICE_FONT = ("Helvetica-Bold", 13)
    PRICE_OLD  = ("Helvetica", 9)
    LINE_GAP   = 0.46 * cm

    card_w        = usable_w / COLS
    rows_per_page = max(1, int(usable_h // CARD_H))
    per_page      = COLS * rows_per_page

    # Header
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, page_h - margin + 0.3*cm, "Catálogo de productos")
    c.setFont("Helvetica", 9)
    if q:
        c.drawString(margin, page_h - margin - 0.1*cm, f"búsqueda: “{q}”")

    products = list(qs)
    if not products:
        c.setFont("Helvetica-Oblique", 12)
        c.drawCentredString(page_w/2, page_h/2, "No hay productos para exportar con el filtro aplicado.")
        c.showPage(); c.save(); return response

    promos = _active_promotions_cached()

    def money(d): return f"$ {d:.2f}"

    for idx, p in enumerate(products):
        idx_in_page = idx % per_page
        col = idx_in_page % COLS
        row = idx_in_page // COLS

        if idx_in_page == 0 and idx != 0:
            c.showPage()
            # Redibujar header y watermark
            if draw_wm and wm_path and os.path.exists(wm_path):
                try:
                    img = ImageReader(wm_path)
                    iw, ih = img.getSize()
                    target_w = page_w * 0.18
                    ratio = target_w / iw
                    w = target_w
                    h = ih * ratio
                    x = (page_w - w) / 2
                    y = (page_h - h) / 2
                    c.drawImage(img, x, y, width=w, height=h, mask='auto', preserveAspectRatio=True)
                except Exception:
                    pass
            c.setFont("Helvetica-Bold", 12)
            c.drawString(margin, page_h - margin + 0.3*cm, "Catálogo de productos")
            c.setFont("Helvetica", 9)
            if q:
                c.drawString(margin, page_h - margin - 0.1*cm, f"búsqueda: “{q}”")

        x = margin + col * card_w
        y_top = page_h - margin - row * CARD_H
        x0, y0 = x + 2, y_top - CARD_H + 2
        w0, h0 = card_w - 4, CARD_H - 4

        # Borde
        c.roundRect(x0, y0, w0, h0, 8, stroke=1, fill=0)

        # Imagen
        img_box_x = x0 + PAD
        img_box_y = y_top - PAD - IMG_H
        img_box_w = w0 - 2 * PAD
        img_box_h = IMG_H

        img_path = _product_image_fs_path(p)
        ok = _draw_image_fit(c, img_path, img_box_x, img_box_y, img_box_w, img_box_h)
        if not ok:
            c.setLineWidth(0.5)
            c.setDash(3, 3)
            c.rect(img_box_x, img_box_y, img_box_w, img_box_h, stroke=1, fill=0)
            c.setDash()
            c.setFont("Helvetica-Oblique", 8)
            c.drawCentredString(img_box_x + img_box_w/2, img_box_y + img_box_h/2 - 4, "Sin imagen")

        # Promo (si aplica)
        promo = _best_promo_for_product(promos, p)
        discounted = None
        if promo:
            discounted = _apply_percent(p.base_price or Decimal("0"), promo.percent)
            # Badge rojo arriba-izquierda
            c.setFillColorRGB(0.87, 0.14, 0.14)
            c.setFont("Helvetica-Bold", 9.5)
            badge = f"{promo.percent}% OFF"
            c.drawString(img_box_x + 2, img_box_y + img_box_h - 10, badge)
            c.setFillColorRGB(0, 0, 0)

        # Área de texto
        text_top = img_box_y - 0.22 * cm
        max_w    = w0 - 2 * PAD

        name = p.public_name or p.sku or "(sin nombre)"
        name = _truncate_to_width(c, name, NAME_FONT[0], NAME_FONT[1], max_w)
        c.setFont(*NAME_FONT)
        c.drawString(x0 + PAD, text_top, name)

        y_run = text_top - LINE_GAP

        if show_sku and p.sku:
            sku_line = _truncate_to_width(c, f"SKU: {p.sku}", SKU_FONT[0], SKU_FONT[1], max_w)
            c.setFont(*SKU_FONT)
            c.drawString(x0 + PAD, y_run, sku_line)
            y_run -= LINE_GAP

        # Precio(s)
        if discounted:
            # Precio viejo (tachado)
            c.setFont(*PRICE_OLD)
            old_x = x0 + PAD
            old_y = y0 + 0.95 * cm
            text = money(p.base_price or Decimal("0"))
            c.drawString(old_x, old_y, text)
            # línea de tachado
            w_text = c.stringWidth(text, PRICE_OLD[0], PRICE_OLD[1])
            c.line(old_x, old_y + 2, old_x + w_text, old_y + 2)
            # Precio nuevo
            c.setFont(*PRICE_FONT)
            c.drawRightString(x0 + PAD + max_w, y0 + 0.65 * cm, money(discounted))
            # Fin promo
            c.setFont("Helvetica", 7)
            c.drawString(x0 + PAD, y0 + 0.35 * cm, f"hasta {promo.end_at:%d/%m %H:%M}")
        else:
            c.setFont(*PRICE_FONT)
            c.drawRightString(x0 + PAD + max_w, y0 + 0.65 * cm, money(p.base_price or Decimal("0")))

    c.showPage()
    c.save()
    return response

@require_POST
@login_required(login_url="/admin/login/")
@staff_required
def owner_api_set_tech(request, pk: int):
    """
    POST JSON: { "tech": "sub" | "laser" | "3d" | "otr" }
    Guarda Product.tech normalizado a: SUB / LAS / 3D / OTR
    """
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("JSON inválido")

    raw = (data.get("tech") or "").strip()
    tech = _normalize_tech_from_json(raw)

    valid = dict(Product.TECH_CHOICES).keys()
    if tech not in valid:
        return HttpResponseBadRequest("tech inválido")

    p = get_object_or_404(Product, pk=pk)
    p.tech = tech
    p.save(update_fields=["tech"])

    label = dict(Product.TECH_CHOICES)[tech]
    return JsonResponse({"ok": True, "tech": tech, "label": label})

@require_GET
@login_required(login_url="/admin/login/")
@staff_required
def owner_api_product_variants(request, product_id: int):
    p = get_object_or_404(Product, pk=product_id, active=True)
    vs = (
        p.variants.filter(active=True)
        .order_by("color", "size")
        .values("id", "color", "size", "stock")
    )
    variants = []
    for v in vs:
        label = (f"{v['color']} {v['size']}".strip() or "Única")
        variants.append({"id": v["id"], "label": label, "stock": v["stock"] or 0})
    return JsonResponse({"variants": variants})

@csrf_protect
@require_POST
@login_required(login_url="/admin/login/")
@staff_required
@transaction.atomic
def owner_stock_intake_api(request):
    """
    Espera JSON:
      {
        "product_id": <int>,
        "variant_id": <int|null>,
        "qty": <int>,
        "unit_cost": <str|number>,  # PRECIO DE COMPRA (solo bitácora)
        "note": <str>
      }
    - Suma qty al stock de la variante.
    - Crea StockEntry con unit_cost (precio de compra), date=localdate().
    - NO toca base_price del producto.
    """
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("JSON inválido")

    product_id = data.get("product_id")
    variant_id = data.get("variant_id")
    qty = data.get("qty")
    unit_cost = data.get("unit_cost", "0")
    note = (data.get("note") or "").strip()

    if not product_id:
        return HttpResponseBadRequest("Falta product_id")
    if qty is None:
        return HttpResponseBadRequest("Falta qty")
    try:
        qty = int(qty)
    except Exception:
        return HttpResponseBadRequest("qty inválido")
    if qty <= 0:
        return HttpResponseBadRequest("qty debe ser > 0")

    try:
        unit_cost_dec = _parse_decimal(unit_cost)
    except InvalidOperation:
        return HttpResponseBadRequest("unit_cost inválido")

    product = get_object_or_404(Product, pk=product_id, active=True)

    if variant_id in (None, "", "null"):
        variant = product.variants.filter(active=True).order_by("id").first()
        if not variant:
            variant = Variant.objects.create(
                product=product, color="", size="", stock=0, active=True
            )
    else:
        variant = get_object_or_404(
            Variant, pk=variant_id, product=product, active=True
        )

    variant.stock = (variant.stock or 0) + qty
    variant.save(update_fields=["stock"])

    entry = StockEntry.objects.create(
        date=timezone.localdate(),
        product=product,
        variant=variant,
        quantity=qty,
        unit_cost=unit_cost_dec,
        note=note,
        source_name=product.sku or product.public_name,
    )

    return JsonResponse(
        {"ok": True, "entry_id": entry.id, "new_stock": variant.stock, "unit_cost": f"{unit_cost_dec:.2f}"}
    )

@require_POST
@login_required(login_url="/admin/login/")
@staff_required
@transaction.atomic
def owner_product_create_api(request):
    """
    Crea un producto y (opcional) una variante inicial.
    JSON:
    {
      "sku": "SKU-001",
      "public_name": "Taza X",
      "description": "texto...",
      "base_price": "1234,56",
      "active": true,
      "tech": "sub|laser|3d|otr",
      "category": "Categoría opcional",
      "category_id": 12,
      "variant": { "color": "...", "size": "...", "sku": "...", "stock": 5, "active": true, "price_override":"0" }
    }
    """
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("JSON inválido")

    sku = (data.get("sku") or "").strip()
    if not sku:
        return HttpResponseBadRequest("Falta sku")
    if Product.objects.filter(sku__iexact=sku).exists():
        return HttpResponseBadRequest("Ya existe un producto con ese SKU")

    public_name = (data.get("public_name") or sku).strip()
    description = data.get("description") or ""
    active = bool(data.get("active", True))

    tech_in = data.get("tech", "")
    tech = _normalize_tech_from_json(tech_in)  # 'SUB'/'LAS'/'3D'/'OTR'

    base_price_raw = data.get("base_price", "0")
    try:
        base_price = _parse_decimal(base_price_raw)
    except InvalidOperation:
        return HttpResponseBadRequest("base_price inválido")

    # Crear producto con tech
    product = Product.objects.create(
        sku=sku,
        public_name=public_name,
        description=description,
        base_price=base_price,
        active=active,
        tech=tech,
    )

    # Categoría opcional
    cat_id = data.get("category_id")
    cat_name = (data.get("category") or "").strip()
    if cat_id:
        category = get_object_or_404(Category, pk=cat_id, active=True)
        product.category = category
        product.save(update_fields=["category"])
    elif cat_name:
        cslug = slugify(cat_name)[:140]
        category, _ = Category.objects.get_or_create(slug=cslug, defaults={"name": cat_name, "active": True})
        product.category = category
        product.save(update_fields=["category"])

    # Variante opcional
    v_in = data.get("variant") or {}
    variant_payload = None
    if v_in:
        v_color = (v_in.get("color") or "").strip()
        v_size = (v_in.get("size") or "").strip()
        v_sku  = (v_in.get("sku") or "").strip() or None
        v_active = bool(v_in.get("active", True))
        v_stock = int(v_in.get("stock") or 0)
        v_price_override_raw = v_in.get("price_override", None)

        price_override = None
        if v_price_override_raw not in (None, "", "null"):
            try:
                price_override = _parse_decimal(v_price_override_raw)
            except InvalidOperation:
                return HttpResponseBadRequest("price_override inválido")

        variant = Variant.objects.create(
            product=product,
            color=v_color,
            size=v_size,
            sku=v_sku,
            active=v_active,
            stock=max(0, v_stock),
            price_override=price_override,
        )
        variant_payload = {
            "id": variant.id,
            "label": f"{variant.color} {variant.size}".strip() or "Única",
            "stock": variant.stock or 0,
        }

    thumb = _product_thumb_or_placeholder(product)

    return JsonResponse({
        "ok": True,
        "product": {
            "id": product.id,
            "sku": product.sku,
            "public_name": product.public_name,
            "base_price": f"{product.base_price:.2f}",
            "thumb": thumb or PLACEHOLDER,
            "tech": product.tech,
        },
        "variant": variant_payload
    })

# ---------------------------------------------------------------------
# Importador PDF (opcional)
# ---------------------------------------------------------------------
@login_required(login_url="/admin/login/")
@staff_required
def owner_import_pdf(request):
    """
    Importa/actualiza productos desde PDF (pdfminer.six) en dos pasos:

    Paso 1 (previsualización):
      - Parseamos el PDF y armamos "candidatos".
      - Detectamos duplicados en el mismo PDF.
      - Buscamos coincidencias EXACTAS en DB y SUGERENCIAS por similitud.
      - Mostramos tabla para decidir por cada ítem: Crear/Actualizar, Ignorar o Unir con existente.
      - Guardamos candidatos en sesión y esperamos confirmación.

    Paso 2 (confirmación):
      - Procesamos las decisiones y aplicamos cambios en DB.
      - Mostramos reporte final con listas (importados, actualizados, omitidos, etc.).

    Notas:
      - Precios en USD NO actualizan base_price; quedan listados para revisión.
      - Para ARS sí se crea/actualiza base_price.
    """

    # -------- helpers locales --------
    def _normalize(s: str) -> str:
        return re.sub(r"[\W_]+", "", (s or "").lower())

    def _similar(a: str, b: str) -> float:
        return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()

    def _best_suggestion(name: str, products) -> tuple[int | None, str, float]:
        """
        Devuelve (product_id, etiqueta, score) para la mejor coincidencia
        por similitud con umbral. Si no supera el umbral, (None, "", 0).
        """
        best_id, best_label, best_score = None, "", 0.0
        for pr in products:
            s1 = _similar(name, pr.get("sku") or "")
            s2 = _similar(name, pr.get("public_name") or "")
            score = max(s1, s2)
            if score > best_score:
                best_score = score
                label = f"{pr.get('sku') or '—'} · {pr.get('public_name') or '—'}"
                best_id, best_label = pr["id"], label
        # umbral "alto" para sugerencia (ajustable)
        return (best_id, best_label, best_score) if best_score >= 0.86 else (None, "", 0.0)

    # -------- import flexible de pdfminer --------
    try:
        from pdfminer_high_level import extract_text  # intento 1
    except Exception:
        try:
            from pdfminer.high_level import extract_text  # intento 2 oficial
        except Exception as e:
            # Sin pdfminer: mostrar pantalla con aviso y reporte vacío
            report = {
                "imported": 0, "updated": 0, "skipped": 0,
                "usd_to_review": [], "not_found": [], "not_seen_active": [],
                "imported_items": [], "updated_items": [], "skipped_items": [],
            }
            return render(
                request, "shop/owner/import_pdf.html",
                {"msg": f"Falta pdfminer.six en este entorno: {e}", "report": report, "update_only": False},
            )

    # -------- fase confirmación (paso 2) --------
    if request.method == "POST" and request.POST.get("confirm") == "1":
        key = f"pdf_import_candidates_{request.user.id}"
        candidates = request.session.get(key) or []
        update_only = bool(request.POST.get("update_only"))

        report = {
            "imported": 0, "updated": 0, "skipped": 0,
            "usd_to_review": [], "not_found": [], "not_seen_active": [],
            "imported_items": [], "updated_items": [], "skipped_items": [],
        }

        with transaction.atomic():
            for i, cand in enumerate(candidates):
                action = (request.POST.get(f"action_{i}") or "apply").strip()
                name = cand["name"]
                currency = cand["currency"]
                price_raw = cand["price"]

                # Acción: ignorar
                if action == "ignore":
                    report["skipped"] += 1
                    report["skipped_items"].append({
                        "reason": "ignorado_por_usuario",
                        "line": cand.get("line", ""),
                        "sku": name
                    })
                    continue

                # Determinar a qué producto aplicar:
                target_product = None
                target_id = None

                # Acción: "merge:<id>" (unir con existente)
                if action.startswith("merge:"):
                    try:
                        target_id = int(action.split(":", 1)[1])
                    except Exception:
                        target_id = None

                # Si no hay merge explícito, y hay "exact_db_id", usamos ese
                if not target_id:
                    target_id = cand.get("exact_db_id")

                if target_id:
                    try:
                        target_product = Product.objects.get(pk=target_id)
                    except Product.DoesNotExist:
                        target_product = None

                # Acción por defecto: apply (crear/actualizar)
                if not target_product:
                    # Buscar por coincidencia exacta SKU o public_name (case-insensitive)
                    p = Product.objects.filter(
                        Q(sku__iexact=name) | Q(public_name__iexact=name)
                    ).first()
                    if p:
                        target_product = p

                # Si no existe y el usuario no eligió ignorar, creamos
                created_now = False
                if not target_product:
                    if update_only:
                        report["not_found"].append(name)
                        report["skipped"] += 1
                        report["skipped_items"].append({
                            "reason": "update_only_sin_existente",
                            "line": cand.get("line", ""),
                            "sku": name
                        })
                        continue
                    target_product = Product.objects.create(
                        sku=name, public_name=name, base_price=Decimal("0.00"), active=True
                    )
                    created_now = True

                # Asegurar variante básica
                Variant.objects.get_or_create(
                    product=target_product, color="", size="",
                    defaults={"stock": 0, "active": True}
                )

                # Tratar el precio según moneda
                if currency == "USD":
                    # No tocamos base_price. Lo listamos para revisión
                    report["usd_to_review"].append({"sku": name, "price_usd": price_raw})
                    if created_now:
                        report["imported"] += 1
                        report["imported_items"].append({
                            "sku": name, "currency": "USD", "price": price_raw,
                            "note": "Creado (base_price=0.00)."
                        })
                    else:
                        report["updated"] += 1
                        report["updated_items"].append({
                            "sku": target_product.sku or name, "currency": "USD",
                            "price": price_raw, "prev_price": f"{target_product.base_price:.2f}",
                            "changed": False, "note": "USD: no se actualiza base_price."
                        })
                else:
                    # ARS ⇒ actualizar base_price
                    try:
                        price_value = Decimal(str(price_raw).replace(".", "").replace(",", "."))
                    except Exception:
                        report["skipped"] += 1
                        report["skipped_items"].append({
                            "reason": "precio_ARS_invalido_confirm",
                            "line": cand.get("line", ""),
                            "sku": name
                        })
                        continue

                    prev = target_product.base_price
                    changed = (prev != price_value)
                    if changed or created_now:
                        target_product.base_price = price_value
                        target_product.save(update_fields=["base_price"])

                    if created_now:
                        report["imported"] += 1
                        report["imported_items"].append({
                            "sku": target_product.sku or name, "currency": "ARS",
                            "price": f"{price_value:.2f}",
                            "note": "Creado"
                        })
                    else:
                        report["updated"] += 1
                        report["updated_items"].append({
                            "sku": target_product.sku or name, "currency": "ARS",
                            "price": f"{price_value:.2f}",
                            "prev_price": f"{prev:.2f}",
                            "changed": bool(changed),
                            "note": "Actualizado" if changed else "Sin cambio"
                        })

        # Informe de activos no vistos (opcional / informativo)
        seen_names = [c["name"] for c in candidates]
        active_with_sku = (
            Product.objects.filter(active=True)
            .exclude(sku__isnull=True).exclude(sku__exact="")
        )
        missing = active_with_sku.exclude(sku__in=seen_names).values_list("sku", flat=True)[:200]
        report["not_seen_active"] = list(missing)

        # limpiar sesión
        key = f"pdf_import_candidates_{request.user.id}"
        request.session.pop(key, None)

        msg = (
            f"PROCESO OK — importados {report['imported']}, actualizados {report['updated']}, omitidos {report['skipped']}, "
            f"USD a revisar {len(report['usd_to_review'])}, no encontrados {len(report['not_found'])}, "
            f"activos no vistos {len(report['not_seen_active'])}."
        )
        return render(
            request, "shop/owner/import_pdf.html",
            {"msg": msg, "report": report, "update_only": update_only, "preview": False},
        )

    # -------- fase previsualización (paso 1) --------
    update_only = bool(request.POST.get("update_only"))
    report = {
        "imported": 0, "updated": 0, "skipped": 0,
        "usd_to_review": [], "not_found": [], "not_seen_active": [],
        "imported_items": [], "updated_items": [], "skipped_items": [],
    }
    msg = ""
    preview = False
    candidates = []

    if request.method == "POST" and request.FILES.get("file"):
        f = request.FILES["file"]
        content = f.read()
        try:
            text = extract_text(io.BytesIO(content)) or ""
        except Exception as e:
            return render(
                request, "shop/owner/import_pdf.html",
                {"msg": f"ERROR al leer PDF: {e}", "report": report, "update_only": update_only, "preview": False},
            )

        lines = [ln.strip() for ln in text.splitlines()]

        def _looks_heading(l: str) -> bool:
            s = l.strip()
            if not s:
                return True
            if (
                s.lower().startswith("pág.")
                or "GENESIS INSUMOS" in s
                or "VIGENCIA:" in s
                or "ÍNDICE" in s.upper()
                or s.upper() == "INDICE"
            ):
                return True
            if len(s) <= 2:
                return True
            if re.fullmatch(r"[A-ZÁÉÍÓÚÜÑ ]{3,}", s) and ("$" not in s and "U$S" not in s):
                return True
            return False

        price_re = re.compile(r"""(?xi)^\s*(?:U\$S\s*(?P<usd>[\d.,]+)|\$\s*(?P<ars>[\d.,]+))\s*$""")
        def is_agotado(s: str) -> bool:
            return "AGOTAD" in (s or "").upper()

        last_name = None
        pending_name_lines = []
        raw_items = []

        def flush_pending_name():
            nonlocal pending_name_lines, last_name
            if not pending_name_lines:
                return
            candidate = re.sub(r"\s+", " ", " ".join(pending_name_lines)).strip()
            pending_name_lines = []
            if candidate and not _looks_heading(candidate):
                last_name = candidate

        for raw in lines:
            ln = raw.strip()

            if _looks_heading(ln):
                flush_pending_name()
                continue

            m = price_re.match(ln)
            if m:
                flush_pending_name()
                if not last_name or is_agotado(last_name):
                    continue

                price_ars = m.group("ars")
                price_usd = m.group("usd")

                if price_usd:
                    raw_items.append({"name": last_name, "currency": "USD", "price": price_usd, "line": ln})
                elif price_ars:
                    raw_items.append({"name": last_name, "currency": "ARS", "price": price_ars, "line": ln})
                last_name = None
                continue

            if ln:
                pending_name_lines.append(ln)
            else:
                flush_pending_name()

        # marcar duplicados en el mismo PDF
        counts = {}
        for it in raw_items:
            counts[it["name"]] = counts.get(it["name"], 0) + 1

        # obtener productos (para sugerencias)
        existing = list(Product.objects.values("id", "sku", "public_name"))

        # preparar candidatos con coincidencias
        for it in raw_items:
            name = it["name"]
            # coincidencia exacta en DB
            exact = Product.objects.filter(Q(sku__iexact=name) | Q(public_name__iexact=name)).values("id", "sku", "public_name").first()
            exact_id = exact["id"] if exact else None
            exact_label = f"{exact['sku'] or '—'} · {exact['public_name'] or '—'}" if exact else ""

            # sugerencia por similitud (si no hay exacta)
            sug_id = sug_label = ""
            sug_score = 0.0
            if not exact_id:
                bid, blabel, bscore = _best_suggestion(name, existing)
                if bid:
                    sug_id, sug_label, sug_score = bid, blabel, bscore

            candidates.append({
                "name": name,
                "currency": it["currency"],
                "price": it["price"],
                "line": it.get("line", ""),
                "dup_in_pdf": counts.get(name, 0) > 1,
                "exact_db_id": exact_id,
                "exact_db_label": exact_label,
                "sug_id": sug_id,
                "sug_label": sug_label,
                "sug_score": round(sug_score * 100),
            })

        # guardar candidatos en sesión para el paso 2
        key = f"pdf_import_candidates_{request.user.id}"
        request.session[key] = candidates
        request.session.modified = True

        preview = True  # mostrar UI de revisión

    return render(
        request, "shop/owner/import_pdf.html",
        {
            "msg": msg,
            "report": report,
            "update_only": update_only,
            "preview": preview,
            "candidates": candidates,
        },
    )

