from braintree.exceptions.not_found_error import NotFoundError
from rest_framework.decorators import api_view
from rest_framework.response import Response

from lib.brains.client import get_client
from lib.brains.errors import BraintreeResultError
from lib.brains.forms import BuyerForm
from lib.brains.models import BraintreeBuyer
from lib.brains.serializers import CustomerSerializer
from lib.buyers.models import Buyer
from lib.buyers.serializers import BuyerSerializer
from solitude.logger import getLogger

log = getLogger('s.brains')


@api_view(['POST'])
def create(request):
    client = get_client().Customer
    form = BuyerForm(request.DATA)

    if not form.is_valid():
        return Response(form.errors, status=400)

    uuid = form.cleaned_data['uuid']

    # Create the solitude objects.
    buyer, buyer_created = Buyer.objects.get_or_create(uuid=uuid)
    log.info('Buyer {0}: {1}'
             .format('created' if buyer_created else 'exists', buyer.pk))

    braintree, braintree_buyer_created = (
        BraintreeBuyer.objects.get_or_create(buyer=buyer))
    log.info('BraintreeBuyer {0}: {1}'
             .format('created' if braintree_buyer_created else 'exists',
                     braintree.pk))

    braintree_id = braintree.braintree_id
    # Create the braintree objects.
    # If anything fails at this point, the transaction will be rolled back.
    braintree_created = False
    try:
        customer = client.find(braintree_id)
        log.info('Customer found: {0}'.format(braintree_id))
    except NotFoundError:
        result = client.create({'id': braintree_id})
        if not result.is_success:
            log.warning('Error on creating Customer: {0}, {1}'
                        .format(uuid, result.message))
            raise BraintreeResultError(result)

        braintree_created = True
        customer = result.customer

    res = BuyerSerializer(instance=buyer).data
    res.update({'braintree': CustomerSerializer(instance=customer).data})
    created = any([braintree_created, braintree_buyer_created, buyer_created])
    status = 201 if created else 200
    return Response(res, status)