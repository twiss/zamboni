import collections
import datetime
import functools
import HTMLParser
import json
import os
import sys
import traceback
import urllib

from django import http
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.signals import post_save
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache

import commonware.log
import jinja2
import requests
from cache_nuggets.lib import Token
from elasticutils import F
from rest_framework.exceptions import ParseError
from rest_framework.generics import CreateAPIView, ListAPIView
from rest_framework.response import Response
from tower import ugettext as _
from waffle.decorators import waffle_switch

import amo
import mkt
from amo.decorators import (any_permission_required, json_view, login_required,
                            permission_required)
from amo.helpers import absolutify, urlparams
from amo.models import manual_order
from amo.utils import (escape_all, HttpResponseSendFile, JSONEncoder, paginate,
                       redirect_for_login, smart_decode)
from lib.crypto.packaged import SigningError
from mkt.abuse.models import AbuseReport
from mkt.access import acl
from mkt.api.authentication import (RestOAuthAuthentication,
                                    RestSharedSecretAuthentication)
from mkt.api.authorization import GroupPermission
from mkt.api.base import SlugOrIdMixin
from mkt.comm.forms import CommAttachmentFormSet
from mkt.developers.models import ActivityLog, ActivityLogAttachment
from mkt.files.models import File
from mkt.ratings.forms import ReviewFlagFormSet
from mkt.ratings.models import Review, ReviewFlag
from mkt.regions.utils import parse_region
from mkt.reviewers.forms import (ApiReviewersSearchForm, ApproveRegionForm,
                                 MOTDForm)
from mkt.reviewers.models import (EditorSubscription, EscalationQueue,
                                  RereviewQueue, ReviewerScore)
from mkt.reviewers.serializers import (ReviewersESAppSerializer,
                                       ReviewingSerializer)
from mkt.reviewers.utils import (AppsReviewing, clean_sort_param,
                                 device_queue_search)
from mkt.search.utils import S
from mkt.search.views import SearchView
from mkt.site import messages
from mkt.site.helpers import product_as_dict
from mkt.submit.forms import AppFeaturesForm
from mkt.tags.models import Tag
from mkt.translations.query import order_by_translation
from mkt.users.models import UserProfile
from mkt.webapps.decorators import app_view
from mkt.webapps.indexers import WebappIndexer
from mkt.webapps.models import AddonDeviceType, Version, Webapp
from mkt.webapps.signals import version_changed
from mkt.zadmin.models import set_config, unmemoized_get_config

from . import forms
from .models import CannedResponse


QUEUE_PER_PAGE = 100
log = commonware.log.getLogger('z.reviewers')


def reviewer_required(region=None):
    """Requires the user to be logged in as a reviewer or admin, or allows
    someone with rule 'ReviewerTools:View' for GET requests.

    Reviewer is someone who is in one of the groups with the following
    permissions:

        Addons:Review
        Apps:Review
        Personas:Review

    """
    def decorator(f):
        @login_required
        @functools.wraps(f)
        def wrapper(request, *args, **kw):
            if (acl.check_reviewer(request, region=kw.get('region')) or
                _view_on_get(request)):
                return f(request, *args, **kw)
            else:
                raise PermissionDenied
        return wrapper
    # If decorator has no args, and is "paren-less", it's callable.
    if callable(region):
        return decorator(region)
    else:
        return decorator


@reviewer_required
def route_reviewer(request):
    """
    Redirect to apps home page if app reviewer.
    """
    return http.HttpResponseRedirect(reverse('reviewers.home'))


@reviewer_required
def home(request):
    durations = (('new', _('New Apps (Under 5 days)')),
                 ('med', _('Passable (5 to 10 days)')),
                 ('old', _('Overdue (Over 10 days)')))

    progress, percentage = _progress()

    data = context(
        request,
        reviews_total=ActivityLog.objects.total_reviews(webapp=True)[:5],
        reviews_monthly=ActivityLog.objects.monthly_reviews(webapp=True)[:5],
        #new_editors=EventLog.new_editors(),  # Bug 747035
        #eventlog=ActivityLog.objects.editor_events()[:6],  # Bug 746755
        progress=progress,
        percentage=percentage,
        durations=durations
    )
    return render(request, 'reviewers/home.html', data)


def queue_counts(request):
    excluded_ids = EscalationQueue.objects.no_cache().values_list('addon',
                                                                  flat=True)
    public_statuses = amo.WEBAPPS_APPROVED_STATUSES

    counts = {
        'pending': Webapp.objects.no_cache()
                         .exclude(id__in=excluded_ids)
                         .filter(type=amo.ADDON_WEBAPP,
                                 disabled_by_user=False,
                                 status=amo.STATUS_PENDING)
                         .count(),
        'rereview': RereviewQueue.objects.no_cache()
                                 .exclude(addon__in=excluded_ids)
                                 .filter(addon__disabled_by_user=False)
                                 .count(),
        # This will work as long as we disable files of existing unreviewed
        # versions when a new version is uploaded.
        'updates': File.objects.no_cache()
                       .exclude(version__addon__id__in=excluded_ids)
                       .filter(version__addon__type=amo.ADDON_WEBAPP,
                               version__addon__disabled_by_user=False,
                               version__addon__is_packaged=True,
                               version__addon__status__in=public_statuses,
                               version__deleted=False,
                               status=amo.STATUS_PENDING)
                       .count(),
        'escalated': EscalationQueue.objects.no_cache()
                                    .filter(addon__disabled_by_user=False)
                                    .count(),
        'moderated': Review.objects.no_cache().filter(
                                            addon__type=amo.ADDON_WEBAPP,
                                            reviewflag__isnull=False,
                                            editorreview=True)
                                    .count(),

        'region_cn': Webapp.objects.pending_in_region(mkt.regions.CN).count(),
    }

    if 'pro' in request.GET:
        counts.update({'device': device_queue_search(request).count()})

    rv = {}
    if isinstance(type, basestring):
        return counts[type]
    for k, v in counts.items():
        if not isinstance(type, list) or k in type:
            rv[k] = v
    return rv


