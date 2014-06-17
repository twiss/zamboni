# -*- coding: utf-8 -*-
import json
import os
from datetime import datetime
from zipfile import ZipFile

from django import forms
from django.conf import settings
from django.core.validators import URLValidator
from django.forms.extras.widgets import SelectDateWidget
from django.forms.models import modelformset_factory
from django.template.defaultfilters import filesizeformat

import commonware
import happyforms
import waffle
from product_details import product_details
from quieter_formset.formset import BaseModelFormSet
from tower import ugettext as _, ugettext_lazy as _lazy, ungettext as ngettext

import amo
import lib.iarc
import mkt
from amo import get_user
from amo.fields import SeparatedValuesField
from amo.utils import remove_icons, slug_validator, slugify
from lib.video import tasks as vtasks
from mkt.access import acl
from mkt.api.models import Access
from mkt.constants import MAX_PACKAGED_APP_SIZE
from mkt.files.models import FileUpload
from mkt.files.utils import WebAppParser
from mkt.regions import REGIONS_CHOICES_SORTED_BY_NAME
from mkt.regions.utils import parse_region
from mkt.reviewers.models import RereviewQueue
from mkt.site.forms import AddonChoiceField
from mkt.tags.models import Tag
from mkt.translations.fields import TransField
from mkt.translations.forms import TranslationFormMixin
from mkt.translations.models import Translation
from mkt.translations.widgets import TranslationTextarea, TransTextarea
from mkt.versions.models import Version
from mkt.webapps.forms import clean_slug, clean_tags, icons
from mkt.webapps.models import (Addon, AddonUser, BlacklistedSlug, Category,
                                IARCInfo, Preview, Webapp)
from mkt.webapps.tasks import index_webapps, update_manifests
from mkt.webapps.widgets import CategoriesSelectMultiple, IconWidgetRenderer

from . import tasks


log = commonware.log.getLogger('mkt.developers')


region_error = lambda region: forms.ValidationError(
    _('You cannot select {region}.').format(
        region=unicode(parse_region(region).name)
    )
)


def toggle_app_for_special_regions(request, app, enabled_regions=None):
    """Toggle for special regions (e.g., China)."""
    if not waffle.flag_is_active(request, 'special-regions'):
        return

    for region in mkt.regions.SPECIAL_REGIONS:
        status = app.geodata.get_status(region)

        if enabled_regions is not None:
            if region.id in enabled_regions:
                # If it's not already enabled, mark as pending.
                if status != amo.STATUS_PUBLIC:
                    # Developer requested for it to be in China.
                    status = amo.STATUS_PENDING
                    value, changed = app.geodata.set_status(region, status)
                    if changed:
                        log.info(u'[Webapp:%s] App marked as pending '
                                 u'special region (%s).' % (app, region.slug))
                        value, changed = app.geodata.set_nominated_date(
                            region, save=True)
                        log.info(u'[Webapp:%s] Setting nomination date to '
                                 u'now for region (%s).' % (app, region.slug))
            else:
                # Developer cancelled request for approval.
                status = amo.STATUS_NULL
                value, changed = app.geodata.set_status(
                    region, status, save=True)
                if changed:
                    log.info(u'[Webapp:%s] App marked as null special '
                             u'region (%s).' % (app, region.slug))

        if status == amo.STATUS_PUBLIC:
            # Reviewer approved for it to be in China.
            aer = app.addonexcludedregion.filter(region=region.id)
            if aer.exists():
                aer.delete()
                log.info(u'[Webapp:%s] App included in new special '
                         u'region (%s).' % (app, region.slug))
        else:
            # Developer requested for it to be in China.
            aer, created = app.addonexcludedregion.get_or_create(
                region=region.id)
            if created:
                log.info(u'[Webapp:%s] App excluded from new special '
                         u'region (%s).' % (app, region.slug))


class AuthorForm(happyforms.ModelForm):

    # TODO: Remove this whole __init__ when the 'allow-refund' flag goes away.
    def __init__(self, *args, **kwargs):
        super(AuthorForm, self).__init__(*args, **kwargs)
        self.fields['role'].choices = (
            (c, s) for c, s in amo.AUTHOR_CHOICES
            if c != amo.AUTHOR_ROLE_SUPPORT or
            waffle.switch_is_active('allow-refund'))

    def clean_user(self):
        user = self.cleaned_data['user']
        if not user.read_dev_agreement:
            raise forms.ValidationError(
                _('All team members must have read and agreed to the '
                  'developer agreement.'))

        return user

    class Meta:
        model = AddonUser
        exclude = ('addon',)


class BaseModelFormSet(BaseModelFormSet):
    """
    Override the parent's is_valid to prevent deleting all forms.
    """

    def is_valid(self):
        # clean() won't get called in is_valid() if all the rows are getting
        # deleted. We can't allow deleting everything.
        rv = super(BaseModelFormSet, self).is_valid()
        return rv and not any(self.errors) and not bool(self.non_form_errors())


