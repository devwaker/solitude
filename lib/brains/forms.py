from django import forms
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

import requests

from lib.brains.models import (
    BraintreeBuyer, BraintreePaymentMethod, BraintreeSubscription)
from lib.buyers.models import Buyer
from lib.sellers.models import SellerProduct
from payments_config import products
from solitude.base import getLogger
from solitude.related_fields import PathRelatedFormField

log = getLogger('s.brains')


class BuyerForm(forms.Form):
    uuid = forms.CharField(max_length=255)

    def clean_uuid(self):
        data = self.cleaned_data['uuid']

        try:
            self.buyer = Buyer.objects.get(uuid=data)
        except ObjectDoesNotExist:
            raise forms.ValidationError('Buyer does not exist.',
                                        code='does_not_exist')

        if BraintreeBuyer.objects.filter(buyer=self.buyer).exists():
            raise forms.ValidationError('Braintree buyer already exists.',
                                        code='already_exists')

        return data


class PaymentMethodForm(forms.Form):
    buyer_uuid = forms.CharField(max_length=255)
    nonce = forms.CharField(max_length=255)

    def clean_buyer_uuid(self):
        data = self.cleaned_data['buyer_uuid']

        try:
            self.buyer = Buyer.objects.get(uuid=data)
        except ObjectDoesNotExist:
            raise forms.ValidationError('Buyer does not exist.',
                                        code='does_not_exist')

        try:
            self.braintree_buyer = self.buyer.braintreebuyer
        except ObjectDoesNotExist:
            raise forms.ValidationError('Braintree buyer does not exist.',
                                        code='does_not_exist')

        # Ideally this should be limited by the type of method
        # as well, something we'll need to remember when we add in another
        # payment method. However, we don't know the type until the reply
        # comes from Braintree.
        if (self.braintree_buyer.paymethods.filter(active=True).count()
                >= settings.BRAINTREE_MAX_METHODS):
            raise forms.ValidationError(
                'Reached maximum number of payment methods',
                code='max_size')

        return data

    @property
    def braintree_data(self):
        return {
            'customer_id': str(self.braintree_buyer.braintree_id),
            'payment_method_nonce': self.cleaned_data['nonce'],
            # This will force the card to be verified upon creation.
            'options': {
                'verify_card': True
            }
        }


class SubscriptionForm(forms.Form):
    paymethod = PathRelatedFormField(
        view_name='braintree:mozilla:paymethod-detail',
        queryset=BraintreePaymentMethod.objects.filter())
    plan = forms.CharField(max_length=255)

    def clean_plan(self):
        data = self.cleaned_data['plan']

        try:
            obj = SellerProduct.objects.get(public_id=data)
        except ObjectDoesNotExist:
            log.info(
                'no seller product with braintree plan id: {plan}'
                .format(plan=data))
            raise forms.ValidationError(
                'Seller product does not exist.', code='does_not_exist')

        self.seller_product = obj
        return data

    def format_descriptor(self, name):
        # The rules for descriptor are:
        #
        # Company name/DBA section must be either 3, 7 or 12 characters and
        # the product descriptor can be up to 18, 14, or 9 characters
        # respectively (with an * in between for a total descriptor
        # name of 22 characters)
        return 'Mozilla*{}'.format(name)[0:22]

    def get_name(self, plan_id):
        if plan_id in products:
            return unicode(products.get(plan_id).description)
        log.warning('Unknown product for descriptor: {}'.format(plan_id))
        return 'Product'

    @property
    def braintree_data(self):
        plan_id = self.seller_product.public_id
        return {
            'payment_method_token': self.cleaned_data['paymethod'].provider_id,
            'plan_id': plan_id,
            'trial_period': False,
            'descriptor': {
                'name': self.format_descriptor(self.get_name(plan_id)),
                'url': 'mozilla.org'
            }
        }


class WebhookVerifyForm(forms.Form):
    bt_challenge = forms.CharField()

    @property
    def braintree_data(self):
        return self.cleaned_data['bt_challenge']

    def clean_bt_challenge(self):
        res = requests.get(
            settings.BRAINTREE_PROXY + '/verify',
            params={'bt_challenge': self.cleaned_data['bt_challenge']}
        )
        if res.status_code != 200:
            log.error('Did not receive a 204 from solitude-auth, got: {}'
                      .format(res.status_code))
            raise forms.ValidationError(
                'Did not pass verification', code='invalid')
        self.response = res.content
        return self.cleaned_data['bt_challenge']


class WebhookParseForm(forms.Form):
    bt_signature = forms.CharField()
    bt_payload = forms.CharField()

    @property
    def braintree_data(self):
        return (
            self.cleaned_data['bt_signature'],
            self.cleaned_data['bt_payload']
        )

    def clean(self):
        res = requests.post(settings.BRAINTREE_PROXY + '/parse', self.data)
        if res.status_code != 204:
            log.error('Did not receive a 204 from solitude-auth, got: {}'
                      .format(res.status_code))
            raise forms.ValidationError(
                'Did not pass verification', code='invalid')
        return self.cleaned_data


class PayMethodDeleteForm(forms.Form):
    paymethod = PathRelatedFormField(
        view_name='braintree:mozilla:paymethod-detail',
        queryset=BraintreePaymentMethod.objects.filter())

    def clean(self):
        solitude_method = self.cleaned_data.get('paymethod')
        if not solitude_method:
            raise forms.ValidationError(
                'Paymethod is required', code='required')

        # An attempt to delete an inactive payment.
        if not solitude_method.active:
            raise forms.ValidationError(
                'Cannot delete an inactive payment method', code='invalid')

        return self.cleaned_data


class SubscriptionUpdateForm(forms.Form):
    paymethod = PathRelatedFormField(
        view_name='braintree:mozilla:paymethod-detail',
        queryset=BraintreePaymentMethod.objects.filter())
    subscription = PathRelatedFormField(
        view_name='braintree:mozilla:subscription-detail',
        queryset=BraintreeSubscription.objects.filter())

    def clean(self):
        solitude_subscription = self.cleaned_data.get('subscription')
        solitude_method = self.cleaned_data.get('paymethod')

        if solitude_subscription and not solitude_subscription.active:
            raise forms.ValidationError(
                'Cannot alter an inactive subscription', code='invalid')

        if solitude_method and not solitude_method.active:
            raise forms.ValidationError(
                'Cannot use an inactive payment method', code='invalid')

        return self.cleaned_data


class SubscriptionCancelForm(forms.Form):
    subscription = PathRelatedFormField(
        view_name='braintree:mozilla:subscription-detail',
        queryset=BraintreeSubscription.objects.filter())

    def clean(self):
        solitude_subscription = self.cleaned_data.get('subscription')

        if solitude_subscription and not solitude_subscription.active:
            raise forms.ValidationError(
                'Cannot cancel an inactive subscription', code='invalid')

        return self.cleaned_data
