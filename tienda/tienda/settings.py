# settings.py
from pathlib import Path
import os, sys

def _paths():
    if hasattr(sys, "_MEIPASS"):  # ejecutable PyInstaller
        APP_DIR = Path(sys._MEIPASS) / "app"              # recursos empaquetados (lectura)
        RUNTIME_DIR = Path(sys.executable).parent         # junto al .exe (lectura/escritura)
    else:  # modo desarrollo
        APP_DIR = Path(__file__).resolve().parent.parent
        RUNTIME_DIR = APP_DIR
    return APP_DIR, RUNTIME_DIR

APP_DIR, RUNTIME_DIR = _paths()

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin","django.contrib.auth","django.contrib.contenttypes",
    "django.contrib.sessions","django.contrib.messages","django.contrib.staticfiles",
    "django.contrib.humanize",
    "shop",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware","django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware","django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware","django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "tienda.urls"
WSGI_APPLICATION = "tienda.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    # Leemos templates del bundle (APP_DIR) y de la app
    "DIRS": [APP_DIR / "templates", APP_DIR / "shop" / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.debug",
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
            # ajustá el nombre si tu CP es distinto:
            "shop.context_processors.cart_badge",
        ],
    },
}]

# ---- Rutas ESCRIBIBLES junto al exe ----
DATA_DIR = RUNTIME_DIR / "data"
MEDIA_ROOT = RUNTIME_DIR / "media"
STATIC_ROOT = RUNTIME_DIR / "staticfiles"   # si usás collectstatic

# Crear si no existen (útil en modo portable)
for d in (DATA_DIR, MEDIA_ROOT, STATIC_ROOT):
    d.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DATA_DIR / "tienda.db"),
    }
}

# Localización
LANGUAGE_CODE = "es-ar"
TIME_ZONE = "America/Argentina/Buenos_Aires"
USE_I18N = True
USE_TZ = True

# Static y media
STATIC_URL = "/static/"
# Siempre apuntamos a los estáticos que empaquetamos bajo APP_DIR
STATICFILES_DIRS = [APP_DIR / "static", APP_DIR / "shop" / "static"]
# STATIC_ROOT lo dejás para uso opcional de collectstatic si querés
STATIC_ROOT = RUNTIME_DIR / "staticfiles"

MEDIA_URL = "/media/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CART_SESSION_KEY = "cart"
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "http://127.0.0.1:8000")
LOGIN_URL = "/admin/login/"
