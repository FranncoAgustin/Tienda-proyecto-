from django.contrib import admin
from django.urls import path, re_path
from django.conf import settings
from django.conf.urls.static import static
from shop import views

urlpatterns = [
    path('admin/', admin.site.urls),

    # Público
    path('', views.catalog, name='catalog'),
    path("p/<int:pk>/", views.product_detail, name="product_detail_pk"),
    #re_path(r"^products/(?P<sku>(?!\d+$)[\w-]+)/$", views.product_detail, name="product_detail"),
    path("products/<path:sku>/", views.product_detail, name="product_detail"), # por sku
    path('cart/', views.cart_view, name='cart'),
    path('cart/add/', views.cart_add, name='cart_add'),
    path('cart/remove/', views.cart_remove, name='cart_remove'),
    path('checkout/', views.checkout, name='checkout'),

    # Owner
    path('owner/', views.owner_dashboard, name='owner_dashboard'),
    path("owner/products/new/", views.product_manage, name="product_new"),
    path("owner/products/<path:sku>/", views.product_manage, name="product_manage"),
    path('owner/stock_intake/', views.owner_stock_intake, name='owner_stock_intake'),
    path('owner/stock_intake/ui/', views.owner_stock_intake_ui, name='owner_stock_intake_ui'),
    path("api/products/<path:sku>/variants/", views.product_variants_json, name="product_variants_json"),
     path("owner/api/product_create/", views.owner_product_create_api, name="owner_product_create_api"),


    # APIs owner (¡solo estas!)
    path("owner/api/search_products/", views.owner_search_products_api, name="owner_search_products_api"),
    path("owner/api/product/<int:pk>/variants/", views.owner_product_variants_api, name="owner_product_variants_api"),
    path("owner/api/stock_intake/", views.owner_stock_intake_api, name="owner_stock_intake_api"),

    path('owner/bulk_pricing/', views.owner_bulk_pricing, name='owner_bulk_pricing'),
    path('owner/bitacora/', views.owner_bitacora, name='owner_bitacora'),
    path('owner/import_pdf/', views.owner_import_pdf, name='owner_import_pdf'),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

