#-*- coding: utf-8 -*-
import hashlib
import hmac
import urllib
from threading import local
from urlparse import urlparse, urlsplit, urlunsplit

import bleach
import jinja2

from django.conf import settings
from django.core import urlresolvers
from django.utils import encoding
from django.utils.translation.trans_real import parse_accept_lang_header

import amo


# Get a pointer to Django's reverse because we're going to hijack it after we
# define our own.
django_reverse = urlresolvers.reverse


# Thread-local storage for URL prefixes.  Access with {get,set}_url_prefix.
_local = local()


def set_url_prefix(prefix):
    """Set ``prefix`` for the current thread."""
    _local.prefix = prefix


def get_url_prefix():
    """Get the prefix for the current thread, or None."""
    return getattr(_local, 'prefix', None)


def clean_url_prefixes():
    """Purge prefix cache."""
    if hasattr(_local, 'prefix'):
        delattr(_local, 'prefix')


def get_app_redirect(app):
    """Redirect request to another app."""
    prefixer = get_url_prefix()
    old_app = prefixer.app
    prefixer.app = app.short
    (_, _, url) = prefixer.split_path(prefixer.request.get_full_path())
    new_url = prefixer.fix(url)
    prefixer.app = old_app
    return new_url


def reverse(viewname, urlconf=None, args=None, kwargs=None, prefix=None,
            current_app=None, add_prefix=True):
    """Wraps django's reverse to prepend the correct locale and app."""
    prefixer = get_url_prefix()
    prefix = None
    # Blank out the script prefix since we add that in prefixer.fix().
    if prefixer:
        prefix = prefix or '/'
    url = django_reverse(viewname, urlconf, args, kwargs, prefix, current_app)
    if prefixer and add_prefix:
        return prefixer.fix(url)
    else:
        return url

# Replace Django's reverse with our own.
urlresolvers.reverse = reverse


class Prefixer(object):

    def __init__(self, request):
        self.request = request
        split = self.split_path(request.path_info)
        self.locale, self.app, self.shortened_path = split

    def split_path(self, path_):
        """
        Split the requested path into (locale, app, remainder).

        locale and app will be empty strings if they're not found.
        """
        path = path_.lstrip('/')

        # Use partition instead of split since it always returns 3 parts.
        first, _, first_rest = path.partition('/')
        second, _, rest = first_rest.partition('/')

        first_lower = first.lower()
        lang, dash, territory = first_lower.partition('-')

        # Check language-territory first.
        if first_lower in settings.LANGUAGES:
            return first, '', first_rest
        # And check just language next.
        elif dash and lang in settings.LANGUAGES:
            first = lang
            return first, '', first_rest
        else:
            return '', '', path

    def get_language(self):
        """
        Return a locale code that we support on the site using the
        user's Accept Language header to determine which is best.  This
        mostly follows the RFCs but read bug 439568 for details.
        """
        data = (self.request.GET or self.request.POST)
        if 'lang' in data:
            lang = data['lang'].lower()
            if lang in settings.LANGUAGE_URL_MAP:
                return settings.LANGUAGE_URL_MAP[lang]
            prefix = lang.split('-')[0]
            if prefix in settings.LANGUAGE_URL_MAP:
                return settings.LANGUAGE_URL_MAP[prefix]

        accept = self.request.META.get('HTTP_ACCEPT_LANGUAGE', '')
        return lang_from_accept_header(accept)

    def fix(self, path):
        # Marketplace URLs are not prefixed with `/<locale>/<app>`.
        return path


def get_outgoing_url(url):
    """
    Bounce a URL off an outgoing URL redirector, such as outgoing.mozilla.org.
    """
    if not settings.REDIRECT_URL:
        return url

    url_netloc = urlparse(url).netloc

    # No double-escaping, and some domain names are excluded.
    if (url_netloc == urlparse(settings.REDIRECT_URL).netloc
        or url_netloc in settings.REDIRECT_URL_WHITELIST):
        return url

    url = encoding.smart_str(jinja2.utils.Markup(url).unescape())
    sig = hmac.new(settings.REDIRECT_SECRET_KEY,
                   msg=url, digestmod=hashlib.sha256).hexdigest()
    # Let '&=' through so query params aren't escaped.  We probably shouldn't
    # bother to quote the query part at all.
    return '/'.join([settings.REDIRECT_URL.rstrip('/'), sig,
                     urllib.quote(url, safe='/&=')])


def linkify_bounce_url_callback(attrs, new=False):
    """Linkify callback that uses get_outgoing_url."""
    attrs['href'] = get_outgoing_url(attrs['href'])
    return attrs


def linkify_with_outgoing(text, nofollow=True):
    """Wrapper around bleach.linkify: uses get_outgoing_url."""
    callbacks = [linkify_bounce_url_callback]
    if nofollow:
        callbacks.append(bleach.callbacks.nofollow)
    return bleach.linkify(unicode(text), callbacks=callbacks)


def url_fix(s, charset='utf-8'):
    """Sometimes you get an URL by a user that just isn't a real
    URL because it contains unsafe characters like ' ' and so on.  This
    function can fix some of the problems in a similar way browsers
    handle data entered by the user:

    >>> url_fix(u'http://de.wikipedia.org/wiki/Elf (Begriffsklärung)')
    'http://de.wikipedia.org/wiki/Elf%20%28Begriffskl%C3%A4rung%29'

    :param charset: The target charset for the URL if the url was
                    given as unicode string.

    Lifted from Werkzeug.
    """
    if isinstance(s, unicode):
        s = s.encode(charset, 'ignore')
    scheme, netloc, path, qs, anchor = urlsplit(s)
    path = urllib.quote(path, '/%:')
    qs = urllib.quote_plus(qs, ':&=')
    return urlunsplit((scheme, netloc, path, qs, anchor))


def lang_from_accept_header(header):
    # Map all our lang codes and any prefixes to the locale code.
    langs = dict((k.lower(), v) for k, v in settings.LANGUAGE_URL_MAP.items())

    # If we have a lang or a prefix of the lang, return the locale code.
    for lang, _ in parse_accept_lang_header(header.lower()):
        if lang in langs:
            return langs[lang]

        prefix = lang.split('-')[0]
        # Downgrade a longer prefix to a shorter one if needed (es-PE > es)
        if prefix in langs:
            return langs[prefix]
        # Upgrade to a longer one, if present (zh > zh-CN)
        lookup = settings.SHORTER_LANGUAGES.get(prefix, '').lower()
        if lookup and lookup in langs:
            return langs[lookup]

    return settings.LANGUAGE_CODE