def _progress():
    """Returns unreviewed apps progress.

    Return the number of apps still unreviewed for a given period of time and
    the percentage.
    """

    days_ago = lambda n: datetime.datetime.now() - datetime.timedelta(days=n)
    excluded_ids = EscalationQueue.objects.values_list('addon', flat=True)
    public_statuses = amo.WEBAPPS_APPROVED_STATUSES

    base_filters = {
        'pending': (Webapp.objects.rated()
                          .exclude(id__in=excluded_ids)
                          .filter(status=amo.STATUS_PENDING,
                                  disabled_by_user=False,
                                  _latest_version__deleted=False),
                    '_latest_version__nomination'),
        'rereview': (RereviewQueue.objects
                                  .exclude(addon__in=excluded_ids)
                                  .filter(addon__disabled_by_user=False),
                     'created'),
        'escalated': (EscalationQueue.objects
                                     .filter(addon__disabled_by_user=False),
                      'created'),
        'updates': (File.objects
                        .exclude(version__addon__id__in=excluded_ids)
                        .filter(version__addon__type=amo.ADDON_WEBAPP,
                                version__addon__disabled_by_user=False,
                                version__addon__is_packaged=True,
                                version__addon__status__in=public_statuses,
                                version__deleted=False,
                                status=amo.STATUS_PENDING),
                    'version__nomination')
    }

    operators_and_values = {
        'new': ('gt', days_ago(5)),
        'med': ('range', (days_ago(10), days_ago(5))),
        'old': ('lt', days_ago(10)),
        'week': ('gte', days_ago(7))
    }

    types = base_filters.keys()
    progress = {}

    for t in types:
        tmp = {}
        base_query, field = base_filters[t]
        for k in operators_and_values.keys():
            operator, value = operators_and_values[k]
            filter_ = {}
            filter_['%s__%s' % (field, operator)] = value
            tmp[k] = base_query.filter(**filter_).count()
        progress[t] = tmp

    # Return the percent of (p)rogress out of (t)otal.
    pct = lambda p, t: (p / float(t)) * 100 if p > 0 else 0

    percentage = {}
    for t in types:
        total = progress[t]['new'] + progress[t]['med'] + progress[t]['old']
        percentage[t] = {}
        for duration in ('new', 'med', 'old'):
            percentage[t][duration] = pct(progress[t][duration], total)

    return (progress, percentage)


def context(request, **kw):
    statuses = dict((k, unicode(v)) for k, v in amo.STATUS_CHOICES_API.items())
    ctx = dict(motd=unmemoized_get_config('mkt_reviewers_motd'),
               queue_counts=queue_counts(request),
               search_url=reverse('reviewers-search-api'),
               statuses=statuses, point_types=amo.REVIEWED_MARKETPLACE)
    ctx.update(kw)
    return ctx


