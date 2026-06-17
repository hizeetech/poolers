from django import forms
from .models import PendingAgentRegistration
from betting.services.email_policy import duplicate_email_details, is_truthy, normalize_email_value

class AgentRegistrationForm(forms.ModelForm):
    confirm_duplicate_email = forms.CharField(required=False, widget=forms.HiddenInput())
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
        email = normalize_email_value(cleaned_data.get("email"))

        if password and confirm_password and password != confirm_password:
            self.add_error('confirm_password', "Passwords do not match")

        if email:
            cleaned_data["email"] = email
            details = duplicate_email_details(email)
            if details["exists"] and not is_truthy(cleaned_data.get("confirm_duplicate_email")):
                self.add_error('email', "This email is already assigned to another user. Confirm to continue.")

        return cleaned_data
