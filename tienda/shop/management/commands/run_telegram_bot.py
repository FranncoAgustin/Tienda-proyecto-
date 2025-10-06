# shop/management/commands/run_telegram_bot.py
import os
from django.conf import settings
from django.core.management.base import BaseCommand
from asgiref.sync import sync_to_async
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from shop.models import Product, Variant


# =======================
# Helpers síncronos (ORM)
# =======================

def _stock_lookup_sync(key: str) -> dict | None:
    """
    Busca primero Variante por SKU exacto; si no, Producto por SKU o nombre.
    Devuelve solo tipos primitivos (dict) para cruzar el límite async/sync.
    """
    # 1) Variante por SKU
    v = (
        Variant.objects
        .filter(sku__iexact=key, active=True, product__active=True)
        .select_related("product")
        .first()
    )
    if v:
        return {
            "type": "variant",
            "product_sku": v.product.sku,
            "product_name": v.product.public_name,
            "variant_sku": v.sku or "",
            "label": " ".join(x for x in [v.color, v.size] if x) or "Única",
            "stock": int(v.stock or 0),
        }

    # 2) Producto por SKU o nombre
    p = (
        Product.objects
        .filter(active=True)
        .filter(Q(sku__iexact=key) | Q(public_name__iexact=key))
        .first()
    )
    if p:
        total = (
            p.variants.filter(active=True)
            .aggregate(t=Coalesce(Sum("stock"), 0))["t"]
            or 0
        )
        # pequeño detalle: listar hasta 6 variantes con su stock
        variants = list(
            p.variants.filter(active=True)
            .order_by("color", "size")
            .values("color", "size", "stock")[:6]
        )
        for it in variants:
            it["label"] = " ".join(x for x in [it["color"], it["size"]] if x) or "Única"
            it["stock"] = int(it["stock"] or 0)
        return {
            "type": "product",
            "product_sku": p.sku,
            "product_name": p.public_name,
            "total_stock": int(total),
            "variants": variants,
        }

    return None


# versión asíncrona para usar en handlers
stock_lookup = sync_to_async(_stock_lookup_sync)


# =======================
# Handlers asíncronos
# =======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hola! 👋\n"
        "Comandos disponibles:\n"
        "• /stock <SKU o nombre exacto> — muestra stock de una variante o del producto."
    )


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Unir args para permitir espacios en clave/nombre
    key = " ".join(context.args).strip() if context.args else ""
    if not key:
        await update.message.reply_text("Uso: /stock <SKU o nombre>")
        return

    try:
        data = await stock_lookup(key)  # <<--- consulta ORM envuelta
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error consultando: {e}")
        return

    if not data:
        await update.message.reply_text("No encontré ese producto/variante.")
        return

    if data["type"] == "variant":
        txt = (
            f"📦 *VARIANTE*\n"
            f"Producto: {data['product_name']} (SKU {data['product_sku']})\n"
            f"Variante: {data['label']} (SKU var: {data['variant_sku'] or '—'})\n"
            f"Stock: *{data['stock']}*"
        )
    else:
        # product
        lines = [
            "🧳 *PRODUCTO*",
            f"Nombre: {data['product_name']}",
            f"SKU: {data['product_sku']}",
            f"Stock total: *{data['total_stock']}*",
        ]
        if data["variants"]:
            lines.append("\nVariantes (máx 6):")
            for it in data["variants"]:
                lines.append(f"• {it['label']}: {it['stock']}")
        txt = "\n".join(lines)

    await update.message.reply_text(txt, parse_mode="Markdown")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Evita que PTB muestre "No error handlers are registered..."
    try:
        raise context.error
    except Exception as e:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(f"⚠️ Ocurrió un error: {e}")


# =======================
# Comando de Django
# =======================

class Command(BaseCommand):
    help = "Levanta el bot de Telegram"

    def handle(self, *args, **options):
        token = getattr(settings, "TELEGRAM_BOT_TOKEN", None) or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise SystemExit("Falta TELEGRAM_BOT_TOKEN en settings o variables de entorno.")

        app = (
            ApplicationBuilder()
            .token(token)
            .build()
        )

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("stock", stock))
        app.add_error_handler(error_handler)

        self.stdout.write(self.style.SUCCESS("Bot corriendo. Ctrl+C para salir."))
        app.run_polling(allowed_updates=Update.ALL_TYPES)
