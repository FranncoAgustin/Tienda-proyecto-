from django.db import models
from decimal import Decimal
class Product(models.Model):
    sku = models.CharField(max_length=100, unique=True)
    public_name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # sacamos image único, usamos relación
    # image = models.ImageField(upload_to="products/", blank=True, null=True)

    @property
    def catalog_image_url(self):
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

    #def __str__(self):
        return self.public_name or self.sku
    
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

    # 👇 NUEVO (foto opcional por variante)
    image = models.ImageField(upload_to="variants/", null=True, blank=True)

    class Meta:
        db_table = 'shop_variant'
        unique_together = [('product','color','size')]

    @property
    def price(self):
        return self.price_override if self.price_override is not None else self.product.base_price

    def __str__(self):
        attrs = ' '.join([self.color, self.size]).strip()
        return f"{self.product.public_name} ({attrs})" if attrs else self.product.public_name
    
    def in_stock(self):
        return (self.stock or 0) > 0
    

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

