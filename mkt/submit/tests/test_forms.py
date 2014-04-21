from django.forms.fields import BooleanField
from django.utils.translation import ugettext_lazy as _

import mock
from nose.tools import eq_, ok_
from test_utils import RequestFactory

import amo
import amo.tests
from devhub.models import AppLog
from editors.models import RereviewQueue
from files.models import FileUpload
from users.models import UserProfile

import mkt.constants
from mkt.site.fixtures import fixture
from mkt.submit import forms
from mkt.webapps.models import AppFeatures, Webapp


class TestNewWebappForm(amo.tests.TestCase):

    def setUp(self):
        self.request = RequestFactory().get('/')
        self.file = FileUpload.objects.create(valid=True)
        self.platform = mkt.PLATFORM_FXOS
        self.form_factor = mkt.FORM_MOBILE

    def data(self, **kw):
        d = {
            'payment': 'free',
            'app_type': 'hosted',
            'platform': [self.platform.id],
            'form_factor': [self.form_factor.id],
            'upload': self.file.uuid,
        }
        d.update(**kw)
        return d

    def test_missing_fields(self):
        form = forms.NewWebappForm({})
        assert not form.is_valid()
        eq_(form.errors['app_type'], [u'This field is required.'])
        eq_(form.errors['payment'], [u'This field is required.'])
        eq_(form.errors['form_factor'], [u'This field is required.'])
        eq_(form.errors['platform'], [u'This field is required.'])

    def test_payment(self):
        for payment in ('free', 'paid'):
            form = forms.NewWebappForm(self.data(payment=payment))
            assert form.is_valid(), form.errors
            eq_(form.cleaned_data['payment'], payment)

    def test_platform(self):
        self.create_flag('android-packaged')
        self.create_flag('desktop-packaged')
        for platform in mkt.PLATFORM_LIST:
            form = forms.NewWebappForm(self.data(platform=[platform.id]))
            assert form.is_valid(), form.errors
            eq_(form.cleaned_data['platform'], [platform])

    def test_form_factor(self):
        self.create_flag('android-packaged')
        self.create_flag('desktop-packaged')
        for ff in mkt.FORM_FACTORS:
            form = forms.NewWebappForm(self.data(form_factor=[ff.id]))
            assert form.is_valid(), form.errors
            eq_(form.cleaned_data['form_factor'], [ff])

    @mock.patch('mkt.submit.forms.parse_addon',
                lambda *args: {'version': None})
    def test_packaged_disallowed(self):
        for platform in (mkt.PLATFORM_DESKTOP, mkt.PLATFORM_ANDROID):
            form = forms.NewWebappForm(self.data(platform=[platform.id],
                                                 app_type='packaged'))
            assert not form.is_valid(), form.errors
            eq_(form.errors['platform'],
                'Packaged apps are not yet supported for those platforms.')

    @mock.patch('mkt.submit.forms.parse_addon',
                lambda *args: {'version': None})
    def test_packaged_allowed_everywhere(self):
        self.create_flag('android-packaged')
        self.create_flag('desktop-packaged')
        for platform in mkt.PLATFORM_LIST:
            form = forms.NewWebappForm(self.data(platform=[platform.id],
                                                 app_type='packaged'))
            assert form.is_valid(), form.errors


class TestNewWebappVersionForm(amo.tests.TestCase):

    def setUp(self):
        self.request = RequestFactory().get('/')
        self.file = FileUpload.objects.create(valid=True)

    def test_no_upload(self):
        form = forms.NewWebappVersionForm(request=self.request,
                                          is_packaged=True)
        assert not form.is_valid(), form.errors

    @mock.patch('mkt.submit.forms.parse_addon',
                lambda *args: {"origin": "app://hy.fr"})
    @mock.patch('mkt.submit.forms.verify_app_domain')
    def test_verify_app_domain_called(self, _verify):
        self.create_switch('webapps-unique-by-domain')
        form = forms.NewWebappVersionForm({'upload': self.file.uuid},
                                          request=self.request,
                                          is_packaged=True)
        assert form.is_valid(), form.errors
        assert _verify.called

    @mock.patch('mkt.submit.forms.parse_addon',
                lambda *args: {"origin": "app://hy.fr"})
    def test_verify_app_domain_exclude_same(self):
        app = amo.tests.app_factory(app_domain='app://hy.fr')
        form = forms.NewWebappVersionForm(
            {'upload': self.file.uuid}, request=self.request, is_packaged=True,
            addon=app)
        assert form.is_valid(), form.errors

    @mock.patch('mkt.submit.forms.parse_addon',
                lambda *args: {"origin": "app://hy.fr"})
    def test_verify_app_domain_exclude_different(self):
        app = amo.tests.app_factory(app_domain='app://yo.lo')
        amo.tests.app_factory(app_domain='app://hy.fr')
        form = forms.NewWebappVersionForm(
            {'upload': self.file.uuid}, request=self.request, is_packaged=True,
            addon=app)
        assert not form.is_valid(), form.errors
        assert 'An app already exists' in ''.join(form.errors['upload'])


