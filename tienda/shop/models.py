from django.db import models
from django.conf import settings
from decimal import Decimal
from django.utils import timezone

class Category(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "shop_category"
        ordering = ["name"]

    def __str__(self):
        return self.name

class Product(models.Model):
    TECH_CHOICES = [
        ('SUB', 'Sublimación'),
        ('LAS', 'Grabado láser'),
        ('3D',  'Impresión 3D'),
        ('OTR', 'Otro'),
    ]

    sku = models.CharField(max_length=100, unique=True)
    public_name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)

    # Filtro por técnica (ya lo tenías)
    tech = models.CharField(max_length=3, choices=TECH_CHOICES, default='OTR', db_index=True)

    # 🔹 NUEVO: Categoría (opcional)
    category = models.ForeignKey(
        Category, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="products"
    )

    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def catalog_image_url(self):
        """
        Miniatura para catálogo:
        - Prioriza imagen de VARIANTE activa (si tiene), si no
        - Usa primera imagen del PRODUCTO, si no
        - Devuelve string vacío (para que el template muestre placeholder)
        """
        v = (self.variants
                 .filter(active=True, image__isnull=False)
                 .exclude(image="")
                 .first())
        if v and getattr(v, "image", None):
            try:
                return v.image.url
            except ValueError:
                pass
        img = self.images.order_by("order").first()
        if img:
            try:
                return img.image.url
            except ValueError:
                pass
        return ""

    def __str__(self):
        # 👇 corregido: era self.sk → debe ser self.sku
        return self.public_name or self.skuu


class ProductImage(models.Model):
    product = models.ForeignKey(Product, related_name="images", on_delete=models.CASCADE)
    image = models.ImageField(upload_to="products/")
    alt_text = models.CharField(max_length=255, blank=True, null=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"Imagen de {self.product.public_name}"


class Variant(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    sku = models.CharField(max_length=64, unique=True, null=True, blank=True)
    color = models.CharField(max_length=64, blank=True, default='')
    size  = models.CharField(max_length=32, blank=True, default='')
    price_override = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    stock = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    image = models.ImageField(upload_to="variants/", null=True, blank=True)  # opcional

    class Meta:
        db_table = 'shop_variant'
        unique_together = [('product','color','size')]

    @property
    def price(self):
        return self.price_override if self.price_override is not None else self.product.base_price

    def __str__(self):
        attrs = ' '.join([self.color, self.size]).strip()
        return f"{self.product.public_name} ({attrs})" if attrs else self.product.public_name


class Order(models.Model):
    created_at=models.DateTimeField(auto_now_add=True)
    email=models.EmailField(); full_name=models.CharField(max_length=160)
    status=models.CharField(max_length=20,default='pending')
    total=models.DecimalField(max_digits=12,decimal_places=2,default=0)
    mp_preference_id=models.CharField(max_length=80,blank=True,default='')
    mp_payment_id=models.CharField(max_length=80,blank=True,default='')
    class Meta: db_table='shop_order'


class OrderItem(models.Model):
    order=models.ForeignKey(Order,on_delete=models.CASCADE,related_name='items')
    variant=models.ForeignKey(Variant,on_delete=models.PROTECT)
    quantity=models.PositiveIntegerField()
    unit_price=models.DecimalField(max_digits=12,decimal_places=2)
    class Meta: db_table='shop_order_item'
    @property
    def subtotal(self): return self.quantity*self.unit_price


class StockEntry(models.Model):
    created_at=models.DateTimeField(auto_now_add=True)
    date=models.DateField()
    product=models.ForeignKey(Product,on_delete=models.PROTECT,related_name='stock_entries')
    variant=models.ForeignKey(Variant,on_delete=models.PROTECT,null=True,blank=True,related_name='stock_entries')
    quantity=models.IntegerField()
    unit_cost=models.DecimalField(max_digits=12,decimal_places=2,default=Decimal('0.00'))
    note=models.CharField(max_length=255,blank=True,default='')
    source_name=models.CharField(max_length=255,blank=True,default='')
    class Meta: db_table='shop_stock_entry'; ordering=['-date','-id']

class PriceChangeBatch(models.Model):
        created_at = models.DateTimeField(auto_now_add=True)
        user = models.ForeignKey(
            settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
        )
        params = models.JSONField(default=dict)   # porcentajes, modo de redondeo, etc.
        note = models.CharField(max_length=255, blank=True, default="")
        updated_count = models.IntegerField(default=0)
        is_reverted = models.BooleanField(default=False)

        class Meta:
            ordering = ["-created_at"]

        def __str__(self) -> str:
            return f"Lote #{self.id} ({self.created_at:%Y-%m-%d %H:%M})"

class PriceChangeItem(models.Model):
    batch = models.ForeignKey(PriceChangeBatch, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("shop.Product", on_delete=models.CASCADE)
    old_price = models.DecimalField(max_digits=12, decimal_places=2)
    new_price = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self) -> str:
        return f"{self.product_id}: {self.old_price} -> {self.new_price}"

class Promotion(models.Model):
    name = models.CharField(max_length=120)
    percent = models.PositiveIntegerField(help_text="Porcentaje 1..90")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    # Si es NULL => aplica a todas las técnicas
    tech_filter = models.CharField(
        max_length=3,
        choices=Product.TECH_CHOICES,
        blank=True, null=True
    )
    # Si se elige una lista de productos, tiene prioridad sobre tech_filter
    products = models.ManyToManyField(Product, blank=True, related_name="promotions")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-active", "-end_at", "name")

    def __str__(self):
        return f"{self.name} ({self.percent}% hasta {self.end_at:%Y-%m-%d %H:%M})"

    def is_active_now(self):
        now = timezone.now()
        return self.active and self.start_at <= now <= self.end_at

    def applies_to(self, product: Product) -> bool:
        if not self.is_active_now():
            return False
        # Si hay lista de productos, manda eso
        if self.products.exists():
            return self.products.filter(pk=product.pk).exists()
        # Si hay filtro de técnica, debe coincidir
        if self.tech_filter:
            return product.tech == self.tech_filter
        # Sin filtro => aplica a todos
        return True


class Coupon(models.Model):
    code = models.CharField(max_length=64, unique=True)
    percent = models.PositiveIntegerField(help_text="Porcentaje 1..90")
    start_at = models.DateTimeField(blank=True, null=True)
    end_at = models.DateTimeField(blank=True, null=True)
    tech_filter = models.CharField(
        max_length=3,
        choices=Product.TECH_CHOICES,
        blank=True, null=True
    )
    active = models.BooleanField(default=True)
    usage_limit = models.PositiveIntegerField(blank=True, null=True)
    used_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-active", "code")

    def __str__(self):
        return f"{self.code} ({self.percent}%)"

    def is_active_now(self):
        now = timezone.now()
        if not self.active:
            return False
        if self.start_at and now < self.start_at:
            return False
        if self.end_at and now > self.end_at:
            return False
        if self.usage_limit is not None and self.used_count >= self.usage_limit:
            return False
        return True

    def applies_to(self, product: Product) -> bool:
        if not self.is_active_now():
            return False
        if self.tech_filter:
            return product.tech == self.tech_filter
        return True