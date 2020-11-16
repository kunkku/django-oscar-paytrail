# Copyright (c) 2014-2020 Data King Ltd
# See LICENSE file for license details

from django.urls import path
from paytrail.views import *

app_name = 'paytrail'

urlpatterns = (
    path('success/', SuccessView.as_view(), name='success'),
    path('failure/', FailureView.as_view(), name='failure'),
    path('notify/<token>/', notification, name='notification')
)
