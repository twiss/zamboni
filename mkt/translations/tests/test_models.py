# -*- coding: utf-8 -*-
from contextlib import nested

import django
from django.conf import settings
from django.db import connections, reset_queries
from django.test.utils import override_settings
from django.utils import translation
from django.utils.functional import lazy

import jinja2
import multidb
from mock import patch
from nose import SkipTest
from nose.tools import eq_
from test_utils import trans_eq, TestCase

from mkt.translations import widgets
from mkt.translations.models import (LinkifiedTranslation, NoLinksTranslation,
                                 NoLinksNoMarkupTranslation,
                                 PurifiedTranslation, Translation,
                                 TranslationSequence)
from mkt.translations.query import order_by_translation
from testapp.models import TranslatedModel, UntranslatedModel, FancyModel


def ids(qs):
    return [o.id for o in qs]


class TranslationFixturelessTestCase(TestCase):
    "We want to be able to rollback stuff."

    def test_whitespace(self):
        t = Translation(localized_string='     khaaaaaan!    ', id=999)
        t.save()
        eq_('khaaaaaan!', t.localized_string)


class TranslationSequenceTestCase(TestCase):
    """
    Make sure automatic translation sequence generation works
    as expected.
    """

    def test_empty_translations_seq(self):
        """Make sure we can handle an empty translation sequence table."""
        TranslationSequence.objects.all().delete()
        newtrans = Translation.new('abc', 'en-us')
        newtrans.save()
        assert newtrans.id > 0, (
            'Empty translation table should still generate an ID.')

    def test_single_translation_sequence(self):
        """Make sure we only ever have one translation sequence."""
        TranslationSequence.objects.all().delete()
        eq_(TranslationSequence.objects.count(), 0)
        for i in range(5):
            newtrans = Translation.new(str(i), 'en-us')
            newtrans.save()
            eq_(TranslationSequence.objects.count(), 1)

    def test_translation_sequence_increases(self):
        """Make sure translation sequence increases monotonically."""
        newtrans1 = Translation.new('abc', 'en-us')
        newtrans1.save()
        newtrans2 = Translation.new('def', 'de')
        newtrans2.save()
        assert newtrans2.pk > newtrans1.pk, (
            'Translation sequence needs to keep increasing.')


