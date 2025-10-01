def cart_badge(request):
    cart = request.session.get("cart", {})
    # cart puede ser {variant_id: {"qty": n, ...}} o {product_id: {...}} según tu implementación
    total = 0
    try:
        for k, item in cart.items():
            q = item.get("qty", 0)
            total += int(q) if q is not None else 0
    except Exception:
        total = 0
    return {"cart_count": total}
