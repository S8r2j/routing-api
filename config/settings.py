from pathlib import Path
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config(
    'SECRET_KEY',
    default='django-insecure-dev-key-change-in-production-abc123xyz'
)

DEBUG = config('DEBUG', default=True, cast=bool)

ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
    'corsheaders',
    'apps.routing',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'apps.routing.middleware.RequestLoggingMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'config.urls'

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Database — SQLite by default, PostgreSQL via DATABASE_URL
_database_url = config('DATABASE_URL', default='')
if _database_url:
    import re
    _m = re.match(
        r'postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/(.+)',
        _database_url,
    )
    if _m:
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'USER': _m.group(1),
                'PASSWORD': _m.group(2),
                'HOST': _m.group(3),
                'PORT': _m.group(4) or '5432',
                'NAME': _m.group(5),
            }
        }
    else:
        raise ValueError(f"Could not parse DATABASE_URL: {_database_url}")
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

_CACHE_DIR = BASE_DIR / 'cache'
_CACHE_DIR.mkdir(exist_ok=True)

_REDIS_URL = config('REDIS_URL', default='')

if _REDIS_URL:
    # Production: Redis as a single shared cache for both route caching and
    # throttle counters. All Uvicorn workers share the same Redis instance so
    # throttle limits are enforced globally across the process pool.
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': _REDIS_URL,
            'TIMEOUT': 3600,
        },
        'throttle': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': _REDIS_URL,
            'TIMEOUT': 3600,
        },
    }
else:
    # Development / no-Redis fallback:
    # - Route cache uses LocMemCache (fast, per-worker in-memory).
    # - Throttle cache uses FileBasedCache (shared across all Uvicorn workers
    #   via the filesystem) so rate limits are enforced globally even with
    #   multiple workers. LocMemCache would be per-process, allowing each worker
    #   to grant its own 60 req/min quota — effectively multiplying the limit
    #   by the worker count.
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'TIMEOUT': 3600,
        },
        'throttle': {
            'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
            'LOCATION': str(_CACHE_DIR / 'throttle'),
            'TIMEOUT': 3600,
        },
    }

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': [],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        # 60 requests per minute per IP for anonymous (public) access.
        # Prevents a single client from hammering OSRM or the planner.
        # Adjust via env var THROTTLE_RATE_ANON if needed.
        'anon': config('THROTTLE_RATE_ANON', default='60/minute'),
    },
    # Use the dedicated throttle cache so counters are shared across all
    # Uvicorn worker processes. Without this, DRF falls back to 'default'
    # (LocMemCache), which is per-process — each worker would grant its own
    # independent quota, multiplying the effective rate limit by worker count.
    'DEFAULT_THROTTLE_CACHE': 'throttle',
}

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Allow any origin by default (public read-only API).
# In production, restrict this to known client domains via CORS_ALLOWED_ORIGINS.
CORS_ALLOW_ALL_ORIGINS = config('CORS_ALLOW_ALL_ORIGINS', default=True, cast=bool)
CORS_ALLOWED_ORIGINS_STR = config('CORS_ALLOWED_ORIGINS', default='')
if CORS_ALLOWED_ORIGINS_STR:
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = [o.strip() for o in CORS_ALLOWED_ORIGINS_STR.split(',') if o.strip()]
CORS_ALLOW_METHODS = ['GET', 'OPTIONS']
CORS_ALLOW_HEADERS = ['accept', 'content-type']

# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True   # X-Content-Type-Options: nosniff
X_FRAME_OPTIONS = 'DENY'             # X-Frame-Options: DENY
SECURE_REFERRER_POLICY = 'no-referrer'

# Request body / field limits — guard against malformed large POST bodies
# (this API is GET-only, but belt-and-suspenders).
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 16   # 16 KB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 10

_LOG_DIR = BASE_DIR / 'logs'
_LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'routing_file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': _LOG_DIR / 'app.log',
            'maxBytes': 10 * 1024 * 1024,  # 10 MB
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
    },
    'loggers': {
        'routing': {
            'handlers': ['console', 'routing_file'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'routing.access': {
            'handlers': ['console', 'routing_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['console'],
            'level': 'WARNING',
        },
        'django.security': {
            'handlers': ['console', 'routing_file'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

OSRM_BASE_URL = config('OSRM_BASE_URL', default='http://router.project-osrm.org')
OSRM_TIMEOUT_SECONDS = config('OSRM_TIMEOUT_SECONDS', default=10, cast=int)
ROUTE_CACHE_TIMEOUT = config('ROUTE_CACHE_TIMEOUT', default=3600, cast=int)

# ---------------------------------------------------------------------------
# Vehicle parameters
# ---------------------------------------------------------------------------
# Maximum range on a full tank in miles. The tank capacity in gallons is
# derived as VEHICLE_TANK_RANGE_MILES / VEHICLE_MPG.
VEHICLE_TANK_RANGE_MILES = config('VEHICLE_TANK_RANGE_MILES', default=500.0, cast=float)
VEHICLE_MPG = config('VEHICLE_MPG', default=10.0, cast=float)
