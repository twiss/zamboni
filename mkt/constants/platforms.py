from tower import ugettext_lazy as _lazy

from constants.applications import (DEVICE_DESKTOP, DEVICE_GAIA, DEVICE_MOBILE,
                                    DEVICE_TABLET)


class PLATFORM_DESKTOP(object):
    id = 1
    name = _lazy(u'Desktop')
    slug = 'desktop'


class PLATFORM_ANDROID(object):
    id = 2
    name = _lazy(u'Android')
    slug = 'android'


class PLATFORM_FXOS(object):
    id = 3
    name = _lazy(u'Firefox OS')
    slug = 'firefoxos'


PLATFORM_LIST = [PLATFORM_DESKTOP, PLATFORM_ANDROID, PLATFORM_FXOS]
PLATFORM_TYPES = dict((d.id, d) for d in PLATFORM_LIST)
REVERSE_PLATFORM_LOOKUP = dict((d.id, d.slug) for d in PLATFORM_LIST)
PLATFORM_LOOKUP = dict((d.slug, d) for d in PLATFORM_LIST)


# Mapping from old device types to platforms. Used as a compatibility layer to
# avoid breaking the API.
DEVICE_TO_PLATFORM = {
    DEVICE_DESKTOP.id: PLATFORM_DESKTOP,
    DEVICE_MOBILE.id: PLATFORM_ANDROID,
    DEVICE_TABLET.id: PLATFORM_ANDROID,
    DEVICE_GAIA.id: PLATFORM_FXOS,
}
