import os
import random
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial, wraps
from urlparse import SplitResult, urlsplit, urlunsplit

from django import forms
from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import default_storage as storage
from django.db.models.signals import post_save
from django.forms.fields import Field
from django.http import SimpleCookie
from django.test.client import Client
from django.utils import translation

import caching
import elasticutils.contrib.django as elasticutils
import mock
import pyelasticsearch.exceptions as pyelasticsearch
import test_utils
import tower
from dateutil.parser import parse as dateutil_parser
from django_browserid.tests import mock_browserid
from nose.exc import SkipTest
from nose.tools import eq_
from pyquery import PyQuery as pq
from redisutils import mock_redis, reset_redis
from test_utils import RequestFactory
from waffle import cache_sample, cache_switch
from waffle.models import Flag, Sample, Switch

import amo
import mkt
from amo.urlresolvers import get_url_prefix, Prefixer, reverse, set_url_prefix
from constants.applications import DEVICE_TYPES
from lib.post_request_task import task as post_request_task
from mkt.access.acl import check_ownership
from mkt.access.models import Group, GroupUser
from mkt.constants import regions
from mkt.feed.indexers import (FeedAppIndexer, FeedBrandIndexer,
                               FeedCollectionIndexer)
from mkt.files.helpers import copyfileobj
from mkt.files.models import File, Platform
from mkt.prices.models import AddonPremium, Price, PriceCurrency
from mkt.site.fixtures import fixture
from mkt.translations.models import Translation
from mkt.users.models import UserProfile
from mkt.versions.models import Version
from mkt.webapps.models import Addon, Category, Webapp, WebappIndexer
from mkt.webapps.models import update_search_index as app_update_search_index
from mkt.webapps.tasks import unindex_webapps


# We might now have gettext available in jinja2.env.globals when running tests.
# It's only added to the globals when activating a language with tower (which
# is usually done in the middlewares). During tests, however, we might not be
# running middlewares, and thus not activating a language, and thus not
# installing gettext in the globals, and thus not have it in the context when
# rendering templates.
tower.activate('en')


def formset(*args, **kw):
    """
    Build up a formset-happy POST.

    *args is a sequence of forms going into the formset.
    prefix and initial_count can be set in **kw.
    """
    prefix = kw.pop('prefix', 'form')
    total_count = kw.pop('total_count', len(args))
    initial_count = kw.pop('initial_count', len(args))
    data = {prefix + '-TOTAL_FORMS': total_count,
            prefix + '-INITIAL_FORMS': initial_count}
    for idx, d in enumerate(args):
        data.update(('%s-%s-%s' % (prefix, idx, k), v)
                    for k, v in d.items())
    data.update(kw)
    return data


def initial(form):
    """Gather initial data from the form into a dict."""
    data = {}
    for name, field in form.fields.items():
        if form.is_bound:
            data[name] = form[name].data
        else:
            data[name] = form.initial.get(name, field.initial)
        # The browser sends nothing for an unchecked checkbox.
        if isinstance(field, forms.BooleanField):
            val = field.to_python(data[name])
            if not val:
                del data[name]
    return data


def assert_required(error_msg):
    eq_(error_msg, unicode(Field.default_error_messages['required']))


def check_links(expected, elements, selected=None, verify=True):
    """Useful for comparing an `expected` list of links against PyQuery
    `elements`. Expected format of links is a list of tuples, like so:

    [
        ('Home', '/'),
        ('Extensions', reverse('browse.extensions')),
        ...
    ]

    If you'd like to check if a particular item in the list is selected,
    pass as `selected` the title of the link.

    Links are verified by default.

    """
    for idx, item in enumerate(expected):
        # List item could be `(text, link)`.
        if isinstance(item, tuple):
            text, link = item
        # Or list item could be `link`.
        elif isinstance(item, basestring):
            text, link = None, item

        e = elements.eq(idx)
        if text is not None:
            eq_(e.text(), text)
        if link is not None:
            # If we passed an <li>, try to find an <a>.
            if not e.filter('a'):
                e = e.find('a')
            eq_(e.attr('href'), link)
            if verify and link != '#':
                eq_(Client().head(link, follow=True).status_code, 200,
                    '%r is dead' % link)
        if text is not None and selected is not None:
            e = e.filter('.selected, .sel') or e.parents('.selected, .sel')
            eq_(bool(e.length), text == selected)


