"""private_mkt will be populated from puppet and placed in this directory"""

from mkt.settings import *
from settings_base import *

import private_mkt

SERVER_EMAIL = 'zmarketplacestage@addons.mozilla.org'

DOMAIN = "payments-alt.allizom.org"
SITE_URL = 'https://%s' % DOMAIN
BROWSERID_AUDIENCES = [SITE_URL]
STATIC_URL = os.getenv('CUSTOM_CDN', 'https://payments-alt-cdn.allizom.org/')
LOCAL_MIRROR_URL = '%s_files' % STATIC_URL
MIRROR_URL = LOCAL_MIRROR_URL

CSP_STATIC_URL = STATIC_URL[:-1]
CSP_IMG_SRC = CSP_IMG_SRC + (CSP_STATIC_URL,)
CSP_SCRIPT_SRC = CSP_SCRIPT_SRC + (CSP_STATIC_URL,)
CSP_STYLE_SRC = CSP_STYLE_SRC + (CSP_STATIC_URL,)

ADDON_ICON_URL = 'img/uploads/addon_icons/%s/%s-%s.png?modified=%s'
PREVIEW_THUMBNAIL_URL = 'img/uploads/previews/thumbs/%s/%d.png?modified=%d'
PREVIEW_FULL_URL = 'img/uploads/previews/full/%s/%d.%s?modified=%d'

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_DOMAIN = ".%s" % DOMAIN

# paths for uploaded extensions
USERPICS_URL = STATIC_URL + 'img/uploads/userpics/%s/%s/%s.png?modified=%d'

MEDIA_URL = STATIC_URL + 'media/'

CACHE_PREFIX = 'stage.mkt.%s' % CACHE_PREFIX
CACHE_MIDDLEWARE_KEY_PREFIX = CACHE_PREFIX

CACHES['default']['KEY_PREFIX'] = CACHE_PREFIX


LOG_LEVEL = logging.DEBUG
# The django statsd client to use, see django-statsd for more.
#STATSD_CLIENT = 'django_statsd.clients.moz_heka'

SYSLOG_TAG = "http_app_mkt_paymentsalt"
SYSLOG_TAG2 = "http_app_mkt_paymentsalt_timer"
SYSLOG_CSP = "http_app_mkt_paymentsalt_csp"
STATSD_PREFIX = 'marketplace-paymentsalt'

## Celery
BROKER_URL = private_mkt.BROKER_URL

CELERY_ALWAYS_EAGER = False
CELERY_IGNORE_RESULT = True
CELERY_DISABLE_RATE_LIMITS = True
CELERYD_PREFETCH_MULTIPLIER = 1

WEBAPPS_RECEIPT_KEY = private_mkt.WEBAPPS_RECEIPT_KEY
WEBAPPS_RECEIPT_URL = private_mkt.WEBAPPS_RECEIPT_URL

WEBAPPS_UNIQUE_BY_DOMAIN = True

SENTRY_DSN = private_mkt.SENTRY_DSN

SOLITUDE_HOSTS = ('https://payments-alt-solitude.allizom.org',)
SOLITUDE_OAUTH = {'key': private_mkt.SOLITUDE_OAUTH_KEY,
                  'secret': private_mkt.SOLITUDE_OAUTH_SECRET}

WEBAPPS_PUBLIC_KEY_DIRECTORY = NETAPP_STORAGE + '/public_keys'
PRODUCT_ICON_PATH = NETAPP_STORAGE + '/product-icons'
DUMPED_APPS_PATH = NETAPP_STORAGE + '/dumped-apps'
DUMPED_USERS_PATH = NETAPP_STORAGE + '/dumped-users'

GOOGLE_ANALYTICS_DOMAIN = 'marketplace.firefox.com'

VALIDATOR_TIMEOUT = 110
VALIDATOR_IAF_URLS = ['https://marketplace.firefox.com',
                      'https://marketplace.allizom.org',
                      'https://payments-alt.allizom.org',
                      'https://marketplace-dev.allizom.org',
                      'https://marketplace-altdev.allizom.org']