def _review(request, addon, version):

    if (not settings.ALLOW_SELF_REVIEWS and
        not acl.action_allowed(request, 'Admin', '%') and
        addon.has_author(request.amo_user)):
        messages.warning(request, _('Self-reviews are not allowed.'))
        return redirect(reverse('reviewers.home'))

    if (addon.status == amo.STATUS_BLOCKED and
        not acl.action_allowed(request, 'Apps', 'ReviewEscalated')):
        messages.warning(
            request, _('Only senior reviewers can review blocklisted apps.'))
        return redirect(reverse('reviewers.home'))

    attachment_formset = CommAttachmentFormSet(data=request.POST or None,
                                               files=request.FILES or None,
                                               prefix='attachment')
    form = forms.get_review_form(data=request.POST or None,
                                 files=request.FILES or None, request=request,
                                 addon=addon, version=version,
                                 attachment_formset=attachment_formset)
    postdata = request.POST if request.method == 'POST' else None
    all_forms = [form, attachment_formset]

    if version:
        features_list = [unicode(f) for f in version.features.to_list()]
        appfeatures_form = AppFeaturesForm(data=postdata,
                                           instance=version.features)
        all_forms.append(appfeatures_form)
    else:
        appfeatures_form = None
        features_list = None

    queue_type = form.helper.review_type
    redirect_url = reverse('reviewers.apps.queue_%s' % queue_type)
    is_admin = acl.action_allowed(request, 'Apps', 'Edit')

    if request.method == 'POST' and all(f.is_valid() for f in all_forms):

        old_types = set(o.id for o in addon.device_types)
        new_types = set(form.cleaned_data.get('device_override'))

        old_features = set(features_list)
        new_features = set(unicode(f) for f
                           in appfeatures_form.instance.to_list())

        if form.cleaned_data.get('action') == 'public':
            if old_types != new_types:
                # The reviewer overrode the device types. We need to not
                # publish this app immediately.
                if addon.make_public == amo.PUBLIC_IMMEDIATELY:
                    addon.update(make_public=amo.PUBLIC_WAIT)

                # And update the device types to what the reviewer set.
                AddonDeviceType.objects.filter(addon=addon).delete()
                for device in form.cleaned_data.get('device_override'):
                    addon.addondevicetype_set.create(device_type=device)

                # Log that the reviewer changed the device types.
                added_devices = new_types - old_types
                removed_devices = old_types - new_types
                msg = _(u'Device(s) changed by '
                         'reviewer: {0}').format(', '.join(
                    [_(u'Added {0}').format(unicode(amo.DEVICE_TYPES[d].name))
                     for d in added_devices] +
                    [_(u'Removed {0}').format(
                     unicode(amo.DEVICE_TYPES[d].name))
                     for d in removed_devices]))
                amo.log(amo.LOG.REVIEW_DEVICE_OVERRIDE, addon,
                        addon.current_version, details={'comments': msg})

            if old_features != new_features:
                # The reviewer overrode the requirements. We need to not
                # publish this app immediately.
                if addon.make_public == amo.PUBLIC_IMMEDIATELY:
                    addon.update(make_public=amo.PUBLIC_WAIT)

                appfeatures_form.save(mark_for_rereview=False)

                # Log that the reviewer changed the minimum requirements.
                added_features = new_features - old_features
                removed_features = old_features - new_features

                fmt = ', '.join(
                      [_(u'Added {0}').format(f) for f in added_features] +
                      [_(u'Removed {0}').format(f) for f in removed_features])
                # L10n: {0} is the list of requirements changes.
                msg = _(u'Requirements changed by reviewer: {0}').format(fmt)
                amo.log(amo.LOG.REVIEW_FEATURES_OVERRIDE, addon,
                        addon.current_version, details={'comments': msg})

        score = form.helper.process()

        if form.cleaned_data.get('notify'):
            # TODO: bug 741679 for implementing notifications in Marketplace.
            EditorSubscription.objects.get_or_create(user=request.amo_user,
                                                     addon=addon)

        is_tarako = form.cleaned_data.get('is_tarako', False)
        if is_tarako:
            Tag(tag_text='tarako').save_tag(addon)
        else:
            Tag(tag_text='tarako').remove_tag(addon)

        # Success message.
        if score:
            score = ReviewerScore.objects.filter(user=request.amo_user)[0]
            # L10N: {0} is the type of review. {1} is the points they earned.
            #       {2} is the points they now have total.
            success = _(
               u'"{0}" successfully processed (+{1} points, {2} total).'
                .format(unicode(amo.REVIEWED_CHOICES[score.note_key]),
                        score.score,
                        ReviewerScore.get_total(request.amo_user)))
        else:
            success = _('Review successfully processed.')
        messages.success(request, success)

        return redirect(redirect_url)

    canned = CannedResponse.objects.all()
    actions = form.helper.actions.items()

    try:
        if not version:
            raise Version.DoesNotExist
        show_diff = (addon.versions.exclude(id=version.id)
                                   .filter(files__isnull=False,
                                           created__lt=version.created,
                                           files__status=amo.STATUS_PUBLIC)
                                   .latest())
    except Version.DoesNotExist:
        show_diff = None

    # The actions we should show a minimal form from.
    actions_minimal = [k for (k, a) in actions if not a.get('minimal')]

    # We only allow the user to check/uncheck files for "pending"
    allow_unchecking_files = form.helper.review_type == "pending"

    versions = (Version.with_deleted.filter(addon=addon)
                                    .order_by('-created')
                                    .transform(Version.transformer_activity)
                                    .transform(Version.transformer))

    product_attrs = {
        'product': json.dumps(
            product_as_dict(request, addon, False, 'reviewer'),
            cls=JSONEncoder),
        'manifest_url': addon.manifest_url,
    }

    pager = paginate(request, versions, 10)

    num_pages = pager.paginator.num_pages
    count = pager.paginator.count

    ctx = context(request, version=version, product=addon, pager=pager,
                  num_pages=num_pages, count=count,
                  form=form, canned=canned, is_admin=is_admin,
                  status_types=amo.MKT_STATUS_CHOICES, show_diff=show_diff,
                  allow_unchecking_files=allow_unchecking_files,
                  actions=actions, actions_minimal=actions_minimal,
                  tab=queue_type, product_attrs=product_attrs,
                  attachment_formset=attachment_formset,
                  appfeatures_form=appfeatures_form)

    if features_list is not None:
        ctx['feature_list'] = features_list

    return render(request, 'reviewers/review.html', ctx)


