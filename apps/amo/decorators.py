import datetime
import functools
import json

from django import http
from django.conf import settings
from django.core.exceptions import PermissionDenied

import commonware.log

from . import models as context
from .utils import JSONEncoder, redirect_for_login

from amo import get_user, set_user
from mkt.users.utils import get_task_user


task_log = commonware.log.getLogger('z.task')


def login_required(f=None, redirect=True):
    """
    Like Django's login_required, but with to= instead of next=.

    If redirect=False then we return 401 instead of redirecting to the
    login page.  That's nice for ajax views.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(request, *args, **kw):
            if request.user.is_authenticated():
                return func(request, *args, **kw)
            else:
                if redirect:
                    return redirect_for_login(request)
                else:
                    return http.HttpResponse(status=401)
        return wrapper
    if f:
        return decorator(f)
    else:
        return decorator


def post_required(f):
    @functools.wraps(f)
    def wrapper(request, *args, **kw):
        if request.method != 'POST':
            return http.HttpResponseNotAllowed(['POST'])
        else:
            return f(request, *args, **kw)
    return wrapper


def permission_required(app, action):
    def decorator(f):
        @functools.wraps(f)
        @login_required
        def wrapper(request, *args, **kw):
            from mkt.access import acl
            if acl.action_allowed(request, app, action):
                return f(request, *args, **kw)
            else:
                raise PermissionDenied
        return wrapper
    return decorator


def any_permission_required(pairs):
    """
    If any permission passes, call the function. Otherwise raise 403.
    """
    def decorator(f):
        @functools.wraps(f)
        @login_required
        def wrapper(request, *args, **kw):
            from mkt.access import acl
            for app, action in pairs:
                if acl.action_allowed(request, app, action):
                    return f(request, *args, **kw)
            raise PermissionDenied
        return wrapper
    return decorator


def restricted_content(f):
    """
    Prevent access to a view function for accounts restricted from
    posting user-generated content.
    """
    @functools.wraps(f)
    def wrapper(request, *args, **kw):
        from mkt.access import acl
        if (acl.action_allowed(request, '*', '*')
            or not acl.action_allowed(request, 'Restricted', 'UGC')):
            return f(request, *args, **kw)
        else:
            raise PermissionDenied
    return wrapper


def modal_view(f):
    @functools.wraps(f)
    def wrapper(*args, **kw):
        response = f(*args, modal=True, **kw)
        return response
    return wrapper


def json_response(response, has_trans=False, status_code=200):
    """
    Return a response as JSON. If you are just wrapping a view,
    then use the json_view decorator.
    """
    if has_trans:
        response = json.dumps(response, cls=JSONEncoder)
    else:
        response = json.dumps(response)
    return http.HttpResponse(response,
                             content_type='application/json',
                             status=status_code)


def json_view(f=None, has_trans=False, status_code=200):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kw):
            response = func(*args, **kw)
            if isinstance(response, http.HttpResponse):
                return response
            else:
                return json_response(response, has_trans=has_trans,
                                     status_code=status_code)
        return wrapper
    if f:
        return decorator(f)
    else:
        return decorator


json_view.error = lambda s: http.HttpResponseBadRequest(
    json.dumps(s), content_type='application/json')


class skip_cache(object):
    def __init__(self, f):
        self.f = f
        functools.update_wrapper(self, f)

    def __call__(self, *args, **kw):
        with context.skip_cache():
            return self.f(*args, **kw)

    def __repr__(self):
        "<SkipCache %s>" % (self.f,)

    def __get__(self, obj, typ=None):
        return skip_cache(self.f.__get__(obj, typ))


def use_master(f):
    @functools.wraps(f)
    def wrapper(*args, **kw):
        with context.use_master():
            return f(*args, **kw)
    return wrapper


def write(f):
    return use_master(skip_cache(f))


def set_modified_on(f):
    """
    Will update the modified timestamp on the provided objects when the wrapped
    function exits sucessfully (returns a truthy value). If the function
    returns a dict, it will also use that dict as additional keyword arguments
    to update on the provided objects.

    Looks up objects defined in the set_modified_on kwarg.
    """
    from amo.tasks import set_modified_on_object

    @functools.wraps(f)
    def wrapper(*args, **kw):
        objs = kw.pop('set_modified_on', None)
        result = f(*args, **kw)
        if objs and result:
            extra_kwargs = result if isinstance(result, dict) else {}
            for obj in objs:
                task_log.info('Delaying setting modified on object: %s, %s' %
                              (obj.__class__.__name__, obj.pk))
                set_modified_on_object.apply_async(
                    args=[obj], kwargs=extra_kwargs,
                    eta=datetime.datetime.now() +
                        datetime.timedelta(seconds=settings.NFS_LAG_DELAY))
        return result
    return wrapper


def allow_cross_site_request(f):
    """Allow other sites to access this resource, see
    https://developer.mozilla.org/en/HTTP_access_control."""
    @functools.wraps(f)
    def wrapper(request, *args, **kw):
        response = f(request, *args, **kw)
        """If Access-Control-Allow-Credentials isn't set, the browser won't
        return data required cookies to see.  This is a good thing, let's keep
        it that way."""
        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        return response
    return wrapper


def set_task_user(f):
    """Sets the user to be the task user, then unsets it."""
    @functools.wraps(f)
    def wrapper(*args, **kw):
        old_user = get_user()
        set_user(get_task_user())
        try:
            result = f(*args, **kw)
        finally:
            set_user(old_user)
        return result
    return wrapper


def allow_mine(f):
    @functools.wraps(f)
    def wrapper(request, username, *args, **kw):
        """
        If the author is `mine` then show the current user's collection
        (or something).
        """
        if username == 'mine':
            if not request.user.is_authenticated():
                return redirect_for_login(request)
            username = request.user.username
        return f(request, username, *args, **kw)
    return wrapper
