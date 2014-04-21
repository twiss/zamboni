import datetime
import os
from collections import defaultdict

from django import forms
from django.conf import settings

import basket
import happyforms
import waffle
from tower import ugettext as _, ugettext_lazy as _lazy

import amo
from addons.models import Addon, AddonUpsell, BlacklistedSlug, Webapp
from amo.utils import slug_validator
from apps.users.models import UserNotification
from apps.users.notifications import app_surveys
from editors.models import RereviewQueue
from files.models import FileUpload
from files.utils import parse_addon
from market.models import AddonPremium, Price
from translations.fields import TransField
from translations.forms import TranslationFormMixin
from translations.widgets import TransInput, TransTextarea

import mkt
from mkt.constants import APP_FEATURES
from mkt.developers.forms import verify_app_domain
from mkt.site.forms import AddonChoiceField, APP_PUBLIC_CHOICES
from mkt.webapps.models import AppFeatures


def mark_for_rereview(addon, added_platforms, removed_platforms):
    msg = _(u'Platform(s) changed: {0}').format(', '.join(
        [_(u'Added {0}').format(unicode(mkt.PLATFORM_TYPES[p].name))
         for p in added_platforms] +
        [_(u'Removed {0}').format(unicode(mkt.PLATFORM_TYPES[p].name))
         for p in removed_platforms]))
    RereviewQueue.flag(addon, amo.LOG.REREVIEW_PLATFORMS_ADDED, msg)


def mark_for_rereview_form_factors(addon, added, removed):
    msg = _(u'Form Factor(s) changed: {0}').format(', '.join(
        [_(u'Added {0}').format(unicode(mkt.FORM_FACTOR_CHOICES[f].name))
         for f in added] +
        [_(u'Removed {0}').format(unicode(mkt.FORM_FACTOR_CHOICES[f].name))
         for f in removed]))
    RereviewQueue.flag(addon, amo.LOG.REREVIEW_FORM_FACTORS_ADDED, msg)


def mark_for_rereview_features_change(addon, added_features, removed_features):
    # L10n: {0} is the list of requirements changes.
    msg = _(u'Requirements changed: {0}').format(', '.join(
        [_(u'Added {0}').format(f) for f in added_features] +
        [_(u'Removed {0}').format(f) for f in removed_features]))
    RereviewQueue.flag(addon, amo.LOG.REREVIEW_FEATURES_CHANGED, msg)


APP_TYPE_CHOICES = [('hosted', 'hosted'), ('packaged', 'packaged')]
PAYMENT_CHOICES = [('free', 'free'), ('paid', 'paid')]
PLATFORM_CHOICES = [(p.id, p.slug) for p in mkt.PLATFORM_LIST]
FORM_FACTOR_CHOICES = [(ff.id, ff.slug) for ff in mkt.FORM_FACTORS]