class BaseAuthorFormSet(BaseModelFormSet):

    def clean(self):
        if any(self.errors):
            return
        # cleaned_data could be None if it's the empty extra form.
        data = filter(None, [f.cleaned_data for f in self.forms
                             if not f.cleaned_data.get('DELETE', False)])
        if not any(d['role'] == amo.AUTHOR_ROLE_OWNER for d in data):
            raise forms.ValidationError(_('Must have at least one owner.'))
        if not any(d['listed'] for d in data):
            raise forms.ValidationError(
                _('At least one team member must be listed.'))
        users = [d['user'] for d in data]
        if sorted(users) != sorted(set(users)):
            raise forms.ValidationError(
                _('A team member can only be listed once.'))


AuthorFormSet = modelformset_factory(AddonUser, formset=BaseAuthorFormSet,
                                     form=AuthorForm, can_delete=True, extra=0)


class DeleteForm(happyforms.Form):
    reason = forms.CharField(required=False)

    def __init__(self, request):
        super(DeleteForm, self).__init__(request.POST)


def trap_duplicate(request, manifest_url):
    # See if this user has any other apps with the same manifest.
    owned = (request.user.addonuser_set
             .filter(addon__manifest_url=manifest_url))
    if not owned:
        return
    try:
        app = owned[0].addon
    except Addon.DoesNotExist:
        return
    error_url = app.get_dev_url()
    msg = None
    if app.status == amo.STATUS_PUBLIC:
        msg = _(u'Oops, looks like you already submitted that manifest '
                 'for %s, which is currently public. '
                 '<a href="%s">Edit app</a>')
    elif app.status == amo.STATUS_PENDING:
        msg = _(u'Oops, looks like you already submitted that manifest '
                 'for %s, which is currently pending. '
                 '<a href="%s">Edit app</a>')
    elif app.status == amo.STATUS_NULL:
        msg = _(u'Oops, looks like you already submitted that manifest '
                 'for %s, which is currently incomplete. '
                 '<a href="%s">Resume app</a>')
    elif app.status == amo.STATUS_REJECTED:
        msg = _(u'Oops, looks like you already submitted that manifest '
                 'for %s, which is currently rejected. '
                 '<a href="%s">Edit app</a>')
    elif app.status == amo.STATUS_DISABLED:
        msg = _(u'Oops, looks like you already submitted that manifest '
                 'for %s, which is currently disabled by Mozilla. '
                 '<a href="%s">Edit app</a>')
    elif app.disabled_by_user:
        msg = _(u'Oops, looks like you already submitted that manifest '
                 'for %s, which is currently disabled. '
                 '<a href="%s">Edit app</a>')
    if msg:
        return msg % (app.name, error_url)


def verify_app_domain(manifest_url, exclude=None, packaged=False):
    if packaged or waffle.switch_is_active('webapps-unique-by-domain'):
        domain = Webapp.domain_from_url(manifest_url)
        qs = Webapp.objects.filter(app_domain=domain)
        if exclude:
            qs = qs.exclude(pk=exclude.pk)
        if qs.exists():
            raise forms.ValidationError(
                _('An app already exists on this domain; '
                  'only one app per domain is allowed.'))


class PreviewForm(happyforms.ModelForm):
    file_upload = forms.FileField(required=False)
    upload_hash = forms.CharField(required=False)
    # This lets us POST the data URIs of the unsaved previews so we can still
    # show them if there were form errors.
    unsaved_image_data = forms.CharField(required=False,
                                         widget=forms.HiddenInput)
    unsaved_image_type = forms.CharField(required=False,
                                         widget=forms.HiddenInput)

    def save(self, addon, commit=True):
        if self.cleaned_data:
            self.instance.addon = addon
            if self.cleaned_data.get('DELETE'):
                # Existing preview.
                if self.instance.id:
                    self.instance.delete()
                # User has no desire to save this preview.
                return

            super(PreviewForm, self).save(commit=commit)
            if self.cleaned_data['upload_hash']:
                upload_hash = self.cleaned_data['upload_hash']
                upload_path = os.path.join(settings.TMP_PATH, 'preview',
                                           upload_hash)
                filetype = (os.path.splitext(upload_hash)[1][1:]
                                   .replace('-', '/'))
                if filetype in amo.VIDEO_TYPES:
                    self.instance.update(filetype=filetype)
                    vtasks.resize_video.delay(upload_path, self.instance,
                                              user=amo.get_user(),
                                              set_modified_on=[self.instance])
                else:
                    self.instance.update(filetype='image/png')
                    tasks.resize_preview.delay(upload_path, self.instance,
                                               set_modified_on=[self.instance])

    class Meta:
        model = Preview
        fields = ('file_upload', 'upload_hash', 'id', 'position')


class JSONField(forms.Field):
    def to_python(self, value):
        if value == '':
            return None

        try:
            if isinstance(value, basestring):
                return json.loads(value)
        except ValueError:
            pass
        return value


class JSONMultipleChoiceField(forms.MultipleChoiceField, JSONField):
    widget = forms.CheckboxSelectMultiple


