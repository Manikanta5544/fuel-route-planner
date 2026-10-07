import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


DEBUG = _env("DJANGO_DEBUG") == "1"
SECRET_KEY = _env("DJANGO_SECRET_KEY") or secrets.token_urlsafe(50)  # unused: no sessions/CSRF
ALLOWED_HOSTS = _env("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")

INSTALLED_APPS = ["api"]
MIDDLEWARE = ["django.middleware.gzip.GZipMiddleware", "api.middleware.RequestMiddleware"]
ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
DATABASES = {}
USE_TZ = True
DATA_UPLOAD_MAX_MEMORY_SIZE = 4096

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": _env("LOG_LEVEL", "INFO")},
}

# --- application settings (all env-driven) ---
BUILD_DIR = Path(_env("BUILD_DIR", str(BASE_DIR / "data" / "build")))
ROUTING_PROVIDER = _env("ROUTING_PROVIDER", "auto")  # auto | ors | osrm
ORS_BASE_URL = _env("ORS_BASE_URL", "https://api.openrouteservice.org")
ORS_API_KEY = _env("ORS_API_KEY")
OSRM_BASE_URL = _env("OSRM_BASE_URL", "https://router.project-osrm.org")
REDIS_URL = _env("REDIS_URL")
START_TANK_BILLING = _env("START_TANK_BILLING", "reference")  # reference | excluded
STATION_PRICE_RULE = _env("STATION_PRICE_RULE", "min")  # min | median | mean (ETL)
MAX_ROUTING_CALLS = int(_env("MAX_ROUTING_CALLS", "3"))
RATE_LIMIT_PER_MIN = int(_env("RATE_LIMIT_PER_MIN", "120"))
RESERVE_MILES = float(_env("RESERVE_MILES", "0"))
FREE_OFFSET_MILES = float(_env("FREE_OFFSET_MILES", "5"))
G_REF_GALLONS = float(_env("G_REF_GALLONS", "25"))