class CompatibilityForm(happyforms.Form):
    """
    Form that handles app compatibility related options.

    This is used during app submission and app editing.

    """
    app_type = forms.ChoiceField(choices=APP_TYPE_CHOICES)
    payment = forms.ChoiceField(choices=PAYMENT_CHOICES)
    form_factor = forms.MultipleChoiceField(choices=FORM_FACTOR_CHOICES)
    platform = forms.MultipleChoiceField(choices=PLATFORM_CHOICES)

    def clean_platform(self):
        return [mkt.PLATFORM_TYPES.get(p)
                for p in map(int, self.cleaned_data.get('platform'))]

    def clean_form_factor(self):
        return [mkt.FORM_FACTOR_CHOICES.get(ff)
                for ff in map(int, self.cleaned_data.get('form_factor'))]

    def clean(self):
        data = super(CompatibilityForm, self).clean()
        if not data:
            return

        # Add packaged-app submission errors for incompatible platforms.
        bad_android = (
            self.is_packaged() and
            not waffle.flag_is_active(self.request, 'android-packaged') and
            mkt.PLATFORM_ANDROID in data.get('platform', [])
        )
        bad_desktop = (
            self.is_packaged() and
            not waffle.flag_is_active(self.request, 'desktop-packaged')
            and mkt.PLATFORM_DESKTOP in data.get('platform', [])
        )
        if bad_android or bad_desktop:
            self._errors['platform'] = _(
                u'Packaged apps are not yet supported for those platforms.')

        return data

    def is_packaged(self):
        return bool(self.cleaned_data.get('app_type') == 'packaged')

    def is_paid(self):
        return bool(self.cleaned_data.get('payment') == 'paid')

    def get_paid(self):
        """
        Returns the premium type.

        Should not be used if the form is used to modify an existing app.

        """
        return amo.ADDON_PREMIUM if self.is_paid() else amo.ADDON_FREE

    def save(self, addon=None):

        # Addon is passed via the submit flow. If not provided, look for it on
        # `self` which is used on the edit flow. If we can't find it there,
        # bail out.
        if not addon:
            addon = getattr(self, 'addon', None)
            if not addon:
                return

        # Update platforms, send to re-review if new platforms added.
        new = set(p.id for p in self.cleaned_data.get('platform'))
        old = set(p.id for p in addon.platforms)

        added_platforms = new - old
        removed_platforms = old - new

        for p in added_platforms:
            addon.platform_set.create(platform_id=p)
        addon.platform_set.filter(platform_id__in=removed_platforms).delete()

        # Send app to re-review queue if public and new platforms are added.
        if added_platforms and addon.status in amo.WEBAPPS_APPROVED_STATUSES:
            mark_for_rereview(addon, added_platforms, removed_platforms)

        # Update form factors.
        addon.form_factor_set.all().delete()
        for ff in self.cleaned_data.get('form_factor'):
            addon.form_factor_set.safer_get_or_create(
                form_factor_id=ff.id)


class DevAgreementForm(happyforms.Form):
    read_dev_agreement = forms.BooleanField(label=_lazy(u'Agree and Continue'),
                                            widget=forms.HiddenInput)
    newsletter = forms.BooleanField(required=False, label=app_surveys.label,
                                    widget=forms.CheckboxInput)

    def __init__(self, *args, **kw):
        self.instance = kw.pop('instance')
        self.request = kw.pop('request')
        super(DevAgreementForm, self).__init__(*args, **kw)

    def save(self):
        self.instance.read_dev_agreement = datetime.datetime.now()
        self.instance.save()
        if self.cleaned_data.get('newsletter'):
            UserNotification.update_or_create(user=self.instance,
                notification_id=app_surveys.id, update={'enabled': True})
            basket.subscribe(self.instance.email,
                             'app-dev',
                             format='H',
                             country=self.request.REGION.slug,
                             lang=self.request.LANG,
                             source_url=os.path.join(settings.SITE_URL,
                                                     'developers/submit'))


class NewWebappVersionForm(happyforms.Form):
    upload_error = _lazy(u'There was an error with your upload. '
                         u'Please try again.')
    upload = forms.ModelChoiceField(widget=forms.HiddenInput,
        queryset=FileUpload.objects.filter(valid=True),
        error_messages={'invalid_choice': upload_error})

    def __init__(self, *args, **kw):
        request = kw.pop('request', None)
        self.addon = kw.pop('addon', None)
        self._is_packaged = kw.pop('is_packaged', False)
        super(NewWebappVersionForm, self).__init__(*args, **kw)

        if (not waffle.flag_is_active(request, 'allow-b2g-paid-submission')
            and 'paid_platforms' in self.fields):
            del self.fields['paid_platforms']

    def clean(self):
        data = self.cleaned_data
        if 'upload' not in self.cleaned_data:
            self._errors['upload'] = self.upload_error
            return

        if self.is_packaged():
            # Now run the packaged app check, done in clean, because
            # clean_packaged needs to be processed first.
            try:
                pkg = parse_addon(data['upload'], self.addon)
            except forms.ValidationError, e:
                self._errors['upload'] = self.error_class(e.messages)
                return

            ver = pkg.get('version')
            if (ver and self.addon and
                self.addon.versions.filter(version=ver).exists()):
                self._errors['upload'] = _(u'Version %s already exists') % ver
                return

            origin = pkg.get('origin')
            if origin:
                try:
                    origin = verify_app_domain(origin, packaged=True,
                                               exclude=self.addon)
                except forms.ValidationError, e:
                    self._errors['upload'] = self.error_class(e.messages)
                    return
                if origin:
                    data['origin'] = origin

        else:
            # Throw an error if this is a dupe.
            # (JS sets manifest as `upload.name`.)
            try:
                verify_app_domain(data['upload'].name)
            except forms.ValidationError, e:
                self._errors['upload'] = self.error_class(e.messages)
                return

        return data

    def is_packaged(self):
        return self._is_packaged


