# Copyright (c) 2014-2015 Data King Ltd
# See LICENSE file for license details

from django.conf import settings
from django.core.signing import Signer
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from oscar.apps.checkout.views import PaymentDetailsView as CorePaymentDetailsView
from oscar.apps.order.models import Order
from oscar.apps.payment import exceptions
from oscar.apps.payment.models import Source, SourceType

import json
import urllib2


source_type = SourceType.objects.get_or_create(name='Paytrail')[0]
signer = Signer()


URL = 'https://payment.paytrail.com/api-payment/create'

pw_manager = urllib2.HTTPPasswordMgrWithDefaultRealm()
pw_manager.add_password(
    None,
    URL,
    # default to testing credentials
    getattr(settings, 'PAYTRAIL_MERCHANT_ID', '13466'),
    getattr(
        settings, 'PAYTRAIL_MERCHANT_SECRET', '6pKF4jkv97zmqBJ3ZL8gUw5DfT2NMQ'
    )
)

class ErrorProcessor(urllib2.HTTPErrorProcessor):
    def http_error_400(self, req, fp, code, msg, hdrs):
        raise exceptions.PaymentError(json.load(fp)['errorMessage'])


url_opener = urllib2.build_opener(
    urllib2.HTTPBasicAuthHandler(pw_manager), ErrorProcessor()
)


class PaymentDetailsView(CorePaymentDetailsView):

    def handle_place_order_submission(self, request):
        return self.submit(
            **self.build_submission(payment_kwargs={'req': request})
        )

    def handle_payment(self, order_number, total, **kwargs):
        def uri(name, arg=None):
            return kwargs['req'].build_absolute_uri(
                reverse('paytrail-' + name, args=(arg,) if arg else None)
            )

        raise exceptions.RedirectRequired(
            json.load(
                url_opener.open(
                    urllib2.Request(
                        URL,
                        json.dumps(
                            {
                                'orderNumber': str(order_number),
                                'currency': total.currency,
                                'urlSet': {
                                    'success': uri('success'),
                                    'failure': uri('failure'),
                                    'notification': uri(
                                        'notification',
                                        signer.sign(order_number)
                                    )
                                },
                                'price': str(total.incl_tax)
                            }
                        ),
                        {
                            'Content-Type': 'application/json',
                            'X-Verkkomaksut-Api-Version': '1'
                        }
                    )
                )
            )['url']
        )


class ReturnView(CorePaymentDetailsView):

    def check_pre_conditions(self, request):
        pass

    def check_skip_conditions(self, request):
        pass

    def get(self, request, *args, **kwargs):
        self.get_submitted_basket().thaw()
        return self.handle_place_order_submission(request)


class SuccessView(ReturnView):

    def handle_payment(self, order_number, total, **kwargs):
        self.add_payment_source(
            Source(amount_allocated=total.incl_tax, source_type=source_type)
        )


class FailureView(ReturnView):

    def handle_payment(self, order_number, total, **kwargs):
        raise exceptions.UnableToTakePayment()


def notification(request, token):
    Order.objects.get(number=signer.unsign(token)).sources.get(
        source_type=source_type
    ).debit()
    return HttpResponse()
