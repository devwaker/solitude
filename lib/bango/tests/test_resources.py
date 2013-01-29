# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
import json

from django.conf import settings

import mock
from nose.tools import eq_, ok_

from lib.sellers.models import (Seller, SellerBango, SellerProduct,
                                SellerProductBango)
from lib.sellers.tests.utils import make_seller_paypal
from lib.transactions import constants
from lib.transactions.constants import (SOURCE_PAYPAL, STATUS_CANCELLED,
                                        TYPE_REFUND)
from lib.transactions.models import Transaction
from solitude.base import APITest

from ..constants import (ALREADY_REFUNDED, BANGO_ALREADY_PREMIUM_ENABLED,
                         CANCEL, CANT_REFUND, OK, PENDING)
from ..client import ClientMock
from ..errors import BangoError
from ..resources.cached import SimpleResource
from ..utils import sign

import samples


class BangoAPI(APITest):
    api_name = 'bango'
    uuid = 'foo:uuid'

    def create(self):
        self.seller = Seller.objects.create(uuid=self.uuid)
        self.seller_bango = SellerBango.objects.create(seller=self.seller,
                                package_id=1, admin_person_id=3,
                                support_person_id=3, finance_person_id=4)
        self.seller_bango_uri = self.get_detail_url('package',
                                                    self.seller_bango.pk)
        self.seller_product = SellerProduct.objects.create(seller=self.seller,
                                                           external_id='xyz')
        self.seller_product_uri = self.get_detail_url('product',
                                                      self.seller_product.pk,
                                                      api_name='generic')


class TestSimple(APITest):

    def test_raises(self):
        class Foo(SimpleResource):
            pass

        with self.assertRaises(ValueError):
            Foo().check_meta()


class TestPackageResource(BangoAPI):

    def setUp(self):
        super(TestPackageResource, self).setUp()
        self.list_url = self.get_list_url('package')

    def test_list_allowed(self):
        self.allowed_verbs(self.list_url, ['get', 'post'])

    def test_create(self):
        post = samples.good_address.copy()
        post['seller'] = ('/generic/seller/%s/' %
                          Seller.objects.create(uuid=self.uuid).pk)
        res = self.client.post(self.list_url, data=post)
        eq_(res.status_code, 201, res.content)
        seller_bango = SellerBango.objects.get()
        eq_(json.loads(res.content)['resource_pk'], seller_bango.pk)

    def test_unicode(self):
        post = samples.good_address.copy()
        post['companyName'] = u'འབྲུག་ཡུལ།'
        post['seller'] = ('/generic/seller/%s/' %
                          Seller.objects.create(uuid=self.uuid).pk)
        res = self.client.post(self.list_url, data=post)
        eq_(res.status_code, 201, res.content)

    def test_missing_field(self):
        data = {'adminEmailAddress': 'admin@place.com',
                'supportEmailAddress': 'support@place.com'}
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 400, res.content)
        eq_(json.loads(res.content)['companyName'],
            ['This field is required.'])

    # TODO: probably should inject this in a better way.
    @mock.patch.object(ClientMock, 'mock_results')
    @mock.patch.object(settings, 'DEBUG', False)
    def test_bango_fail(self, mock_results):
        post = samples.good_address.copy()
        post['seller'] = ('/generic/seller/%s/' %
                          Seller.objects.create(uuid=self.uuid).pk)
        res = self.client.post(self.list_url, data=post)
        mock_results.return_value = {'responseCode': 'FAIL'}
        res = self.client.post(self.list_url, data=samples.good_address)
        eq_(res.status_code, 500)

    def test_get_allowed(self):
        self.create()
        url = self.get_detail_url('package', self.seller_bango.pk)
        self.allowed_verbs(url, ['get', 'patch'])

    def test_get(self):
        self.create()
        url = self.get_detail_url('package', self.seller_bango.pk)
        seller_bango = SellerBango.objects.get()
        data = json.loads(self.client.get(url).content)
        eq_(data['resource_pk'], seller_bango.pk)

    def test_get_generic(self):
        self.create()
        url = self.get_detail_url('seller', self.seller.pk, api_name='generic')
        data = json.loads(self.client.get(url).content)
        eq_(data['bango']['resource_pk'], self.seller_bango.pk)

    def test_patch(self):
        self.create()
        url = self.get_detail_url('package', self.seller_bango.pk)
        seller_bango = SellerBango.objects.get()
        old_support = seller_bango.support_person_id
        old_finance = seller_bango.finance_person_id

        res = self.client.patch(url, data={'supportEmailAddress': 'a@a.com'})
        eq_(res.status_code, 202, res.content)
        seller_bango = SellerBango.objects.get()

        # Check that support changed, but finance didn't.
        assert seller_bango.support_person_id != old_support
        eq_(seller_bango.finance_person_id, old_finance)