class TranslationTestCase(TestCase):
    fixtures = ['testapp/test_models.json']

    def setUp(self):
        super(TranslationTestCase, self).setUp()
        self.redirect_url = settings.REDIRECT_URL
        self.redirect_secret_key = settings.REDIRECT_SECRET_KEY
        settings.REDIRECT_URL = None
        settings.REDIRECT_SECRET_KEY = 'sekrit'
        translation.activate('en-US')

    def tearDown(self):
        super(TranslationTestCase, self).tearDown()
        settings.REDIRECT_URL = self.redirect_url
        settings.REDIRECT_SECRET_KEY = self.redirect_secret_key

    def test_meta_translated_fields(self):
        assert not hasattr(UntranslatedModel._meta, 'translated_fields')

        eq_(set(TranslatedModel._meta.translated_fields),
            set([TranslatedModel._meta.get_field('no_locale'),
                 TranslatedModel._meta.get_field('name'),
                 TranslatedModel._meta.get_field('description')]))

        eq_(set(FancyModel._meta.translated_fields),
            set([FancyModel._meta.get_field('purified'),
                 FancyModel._meta.get_field('linkified')]))

    def test_fetch_translations(self):
        """Basic check of fetching translations in the current locale."""
        o = TranslatedModel.objects.get(id=1)
        trans_eq(o.name, 'some name', 'en-US')
        trans_eq(o.description, 'some description', 'en-US')

    def test_fetch_no_translations(self):
        """Make sure models with no translations aren't harmed."""
        o = UntranslatedModel.objects.get(id=1)
        eq_(o.number, 17)

    def test_fetch_translation_de_locale(self):
        """Check that locale fallbacks work."""
        try:
            translation.activate('de')
            o = TranslatedModel.objects.get(id=1)
            trans_eq(o.name, 'German!! (unst unst)', 'de')
            trans_eq(o.description, 'some description', 'en-US')
        finally:
            translation.deactivate()

    def test_create_translation(self):
        o = TranslatedModel.objects.create(name='english name')
        get_model = lambda: TranslatedModel.objects.get(id=o.id)
        trans_eq(o.name, 'english name', 'en-US')
        eq_(o.description, None)

        # Make sure the translation id is stored on the model, not the autoid.
        eq_(o.name.id, o.name_id)

        # Check that a different locale creates a new row with the same id.
        translation.activate('de')
        german = get_model()
        trans_eq(o.name, 'english name', 'en-US')

        german.name = u'Gemütlichkeit name'
        german.description = u'clöüserw description'
        german.save()

        trans_eq(german.name, u'Gemütlichkeit name', 'de')
        trans_eq(german.description, u'clöüserw description', 'de')

        # ids should be the same, autoids are different.
        eq_(o.name.id, german.name.id)
        assert o.name.autoid != german.name.autoid

        # Check that de finds the right translation.
        fresh_german = get_model()
        trans_eq(fresh_german.name, u'Gemütlichkeit name', 'de')
        trans_eq(fresh_german.description, u'clöüserw description', 'de')

        # Check that en-US has the right translations.
        translation.deactivate()
        english = get_model()
        trans_eq(english.name, 'english name', 'en-US')
        english.debug = True
        eq_(english.description, None)

        english.description = 'english description'
        english.save()

        fresh_english = get_model()
        trans_eq(fresh_english.description, 'english description', 'en-US')
        eq_(fresh_english.description.id, fresh_german.description.id)

    def test_update_translation(self):
        o = TranslatedModel.objects.get(id=1)
        translation_id = o.name.autoid

        o.name = 'new name'
        o.save()

        o = TranslatedModel.objects.get(id=1)
        trans_eq(o.name, 'new name', 'en-US')
        # Make sure it was an update, not an insert.
        eq_(o.name.autoid, translation_id)

    def test_create_with_dict(self):
        # Set translations with a dict.
        strings = {'en-US': 'right language', 'de': 'wrong language'}
        o = TranslatedModel.objects.create(name=strings)

        # Make sure we get the English text since we're in en-US.
        trans_eq(o.name, 'right language', 'en-US')

        # Check that de was set.
        translation.activate('de')
        o = TranslatedModel.objects.get(id=o.id)
        trans_eq(o.name, 'wrong language', 'de')

        # We're in de scope, so we should see the de text.
        de = TranslatedModel.objects.create(name=strings)
        trans_eq(o.name, 'wrong language', 'de')

        # Make sure en-US was still set.
        translation.deactivate()
        o = TranslatedModel.objects.get(id=de.id)
        trans_eq(o.name, 'right language', 'en-US')

    def test_update_with_dict(self):
        # There's existing en-US and de strings.
        strings = {'de': None, 'fr': 'oui'}
        get_model = lambda: TranslatedModel.objects.get(id=1)

        # Don't try checking that the model's name value is en-US.  It will be
        # one of the other locales, but we don't know which one.  You just set
        # the name to a dict, deal with it.
        m = get_model()
        m.name = strings
        m.save()

        # en-US was not touched.
        trans_eq(get_model().name, 'some name', 'en-US')

        # de was updated to NULL, so it falls back to en-US.
        translation.activate('de')
        trans_eq(get_model().name, 'some name', 'en-US')

        # fr was added.
        translation.activate('fr')
        trans_eq(get_model().name, 'oui', 'fr')

    def test_dict_with_hidden_locale(self):
        with self.settings(HIDDEN_LANGUAGES=('xxx',)):
            o = TranslatedModel.objects.get(id=1)
            o.name = {'en-US': 'active language', 'xxx': 'hidden language',
                      'de': 'another language'}
            o.save()
        ts = Translation.objects.filter(id=o.name_id)
        eq_(sorted(ts.values_list('locale', flat=True)),
            ['de', 'en-US', 'xxx'])

    def test_dict_bad_locale(self):
        m = TranslatedModel.objects.get(id=1)
        m.name = {'de': 'oof', 'xxx': 'bam', 'es': 'si'}
        m.save()

        ts = Translation.objects.filter(id=m.name_id)
        eq_(sorted(ts.values_list('locale', flat=True)),
            ['de', 'en-US', 'es'])

    def test_sorting(self):
        """Test translation comparisons in Python code."""
        b = Translation.new('bbbb', 'de')
        a = Translation.new('aaaa', 'de')
        c = Translation.new('cccc', 'de')
        eq_(sorted([c, a, b]), [a, b, c])

    def test_sorting_en(self):
        q = TranslatedModel.objects.all()
        expected = [4, 1, 3]

        eq_(ids(order_by_translation(q, 'name')), expected)
        eq_(ids(order_by_translation(q, '-name')), list(reversed(expected)))

    def test_sorting_mixed(self):
        translation.activate('de')
        q = TranslatedModel.objects.all()
        expected = [1, 4, 3]

        eq_(ids(order_by_translation(q, 'name')), expected)
        eq_(ids(order_by_translation(q, '-name')), list(reversed(expected)))

    def test_sorting_by_field(self):
        field = TranslatedModel._meta.get_field('default_locale')
        TranslatedModel.get_fallback = classmethod(lambda cls: field)

        translation.activate('de')
        q = TranslatedModel.objects.all()
        expected = [3, 1, 4]

        eq_(ids(order_by_translation(q, 'name')), expected)
        eq_(ids(order_by_translation(q, '-name')), list(reversed(expected)))

        del TranslatedModel.get_fallback

    def test_new_purified_field(self):
        # This is not a full test of the html sanitizing.  We expect the
        # underlying bleach library to have full tests.
        s = '<a id=xx href="http://xxx.com">yay</a> <i>http://yyy.com</i>'
        m = FancyModel.objects.create(purified=s)
        eq_(m.purified.localized_string_clean,
            '<a rel="nofollow" href="http://xxx.com">yay</a> '
            '<i><a rel="nofollow" href="http://yyy.com">'
            'http://yyy.com</a></i>')
        eq_(m.purified.localized_string, s)

    def test_new_linkified_field(self):
        s = '<a id=xx href="http://xxx.com">yay</a> <i>http://yyy.com</i>'
        m = FancyModel.objects.create(linkified=s)
        eq_(m.linkified.localized_string_clean,
            '<a rel="nofollow" href="http://xxx.com">yay</a> '
            '&lt;i&gt;<a rel="nofollow" href="http://yyy.com">'
            'http://yyy.com</a>&lt;/i&gt;')
        eq_(m.linkified.localized_string, s)

    def test_update_purified_field(self):
        m = FancyModel.objects.get(id=1)
        s = '<a id=xx href="http://xxx.com">yay</a> <i>http://yyy.com</i>'
        m.purified = s
        m.save()
        eq_(m.purified.localized_string_clean,
            '<a rel="nofollow" href="http://xxx.com">yay</a> '
            '<i><a rel="nofollow" href="http://yyy.com">'
            'http://yyy.com</a></i>')
        eq_(m.purified.localized_string, s)

    def test_update_linkified_field(self):
        m = FancyModel.objects.get(id=1)
        s = '<a id=xx href="http://xxx.com">yay</a> <i>http://yyy.com</i>'
        m.linkified = s
        m.save()
        eq_(m.linkified.localized_string_clean,
            '<a rel="nofollow" href="http://xxx.com">yay</a> '
            '&lt;i&gt;<a rel="nofollow" href="http://yyy.com">'
            'http://yyy.com</a>&lt;/i&gt;')
        eq_(m.linkified.localized_string, s)

    def test_purified_field_str(self):
        m = FancyModel.objects.get(id=1)
        eq_(u'%s' % m.purified,
            '<i>x</i> '
            '<a rel="nofollow" href="http://yyy.com">http://yyy.com</a>')

    def test_linkified_field_str(self):
        m = FancyModel.objects.get(id=1)
        eq_(u'%s' % m.linkified,
            '&lt;i&gt;x&lt;/i&gt; '
            '<a rel="nofollow" href="http://yyy.com">http://yyy.com</a>')

    def test_purifed_linkified_fields_in_template(self):
        m = FancyModel.objects.get(id=1)
        env = jinja2.Environment()
        t = env.from_string('{{ m.purified }}=={{ m.linkified }}')
        s = t.render({'m': m})
        eq_(s, u'%s==%s' % (m.purified.localized_string_clean,
                            m.linkified.localized_string_clean))

    def test_outgoing_url(self):
        """
        Make sure linkified field is properly bounced off our outgoing URL
        redirector.
        """
        settings.REDIRECT_URL = 'http://example.com/'

        s = 'I like http://example.org/awesomepage.html .'
        m = FancyModel.objects.create(linkified=s)
        eq_(m.linkified.localized_string_clean,
            'I like <a rel="nofollow" href="http://example.com/'
            '40979175e3ef6d7a9081085f3b99f2f05447b22ba790130517dd62b7ee59ef94/'
            'http%3A//example.org/'
            'awesomepage.html">http://example.org/awesomepage'
            '.html</a> .')
        eq_(m.linkified.localized_string, s)

    def test_require_locale(self):
        obj = TranslatedModel.objects.get(id=1)
        eq_(unicode(obj.no_locale), 'blammo')
        eq_(obj.no_locale.locale, 'en-US')

        # Switch the translation to a locale we wouldn't pick up by default.
        obj.no_locale.locale = 'fr'
        obj.no_locale.save()

        obj = TranslatedModel.objects.get(id=1)
        eq_(unicode(obj.no_locale), 'blammo')
        eq_(obj.no_locale.locale, 'fr')

    def test_delete_set_null(self):
        """
        Test that deleting a translation sets the corresponding FK to NULL,
        if it was the only translation for this field.
        """
        obj = TranslatedModel.objects.get(id=1)
        trans_id = obj.description.id
        eq_(Translation.objects.filter(id=trans_id).count(), 1)

        obj.description.delete()

        obj = TranslatedModel.objects.no_cache().get(id=1)
        eq_(obj.description_id, None)
        eq_(obj.description, None)
        eq_(Translation.objects.no_cache().filter(id=trans_id).exists(), False)

    @patch.object(TranslatedModel, 'get_fallback', create=True)
    def test_delete_keep_other_translations(self, get_fallback):
        # To make sure both translations for the name are used, set the
        # fallback to the second locale, which is 'de'.
        get_fallback.return_value = 'de'

        obj = TranslatedModel.objects.get(id=1)

        orig_name_id = obj.name.id
        eq_(obj.name.locale.lower(), 'en-us')
        eq_(Translation.objects.filter(id=orig_name_id).count(), 2)

        obj.name.delete()

        obj = TranslatedModel.objects.no_cache().get(id=1)
        eq_(Translation.objects.no_cache().filter(id=orig_name_id).count(), 1)

        # We shouldn't have set name_id to None.
        eq_(obj.name_id, orig_name_id)

        # We should find a Translation.
        eq_(obj.name.id, orig_name_id)
        eq_(obj.name.locale, 'de')