@transaction.commit_manually
@reviewer_required
@app_view
def app_review(request, addon):
    version = addon.latest_version
    resp = None
    try:
        resp = _review(request, addon, version)
    except SigningError, exc:
        transaction.rollback()
        messages.error(request, 'Signing Error: %s' % exc)
        transaction.commit()
        return redirect(
            reverse('reviewers.apps.review', args=[addon.app_slug]))
    except Exception:
        transaction.rollback()
        raise
    else:
        transaction.commit()
        # We (hopefully) have been avoiding sending send post_save and
        # version_changed signals in the review process till now (_review()
        # uses ReviewHelper which should have done all of its update() calls
        # with _signal=False).
        #
        # Now is a good time to send them: the transaction we were in has been
        # committed, so we know everything is ok. This is important: we need
        # them to index the app or call update_version() if that wasn't done
        # before already.
        if request.method == 'POST':
            try:
                post_save.send(sender=Webapp, instance=addon, created=False)
                post_save.send(sender=Version, instance=version, created=False)
                if getattr(addon, 'resend_version_changed_signal', False):
                    version_changed.send(sender=addon)
                    del addon.resend_version_changed_signal
            except Exception:
                transaction.rollback()
                raise
            else:
                transaction.commit()
        if resp:
            return resp
        raise


QueuedApp = collections.namedtuple('QueuedApp', 'app created')


def _queue(request, apps, tab, pager_processor=None, date_sort='created',
           template='reviewers/queue.html', data=None):
    per_page = request.GET.get('per_page', QUEUE_PER_PAGE)
    pager = paginate(request, apps, per_page)

    ctx = {
        'addons': pager.object_list,
        'pager': pager,
        'tab': tab,
        'search_form': _get_search_form(request),
        'date_sort': date_sort
    }

    # Additional context variables.
    if data is not None:
        ctx.update(data)

    return render(request, template, context(request, **ctx))


def _do_sort(request, qs, date_sort='created'):
    """Returns sorted Webapp queryset."""
    if qs.model is Webapp:
        return _do_sort_webapp(request, qs, date_sort)
    return _do_sort_queue_obj(request, qs, date_sort)


def _do_sort_webapp(request, qs, date_sort):
    """
    Column sorting logic based on request GET parameters.
    """
    sort_type, order = clean_sort_param(request, date_sort=date_sort)
    order_by = ('-' if order == 'desc' else '') + sort_type

    # Sort.
    if sort_type == 'name':
        # Sorting by name translation.
        return order_by_translation(qs, order_by)

    elif sort_type == 'num_abuse_reports':
        return (qs.annotate(num_abuse_reports=Count('abuse_reports'))
                .order_by(order_by))

    else:
        return qs.order_by('-priority_review', order_by)


def _do_sort_queue_obj(request, qs, date_sort):
    """
    Column sorting logic based on request GET parameters.
    Deals with objects with joins on the Addon (e.g. RereviewQueue, Version).
    Returns qs of apps.
    """
    sort_type, order = clean_sort_param(request, date_sort=date_sort)
    sort_str = sort_type

    if sort_type not in [date_sort, 'name']:
        sort_str = 'addon__' + sort_type

    # sort_str includes possible joins when ordering.
    # sort_type is the name of the field to sort on without desc/asc markers.
    # order_by is the name of the field to sort on with desc/asc markers.
    order_by = ('-' if order == 'desc' else '') + sort_str

    # Sort.
    if sort_type == 'name':
        # Sorting by name translation through an addon foreign key.
        return order_by_translation(
            Webapp.objects.filter(id__in=qs.values_list('addon', flat=True)),
            order_by)

    elif sort_type == 'num_abuse_reports':
        qs = qs.annotate(num_abuse_reports=Count('abuse_reports'))

    # Convert sorted queue object queryset to sorted app queryset.
    sorted_app_ids = (qs.order_by('-addon__priority_review', order_by)
                        .values_list('addon', flat=True))
    qs = Webapp.objects.filter(id__in=sorted_app_ids)
    return manual_order(qs, sorted_app_ids, 'addons.id')


@reviewer_required
def queue_apps(request):
    excluded_ids = EscalationQueue.objects.no_cache().values_list('addon',
                                                                  flat=True)
    qs = (Version.objects.no_cache().filter(
          files__status=amo.STATUS_PENDING, addon__type=amo.ADDON_WEBAPP,
          addon__disabled_by_user=False,
          addon__status=amo.STATUS_PENDING)
          .exclude(addon__id__in=excluded_ids)
          .order_by('nomination', 'created')
          .select_related('addon', 'files').no_transforms())

    apps = _do_sort(request, qs, date_sort='nomination')
    apps = [QueuedApp(app, app.all_versions[0].nomination)
            for app in Webapp.version_and_file_transformer(apps)]

    return _queue(request, apps, 'pending', date_sort='nomination')


@reviewer_required
def queue_region(request, region=None):
    # TODO: Create a landing page that lists all the special regions.
    if region is None:
        raise http.Http404

    region = parse_region(region)
    column = '_geodata__region_%s_nominated' % region.slug

    qs = Webapp.objects.pending_in_region(region)

    apps = [QueuedApp(app, app.geodata.get_nominated_date(region))
            for app in _do_sort(request, qs, date_sort=column)]

    return _queue(request, apps, 'region', date_sort=column,
                  template='reviewers/queue_region.html',
                  data={'region': region})


