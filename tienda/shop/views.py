from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST , require_GET
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest, Http404
from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import models, transaction
from .models import Product, Variant, Order, OrderItem, StockEntry, ProductImage
from decimal import Decimal,  InvalidOperation
import csv, io, re
from django.utils import timezone
from datetime import datetime, date
import json
from django.core.paginator import Paginator
from django.db.models import Q , Prefetch, Sum
from .forms import ProductForm, VariantFormSet, ProductImageFormSet
from django.contrib import messages
from django.db.models.functions import Coalesce





def product_variants_json(request, sku):
    product = get_object_or_404(Product, sku=sku, active=True)
    variants = product.variants.filter(active=True).values(
        "id", "color", "size", "stock", "price_override", "image"
    )
    return JsonResponse({
        "sku": product.sku,
        "public_name": product.public_name,
        "base_price": str(product.base_price),
        "variants": [
            {
                "id": v["id"],
                "label": " ".join([v["color"] or "", v["size"] or ""]).strip() or "Única",
                "stock": v["stock"],
                "price": str(v["price_override"]) if v["price_override"] is not None else str(product.base_price),
                "image_url": (request.build_absolute_uri(v["image"]) if v["image"] else None),
            } for v in variants
        ]
    })

def catalog(request):
    q = (request.GET.get("q") or "").strip()

    qs = Product.objects.filter(active=True)

    if q:
        qs = qs.filter(
            Q(public_name__icontains=q) |
            Q(sku__icontains=q) |
            Q(description__icontains=q) |
            Q(variants__color__icontains=q) |
            Q(variants__size__icontains=q)
        ).distinct()

    qs = qs.prefetch_related(
        Prefetch("variants", queryset=Variant.objects.filter(active=True)),
        Prefetch("images", queryset=ProductImage.objects.order_by("order"))
    ).order_by("public_name")

    paginator = Paginator(qs, 12)  # 12 cards por página
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)
    products = page_obj.object_list  # 👈 esto alimenta tu {% for p in products %}

    return render(request, "shop/catalog.html", {
        "products": products,
        "page_obj": page_obj,
        "q": q,
    })

def cart_add(request):
    if request.method != "POST":
        return redirect("catalog")

    variant_id = request.POST.get("variant_id")
    qty = int(request.POST.get("qty", "1") or "1")

    v = get_object_or_404(Variant, pk=variant_id, active=True, product__active=True)

    if qty < 1:
        qty = 1

    if v.stock is None or v.stock < qty:
        messages.error(request, "No hay stock suficiente para esa variante.")
        return redirect(request.META.get("HTTP_REFERER", "catalog"))

    # TODO: agregar al carrito (sesión/DB)
    # ejemplo: cart.add(variant=v, quantity=qty, unit_price=v.price)

    messages.success(request, "Producto agregado al carrito.")
    return redirect("catalog")

def product_detail(request, pk=None, sku=None):
    product = None

    if pk is not None:
        product = get_object_or_404(Product, pk=pk, active=True)
    elif sku is not None:
        # primero buscar por SKU exacto
        product = Product.objects.filter(sku=sku, active=True).first()
        # si no hay y el sku es numérico, probar por PK
        if not product and sku.isdigit():
            product = Product.objects.filter(pk=int(sku), active=True).first()
        if not product:
            raise Http404("Producto no encontrado")
    else:
        raise Http404("Producto no encontrado")

    variants = product.variants.filter(active=True).order_by("color", "size")
    return render(request, "shop/product_detail.html", {
        "product": product,
        "variants": variants,
    })

def _get_cart(session): return session.get(settings.CART_SESSION_KEY, {})
def _save_cart(session, cart): session[settings.CART_SESSION_KEY]=cart; session.modified=True

@require_POST
def cart_add(request):
    variant_id = request.POST.get("variant_id"); qty = int(request.POST.get("qty","1"))
    variant = get_object_or_404(Variant, pk=variant_id, active=True)
    if variant.stock < qty: return JsonResponse({"ok": False, "error": "Sin stock suficiente"}, status=400)
    cart = _get_cart(request.session); key=str(variant.id)
    if key in cart:
        new_qty = cart[key]["qty"] + qty
        if new_qty > variant.stock: return JsonResponse({"ok": False, "error": "Sin stock suficiente"}, status=400)
        cart[key]["qty"]=new_qty
    else:
        cart[key]={"name": str(variant), "price": str(variant.price), "qty": qty}
    _save_cart(request.session, cart); return JsonResponse({"ok": True, "count": sum(i['qty'] for i in cart.values())})