class NewWebappForm(CompatibilityForm, NewWebappVersionForm):
    upload = forms.ModelChoiceField(widget=forms.HiddenInput,
        queryset=FileUpload.objects.filter(valid=True),
        error_messages={'invalid_choice': _lazy(
            u'There was an error with your upload. Please try again.')})

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super(NewWebappForm, self).__init__(*args, **kwargs)


class UpsellForm(happyforms.Form):
    price = forms.ModelChoiceField(queryset=Price.objects.active(),
                                   label=_lazy(u'App Price'),
                                   empty_label=None,
                                   required=True)
    make_public = forms.TypedChoiceField(choices=APP_PUBLIC_CHOICES,
                                    widget=forms.RadioSelect(),
                                    label=_lazy(u'When should your app be '
                                                 'made available for sale?'),
                                    coerce=int,
                                    required=False)
    free = AddonChoiceField(queryset=Addon.objects.none(),
        required=False, empty_label='',
        # L10n: "App" is a paid version of this app. "from" is this app.
        label=_lazy(u'App to upgrade from'),
        widget=forms.Select())

    def __init__(self, *args, **kw):
        self.extra = kw.pop('extra')
        self.request = kw.pop('request')
        self.addon = self.extra['addon']

        if 'initial' not in kw:
            kw['initial'] = {}

        kw['initial']['make_public'] = amo.PUBLIC_IMMEDIATELY
        if self.addon.premium:
            kw['initial']['price'] = self.addon.premium.price

        super(UpsellForm, self).__init__(*args, **kw)
        self.fields['free'].queryset = (self.extra['amo_user'].addons
                                    .exclude(pk=self.addon.pk)
                                    .filter(premium_type__in=amo.ADDON_FREES,
                                            status__in=amo.VALID_STATUSES,
                                            type=self.addon.type))

        if len(self.fields['price'].choices) > 1:
            # Tier 0 (Free) should not be the default selection.
            self.initial['price'] = (Price.objects.active()
                                     .exclude(price='0.00')[0])

    def clean_make_public(self):
        return (amo.PUBLIC_WAIT if self.cleaned_data.get('make_public')
                                else None)

    def save(self):
        if 'price' in self.cleaned_data:
            premium = self.addon.premium
            if not premium:
                premium = AddonPremium()
                premium.addon = self.addon
            premium.price = self.cleaned_data['price']
            premium.save()

        upsell = self.addon.upsold
        if self.cleaned_data['free']:

            # Check if this app was already a premium version for another app.
            if upsell and upsell.free != self.cleaned_data['free']:
                upsell.delete()

            if not upsell:
                upsell = AddonUpsell(premium=self.addon)
            upsell.free = self.cleaned_data['free']
            upsell.save()
        elif upsell:
            upsell.delete()

        self.addon.update(make_public=self.cleaned_data['make_public'])