@reviewer_required
def queue_rereview(request):
    excluded_ids = EscalationQueue.objects.no_cache().values_list('addon',
                                                                  flat=True)
    rqs = (RereviewQueue.objects.no_cache()
                        .filter(addon__type=amo.ADDON_WEBAPP,
                                addon__disabled_by_user=False)
                        .exclude(addon__in=excluded_ids))
    apps = _do_sort(request, rqs)
    apps = [QueuedApp(app, app.rereviewqueue_set.all()[0].created)
            for app in apps]
    return _queue(request, apps, 'rereview')


@permission_required('Apps', 'ReviewEscalated')
def queue_escalated(request):
    eqs = EscalationQueue.objects.no_cache().filter(
        addon__type=amo.ADDON_WEBAPP, addon__disabled_by_user=False)
    apps = _do_sort(request, eqs)
    apps = [QueuedApp(app, app.escalationqueue_set.all()[0].created)
            for app in apps]
    return _queue(request, apps, 'escalated')


@reviewer_required
def queue_updates(request):
    excluded_ids = EscalationQueue.objects.no_cache().values_list('addon',
                                                                  flat=True)
    qs = (Version.objects.no_cache().filter(
          files__status=amo.STATUS_PENDING,
          addon__type=amo.ADDON_WEBAPP,
          addon__disabled_by_user=False,
          addon__status__in=amo.WEBAPPS_APPROVED_STATUSES)
          .exclude(addon__id__in=excluded_ids)
          .order_by('nomination', 'created')
          .select_related('addon', 'files').no_transforms())

    apps = _do_sort(request, qs, date_sort='nomination')
    apps = [QueuedApp(app, app.all_versions[0].nomination)
            for app in Webapp.version_and_file_transformer(apps)]

    return _queue(request, apps, 'updates', date_sort='nomination')


@reviewer_required
def queue_device(request):
    """
    A device specific queue matching apps which require features that our
    device support based on the `profile` query string.
    """
    if 'pro' in request.GET:
        apps = [QueuedApp(app, app.all_versions[0].nomination)
                for app in device_queue_search(request)]
    else:
        apps = []

    return _queue(request, apps, 'device')


@reviewer_required
def queue_moderated(request):
    """Queue for reviewing app reviews."""
    rf = (Review.objects.no_cache()
                .exclude(Q(addon__isnull=True) | Q(reviewflag__isnull=True))
                .filter(addon__type=amo.ADDON_WEBAPP, editorreview=True)
                .order_by('reviewflag__created'))

    page = paginate(request, rf, per_page=20)
    flags = dict(ReviewFlag.FLAGS)
    reviews_formset = ReviewFlagFormSet(request.POST or None,
                                        queryset=page.object_list,
                                        request=request)

    if reviews_formset.is_valid():
        reviews_formset.save()
        return redirect(reverse('reviewers.apps.queue_moderated'))

    return render(request, 'reviewers/queue.html',
                  context(request, reviews_formset=reviews_formset,
                          tab='moderated', page=page, flags=flags))


def _get_search_form(request):
    form = ApiReviewersSearchForm()
    fields = [f.name for f in form.visible_fields() + form.hidden_fields()]
    get = dict((k, v) for k, v in request.GET.items() if k in fields)
    return ApiReviewersSearchForm(get or None)


@permission_required('Apps', 'Review')
def logs(request):
    data = request.GET.copy()

    if not data.get('start') and not data.get('end'):
        today = datetime.date.today()
        data['start'] = datetime.date(today.year, today.month, 1)

    form = forms.ReviewAppLogForm(data)

    approvals = ActivityLog.objects.review_queue(webapp=True)

    if form.is_valid():
        data = form.cleaned_data
        if data.get('start'):
            approvals = approvals.filter(created__gte=data['start'])
        if data.get('end'):
            approvals = approvals.filter(created__lt=data['end'])
        if data.get('search'):
            term = data['search']
            approvals = approvals.filter(
                Q(commentlog__comments__icontains=term) |
                Q(applog__addon__name__localized_string__icontains=term) |
                Q(applog__addon__app_slug__icontains=term) |
                Q(user__display_name__icontains=term) |
                Q(user__username__icontains=term)).distinct()

    pager = paginate(request, approvals, 50)
    data = context(request, form=form, pager=pager, ACTION_DICT=amo.LOG_BY_ID,
                   tab='apps')
    return render(request, 'reviewers/logs.html', data)


@reviewer_required
def motd(request):
    form = None
    motd = unmemoized_get_config('mkt_reviewers_motd')
    if acl.action_allowed(request, 'AppReviewerMOTD', 'Edit'):
        form = MOTDForm(request.POST or None, initial={'motd': motd})
    if form and request.method == 'POST' and form.is_valid():
        set_config(u'mkt_reviewers_motd', form.cleaned_data['motd'])
        messages.success(request, _('Changes successfully saved.'))
        return redirect(reverse('reviewers.apps.motd'))
    data = context(request, form=form)
    return render(request, 'reviewers/motd.html', data)