class AdminSettingsForm(PreviewForm):
    DELETE = forms.BooleanField(required=False)
    mozilla_contact = SeparatedValuesField(forms.EmailField, separator=',',
                                           required=False)
    vip_app = forms.BooleanField(required=False)
    priority_review = forms.BooleanField(required=False)
    tags = forms.CharField(required=False)
    banner_regions = JSONMultipleChoiceField(
        required=False, choices=mkt.regions.REGIONS_CHOICES_NAME)
    banner_message = TransField(required=False)

    class Meta:
        model = Preview
        fields = ('file_upload', 'upload_hash', 'position')

    def __init__(self, *args, **kw):
        # Get the object for the app's promo `Preview` and pass it to the form.
        if kw.get('instance'):
            addon = kw.pop('instance')
            self.instance = addon
            self.promo = addon.get_promo()

        self.request = kw.pop('request', None)

        # Note: After calling `super`, `self.instance` becomes the `Preview`
        # object.
        super(AdminSettingsForm, self).__init__(*args, **kw)

        self.initial['vip_app'] = addon.vip_app
        self.initial['priority_review'] = addon.priority_review

        if self.instance:
            self.initial['mozilla_contact'] = addon.mozilla_contact
            self.initial['tags'] = ', '.join(self.get_tags(addon))

        self.initial['banner_regions'] = addon.geodata.banner_regions or []
        self.initial['banner_message'] = addon.geodata.banner_message_id

    @property
    def regions_by_id(self):
        return mkt.regions.REGIONS_CHOICES_ID_DICT

    def clean_position(self):
        return -1

    def clean_banner_regions(self):
        try:
            regions = map(int, self.cleaned_data.get('banner_regions'))
        except (TypeError, ValueError):
            # input data is not a list or data contains non-integers.
            raise forms.ValidationError(_('Invalid region(s) selected.'))

        return list(regions)

    def get_tags(self, addon):
        if acl.action_allowed(self.request, 'Apps', 'Edit'):
            return list(addon.tags.values_list('tag_text', flat=True))
        else:
            return list(addon.tags.filter(restricted=False)
                        .values_list('tag_text', flat=True))

    def clean_tags(self):
        return clean_tags(self.request, self.cleaned_data['tags'])

    def clean_mozilla_contact(self):
        contact = self.cleaned_data.get('mozilla_contact')
        if self.cleaned_data.get('mozilla_contact') is None:
            return u''
        return contact

    def save(self, addon, commit=True):
        if (self.cleaned_data.get('DELETE') and
            'upload_hash' not in self.changed_data and self.promo.id):
            self.promo.delete()
        elif self.promo and 'upload_hash' in self.changed_data:
            self.promo.delete()
        elif self.cleaned_data.get('upload_hash'):
            super(AdminSettingsForm, self).save(addon, True)

        contact = self.cleaned_data.get('mozilla_contact')
        updates = {} if contact is None else {'mozilla_contact': contact}
        updates.update({'vip_app': self.cleaned_data.get('vip_app')})
        updates.update({'priority_review': self.cleaned_data.get('priority_review')})
        addon.update(**updates)

        tags_new = self.cleaned_data['tags']
        tags_old = [slugify(t, spaces=True) for t in self.get_tags(addon)]

        add_tags = set(tags_new) - set(tags_old)
        del_tags = set(tags_old) - set(tags_new)

        # Add new tags.
        for t in add_tags:
            Tag(tag_text=t).save_tag(addon)

        # Remove old tags.
        for t in del_tags:
            Tag(tag_text=t).remove_tag(addon)

        geodata = addon.geodata
        geodata.banner_regions = self.cleaned_data.get('banner_regions')
        geodata.banner_message = self.cleaned_data.get('banner_message')
        geodata.save()

        uses_flash = self.cleaned_data.get('flash')
        af = addon.get_latest_file()
        if af is not None:
            af.update(uses_flash=bool(uses_flash))

        index_webapps.delay([addon.id])

        return addon


class BasePreviewFormSet(BaseModelFormSet):

    def clean(self):
        if any(self.errors):
            return
        at_least_one = False
        for form in self.forms:
            if (not form.cleaned_data.get('DELETE') and
                form.cleaned_data.get('upload_hash') is not None):
                at_least_one = True
        if not at_least_one:
            raise forms.ValidationError(
                _('You must upload at least one screenshot or video.'))


PreviewFormSet = modelformset_factory(Preview, formset=BasePreviewFormSet,
                                      form=PreviewForm, can_delete=True,
                                      extra=1)


class NewManifestForm(happyforms.Form):
    manifest = forms.URLField()

    def __init__(self, *args, **kwargs):
        self.is_standalone = kwargs.pop('is_standalone', False)
        super(NewManifestForm, self).__init__(*args, **kwargs)

    def clean_manifest(self):
        manifest = self.cleaned_data['manifest']
        # Skip checking the domain for the standalone validator.
        if not self.is_standalone:
            verify_app_domain(manifest)
        return manifest


