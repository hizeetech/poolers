from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, identifier=None, **kwargs):
        if password is None:
            return None

        raw_identifier = identifier or username or kwargs.get("username") or ""
        raw_identifier = (raw_identifier or "").strip()
        if not raw_identifier:
            return None

        UserModel = get_user_model()
        user = UserModel.objects.filter(username__iexact=raw_identifier).first()

        if not user:
            return None

        if not self.user_can_authenticate(user):
            return None

        if user.check_password(password):
            return user

        return None
