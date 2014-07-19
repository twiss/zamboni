import datetime
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.files.storage import default_storage as storage
from django.core.urlresolvers import reverse
from django.template import Context, loader

import elasticsearch
import pytz
import requests
from celery import chord
from celery.exceptions import RetryTaskError
from celeryutils import task
from requests.exceptions import RequestException
from test_utils import RequestFactory
from tower import ugettext as _

import amo
import mkt
from amo.decorators import use_master, write
from amo.helpers import absolutify
from amo.utils import chunked, days_ago, JSONEncoder, send_mail_jinja
from lib.es.models import Reindexing
from lib.metrics import get_monolith_client
from lib.post_request_task.task import task as post_request_task
from mkt.constants.regions import RESTOFWORLD
from mkt.developers.tasks import (_fetch_manifest, fetch_icon, pngcrush_image,
                                  resize_preview, validator)
from mkt.files.models import FileUpload
from mkt.files.utils import WebAppParser
from mkt.reviewers.models import RereviewQueue
from mkt.users.models import UserProfile
from mkt.users.utils import get_task_user
from mkt.webapps.indexers import WebappIndexer
from mkt.webapps.models import Addon, AppManifest, Preview, Webapp
from mkt.webapps.utils import get_locale_properties


task_log = logging.getLogger('z.task')


@task
@write
def version_changed(addon_id, **kw):
    update_last_updated(addon_id)


def update_last_updated(addon_id):
    qs = Addon._last_updated_queries()
    if not Webapp.objects.filter(pk=addon_id).exists():
        task_log.info(
            '[1@None] Updating last updated for %s failed, no addon found'
            % addon_id)
        return

    task_log.info('[1@None] Updating last updated for %s.' % addon_id)

    res = (qs.filter(pk=addon_id)
             .using('default')
             .values_list('id', 'last_updated'))
    if res:
        pk, t = res[0]
        Webapp.objects.filter(pk=pk).update(last_updated=t)


@task
def delete_preview_files(id, **kw):
    task_log.info('[1@None] Removing preview with id of %s.' % id)

    p = Preview(id=id)
    for f in (p.thumbnail_path, p.image_path):
        try:
            storage.delete(f)
        except Exception, e:
            task_log.error('Error deleting preview file (%s): %s' % (f, e))


def _get_content_hash(content):
    return 'sha256:%s' % hashlib.sha256(content).hexdigest()


def _log(webapp, message, rereview=False, exc_info=False):
    if rereview:
        message = u'(Re-review) ' + unicode(message)
    task_log.info(u'[Webapp:%s] %s' % (webapp, unicode(message)),
                  exc_info=exc_info)


@task
@write
def update_manifests(ids, **kw):
    retry_secs = 3600
    task_log.info('[%s@%s] Update manifests.' %
                  (len(ids), update_manifests.rate_limit))
    check_hash = kw.pop('check_hash', True)
    retries = kw.pop('retries', {})
    # Since we'll be logging the updated manifest change to the users log,
    # we'll need to log in as user.
    amo.set_user(get_task_user())

    for id in ids:
        _update_manifest(id, check_hash, retries)
    if retries:
        try:
            update_manifests.retry(args=(retries.keys(),),
                                   kwargs={'check_hash': check_hash,
                                           'retries': retries},
                                   eta=datetime.datetime.now() +
                                       datetime.timedelta(seconds=retry_secs),
                                   max_retries=5)
        except RetryTaskError:
            _log(id, 'Retrying task in %d seconds.' % retry_secs)

    return retries


def notify_developers_of_failure(app, error_message, has_link=False):
    if (app.status not in amo.WEBAPPS_APPROVED_STATUSES or
        RereviewQueue.objects.filter(addon=app).exists()):
        # If the app isn't public, or has already been reviewed, we don't
        # want to send the mail.
        return

    # FIXME: how to integrate with commbadge?

    for author in app.authors.all():
        context = {
            'error_message': error_message,
            'SITE_URL': settings.SITE_URL,
            'MKT_SUPPORT_EMAIL': settings.MKT_SUPPORT_EMAIL,
            'has_link': has_link
        }
        to = [author.email]
        with author.activate_lang():
            # Re-fetch the app to get translations in the right language.
            context['app'] = Webapp.objects.get(pk=app.pk)

            subject = _(u'Issue with your app "{app}" on the Firefox '
                        u'Marketplace').format(**context)
            send_mail_jinja(subject,
                            'webapps/emails/update_manifest_failure.txt',
                            context, recipient_list=to)