class NewPackagedAppForm(happyforms.Form):
    upload = forms.FileField()

    def __init__(self, *args, **kwargs):
        self.max_size = kwargs.pop('max_size', MAX_PACKAGED_APP_SIZE)
        self.user = kwargs.pop('user', get_user())
        self.addon = kwargs.pop('addon', None)
        self.file_upload = None
        super(NewPackagedAppForm, self).__init__(*args, **kwargs)

    def clean_upload(self):
        upload = self.cleaned_data['upload']
        errors = []

        if upload.size > self.max_size:
            errors.append({
                'type': 'error',
                'message': _('Packaged app too large for submission. Packages '
                             'must be smaller than %s.' % filesizeformat(
                                 self.max_size)),
                'tier': 1,
            })
            # Immediately raise an error, do not process the rest of the view,
            # which would read the file.
            raise self.persist_errors(errors, upload)

        manifest = None
        try:
            # Be careful to keep this as in-memory zip reading.
            manifest = ZipFile(upload, 'r').read('manifest.webapp')
        except Exception as e:
            errors.append({
                'type': 'error',
                'message': _('Error extracting manifest from zip file.'),
                'tier': 1,
            })

        origin = None
        if manifest:
            try:
                origin = WebAppParser.decode_manifest(manifest).get('origin')
            except forms.ValidationError as e:
                errors.append({
                    'type': 'error',
                    'message': ''.join(e.messages),
                    'tier': 1,
                })

        if origin:
            try:
                verify_app_domain(origin, packaged=True, exclude=self.addon)
            except forms.ValidationError, e:
                errors.append({
                    'type': 'error',
                    'message': ''.join(e.messages),
                    'tier': 1,
                })

        if errors:
            raise self.persist_errors(errors, upload)

        # Everything passed validation.
        self.file_upload = FileUpload.from_post(
            upload, upload.name, upload.size, is_webapp=True)
        self.file_upload.user = self.user
        self.file_upload.save()

    def persist_errors(self, errors, upload):
        """
        Persist the error with this into FileUpload (but do not persist
        the file contents, which are too large) and return a ValidationError.
        """
        validation = {
            'errors': len(errors),
            'success': False,
            'messages': errors,
        }

        self.file_upload = FileUpload.objects.create(
                is_webapp=True, user=self.user,
                name=getattr(upload, 'name', ''),
                validation=json.dumps(validation))

        # Return a ValidationError to be raised by the view.
        return forms.ValidationError(' '.join(e['message'] for e in errors))


class AddonFormBase(TranslationFormMixin, happyforms.ModelForm):

    def __init__(self, *args, **kw):
        self.request = kw.pop('request')
        super(AddonFormBase, self).__init__(*args, **kw)

    class Meta:
        models = Addon
        fields = ('name', 'slug', 'tags')

    def clean_slug(self):
        return clean_slug(self.cleaned_data['slug'], self.instance)

    def clean_tags(self):
        return clean_tags(self.request, self.cleaned_data['tags'])

    def get_tags(self, addon):
        if acl.action_allowed(self.request, 'Apps', 'Edit'):
            return list(addon.tags.values_list('tag_text', flat=True))
        else:
            return list(addon.tags.filter(restricted=False)
                        .values_list('tag_text', flat=True))


class AppFormBasic(AddonFormBase):
    """Form to edit basic app info."""
    slug = forms.CharField(max_length=30, widget=forms.TextInput)
    manifest_url = forms.URLField()
    description = TransField(required=True,
        label=_lazy(u'Provide a detailed description of your app'),
        help_text=_lazy(u'This description will appear on the details page.'),
        widget=TransTextarea)

    class Meta:
        model = Addon
        fields = ('slug', 'manifest_url', 'description')

    def __init__(self, *args, **kw):
        # Force the form to use app_slug if this is a webapp. We want to keep
        # this under "slug" so all the js continues to work.
        if kw['instance'].is_webapp():
            kw.setdefault('initial', {})['slug'] = kw['instance'].app_slug

        super(AppFormBasic, self).__init__(*args, **kw)

        self.old_manifest_url = self.instance.manifest_url

        if self.instance.is_packaged:
            # Manifest URL cannot be changed for packaged apps.
            del self.fields['manifest_url']

    def _post_clean(self):
        # Switch slug to app_slug in cleaned_data and self._meta.fields so
        # we can update the app_slug field for webapps.
        try:
            self._meta.fields = list(self._meta.fields)
            slug_idx = self._meta.fields.index('slug')
            data = self.cleaned_data
            if 'slug' in data:
                data['app_slug'] = data.pop('slug')
            self._meta.fields[slug_idx] = 'app_slug'
            super(AppFormBasic, self)._post_clean()
        finally:
            self._meta.fields[slug_idx] = 'slug'

    def clean_slug(self):
        slug = self.cleaned_data['slug']
        slug_validator(slug, lower=False)

        if slug != self.instance.app_slug:
            if Webapp.objects.filter(app_slug=slug).exists():
                raise forms.ValidationError(
                    _('This slug is already in use. Please choose another.'))

            if BlacklistedSlug.blocked(slug):
                raise forms.ValidationError(_('The slug cannot be "%s". '
                                              'Please choose another.' % slug))

        return slug.lower()

    def clean_manifest_url(self):
        manifest_url = self.cleaned_data['manifest_url']
        # Only verify if manifest changed.
        if 'manifest_url' in self.changed_data:
            verify_app_domain(manifest_url, exclude=self.instance)
        return manifest_url

    def save(self, addon, commit=False):
        # We ignore `commit`, since we need it to be `False` so we can save
        # the ManyToMany fields on our own.
        addonform = super(AppFormBasic, self).save(commit=False)
        addonform.save()

        if 'manifest_url' in self.changed_data:
            before_url = self.old_manifest_url
            after_url = self.cleaned_data['manifest_url']

            # If a non-admin edited the manifest URL, add to Re-review Queue.
            if not acl.action_allowed(self.request, 'Admin', '%'):
                log.info(u'[Webapp:%s] (Re-review) Manifest URL changed '
                         u'from %s to %s'
                         % (self.instance, before_url, after_url))

                msg = (_(u'Manifest URL changed from {before_url} to '
                         u'{after_url}')
                       .format(before_url=before_url, after_url=after_url))

                RereviewQueue.flag(self.instance,
                                   amo.LOG.REREVIEW_MANIFEST_URL_CHANGE, msg)

            # Refetch the new manifest.
            log.info('Manifest %s refreshed for %s'
                     % (addon.manifest_url, addon))
            update_manifests.delay([self.instance.id])

        return addonform