class TranslationMultiDbTests(TestCase):
    fixtures = ['testapp/test_models.json']

    def setUp(self):
        super(TranslationMultiDbTests, self).setUp()
        translation.activate('en-US')

    def tearDown(self):
        self.cleanup_fake_connections()
        super(TranslationMultiDbTests, self).tearDown()

    @property
    def mocked_dbs(self):
        return {
            'default': settings.DATABASES['default'],
            'slave-1': settings.DATABASES['default'].copy(),
            'slave-2': settings.DATABASES['default'].copy(),
        }

    def cleanup_fake_connections(self):
        with patch.object(django.db.connections, 'databases', self.mocked_dbs):
            for key in ('default', 'slave-1', 'slave-2'):
                connections[key].close()

    @override_settings(DEBUG=True)
    def test_translations_queries(self):
        # Make sure we are in a clean environnement.
        reset_queries()
        TranslatedModel.objects.get(pk=1)
        eq_(len(connections['default'].queries), 3)

    @override_settings(DEBUG=True)
    def test_translations_reading_from_multiple_db(self):
        with patch.object(django.db.connections, 'databases', self.mocked_dbs):
            # Make sure we are in a clean environnement.
            reset_queries()

            with patch('multidb.get_slave', lambda: 'slave-2'):
                TranslatedModel.objects.get(pk=1)
                eq_(len(connections['default'].queries), 0)
                eq_(len(connections['slave-1'].queries), 0)
                eq_(len(connections['slave-2'].queries), 3)

    @override_settings(DEBUG=True)
    def test_translations_reading_from_multiple_db_using(self):
        raise SkipTest('Will need a django-queryset-transform patch to work')
        with patch.object(django.db.connections, 'databases', self.mocked_dbs):
            # Make sure we are in a clean environnement.
            reset_queries()

            with patch('multidb.get_slave', lambda: 'slave-2'):
                TranslatedModel.objects.using('slave-1').get(pk=1)
                eq_(len(connections['default'].queries), 0)
                eq_(len(connections['slave-1'].queries), 3)
                eq_(len(connections['slave-2'].queries), 0)

    @override_settings(DEBUG=True)
    def test_translations_reading_from_multiple_db_pinning(self):
        with patch.object(django.db.connections, 'databases', self.mocked_dbs):
            # Make sure we are in a clean environnement.
            reset_queries()

            with nested(patch('multidb.get_slave', lambda: 'slave-2'),
                        multidb.pinning.use_master):
                TranslatedModel.objects.get(pk=1)
                eq_(len(connections['default'].queries), 3)
                eq_(len(connections['slave-1'].queries), 0)
                eq_(len(connections['slave-2'].queries), 0)


