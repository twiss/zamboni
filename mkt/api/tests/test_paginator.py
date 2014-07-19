from urlparse import urlparse

from django.core.paginator import Paginator
from django.http import QueryDict

import mock
from elasticutils.contrib.django import S
from nose.tools import eq_
from test_utils import RequestFactory

from amo.tests import TestCase

from mkt.api.paginator import MetaSerializer, ESPaginator
from mkt.webapps.indexers import WebappIndexer


class TestSearchPaginator(TestCase):

    # TODO: When we update searching update this also.
    # @mock.patch('elasticsearch.connection.http_requests'
    #             '.RequestsHttpConnection.perform_request')
    @mock.patch('pyelasticsearch.client.ElasticSearch.send_request')
    def test_single_hit(self, _mock):
        """Test the ES paginator only queries ES one time."""
        ESPaginator(S(WebappIndexer), 5).object_list.execute()
        eq_(_mock.call_count, 1)


class TestMetaSerializer(TestCase):
    def setUp(self):
        self.url = '/api/whatever'
        self.request = RequestFactory().get(self.url)

    def get_serialized_data(self, page):
        return MetaSerializer(page, context={'request': self.request}).data

    def test_simple(self):
        data = ['a', 'b', 'c']
        per_page = 3
        page = Paginator(data, per_page).page(1)
        serialized = self.get_serialized_data(page)
        eq_(serialized['offset'], 0)
        eq_(serialized['next'], None)
        eq_(serialized['previous'], None)
        eq_(serialized['total_count'], len(data))
        eq_(serialized['limit'], per_page)

    def test_first_page_of_two(self):
        data = ['a', 'b', 'c', 'd', 'e']
        per_page = 3
        page = Paginator(data, per_page).page(1)
        serialized = self.get_serialized_data(page)
        eq_(serialized['offset'], 0)
        eq_(serialized['total_count'], len(data))
        eq_(serialized['limit'], per_page)

        eq_(serialized['previous'], None)

        next = urlparse(serialized['next'])
        eq_(next.path, self.url)
        eq_(QueryDict(next.query), QueryDict('limit=3&offset=3'))

    def test_third_page_of_four(self):
        data = ['a', 'b', 'c', 'd', 'e', 'f', 'g']
        per_page = 2
        page = Paginator(data, per_page).page(3)
        serialized = self.get_serialized_data(page)
        # Third page will begin after fourth item
        # (per_page * number of pages before) item.
        eq_(serialized['offset'], 4)
        eq_(serialized['total_count'], len(data))
        eq_(serialized['limit'], per_page)

        prev = urlparse(serialized['previous'])
        eq_(prev.path, self.url)
        eq_(QueryDict(prev.query), QueryDict('limit=2&offset=2'))

        next = urlparse(serialized['next'])
        eq_(next.path, self.url)
        eq_(QueryDict(next.query), QueryDict('limit=2&offset=6'))

    def test_fourth_page_of_four(self):
        data = ['a', 'b', 'c', 'd', 'e', 'f', 'g']
        per_page = 2
        page = Paginator(data, per_page).page(4)
        serialized = self.get_serialized_data(page)
        # Third page will begin after fourth item
        # (per_page * number of pages before) item.
        eq_(serialized['offset'], 6)
        eq_(serialized['total_count'], len(data))
        eq_(serialized['limit'], per_page)

        prev = urlparse(serialized['previous'])
        eq_(prev.path, self.url)
        eq_(QueryDict(prev.query), QueryDict('limit=2&offset=4'))

        eq_(serialized['next'], None)

    def test_without_request_path(self):
        data = ['a', 'b', 'c', 'd', 'e']
        per_page = 2
        page = Paginator(data, per_page).page(2)
        serialized = MetaSerializer(page).data
        eq_(serialized['offset'], 2)
        eq_(serialized['total_count'], len(data))
        eq_(serialized['limit'], per_page)

        prev = urlparse(serialized['previous'])
        eq_(prev.path, '')
        eq_(QueryDict(prev.query), QueryDict('limit=2&offset=0'))

        next = urlparse(serialized['next'])
        eq_(next.path, '')
        eq_(QueryDict(next.query), QueryDict('limit=2&offset=4'))

    def test_with_request_path_override_existing_params(self):
        self.url = '/api/whatever/?limit=0&offset=xxx&extra&superfluous=yes'
        self.request = RequestFactory().get(self.url)

        data = ['a', 'b', 'c', 'd', 'e', 'f']
        per_page = 2
        page = Paginator(data, per_page).page(2)
        serialized = self.get_serialized_data(page)
        eq_(serialized['offset'], 2)
        eq_(serialized['total_count'], len(data))
        eq_(serialized['limit'], per_page)

        prev = urlparse(serialized['previous'])
        eq_(prev.path, '/api/whatever/')
        eq_(QueryDict(prev.query),
            QueryDict('limit=2&offset=0&extra=&superfluous=yes'))

        next = urlparse(serialized['next'])
        eq_(next.path, '/api/whatever/')
        eq_(QueryDict(next.query),
            QueryDict('limit=2&offset=4&extra=&superfluous=yes'))
