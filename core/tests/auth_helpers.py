"""Authenticated MFA sessions for domain tests; authentication itself is tested separately."""
import time
from core.models import MFADevice, Membership
from core.security import encrypt_secret

TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

def verified_login(client, user):
    device, _ = MFADevice.objects.get_or_create(user=user, defaults={"encrypted_secret": encrypt_secret(TEST_TOTP_SECRET)})
    client.force_login(user)
    session = client.session
    session.update({"access_version": Membership.objects.get(user=user).session_version,
                    "mfa_revision": str(device.revision), "mfa_verified_at": time.time(),
                    "started_at": time.time(), "last_seen": time.time()})
    session.save()
