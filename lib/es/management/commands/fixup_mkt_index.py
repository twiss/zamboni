"""
A Marketplace only command that finds apps missing from the search index and
adds them.
"""
import sys

from pyelasticsearch.exceptions import ElasticHttpNotFoundError

from django.core.management.base import BaseCommand

from mkt.webapps.indexers import WebappIndexer
from mkt.webapps.models import Webapp
from mkt.webapps.tasks import index_webapps


class Command(BaseCommand):
    help = 'Fix up Marketplace index.'

    def handle(self, *args, **kwargs):
        index = WebappIndexer.get_index()
        doctype = WebappIndexer.get_mapping_type_name()
        es = WebappIndexer.get_es()

        apps = Webapp.objects.values_list('id', flat=True)

        missing_ids = []

        for app in apps:
            try:
                es.get(index, doctype, app, fields='id')
            except ElasticHttpNotFoundError:
                # App doesn't exist in our index, add it to `missing_ids`.
                missing_ids.append(app)

        if missing_ids:
            sys.stdout.write('Adding %s doc(s) to the index.'
                             % len(missing_ids))
            index_webapps.delay(missing_ids)
        else:
            sys.stdout.write('No docs missing from index.')
