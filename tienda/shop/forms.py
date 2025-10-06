from django import forms
from django.forms import inlineformset_factory
from .models import Product, Variant, ProductImage

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = "__all__"
        exclude = ("created_at",)
        widgets = {
            "sku": forms.TextInput(attrs={"class": "form-control"}),
            "public_name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "base_price": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

class VariantForm(forms.ModelForm):
    class Meta:
        model = Variant
        fields = ("color", "size", "sku", "image", "stock", "active", "price_override")
        widgets = {
            "color": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "size": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "sku": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "image": forms.ClearableFileInput(attrs={"class": "form-control form-control-sm"}),
            "stock": forms.NumberInput(attrs={"class": "form-control form-control-sm"}),
            "price_override": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01"}),
            "active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

class ProductImageForm(forms.ModelForm):
    class Meta:
        model = ProductImage
        fields = ("image", "alt_text", "order")
        widgets = {
            "image": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "alt_text": forms.TextInput(attrs={"class": "form-control"}),
            "order": forms.NumberInput(attrs={"class": "form-control"}),
        }

VariantFormSet = inlineformset_factory(Product, Variant, form=VariantForm, extra=1, can_delete=True)
ProductImageFormSet = inlineformset_factory(Product, ProductImage, form=ProductImageForm, extra=1, can_delete=True)

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = "__all__"      # o: ("sku","public_name","description","base_price","tech","active",...)
        exclude = ("created_at",)