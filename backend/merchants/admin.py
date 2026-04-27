from django.contrib import admin

from .models import BankAccount, LedgerEntry, Merchant


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "email", "created_at")
    search_fields = ("name", "email")
    readonly_fields = ("created_at",)


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merchant",
        "holder_name",
        "account_number_last4",
        "ifsc",
        "is_default",
    )
    list_filter = ("is_default",)
    search_fields = ("holder_name", "merchant__name")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merchant",
        "entry_type",
        "amount_paise",
        "description",
        "created_at",
    )
    list_filter = ("entry_type",)
    search_fields = ("merchant__name", "description")
    readonly_fields = (
        "merchant",
        "amount_paise",
        "entry_type",
        "description",
        "created_at",
    )

    def has_add_permission(self, request):
        # Ledger entries are written by the application, never manually.
        return False

    def has_change_permission(self, request, obj=None):
        # Ledger entries are immutable — corrections are made via new entries.
        return False

    def has_delete_permission(self, request, obj=None):
        return False
