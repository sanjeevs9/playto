from django.apps import AppConfig
from django.conf import settings


class PayoutsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payouts"

    def ready(self) -> None:
        # Validate bank-simulation probabilities at app startup. Catching this
        # at boot (and not at first task fire) means a bad deploy fails fast
        # in CI / on Railway rather than silently mis-routing payouts.
        s = settings.BANK_SIMULATION_SUCCESS
        f = settings.BANK_SIMULATION_FAILURE
        if not (0 <= s <= 1 and 0 <= f <= 1 and s + f <= 1):
            raise RuntimeError(
                "Invalid BANK_SIMULATION_* config: success="
                f"{s} + failure={f} must each be in [0,1] and sum to <= 1 "
                "(remainder is the simulated 'hang' probability)."
            )
