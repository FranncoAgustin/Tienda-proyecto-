# shop/admin.py
from django.contrib import admin
from .models import Product, Variant, Order, OrderItem, StockEntry, ProductImage, PriceChangeBatch, PriceChangeItem

# === Acciones comunes ===
@admin.action(description="Deshabilitar seleccionados")
def make_inactive(modeladmin, request, queryset):
    queryset.update(active=False)

@admin.action(description="Habilitar seleccionados")
def make_active(modeladmin, request, queryset):
    queryset.update(active=True)


# === Inlines ===
class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ("image", "alt_text", "order")
    ordering = ("order",)


# ⬇️ Usamos StackedInline para asegurarnos de ver bien todos los campos
class VariantInline(admin.StackedInline):
    model = Variant
    extra = 1
    # Mostramos SOLO los campos que te interesan en la variante
    fields = ("color", "size", "image", "stock")
    show_change_link = False
    # Si querés que las variantes inactivas no existan, simplemente no las mostramos aquí;
    # el campo 'active' queda oculto y conservará su valor por defecto (True).


# === Product en admin con inlines ===
@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("sku", "public_name", "base_price", "tech", "active")
    list_filter  = ("active", "tech")
    search_fields = ("sku", "public_name")
    list_editable = ("active",)
    actions       = [make_active, make_inactive]
    inlines       = [ProductImageInline, VariantInline]

    # Guardado robusto de inlines (por si algún formset no hace commit directo)
    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in instances:
            obj.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()

class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "active")
    list_editable = ("active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


# === Order/OrderItem/StockEntry ===
class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("variant", "quantity", "unit_price")

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display  = ("id", "email", "full_name", "status", "total", "created_at")
    list_filter   = ("status",)
    search_fields = ("email", "full_name", "mp_preference_id", "mp_payment_id")
    inlines       = [OrderItemInline]

@admin.register(StockEntry)
class StockEntryAdmin(admin.ModelAdmin):
    list_display  = ("date", "product", "variant", "quantity", "unit_cost", "note", "source_name", "created_at")
    list_filter   = ("date",)
    search_fields = ("product__public_name", "variant__color", "source_name", "note")


# === Asegurar que Variant NO tenga sección propia en el admin ===
try:
    admin.site.unregister(Variant)
except admin.sites.NotRegistered:
    pass


class PriceChangeItemInline(admin.TabularInline):
    model = PriceChangeItem
    extra = 0
    readonly_fields = ("product", "old_price", "new_price")

@admin.register(PriceChangeBatch)
class PriceChangeBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "user", "updated_count", "is_reverted", "note")
    inlines = [PriceChangeItemInline]
    readonly_fields = ("created_at", "updated_count", "is_reverted", "params")