class TestAppDetailsBasicForm(amo.tests.TestCase):
    fixtures = fixture('user_999', 'webapp_337141')

    def setUp(self):
        self.request = mock.Mock()
        self.request.amo_user = UserProfile.objects.get(id=999)

    def test_slug(self):
        app = Webapp.objects.get(pk=337141)
        data = {
            'app_slug': 'thisIsAslug',
            'description': '.',
            'privacy_policy': '.',
            'support_email': 'test@example.com',
        }
        form = forms.AppDetailsBasicForm(data, request=self.request,
                                         instance=app)
        assert form.is_valid()
        form.save()
        eq_(app.app_slug, 'thisisaslug')


class TestAppFeaturesForm(amo.tests.TestCase):
    fixtures = fixture('user_999', 'webapp_337141')

    def setUp(self):
        amo.set_user(UserProfile.objects.all()[0])
        self.form = forms.AppFeaturesForm()
        self.app = Webapp.objects.get(pk=337141)
        self.features = self.app.current_version.features

    def _check_log(self, action):
        assert AppLog.objects.filter(
            addon=self.app, activity_log__action=action.id).exists(), (
                "Didn't find `%s` action in logs." % action.short)

    def test_required(self):
        f_names = self.form.fields.keys()
        for value in (True, False):
            form = forms.AppFeaturesForm(dict((n, value) for n in f_names))
            eq_(form.is_valid(), True, form.errors)

    def test_correct_fields(self):
        fields = self.form.fields
        f_values = fields.values()
        assert 'version' not in fields
        assert all(isinstance(f, BooleanField) for f in f_values)
        self.assertSetEqual(fields, AppFeatures()._fields())

    def test_required_api_fields(self):
        fields = [f.help_text for f in self.form.required_api_fields()]
        eq_(fields, sorted(f['name'] for f in
                           mkt.constants.APP_FEATURES.values()))

    def test_required_api_fields_nonascii(self):
        forms.AppFeaturesForm.base_fields['has_apps'].help_text = _(
            u'H\xe9llo')
        fields = [f.help_text for f in self.form.required_api_fields()]
        eq_(fields, sorted(f['name'] for f in
                           mkt.constants.APP_FEATURES.values()))

    def test_changes_mark_for_rereview(self):
        self.features.update(has_sms=True)
        data = {'has_apps': True}
        self.form = forms.AppFeaturesForm(instance=self.features, data=data)
        self.form.save()
        ok_(self.features.has_apps)
        ok_(not self.features.has_sms)
        ok_(not self.features.has_contacts)
        action_id = amo.LOG.REREVIEW_FEATURES_CHANGED.id
        assert AppLog.objects.filter(addon=self.app,
            activity_log__action=action_id).exists()
        eq_(RereviewQueue.objects.count(), 1)

    def test_no_changes_not_marked_for_rereview(self):
        self.features.update(has_sms=True)
        data = {'has_sms': True}
        self.form = forms.AppFeaturesForm(instance=self.features, data=data)
        self.form.save()
        ok_(not self.features.has_apps)
        ok_(self.features.has_sms)
        eq_(RereviewQueue.objects.count(), 0)
        action_id = amo.LOG.REREVIEW_FEATURES_CHANGED.id
        assert not AppLog.objects.filter(addon=self.app,
             activity_log__action=action_id).exists()

    def test_changes_mark_for_rereview_bypass(self):
        self.features.update(has_sms=True)
        data = {'has_apps': True}
        self.form = forms.AppFeaturesForm(instance=self.features, data=data)
        self.form.save(mark_for_rereview=False)
        ok_(self.features.has_apps)
        ok_(not self.features.has_sms)
        eq_(RereviewQueue.objects.count(), 0)
        action_id = amo.LOG.REREVIEW_FEATURES_CHANGED.id
        assert not AppLog.objects.filter(addon=self.app,
             activity_log__action=action_id).exists()