def _update_manifest(id, check_hash, failed_fetches):
    webapp = Webapp.objects.get(pk=id)
    version = webapp.versions.latest()
    file_ = version.files.latest()

    _log(webapp, u'Fetching webapp manifest')
    if not file_:
        _log(webapp, u'Ignoring, no existing file')
        return

    # Fetch manifest, catching and logging any exception.
    try:
        content = _fetch_manifest(webapp.manifest_url)
    except Exception, e:
        msg = u'Failed to get manifest from %s. Error: %s' % (
            webapp.manifest_url, e)
        failed_fetches[id] = failed_fetches.get(id, 0) + 1
        if failed_fetches[id] == 3:
            # This is our 3rd attempt, let's send the developer(s) an email to
            # notify him of the failures.
            notify_developers_of_failure(webapp, u'Validation errors:\n' + msg)
        elif failed_fetches[id] >= 4:
            # This is our 4th attempt, we should already have notified the
            # developer(s). Let's put the app in the re-review queue.
            _log(webapp, msg, rereview=True, exc_info=True)
            if webapp.status in amo.WEBAPPS_APPROVED_STATUSES:
                RereviewQueue.flag(webapp, amo.LOG.REREVIEW_MANIFEST_CHANGE,
                                   msg)
            del failed_fetches[id]
        else:
            _log(webapp, msg, rereview=False, exc_info=True)
        return

    # Check hash.
    if check_hash:
        hash_ = _get_content_hash(content)
        if file_.hash == hash_:
            _log(webapp, u'Manifest the same')
            return
        _log(webapp, u'Manifest different')

    # Validate the new manifest.
    upload = FileUpload.objects.create()
    upload.add_file([content], webapp.manifest_url, len(content))

    validator(upload.pk)

    upload = FileUpload.objects.get(pk=upload.pk)
    if upload.validation:
        v8n = json.loads(upload.validation)
        if v8n['errors']:
            v8n_url = absolutify(reverse(
                'mkt.developers.upload_detail', args=[upload.uuid]))
            msg = u'Validation errors:\n'
            for m in v8n['messages']:
                if m['type'] == u'error':
                    msg += u'* %s\n' % m['message']
            msg += u'\nValidation Result:\n%s' % v8n_url
            _log(webapp, msg, rereview=True)
            if webapp.status in amo.WEBAPPS_APPROVED_STATUSES:
                notify_developers_of_failure(webapp, msg, has_link=True)
                RereviewQueue.flag(webapp, amo.LOG.REREVIEW_MANIFEST_CHANGE,
                                   msg)
            return
    else:
        _log(webapp,
             u'Validation for upload UUID %s has no result' % upload.uuid)

    # Get the old manifest before we overwrite it.
    new = json.loads(content)
    old = webapp.get_manifest_json(file_)

    # New manifest is different and validates, update version/file.
    try:
        webapp.manifest_updated(content, upload)
    except:
        _log(webapp, u'Failed to create version', exc_info=True)

    # Check for any name changes at root and in locales. If any were added or
    # updated, send to re-review queue.
    msg = []
    rereview = False
    # Some changes require a new call to IARC's SET_STOREFRONT_DATA.
    iarc_storefront = False

    if old and old.get('name') != new.get('name'):
        rereview = True
        iarc_storefront = True
        msg.append(u'Manifest name changed from "%s" to "%s".' % (
            old.get('name'), new.get('name')))

    new_version = webapp.versions.latest()
    # Compare developer_name between old and new version using the property
    # that fallbacks to the author name instead of using the db field directly.
    # This allows us to avoid forcing a re-review on old apps which didn't have
    # developer name in their manifest initially and upload a new version that
    # does, providing that it matches the original author name.
    if version.developer_name != new_version.developer_name:
        rereview = True
        iarc_storefront = True
        msg.append(u'Developer name changed from "%s" to "%s".'
            % (version.developer_name, new_version.developer_name))

    # Get names in "locales" as {locale: name}.
    locale_names = get_locale_properties(new, 'name', webapp.default_locale)

    # Check changes to default_locale.
    locale_changed = webapp.update_default_locale(new.get('default_locale'))
    if locale_changed:
        msg.append(u'Default locale changed from "%s" to "%s".'
                   % locale_changed)

    # Update names
    crud = webapp.update_names(locale_names)
    if any(crud.values()):
        webapp.save()

    if crud.get('added'):
        rereview = True
        msg.append(u'Locales added: %s' % crud.get('added'))
    if crud.get('updated'):
        rereview = True
        msg.append(u'Locales updated: %s' % crud.get('updated'))

    # Check if supported_locales changed and update if so.
    webapp.update_supported_locales(manifest=new)

    if rereview:
        msg = ' '.join(msg)
        _log(webapp, msg, rereview=True)
        if webapp.status in amo.WEBAPPS_APPROVED_STATUSES:
            RereviewQueue.flag(webapp, amo.LOG.REREVIEW_MANIFEST_CHANGE, msg)

    if iarc_storefront:
        webapp.set_iarc_storefront_data()