class AppFormDetails(AddonFormBase):
    default_locale = forms.TypedChoiceField(required=False,
                                            choices=Addon.LOCALES)
    homepage = TransField.adapt(forms.URLField)(required=False)
    privacy_policy = TransField(widget=TransTextarea(), required=True,
        label=_lazy(u"Please specify your app's Privacy Policy"))

    class Meta:
        model = Addon
        fields = ('default_locale', 'homepage', 'privacy_policy')

    def clean(self):
        # Make sure we have the required translations in the new locale.
        required = ['name', 'description']
        data = self.cleaned_data
        if not self.errors and 'default_locale' in self.changed_data:
            fields = dict((k, getattr(self.instance, k + '_id'))
                          for k in required)
            locale = data['default_locale']
            ids = filter(None, fields.values())
            qs = (Translation.objects.filter(locale=locale, id__in=ids,
                                             localized_string__isnull=False)
                  .values_list('id', flat=True))
            missing = [k for k, v in fields.items() if v not in qs]
            if missing:
                raise forms.ValidationError(
                    _('Before changing your default locale you must have a '
                      'name and description in that locale. '
                      'You are missing %s.') % ', '.join(map(repr, missing)))
        return data


class AppFormMedia(AddonFormBase):
    icon_type = forms.CharField(required=False,
        widget=forms.RadioSelect(renderer=IconWidgetRenderer, choices=[]))
    icon_upload_hash = forms.CharField(required=False)
    unsaved_icon_data = forms.CharField(required=False,
                                        widget=forms.HiddenInput)

    class Meta:
        model = Addon
        fields = ('icon_upload_hash', 'icon_type')

    def __init__(self, *args, **kwargs):
        super(AppFormMedia, self).__init__(*args, **kwargs)

        # Add icons here so we only read the directory when
        # AppFormMedia is actually being used.
        self.fields['icon_type'].widget.choices = icons()

    def save(self, addon, commit=True):
        if self.cleaned_data['icon_upload_hash']:
            upload_hash = self.cleaned_data['icon_upload_hash']
            upload_path = os.path.join(settings.TMP_PATH, 'icon', upload_hash)

            dirname = addon.get_icon_dir()
            destination = os.path.join(dirname, '%s' % addon.id)

            remove_icons(destination)
            tasks.resize_icon.delay(upload_path, destination,
                                    amo.APP_ICON_SIZES,
                                    set_modified_on=[addon])

        return super(AppFormMedia, self).save(commit)


class AppFormSupport(AddonFormBase):
    support_url = TransField.adapt(forms.URLField)(required=False)
    support_email = TransField.adapt(forms.EmailField)()

    class Meta:
        model = Addon
        fields = ('support_email', 'support_url')


class AppAppealForm(happyforms.Form):
    """
    If a developer's app is rejected he can make changes and request
    another review.
    """
    notes = forms.CharField(
        label=_lazy(u'Your comments'),
        required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, **kw):
        self.product = kw.pop('product', None)
        super(AppAppealForm, self).__init__(*args, **kw)

    def save(self):
        version = self.product.versions.latest()
        notes = self.cleaned_data['notes']
        if notes:
            amo.log(amo.LOG.WEBAPP_RESUBMIT, self.product, version,
                    details={'comments': notes})
        else:
            amo.log(amo.LOG.WEBAPP_RESUBMIT, self.product, version)
        # Mark app and file as pending again.
        self.product.update(status=amo.WEBAPPS_UNREVIEWED_STATUS)
        version.all_files[0].update(status=amo.WEBAPPS_UNREVIEWED_STATUS)
        return version