class TestBangoProduct(BangoAPI):

    def setUp(self):
        super(TestBangoProduct, self).setUp()
        self.list_url = self.get_list_url('product')

    def test_list_allowed(self):
        self.allowed_verbs(self.list_url, ['post', 'get'])

    def test_create(self):
        self.create()
        data = samples.good_bango_number
        data['seller_product'] = ('/generic/product/%s/' %
                                  self.seller_product.pk)
        data['seller_bango'] = self.seller_bango_uri
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 201, res.content)

        obj = SellerProductBango.objects.get()
        eq_(obj.bango_id, 'some-bango-number')
        eq_(obj.seller_product_id, self.seller_bango.pk)

    def test_create_multiple(self):
        # Just a generic test to ensure that multiple posts are 400.
        self.create()
        data = samples.good_bango_number
        data['seller_product'] = ('/generic/product/%s/' %
                                  self.seller_product.pk)
        data['seller_bango'] = self.seller_bango_uri
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 201, res.content)
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 400, res.content)

    def test_get_by_seller_product(self):
        self.create()

        # This is a decoy product that should be ignored by the filter.
        pr = SellerProduct.objects.create(seller=self.seller,
                                          external_id='decoy-product')
        SellerProductBango.objects.create(seller_product=pr,
                                          seller_bango=self.seller_bango,
                                          bango_id='999999')

        # This is the product we want to fetch.
        SellerProductBango.objects.create(seller_product=self.seller_product,
                                          seller_bango=self.seller_bango,
                                          bango_id='1234')

        res = self.client.get(self.list_url, data=dict(
            seller_product__seller=self.seller.pk,
            seller_product__external_id=self.seller_product.external_id
        ))

        eq_(res.status_code, 200, res.content)
        data = json.loads(res.content)
        eq_(data['meta']['total_count'], 1, data)
        eq_(data['objects'][0]['bango_id'], '1234')


class SellerProductBangoBase(BangoAPI):

    def create(self):
        super(SellerProductBangoBase, self).create()
        self.seller_product_bango = SellerProductBango.objects.create(
                                        seller_product=self.seller_product,
                                        seller_bango=self.seller_bango,
                                        bango_id='some-123')
        self.seller_product_bango_uri = ('/bango/product/%s/' %
                                         self.seller_product_bango.pk)


class TestBangoMarkPremium(SellerProductBangoBase):

    def test_list_allowed(self):
        self.allowed_verbs(self.list_url, ['post'])

    def setUp(self):
        super(TestBangoMarkPremium, self).setUp()
        self.list_url = self.get_list_url('premium')

    def create(self):
        super(TestBangoMarkPremium, self).create()
        data = samples.good_make_premium.copy()
        data['seller_product_bango'] = self.seller_product_bango_uri
        return data

    def test_mark(self):
        res = self.client.post(self.list_url, data=self.create())
        eq_(res.status_code, 201)

    def test_fail(self):
        data = self.create()
        data['currencyIso'] = 'FOO'
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 400)

    @mock.patch.object(ClientMock, 'mock_results')
    def test_other_error(self, mock_results):
        data = self.create()
        mock_results.return_value = {'responseCode': 'wat?'}
        with self.assertRaises(BangoError):
            self.client.post(self.list_url, data=data)

    @mock.patch.object(ClientMock, 'mock_results')
    def test_done_twice(self, mock_results):
        data = self.create()
        mock_results.return_value = {'responseCode':
                                     BANGO_ALREADY_PREMIUM_ENABLED}
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 204)


