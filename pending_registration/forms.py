from django import forms
from django.contrib.auth import get_user_model
from .models import PendingAgentRegistration

User = get_user_model()

class AgentRegistrationForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control'}))
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control'}))

    class Meta:
        model = PendingAgentRegistration
        fields = ['full_name', 'email', 'phone', 'state', 'user_type', 'password']
        widgets = {
            'full_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'state': forms.TextInput(attrs={'class': 'form-control'}),
            'user_type': forms.Select(attrs={'class': 'form-select'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")
        email = cleaned_data.get("email")

        if password and confirm_password and password != confirm_password:
            self.add_error('confirm_password', "Passwords do not match")
            
        if email and User.objects.filter(email=email).exists():
             self.add_error('email', "User with this email already exists.")
             
        if email and PendingAgentRegistration.objects.filter(email=email, status='PENDING').exists():
             self.add_error('email', "A pending registration for this email already exists.")

        return cleaned_data