@task
def update_cached_manifests(id, **kw):
    try:
        webapp = Webapp.objects.get(pk=id)
    except Webapp.DoesNotExist:
        _log(id, u'Webapp does not exist')
        return

    if not webapp.is_packaged:
        return

    # Rebuilds the packaged app mini manifest and stores it in cache.
    webapp.get_cached_manifest(force=True)
    _log(webapp, u'Updated cached mini manifest')


@task
@write
def add_uuids(ids, **kw):
    for chunk in chunked(ids, 50):
        for app in Webapp.objects.filter(id__in=chunk):
            # Save triggers the creation of a guid if the app doesn't currently
            # have one.
            app.save()


@task
@write
def update_supported_locales(ids, **kw):
    """
    Task intended to run via command line to update all apps' supported locales
    based on the current version.
    """
    for chunk in chunked(ids, 50):
        for app in Webapp.objects.filter(id__in=chunk):
            try:
                if app.update_supported_locales():
                    _log(app, u'Updated supported locales')
            except Exception:
                _log(app, u'Updating supported locales failed.', exc_info=True)


@post_request_task(acks_late=True)
@write
def index_webapps(ids, **kw):
    """TODO: use search/indexers.py:index."""
    task_log.info('Indexing apps %s-%s. [%s]' % (ids[0], ids[-1], len(ids)))

    index = kw.pop('index', WebappIndexer.get_index())
    # Note: If reindexing is currently occurring, `get_indices` will return
    # more than one index.
    indices = Reindexing.get_indices(index)

    es = WebappIndexer.get_es(urls=settings.ES_URLS)
    qs = Webapp.with_deleted.no_cache().filter(id__in=ids)
    for obj in qs:
        doc = WebappIndexer.extract_document(obj.id, obj)
        for idx in indices:
            WebappIndexer.index(doc, id_=obj.id, index=idx)


@post_request_task(acks_late=True)
@write
def unindex_webapps(ids, **kw):
    if not ids:
        return

    task_log.info('Un-indexing apps %s-%s. [%s]' % (ids[0], ids[-1], len(ids)))

    index = kw.pop('index', WebappIndexer.get_index())
    # Note: If reindexing is currently occurring, `get_indices` will return
    # more than one index.
    indices = Reindexing.get_indices(index)

    es = WebappIndexer.get_es(urls=settings.ES_URLS)
    for id_ in ids:
        for idx in indices:
            try:
                WebappIndexer.unindex(id_=id_, es=es, index=idx)
            except elasticsearch.NotFoundError:
                # Ignore if it's not there.
                task_log.info(
                    u'[Webapp:%s] Unindexing app but not found in index' % id_)


@task
def dump_app(id, **kw):
    from mkt.webapps.serializers import AppSerializer
    # Because @robhudson told me to.
    # Note: not using storage because all these operations should be local.
    target_dir = os.path.join(settings.DUMPED_APPS_PATH, 'apps',
                              str(id / 1000))
    target_file = os.path.join(target_dir, str(id) + '.json')

    try:
        obj = Webapp.objects.get(pk=id)
    except Webapp.DoesNotExist:
        task_log.info(u'Webapp does not exist: {0}'.format(id))
        return

    req = RequestFactory().get('/')
    req.user = AnonymousUser()
    req.REGION = RESTOFWORLD

    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    task_log.info('Dumping app {0} to {1}'.format(id, target_file))
    res = AppSerializer(obj, context={'request': req}).data
    json.dump(res, open(target_file, 'w'), cls=JSONEncoder)
    return target_file


@task
def clean_apps(pks, **kw):
    app_dir = os.path.join(settings.DUMPED_APPS_PATH, 'apps')
    rm_directory(app_dir)
    return pks


@task(ignore_result=False)
def dump_apps(ids, **kw):
    task_log.info(u'Dumping apps {0} to {1}. [{2}]'
                  .format(ids[0], ids[-1], len(ids)))
    for id in ids:
        dump_app(id)