class AppDetailsBasicForm(TranslationFormMixin, happyforms.ModelForm):
    """Form for "Details" submission step."""

    app_slug = forms.CharField(max_length=30,
                           widget=forms.TextInput(attrs={'class': 'm'}))
    description = TransField(required=True,
        label=_lazy(u'Description:'),
        help_text=_lazy(u'This description will appear on the details page.'),
        widget=TransTextarea(attrs={'rows': 4}))
    privacy_policy = TransField(widget=TransTextarea(attrs={'rows': 6}),
        label=_lazy(u'Privacy Policy:'),
        help_text=_lazy(u"A privacy policy that explains what "
                         "data is transmitted from a user's computer and how "
                         "it is used is required."))
    homepage = TransField.adapt(forms.URLField)(required=False,
        label=_lazy(u'Homepage:'),
        help_text=_lazy(u'If your app has another homepage, enter its address '
                         'here.'),
        widget=TransInput(attrs={'class': 'full'}))
    support_url = TransField.adapt(forms.URLField)(required=False,
        label=_lazy(u'Support Website:'),
        help_text=_lazy(u'If your app has a support website or forum, enter '
                         'its address here.'),
        widget=TransInput(attrs={'class': 'full'}))
    support_email = TransField.adapt(forms.EmailField)(
        label=_lazy(u'Support Email:'),
        help_text=_lazy(u'This email address will be listed publicly on the '
                        u'Marketplace and used by end users to contact you '
                        u'with support issues. This email address will be '
                        u'listed publicly on your app details page.'),
        widget=TransInput(attrs={'class': 'full'}))
    flash = forms.TypedChoiceField(required=False,
        coerce=lambda x: bool(int(x)),
        label=_lazy(u'Does your app require Flash support?'),
        initial=0,
        choices=(
            (1, _lazy(u'Yes')),
            (0, _lazy(u'No')),
        ),
        widget=forms.RadioSelect)
    publish = forms.BooleanField(required=False, initial=1,
        label=_lazy(u"Publish my app in the Firefox Marketplace as soon as "
                     "it's reviewed."),
        help_text=_lazy(u"If selected your app will be published immediately "
                         "following its approval by reviewers.  If you don't "
                         "select this option you will be notified via email "
                         "about your app's approval and you will need to log "
                         "in and manually publish it."))

    class Meta:
        model = Addon
        fields = ('app_slug', 'description', 'privacy_policy', 'homepage',
                  'support_url', 'support_email')

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request')
        super(AppDetailsBasicForm, self).__init__(*args, **kwargs)

    def clean_app_slug(self):
        slug = self.cleaned_data['app_slug']
        slug_validator(slug, lower=False)

        if slug != self.instance.app_slug:
            if Webapp.objects.filter(app_slug=slug).exists():
                raise forms.ValidationError(
                    _('This slug is already in use. Please choose another.'))

            if BlacklistedSlug.blocked(slug):
                raise forms.ValidationError(
                    _('The slug cannot be "%s". Please choose another.'
                      % slug))

        return slug.lower()

    def save(self, *args, **kw):
        self.instance = super(AppDetailsBasicForm, self).save(commit=True)
        uses_flash = self.cleaned_data.get('flash')
        af = self.instance.get_latest_file()
        if af is not None:
            af.update(uses_flash=bool(uses_flash))

        return self.instance


class AppFeaturesForm(happyforms.ModelForm):
    class Meta:
        exclude = ['version']
        model = AppFeatures

    def __init__(self, *args, **kwargs):
        super(AppFeaturesForm, self).__init__(*args, **kwargs)
        if self.instance:
            self.initial_features = sorted(self.instance.to_keys())
        else:
            self.initial_features = None

    def all_fields(self):
        """
        Degeneratorizes self.__iter__(), the list of fields on the form. This
        allows further manipulation of fields: to display a subset of fields or
        order them in a specific way.
        """
        return [f for f in self.__iter__()]

    def required_api_fields(self):
        """
        All fields on the form, alphabetically sorted by help text.
        """
        return sorted(self.all_fields(), key=lambda x: x.help_text)

    def get_tooltip(self, field):
        field_id = field.name.split('_', 1)[1].upper()
        return (unicode(APP_FEATURES[field_id].get('description') or '')
                if field_id in APP_FEATURES else None)

    def _changed_features(self):
        old_features = defaultdict.fromkeys(self.initial_features, True)
        old_features = set(unicode(f) for f
                           in AppFeatures(**old_features).to_list())
        new_features = set(unicode(f) for f in self.instance.to_list())

        added_features = new_features - old_features
        removed_features = old_features - new_features
        return added_features, removed_features

    def save(self, *args, **kwargs):
        mark_for_rereview = kwargs.pop('mark_for_rereview', True)
        addon = self.instance.version.addon
        rval = super(AppFeaturesForm, self).save(*args, **kwargs)
        if (self.instance and mark_for_rereview and
                addon.status in amo.WEBAPPS_APPROVED_STATUSES and
                sorted(self.instance.to_keys()) != self.initial_features):
            added_features, removed_features = self._changed_features()
            mark_for_rereview_features_change(addon,
                                              added_features,
                                              removed_features)
        return rval
