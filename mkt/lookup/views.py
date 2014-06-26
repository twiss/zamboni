import hashlib
import json
import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.core.urlresolvers import reverse
from django.db import connection
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render

import commonware.log
from babel import numbers
from elasticutils.contrib.django import S
from slumber.exceptions import HttpClientError, HttpServerError
from tower import ugettext as _

import amo
import mkt.constants.lookup as lkp
from amo.decorators import (json_view, login_required, permission_required,
                            post_required)
from amo.utils import paginate
from lib.pay_server import client
from mkt.access import acl
from mkt.account.utils import purchase_list
from mkt.comm.utils import create_comm_note
from mkt.constants import comm
from mkt.constants.payments import COMPLETED, FAILED, PENDING, REFUND_STATUSES
from mkt.developers.models import ActivityLog, AddonPaymentAccount
from mkt.developers.providers import get_provider
from mkt.developers.views_payments import _redirect_to_bango_portal
from mkt.lookup.forms import (APIFileStatusForm, APIStatusForm, DeleteUserForm,
                              TransactionRefundForm, TransactionSearchForm)
from mkt.lookup.tasks import (email_buyer_refund_approved,
                              email_buyer_refund_pending)
from mkt.prices.models import AddonPaymentData, Refund
from mkt.purchase.models import Contribution
from mkt.site import messages
from mkt.users.models import UserProfile
from mkt.webapps.indexers import WebappIndexer
from mkt.webapps.models import Webapp


log = commonware.log.getLogger('z.lookup')


@login_required
@permission_required('Lookup', 'View')
def home(request):
    tx_form = TransactionSearchForm()

    return render(request, 'lookup/home.html', {'tx_form': tx_form})


@login_required
@permission_required('AccountLookup', 'View')
def user_summary(request, user_id):
    user = get_object_or_404(UserProfile, pk=user_id)
    is_admin = acl.action_allowed(request, 'Users', 'Edit')
    app_summary = _app_summary(user.pk)
    # All refunds that this user has requested (probably as a consumer).
    req = Refund.objects.filter(contribution__user=user)
    # All instantly-approved refunds that this user has requested.
    appr = req.filter(status=amo.REFUND_APPROVED_INSTANT)
    refund_summary = {'approved': appr.count(),
                      'requested': req.count()}
    # TODO: This should return all `addon` types and not just webapps.
    # -- currently get_details_url() fails on non-webapps so this is a
    # temp fix.
    user_addons = (user.addons.filter(type=amo.ADDON_WEBAPP)
                              .order_by('-created'))
    user_addons = paginate(request, user_addons, per_page=15)

    payment_data = (AddonPaymentData.objects.filter(addon__authors=user)
                    .values(*AddonPaymentData.address_fields())
                    .distinct())

    # If the user is deleted, get the log detailing the delete.
    try:
        delete_log = ActivityLog.objects.for_user(user).filter(
            action=amo.LOG.DELETE_USER_LOOKUP.id)[0]
    except IndexError:
        delete_log = None

    provider_portals = get_payment_provider_portals(user=user)
    return render(request, 'lookup/user_summary.html',
                  {'account': user, 'app_summary': app_summary,
                   'delete_form': DeleteUserForm(), 'delete_log': delete_log,
                   'is_admin': is_admin, 'refund_summary': refund_summary,
                   'user_addons': user_addons, 'payment_data': payment_data,
                   'provider_portals': provider_portals})


@login_required
@permission_required('AccountLookup', 'View')
def user_delete(request, user_id):
    delete_form = DeleteUserForm(request.POST)
    if not delete_form.is_valid():
        messages.error(request, delete_form.errors)
        return HttpResponseRedirect(reverse('lookup.user_summary',
                                    args=[user_id]))

    user = get_object_or_404(UserProfile, pk=user_id)
    user.deleted = True
    user.save()  # Must call the save function to delete user.
    amo.log(amo.LOG.DELETE_USER_LOOKUP, user,
            details={'reason': delete_form.cleaned_data['delete_reason']},
            user=request.amo_user)

    return HttpResponseRedirect(reverse('lookup.user_summary', args=[user_id]))