@task
def zip_apps(*args, **kw):
    today = datetime.datetime.today().strftime('%Y-%m-%d')
    files = ['apps'] + compile_extra_files(date=today)
    tarball = compress_export(filename=today, files=files)
    link_latest_export(tarball)
    return tarball


def link_latest_export(tarball):
    """
    Atomically links basename(tarball) to
    DUMPED_APPS_PATH/tarballs/latest.tgz.
    """
    tarball_name = os.path.basename(tarball)
    target_dir = os.path.join(settings.DUMPED_APPS_PATH, 'tarballs')
    target_file = os.path.join(target_dir, 'latest.tgz')
    tmp_file = os.path.join(target_dir, '.latest.tgz')
    if os.path.lexists(tmp_file):
        os.unlink(tmp_file)

    os.symlink(tarball_name, tmp_file)
    os.rename(tmp_file, target_file)

    return target_file


def rm_directory(path):
    if os.path.exists(path):
        shutil.rmtree(path)


def dump_all_apps_tasks():
    all_pks = (Webapp.objects.visible()
                             .values_list('pk', flat=True)
                             .order_by('pk'))
    return [dump_apps.si(pks) for pks in chunked(all_pks, 100)]


@task
def export_data(name=None):
    from mkt.collections.tasks import dump_all_collections_tasks
    today = datetime.datetime.today().strftime('%Y-%m-%d')
    if name is None:
        name = today
    root = settings.DUMPED_APPS_PATH
    directories = ['apps', 'collections']
    for directory in directories:
        rm_directory(os.path.join(root, directory))
    files = directories + compile_extra_files(date=today)
    chord(dump_all_apps_tasks() + dump_all_collections_tasks(),
          compress_export.si(filename=name, files=files)).apply_async()


def compile_extra_files(date):
    # Put some .txt files in place.
    context = Context({'date': date, 'url': settings.SITE_URL})
    files = ['license.txt', 'readme.txt']
    if not os.path.exists(settings.DUMPED_APPS_PATH):
        os.makedirs(settings.DUMPED_APPS_PATH)
    created_files = []
    for f in files:
        template = loader.get_template('webapps/dump/apps/' + f)
        dest = os.path.join(settings.DUMPED_APPS_PATH, f)
        open(dest, 'w').write(template.render(context))
        created_files.append(f)
    return created_files


@task
def compress_export(filename, files):
    # Note: not using storage because all these operations should be local.
    target_dir = os.path.join(settings.DUMPED_APPS_PATH, 'tarballs')
    target_file = os.path.join(target_dir, filename + '.tgz')

    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    # Put some .txt files in place.
    cmd = ['tar', 'czf', target_file, '-C',
           settings.DUMPED_APPS_PATH] + files
    task_log.info(u'Creating dump {0}'.format(target_file))
    subprocess.call(cmd)
    return target_file


@task(ignore_result=False)
def dump_user_installs(ids, **kw):
    task_log.info(u'Dumping user installs {0} to {1}. [{2}]'
                  .format(ids[0], ids[-1], len(ids)))

    for user_id in ids:
        try:
            user = UserProfile.objects.get(pk=user_id)
        except UserProfile.DoesNotExist:
            task_log.info('User profile does not exist: {0}'.format(user_id))
            continue

        hash = hashlib.sha256('%s%s' % (str(user.id),
                                        settings.SECRET_KEY)).hexdigest()
        target_dir = os.path.join(settings.DUMPED_USERS_PATH, 'users', hash[0])
        target_file = os.path.join(target_dir, '%s.json' % hash)

        if not os.path.exists(target_dir):
            try:
                os.makedirs(target_dir)
            except OSError:
                pass  # Catch race condition if file exists now.

        # Gather data about user.
        installed = []
        zone = pytz.timezone(settings.TIME_ZONE)
        for install in user.installed_set.filter(addon__type=amo.ADDON_WEBAPP):
            try:
                app = install.addon
            except Addon.DoesNotExist:
                continue

            installed.append({
                'id': app.id,
                'slug': app.app_slug,
                'installed': pytz.utc.normalize(
                    zone.localize(install.created)).strftime(
                        '%Y-%m-%dT%H:%M:%S')
            })

        data = {
            'user': hash,
            'region': user.region,
            'lang': user.lang,
            'installed_apps': installed,
        }

        task_log.info('Dumping user {0} to {1}'.format(user.id, target_file))
        json.dump(data, open(target_file, 'w'), cls=JSONEncoder)


