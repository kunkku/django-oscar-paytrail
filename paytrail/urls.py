# Copyright (c) 2014 Data King Ltd
# See LICENSE file for license details

from django.conf.urls import *
from paytrail.views import *

urlpatterns = patterns(
    '',
    url(r'^success/$', SuccessView.as_view(), name='paytrail-success'),
    url(r'^failure/$', FailureView.as_view(), name='paytrail-failure'),
    url(r'^notify/(.+)/$', notification, name='paytrail-notification')
)