class RegionForm(forms.Form):
    regions = forms.MultipleChoiceField(required=False,
        label=_lazy(u'Choose the regions your app will be listed in:'),
        choices=[],
        widget=forms.CheckboxSelectMultiple,
        error_messages={'required':
            _lazy(u'You must select at least one region.')})
    special_regions = forms.MultipleChoiceField(required=False,
        choices=[(x.id, x.name) for x in mkt.regions.SPECIAL_REGIONS],
        widget=forms.CheckboxSelectMultiple)
    enable_new_regions = forms.BooleanField(required=False,
        label=_lazy(u'Enable new regions'))
    restricted = forms.TypedChoiceField(required=False,
        choices=[(0, _lazy('Make my app available in most regions')),
                 (1, _lazy('Choose where my app is made available'))],
        widget=forms.RadioSelect(attrs={'class': 'choices'}),
        initial=0,
        coerce=int)

    def __init__(self, *args, **kw):
        self.product = kw.pop('product', None)
        self.request = kw.pop('request', None)
        super(RegionForm, self).__init__(*args, **kw)

        self.fields['regions'].choices = REGIONS_CHOICES_SORTED_BY_NAME()
        # If we have excluded regions, uncheck those.
        # Otherwise, default to everything checked.
        self.regions_before = self.product.get_region_ids(restofworld=True)

        self.initial = {
            'regions': sorted(self.regions_before),
            'restricted': int(self.product.geodata.restricted),
            'enable_new_regions': self.product.enable_new_regions,
        }

        # The checkboxes for special regions are
        #
        # - checked ... if an app has not been requested for approval in
        #   China or the app has been rejected in China.
        #
        # - unchecked ... if an app has been requested for approval in
        #   China or the app has been approved in China.
        unchecked_statuses = (amo.STATUS_NULL, amo.STATUS_REJECTED)

        for region in self.special_region_objs:
            if self.product.geodata.get_status(region) in unchecked_statuses:
                # If it's rejected in this region, uncheck its checkbox.
                if region.id in self.initial['regions']:
                    self.initial['regions'].remove(region.id)
            elif region.id not in self.initial['regions']:
                # If it's pending/public, check its checkbox.
                self.initial['regions'].append(region.id)

    @property
    def regions_by_id(self):
        return mkt.regions.REGIONS_CHOICES_ID_DICT

    @property
    def special_region_objs(self):
        return mkt.regions.SPECIAL_REGIONS

    @property
    def special_region_ids(self):
        return mkt.regions.SPECIAL_REGION_IDS

    @property
    def special_region_statuses(self):
        """Returns the null/pending/public status for each region."""
        statuses = {}
        for region in self.special_region_objs:
            statuses[region.id] = self.product.geodata.get_status_slug(region)
        return statuses

    @property
    def special_region_messages(self):
        """Returns the L10n messages for each region's status."""
        return self.product.geodata.get_status_messages()

    def is_toggling(self):
        if not self.request or not hasattr(self.request, 'POST'):
            return False
        value = self.request.POST.get('toggle-paid')
        return value if value in ('free', 'paid') else False

    def _product_is_paid(self):
        return (self.product.premium_type in amo.ADDON_PREMIUMS
                or self.product.premium_type == amo.ADDON_FREE_INAPP)

    def clean_regions(self):
        regions = self.cleaned_data['regions']
        if not self.is_toggling():
            if not regions:
                raise forms.ValidationError(
                    _('You must select at least one region.'))
        return regions

    def save(self):
        # Don't save regions if we are toggling.
        if self.is_toggling():
            return

        regions = [int(x) for x in self.cleaned_data['regions']]
        special_regions = [
            int(x) for x in self.cleaned_data['special_regions']
        ]
        restricted = int(self.cleaned_data['restricted'] or 0)

        if restricted:
            before = set(self.regions_before)
            after = set(regions)

            log.info(u'[Webapp:%s] App mark as restricted.' % self.product)

            # Add new region exclusions.
            to_add = before - after
            for region in to_add:
                aer, created = self.product.addonexcludedregion.get_or_create(
                    region=region)
                if created:
                    log.info(u'[Webapp:%s] Excluded from new region (%s).'
                             % (self.product, region))

            # Remove old region exclusions.
            to_remove = after - before
            for region in to_remove:
                self.product.addonexcludedregion.filter(
                    region=region).delete()
                log.info(u'[Webapp:%s] No longer exluded from region (%s).'
                         % (self.product, region))
        else:
            self.product.addonexcludedregion.all().delete()
            log.info(u'[Webapp:%s] App mark as unrestricted.' % self.product)

        self.product.geodata.update(restricted=restricted)

        # Toggle region exclusions/statuses for special regions (e.g., China).
        toggle_app_for_special_regions(self.request, self.product,
                                       special_regions)

        if self.cleaned_data['enable_new_regions']:
            self.product.update(enable_new_regions=True)
            log.info(u'[Webapp:%s] will be added to future regions.'
                     % self.product)
        else:
            self.product.update(enable_new_regions=False)
            log.info(u'[Webapp:%s] will not be added to future regions.'
                     % self.product)


