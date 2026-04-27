from django.urls import path

from .views import (
    MerchantBalanceView,
    MerchantBankAccountsView,
    MerchantLedgerView,
    MerchantsView,
)

urlpatterns = [
    path("merchants", MerchantsView.as_view(), name="merchants-list"),
    path(
        "merchants/<int:pk>/balance",
        MerchantBalanceView.as_view(),
        name="merchant-balance",
    ),
    path(
        "merchants/<int:pk>/ledger",
        MerchantLedgerView.as_view(),
        name="merchant-ledger",
    ),
    path(
        "merchants/<int:pk>/bank-accounts",
        MerchantBankAccountsView.as_view(),
        name="merchant-bank-accounts",
    ),
]