class PurifiedTranslationTest(TestCase):

    def test_output(self):
        assert isinstance(PurifiedTranslation().__html__(), unicode)

    def test_raw_text(self):
        s = u'   This is some text   '
        x = PurifiedTranslation(localized_string=s)
        eq_(x.__html__(), 'This is some text')

    def test_allowed_tags(self):
        s = u'<b>bold text</b> or <code>code</code>'
        x = PurifiedTranslation(localized_string=s)
        eq_(x.__html__(),  u'<b>bold text</b> or <code>code</code>')

    def test_forbidden_tags(self):
        s = u'<script>some naughty xss</script>'
        x = PurifiedTranslation(localized_string=s)
        eq_(x.__html__(), '&lt;script&gt;some naughty xss&lt;/script&gt;')

    def test_internal_link(self):
        s = u'<b>markup</b> <a href="http://addons.mozilla.org/foo">bar</a>'
        x = PurifiedTranslation(localized_string=s)
        eq_(x.__html__(),
            u'<b>markup</b> <a rel="nofollow" '
            u'href="http://addons.mozilla.org/foo">bar</a>')

    @patch('amo.urlresolvers.get_outgoing_url')
    def test_external_link(self, get_outgoing_url_mock):
        get_outgoing_url_mock.return_value = 'http://external.url'
        s = u'<b>markup</b> <a href="http://example.com">bar</a>'
        x = PurifiedTranslation(localized_string=s)
        eq_(x.__html__(),
            u'<b>markup</b> <a rel="nofollow" '
            u'href="http://external.url">bar</a>')

    @patch('amo.urlresolvers.get_outgoing_url')
    def test_external_text_link(self, get_outgoing_url_mock):
        get_outgoing_url_mock.return_value = 'http://external.url'
        s = u'<b>markup</b> http://example.com'
        x = PurifiedTranslation(localized_string=s)
        eq_(x.__html__(),
            u'<b>markup</b> <a rel="nofollow" '
            u'href="http://external.url">http://example.com</a>')

    def test_newlines_normal(self):
        before = ("Paragraph one.\n"
                  "This should be on the very next line.\n\n"
                  "Should be two nl's before this line.\n\n\n"
                  "Should be three nl's before this line.\n\n\n\n"
                  "Should be four nl's before this line.")
        after = before  # Nothing special; this shouldn't change.
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_ul(self):
        before = ("<ul>\n\n"
                  "<li>No nl's between the ul and the li.</li>\n\n"
                  "<li>No nl's between li's.\n\n"
                  "But there should be two before this line.</li>\n\n"
                  "</ul>")
        after = ("<ul>"
                 "<li>No nl's between the ul and the li.</li>"
                 "<li>No nl's between li's.\n\n"
                 "But there should be two before this line.</li>"
                 "</ul>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_ul_tight(self):
        before = ("There should be one nl between this and the ul.\n"
                  "<ul><li>test</li><li>test</li></ul>\n"
                  "There should be no nl's above this line.")
        after = ("There should be one nl between this and the ul.\n"
                 "<ul><li>test</li><li>test</li></ul>"
                 "There should be no nl's above this line.")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_ul_loose(self):
        before = ("There should be two nl's between this and the ul.\n\n"
                  "<ul><li>test</li><li>test</li></ul>\n\n"
                  "There should be one nl above this line.")
        after = ("There should be two nl's between this and the ul.\n\n"
                 "<ul><li>test</li><li>test</li></ul>\n"
                 "There should be one nl above this line.")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_blockquote_tight(self):
        before = ("There should be one nl below this.\n"
                  "<blockquote>Hi</blockquote>\n"
                  "There should be no nl's above this.")
        after = ("There should be one nl below this.\n"
                 "<blockquote>Hi</blockquote>"
                 "There should be no nl's above this.")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_blockquote_loose(self):
        before = ("There should be two nls below this.\n\n"
                  "<blockquote>Hi</blockquote>\n\n"
                  "There should be one nl above this.")
        after = ("There should be two nls below this.\n\n"
                 "<blockquote>Hi</blockquote>\n"
                 "There should be one nl above this.")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_inline(self):
        before = ("If we end a paragraph w/ a <b>non-block-level tag</b>\n\n"
                  "<b>The newlines</b> should be kept")
        after = before  # Should stay the same
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_code_inline(self):
        before = ("Code tags aren't blocks.\n\n"
                  "<code>alert(test);</code>\n\n"
                  "See?")
        after = before  # Should stay the same
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_li_newlines(self):
        before = ("<ul><li>\nxx</li></ul>")
        after = ("<ul><li>xx</li></ul>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

        before = ("<ul><li>xx\n</li></ul>")
        after = ("<ul><li>xx</li></ul>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

        before = ("<ul><li>xx\nxx</li></ul>")
        after = ("<ul><li>xx\nxx</li></ul>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

        before = ("<ul><li></li></ul>")
        after = ("<ul><li></li></ul>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

        # All together now
        before = ("<ul><li>\nxx</li> <li>xx\n</li> <li>xx\nxx</li> "
                  "<li></li>\n</ul>")
        after = ("<ul><li>xx</li> <li>xx</li> <li>xx\nxx</li> "
                 "<li></li></ul>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_empty_tag(self):
        before = ("This is a <b></b> test!")
        after = before
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_empty_tag_nested(self):
        before = ("This is a <b><i></i></b> test!")
        after = before
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_empty_tag_block_nested(self):
        before = ("Test.\n\n<blockquote><ul><li>"
                  "</li></ul></blockquote>\ntest.")
        after = ("Test.\n\n<blockquote><ul><li>"
                 "</li></ul></blockquote>test.")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_empty_tag_block_nested_spaced(self):
        before = ("Test.\n\n<blockquote>\n\n<ul>\n\n<li>"
                  "</li>\n\n</ul>\n\n</blockquote>\ntest.")
        after = ("Test.\n\n<blockquote><ul><li></li></ul></blockquote>test.")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_li_newlines_inline(self):
        before = ("<ul><li>\n<b>test\ntest\n\ntest</b>\n</li>"
                  "<li>Test <b>test</b> test.</li></ul>")
        after = ("<ul><li><b>test\ntest\n\ntest</b></li>"
                 "<li>Test <b>test</b> test.</li></ul>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_li_all_inline(self):
        before = ("Test with <b>no newlines</b> and <code>block level "
                  "stuff</code> to see what happens.")
        after = before  # Should stay the same
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_spaced_blocks(self):
        before = ("<blockquote>\n\n<ul>\n\n<li>\n\ntest\n\n</li>\n\n"
                  "</ul>\n\n</blockquote>")
        after = "<blockquote><ul><li>test</li></ul></blockquote>"
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_spaced_inline(self):
        before = "Line.\n\n<b>\nThis line is bold.\n</b>\n\nThis isn't."
        after = before
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_nested_inline(self):
        before = "<b>\nThis line is bold.\n\n<i>This is also italic</i></b>"
        after = before
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_xss_script(self):
        before = "<script>\n\nalert('test');\n</script>"
        after = "&lt;script&gt;\n\nalert('test');\n&lt;/script&gt;"
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_xss_inline(self):
        before = "<b onclick=\"alert('test');\">test</b>"
        after = "<b>test</b>"
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    @patch('amo.helpers.urlresolvers.get_outgoing_url')
    def test_newlines_attribute_link_doublequote(self, mock_get_outgoing_url):
        mock_get_outgoing_url.return_value = 'http://google.com'
        before = '<a href="http://google.com">test</a>'
        after = '<a rel="nofollow" href="http://google.com">test</a>'
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_attribute_singlequote(self):
        before = "<abbr title='laugh out loud'>lol</abbr>"
        after = '<abbr title="laugh out loud">lol</abbr>'
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_attribute_doublequote(self):
        before = '<abbr title="laugh out loud">lol</abbr>'
        after = before
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_attribute_nestedquotes_doublesingle(self):
        before = '<abbr title="laugh \'out\' loud">lol</abbr>'
        after = before
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_attribute_nestedquotes_singledouble(self):
        before = '<abbr title=\'laugh "out" loud\'>lol</abbr>'
        after = before
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_unclosed_b(self):
        before = ("<b>test")
        after = ("<b>test</b>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_unclosed_b_wrapped(self):
        before = ("This is a <b>test")
        after = ("This is a <b>test</b>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_unclosed_li(self):
        before = ("<ul><li>test</ul>")
        after = ("<ul><li>test</li></ul>")
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_malformed_faketag(self):
        before = "<madonna"
        after = ""
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_correct_faketag(self):
        before = "<madonna>"
        after = "&lt;madonna&gt;"
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_malformed_tag(self):
        before = "<strong"
        after = ""
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_malformed_faketag_surrounded(self):
        before = "This is a <test of bleach"
        after = 'This is a'
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_malformed_tag_surrounded(self):
        before = "This is a <strong of bleach"
        after = "This is a"
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_less_than(self):
        before = "3 < 5"
        after = "3 &lt; 5"
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)

    def test_newlines_less_than_tight(self):
        before = "abc 3<5 def"
        after = "abc 3&lt;5 def"
        eq_(PurifiedTranslation(localized_string=before).__html__(), after)


class LinkifiedTranslationTest(TestCase):

    @patch('amo.urlresolvers.get_outgoing_url')
    def test_allowed_tags(self, get_outgoing_url_mock):
        get_outgoing_url_mock.return_value = 'http://external.url'
        s = u'<a href="http://example.com">bar</a>'
        x = LinkifiedTranslation(localized_string=s)
        eq_(x.__html__(),
            u'<a rel="nofollow" href="http://external.url">bar</a>')

    def test_forbidden_tags(self):
        s = u'<script>some naughty xss</script> <b>bold</b>'
        x = LinkifiedTranslation(localized_string=s)
        eq_(x.__html__(),
            '&lt;script&gt;some naughty xss&lt;/script&gt; '
            '&lt;b&gt;bold&lt;/b&gt;')


class NoLinksTranslationTest(TestCase):

    def test_allowed_tags(self):
        s = u'<b>bold text</b> or <code>code</code>'
        x = NoLinksTranslation(localized_string=s)
        eq_(x.__html__(),  u'<b>bold text</b> or <code>code</code>')

    def test_forbidden_tags(self):
        s = u'<script>some naughty xss</script>'
        x = NoLinksTranslation(localized_string=s)
        eq_(x.__html__(), '&lt;script&gt;some naughty xss&lt;/script&gt;')

    def test_links_stripped(self):
        # Link with markup.
        s = u'a <a href="http://example.com">link</a> with markup'
        x = NoLinksTranslation(localized_string=s)
        eq_(x.__html__(), u'a  with markup')

        # Text link.
        s = u'a text http://example.com link'
        x = NoLinksTranslation(localized_string=s)
        eq_(x.__html__(), u'a text  link')

        # Text link, markup link, allowed tags, forbidden tags and bad markup.
        s = (u'a <a href="http://example.com">link</a> with markup, a text '
             u'http://example.com link, <b>with allowed tags</b>, '
             u'<script>forbidden tags</script> and <http://bad.markup.com')
        x = NoLinksTranslation(localized_string=s)
        eq_(x.__html__(), u'a  with markup, a text  link, '
                          u'<b>with allowed tags</b>, '
                          u'&lt;script&gt;forbidden tags&lt;/script&gt; and')


class NoLinksNoMarkupTranslationTest(TestCase):

    def test_forbidden_tags(self):
        s = u'<script>some naughty xss</script> <b>bold</b>'
        x = NoLinksNoMarkupTranslation(localized_string=s)
        eq_(x.__html__(),
            '&lt;script&gt;some naughty xss&lt;/script&gt; '
            '&lt;b&gt;bold&lt;/b&gt;')

    def test_links_stripped(self):
        # Link with markup.
        s = u'a <a href="http://example.com">link</a> with markup'
        x = NoLinksNoMarkupTranslation(localized_string=s)
        eq_(x.__html__(), u'a  with markup')

        # Text link.
        s = u'a text http://example.com link'
        x = NoLinksNoMarkupTranslation(localized_string=s)
        eq_(x.__html__(), u'a text  link')

        # Text link, markup link, forbidden tags and bad markup.
        s = (u'a <a href="http://example.com">link</a> with markup, a text '
             u'http://example.com link, <b>with forbidden tags</b>, '
             u'<script>forbidden tags</script> and <http://bad.markup.com')
        x = NoLinksNoMarkupTranslation(localized_string=s)
        eq_(x.__html__(), u'a  with markup, a text  link, '
                          u'&lt;b&gt;with forbidden tags&lt;/b&gt;, '
                          u'&lt;script&gt;forbidden tags&lt;/script&gt; and')


def test_translation_bool():
    t = lambda s: Translation(localized_string=s)

    assert bool(t('text')) is True
    assert bool(t(' ')) is False
    assert bool(t('')) is False
    assert bool(t(None)) is False


def test_translation_unicode():
    t = lambda s: Translation(localized_string=s)

    eq_(unicode(t('hello')), 'hello')
    eq_(unicode(t(None)), '')


def test_widget_value_from_datadict():
    data = {'f_en-US': 'woo', 'f_de': 'herr', 'f_fr_delete': ''}
    actual = widgets.TransMulti().value_from_datadict(data, [], 'f')
    expected = {'en-US': 'woo', 'de': 'herr', 'fr': None}
    eq_(actual, expected)


def test_comparison_with_lazy():
    x = Translation(localized_string='xxx')
    lazy_u = lazy(lambda x: x, unicode)
    x == lazy_u('xxx')
    lazy_u('xxx') == x


def test_cache_key():
    # Test that we are not taking the db into account when building our
    # cache keys for django-cache-machine. See bug 928881.
    eq_(Translation._cache_key(1, 'default'),
        Translation._cache_key(1, 'slave'))

    # Test that we are using the same cache no matter what Translation class
    # we use.
    eq_(PurifiedTranslation._cache_key(1, 'default'),
        Translation._cache_key(1, 'default'))
    eq_(LinkifiedTranslation._cache_key(1, 'default'),
        Translation._cache_key(1, 'default'))