# TODO: Move these to the validator when they live there someday.
PRIVILEGED_PERMISSIONS = set([
    'tcp-socket', 'contacts', 'device-storage:pictures',
    'device-storage:videos', 'device-storage:music', 'device-storage:sdcard',
    'browser', 'systemXHR', 'audio-channel-notification',
    'audio-channel-alarm'])
CERTIFIED_PERMISSIONS = set([
    'camera', 'tcp-socket', 'network-events', 'contacts',
    'device-storage:apps', 'device-storage:pictures',
    'device-storage:videos', 'device-storage:music', 'device-storage:sdcard',
    'sms', 'telephony', 'browser', 'bluetooth', 'mobileconnection', 'power',
    'settings', 'permissions', 'attention', 'webapps-manage',
    'backgroundservice', 'networkstats-manage', 'wifi-manage', 'systemXHR',
    'voicemail', 'deprecated-hwvideo', 'idle', 'time', 'embed-apps',
    'background-sensors', 'cellbroadcast', 'audio-channel-notification',
    'audio-channel-alarm', 'audio-channel-telephony', 'audio-channel-ringer',
    'audio-channel-publicnotification', 'open-remote-window'])


def _get_permissions(manifest):
    permissions = {}

    for perm in manifest.get('permissions', {}).keys():
        pval = permissions[perm] = {'type': 'web'}
        if perm in PRIVILEGED_PERMISSIONS:
            pval['type'] = 'priv'
        elif perm in CERTIFIED_PERMISSIONS:
            pval['type'] = 'cert'

        pval['description'] = manifest['permissions'][perm].get('description')

    return permissions


def _get_manifest_json(addon):
    return addon.get_manifest_json(addon.versions.latest().all_files[0])


@any_permission_required([('AppLookup', 'View'), ('Apps', 'Review')])
@app_view
@json_view
def app_view_manifest(request, addon):
    headers = {}
    manifest = {}
    success = False

    if addon.is_packaged:
        manifest = _get_manifest_json(addon)
        content = json.dumps(manifest, indent=4)
        success = True

    else:  # Show the hosted manifest_url.
        content, headers = u'', {}
        if addon.manifest_url:
            try:
                req = requests.get(addon.manifest_url, verify=False)
                content, headers = req.content, req.headers
                success = True
            except Exception:
                content = u''.join(traceback.format_exception(*sys.exc_info()))
            else:
                success = True

            try:
                # Reindent the JSON.
                manifest = json.loads(content)
                content = json.dumps(manifest, indent=4)
            except:
                # If it's not valid JSON, just return the content as is.
                pass

    return {
        'content': jinja2.escape(smart_decode(content)),
        'headers': dict((jinja2.escape(k), jinja2.escape(v))
                        for k, v in headers.items()),
        'success': success,
        # Note: We're using `escape_all` on the values here since we know the
        # keys of the nested dict don't come from user input (manifest) and are
        # known safe.
        'permissions': dict((jinja2.escape(k), escape_all(v))
                            for k, v in _get_permissions(manifest).items())
    }


def reviewer_or_token_required(f):
    @functools.wraps(f)
    def wrapper(request, addon, *args, **kw):
        # If there is a 'token' in request.GET we either return 200 or 403.
        # Otherwise we treat it like a normal django view and redirect to a
        # login page or check for Apps:Review permissions.
        allowed = False
        token = request.GET.get('token')

        if token and Token.pop(token, data={'app_id': addon.id}):
            log.info('Token for app:%s was successfully used' % addon.id)
            allowed = True
        elif not token and not request.user.is_authenticated():
            return redirect_for_login(request)
        elif acl.action_allowed(request, 'Apps', 'Review'):
            allowed = True

        if allowed:
            if token:
                log.info('Token provided for app:%s and all was happy'
                         % addon.id)
            else:
                log.info('Apps:Review (no token) all happy for app:%s'
                         % addon.id)
            return f(request, addon, *args, **kw)
        else:
            if token:
                log.info('Token provided for app:%s but was not valid'
                         % addon.id)
            else:
                log.info('Apps:Review permissions not met for app:%s'
                         % addon.id)
            raise PermissionDenied

    return wrapper


@app_view
@reviewer_or_token_required
def mini_manifest(request, addon, version_id):
    token = request.GET.get('token')
    return http.HttpResponse(
        _mini_manifest(addon, version_id, token),
        content_type='application/x-web-app-manifest+json; charset=utf-8')


def _mini_manifest(addon, version_id, token=None):
    if not addon.is_packaged:
        raise http.Http404

    version = get_object_or_404(addon.versions, pk=version_id)
    file_ = version.all_files[0]
    manifest = addon.get_manifest_json(file_)

    package_path = absolutify(
        reverse('reviewers.signed', args=[addon.app_slug, version.id]))

    if token:
        # Generate a fresh token.
        token = Token(data={'app_id': addon.id})
        token.save()
        package_path = urlparams(package_path, token=token.token)

    data = {
        'name': manifest['name'],
        'version': version.version,
        'size': file_.size,
        'release_notes': version.releasenotes,
        'package_path': package_path,
    }
    for key in ['developer', 'icons', 'locales']:
        if key in manifest:
            data[key] = manifest[key]

    return json.dumps(data, cls=JSONEncoder)


