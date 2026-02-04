
// WebAuthn Helpers

function bufferToBase64url(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.byteLength; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary)
        .replace(/\+/g, '-')
        .replace(/\//g, '_')
        .replace(/=/g, '');
}

function base64urlToBuffer(base64url) {
    const padding = '='.repeat((4 - base64url.length % 4) % 4);
    const base64 = (base64url + padding)
        .replace(/\-/g, '+')
        .replace(/_/g, '/');
    const binary = atob(base64);
    const buffer = new ArrayBuffer(binary.length);
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    return buffer;
}

async function registerWebAuthn() {
    // Check for IP address usage (insecure context for WebAuthn)
    if (window.location.hostname !== 'localhost' && /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(window.location.hostname) && window.location.protocol !== 'https:') {
        alert("Security Alert: WebAuthn requires a secure context.\n\nPlease use 'http://localhost:8000' instead of the IP address (" + window.location.hostname + ") to enable fingerprint login.");
        return;
    }

    try {
        const deviceName = prompt("Enter a name for this device:", "My Device");
        if (!deviceName) return;

        // Get options from server
        const resp = await fetch('/webauthn/register/begin/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value,
                'Content-Type': 'application/json'
            }
        });
        const options = await resp.json();
        if (options.status === 'error') throw new Error(options.message);

        // Convert base64url fields to ArrayBuffer
        options.user.id = base64urlToBuffer(options.user.id);
        options.challenge = base64urlToBuffer(options.challenge);
        if (options.excludeCredentials) {
            options.excludeCredentials.forEach(cred => {
                cred.id = base64urlToBuffer(cred.id);
            });
        }

        // Create credentials
        const credential = await navigator.credentials.create({ publicKey: options });

        // Send to server
        const credentialData = {
            id: credential.id,
            rawId: bufferToBase64url(credential.rawId),
            type: credential.type,
            response: {
                clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
                attestationObject: bufferToBase64url(credential.response.attestationObject)
            },
            device_name: deviceName
        };

        const verifyResp = await fetch('/webauthn/register/complete/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(credentialData)
        });

        const verifyResult = await verifyResp.json();
        if (verifyResult.status === 'success') {
            alert('Fingerprint registered successfully!');
            location.reload();
        } else {
            throw new Error(verifyResult.message);
        }

    } catch (err) {
        console.error(err);
        alert('Registration failed: ' + err.message);
    }
}

async function loginWebAuthn() {
    // Check for IP address usage (insecure context for WebAuthn)
    if (window.location.hostname !== 'localhost' && /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(window.location.hostname) && window.location.protocol !== 'https:') {
        alert("Security Alert: WebAuthn requires a secure context.\n\nPlease use 'http://localhost:8000' instead of the IP address (" + window.location.hostname + ") to enable fingerprint login.");
        return;
    }

    try {
        const emailField = document.querySelector('input[name="username"]'); // Assuming 'username' is the field name
        const email = emailField ? emailField.value : null;
        
        // Removed email requirement for usernameless flow
        // if (!email) {
        //     alert('Please enter your email/username first.');
        //     return;
        // }

        const resp = await fetch('/webauthn/login/begin/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ email: email })
        });
        
        const options = await resp.json();
        if (options.status === 'error') throw new Error(options.message);

        // Convert options
        options.challenge = base64urlToBuffer(options.challenge);
        if (options.allowCredentials) {
            options.allowCredentials.forEach(cred => {
                cred.id = base64urlToBuffer(cred.id);
            });
        }

        // Get assertion
        const assertion = await navigator.credentials.get({ publicKey: options });

        // Send to server
        const authData = {
            id: assertion.id,
            rawId: bufferToBase64url(assertion.rawId),
            type: assertion.type,
            response: {
                clientDataJSON: bufferToBase64url(assertion.response.clientDataJSON),
                authenticatorData: bufferToBase64url(assertion.response.authenticatorData),
                signature: bufferToBase64url(assertion.response.signature),
                userHandle: assertion.response.userHandle ? bufferToBase64url(assertion.response.userHandle) : null
            }
        };

        const verifyResp = await fetch('/webauthn/login/complete/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(authData)
        });

        const verifyResult = await verifyResp.json();
        if (verifyResult.status === 'success') {
            window.location.href = verifyResult.redirect_url;
        } else {
            throw new Error(verifyResult.message);
        }

    } catch (err) {
        console.error(err);
        alert('Login failed: ' + err.message);
    }
}
