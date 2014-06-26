.. _search:

======
Search
======

This API allows search for apps by various properties.

.. _search-api:

Search
======

.. http:get:: /api/v1/apps/search/

    **Request**

    :param optional q: The query string to search for.
    :type q: string
    :param optional cat: The category slug or ID to filter by. Use the
        category API to find the ids of the categories.
    :type cat: int|string
    :param optional device: Filters by supported device. One of 'desktop',
        'mobile', 'tablet', or 'firefoxos'.
    :type device: string
    :param optional dev: Enables filtering by device profile if either
                         'firefoxos' or 'android'.
    :type dev: string
    :param optional pro: A :ref:`feature profile <feature-profile-label>`
                         describing the features to filter by.
    :type pro: string
    :param optional premium_types: Filters by whether the app is free or
        premium or has in-app purchasing. Any of 'free', 'free-inapp',
        'premium', 'premium-inapp', or 'other'.
    :type premium_types: string
    :param optional app_type: Filters by types of web apps. Any of 'hosted',
        'packaged', or 'privileged'.
    :type app_type: string
    :param optional manifest_url: Filters by manifest URL. Requires an
        exact match and should only return a single result if a match is
        found.
    :type manifest_url: string
    :param optional offline: Filters by whether the app works offline or not.
        'True' to show offline-capable apps; 'False' to show apps requiring
        online support; any other value will show all apps unfiltered by
        offline support.
    :type offline: string
    :param optional languages: Filters apps by a supported language. Language
        codes should be provided in ISO 639-1 format, using a comma-separated
        list if supplying multiple languages.
    :type languages: string
    :param optional region: Filters apps by a supported region. A region
        code should be provided in ISO 3166 format (e.g., `pl`). If not
        provided, the region is automatically detected via requesting IP
        address. To disable automatic region detection, `None` may be passed.
    :type region: string
    :param optional sort: The fields to sort by. One or more of 'created',
        'downloads', 'name', 'rating', or 'reviewed'. Sorts by
        relevance by default. In every case except 'name', sorting is done in
        descending order.
    :type sort: string

    **Response**

    :param meta: :ref:`meta-response-label`.
    :type meta: object
    :param objects: A :ref:`listing <objects-response-label>` of
        :ref:`apps <app-response-label>`, with the following additional
        fields:
    :type objects: array


    .. code-block:: json

        {
            "absolute_url": https://marketplace.firefox.com/app/my-app/",
        }

    :status 200: successfully completed.

.. _featured-search-api:

Featured App Listing
====================

.. http:get::  /api/v1/fireplace/search/featured/

    **Request**

    Accepts the same parameters and returns the same objects as the
    normal search interface: :ref:`search-api`.  Includes 'featured'
    list of apps, listing featured apps for the requested category, if
    any. When no category is specified, frontpage featured apps are
    listed.

    **Response**:

    :param collections: A list of collections for the requested
        category/region/carrier set, if any
    :type collections: array
    :param featured: A list of :ref:`apps <app-response-label>` featured
        for the requested category/region/carrier set, if any
    :type featured: array
    :param meta: :ref:`meta-response-label`.
    :type meta: object
    :param objects: A :ref:`listing <objects-response-label>` of
        :ref:`apps <app-response-label>` satisfying the search parameters.
    :type objects: array
    :param operator: A list of apps in the operator shelf for the requested
        category/region/carrier set, if any
    :type operator: array
    :status 200: successfully completed.

    The different types of collections returned are filtered using the same
    parameters as :ref:`rocketfuel <rocketfuel>` listing API, using the same
    :ref:`fallback mechanism <rocketfuel-fallback>` if no results are found
    with the filters specified.

    However, because there are 3 separate types of collections returned,
    you can have 3 different fallbacks. Therefore, instead of returning one
    single `API-Fallback` header, the HTTP response will contain up to 3
    separate headers: `API-Fallback-collections`, `API-Fallback-featured` and
    `API-Fallback-operator`. Their content is identical to the `API-Fallback`
    header returned in rocketfuel listing API.

.. _feature-profile-label:

Feature Profile Signatures
==========================

Feature profile signatures indicate what features a device supports or
does not support, so the search results can exclude apps that require
features your device doesn't provide.

The format of a signature is FEATURES.SIZE.VERSION, where FEATURES is
a bitfield in hexadecimal, SIZE is its length in bits as a decimal
number, and VERSION is a decimal number indicating the version of the
features table.

Each bit in the features bitfield represents the presence or absence
of a feature.

Feature table version 4:

=====  ============================
  bit   feature
=====  ============================
    0   Multiple Network Information
    1   Third-Party Keyboard Support
    2   TCP Sockets
    3   SystemXHR
    4   Alarms
    5   Notifications
    6   Pointer Lock
    7   Web Speech Recognition
    8   Web Speech Synthesis
    9   WebRTC PeerConnection
   10   WebRTC DataChannel
   11   WebRTC MediaStream
   12   Screen Capture
   13   Microphone
   14   Camera
   15   Quota Management
   16   Gamepad
   17   Full Screen
   18   WebM
   19   H.264
   20   Web Audio
   21   Audio
   22   MP3
   23   Smartphone-Sized Displays
   24   Touch
   25   WebSMS
   26   WebFM
   27   Vibration
   28   Time/Clock
   29   Screen Orientation
   30   Simple Push
   31   Proximity
   32   Network Stats
   33   Network Information
   34   Idle
   35   Geolocation
   36   IndexedDB
   37   Device Storage
   38   Contacts
   39   Bluetooth
   40   Battery
   41   Archive
   42   Ambient Light Sensor
   43   Web Activities
   44   Web Payment
   45   Packaged Apps Install API
   46   App Management API
=====  ============================


For example, a device with the 'App Management API', 'Proximity',
'Ambient Light Sensor', and 'Vibration' features would send this
feature profile signature::

    440088000000.47.4