@permission_required('Apps', 'Review')
@app_view
def app_abuse(request, addon):
    reports = AbuseReport.objects.filter(addon=addon).order_by('-created')
    total = reports.count()
    reports = paginate(request, reports, count=total)
    return render(request, 'reviewers/abuse.html',
                  context(request, addon=addon, reports=reports,
                          total=total))


@app_view
@reviewer_or_token_required
def get_signed_packaged(request, addon, version_id):
    version = get_object_or_404(addon.versions, pk=version_id)
    file = version.all_files[0]
    path = addon.sign_if_packaged(version.pk, reviewer=True)
    if not path:
        raise http.Http404
    log.info('Returning signed package addon: %s, version: %s, path: %s' %
             (addon.pk, version_id, path))
    return HttpResponseSendFile(request, path, content_type='application/zip',
                                etag=file.hash.split(':')[-1])


@permission_required('Apps', 'Review')
def performance(request, username=None):
    is_admin = acl.action_allowed(request, 'Admin', '%')

    if username:
        if username == request.amo_user.username:
            user = request.amo_user
        elif is_admin:
            user = get_object_or_404(UserProfile, username=username)
        else:
            raise http.Http404
    else:
        user = request.amo_user

    today = datetime.date.today()
    month_ago = today - datetime.timedelta(days=30)
    year_ago = today - datetime.timedelta(days=365)

    total = ReviewerScore.get_total(user)
    totals = ReviewerScore.get_breakdown(user)
    months = ReviewerScore.get_breakdown_since(user, month_ago)
    years = ReviewerScore.get_breakdown_since(user, year_ago)

    def _sum(iter, types):
        return sum(s.total for s in iter if s.atype in types)

    breakdown = {
        'month': {
            'addons': _sum(months, amo.GROUP_TYPE_ADDON),
            'apps': _sum(months, amo.GROUP_TYPE_WEBAPP),
        },
        'year': {
            'addons': _sum(years, amo.GROUP_TYPE_ADDON),
            'apps': _sum(years, amo.GROUP_TYPE_WEBAPP),
        },
        'total': {
            'addons': _sum(totals, amo.GROUP_TYPE_ADDON),
            'apps': _sum(totals, amo.GROUP_TYPE_WEBAPP),
        }
    }

    ctx = context(request, **{
        'profile': user,
        'total': total,
        'breakdown': breakdown,
    })

    return render(request, 'reviewers/performance.html', ctx)


@any_permission_required([('Apps', 'Review')])
def leaderboard(request):
    return render(request, 'reviewers/leaderboard.html',
                  context(request,
                          **{'scores': ReviewerScore.all_users_by_score()}))


@permission_required('Apps', 'Review')
@json_view
def apps_reviewing(request):
    return render(request, 'reviewers/apps_reviewing.html',
                  context(request,
                          **{'tab': 'reviewing',
                             'apps': AppsReviewing(request).get_apps()}))


@permission_required('Apps', 'Review')
def attachment(request, attachment):
    """
    Serve an attachment directly to the user.
    """
    try:
        a = ActivityLogAttachment.objects.get(pk=attachment)
        full_path = os.path.join(settings.REVIEWER_ATTACHMENTS_PATH,
                                 a.filepath)
        fsock = open(full_path, 'r')
    except (ActivityLogAttachment.DoesNotExist, IOError,):
        response = http.HttpResponseNotFound()
    else:
        filename = urllib.quote(a.filename())
        response = http.HttpResponse(fsock,
                                     mimetype='application/force-download')
        response['Content-Disposition'] = 'attachment; filename=%s' % filename
        response['Content-Length'] = os.path.getsize(full_path)
    return response


def _retrieve_translation(text, language):
    try:
        r = requests.get(
            settings.GOOGLE_TRANSLATE_API_URL, params={
                'key': getattr(settings, 'GOOGLE_API_CREDENTIALS', ''),
                'q': text, 'target': language})
    except Exception, e:
        log.error(e)
        raise
    try:
        translated = (HTMLParser.HTMLParser().unescape(r.json()['data']
                      ['translations'][0]['translatedText']))
    except (KeyError, IndexError):
        translated = ''
    return translated, r


@waffle_switch('reviews-translate')
@permission_required('Apps', 'Review')
def review_translate(request, addon_slug, review_pk, language):
    review = get_object_or_404(Review, addon__slug=addon_slug, pk=review_pk)

    if '-' in language:
        language = language.split('-')[0]

    if request.is_ajax():
        title = ''
        body = ''
        status = 200

        if review.title is not None:
            title, r = _retrieve_translation(review.title, language)
            if r.status_code != 200:
                status = r.status_code

        if review.body is not None:
            body, r = _retrieve_translation(review.body, language)
            if r.status_code != 200:
                status = r.status_code

        return http.HttpResponse(json.dumps({'title': title, 'body': body}),
                                 status=status)
    else:
        return redirect(settings.GOOGLE_TRANSLATE_REDIRECT_URL.format(
            lang=language, text=review.body))


class ReviewingView(ListAPIView):
    authentication_classes = [RestOAuthAuthentication,
                              RestSharedSecretAuthentication]
    permission_classes = [GroupPermission('Apps', 'Review')]
    serializer_class = ReviewingSerializer

    def get_queryset(self):
        return [row['app'] for row in AppsReviewing(self.request).get_apps()]


