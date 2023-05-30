# Copyright (c) 2014-2023 Data King Ltd
# See LICENSE file for license details

import hashlib
import hmac
import json
import logging
import urllib.request
import uuid
from datetime import datetime
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from oscar.apps.checkout.views import PaymentDetailsView as CorePaymentDetailsView
from oscar.apps.payment import exceptions
from oscar.apps.payment.models import Source, SourceType
from oscar.core.loading import get_model

TEST_MERCHANT_ID = '375917'
TEST_MERCHANT_SECRET = 'SAIPPUAKAUPPIAS'
URL = 'https://services.paytrail.com/payments'

Basket = get_model('basket', 'Basket')
Order = get_model('order', 'Order')
Transaction = get_model('payment', 'Transaction')
logger = logging.getLogger(__name__)

def calculate_hmac(headers, body):
    """
    Calculates HMAC for from given headers and body.
    See https://docs.paytrail.com/#/?id=authentication

    headers is a list of (header, value) tuples.
    """
    msg = '\n'.join(
        list(
            map(
                lambda hdr: hdr[0] + ':' + hdr[1],
                sorted(
                    filter(lambda hdr: hdr[0].startswith('checkout-'), headers),
                    key=lambda hdr: hdr[0],
                ),
            )
        ) + [body],
    )

    # use secret as hmac key, defaults to testing credentials
    key = getattr(settings, 'PAYTRAIL_MERCHANT_SECRET', TEST_MERCHANT_SECRET).encode()
    return hmac.new(key, msg.encode(), hashlib.sha512).hexdigest()

def get_source_type():
    return SourceType.objects.get_or_create(name='Paytrail')[0]

def hundred(value):
    return round(value * 100)


class ErrorProcessor(urllib.request.HTTPErrorProcessor):
    def handle_error(self, fp):
        data = json.load(fp)
        raise exceptions.PaymentError(data['message'])

    def http_error_400(self, req, fp, code, msg, hdrs):
        self.handle_error(fp)

    def http_error_401(self, req, fp, code, msg, hdrs):
        self.handle_error(fp)

url_opener = urllib.request.build_opener(
    ErrorProcessor(),
)


class PaymentDetailsView(CorePaymentDetailsView):
    preview = True

    def gen_headers(self, body):
        """Returns headers for payment request as list of (header, value) tuples."""
        headers = [
            # read merchant id from settings or default to testing credentials
            ('checkout-account', getattr(settings, 'PAYTRAIL_MERCHANT_ID', TEST_MERCHANT_ID)),
            ('checkout-algorithm', 'sha512'),
            ('checkout-method', 'POST'),
            ('checkout-nonce', str(uuid.uuid4())),
            ('checkout-timestamp', datetime.now().isoformat()),
            ('Content-Type', 'application/json; charset=utf-8'),
        ]
        headers.append(('signature', calculate_hmac(headers, body)))
        return headers

    def get_customer_dict(self, user):
        """Converts given user to dict for payment request."""
        if user.is_anonymous:
            return {
                'email': self.checkout_session.get_guest_email(),
            }

        return {
            'email': user.email,
            'firstName': user.first_name,
            'lastName': user.last_name,
        }

    def get_address_dict(self, address):
        """Returns address dict for payment request."""
        if address is not None:
            addr = {
                'streetAddress': ', '.join(
                    list(
                        filter(
                            lambda x: len(x) > 0,
                            [address.line1, address.line2, address.line3],
                        ),
                    ),
                ),
                'city': address.line4,
                'postalCode': address.postcode,
                'country': address.country.code,
            }
            if len(address.state) > 0:
                addr['county'] = address.state
            return addr
        return None

    def create_payment_request(self, order_number, order_total, lang):
        """Returns payment request."""
        ctx = self.get_context_data()
        basket = ctx['basket']

        # use basket's strategy to set fixed VAT percentage
        vat_percentage = hundred(basket.strategy.rate)

        class PaytrailItem:
            def __init__(self, basket_line=None, unit_price=None, product_code=None):
                self.unit_price = unit_price
                self.units = 1
                self.product_code = product_code
                if basket_line is not None:
                    self.unit_price = basket_line.purchase_info.price.incl_tax
                    self.units = basket_line.quantity
                    self.product_code = basket_line.product.upc

            def to_json(self):
                return {
                    'unitPrice': hundred(self.unit_price),
                    'units': self.units,
                    'vatPercentage': vat_percentage,
                    'productCode': str(self.product_code),
                }

        # Paytrail requires at least one item and total payment amount
        # must match the total sum of items.

        # add basket lines to items
        items = list(map(lambda l: PaytrailItem(basket_line=l), basket.lines.all()))

        # add optional shipping charge to items
        if ctx['shipping_charge'] is not None:
            items.append(
                PaytrailItem(
                    unit_price=ctx['shipping_charge'].incl_tax,
                    product_code='shipping',
                ),
            )

        # add optional surcharges to items
        if ctx['surcharges'] is not None:
            items.append(
                PaytrailItem(
                    unit_price=ctx['surcharges'].total.incl_tax,
                    product_code='surcharges',
                ),
            )

        def uri(name, arg=None):
            return self.request.build_absolute_uri(
                reverse('paytrail:' + name, args=(arg,) if arg else None)
            )

        body = {
            'stamp': str(uuid.uuid4()),
            'reference': str(order_number),
            'amount': hundred(order_total.incl_tax),
            'currency': order_total.currency,
            'language': lang,
            'items': list(map(lambda item: item.to_json(), items)),
            'customer': self.get_customer_dict(ctx['user']),
            'redirectUrls': {
                'success': uri('success'),
                'cancel': uri('failure'),
            },
        }

        shipping_address = self.get_address_dict(ctx['shipping_address'])
        if shipping_address is not None:
            body['deliveryAddress'] = shipping_address

        billing_address = self.get_address_dict(ctx['billing_address'])
        if billing_address is not None:
            body['invoicingAddress'] = billing_address

        body_str = json.dumps(body)
        return urllib.request.Request(
            URL,
            body_str.encode(),
            dict(self.gen_headers(body_str)),
        )

    def handle_payment(self, order_number, total, **kwargs):
        lang = kwargs['lang'] if 'lang' in kwargs else 'FI'

        response = url_opener.open(
            self.create_payment_request(order_number, total, lang)
        )

        # logging the value of request-id header is recommended
        request_id = response.getheader('request-id')
        logger.info('Paytrail request id for order #%s: %s', order_number, request_id)

        body_str = response.read().decode('utf-8')

        # validate signature in response
        received_hmac = response.getheader('signature')
        calculated_hmac = calculate_hmac(response.getheaders(), body_str)
        if received_hmac != calculated_hmac:
            logger.error('Invalid signature in response: %s - should be %s', received_hmac, calculated_hmac)
            raise exceptions.PaymentError('Invalid signature received')

        body = json.loads(body_str)
        raise exceptions.RedirectRequired(body['href'])