class TestBangoUpdateRating(SellerProductBangoBase):

    def test_list_allowed(self):
        self.allowed_verbs(self.list_url, ['post'])

    def setUp(self):
        super(TestBangoUpdateRating, self).setUp()
        self.list_url = self.get_list_url('rating')

    def test_update(self):
        self.create()
        data = samples.good_update_rating.copy()
        data['seller_product_bango'] = self.seller_product_bango_uri
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 201, res.content)

    def test_fail(self):
        self.create()
        data = samples.good_update_rating.copy()
        data['rating'] = 'AWESOME!'
        data['seller_product_bango'] = self.seller_product_bango_uri
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 400, res.content)


class TestCreateBillingConfiguration(SellerProductBangoBase):

    def setUp(self):
        super(TestCreateBillingConfiguration, self).setUp()
        self.list_url = self.get_list_url('billing')

    def create(self):
        super(TestCreateBillingConfiguration, self).create()
        self.transaction = Transaction.objects.create(
            provider=constants.SOURCE_BANGO,
            seller_product=self.seller_product,
            status=constants.STATUS_RECEIVED,
            uuid=self.uuid)

    def good(self):
        self.create()
        data = samples.good_billing_request.copy()
        data['seller_product_bango'] = self.seller_product_bango_uri
        data['transaction_uuid'] = self.transaction.uuid
        return data

    def test_good(self):
        res = self.client.post(self.list_url, data=self.good())
        eq_(res.status_code, 201, res.content)
        assert 'billingConfigurationId' in json.loads(res.content)

    def test_create_trans_if_not_existing(self):
        data = self.good()
        data['transaction_uuid'] = '<some-new-trans-uuid>'
        self.transaction.provider = constants.SOURCE_PAYPAL
        self.transaction.save()
        res = self.client.post(self.list_url, data=data)
        data = json.loads(res.content)
        tr = Transaction.objects.get(uid_pay=data['billingConfigurationId'])
        assert tr is not self.transaction

    def test_changed(self):
        res = self.client.post(self.list_url, data=self.good())
        eq_(res.status_code, 201)
        transactions = Transaction.objects.all()
        eq_(len(transactions), 1)
        transaction = transactions[0]
        eq_(transaction.status, constants.STATUS_PENDING)
        eq_(transaction.type, constants.TYPE_PAYMENT)
        ok_(transaction.uid_pay)
        eq_(transaction.uid_support, None)

    def test_missing(self):
        data = samples.good_billing_request.copy()
        del data['prices']
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 400)
        assert 'prices' in json.loads(res.content)

    def test_missing_success_url(self):
        data = self.good()
        del data['redirect_url_onsuccess']
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 400, res)

    def test_missing_error_url(self):
        data = self.good()
        del data['redirect_url_onerror']
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 400, res)

    def test_transaction(self):
        data = self.good()
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 201, res.content)
        tran = Transaction.objects.get()
        eq_(tran.provider, 1)

    def test_no_transaction(self):
        data = self.good()
        del data['transaction_uuid']
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 400, res)
        assert 'transaction_uuid' in json.loads(res.content)


class TestCreateBankConfiguration(BangoAPI):

    def setUp(self):
        super(TestCreateBankConfiguration, self).setUp()
        self.list_url = self.get_list_url('bank')

    def test_bank(self):
        self.create()
        data = samples.good_bank_details.copy()
        data['seller_bango'] = self.seller_bango_uri
        res = self.client.post(self.list_url, data=data)
        eq_(res.status_code, 201)