class ReviewersSearchView(SearchView):
    cors_allowed_methods = ['get']
    authentication_classes = [RestSharedSecretAuthentication,
                              RestOAuthAuthentication]
    permission_classes = [GroupPermission('Apps', 'Review')]
    form_class = ApiReviewersSearchForm
    serializer_class = ReviewersESAppSerializer

    def search(self, request):
        form_data = self.get_search_data(request)
        query = form_data.get('q', '')
        base_filters = {'type': form_data['type']}
        if form_data.get('status') != 'any':
            base_filters['status'] = form_data.get('status')
        qs = S(WebappIndexer).filter(**base_filters)
        qs = self.apply_filters(request, qs, data=form_data)
        qs = apply_reviewer_filters(request, qs, data=form_data)
        page = self.paginate_queryset(qs)
        return self.get_pagination_serializer(page), query


def apply_reviewer_filters(request, qs, data=None):
    for k in ('has_info_request', 'has_editor_comment'):
        if data.get(k, None) is not None:
            qs = qs.filter(**{
                'latest_version.%s' % k: data[k]
            })
    if data.get('is_escalated', None) is not None:
        qs = qs.filter(is_escalated=data['is_escalated'])
    is_tarako = data.get('is_tarako')
    if is_tarako is not None:
        if is_tarako:
            qs = qs.filter(tags='tarako')
        else:
            qs = qs.filter(~F(tags='tarako'))
    return qs


class ApproveRegion(SlugOrIdMixin, CreateAPIView):
    """
    TODO: Document this API.
    """
    authentication_classes = (RestOAuthAuthentication,
                              RestSharedSecretAuthentication)
    model = Webapp
    slug_field = 'app_slug'

    def get_permissions(self):
        region = parse_region(self.request.parser_context['kwargs']['region'])
        region_slug = region.slug.upper()
        return (GroupPermission('Apps', 'ReviewRegion%s' % region_slug),)

    def get_queryset(self):
        region = parse_region(self.request.parser_context['kwargs']['region'])
        return self.model.objects.pending_in_region(region)

    def post(self, request, pk, region, *args, **kwargs):
        app = self.get_object()
        region = parse_region(region)

        form = ApproveRegionForm(request.DATA, app=app, region=region)
        if not form.is_valid():
            raise ParseError(dict(form.errors.items()))
        form.save()

        return Response({'approved': bool(form.cleaned_data['approve'])})


class GenerateToken(SlugOrIdMixin, CreateAPIView):
    """
    This generates a short-lived token to be used by the APK factory service
    for authentication of requests to the reviewer mini-manifest and package.

    """
    authentication_classes = (RestOAuthAuthentication,
                              RestSharedSecretAuthentication)
    permission_classes = [GroupPermission('Apps', 'Review')]
    model = Webapp
    slug_field = 'app_slug'

    def post(self, request, pk, *args, **kwargs):
        app = self.get_object()
        token = Token(data={'app_id': app.id})
        token.save()

        log.info('Generated token on app:%s for user:%s' % (
            app.id, request.amo_user.id))

        return Response({'token': token.token})


def _view_on_get(request):
    """Returns whether the user can access this page.

    If the user is in a group with rule 'ReviewerTools:View' and the request is
    a GET request, they are allowed to view.
    """
    return (request.method == 'GET' and
            acl.action_allowed(request, 'ReviewerTools', 'View'))


@never_cache
@json_view
@reviewer_required
def review_viewing(request):
    if 'addon_id' not in request.POST:
        return {}

    addon_id = request.POST['addon_id']
    user_id = request.amo_user.id
    current_name = ''
    is_user = 0
    key = '%s:review_viewing:%s' % (settings.CACHE_PREFIX, addon_id)
    interval = amo.EDITOR_VIEWING_INTERVAL

    # Check who is viewing.
    currently_viewing = cache.get(key)

    # If nobody is viewing or current user is, set current user as viewing
    if not currently_viewing or currently_viewing == user_id:
        # We want to save it for twice as long as the ping interval,
        # just to account for latency and the like.
        cache.set(key, user_id, interval * 2)
        currently_viewing = user_id
        current_name = request.amo_user.name
        is_user = 1
    else:
        current_name = UserProfile.objects.get(pk=currently_viewing).name

    AppsReviewing(request).add(addon_id)

    return {'current': currently_viewing, 'current_name': current_name,
            'is_user': is_user, 'interval_seconds': interval}


@never_cache
@json_view
@reviewer_required
def queue_viewing(request):
    if 'addon_ids' not in request.POST:
        return {}

    viewing = {}
    user_id = request.amo_user.id

    for addon_id in request.POST['addon_ids'].split(','):
        addon_id = addon_id.strip()
        key = '%s:review_viewing:%s' % (settings.CACHE_PREFIX, addon_id)
        currently_viewing = cache.get(key)
        if currently_viewing and currently_viewing != user_id:
            viewing[addon_id] = (UserProfile.objects
                                            .get(id=currently_viewing)
                                            .display_name)

    return viewing
