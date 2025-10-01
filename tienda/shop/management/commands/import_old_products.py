from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, models
from shop.models import Product, Variant, ProductAlias
import os, sqlite3, re
from decimal import Decimal
def guess_color_size(options_text):
    if not options_text: return [('', '')]
    opts=[]; raw=re.split(r'[;|]', options_text)
    for chunk in raw:
        chunk=chunk.strip()
        if not chunk: continue
        parts=re.split(r'[ ,]+', chunk)
        if len(parts)==1: opts.append((parts[0],''))
        else: opts.append((parts[0],' '.join(parts[1:])))
    return opts or [('', '')]
class Command(BaseCommand):
    help='Importa productos desde DB vieja, con alias, priorizando SKU.'
    def add_arguments(self, parser):
        parser.add_argument('--old-db', required=True)
        parser.add_argument('--price-factor', type=float, default=1.0)
    def handle(self,*a,**o):
        old=o['old_db']
        if not os.path.isfile(old): raise CommandError(f'No existe: {old}')
        con=sqlite3.connect(old); cur=con.cursor()
        cols=[c[1] for c in cur.execute('PRAGMA table_info(products)').fetchall()]
        name_col='product' if 'product' in cols else ('name' if 'name' in cols else None)
        if not name_col: raise CommandError("No encuentro 'product' ni 'name'.")
        has_img='image_path' in cols; has_opts='options' in cols; has_price='price' in cols; has_sku='sku' in cols
        rows=cur.execute(f"SELECT {name_col}{', price' if has_price else ''}{', image_path' if has_img else ''}{', options' if has_opts else ''}{', sku' if has_sku else ''} FROM products").fetchall()
        imported=0; updated=0
        with transaction.atomic():
            for r in rows:
                i=0; old_name=r[i]; i+=1
                price=Decimal(str(r[i])) if has_price else Decimal('0'); i+=1 if has_price else 0
                image=r[i] if has_img else None; i+=1 if has_img else 0
                options=r[i] if has_opts else ''; i+=1 if has_opts else 0
                sku=r[i] if has_sku else None
                if not old_name: continue
                public_name=old_name; internal_name=sku or old_name
                base_price=price*Decimal(str(o['price_factor'])) if has_price else Decimal('0')
                p=None
                if sku: p=Product.objects.filter(sku__iexact=sku).first()
                if not p:
                    alias=ProductAlias.objects.filter(source_name__iexact=internal_name).select_related('product').first()
                    p=alias.product if alias else Product.objects.filter(internal_name__iexact=internal_name).first()
                if p:
                    if sku and not p.sku: p.sku=sku
                    if public_name: p.public_name=public_name
                    if base_price: p.base_price=base_price
                    p.save(); updated+=1
                else:
                    p=Product.objects.create(sku=sku or None, internal_name=internal_name, public_name=public_name, base_price=base_price, active=True)
                    ProductAlias.objects.get_or_create(product=p, source_name=internal_name); imported+=1
                for (color,size) in guess_color_size(options or ''):
                    Variant.objects.get_or_create(product=p, color=color[:64], size=size[:32], defaults={'stock':0,'active':True})
        self.stdout.write(self.style.SUCCESS(f'Importados {imported}, actualizados {updated}.'))