@login_required
@permission_required('Transaction', 'View')
def transaction_summary(request, tx_uuid):
    tx_data = _transaction_summary(tx_uuid)
    if not tx_data:
        raise Http404

    tx_form = TransactionSearchForm()
    tx_refund_form = TransactionRefundForm()

    return render(request, 'lookup/transaction_summary.html',
                  dict({'uuid': tx_uuid, 'tx_form': tx_form,
                        'tx_refund_form': tx_refund_form}.items() +
                            tx_data.items()))


def _transaction_summary(tx_uuid):
    """Get transaction details from Solitude API."""
    contrib = get_object_or_404(Contribution, uuid=tx_uuid)
    refund_contribs = contrib.get_refund_contribs()
    refund_contrib = refund_contribs[0] if refund_contribs.exists() else None

    # Get refund status.
    refund_status = None
    if refund_contrib and refund_contrib.refund.status == amo.REFUND_PENDING:
        try:
            refund_status = REFUND_STATUSES[client.api.bango.refund.status.get(
                data={'uuid': refund_contrib.transaction_id})['status']]
        except HttpServerError:
            refund_status = _('Currently unable to retrieve refund status.')

    return {
        # Solitude data.
        'refund_status': refund_status,

        # Zamboni data.
        'app': contrib.addon,
        'contrib': contrib,
        'related': contrib.related,
        'type': amo.CONTRIB_TYPES.get(contrib.type, _('Incomplete')),
        # Whitelist what is refundable.
        'is_refundable': ((contrib.type == amo.CONTRIB_PURCHASE)
                          and not refund_contrib),
    }


@post_required
@login_required
@permission_required('Transaction', 'Refund')
def transaction_refund(request, tx_uuid):
    contrib = get_object_or_404(Contribution, uuid=tx_uuid,
                                type=amo.CONTRIB_PURCHASE)
    refund_contribs = contrib.get_refund_contribs()
    refund_contrib = refund_contribs[0] if refund_contribs.exists() else None

    if refund_contrib:
        messages.error(request, _('A refund has already been processed.'))
        return redirect(reverse('lookup.transaction_summary', args=[tx_uuid]))

    form = TransactionRefundForm(request.POST)
    if not form.is_valid():
        return render(request, 'lookup/transaction_summary.html',
                      dict({'uuid': tx_uuid, 'tx_refund_form': form,
                            'tx_form': TransactionSearchForm()}.items() +
                           _transaction_summary(tx_uuid).items()))

    data = {'uuid': contrib.transaction_id,
            'manual': form.cleaned_data['manual']}
    if settings.BANGO_FAKE_REFUNDS:
        data['fake_response_status'] = {'responseCode':
                                        form.cleaned_data['fake']}

    try:
        res = client.api.bango.refund.post(data)
    except (HttpClientError, HttpServerError):
        # Either doing something not supposed to or Solitude had an issue.
        log.exception('Refund error: %s' % tx_uuid)
        messages.error(
            request,
            _('You cannot make a refund request for this transaction.'))
        return redirect(reverse('lookup.transaction_summary', args=[tx_uuid]))

    if res['status'] in [PENDING, COMPLETED]:
        # Create refund Contribution by cloning the payment Contribution.
        refund_contrib = Contribution.objects.get(id=contrib.id)
        refund_contrib.id = None
        refund_contrib.save()
        refund_contrib.update(
            type=amo.CONTRIB_REFUND, related=contrib,
            uuid=hashlib.md5(str(uuid.uuid4())).hexdigest(),
            amount=-refund_contrib.amount if refund_contrib.amount else None,
            transaction_id=res['uuid'])

    if res['status'] == PENDING:
        # Create pending Refund.
        refund_contrib.enqueue_refund(
            amo.REFUND_PENDING, request.amo_user,
            refund_reason=form.cleaned_data['refund_reason'])
        log.info('Refund pending: %s' % tx_uuid)
        email_buyer_refund_pending(contrib)
        messages.success(
            request, _('Refund for this transaction now pending.'))
    elif res['status'] == COMPLETED:
        # Create approved Refund.
        refund_contrib.enqueue_refund(
            amo.REFUND_APPROVED, request.amo_user,
            refund_reason=form.cleaned_data['refund_reason'])
        log.info('Refund approved: %s' % tx_uuid)
        email_buyer_refund_approved(contrib)
        messages.success(
            request, _('Refund for this transaction successfully approved.'))
    elif res['status'] == FAILED:
        # Bango no like.
        log.error('Refund failed: %s' % tx_uuid)
        messages.error(
            request, _('Refund request for this transaction failed.'))

    return redirect(reverse('lookup.transaction_summary', args=[tx_uuid]))