@task
def zip_users(*args, **kw):
    # Note: not using storage because all these operations should be local.
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    target_dir = os.path.join(settings.DUMPED_USERS_PATH, 'tarballs')
    target_file = os.path.join(target_dir, '{0}.tgz'.format(today))

    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    # Put some .txt files in place.
    context = Context({'date': today, 'url': settings.SITE_URL})
    files = ['license.txt', 'readme.txt']
    for f in files:
        template = loader.get_template('webapps/dump/users/' + f)
        dest = os.path.join(settings.DUMPED_USERS_PATH, 'users', f)
        open(dest, 'w').write(template.render(context))

    cmd = ['tar', 'czf', target_file, '-C',
           settings.DUMPED_USERS_PATH, 'users']
    task_log.info(u'Creating user dump {0}'.format(target_file))
    subprocess.call(cmd)
    return target_file


def _fix_missing_icons(id):
    try:
        webapp = Webapp.objects.get(pk=id)
    except Webapp.DoesNotExist:
        _log(id, u'Webapp does not exist')
        return

    # Check for missing icons. If we find one important size missing, call
    # fetch_icon for this app.
    dirname = webapp.get_icon_dir()
    destination = os.path.join(dirname, '%s' % webapp.id)
    for size in (64, 128):
        filename = '%s-%s.png' % (destination, size)
        if not storage.exists(filename):
            _log(id, u'Webapp is missing icon size %d' % (size, ))
            return fetch_icon(webapp)


@task
@write
def fix_missing_icons(ids, **kw):
    for id in ids:
        _fix_missing_icons(id)


def _regenerate_icons_and_thumbnails(pk):
    try:
        webapp = Webapp.objects.get(pk=pk)
    except Webapp.DoesNotExist:
        _log(id, u'Webapp does not exist')
        return

    # Previews.
    for preview in webapp.all_previews:
        # Re-resize each preview by calling the task with the image that we
        # have and asking the task to only deal with the thumbnail. We no
        # longer have the original, but it's fine, the image should be large
        # enough for us to generate a thumbnail.
        resize_preview.delay(preview.image_path, preview, generate_image=False)

    # Icons. The only thing we need to do is crush the 64x64 icon.
    icon_path = os.path.join(webapp.get_icon_dir(), '%s-64.png' % webapp.id)
    pngcrush_image.delay(icon_path)


@task
@write
def regenerate_icons_and_thumbnails(ids, **kw):
    for pk in ids:
        _regenerate_icons_and_thumbnails(pk)


@task
@write
def import_manifests(ids, **kw):
    for app in Webapp.objects.filter(id__in=ids):
        for version in app.versions.all():
            try:
                file_ = version.files.latest()
                if file_.status == amo.STATUS_DISABLED:
                    file_path = file_.guarded_file_path
                else:
                    file_path = file_.file_path
                manifest = WebAppParser().get_json_data(file_path)
                m, c = AppManifest.objects.get_or_create(
                    version=version, manifest=json.dumps(manifest))
                if c:
                    task_log.info(
                        '[Webapp:%s] Imported manifest for version %s' % (
                            app.id, version.id))
                else:
                    task_log.info(
                        '[Webapp:%s] App manifest exists for version %s' % (
                            app.id, version.id))
            except Exception as e:
                task_log.info('[Webapp:%s] Error loading manifest for version '
                              '%s: %s' % (app.id, version.id, e))


def _get_trending(app_id, region=None):
    """
    Calculate trending.

    a = installs from 7 days ago to now
    b = installs from 28 days ago to 8 days ago, averaged per week

    trending = (a - b) / b if a > 100 and b > 1 else 0

    """
    client = get_monolith_client()

    kwargs = {'app-id': app_id}
    if region:
        kwargs['region'] = region.slug

    today = datetime.datetime.today()

    # If we query monolith with interval=week and the past 7 days
    # crosses a Monday, Monolith splits the counts into two. We want
    # the sum over the past week so we need to `sum` these.
    try:
        count_1 = sum(
            c['count'] for c in
            client('app_installs', days_ago(7), today, 'week', **kwargs)
            if c.get('count'))
    except ValueError as e:
        task_log.info('Call to ES failed: {0}'.format(e))
        count_1 = 0

    # If count_1 isn't more than 100, stop here to avoid extra Monolith calls.
    if not count_1 > 100:
        return 0.0

    # Get the average installs for the prior 3 weeks. Don't use the `len` of
    # the returned counts because of week boundaries.
    try:
        count_3 = sum(
            c['count'] for c in
            client('app_installs', days_ago(28), days_ago(8), 'week', **kwargs)
            if c.get('count')) / 3
    except ValueError as e:
        task_log.info('Call to ES failed: {0}'.format(e))
        count_3 = 0

    if count_3 > 1:
        return (count_1 - count_3) / count_3
    else:
        return 0.0


