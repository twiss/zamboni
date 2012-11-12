# Making the assumption there's a settings_local_mkt file
from settings_local_mkt import *

# Vaurien proxies
VAURIEN = True

# ElasticSearch 9202 => 9200
ES_HOSTS = ['127.0.0.1:9202']

# MySQL 3307 => 3306
DATABASES['default']['PORT'] = 3307

# Celery 5673 => 5672
BROKER_PORT = 5673

# Statsd 8126 => 8125
STATSD_PORT = 8126

# Graphite 2004 => 2003
GRAPHITE_PORT = 2003

# SMTP 2525 => 25
EMAIL_PORT = 2525