@login_required
@permission_required('AppLookup', 'View')
def app_summary(request, addon_id):
    app = get_object_or_404(Webapp.with_deleted, pk=addon_id)

    if 'prioritize' in request.POST and not app.priority_review:
        app.update(priority_review=True)
        msg = u'Priority Review Requested'
        # Create notes and log entries.
        create_comm_note(app, app.latest_version, request.amo_user, msg,
                         note_type=comm.NO_ACTION)
        amo.log(amo.LOG.PRIORITY_REVIEW_REQUESTED, app, app.latest_version,
                created=datetime.now(), details={'comments': msg})

    authors = (app.authors.filter(addonuser__role__in=(amo.AUTHOR_ROLE_DEV,
                                                       amo.AUTHOR_ROLE_OWNER))
                          .order_by('display_name'))

    if app.premium and app.premium.price:
        price = app.premium.price
    else:
        price = None

    purchases, refunds = _app_purchases_and_refunds(app)
    provider_portals = get_payment_provider_portals(app=app)
    versions = None

    status_form = APIStatusForm(initial={
        'status': amo.STATUS_CHOICES_API[app.status]
    })
    version_status_forms = {}
    if app.is_packaged:
        versions = app.versions.all().order_by('-created')
        for v in versions:
            version_status_forms[v.pk] = APIFileStatusForm(initial={
                'status': amo.STATUS_CHOICES_API[v.all_files[0].status]
            })

    return render(request, 'lookup/app_summary.html', {
        'abuse_reports': app.abuse_reports.count(), 'app': app,
        'authors': authors, 'downloads': _app_downloads(app),
        'purchases': purchases, 'refunds': refunds, 'price': price,
        'provider_portals': provider_portals,
        'status_form': status_form, 'versions': versions,
        'version_status_forms': version_status_forms
    })


@login_required
@permission_required('AccountLookup', 'View')
def app_activity(request, addon_id):
    """Shows the app activity age for single app."""
    app = get_object_or_404(Webapp.with_deleted, pk=addon_id)

    user_items = ActivityLog.objects.for_apps([app]).exclude(
        action__in=amo.LOG_HIDE_DEVELOPER)
    admin_items = ActivityLog.objects.for_apps([app]).filter(
        action__in=amo.LOG_HIDE_DEVELOPER)

    user_items = paginate(request, user_items, per_page=20)
    admin_items = paginate(request, admin_items, per_page=20)

    return render(request, 'lookup/app_activity.html', {
        'admin_items': admin_items, 'app': app, 'user_items': user_items})


@login_required
@permission_required('BangoPortal', 'Redirect')
def bango_portal_from_package(request, package_id):
    response = _redirect_to_bango_portal(package_id,
                                         'package_id: %s' % package_id)
    if 'Location' in response:
        return HttpResponseRedirect(response['Location'])
    else:
        message = (json.loads(response.content)
                       .get('__all__', response.content)[0])
        messages.error(request, message)
        return HttpResponseRedirect(reverse('lookup.home'))


@login_required
@permission_required('AccountLookup', 'View')
def user_purchases(request, user_id):
    """Shows the purchase page for another user."""
    user = get_object_or_404(UserProfile, pk=user_id)
    is_admin = acl.action_allowed(request, 'Users', 'Edit')
    products, contributions, listing = purchase_list(request, user, None)
    return render(request, 'lookup/user_purchases.html',
                  {'pager': products, 'account': user, 'is_admin': is_admin,
                   'listing_filter': listing, 'contributions': contributions,
                   'single': bool(None), 'show_link': False})