@require_POST
def cart_remove(request):
    variant_id = request.POST.get("variant_id"); cart=_get_cart(request.session)
    if variant_id in cart: del cart[variant_id]; _save_cart(request.session, cart)
    return redirect("cart")

def cart_view(request):
    cart = _get_cart(request.session); items=[]; total=Decimal("0.00")
    for key, it in cart.items():
        price=Decimal(it["price"]); qty=int(it["qty"]); subtotal=price*qty; total+=subtotal
        items.append({"key":key,"name":it["name"],"price":price,"qty":qty,"subtotal":subtotal})
    return render(request,"shop/cart.html",{"items":items,"total":total})

def checkout(request):
    cart = _get_cart(request.session)
    if request.method=="POST":
        email=request.POST.get("email","").strip(); full_name=request.POST.get("full_name","").strip()
        if not email or not full_name: return render(request,"shop/checkout.html",{"error":"Completá tus datos"})
        total=sum(Decimal(i["price"])*int(i["qty"]) for i in cart.values())
        order=Order.objects.create(email=email, full_name=full_name, total=total, status="pending")
        for key,it in cart.items():
            v=get_object_or_404(Variant, pk=int(key), active=True)
            OrderItem.objects.create(order=order, variant=v, quantity=int(it["qty"]), unit_price=Decimal(it["price"]))
        return redirect(f"/pay/mp/create/?order_id={order.id}")
    return render(request,"shop/checkout.html")

@transaction.atomic
def product_manage(request, sku=None, pk=None):
    # Obtener producto por pk o sku
    if pk:
        product = get_object_or_404(Product, pk=pk)
    else:
        product = get_object_or_404(Product, sku=sku)

    if request.method == "POST":
        form = ProductForm(request.POST, request.FILES, instance=product)
        imgformset = ProductImageFormSet(request.POST, request.FILES, instance=product, prefix="images")
        vformset = VariantFormSet(request.POST, request.FILES, instance=product, prefix="variants")
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

    return render(request, "shop/owner/product_manage.html", {
        "product": product,
        "form": form,
        "imgformset": imgformset,
        "vformset": vformset,
    })


def mp_create_preference(request):
    from mercadopago import SDK
    order_id=request.GET.get("order_id"); 
    if not order_id: return HttpResponseBadRequest("Falta order_id")
    order=get_object_or_404(Order, pk=order_id, status="pending")
    sdk=SDK(settings.MP_ACCESS_TOKEN or "")
    items=[{"title":str(it.variant),"quantity":int(it.quantity),"currency_id":"ARS","unit_price":float(it.unit_price)} for it in order.items.all()]
    pref=sdk.preference().create({"items":items,"external_reference":str(order.id),"back_urls":{"success":settings.SITE_BASE_URL+"/pay/mp/success/","failure":settings.SITE_BASE_URL+"/pay/mp/failure/","pending":settings.SITE_BASE_URL+"/pay/mp/pending/"},"auto_return":"approved","notification_url":settings.SITE_BASE_URL+"/pay/mp/webhook/"})
    if pref.get("status")!=201: return HttpResponse(f"Error MP {pref.get('status')}: {pref.get('response')}", status=500)
    order.mp_preference_id=pref["response"]["id"]; order.save(update_fields=["mp_preference_id"])
    init_point=pref["response"]["init_point"]; request.session[settings.CART_SESSION_KEY]={}; request.session.modified=True
    return redirect(init_point)

@csrf_exempt
def mp_webhook(request): return HttpResponse("OK")

def staff_required(view): return user_passes_test(lambda u: u.is_active and u.is_staff, login_url="/admin/login/")(view)
def normalize_name(s:str)->str: import re; return re.sub(r'\s+',' ',(s or '').strip()).lower()

