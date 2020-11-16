# Copyright (c) 2014-2020 Data King Ltd
# See LICENSE file for license details

from django.conf import settings
from django.core.signing import Signer
from django.http import HttpResponse
from django.urls import reverse
from oscar.apps.checkout.views import PaymentDetailsView as CorePaymentDetailsView
from oscar.apps.payment import exceptions
from oscar.apps.payment.models import Source, SourceType
from oscar.core.loading import get_model

import json
import urllib.request


source_type = SourceType.objects.get_or_create(name='Paytrail')[0]
signer = Signer()


URL = 'https://payment.paytrail.com/api-payment/create'

pw_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
pw_manager.add_password(
    None,
    URL,
    # default to testing credentials
    getattr(settings, 'PAYTRAIL_MERCHANT_ID', '13466'),
    getattr(
        settings, 'PAYTRAIL_MERCHANT_SECRET', '6pKF4jkv97zmqBJ3ZL8gUw5DfT2NMQ'
    )
)

class ErrorProcessor(urllib.request.HTTPErrorProcessor):
    def http_error_400(self, req, fp, code, msg, hdrs):
        raise exceptions.PaymentError(json.load(fp)['errorMessage'])


url_opener = urllib.request.build_opener(
    urllib.request.HTTPBasicAuthHandler(pw_manager), ErrorProcessor()
)


class PaymentDetailsView(CorePaymentDetailsView):
    preview = True

    def handle_place_order_submission(self, request):
        return self.submit(
            **self.build_submission(payment_kwargs={'req': request})
        )

    def handle_payment(self, order_number, total, **kwargs):
        def uri(name, arg=None):
            return kwargs['req'].build_absolute_uri(
                reverse('paytrail:' + name, args=(arg,) if arg else None)
            )

        raise exceptions.RedirectRequired(
            json.load(
                url_opener.open(
                    urllib.request.Request(
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
                        ).encode(),
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

    def render_payment_details(self, request, **kwargs):
        return self.render_preview(request, **kwargs)


def notification(request, token):
    get_model('order', 'Order').objects.get(
        number=signer.unsign(token)
    ).sources.get(source_type=source_type).debit()
    return HttpResponse()