@login_required
@permission_required('AccountLookup', 'View')
def user_activity(request, user_id):
    """Shows the user activity page for another user."""
    user = get_object_or_404(UserProfile, pk=user_id)
    products, contributions, listing = purchase_list(request, user, None)
    is_admin = acl.action_allowed(request, 'Users', 'Edit')

    user_items = ActivityLog.objects.for_user(user).exclude(
        action__in=amo.LOG_HIDE_DEVELOPER)
    admin_items = ActivityLog.objects.for_user(user).filter(
        action__in=amo.LOG_HIDE_DEVELOPER)
    amo.log(amo.LOG.ADMIN_VIEWED_LOG, request.amo_user, user=user)
    return render(request, 'lookup/user_activity.html',
                  {'pager': products, 'account': user, 'is_admin': is_admin,
                   'listing_filter': listing,
                   'contributions': contributions, 'single': bool(None),
                   'user_items': user_items, 'admin_items': admin_items,
                   'show_link': False})


def _expand_query(q, fields):
    query = {}
    rules = [
        ('term', {'value': q, 'boost': 10}),
        ('match', {'query': q, 'boost': 4, 'type': 'phrase'}),
        ('match', {'query': q, 'boost': 3}),
        ('fuzzy', {'value': q, 'boost': 2, 'prefix_length': 4}),
        ('startswith', {'value': q, 'boost': 1.5}),
    ]
    for k, v in rules:
        for field in fields:
            query['%s__%s' % (field, k)] = v
    return query


@login_required
@permission_required('AccountLookup', 'View')
@json_view
def user_search(request):
    results = []
    q = request.GET.get('q', u'').lower().strip()
    search_fields = ('username', 'display_name', 'email')
    fields = ('id',) + search_fields

    if q.isnumeric():
        # id is added implictly by the ES filter. Add it explicitly:
        qs = UserProfile.objects.filter(pk=q).values(*fields)
    else:
        qs = UserProfile.objects.all()
        filters = Q()
        for field in search_fields:
            filters = filters | Q(**{'%s__icontains' % field: q})
        qs = qs.filter(filters)
        qs = qs.values(*fields)
        qs = _slice_results(request, qs)
    for user in qs:
        user['url'] = reverse('lookup.user_summary', args=[user['id']])
        user['name'] = user['username']
        results.append(user)
    return {'results': results}


@login_required
@permission_required('Transaction', 'View')
def transaction_search(request):
    tx_form = TransactionSearchForm(request.GET)
    if tx_form.is_valid():
        return redirect(reverse('lookup.transaction_summary',
                                args=[tx_form.cleaned_data['q']]))
    else:
        return render(request, 'lookup/home.html', {'tx_form': tx_form})


@login_required
@permission_required('AppLookup', 'View')
@json_view
def app_search(request):
    results = []
    q = request.GET.get('q', u'').lower().strip()
    fields = ('name', 'app_slug')
    non_es_fields = ['id', 'name__localized_string'] + list(fields)
    if q.isnumeric():
        qs = (Webapp.objects.filter(pk=q)
                            .values(*non_es_fields))
    else:
        # Try to load by GUID:
        qs = (Webapp.objects.filter(guid=q)
                            .values(*non_es_fields))
        if not qs.count():
            qs = (S(WebappIndexer)
                  .query(should=True, **_expand_query(q, fields))
                  .values_dict(*['id'] + list(fields)))
        qs = _slice_results(request, qs)
    for app in qs:
        if 'name__localized_string' in app:
            # This is a result from the database.
            app['url'] = reverse('lookup.app_summary', args=[app['id']])
            app['name'] = app['name__localized_string']
            results.append(app)
        else:
            # This is a result from elasticsearch which returns name as a list.
            app['url'] = reverse('lookup.app_summary', args=[app['id']])
            for field in ('id', 'app_slug'):
                app[field] = app.get(field)
            for name in app['name']:
                dd = app.copy()
                dd['name'] = name
                results.append(dd)
    return {'results': results}