class RedisTest(object):
    """Mixin for when you need to mock redis for testing."""

    def _pre_setup(self):
        self._redis = mock_redis()
        super(RedisTest, self)._pre_setup()

    def _post_teardown(self):
        super(RedisTest, self)._post_teardown()
        reset_redis(self._redis)


class TestClient(Client):

    def __getattr__(self, name):
        """
        Provides get_ajax, post_ajax, head_ajax methods etc in the
        test_client so that you don't need to specify the headers.
        """
        if name.endswith('_ajax'):
            method = getattr(self, name.split('_')[0])
            return partial(method, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        else:
            raise AttributeError


ES_patchers = [mock.patch('elasticutils.contrib.django', spec=True),
               mock.patch('mkt.webapps.tasks.WebappIndexer', spec=True),
               mock.patch('mkt.webapps.tasks.Reindexing',
                          spec=True, side_effect=lambda i: [i])]


def start_es_mock():
    for patch in ES_patchers:
        patch.start()


def stop_es_mock():
    for patch in ES_patchers:
        patch.stop()

    if hasattr(elasticutils, '_local') and hasattr(elasticutils._local, 'es'):
        delattr(elasticutils._local, 'es')


def mock_es(f):
    """
    Test decorator for mocking elasticsearch calls in ESTestCase if we don't
    care about ES results.
    """
    @wraps(f)
    def decorated(request, *args, **kwargs):
        start_es_mock()
        try:
            return f(request, *args, **kwargs)
        finally:
            stop_es_mock()
    return decorated


def days_ago(days):
    return datetime.now().replace(microsecond=0) - timedelta(days=days)


class MockEsMixin(object):
    mock_es = True

    @classmethod
    def setUpClass(cls):
        if cls.mock_es:
            start_es_mock()
        try:
            super(MockEsMixin, cls).setUpClass()
        except Exception:
            # We need to unpatch here because tearDownClass will not be
            # called.
            if cls.mock_es:
                stop_es_mock()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            super(MockEsMixin, cls).tearDownClass()
        finally:
            if cls.mock_es:
                stop_es_mock()


class MockBrowserIdMixin(object):

    def mock_browser_id(self):
        cache.clear()
        # Override django-cache-machine caching.base.TIMEOUT because it's
        # computed too early, before settings_test.py is imported.
        caching.base.TIMEOUT = settings.CACHE_COUNT_TIMEOUT

        real_login = self.client.login

        def fake_login(username, password):
            with mock_browserid(email=username):
                return real_login(username=username, password=password)

        self.client.login = fake_login


class TestCase(MockEsMixin, RedisTest, MockBrowserIdMixin,
               test_utils.TestCase):
    """Base class for all amo tests."""
    client_class = TestClient

    def shortDescription(self):
        # Stop nose using the test docstring and instead the test method name.
        pass

    def _post_teardown(self):
        amo.set_user(None)
        super(TestCase, self)._post_teardown()

    def _pre_setup(self):
        super(TestCase, self)._pre_setup()
        self.mock_browser_id()

    @contextmanager
    def activate(self, locale=None, app=None):
        """Active an app or a locale."""
        prefixer = old_prefix = get_url_prefix()
        old_app = old_prefix.app
        old_locale = translation.get_language()
        if locale:
            rf = test_utils.RequestFactory()
            prefixer = Prefixer(rf.get('/%s/' % (locale,)))
            translation.activate(locale)
        if app:
            prefixer.app = app
        set_url_prefix(prefixer)
        yield
        old_prefix.app = old_app
        set_url_prefix(old_prefix)
        translation.activate(old_locale)

    def assertNoFormErrors(self, response):
        """Asserts that no form in the context has errors.

        If you add this check before checking the status code of the response
        you'll see a more informative error.
        """
        # TODO(Kumar) liberate upstream to Django?
        if response.context is None:
            # It's probably a redirect.
            return
        if len(response.templates) == 1:
            tpl = [response.context]
        else:
            # There are multiple contexts so iter all of them.
            tpl = response.context
        for ctx in tpl:
            for k, v in ctx.iteritems():
                if isinstance(v, (forms.BaseForm, forms.formsets.BaseFormSet)):
                    if isinstance(v, forms.formsets.BaseFormSet):
                        # Concatenate errors from each form in the formset.
                        msg = '\n'.join(f.errors.as_text() for f in v.forms)
                    else:
                        # Otherwise, just return the errors for this form.
                        msg = v.errors.as_text()
                    msg = msg.strip()
                    if msg != '':
                        self.fail('form %r had the following error(s):\n%s'
                                  % (k, msg))
                    if hasattr(v, 'non_field_errors'):
                        self.assertEquals(v.non_field_errors(), [])
                    if hasattr(v, 'non_form_errors'):
                        self.assertEquals(v.non_form_errors(), [])

    def assertLoginRedirects(self, response, to, status_code=302):
        # Not using urlparams, because that escapes the variables, which
        # is good, but bad for assertRedirects which will fail.
        self.assert3xx(response,
            '%s?to=%s' % (reverse('users.login'), to), status_code)

    def assert3xx(self, response, expected_url, status_code=302,
                  target_status_code=200):
        """Asserts redirect and final redirect matches expected URL.

        Similar to Django's `assertRedirects` but skips the final GET
        verification for speed.

        """
        if hasattr(response, 'redirect_chain'):
            # The request was a followed redirect
            self.assertTrue(len(response.redirect_chain) > 0,
                "Response didn't redirect as expected: Response"
                " code was %d (expected %d)" %
                    (response.status_code, status_code))

            url, status_code = response.redirect_chain[-1]

            self.assertEqual(response.status_code, target_status_code,
                "Response didn't redirect as expected: Final"
                " Response code was %d (expected %d)" %
                    (response.status_code, target_status_code))

        else:
            # Not a followed redirect
            self.assertEqual(response.status_code, status_code,
                "Response didn't redirect as expected: Response"
                " code was %d (expected %d)" %
                    (response.status_code, status_code))
            url = response['Location']

        scheme, netloc, path, query, fragment = urlsplit(url)
        e_scheme, e_netloc, e_path, e_query, e_fragment = urlsplit(
            expected_url)
        if (scheme and not e_scheme) and (netloc and not e_netloc):
            expected_url = urlunsplit(('http', 'testserver', e_path, e_query,
                                       e_fragment))

        self.assertEqual(url, expected_url,
            "Response redirected to '%s', expected '%s'" % (url, expected_url))

    def assertLoginRequired(self, response, status_code=302):
        """
        A simpler version of assertLoginRedirects that just checks that we
        get the matched status code and bounced to the correct login page.
        """
        assert response.status_code == status_code, (
                'Response returned: %s, expected: %s'
                % (response.status_code, status_code))

        path = urlsplit(response['Location'])[2]
        assert path == reverse('users.login'), (
                'Redirected to: %s, expected: %s'
                % (path, reverse('users.login')))

    def assertSetEqual(self, a, b, message=None):
        """
        This is a thing in unittest in 2.7,
        but until then this is the thing.

        Oh, and Django's `assertSetEqual` is lame and requires actual sets:
        http://bit.ly/RO9sTr
        """
        eq_(set(a), set(b), message)
        eq_(len(a), len(b), message)

    def assertCloseToNow(self, dt, now=None):
        """
        Make sure the datetime is within a minute from `now`.
        """

        # Try parsing the string if it's not a datetime.
        if isinstance(dt, basestring):
            try:
                dt = dateutil_parser(dt)
            except ValueError, e:
                raise AssertionError(
                    'Expected valid date; got %s\n%s' % (dt, e))

        if not dt:
            raise AssertionError('Expected datetime; got %s' % dt)

        dt_later_ts = time.mktime((dt + timedelta(minutes=1)).timetuple())
        dt_earlier_ts = time.mktime((dt - timedelta(minutes=1)).timetuple())
        if not now:
            now = datetime.now()
        now_ts = time.mktime(now.timetuple())

        assert dt_earlier_ts < now_ts < dt_later_ts, (
            'Expected datetime to be within a minute of %s. Got %r.' % (now,
                                                                        dt))

    def assertQuerySetEqual(self, qs1, qs2):
        """
        Assertion to check the equality of two querysets
        """
        return self.assertSetEqual(qs1.values_list('id', flat=True),
                                   qs2.values_list('id', flat=True))

    def assertCORS(self, res, *verbs):
        """
        Determines if a response has suitable CORS headers. Appends 'OPTIONS'
        on to the list of verbs.
        """
        eq_(res['Access-Control-Allow-Origin'], '*')
        assert 'API-Status' in res['Access-Control-Expose-Headers']
        assert 'API-Version' in res['Access-Control-Expose-Headers']

        verbs = map(str.upper, verbs) + ['OPTIONS']
        actual = res['Access-Control-Allow-Methods'].split(', ')
        self.assertSetEqual(verbs, actual)
        eq_(res['Access-Control-Allow-Headers'],
            'X-HTTP-Method-Override, Content-Type')

    def assertApiUrlEqual(self, *args, **kwargs):
        """
        Allows equality comparison of two or more URLs agnostic of API version.
        This is done by prepending '/api/vx' (where x is equal to the `version`
        keyword argument or API_CURRENT_VERSION) to each string passed as a
        positional argument if that URL doesn't already start with that string.
        Also accepts 'netloc' and 'scheme' optional keyword arguments to
        compare absolute URLs.

        Example usage:

        url = '/api/v1/apps/app/bastacorp/'
        self.assertApiUrlEqual(url, '/apps/app/bastacorp1/')

        # settings.API_CURRENT_VERSION = 2
        url = '/api/v1/apps/app/bastacorp/'
        self.assertApiUrlEqual(url, '/apps/app/bastacorp/', version=1)
        """
        # Constants for the positions of the URL components in the tuple
        # returned by urlsplit. Only here for readability purposes.
        SCHEME = 0
        NETLOC = 1
        PATH = 2

        version = kwargs.get('version', settings.API_CURRENT_VERSION)
        scheme = kwargs.get('scheme', None)
        netloc = kwargs.get('netloc', None)
        urls = list(args)
        prefix = '/api/v%d' % version
        for idx, url in enumerate(urls):
            urls[idx] = list(urlsplit(url))
            if not urls[idx][PATH].startswith(prefix):
                urls[idx][PATH] = prefix + urls[idx][PATH]
            if scheme and not urls[idx][SCHEME]:
                urls[idx][SCHEME] = scheme
            if netloc and not urls[idx][NETLOC]:
                urls[idx][NETLOC] = netloc
            urls[idx] = SplitResult(*urls[idx])
        eq_(*urls)

    def update_session(self, session):
        """
        Update the session on the client. Needed if you manipulate the session
        in the test. Needed when we use signed cookies for sessions.
        """
        cookie = SimpleCookie()
        cookie[settings.SESSION_COOKIE_NAME] = session._get_session_key()
        self.client.cookies.update(cookie)

    def make_price(self, price='1.00'):
        price_obj, created = Price.objects.get_or_create(price=price,
                                                         name='1')
        for region in [regions.US.id, regions.RESTOFWORLD.id]:
            PriceCurrency.objects.create(region=region, currency='USD',
                                         price=price, tier=price_obj,
                                         provider=1)
        return price_obj

    def make_premium(self, addon, price='1.00'):
        price_obj = self.make_price(price=Decimal(price))
        addon.update(premium_type=amo.ADDON_PREMIUM)
        addon._premium = AddonPremium.objects.create(addon=addon,
                                                     price=price_obj)
        if hasattr(Price, '_currencies'):
            del Price._currencies
        return addon._premium

    def create_sample(self, name=None, db=False, **kw):
        if name is not None:
            kw['name'] = name
        kw.setdefault('percent', 100)
        sample = Sample(**kw)
        sample.save() if db else cache_sample(instance=sample)
        return sample

    def create_switch(self, name=None, db=False, **kw):
        kw.setdefault('active', True)
        if name is not None:
            kw['name'] = name
        switch = Switch(**kw)
        switch.save() if db else cache_switch(instance=switch)
        return switch

    def create_flag(self, name=None, **kw):
        if name is not None:
            kw['name'] = name
        kw.setdefault('everyone', True)
        return Flag.objects.create(**kw)

    def grant_permission(self, user_obj, rules, name='Test Group'):
        """Creates group with rule, and adds user to group."""
        group = Group.objects.create(name=name, rules=rules)
        GroupUser.objects.create(group=group, user=user_obj)

    def days_ago(self, days):
        return days_ago(days)

    def login(self, profile):
        email = getattr(profile, 'email', profile)
        if '@' not in email:
            email += '@mozilla.com'
        assert self.client.login(username=email, password='password')

    def trans_eq(self, trans, locale, localized_string):
        eq_(Translation.objects.get(id=trans.id,
                                    locale=locale).localized_string,
            localized_string)

    def extract_script_template(self, html, template_selector):
        """Extracts the inner JavaScript text/template from a html page.

        Example::

            >>> template = extract_script_template(res.content, '#template-id')
            >>> template('#my-jquery-selector')

        Returns a PyQuery object that you can refine using jQuery selectors.
        """
        return pq(pq(html)(template_selector).html())


class AMOPaths(object):
    """Mixin for getting common AMO Paths."""

    def file_fixture_path(self, name):
        path = 'mkt/files/fixtures/files/%s' % name
        return os.path.join(settings.ROOT, path)

    def xpi_path(self, name):
        if os.path.splitext(name)[-1] not in ['.xml', '.xpi', '.jar']:
            return self.file_fixture_path(name + '.xpi')
        return self.file_fixture_path(name)

    def manifest_path(self, name):
        return os.path.join(settings.ROOT,
                            'mkt/submit/tests/webapps/%s' % name)

    def manifest_copy_over(self, dest, name):
        with storage.open(dest, 'wb') as f:
            copyfileobj(open(self.manifest_path(name)), f)

    @staticmethod
    def sample_key():
        return os.path.join(settings.ROOT,
                            'mkt/webapps/tests/sample.key')

    def sample_packaged_key(self):
        return os.path.join(settings.ROOT,
                            'mkt/webapps/tests/sample.packaged.pem')

    def mozball_image(self):
        return os.path.join(settings.ROOT,
                            'mkt/developers/tests/addons/mozball-128.png')

    def preview_image(self):
        return os.path.join(settings.ROOT,
                            'apps/amo/tests/images/preview.jpg')

    def packaged_app_path(self, name):
        return os.path.join(
            settings.ROOT, 'mkt/submit/tests/packaged/%s' % name)

    def packaged_copy_over(self, dest, name):
        with storage.open(dest, 'wb') as f:
            copyfileobj(open(self.packaged_app_path(name)), f)


def assert_no_validation_errors(validation):
    """Assert that the validation (JSON) does not contain a traceback.

    Note that this does not test whether the addon passed
    validation or not.
    """
    if hasattr(validation, 'task_error'):
        # FileUpload object:
        error = validation.task_error
    else:
        # Upload detail - JSON output
        error = validation['error']
    if error:
        print '-' * 70
        print error
        print '-' * 70
        raise AssertionError("Unexpected task error: %s" %
                             error.rstrip().split("\n")[-1])


def _get_created(created):
    """
    Returns a datetime.

    If `created` is "now", it returns `datetime.datetime.now()`. If `created`
    is set use that. Otherwise generate a random datetime in the year 2011.
    """
    if created == 'now':
        return datetime.now()
    elif created:
        return created
    else:
        return datetime(2011,
                        random.randint(1, 12),  # Month
                        random.randint(1, 28),  # Day
                        random.randint(0, 23),  # Hour
                        random.randint(0, 59),  # Minute
                        random.randint(0, 59))  # Seconds


def addon_factory(status=amo.STATUS_PUBLIC, version_kw={}, file_kw={}, **kw):
    # Disconnect signals until the last save.
    post_save.disconnect(app_update_search_index, sender=Webapp,
                         dispatch_uid='webapp.search.index')

    type_ = kw.pop('type', amo.ADDON_EXTENSION)
    popularity = kw.pop('popularity', None)
    when = _get_created(kw.pop('created', None))

    # Keep as much unique data as possible in the uuid: '-' aren't important.
    name = kw.pop('name', u'Addon %s' % unicode(uuid.uuid4()).replace('-', ''))

    kwargs = {
        # Set artificially the status to STATUS_PUBLIC for now, , the real
        # status will be set a few lines below, after the update_version()
        # call. This prevents issues when calling addon_factory with
        # STATUS_DELETED.
        'status': amo.STATUS_PUBLIC,
        'name': name,
        'slug': name.replace(' ', '-').lower()[:30],
        'bayesian_rating': random.uniform(1, 5),
        'weekly_downloads': popularity or random.randint(200, 2000),
        'created': when,
        'last_updated': when,
    }
    kwargs.update(kw)

    # Save 1.
    a = Addon.objects.create(type=type_, **kwargs)
    version = version_factory(file_kw, addon=a, **version_kw)  # Save 2.
    a.update_version()
    a.status = status

    # Put signals back.
    post_save.connect(app_update_search_index, sender=Webapp,
                      dispatch_uid='webapp.search.index')

    a.save()  # Save 4.

    if 'nomination' in version_kw:
        # If a nomination date was set on the version, then it might have been
        # erased at post_save by addons.models.watch_status() or
        # mkt.webapps.models.watch_status().
        version.save()
    return a


def app_factory(**kw):
    """
    Create an app. Keyword arguments are passed to addon_factory.

    complete -- fills out app details + creates content ratings.
    rated -- creates content ratings
    """
    complete = kw.pop('complete', False)
    rated = kw.pop('rated', False)
    if complete:
        kw.setdefault('support_email', 'support@example.com')

    kw.update(type=amo.ADDON_WEBAPP)
    app = amo.tests.addon_factory(**kw)

    if rated or complete:
        make_rated(app)

    if complete:
        cat, _ = Category.objects.get_or_create(slug='utilities',
                                                type=amo.ADDON_WEBAPP)
        app.addoncategory_set.create(category=cat)
        app.addondevicetype_set.create(device_type=DEVICE_TYPES.keys()[0])
        app.previews.create()

    return app


def file_factory(**kw):
    v = kw['version']
    p, _ = Platform.objects.get_or_create(id=amo.PLATFORM_ALL.id)
    status = kw.pop('status', amo.STATUS_PUBLIC)
    f = File.objects.create(filename='%s-%s' % (v.addon_id, v.id),
                            platform=p, status=status, **kw)
    return f


def req_factory_factory(url, user=None, post=False, data=None):
    """Creates a request factory, logged in with the user."""
    req = RequestFactory()
    if post:
        req = req.post(url, data or {})
    else:
        req = req.get(url, data or {})
    if user:
        req.amo_user = UserProfile.objects.get(id=user.id)
        req.user = user
        req.groups = user.groups.all()
    req.check_ownership = partial(check_ownership, req)
    return req


user_factory_counter = 0


def user_factory(**kw):
    global user_factory_counter
    username = kw.pop('username', 'factoryuser%d' % user_factory_counter)

    user = UserProfile.objects.create(
        username=username, email='%s@mozilla.com' % username, **kw)

    if 'username' not in kw:
        user_factory_counter = user.id + 1
    return user


def version_factory(file_kw={}, **kw):
    version = kw.pop('version', '%.1f' % random.uniform(0, 2))
    v = Version.objects.create(version=version, **kw)
    v.created = v.last_updated = _get_created(kw.pop('created', 'now'))
    v.save()
    file_factory(version=v, **file_kw)
    return v


class ESTestCase(TestCase):
    """Base class for tests that require elasticsearch."""
    # ES is slow to set up so this uses class setup/teardown. That happens
    # outside Django transactions so be careful to clean up afterwards.
    test_es = True
    mock_es = False
    exempt_from_fixture_bundling = True  # ES doesn't support bundling (yet?)

    @classmethod
    def setUpClass(cls):
        if not settings.RUN_ES_TESTS:
            raise SkipTest('ES disabled')
        cls.es = elasticutils.get_es(timeout=settings.ES_TIMEOUT)

        # The ES setting are set before we call super()
        # because we may have indexation occuring in upper classes.
        for key, index in settings.ES_INDEXES.items():
            if not index.startswith('test_'):
                settings.ES_INDEXES[key] = 'test_%s' % index

        super(ESTestCase, cls).setUpClass()
        try:
            cls.es.health()
        except Exception, e:
            e.args = tuple([u'%s (it looks like ES is not running, '
                            'try starting it or set RUN_ES_TESTS=False)'
                            % e.args[0]] + list(e.args[1:]))
            raise

        cls._SEARCH_ANALYZER_MAP = amo.SEARCH_ANALYZER_MAP
        amo.SEARCH_ANALYZER_MAP = {
            'english': ['en-us'],
            'spanish': ['es'],
        }

        for index in set(settings.ES_INDEXES.values()):
            # Get the index that's pointed to by the alias.
            try:
                indices = cls.es.aliases(index)
                assert indices[index]['aliases']
            except (KeyError, AssertionError):
                # There's no alias, just use the index.
                print 'Found no alias for %s.' % index
            except pyelasticsearch.ElasticHttpNotFoundError:
                pass

            # Remove any alias as well.
            try:
                cls.es.delete_index(index)
            except pyelasticsearch.ElasticHttpNotFoundError as exc:
                print 'Could not delete index %r: %s' % (index, exc)

        for indexer in (WebappIndexer, FeedAppIndexer, FeedBrandIndexer,
                        FeedCollectionIndexer):
            indexer.setup_mapping()

    @classmethod
    def tearDownClass(cls):
        try:
            if hasattr(cls, '_addons'):
                Addon.objects.filter(
                    pk__in=[a.id for a in cls._addons]).delete()
                unindex_webapps([a.id for a in cls._addons
                                 if a.type == amo.ADDON_WEBAPP])
            amo.SEARCH_ANALYZER_MAP = cls._SEARCH_ANALYZER_MAP
        finally:
            # Make sure we're calling super's tearDownClass even if something
            # went wrong in the code above, as otherwise we'd run into bug
            # 960598.
            super(ESTestCase, cls).tearDownClass()

    def tearDown(self):
        post_request_task._send_tasks()

    @classmethod
    def setUpIndex(cls):
        cls.refresh()

    @classmethod
    def refresh(cls, index='webapp', timesleep=0):
        post_request_task._send_tasks()
        cls.es.refresh(settings.ES_INDEXES[index])

    @classmethod
    def reindex(cls, model, index='default'):
        # Emit post-save signal so all of the objects get reindexed.
        [o.save() for o in model.objects.all()]
        cls.refresh(index)


class WebappTestCase(TestCase):
    fixtures = fixture('webapp_337141')

    def setUp(self):
        self.app = self.get_app()

    def get_app(self):
        return Addon.objects.get(id=337141)

    def make_game(self, app=None, rated=False):
        app = make_game(self.app or app, rated)


def make_game(app, rated):
    cat, created = Category.objects.get_or_create(slug='games',
        type=amo.ADDON_WEBAPP)
    app.addoncategory_set.create(category=cat)
    if rated:
        make_rated(app)
    app = app.reload()
    return app


def make_rated(app):
    app.set_content_ratings(
        dict((body, body.ratings[0]) for body in
        mkt.ratingsbodies.ALL_RATINGS_BODIES))
    app.set_iarc_info(123, 'abc')
    app.set_descriptors([])
    app.set_interactives([])