@task
@write
def update_trending(ids, **kw):
    count = 0
    times = []

    for app in Webapp.objects.filter(id__in=ids).no_transforms():

        count += 1
        t_start = time.time()

        # Calculate global trending, then per-region trending below.
        value = _get_trending(app.id)
        if value:
            trending, created = app.trending.get_or_create(
                region=0, defaults={'value': value})
            if not created:
                trending.update(value=value)

        for region in mkt.regions.REGIONS_DICT.values():
            value = _get_trending(app.id, region)
            if value:
                trending, created = app.trending.get_or_create(
                    region=region.id, defaults={'value': value})
                if not created:
                    trending.update(value=value)

        times.append(time.time() - t_start)

    task_log.info('Trending calculated for %s apps. Avg time overall: '
                  '%0.2fs' % (count, sum(times) / count))


@task
@write
def update_downloads(ids, **kw):
    client = get_monolith_client()
    count = 0

    for app in Webapp.objects.filter(id__in=ids).no_transforms():

        appid = {'app-id': app.id}

        # Get weekly downloads.
        query = {
            'query': {'match_all': {}},
            'facets': {
                'installs': {
                    'date_histogram': {
                        'value_field': 'app_installs',
                        'interval': 'week',
                        'key_field': 'date',
                    },
                    'facet_filter': {
                        'and': [
                            {'term': appid},
                            {'range': {'date': {
                                'gte': days_ago(8).date().strftime('%Y-%m-%d'),
                                'lte': days_ago(1).date().strftime('%Y-%m-%d'),
                            }}}
                        ]
                    }
                }
            },
            'size': 0}

        try:
            resp = client.raw(query)
            # If we query monolith with interval=week and the past 7 days
            # crosses a Monday, Monolith splits the counts into two. We want
            # the sum over the past week so we need to `sum` these.
            weekly = sum(
                c['total'] for c in
                resp.get('facets', {}).get('installs', {}).get('entries')
                if c.get('total'))
        except Exception as e:
            task_log.info('Call to ES failed: {0}'.format(e))
            weekly = 0

        # Get total downloads.
        query = {'query': {'match_all': {}},
                 'facets': {
                     'installs': {
                         'statistical': {'field': 'app_installs'},
                         'facet_filter': {'term': appid}}},
                 'size': 0}
        try:
            resp = client.raw(query)
            total = resp.get('facets', {}).get('installs', {}).get('total', 0)
        except Exception as e:
            task_log.info('Call to ES failed: {0}'.format(e))
            total = 0

        # Update Webapp object, if needed.
        update = False
        signal = False
        if weekly != app.weekly_downloads:
            update = True
            signal = True
        if total != app.total_downloads:
            update = True

        if update:
            # Note: Calling `update` will trigger a reindex on the app if
            # `_signal` is True. Since we only index `weekly_downloads`, we
            # can skip reindexing if this hasn't changed.
            count += 1
            app.update(weekly_downloads=weekly, total_downloads=total,
                       _signal=signal)

    task_log.info('App downloads updated for %s out of %s apps.'
                  % (count, len(ids)))


class PreGenAPKError(Exception):
    """
    An error encountered while trying to pre-generate an APK.
    """


@task
@use_master
def pre_generate_apk(app_id, **kw):
    app = Webapp.objects.get(pk=app_id)
    manifest_url = app.get_manifest_url()
    task_log.info('pre-generating APK for app {a} at {url}'
                  .format(a=app, url=manifest_url))
    if not manifest_url:
        raise PreGenAPKError('Webapp {w} has an empty manifest URL'
                             .format(w=app))
    try:
        res = requests.get(settings.PRE_GENERATE_APK_URL,
                           params={'manifestUrl': manifest_url})
        res.raise_for_status()
    except RequestException, exc:
        raise PreGenAPKError('Error pre-generating APK for app {a} at {url}; '
                             'generator={gen} (SSL cert ok?); '
                             '{e.__class__.__name__}: {e}'
                             .format(a=app, url=manifest_url, e=exc,
                                     gen=settings.PRE_GENERATE_APK_URL))

    # The factory returns a binary APK blob but we don't need it.
    res.close()
    del res
