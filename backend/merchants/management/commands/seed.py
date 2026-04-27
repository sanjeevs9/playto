"""Seed merchants, credit history, and bank accounts.

Usage:
    python manage.py seed              # idempotent — skips merchants already in the DB
    python manage.py seed --reset      # delete all merchant data and reseed from scratch

Three merchants are created with realistic credit histories and registered
bank accounts. They mirror the kind of users Playto Pay targets — small
Indian agencies, freelancers, and consultancies collecting USD from
international customers.

All amounts are stored as ``BigIntegerField`` paise (no floats, no decimals).
The ``_rupees`` helper at the top makes the seed file readable in INR while
keeping the conversion deterministic.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from merchants.models import BankAccount, LedgerEntry, Merchant


def _rupees(n: int) -> int:
    """Convert whole INR rupees to paise. Always returns int. No floats."""
    return n * 100


SEED_DATA = [
    {
        "name": "Acme Web Studios",
        "email": "founders@acme-web-studios.example",
        "credits": [
            (_rupees(75_000), "Customer payment — Stripe Acme Inc"),
            (_rupees(1_20_000), "Customer payment — GitHub Marketplace"),
            (_rupees(32_500), "Customer payment — Notion Labs"),
        ],
        "banks": [
            {
                "holder_name": "Acme Web Studios LLP",
                "account_number_last4": "8421",
                "ifsc": "HDFC0001234",
                "nickname": "HDFC primary",
                "is_default": True,
            },
            {
                "holder_name": "Acme Web Studios LLP",
                "account_number_last4": "9912",
                "ifsc": "ICIC0009876",
                "nickname": "ICICI ops",
                "is_default": False,
            },
        ],
    },
    {
        "name": "Priya Sharma — Brand Design",
        "email": "hello@priyasharma.design.example",
        "credits": [
            (_rupees(45_000), "Customer payment — Webflow"),
            (_rupees(28_000), "Customer payment — Linear"),
        ],
        "banks": [
            {
                "holder_name": "Priya Sharma",
                "account_number_last4": "1093",
                "ifsc": "UTIB0005555",
                "nickname": "Personal current",
                "is_default": True,
            },
        ],
    },
    {
        "name": "Bharat Coders Consultancy",
        "email": "ops@bharatcoders.example",
        "credits": [
            (_rupees(2_50_000), "Customer payment — Anthropic"),
            (_rupees(1_85_000), "Customer payment — OpenAI"),
            (_rupees(3_60_000), "Customer payment — Datadog"),
        ],
        "banks": [
            {
                "holder_name": "Bharat Coders Pvt Ltd",
                "account_number_last4": "0042",
                "ifsc": "SBIN0011111",
                "nickname": "SBI corporate",
                "is_default": True,
            },
            {
                "holder_name": "Bharat Coders Pvt Ltd",
                "account_number_last4": "7777",
                "ifsc": "KKBK0002222",
                "nickname": "Kotak settlements",
                "is_default": False,
            },
        ],
    },
]


class Command(BaseCommand):
    help = "Seed merchants, credit history, and bank accounts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Delete all merchants, bank accounts, ledger entries, "
                "payouts, and idempotency keys before reseeding."
            ),
        )

    @transaction.atomic
    def handle(self, *args, reset: bool = False, **options) -> None:
        if reset:
            self.stdout.write(
                self.style.WARNING("--reset: deleting all merchant data...")
            )
            # Delete in dependency order — payouts + idempotency keys reference
            # merchants; ledger entries reference both. Without this order
            # PROTECT FKs would refuse the delete.
            from payouts.models import IdempotencyKey, Payout

            IdempotencyKey.objects.all().delete()
            LedgerEntry.objects.all().delete()
            Payout.objects.all().delete()
            BankAccount.objects.all().delete()
            Merchant.objects.all().delete()

        for spec in SEED_DATA:
            merchant, created = Merchant.objects.get_or_create(
                email=spec["email"],
                defaults={"name": spec["name"]},
            )
            if not created:
                self.stdout.write(
                    f"  - {merchant.name} (id={merchant.id}) already seeded; skipping."
                )
                continue

            for amount_paise, description in spec["credits"]:
                LedgerEntry.objects.create(
                    merchant=merchant,
                    amount_paise=amount_paise,
                    entry_type=LedgerEntry.EntryType.CREDIT,
                    description=description,
                )

            for bank in spec["banks"]:
                BankAccount.objects.create(merchant=merchant, **bank)

            total_credit = sum(c[0] for c in spec["credits"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"  + {merchant.name} (id={merchant.id}) — "
                    f"{len(spec['credits'])} credit(s), "
                    f"{len(spec['banks'])} bank account(s), "
                    f"balance {total_credit / 100:,.2f} INR"
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSeed complete. {Merchant.objects.count()} merchants in DB."
            )
        )
