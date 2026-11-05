import os
from pathlib import Path
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

# Build paths
BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = 'core_core'  # Change this later when you create your core app module

# ENVIRONMENT-SPECIFIC SETTINGS (Pulled from .env)
SECRET_KEY = os.getenv('SECRET_KEY')
CAS_COMMON_API_KEY = os.getenv('CAS_COMMON_API_KEY', default='')
COMPTOX_API_KEY = os.getenv('CTX_X_API_KEY', default='')
CHEMSPIDER_API_KEY = os.getenv('CHEMSPIDER_API_KEY', default='')
DEBUG = os.getenv('DEBUG') == 'True'
BASE_APP_URL = os.getenv('BASE_APP_URL')
BASE_NAME = os.getenv('BASE_NAME')
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL')
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h]

# Paths derived from settings
LOGIN_URL = f'/{BASE_APP_URL}/accounts/login'
IMAGE_PATH = BASE_DIR / 'static' / 'img'
MEDIA_ROOT = Path(os.getenv('MEDIA_ROOT', str(BASE_DIR / 'media'))
)
STRUCTURE_CURATION_ROOT = Path(os.getenv(
    'STRUCTURE_CURATION_ROOT',
    str(BASE_DIR / '.private' / 'structure-curation'),
))
# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'apps.registry.apps.RegistryConfig',
]

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
]

ROOT_URLCONF = 'config.urls'
X_FRAME_OPTIONS = 'SAMEORIGIN'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database settings (PostgreSQL)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql_psycopg2',
        'NAME': os.getenv('DB_NAME'),
        'USER': os.getenv('DB_USER'),
        'PASSWORD': os.getenv('DB_PASS'),
        'HOST': os.getenv('DB_HOST'),
        'PORT': os.getenv('DB_PORT'),
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'

CELERY_BROKER_URL = os.getenv(
    "CELERY_BROKER_URL",
    "redis://127.0.0.1:6379/0",
)
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_IGNORE_RESULT = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

DATA_GATHERING_BATCH_PAUSE_SECONDS = 30
CELERY_TASK_ROUTES = {
    'registry.process_data_gathering_run': {'queue': 'registry'},
    'registry.collect_structure_curation_preview': {'queue': 'registry'},
    'registry.process_source_import': {'queue': 'registry'},
    'registry.stage_source_import': {'queue': 'registry'},
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'loggers': {
        'chembl_webresource_client': {
            'level': 'WARNING',
        },
    },
}

# Internationalization
LANGUAGE_CODE = 'en-ca'
TIME_ZONE = 'America/Toronto'
USE_I18N = True
USE_TZ = True

# Email settings
EMAIL_BACKEND = 'django_sendmail_backend.backends.EmailBackend'
SENDMAIL_BINARY = os.getenv('SENDMAIL_BINARY')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL')

# Static files settings (handled via Whitenoise under sub-path)
STATIC_URL = f'/{BASE_APP_URL}/static/'
STATIC_ROOT = os.getenv('STATIC_ROOT', os.path.join(BASE_DIR, 'staticfiles'))
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static'),]

WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True

# Session and CSRF settings
CSRF_COOKIE_PATH = f'/{BASE_APP_URL}'
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o]
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_PATH = f'/{BASE_APP_URL}'
SESSION_COOKIE_NAME = "edit_session"
SESSION_SAVE_EVERY_REQUEST = True
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024