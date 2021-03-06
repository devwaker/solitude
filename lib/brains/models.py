from django.core.urlresolvers import reverse
from django.db import models
from django.dispatch import receiver

from lib.brains.client import get_client
from lib.brains.errors import BraintreeResultError
from lib.buyers.models import Buyer
from solitude.base import getLogger, Model
from solitude.constants import PAYMENT_METHOD_CARD

log = getLogger('s.brains')


class BraintreeBuyer(Model):

    """A holder for any braintree specific stuff around the buyer."""
    # Allow us to turn off braintree buyers if we need to.
    active = models.BooleanField(default=True)
    # The specific braintree-id confirming to braintree's requirements.
    # Braintree requires that ids are letters, numbers, -, _. See:
    # https://developers.braintreepayments.com/reference/request/customer/create
    braintree_id = models.CharField(max_length=255, db_index=True, unique=True)
    buyer = models.OneToOneField(Buyer)

    class Meta(Model.Meta):
        db_table = 'buyer_braintree'

    def get_uri(self):
        return reverse('braintree:mozilla:buyer', kwargs={'pk': self.pk})


@receiver(Buyer.close_signal, sender=Buyer)
def close(signal, *args, **kw):
    buyer = kw['buyer']
    try:
        braintree_buyer = BraintreeBuyer.objects.get(buyer=buyer)
    except BraintreeBuyer.DoesNotExist:
        # The buyer might not have used braintree. If that's the
        # case continue.
        log.info('No braintree buyer found for buyer: {}'
                 .format(buyer.pk))
        return

    for paymethod in braintree_buyer.paymethods.all():

        # Find and clear out all subscriptions.
        for subscription in paymethod.subscriptions.filter(active=True):
            subscription.braintree_cancel()
            subscription.active = False
            subscription.save()
            log.info('Cancelled subscription: {}'.format(subscription.pk))

        # Delete the payment method from braintree.
        paymethod.braintree_delete()
        paymethod.active = False
        paymethod.save()
        log.info('Deleted payment method: {}'.format(paymethod.pk))


class BraintreePaymentMethod(Model):

    """A holder for braintree specific payment method."""

    active = models.BooleanField(default=True)
    braintree_buyer = models.ForeignKey(
        BraintreeBuyer, related_name='paymethods')
    # An id specific to the provider.
    provider_id = models.CharField(max_length=255)
    # The type of payment method eg: card, paypal or bitcon
    type = models.PositiveIntegerField(choices=(
        (PAYMENT_METHOD_CARD, PAYMENT_METHOD_CARD),
    ))
    # Details about the type, eg Amex, Orange.
    type_name = models.CharField(max_length=255)
    # For credit cards, this is the last 4 numbers, could be a truncated
    # phone number or paypal account for example.
    truncated_id = models.CharField(max_length=255)

    class Meta(Model.Meta):
        db_table = 'braintree_pay_method'

    def get_uri(self):
        return reverse('braintree:mozilla:paymethod-detail',
                       kwargs={'pk': self.pk})

    def braintree_delete(self):
        """
        Deletes this payment method on braintree.
        """
        result = get_client().PaymentMethod.delete(self.provider_id)
        if not result.is_success:
            log.warning('Error on deleting Payment method: {} {}'
                        .format(self.pk, result.message))
            raise BraintreeResultError(result)

        log.info('Payment method deleted in braintree: {}'.format(self.pk))
        return result


class BraintreeSubscription(Model):

    """
    A holder for Braintree specific information around the subscriber.
    """
    active = models.BooleanField(default=True)
    # From the payment method we know the buyer.
    paymethod = models.ForeignKey(
        BraintreePaymentMethod, db_index=True, related_name='subscriptions')
    seller_product = models.ForeignKey('sellers.SellerProduct', db_index=True)
    # An id specific to the provider.
    provider_id = models.CharField(max_length=255)

    class Meta(Model.Meta):
        db_table = 'braintree_subscription'
        unique_together = (('paymethod', 'seller_product'),)

    def get_uri(self):
        return reverse('braintree:mozilla:subscription-detail',
                       kwargs={'pk': self.pk})

    def braintree_cancel(self):
        """
        Cancels this subscription on braintree. See: http://bit.ly/1M84dbi
        for more.
        """
        result = get_client().Subscription.cancel(self.provider_id)
        if not result.is_success:
            log.warning('Error on cancelling subscription: {} {}'
                        .format(self.pk, result.message))
            raise BraintreeResultError(result)

        log.info('Subscription cancelled in braintree: {}'.format(self.pk))
        return result


class BraintreeTransaction(Model):

    """
    A holder for Braintree specific information about the transaction since
    some of this is not stored in the generic transaction.
    """
    # There isn't enough information on the Transaction.
    paymethod = models.ForeignKey(BraintreePaymentMethod, db_index=True)
    subscription = models.ForeignKey(BraintreeSubscription, db_index=True)
    transaction = models.OneToOneField(
        'transactions.Transaction', db_index=True)

    # Data from Braintree that we'd like to store and re-use
    billing_period_end_date = models.DateTimeField()
    billing_period_start_date = models.DateTimeField()
    kind = models.CharField(max_length=255)
    next_billing_date = models.DateTimeField()
    next_billing_period_amount = models.DecimalField(
        max_digits=9, decimal_places=2, blank=True,  null=True)

    class Meta(Model.Meta):
        db_table = 'braintree_transaction'

    def get_uri(self):
        return reverse('braintree:mozilla:transaction-detail',
                       kwargs={'pk': self.pk})