@login_required(login_url="/admin/login/")
@staff_required
def owner_dashboard(request):
    q = (request.GET.get("q") or "").strip()

    qs = Product.objects.all()

    if q:
        qs = qs.filter(
            Q(public_name__icontains=q) |
            Q(sku__icontains=q)
        )

    # total_stock = suma de stock de todas las variantes del producto
    qs = (qs
          .annotate(total_stock=Coalesce(Sum("variants__stock"), 0))
          .prefetch_related(
              Prefetch("images", queryset=ProductImage.objects.order_by("order")),
              Prefetch("variants", queryset=Variant.objects.only("id", "stock"))
          )
          .order_by("public_name", "sku"))

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    products = page_obj.object_list

    return render(request, "shop/owner/dashboard.html", {
        "q": q,
        "products": products,
        "page_obj": page_obj,
    })


@login_required(login_url="/admin/login/")
@staff_required
def owner_bulk_pricing(request):
    msg=""; 
    if request.method=="POST":
        from decimal import Decimal
        try:
            f=Decimal(request.POST.get("factor","1.0"))
            for p in Product.objects.all():
                p.base_price=(p.base_price*f).quantize(Decimal("0.01")); p.save(update_fields=["base_price"])
            msg=f"Precios actualizados x{f}"
        except Exception as e: msg="Error: "+str(e)
    return render(request,"shop/owner/bulk_pricing.html",{"msg":msg})

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
                entries.append({
                    "date": parts[0],
                    "sku_or_interno": parts[1],
                    "color": parts[2] if len(parts) > 2 else "",
                    "size": parts[3] if len(parts) > 3 else "",
                    "qty": parts[4],
                    "unit_cost": parts[5] if len(parts) > 5 else "0",
                    "note": parts[6] if len(parts) > 6 else "",
                })
        elif file:
            decoded = file.read().decode("utf-8", errors="ignore")
            reader = csv.DictReader(io.StringIO(decoded))
            for row in reader:
                entries.append({
                    "date": row.get("date", ""),
                    "sku_or_interno": (row.get("sku", "") or row.get("interno", "") or row.get("Producto", "")),
                    "color": row.get("color", "") or row.get("colores o talles", ""),
                    "size": row.get("size", ""),
                    "qty": row.get("qty", "") or row.get("cantidad", ""),
                    "unit_cost": row.get("unit_cost", "") or row.get("costo", "") or "0",
                    "note": row.get("note", "") or row.get("nota", ""),
                })

        ok, fail = 0, 0

        with transaction.atomic():
            for e in entries:
                try:
                    # fecha: mantiene formato YYYY-MM-DD (igual que antes)
                    date = datetime.strptime(e["date"], "%Y-%m-%d").date()

                    key   = (e["sku_or_interno"] or "").strip()
                    color = (e["color"] or "").strip()[:64]
                    size  = (e["size"] or "").strip()[:32]
                    qty   = int(e["qty"])
                    unit_cost = Decimal(str(e["unit_cost"]).replace(",", "."))

                    if not key or not qty:
                        fail += 1
                        continue

                    # === BÚSQUEDA SIN ALIAS ===
                    # Busca por sku, internal_name o public_name, insensible a may/min
                    p = Product.objects.filter(
                        Q(sku__iexact=key) |
                        Q(public_name__iexact=key)
                    ).first()

                    # Si no existe, lo creamos usando la clave como sku/internal/public
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
                        source_name=key,   # mantenemos el campo de seguimiento
                    )

                    ok += 1

                except Exception:
                    fail += 1

        msg = f"Ingresos OK: {ok}, con error: {fail}."

    return render(request, "shop/owner/stock_intake.html", {"msg": msg})

@login_required(login_url="/admin/login/")
@staff_required
def owner_bitacora(request):
    rows=StockEntry.objects.select_related("product","variant").order_by("-date","-id")[:500]
    return render(request,"shop/owner/bitacora.html",{"rows":rows})

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction, models
from django.shortcuts import render
from decimal import Decimal
from pdfminer.high_level import extract_text
import io, re, unicodedata

from .models import Product, Variant

def staff_required(view):
    return user_passes_test(lambda u: u.is_active and u.is_staff, login_url="/admin/login/")(view)

