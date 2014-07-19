# -*- coding: utf-8 -*-
import base64
import datetime

from django.test.utils import override_settings

from nose.tools import eq_

import amo.tests

from lib.iarc.client import get_iarc_client
from lib.iarc.utils import IARC_XML_Parser, render_xml

from mkt.constants import ratingsbodies


class TestRenderAppInfo(amo.tests.TestCase):

    def setUp(self):
        self.template = 'get_app_info.xml'

    @override_settings(IARC_PASSWORD='s3kr3t')
    def test_render(self):
        xml = render_xml(self.template, {'submission_id': 100,
                                         'security_code': 'AB12CD3'})
        assert xml.startswith('<?xml version="1.0" encoding="utf-8"?>')
        assert '<FIELD NAME="password" VALUE="s3kr3t"' in xml
        assert '<FIELD NAME="submission_id" VALUE="100"' in xml
        assert '<FIELD NAME="security_code" VALUE="AB12CD3"' in xml
        assert '<FIELD NAME="platform" VALUE="Firefox"' in xml


class TestRenderSetStorefrontData(amo.tests.TestCase):

    def setUp(self):
        self.template = 'set_storefront_data.xml'

    @override_settings(IARC_PASSWORD='s3kr3t',
                       IARC_PLATFORM='Firefox')
    def test_render(self):
        xml = render_xml(self.template, {
            'submission_id': 100,
            'security_code': 'AB12CD3',
            'rating_system': 'PEGI',
            'release_date': datetime.date(2013, 11, 1),
            'title': 'Twitter',
            'company': 'Test User',
            'rating': '16+',
            'descriptors': u'N\xc3\xa3o h\xc3\xa1 inadequa\xc3\xa7\xc3\xb5es',
            'interactive_elements': 'users interact'})
        assert xml.startswith('<?xml version="1.0" encoding="utf-8"?>')
        assert '<FIELD NAME="password" VALUE="s3kr3t"' in xml
        assert '<FIELD NAME="storefront_company" VALUE="Test User"' in xml
        assert '<FIELD NAME="platform" VALUE="Firefox"' in xml
        assert '<FIELD NAME="submission_id" VALUE="100"' in xml
        assert '<FIELD NAME="security_code" VALUE="AB12CD3"' in xml
        assert '<FIELD NAME="rating_system" VALUE="PEGI"' in xml
        assert '<FIELD NAME="release_date" VALUE="2013-11-01"' in xml
        assert '<FIELD NAME="storefront_title" VALUE="Twitter"' in xml
        assert '<FIELD NAME="storefront_rating" VALUE="16+"' in xml
        assert ('<FIELD NAME="storefront_descriptors" '
                u'VALUE="N\xc3\xa3o h\xc3\xa1 inadequa\xc3\xa7\xc3\xb5es"'
                in xml)
        assert ('<FIELD NAME="storefront_interactive_elements" '
                'VALUE="users interact"') in xml

        # The client base64 encodes these. Mimic what the client does here to
        # ensure no unicode problems.
        base64.b64encode(xml.encode('utf-8'))


class TestRenderRatingChanges(amo.tests.TestCase):

    def setUp(self):
        self.template = 'get_rating_changes.xml'

    @override_settings(IARC_PASSWORD='s3kr3t')
    def test_render(self):
        xml = render_xml(self.template, {
            'date_from': datetime.date(2011, 1, 1),
            'date_to': datetime.date(2011, 2, 1)})
        assert xml.startswith('<?xml version="1.0" encoding="utf-8"?>')
        assert '<FIELD NAME="password" VALUE="s3kr3t"' in xml
        assert '<FIELD NAME="date_from" VALUE="2011-01-01"' in xml
        assert '<FIELD NAME="date_to" VALUE="2011-02-01"' in xml


class TestXMLParser(amo.tests.TestCase):

    def setUp(self):
        self.client = get_iarc_client('service')

    def test_missing_value(self):
        """Sometimes the VALUE attribute in the XML is missing."""
        xml = '''<?xml version="1.0" encoding="utf-16"?>
            <WEBSERVICE SERVICE_NAME="SET_STOREFRONT_DATA" TYPE="REQUEST">
            <ROW>
            <FIELD NAME="rating_PEGI" TYPE="string" VALUE="16+" />
            <FIELD NAME="descriptors_PEGI" TYPE="string" />
            </ROW>
            </WEBSERVICE>'''
        data = IARC_XML_Parser().parse_string(xml)
        assert 'ratings' in data['rows'][0]
        assert 'descriptors' not in data['rows'][0]

    def test_app_info(self):
        xml = self.client.Get_App_Info(XMLString='foo')
        data = IARC_XML_Parser().parse_string(xml)['rows'][0]

        eq_(data['submission_id'], 52)
        eq_(data['title'], 'Twitter')
        eq_(data['company'], 'Mozilla')
        eq_(data['storefront'], 'Mozilla')
        eq_(data['platform'], 'Firefox')

        # Test ratings get mapped to their appropriate rating classes.
        eq_(data['ratings'][ratingsbodies.ESRB], ratingsbodies.ESRB_M)
        eq_(data['ratings'][ratingsbodies.USK], ratingsbodies.USK_REJECTED)
        eq_(data['ratings'][ratingsbodies.CLASSIND], ratingsbodies.CLASSIND_14)
        eq_(data['ratings'][ratingsbodies.PEGI], ratingsbodies.PEGI_16)
        eq_(data['ratings'][ratingsbodies.GENERIC], ratingsbodies.GENERIC_16)

        # Test descriptors.
        self.assertSetEqual(data['descriptors'],
                            ['has_usk_lang',
                             'has_esrb_strong_lang',
                             'has_classind_lang',
                             'has_pegi_lang', 'has_pegi_online'])

        # Test interactives.
        self.assertSetEqual(data['interactives'],
                            ['has_shares_info', 'has_shares_location',
                             'has_digital_purchases', 'has_users_interact'])

    def test_rating_changes(self):
        xml = self.client.Get_Rating_Changes(XMLString='foo')
        data = IARC_XML_Parser().parse_string(xml)

        eq_(len(data['rows']), 2)

        row = data['rows'][0]
        eq_(row['rowId'], 1)
        eq_(row['submission_id'], 52)
        eq_(row['title'], 'Twitter')
        eq_(row['company'], 'Mozilla')
        eq_(row['change_date'], '11/12/2013')
        eq_(row['security_code'], 'FZ32CU8')
        eq_(row['email'], 'nobody@mozilla.com')
        eq_(row['rating_system'], ratingsbodies.CLASSIND)
        eq_(row['new_rating'], '14+')
        eq_(row['new_descriptors'], u'Linguagem Impr\xf3pria')
        eq_(row['change_reason'],
            'Significant issues found in special mission cut scenes.')

        row = data['rows'][1]
        eq_(row['rowId'], 2)
        eq_(row['submission_id'], 68)
        eq_(row['title'], 'Other App')
        eq_(row['company'], 'Mozilla')
        eq_(row['change_date'], '11/12/2013')
        eq_(row['security_code'], 'GZ32CU8')
        eq_(row['email'], 'nobody@mozilla.com')
        eq_(row['new_rating'], 'Rating Refused')
        eq_(row['new_descriptors'], 'Explizite Sprache')
        eq_(row['rating_system'], ratingsbodies.USK)
        eq_(row['change_reason'],
            'Discrimination found to be within German law.')