def validate_signature(query_dict):
    """Validates signature in request query parameters."""
    received_hmac = query_dict['signature']
    calculated_hmac = calculate_hmac(
        # convert query params to list of tuples
        [(k, v) for k, v in query_dict.items()],
        '', # empty body
    )
    if received_hmac != calculated_hmac:
        logger.error('Invalid signature in query params: %s - should be %s', received_hmac, calculated_hmac)
        raise ValidationError('Invalid signature', code=400)


class ReturnView(CorePaymentDetailsView):

    def check_pre_conditions(self, request):
        pass

    def check_skip_conditions(self, request):
        pass

    def validate_request(self, request):
        validate_signature(request.GET)
        if 'checkout-transaction-id' not in request.GET:
            logger.error('checkout-transaction-id query parameter missing')
            raise ValidationError('checkout-transaction-id missing', code=400)

        if 'checkout-status' not in request.GET:
            logger.error('checkout-status query parameter missing')
            raise ValidationError('checkout-status missing', code=400)

    def get(self, request, *args, **kwargs):
        self.validate_request(request)

        try:
            self.get_submitted_basket().thaw()
        except Basket.DoesNotExist:
            # no basket found from session, this might
            # be a request to update transaction status
            self.update_transaction_status()
            return HttpResponse()

        return self.handle_place_order_submission(request)

    def update_transaction_status(self):
        transaction_id = self.request.GET.get('checkout-transaction-id')
        transaction = get_object_or_404(Transaction, reference=transaction_id)
        new_status = self.request.GET.get('checkout-status')
        if new_status != transaction.status:
            transaction.status = new_status
            transaction.save()
            logger.info('Transaction %d status changed to %s', transaction.id, new_status)


class SuccessView(ReturnView):
    """Payment was successful."""

    def handle_payment(self, order_number, total, **kwargs):
        self.add_payment_source(
            Source(amount_allocated=total.incl_tax, source_type=get_source_type())
        )

    def place_order(self, *args, **kwargs):
        order = super().place_order(*args, **kwargs)
        source = order.sources.get(source_type=get_source_type())
        # create new transaction by debit method
        source.debit(
            reference=self.request.GET.get('checkout-transaction-id'),
            status=self.request.GET.get('checkout-status'),
        )
        return order


class FailureView(ReturnView):
    """Payment failed or it was cancelled by user."""

    def handle_payment(self, order_number, total, **kwargs):
        raise exceptions.UnableToTakePayment()

    def render_payment_details(self, request, **kwargs):
        return self.render_preview(request, **kwargs)
