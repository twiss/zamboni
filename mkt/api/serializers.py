from django.conf import settings

import commonware.log
from rest_framework import serializers
from rest_framework.reverse import reverse
from tower import ugettext as _


log = commonware.log.getLogger('z.mkt.api.forms')


class PotatoCaptchaSerializer(serializers.Serializer):
    """
    Serializer class to inherit from to get PotatoCaptcha (tm) protection for
    an API based on DRF.

    Clients using this API are supposed to have 2 fields in their HTML, "tuber"
    and "sprout". They should never submit a value for "tuber", and they should
    always submit "potato" as the value for "sprout". This is to prevent dumb
    bots from spamming us.

    If a wrong value is entered for "sprout" or "tuber" is present, a
    ValidationError will be returned.

    Note: this is completely disabled for authenticated users.
    """

    # This field's value should always be blank (spammers are dumb).
    tuber = serializers.CharField(required=False)

    # This field's value should always be 'potato' (set by JS).
    sprout = serializers.CharField()

    def __init__(self, *args, **kwargs):
        super(PotatoCaptchaSerializer, self).__init__(*args, **kwargs)
        if hasattr(self, 'context') and 'request' in self.context:
            self.request = self.context['request']
        else:
            raise serializers.ValidationError('Need request in context')

        self.has_potato_recaptcha = True
        if self.request.user.is_authenticated():
            self.fields.pop('tuber')
            self.fields.pop('sprout')
            self.has_potato_recaptcha = False

    def validate(self, attrs):
        attrs = super(PotatoCaptchaSerializer, self).validate(attrs)
        if self.has_potato_recaptcha:
            sprout = attrs.get('sprout', None)
            tuber = attrs.get('tuber', None)

            if tuber or sprout != 'potato':
                ip = self.request.META.get('REMOTE_ADDR', '')
                log.info(u'Spammer thwarted: %s' % ip)
                raise serializers.ValidationError(
                    _('Form could not be submitted.'))

            # Don't keep the internal captcha fields, we don't want them to
            # pollute self.data
            self.fields.pop('tuber')
            self.fields.pop('sprout')
        return attrs


class CarrierSerializer(serializers.Serializer):
    name = serializers.CharField()
    slug = serializers.CharField()
    id = serializers.IntegerField()


class RegionSerializer(CarrierSerializer):
    default_currency = serializers.CharField()
    default_language = serializers.CharField()


class CategorySerializer(serializers.Serializer):
    def to_native(self, obj):
        return {'slug': obj[0], 'name': unicode(obj[1])}


class URLSerializerMixin(serializers.ModelSerializer):
    """
    ModelSerializer mixin that adds a field named `url` to the object with that
    resource's URL. DRF will automatically populate the `Location` header from
    this field when appropriate.

    You may define that url in one of two ways:
    1) By defining a `url_basename` property on the Meta class. The URL will
       then be determined by reversing `<url_basename>-detail`, with the `pk`
       passed as a keyword argument.
    2) By overriding the get_url method.
    """
    url = serializers.SerializerMethodField('get_url')

    def get_url(self, obj):
        if 'request' in self.context and hasattr(self.Meta, 'url_basename'):
            request = self.context['request']
            namespace = ''
            if request.API_VERSION != settings.API_CURRENT_VERSION:
                namespace = 'api-v%d:' % request.API_VERSION
            return reverse('%s%s-detail' % (namespace, self.Meta.url_basename,),
                           request=request, kwargs={'pk': obj.pk})
        return None