def _app_downloads(app):
    stats = {'last_7_days': 0,
             'last_24_hours': 0,
             'alltime': 0}

    # Webapps populate these fields via Monolith.
    stats['last_24_hours'] = 'N/A'
    stats['last_7_days'] = app.weekly_downloads
    stats['alltime'] = app.total_downloads
    return stats


def _app_summary(user_id):
    sql = """
        select currency,
            sum(case when type=%(purchase)s then 1 else 0 end)
                as app_total,
            sum(case when type=%(purchase)s then amount else 0.0 end)
                as app_amount
        from stats_contributions
        where user_id=%(user_id)s
        group by currency
    """
    cursor = connection.cursor()
    cursor.execute(sql, {'user_id': user_id,
                         'purchase': amo.CONTRIB_PURCHASE})
    summary = {'app_total': 0,
               'app_amount': {}}
    cols = [cd[0] for cd in cursor.description]
    while 1:
        row = cursor.fetchone()
        if not row:
            break
        row = dict(zip(cols, row))
        for cn in cols:
            if cn.endswith('total'):
                summary[cn] += row[cn]
            elif cn.endswith('amount'):
                summary[cn][row['currency']] = row[cn]
    return summary


def _app_purchases_and_refunds(addon):
    purchases = {}
    now = datetime.now()
    base_qs = (Contribution.objects.values('currency')
                           .annotate(total=Count('id'),
                                     amount=Sum('amount'))
                           .filter(addon=addon)
                           .exclude(type__in=[amo.CONTRIB_REFUND,
                                              amo.CONTRIB_CHARGEBACK,
                                              amo.CONTRIB_PENDING]))
    for typ, start_date in (('last_24_hours', now - timedelta(hours=24)),
                            ('last_7_days', now - timedelta(days=7)),
                            ('alltime', None),):
        qs = base_qs.all()
        if start_date:
            qs = qs.filter(created__gte=start_date)
        sums = list(qs)
        purchases[typ] = {'total': sum(s['total'] for s in sums),
                          'amounts': [numbers.format_currency(s['amount'],
                                                              s['currency'])
                                      for s in sums if s['currency']]}
    refunds = {}
    rejected_q = Q(status=amo.REFUND_DECLINED) | Q(status=amo.REFUND_FAILED)
    qs = Refund.objects.filter(contribution__addon=addon)

    refunds['requested'] = qs.exclude(rejected_q).count()
    percent = 0.0
    total = purchases['alltime']['total']
    if total:
        percent = (refunds['requested'] / float(total)) * 100.0
    refunds['percent_of_purchases'] = '%.1f%%' % percent
    refunds['auto-approved'] = (qs.filter(status=amo.REFUND_APPROVED_INSTANT)
                                .count())
    refunds['approved'] = qs.filter(status=amo.REFUND_APPROVED).count()
    refunds['rejected'] = qs.filter(rejected_q).count()

    return purchases, refunds


def _slice_results(request, qs):
    if request.GET.get('all_results'):
        return qs[:lkp.MAX_RESULTS]
    else:
        return qs[:lkp.SEARCH_LIMIT]


def get_payment_provider_portals(app=None, user=None):
    """
    Get a list of dicts describing the payment portals for this app or user.

    Either app or user is required.
    """
    provider_portals = []
    if app:
        q = dict(addon=app)
    elif user:
        q = dict(payment_account__user=user)
    else:
        raise ValueError('user or app is required')

    for acct in (AddonPaymentAccount.objects.filter(**q)
                 .select_related('payment_account')):
        provider = get_provider(id=acct.payment_account.provider)
        portal_url = provider.get_portal_url(acct.addon.app_slug)
        if portal_url:
            provider_portals.append({
                'provider': provider,
                'app': acct.addon,
                'portal_url': portal_url,
                'payment_account': acct.payment_account
            })
    return provider_portals