if getattr(private_mkt, 'LOAD_TESTING', False):
    # mock the authentication and use django_fakeauth for this
    AUTHENTICATION_BACKENDS = ('django_fakeauth.FakeAuthBackend',)\
                              + AUTHENTICATION_BACKENDS
    MIDDLEWARE_CLASSES.insert(
            MIDDLEWARE_CLASSES.index('mkt.access.middleware.ACLMiddleware'),
            'django_fakeauth.FakeAuthMiddleware')
    FAKEAUTH_TOKEN = private_mkt.FAKEAUTH_TOKEN

    # we are also creating access tokens for OAuth, here are the keys and
    # secrets used for them
    API_PASSWORD = getattr(private_mkt, 'API_PASSWORD', FAKEAUTH_TOKEN)
AMO_LANGUAGES = AMO_LANGUAGES + ('dbg',)
LANGUAGES = lazy(langs, dict)(AMO_LANGUAGES)
LANGUAGE_URL_MAP = dict([(i.lower(), i) for i in AMO_LANGUAGES])

BLUEVIA_SECRET = private_mkt.BLUEVIA_SECRET

#Bug 748403
SIGNING_SERVER = private_mkt.SIGNING_SERVER
SIGNING_SERVER_ACTIVE = True
SIGNING_VALID_ISSUERS = [DOMAIN]

#Bug 793876
SIGNED_APPS_KEY = private_mkt.SIGNED_APPS_KEY
SIGNED_APPS_SERVER_ACTIVE = True
SIGNED_APPS_SERVER = private_mkt.SIGNED_APPS_SERVER
SIGNED_APPS_REVIEWER_SERVER_ACTIVE = True
SIGNED_APPS_REVIEWER_SERVER = private_mkt.SIGNED_APPS_REVIEWER_SERVER

# See mkt/settings.py for more info.
APP_PURCHASE_KEY = DOMAIN
APP_PURCHASE_AUD = DOMAIN
APP_PURCHASE_TYP = 'mozilla-alt/payments/pay/v1'
APP_PURCHASE_SECRET = private_mkt.APP_PURCHASE_SECRET

MONOLITH_PASSWORD = private_mkt.MONOLITH_PASSWORD

# This is mainly for Marionette tests.
WEBAPP_MANIFEST_NAME = 'Marketplace Stage'

ENABLE_API_ERROR_SERVICE = True

NEWRELIC_INI = None

ES_DEFAULT_NUM_REPLICAS = 2
ES_USE_PLUGINS = True

BANGO_BASE_PORTAL_URL = 'https://mozilla.bango.com/login/al.aspx?'

MONOLITH_INDEX = 'mktstage-time_*'

# IARC content ratings.
IARC_ENV = 'test'
IARC_MOCK = False
IARC_PASSWORD = private_mkt.IARC_PASSWORD
IARC_PLATFORM = 'Firefox'
IARC_SERVICE_ENDPOINT = 'https://www.globalratings.com/IARCDEMOService/IARCServices.svc'
IARC_STOREFRONT_ID = 4
IARC_SUBMISSION_ENDPOINT = 'https://www.globalratings.com/IARCDEMORating/Submission.aspx'
IARC_ALLOW_CERT_REUSE = True

PRE_GENERATE_APKS = False
PRE_GENERATE_APK_URL = \
    'https://apk-controller.stage.mozaws.net/application.apk'

# Bug 1002569.
PAYMENT_PROVIDERS = ['bango', 'boku', 'reference']
DEFAULT_PAYMENT_PROVIDER = 'bango'

FXA_OAUTH_URL = getattr(private_mkt, 'FXA_OAUTH_URL', '')
FXA_CLIENT_ID = getattr(private_mkt, 'FXA_CLIENT_ID', '')
FXA_CLIENT_SECRET = getattr(private_mkt, 'FXA_CLIENT_SECRET', '')