class TestGetSBI(BangoAPI):

    def setUp(self):
        self.get_url = '/bango/sbi/agreement/'
        self.list_url = '/bango/sbi/'

    def test_not_there(self):
        res = self.client.get_with_body(self.get_url,
                data={'seller_bango': '/some/uri/4/'})
        eq_(res.status_code, 400)

    def test_wrong_url(self):
        res = self.client.get('/bango/sbi/foo/')
        eq_(res.status_code, 404)

    def test_sbi(self):
        self.create()
        res = self.client.get_with_body(self.get_url,
                data={'seller_bango': self.seller_bango_uri})
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(data['text'], 'Blah...')
        eq_(data['valid'], '2010-08-31')

    def test_post(self):
        self.create()
        res = self.client.post(self.list_url,
                data={'seller_bango': self.seller_bango_uri})
        eq_(res.status_code, 201)
        data = json.loads(res.content)
        eq_(data['accepted'], '2014-01-23')
        eq_(data['expires'], '2013-01-23')
        eq_(str(self.seller_bango.reget().sbi_expires), '2014-01-23 00:00:00')


class TestNotification(APITest):
    api_name = 'bango'

    def setUp(self):
        self.trans_uuid = 'some-transaction-uid'
        self.seller = Seller.objects.create(uuid='seller-uuid')
        self.product = SellerProduct.objects.create(seller=self.seller,
                                                    external_id='xyz')
        self.trans = Transaction.objects.create(
            amount=1, provider=constants.SOURCE_BANGO,
            seller_product=self.product,
            uuid=self.trans_uuid,
            uid_pay='external-trans-uid'
        )
        self.url = self.get_list_url('notification')

    def data(self, overrides=None):
        data = {'moz_transaction': self.trans_uuid,
                'moz_signature': sign(self.trans_uuid),
                'billing_config_id': '1234',
                'bango_trans_id': '56789',
                'bango_response_code': 'OK',
                'bango_response_message': 'Success'}
        if overrides:
            data.update(overrides)
        return data

    def post(self, data, expected_status=201):
        res = self.client.post(self.url, data=data)
        eq_(res.status_code, expected_status, res.content)
        return json.loads(res.content)

    def test_success(self):
        self.post(self.data())
        tr = self.trans.reget()
        eq_(tr.status, constants.STATUS_COMPLETED)
        ok_(tr.uid_support)

    def test_failed(self):
        self.post(self.data(overrides={'bango_response_code': 'NOT OK'}))
        tr = self.trans.reget()
        eq_(tr.status, constants.STATUS_FAILED)

    def test_cancelled(self):
        self.post(self.data(overrides={'bango_response_code':
                                       CANCEL}))
        tr = self.trans.reget()
        eq_(tr.status, constants.STATUS_CANCELLED)

    def test_incorrect_sig(self):
        data = self.data({'moz_signature': sign(self.trans_uuid) + 'garbage'})
        self.post(data, expected_status=400)

    def test_missing_sig(self):
        data = self.data()
        del data['moz_signature']
        self.post(data, expected_status=400)

    def test_missing_transaction(self):
        data = self.data()
        del data['moz_transaction']
        self.post(data, expected_status=400)

    def test_unknown_transaction(self):
        self.post(self.data({'moz_transaction': 'does-not-exist'}),
                  expected_status=400)

    def test_already_completed(self):
        self.trans.status = constants.STATUS_COMPLETED
        self.trans.save()
        self.post(self.data(), expected_status=400)

    def test_expired_transaction(self):
        self.trans.created = datetime.now() - timedelta(seconds=62)
        self.trans.save()
        with self.settings(TRANSACTION_EXPIRY=60):
            self.post(self.data(), expected_status=400)


