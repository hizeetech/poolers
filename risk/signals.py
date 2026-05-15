from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import RiskEngineSettings
from .services import clear_risk_settings_cache


@receiver(post_save, sender=RiskEngineSettings)
def _clear_risk_cache_on_settings_change(sender, instance, **kwargs):
    clear_risk_settings_cache()