def _clean(s: str) -> str:
    """Normaliza whitespaces y quita dobles espacios."""
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def _looks_heading(line: str) -> bool:
    """Heurísticas para saltar títulos / índice / pie de página."""
    l = line.strip()
    if not l:
        return True
    # Páginas, índices y pies (hechos del PDF)
    if l.lower().startswith("pág.") or "GENESIS INSUMOS" in l or "VIGENCIA:" in l or "ÍNDICE" in l.upper() or l.upper() == "INDICE":
        return True
    # Palabras muy cortas sueltas
    if len(l) <= 2:
        return True
    # Líneas sólo con la categoría (todas mayúsculas y sin $/U$S)
    if re.fullmatch(r"[A-ZÁÉÍÓÚÜÑ ]{3,}", l) and ("$" not in l and "U$S" not in l):
        return True
    return False

_price_re = re.compile(
    r"""(?xi)
    ^\s*
    (?:
        U\$S \s* (?P<usd>[\d]+(?:[.,]\d{1,2})?)   # precio en USD
      | \$ \s* (?P<ars>[\d]+(?:[.,]\d{1,2})?)     # precio en ARS
    )
    \s*$
    """
)

def _is_agotado(line: str) -> bool:
    return "AGOTADO" in line.upper() or "AGOTADA" in line.upper()

