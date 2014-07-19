"""private_base will be populated from puppet and placed in this directory"""

import logging
import os

import dj_database_url

from mkt.settings import CACHE_PREFIX, ES_INDEXES, KNOWN_PROXIES, LOGGING

from .. import splitstrip
import private_base as private

ALLOWED_HOSTS = ['.allizom.org', '.mozflare.net']

ENGAGE_ROBOTS = False

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = private.EMAIL_HOST

DEBUG = False
TEMPLATE_DEBUG = DEBUG
DEBUG_PROPAGATE_EXCEPTIONS = False
SESSION_COOKIE_SECURE = True
REDIRECT_SECRET_KEY = private.REDIRECT_SECRET_KEY

ADMINS = ()

DATABASES = {}
DATABASES['default'] = dj_database_url.parse(private.DATABASES_DEFAULT_URL)
DATABASES['default']['ENGINE'] = 'mysql_pool'
DATABASES['default']['sa_pool_key'] = 'master'
DATABASES['default']['OPTIONS'] = {'init_command': 'SET storage_engine=InnoDB'}

DATABASES['slave'] = dj_database_url.parse(private.DATABASES_SLAVE_URL)
DATABASES['slave']['ENGINE'] = 'mysql_pool'
DATABASES['slave']['OPTIONS'] = {'init_command': 'SET storage_engine=InnoDB'}
DATABASES['slave']['sa_pool_key'] = 'slave'

SERVICES_DATABASE = dj_database_url.parse(private.SERVICES_DATABASE_URL)

DATABASE_POOL_ARGS = {
    'max_overflow': 10,
    'pool_size': 5,
    'recycle': 30
}

SLAVE_DATABASES = ['slave']

CACHES = {
    'default': {
        'BACKEND': 'caching.backends.memcached.MemcachedCache',
#        'BACKEND': 'django.core.cache.backends.memcached.MemcachedCache',
#        'BACKEND': 'memcachepool.cache.UMemcacheCache',
        'LOCATION': splitstrip(private.CACHES_DEFAULT_LOCATION),
        'TIMEOUT': 500,
        'KEY_PREFIX': CACHE_PREFIX,
    },
}

SECRET_KEY = private.SECRET_KEY

LOG_LEVEL = logging.DEBUG

## Celery
BROKER_URL = private.BROKER_URL

CELERY_ALWAYS_EAGER = True
CELERY_IGNORE_RESULT = True
CELERY_DISABLE_RATE_LIMITS = True
CELERYD_PREFETCH_MULTIPLIER = 1

NETAPP_STORAGE = private.NETAPP_STORAGE_ROOT + '/shared_storage'
MIRROR_STAGE_PATH = private.NETAPP_STORAGE_ROOT + '/public-staging'
GUARDED_ADDONS_PATH = private.NETAPP_STORAGE_ROOT + '/guarded-addons'
UPLOADS_PATH = NETAPP_STORAGE + '/uploads'
USERPICS_PATH = UPLOADS_PATH + '/userpics'
ADDON_ICONS_PATH = UPLOADS_PATH + '/addon_icons'
COLLECTIONS_ICON_PATH = UPLOADS_PATH + '/collection_icons'
IMAGEASSETS_PATH = UPLOADS_PATH + '/imageassets'
REVIEWER_ATTACHMENTS_PATH = UPLOADS_PATH + '/reviewer_attachment'
PREVIEWS_PATH = UPLOADS_PATH + '/previews'
SIGNED_APPS_PATH = NETAPP_STORAGE + '/signed_apps'
SIGNED_APPS_REVIEWER_PATH = NETAPP_STORAGE + '/signed_apps_reviewer'
PREVIEW_THUMBNAIL_PATH = PREVIEWS_PATH + '/thumbs/%s/%d.png'
PREVIEW_FULL_PATH = PREVIEWS_PATH + '/full/%s/%d.%s'

LOGGING['loggers'].update({
    'z.task': { 'level': logging.DEBUG },
    'z.redis': { 'level': logging.DEBUG },
    'z.pool': { 'level': logging.ERROR },
})

REDIS_BACKEND = private.REDIS_BACKENDS_CACHE
REDIS_BACKENDS = {
    'cache': private.REDIS_BACKENDS_CACHE,
    'cache_slave': private.REDIS_BACKENDS_CACHE_SLAVE,
    'master': private.REDIS_BACKENDS_MASTER,
    'slave': private.REDIS_BACKENDS_SLAVE,
}
CACHE_MACHINE_USE_REDIS = True

TMP_PATH = os.path.join(NETAPP_STORAGE, 'tmp')

ADDONS_PATH = private.NETAPP_STORAGE_ROOT + '/files'

SPIDERMONKEY = '/usr/bin/tracemonkey'

csp = 'csp.middleware.CSPMiddleware'


RESPONSYS_ID = private.RESPONSYS_ID

CRONJOB_LOCK_PREFIX = 'mkt-landfill'

ES_HOSTS = splitstrip(private.ES_HOSTS)
ES_URLS = ['http://%s' % h for h in ES_HOSTS]
ES_INDEXES = dict((k, '%s_landfill' % v) for k, v in ES_INDEXES.items())

STATSD_HOST = private.STATSD_HOST
STATSD_PORT = private.STATSD_PORT
STATSD_PREFIX = private.STATSD_PREFIX

CEF_PRODUCT = STATSD_PREFIX

ES_TIMEOUT = 60

EXPOSE_VALIDATOR_TRACEBACKS = False

KNOWN_PROXIES += ['10.2.83.105',
                  '10.2.83.106',
                  '10.2.83.107',
                  '10.8.83.200',
                  '10.8.83.201',
                  '10.8.83.202',
                  '10.8.83.203',
                  '10.8.83.204',
                  '10.8.83.210',
                  '10.8.83.211',
                  '10.8.83.212',
                  '10.8.83.213',
                  '10.8.83.214',
                  '10.8.83.215',
                  '10.8.83.251',
                  '10.8.83.252',
                  '10.8.83.253',
                  ]

NEW_FEATURES = True

REDIRECT_URL = 'https://outgoing.allizom.org/v1/'

CLEANCSS_BIN = 'cleancss'
LESS_BIN = 'lessc'
STYLUS_BIN = 'stylus'
UGLIFY_BIN = 'uglifyjs'

CELERYD_TASK_SOFT_TIME_LIMIT = 240

LESS_PREPROCESS = True

XSENDFILE_HEADER  = 'X-Accel-Redirect'

ALLOW_SELF_REVIEWS = True

AES_KEYS = private.AES_KEYS

TASK_USER_ID = 4757633
SERVE_TMP_PATH = False
