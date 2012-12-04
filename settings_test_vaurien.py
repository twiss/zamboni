# Making the assumption there's a settings_local_mkt file
from settings_local_mkt import *

# Vaurien proxies
VAURIEN = True

AUTHENTICATION_BACKENDS = ('django_fakeauth.FakeAuthBackend',) + AUTHENTICATION_BACKENDS
MIDDLEWARE_CLASSES.insert(
        MIDDLEWARE_CLASSES.index('access.middleware.ACLMiddleware'),
        'django_fakeauth.FakeAuthMiddleware')

FAKEAUTH_TOKEN = 'sqldkjqlskjd34'

# ElasticSearch 9202 => 9200
#ES_HOSTS = ['127.0.0.1:9202']
#
## MySQL 3307 => 3306
#DATABASES['default']['PORT'] = 3307
#
## Celery 5673 => 5672
#BROKER_PORT = 5673
#
## Statsd 8126 => 8125
#STATSD_PORT = 8126
#
## Graphite 2004 => 2003
#GRAPHITE_PORT = 2003
#
## SMTP 2525 => 25
#EMAIL_PORT = 2525
#
## REDIS  6380 => 6379
#REDIS_BACKENDS = {'master': 'redis://localhost:6380?socket_timeout=0.5'}
#
## MEMCACHE 11212 => 11211
##
## to be adapted if we have several backends
#default_cache = CACHES.get('default')
#
#values = ('127.0.0.1:11211', '0.0.0.0:11211')
#
#if default_cache is not None:
#    backend = default_cache['BACKEND']
#    if backend in ('django.core.cache.backends.memcached.Memcached',
#                   'memcachepool.cache.UMemcacheCache'):
#        locations = default_cache['LOCATION']
#        if not isinstance(locations, (tuple, list)):
#            locations = [locations]
#
#        if locations[0] in values:
#            CACHES['default']['LOCATION'] = '0.0.0.0:11212'