@login_required(login_url="/admin/login/")
@staff_required
def owner_import_pdf(request):
    """
    Modo normal (por defecto):
      - Crea o actualiza productos (como venía).
    Modo "Actualizar solo" (update_only=True):
      - Si el SKU/nombre existe -> actualiza precio
      - Si NO existe -> NO crea; lo reporta como 'not_found'
    Además:
      - USD: no modifica base_price; agrega a 'usd_to_review'
      - AGOTADO: se omite
    """
    # IMPORT LAZY para que el sitio arranque aunque falte la dependencia
    try:
        from pdfminer.high_level import extract_text
    except Exception as e:
        return render(request, "shop/owner/import_pdf.html", {
            "msg": f"Falta pdfminer.six en este entorno: {e}",
            "report": {"imported":0,"updated":0,"skipped":0,"usd_to_review":[],"not_found":[],"not_seen_active":[]}
        })

    update_only = bool(request.POST.get("update_only"))  # 👈 nuevo flag
    report = {
        "imported": 0,
        "updated": 0,
        "skipped": 0,
        "usd_to_review": [],
        "not_found": [],       # en update_only: productos que NO estaban en DB
        "not_seen_active": [], # activos en DB no vistos en este PDF (solo reporte)
    }
    msg = ""
    if request.method == "POST" and request.FILES.get("file"):
        f = request.FILES["file"]
        content = f.read()
        try:
            text = extract_text(io.BytesIO(content)) or ""
        except Exception as e:
            return render(request, "shop/owner/import_pdf.html", {"msg": f"ERROR al leer PDF: {e}", "report": report})

        lines = [ln.strip() for ln in text.splitlines()]

        # helpers locales
        def _clean(s: str) -> str:
            s = (s or "").strip()
            return re.sub(r"\s+", " ", s)

        def _looks_heading(line: str) -> bool:
            l = line.strip()
            if not l:
                return True
            if l.lower().startswith("pág.") or "GENESIS INSUMOS" in l or "VIGENCIA:" in l or "ÍNDICE" in l.upper() or l.upper() == "INDICE":
                return True
            if len(l) <= 2:
                return True
            if re.fullmatch(r"[A-ZÁÉÍÓÚÜÑ ]{3,}", l) and ("$" not in l and "U$S" not in l):
                return True
            return False

        price_re = re.compile(r"""(?xi)^\s*(?:U\$S\s*(?P<usd>[\d.,]+)|\$\s*(?P<ars>[\d.,]+))\s*$""")
        def is_agotado(s: str) -> bool:
            return "AGOTAD" in (s or "").upper()

        last_name = None
        pending_name_lines = []
        seen_this_run = set()  # (sku, precio_line)
        seen_skus = set()      # para reporte de "no vistos"

        def flush_pending_name():
            nonlocal pending_name_lines, last_name
            if not pending_name_lines:
                return
            candidate = _clean(" ".join(pending_name_lines))
            pending_name_lines = []
            if candidate and not _looks_heading(candidate):
                last_name = candidate

        with transaction.atomic():
            for raw in lines:
                ln = raw.strip()

                if _looks_heading(ln):
                    flush_pending_name()
                    continue

                m = price_re.match(ln)
                if m:
                    flush_pending_name()
                    if not last_name:
                        report["skipped"] += 1
                        continue
                    if is_agotado(last_name):
                        last_name = None
                        continue

                    sku = last_name  # SKU = nombre del PDF
                    seen_skus.add(sku)
                    key = (sku, ln)
                    if key in seen_this_run:
                        last_name = None
                        continue
                    seen_this_run.add(key)

                    p = Product.objects.filter(
                        Q(sku__iexact=sku) |
                        Q(public_name__iexact=sku)
                        ).first()

                    price_ars = m.group("ars")
                    price_usd = m.group("usd")

                    if price_usd:
                        # USD: NO toco base_price; sólo marco para revisar
                        if p:
                            changed = False
                            if not p.sku:
                                p.sku = sku; changed = True
                            if changed:
                                p.save()
                            Variant.objects.get_or_create(product=p, color="", size="", defaults={"stock": 0, "active": True})
                            report["updated"] += 1
                        else:
                            if update_only:
                                report["not_found"].append(sku)
                            else:
                                p = Product.objects.create(
                                    sku=sku, public_name=sku,
                                    base_price=Decimal("0.00"), active=True,
                                )
                                
                                Variant.objects.get_or_create(product=p, color="", size="", defaults={"stock": 0, "active": True})
                                report["imported"] += 1
                        report["usd_to_review"].append({"sku": sku, "price_usd": price_usd})
                        last_name = None
                        continue

                    if price_ars:
                        # Normalizo $ 1.234,56 -> 1234.56
                        norm = price_ars.replace(".", "").replace(",", ".")
                        try:
                            price_value = Decimal(norm)
                        except Exception:
                            report["skipped"] += 1
                            last_name = None
                            continue

                        if p:
                            # SIEMPRE sobrescribir precio con el del PDF
                            changed = False
                            if not p.sku:
                                p.sku = sku; changed = True
                            if p.base_price != price_value:
                                p.base_price = price_value; changed = True
                            if changed:
                                p.save()
                            Variant.objects.get_or_create(product=p, color="", size="", defaults={"stock": 0, "active": True})
                            report["updated"] += 1
                        else:
                            if update_only:
                                report["not_found"].append(sku)
                            else:
                                p = Product.objects.create(
                                    sku=sku, public_name=sku,
                                    base_price=price_value, active=True,
                                )
                                
                                Variant.objects.get_or_create(product=p, color="", size="", defaults={"stock": 0, "active": True})
                                report["imported"] += 1

                        last_name = None
                        continue

                    report["skipped"] += 1
                    last_name = None
                    continue

                # no es precio → acumulo potencial nombre
                if ln:
                    pending_name_lines.append(ln)
                else:
                    flush_pending_name()

        # (Opcional) Informe de activos no vistos en este PDF
        # No tocamos DB: sólo listamos para tu revisión.
        active_with_sku = Product.objects.filter(active=True).exclude(sku__isnull=True).exclude(sku__exact="")
        missing = active_with_sku.exclude(sku__in=list(seen_skus)).values_list("sku", flat=True)[:200]
        report["not_seen_active"] = list(missing)

        msg = (f"{'ACTUALIZACIÓN' if update_only else 'IMPORTACIÓN'} OK — "
               f"importados {report['imported']}, actualizados {report['updated']}, omitidos {report['skipped']}, "
               f"USD a revisar {len(report['usd_to_review'])}, no encontrados {len(report['not_found'])}, "
               f"activos no vistos {len(report['not_seen_active'])}.")

    return render(request, "shop/owner/import_pdf.html", {"msg": msg, "report": report, "update_only": update_only})

def staff_required(view):
    return user_passes_test(lambda u: u.is_active and u.is_staff, login_url="/admin/login/")(view)


@require_GET
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
            "thumb": thumb,               # <--- CAMPO CLAVE
        })
    return JsonResponse({"results": results})


# ---------- UI nueva ----------
@login_required(login_url="/admin/login/")
@staff_required
def owner_stock_intake_ui(request):
    """
    Pantalla nueva guiada de ingresos:
    - búsqueda por SKU/producto con sugerencias + imagen
    - selección de variante con stock actual
    - cantidad, costo y notas
    """
    return render(request, "shop/owner/stock_intake_ui.html")