class CategoryForm(happyforms.Form):
    categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.filter(type=amo.ADDON_WEBAPP),
        widget=CategoriesSelectMultiple)

    def __init__(self, *args, **kw):
        self.request = kw.pop('request', None)
        self.product = kw.pop('product', None)
        super(CategoryForm, self).__init__(*args, **kw)

        self.cats_before = list(
            self.product.categories.values_list('id', flat=True))

        self.initial['categories'] = self.cats_before

    def max_categories(self):
        return amo.MAX_CATEGORIES

    def clean_categories(self):
        categories = self.cleaned_data['categories']
        set_categories = set(categories.values_list('id', flat=True))

        total = len(set_categories)
        max_cat = amo.MAX_CATEGORIES

        if total > max_cat:
            # L10n: {0} is the number of categories.
            raise forms.ValidationError(ngettext(
                'You can have only {0} category.',
                'You can have only {0} categories.',
                max_cat).format(max_cat))

        return categories

    def save(self):
        after = list(self.cleaned_data['categories']
                     .values_list('id', flat=True))
        before = self.cats_before

        # Add new categories.
        to_add = set(after) - set(before)
        for c in to_add:
            self.product.addoncategory_set.create(category_id=c)

        # Remove old categories.
        to_remove = set(before) - set(after)
        self.product.addoncategory_set.filter(
            category_id__in=to_remove).delete()

        toggle_app_for_special_regions(self.request, self.product)


class DevAgreementForm(happyforms.Form):
    read_dev_agreement = forms.BooleanField(label=_lazy(u'Agree'),
                                            widget=forms.HiddenInput)

    def __init__(self, *args, **kw):
        self.instance = kw.pop('instance')
        super(DevAgreementForm, self).__init__(*args, **kw)

    def save(self):
        self.instance.read_dev_agreement = datetime.now()
        self.instance.save()


class DevNewsletterForm(happyforms.Form):
    """Devhub newsletter subscription form."""

    email = forms.EmailField(
        error_messages={'required':
                        _lazy(u'Please enter a valid email address.')},
        widget=forms.TextInput(attrs={'required': '',
                                      'placeholder':
                                      _lazy(u'Your email address')}))
    email_format = forms.ChoiceField(
        widget=forms.RadioSelect(),
        choices=(('H', 'HTML'), ('T', _lazy(u'Text'))),
        initial='H')
    privacy = forms.BooleanField(
        error_messages={'required':
                        _lazy(u'You must agree to the Privacy Policy.')})
    country = forms.ChoiceField(label=_lazy(u'Country'))

    def __init__(self, locale, *args, **kw):
        regions = product_details.get_regions(locale)
        regions = sorted(regions.iteritems(), key=lambda x: x[1])

        super(DevNewsletterForm, self).__init__(*args, **kw)

        self.fields['country'].choices = regions
        self.fields['country'].initial = 'us'


class AppFormTechnical(AddonFormBase):
    flash = forms.BooleanField(required=False)

    class Meta:
        model = Addon
        fields = ('public_stats',)

    def __init__(self, *args, **kw):
        super(AppFormTechnical, self).__init__(*args, **kw)
        self.initial['flash'] = self.instance.uses_flash

    def save(self, addon, commit=False):
        uses_flash = self.cleaned_data.get('flash')
        self.instance = super(AppFormTechnical, self).save(commit=True)
        af = self.instance.get_latest_file()
        if af is not None:
            af.update(uses_flash=bool(uses_flash))
        return self.instance


class TransactionFilterForm(happyforms.Form):
    app = AddonChoiceField(queryset=None, required=False, label=_lazy(u'App'))
    transaction_type = forms.ChoiceField(
        required=False, label=_lazy(u'Transaction Type'),
        choices=[(None, '')] + amo.MKT_TRANSACTION_CONTRIB_TYPES.items())
    transaction_id = forms.CharField(
        required=False, label=_lazy(u'Transaction ID'))

    current_year = datetime.today().year
    years = [current_year - x for x in range(current_year - 2012)]
    date_from = forms.DateTimeField(
        required=False, widget=SelectDateWidget(years=years),
        label=_lazy(u'From'))
    date_to = forms.DateTimeField(
        required=False, widget=SelectDateWidget(years=years),
        label=_lazy(u'To'))

    def __init__(self, *args, **kwargs):
        self.apps = kwargs.pop('apps', [])
        super(TransactionFilterForm, self).__init__(*args, **kwargs)
        self.fields['app'].queryset = self.apps


class APIConsumerForm(happyforms.ModelForm):
    app_name = forms.CharField(required=True)
    redirect_uri = forms.CharField(validators=[URLValidator()],
                                   required=True)

    class Meta:
        model = Access
        fields = ('app_name', 'redirect_uri')


