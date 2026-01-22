from django.urls import path
from . import views

urlpatterns = [
    path('api/calculate-commission/', views.get_commission_calculation, name='api_calculate_commission'),
]
