from functools import partial

from rest_framework import fields, serializers

from mkt.access import acl
from mkt.api.serializers import PotatoCaptchaSerializer
from mkt.users.models import UserProfile


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = ('display_name',)

    def validate_display_name(self, attrs, source):
        """Validate that display_name is not empty"""
        value = attrs.get(source)
        if value is None or not value.strip():
            raise serializers.ValidationError("This field is required")
        return attrs

    def transform_display_name(self, obj, value):
        """Return obj.name instead of display_name to handle users without
        a valid display_name."""
        return obj.name


class FeedbackSerializer(PotatoCaptchaSerializer):
    feedback = fields.CharField()
    platform = fields.CharField(required=False)
    chromeless = fields.CharField(required=False)
    from_url = fields.CharField(required=False)
    user = fields.Field()

    def validate(self, attrs):
        attrs = super(FeedbackSerializer, self).validate(attrs)

        if not attrs.get('platform'):
            attrs['platform'] = self.request.GET.get('dev', '')
        attrs['user'] = self.request.amo_user

        return attrs


class LoginSerializer(serializers.Serializer):
    assertion = fields.CharField(required=True)
    audience = fields.CharField(required=False)
    is_mobile = fields.BooleanField(required=False, default=False)


class NewsletterSerializer(serializers.Serializer):
    email = fields.EmailField()


class PermissionsSerializer(serializers.Serializer):
    permissions = fields.SerializerMethodField('get_permissions')

    def get_permissions(self, obj):
        request = self.context['request']
        allowed = partial(acl.action_allowed, request)
        permissions = {
            'admin': allowed('Admin', '%'),
            'developer': request.amo_user.is_developer,
            'localizer': allowed('Localizers', '%'),
            'lookup': allowed('AccountLookup', '%'),
            'curator': allowed('Collections', 'Curate') or
                       allowed('Feed', 'Curate'),
            'reviewer': acl.action_allowed(request, 'Apps', 'Review'),
            'webpay': (allowed('Transaction', 'NotifyFailure')
                       and allowed('ProductIcon', 'Create')),
            'stats': allowed('Stats', 'View'),
            'revenue_stats': allowed('RevenueStats', 'View'),
        }
        return permissions


class UserSerializer(AccountSerializer):
    """
    A wacky serializer type that unserializes PK numbers and
    serializes user fields.
    """
    resource_uri = serializers.HyperlinkedRelatedField(
        view_name='account-settings', source='pk',
        read_only=True)

    class Meta:
        model = UserProfile
        fields = ('display_name', 'resource_uri')