class AppVersionForm(happyforms.ModelForm):
    releasenotes = TransField(widget=TransTextarea(), required=False)
    approvalnotes = forms.CharField(
        widget=TranslationTextarea(attrs={'rows': 4}), required=False)
    publish_immediately = forms.BooleanField(required=False)

    class Meta:
        model = Version
        fields = ('releasenotes', 'approvalnotes')

    def __init__(self, *args, **kwargs):
        super(AppVersionForm, self).__init__(*args, **kwargs)
        self.fields['publish_immediately'].initial = (
            self.instance.addon.make_public == amo.PUBLIC_IMMEDIATELY)

    def save(self, *args, **kwargs):
        rval = super(AppVersionForm, self).save(*args, **kwargs)
        if self.instance.all_files[0].status == amo.STATUS_PENDING:
            # If version is pending, allow changes to make_public, which lives
            # on the app itself.
            if self.cleaned_data.get('publish_immediately'):
                make_public = amo.PUBLIC_IMMEDIATELY
            else:
                make_public = amo.PUBLIC_WAIT
            self.instance.addon.update(make_public=make_public)
        return rval


class PreloadTestPlanForm(happyforms.Form):
    agree = forms.BooleanField(
        widget=forms.CheckboxInput,
        label=_lazy(
            u'Please consider my app as a candidate to be pre-loaded on a '
            u'Firefox OS device. I agree to the terms and conditions outlined '
            u'above. I understand that this document is not a commitment to '
            u'pre-load my app.'
        ))
    test_plan = forms.FileField(
        label=_lazy(u'Upload Your Test Plan (.pdf, .xls under 2.5MB)'),
        widget=forms.FileInput(attrs={'class': 'button'}))

    def clean(self):
        """Validate test_plan file."""
        content_types = ['application/pdf', 'application/vnd.ms-excel']
        max_upload_size = 2621440  # 2.5MB

        if 'test_plan' not in self.files:
            raise forms.ValidationError(_('Test plan required.'))

        file = self.files['test_plan']
        content_type = file.content_type

        if content_type in content_types:
            if file._size > max_upload_size:
                msg = _('File too large. Keep size under %s. Current size %s.')
                msg = msg % (filesizeformat(max_upload_size),
                             filesizeformat(file._size))
                self._errors['test_plan'] = self.error_class([msg])
                raise forms.ValidationError(msg)
        else:
            msg = (_('Invalid file type. Only %s files are supported.') %
                   ', '.join(content_types))
            self._errors['test_plan'] = self.error_class([msg])
            raise forms.ValidationError(msg)

        return self.cleaned_data


class IARCGetAppInfoForm(happyforms.Form):
    submission_id = forms.CharField()
    security_code = forms.CharField(max_length=10)

    def __init__(self, app, *args, **kwargs):
        self.app = app
        super(IARCGetAppInfoForm, self).__init__(*args, **kwargs)

    def clean_submission_id(self):
        submission_id = (
            # Also allow "subm-1234" since that's what IARC tool displays.
            self.cleaned_data['submission_id'].lower().replace('subm-', ''))

        if submission_id.isdigit():
            return int(submission_id)

        raise forms.ValidationError(_('Please enter a valid submission ID.'))

    def clean(self):
        cleaned_data = super(IARCGetAppInfoForm, self).clean()

        app = self.app
        iarc_id = cleaned_data.get('submission_id')

        if not app or not iarc_id:
            return cleaned_data

        if (not settings.IARC_ALLOW_CERT_REUSE and
            IARCInfo.objects.filter(submission_id=iarc_id)
                            .exclude(addon=app).exists()):
            del cleaned_data['submission_id']
            raise forms.ValidationError(
                _('This IARC certificate is already being used for another '
                  'app. Please create a new IARC Ratings Certificate.'))

        return cleaned_data

    def save(self, *args, **kwargs):
        app = self.app
        iarc_id = self.cleaned_data['submission_id']
        iarc_code = self.cleaned_data['security_code']

        # Generate XML.
        xml = lib.iarc.utils.render_xml(
            'get_app_info.xml',
            {'submission_id': iarc_id, 'security_code': iarc_code})

        # Process that shizzle.
        client = lib.iarc.client.get_iarc_client('services')
        resp = client.Get_App_Info(XMLString=xml)

        # Handle response.
        data = lib.iarc.utils.IARC_XML_Parser().parse_string(resp)

        if data.get('rows'):
            row = data['rows'][0]

            if 'submission_id' not in row:
                # [{'ActionStatus': 'No records found. Please try another
                #                   'criteria.', 'rowId: 1}].
                msg = _('Invalid submission ID or security code.')
                self._errors['submission_id'] = self.error_class([msg])
                log.info('[IARC] Bad GetAppInfo: %s' % row)
                raise forms.ValidationError(msg)

            # We found a rating, so store the id and code for future use.
            app.set_iarc_info(iarc_id, iarc_code)
            app.set_descriptors(row.get('descriptors', []))
            app.set_interactives(row.get('interactives', []))
            app.set_content_ratings(row.get('ratings', {}))

        else:
            msg = _('Invalid submission ID or security code.')
            self._errors['submission_id'] = self.error_class([msg])
            log.info('[IARC] Bad GetAppInfo. No rows: %s' % data)
            raise forms.ValidationError(msg)


class ContentRatingForm(happyforms.Form):
    since = forms.DateTimeField()
