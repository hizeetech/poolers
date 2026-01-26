import re
from django.core.management.utils import get_random_secret_key

# Path to your .env file
ENV_FILE = ".env"

# Generate new secret key
new_key = get_random_secret_key()
print(f"✅ New SECRET_KEY generated:\n{new_key}\n")

# Read .env content
with open(ENV_FILE, "r") as f:
    content = f.read()

# Replace existing SECRET_KEY line or append if not found
if "SECRET_KEY" in content:
    content = re.sub(r"^SECRET_KEY=.*", f"SECRET_KEY={new_key}", content, flags=re.MULTILINE)
else:
    content += f"\nSECRET_KEY={new_key}\n"

# Write back updated .env
with open(ENV_FILE, "w") as f:
    f.write(content)

print("🔄 .env updated with new SECRET_KEY.")