# ---------- APIs ----------
@login_required(login_url="/admin/login/")
@staff_required


@login_required(login_url="/admin/login/")
@staff_required


@login_required(login_url="/admin/login/")
@staff_required
@require_POST


def stock_entry_ui(request):
    q = (request.GET.get("q") or "").strip()
    qs = Product.objects.all()
    if q:
        qs = qs.filter(sku__icontains=q)  # o por nombre interno, etc.
    products = qs.prefetch_related("images", "variants")  # ← IMPORTANTE
    return render(request, "shop/stock_entry_ui.html", {"products": products, "q": q})

def _parse_decimal(s: str) -> Decimal:
    """
    Acepta "1.234,56", "1234.56", "$ 1.234,56", "U$S 5", etc.
    """
    if s is None:
        raise InvalidOperation("empty")
    s = str(s).strip()
    s = re.sub(r"[^\d,.\-]", "", s)  # quita símbolos
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return Decimal(s)

@csrf_protect
@require_POST
@transaction.atomic
def owner_stock_intake_api(request):
    """
    POST JSON esperado:
    {
      "product_id": <int>,
      "variant_id": <int|null>,
      "qty": <int>,
      "unit_cost": <str|number>,  # precio de compra (solo bitácora)
      "note": <str>
    }
    Efectos:
      - Suma qty al stock de Variant
      - Crea StockEntry con unit_cost (bitácora)
      - NO toca base_price del Product
    """
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("JSON inválido")

    product_id = data.get("product_id")
    variant_id = data.get("variant_id")
    qty        = data.get("qty")
    unit_cost  = data.get("unit_cost", "0")
    note       = (data.get("note") or "").strip()

    if not product_id:
        return HttpResponseBadRequest("Falta product_id")

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

    # admitir "null"/"" como None
    if variant_id in (None, "", "null"):
        variant = product.variants.filter(active=True).order_by("id").first()
        if not variant:
            variant = Variant.objects.create(product=product, color="", size="", stock=0, active=True)
    else:
        variant = get_object_or_404(Variant, pk=variant_id, product=product, active=True)

    # actualizar stock
    variant.stock = (variant.stock or 0) + qty
    variant.save(update_fields=["stock"])

    # bitácora
    entry = StockEntry.objects.create(
        date=timezone.localdate(),
        product=product,
        variant=variant,
        quantity=qty,
        unit_cost=unit_cost_dec,  # << guarda el PRECIO DE COMPRA
        note=note,
        source_name=product.sku or product.public_name
    )

    return JsonResponse({
        "ok": True,
        "entry_id": entry.id,
        "new_stock": variant.stock,
        "unit_cost": f"{unit_cost_dec:.2f}",
    })


@require_GET
def owner_product_variants_api(request, pk):
    p = Product.objects.prefetch_related("variants").get(pk=pk)
    variants = []
    for v in p.variants.all().order_by("color", "size", "id"):
        label = " ".join([x for x in [v.color, v.size] if x]) or "Única"
        variants.append({
            "id": v.id,
            "label": label,
            "stock": v.stock,
            "price": float(v.price or 0),
        })
    return JsonResponse({"variants": variants})

PLACEHOLDER = "https://via.placeholder.com/64?text=%E2%80%94"

