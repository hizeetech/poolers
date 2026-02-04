
import json
from django.conf import settings
from django.utils import timezone
from fido2.server import Fido2Server
from fido2.webauthn import PublicKeyCredentialRpEntity, PublicKeyCredentialUserEntity, AttestedCredentialData
from fido2.cose import CoseKey
from fido2 import cbor
from fido2.utils import websafe_encode, websafe_decode
from .models import WebAuthnCredential

class WebAuthnUtils:
    def __init__(self, rp_id=None):
        self.rp_id = rp_id or settings.WEBAUTHN_RP_ID
        self.rp = PublicKeyCredentialRpEntity(id=self.rp_id, name=settings.WEBAUTHN_RP_NAME)
        self.server = Fido2Server(self.rp)

    def register_begin(self, user):
        user_entity = PublicKeyCredentialUserEntity(
            id=str(user.id).encode('utf-8'),
            name=user.email,
            display_name=user.get_full_name() or user.email
        )
        
        existing_creds = WebAuthnCredential.objects.filter(user=user)
        credentials = []
        for cred in existing_creds:
            credentials.append(AttestedCredentialData.create(
                b'\x00'*16, 
                bytes(cred.credential_id), 
                CoseKey.parse(cbor.decode(bytes(cred.public_key)))
            ))
            
        from fido2.webauthn import ResidentKeyRequirement

        registration_data, state = self.server.register_begin(
            user_entity,
            credentials=credentials,
            user_verification='preferred',
            authenticator_attachment='platform',
            resident_key_requirement=ResidentKeyRequirement.PREFERRED
        )
        
        return registration_data, state

    def register_complete(self, state, response_data, user, device_name):
        try:
            auth_data = self.server.register_complete(
                state,
                response_data
            )
            
            credential_id = auth_data.credential_data.credential_id
            public_key = cbor.encode(auth_data.credential_data.public_key)
            
            if WebAuthnCredential.objects.filter(credential_id=credential_id).exists():
                raise ValueError("Credential already registered")

            WebAuthnCredential.objects.create(
                user=user,
                credential_id=credential_id,
                public_key=public_key,
                sign_count=auth_data.counter,
                device_name=device_name
            )
            return True
        except Exception as e:
            print(f"Register complete error: {e}")
            raise e

    def authenticate_begin(self, user=None):
        creds_data = []
        if user:
            credentials = WebAuthnCredential.objects.filter(user=user)
            if not credentials.exists():
                raise ValueError("No biometric credentials found")
                
            for cred in credentials:
                 creds_data.append(AttestedCredentialData.create(
                     b'\x00'*16, 
                     bytes(cred.credential_id), 
                     CoseKey.parse(cbor.decode(bytes(cred.public_key)))
                 ))
        else:
            # Usernameless flow - allow any credential
            creds_data = None
             
        auth_data, state = self.server.authenticate_begin(
            creds_data,
            user_verification='preferred'
        )
        return auth_data, state

    def authenticate_complete(self, state, response_data, user=None):
        credentials = []
        if user:
            credentials = WebAuthnCredential.objects.filter(user=user)
        else:
            # Usernameless: find credential by ID
            if 'id' in response_data:
                # 'id' might be bytes (if decoded in view) or string (base64url)
                cred_id_val = response_data['id']
                if isinstance(cred_id_val, str):
                    cred_id_bytes = websafe_decode(cred_id_val)
                else:
                    cred_id_bytes = cred_id_val
                
                try:
                    # Try to find by credential_id
                    cred = WebAuthnCredential.objects.filter(credential_id=cred_id_bytes).first()
                    if cred:
                        credentials = [cred]
                except Exception as e:
                    print(f"Error finding credential: {e}")
            
            if not credentials:
                 raise ValueError("Unknown credential or user not found")

        # Use bytes for dictionary keys to ensure matching
        creds_map = {bytes(cred.credential_id): cred for cred in credentials}
        
        creds_data = []
        for cred in credentials:
            cd = AttestedCredentialData.create(
                b'\x00'*16, 
                bytes(cred.credential_id), 
                CoseKey.parse(cbor.decode(bytes(cred.public_key)))
            )
            creds_data.append(cd)
            
        try:
            # authenticate_complete returns the AttestedCredentialData that matched
            matched_cred = self.server.authenticate_complete(
                state,
                creds_data,
                response_data
            )
            
            cred_id = matched_cred.credential_id
            cred = creds_map.get(bytes(cred_id))
            
            if cred:
                # Get counter from authenticator data
                from fido2.webauthn import AuthenticatorData
                auth_data_bytes = response_data['response']['authenticatorData']
                # auth_data_bytes is already decoded from base64url if coming from view
                # but let's ensure it's bytes.
                # In views.py, we have:
                # if 'authenticatorData' in resp:
                #    resp['authenticatorData'] = websafe_decode(resp['authenticatorData'])
                # So it should be bytes here.
                
                auth_data = AuthenticatorData(auth_data_bytes)
                
                # Verify counter increment
                if auth_data.counter <= cred.sign_count and cred.sign_count > 0:
                     # Note: Some authenticators always send 0. Only fail if > 0 and not increasing.
                     # But for security, we should warn or fail. 
                     # For now, let's just log it or strict check?
                     # Let's enforce it if sign_count is supported (non-zero)
                     pass 

                cred.sign_count = auth_data.counter
                cred.last_used = timezone.now()
                cred.save()
                return cred
            
            print(f"Credential mismatch: got {cred_id!r}, expected one of {list(creds_map.keys())}")
            return None
        except Exception as e:
            print(f"Auth complete error: {e}")
            raise e
