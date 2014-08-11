import functools

from django import http
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

import commonware.log

from mkt.webapps.models import Webapp


log = commonware.log.getLogger('mkt.purchase')


def has_purchased(f):
    """
    If the addon is premium, require a purchase.
    Must be called after addon_view decorator.
    """
    @functools.wraps(f)
    def wrapper(request, addon, *args, **kw):
        if addon.is_premium() and not addon.has_purchased(request.user):
            log.info('Not purchased: %d' % addon.pk)
            raise PermissionDenied
        return f(request, addon, *args, **kw)
    return wrapper


def can_become_premium(f):
    """Check that the addon can become premium."""
    @functools.wraps(f)
    def wrapper(request, addon_id, addon, *args, **kw):
        if not addon.can_become_premium():
            log.info('Cannot become premium: %d' % addon.pk)
            raise PermissionDenied
        return f(request, addon_id, addon, *args, **kw)
    return wrapper


def app_view(f, qs=Webapp.objects.all):
    @functools.wraps(f)
    def wrapper(request, addon_id=None, app_slug=None, *args,
                **kw):
        """Provides an addon given either an addon_id or app_slug."""
        assert addon_id or app_slug, 'Must provide addon_id or app_slug'
        get = lambda **kw: get_object_or_404(qs(), **kw)
        if addon_id and addon_id.isdigit():
            addon = get(id=addon_id)
            # Don't get in an infinite loop if addon.app_slug.isdigit().
            if addon.app_slug != addon_id:
                url = request.path.replace(addon_id, addon.app_slug, 1)
                if request.GET:
                    url += '?' + request.GET.urlencode()
                return http.HttpResponsePermanentRedirect(url)
        elif addon_id:
            addon = get(app_slug=addon_id)
        elif app_slug:
            addon = get(app_slug=app_slug)
        return f(request, addon, *args, **kw)
    return wrapper


def app_view_factory(qs):
    """
    Don't evaluate qs or the locale will get stuck on whatever the server
    starts with. The app_view() decorator will call qs with no arguments before
    doing anything, so lambdas are ok.

        GOOD: Webapp.objects.valid
        GOOD: lambda: Webapp.objects.valid().filter(type=1)
        BAD: Webapp.objects.valid()

    """
    return functools.partial(app_view, qs=qs)