class TestRefund(APITest):

    def setUp(self):
        self.api_name = 'bango'
        self.uuid = 'sample:uid'
        self.seller, self.paypal, self.product = (
            make_seller_paypal('webpay:sample:uid'))
        self.trans = Transaction.objects.create(
            amount=5, seller_product=self.product,
            provider=constants.SOURCE_BANGO, uuid=self.uuid,
            status=constants.STATUS_COMPLETED)
        self.url = self.get_list_url('refund')

    def test_get(self):
        res = self.client.post(self.url, data={'uuid': self.uuid})
        eq_(res.status_code, 201, res.content)
        data = json.loads(res.content)
        eq_(data['status'], OK)

        eq_(len(Transaction.objects.all()), 2)
        trans = Transaction.objects.get(pk=data['resource_pk'])
        eq_(trans.related.pk, self.trans.pk)
        eq_(trans.type, TYPE_REFUND)

    def _fail(self):
        res = self.client.post(self.url, data={'uuid': self.uuid})
        eq_(res.status_code, 400)
        ok_(self.get_errors(res.content, 'uuid'))

    def test_not_bango(self):
        self.trans.provider = SOURCE_PAYPAL
        self.trans.save()
        self._fail()

    def test_not_complete(self):
        self.trans.status = STATUS_CANCELLED
        self.trans.save()
        self._fail()

    def test_not_payment(self):
        self.trans.type = TYPE_REFUND
        self.trans.save()
        self._fail()

    def test_refunded(self):
        Transaction.objects.create(seller_product=self.product,
            related=self.trans, provider=constants.SOURCE_BANGO,
            status=constants.STATUS_COMPLETED, type=constants.TYPE_REFUND,
            uuid='something', uid_pay='something')
        self._fail()

    @mock.patch.object(ClientMock, 'mock_results')
    def test_already(self, mock_results):
        mock_results.return_value = {'responseCode': ALREADY_REFUNDED}
        with self.assertRaises(BangoError):
            self.client.post(self.url, data={'uuid': self.uuid})
        # Check we didn't create a transaction.
        eq_(len(Transaction.objects.all()), 1)


class TestRefundStatus(APITest):

    def setUp(self):
        self.api_name = 'bango'
        self.refund_uuid = 'sample:refund'
        self.seller, self.paypal, self.product = (
            make_seller_paypal('webpay:sample:uid'))
        self.refund = Transaction.objects.create(
            amount=5, seller_product=self.product,
            type=constants.TYPE_REFUND,
            provider=constants.SOURCE_BANGO,
            uuid=self.refund_uuid, uid_pay='asd',
            status=constants.STATUS_COMPLETED)
        self.url = '/bango/refund/status/'

    def test_get(self):
        res = self.client.get_with_body(self.url,
                                        data={'uuid': self.refund_uuid})
        data = json.loads(res.content)
        eq_(data['status'], OK)

    def test_not_refund(self):
        self.refund.type = constants.TYPE_PAYMENT
        self.refund.save()

        res = self.client.get_with_body(self.url,
                                        data={'uuid': self.refund_uuid})
        eq_(res.status_code, 400)
        ok_(self.get_errors(res.content, 'uuid'))

    @mock.patch.object(ClientMock, 'mock_results')
    def test_pending(self, mock_results):
        mock_results.return_value = {'responseCode': PENDING}
        res = self.client.get_with_body(self.url,
                                        data={'uuid': self.refund.uuid})
        data = json.loads(res.content)
        eq_(data['status'], PENDING)
        eq_(self.refund.reget().status, constants.STATUS_PENDING)

    @mock.patch.object(ClientMock, 'mock_results')
    def test_failed(self, mock_results):
        mock_results.return_value = {'responseCode': CANT_REFUND}
        res = self.client.get_with_body(self.url,
                                        data={'uuid': self.refund.uuid})
        data = json.loads(res.content)
        eq_(data['status'], CANT_REFUND)
        eq_(self.refund.reget().status, constants.STATUS_FAILED)

    @mock.patch.object(ClientMock, 'mock_results')
    def test_ok(self, mock_results):
        self.refund.status = constants.STATUS_PENDING
        self.refund.save()
        mock_results.return_value = {'responseCode': OK}
        res = self.client.get_with_body(self.url,
                                        data={'uuid': self.refund.uuid})
        data = json.loads(res.content)
        eq_(data['status'], OK)
        eq_(self.refund.reget().status, constants.STATUS_COMPLETED)