@require_POST
@transaction.atomic
def owner_product_create_api(request):
    """
    Crea un producto y (opcional) una variante inicial.
    JSON esperado:
    {
      "sku": "SKU-001",            # requerido
      "public_name": "Taza X",     # si no viene, se usa el sku
      "description": "texto...",   # opcional
      "base_price": "1234,56",     # opcional (default 0)
      "active": true,              # opcional (default true)
      "variant": {                 # opcional
        "color": "Rojo",
        "size": "400cc",
        "sku": "SKU-001-ROJO",     # opcional
        "stock": 5,                # opcional (si viene, se suma al stock)
        "active": true,            # opcional
        "price_override": "0"      # opcional
      }
    }
    Devuelve { ok, product: {...}, variant: {...?} }
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

    base_price_raw = data.get("base_price", "0")
    try:
        base_price = _parse_decimal(base_price_raw)
    except InvalidOperation:
        return HttpResponseBadRequest("base_price inválido")

    # Crear producto
    product = Product.objects.create(
        sku=sku,
        public_name=public_name,
        description=description,
        base_price=base_price,
        active=active,
    )

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
        },
        "variant": variant_payload
    })

def _product_thumb_or_placeholder(product: Product) -> str:
    img = product.images.order_by("order").first()
    if img:
        try:
            return img.image.url
        except Exception:
            pass
    # si no hay imagen de producto, probá con alguna variante con imagen
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
    # si tiene coma y punto, asumimos '1.234,56' -> quitamos miles y usamos coma como decimal
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return Decimal(s)

# ---------- API: búsqueda de productos ----------

@require_GET
def owner_api_search_products(request):
    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"results": []})

    qs = (Product.objects.filter(active=True)
          .filter(Q(public_name__icontains=q) |
                  Q(sku__icontains=q) |
                  Q(description__icontains=q))
          .prefetch_related(Prefetch("images", queryset=ProductImage.objects.order_by("order")))
          .order_by("public_name")[:20])

    results = []
    for p in qs:
        thumb = _product_thumb_or_placeholder(p)
        results.append({
            "id": p.id,
            "sku": p.sku,
            "public_name": p.public_name,
            "base_price": f"{p.base_price:.2f}",
            "thumb": thumb or PLACEHOLDER,
        })
    return JsonResponse({"results": results})

# ---------- API: variantes de un producto ----------

@require_GET
def owner_api_product_variants(request, product_id: int):
    p = get_object_or_404(Product, pk=product_id, active=True)
    vs = (p.variants
            .filter(active=True)
            .order_by("color", "size")
            .values("id", "color", "size", "stock"))
    variants = []
    for v in vs:
        label = (f"{v['color']} {v['size']}".strip() or "Única")
        variants.append({
            "id": v["id"],         # se usa en el payload
            "label": label,
            "stock": v["stock"] or 0,
        })
    return JsonResponse({"variants": variants})

# ---------- API: guardar ingreso de stock (BITÁCORA) ----------

@require_POST
@transaction.atomic
def owner_api_stock_intake(request):
    """
    Espera JSON:
      {
        "product_id": <int>,
        "variant_id": <int|null>,
        "qty": <int>,
        "unit_cost": <str|number>,   # PRECIO DE COMPRA (solo bitácora)
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
    qty        = data.get("qty")
    unit_cost  = data.get("unit_cost", "0")
    note       = (data.get("note") or "").strip()

    # Validaciones básicas
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

    # Precio de compra (bitácora) -> Decimal
    try:
        unit_cost_dec = _parse_decimal(unit_cost)
    except InvalidOperation:
        return HttpResponseBadRequest("unit_cost inválido")

    # Entidades
    product = get_object_or_404(Product, pk=product_id, active=True)

    variant = None
    if variant_id is not None:
        variant = get_object_or_404(Variant, pk=variant_id, product=product, active=True)
    else:
        # si no viene variante, intentá usar la variante “única” (color/size vacíos)
        variant = product.variants.filter(active=True).order_by("id").first()
        if not variant:
            # Creamos una variante por defecto si no existía
            variant = Variant.objects.create(product=product, color="", size="", stock=0, active=True)

    # Actualiza stock de la variante
    variant.stock = (variant.stock or 0) + qty
    variant.save(update_fields=["stock"])

    # Registra en StockEntry (bitácora)
    entry = StockEntry.objects.create(
        date=timezone.localdate(),
        product=product,
        variant=variant,
        quantity=qty,
        unit_cost=unit_cost_dec,    # <-- guarda PRECIO DE COMPRA
        note=note,
        source_name=product.sku or product.public_name
    )

    return JsonResponse({
        "ok": True,
        "entry_id": entry.id,
        "new_stock": variant.stock,
        "unit_cost": f"{unit_cost_dec:.2f}",
    })


# ---------- Vista de bitácora (opcional, para ver últimos movimientos) ----------

def owner_bitacora(request):
    entries = (StockEntry.objects
               .select_related("product", "variant")
               .order_by("-date", "-id")[:300])
    return render(request, "shop/owner/bitacora.html", {"entries": entries})