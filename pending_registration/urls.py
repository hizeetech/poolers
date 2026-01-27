from django.urls import path
from . import views

app_name = 'pending_registration'

urlpatterns = [
    path('register-agent/', views.register_agent, name='register_agent'),
]
