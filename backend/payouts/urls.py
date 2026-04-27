from django.urls import path

from .views import PayoutDetailView, PayoutsView

urlpatterns = [
    path("payouts", PayoutsView.as_view(), name="payouts-list"),
    path("payouts/<uuid:pk>", PayoutDetailView.as_view(), name="payouts-detail"),
]
