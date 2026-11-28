"""Verify the password reset flow against a disposable authenticated STARTTLS server."""
import base64
import hmac
import io
import json
import logging
import os
import re
import secrets
import socketserver
import ssl
import sys
import tempfile
import traceback
from contextlib import ExitStack
from email import policy
from email.parser import BytesParser
from pathlib import Path
from threading import Thread

import pyotp

from staging_lab import ROOT, prepare

sys.path.insert(0, str(ROOT))


class ResetSMTPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.connections += 1
        connection = self.request
        connection.settimeout(8)
        try:
            if self.server.stall_greeting:
                connection.recv(1)
                return
            with ExitStack() as stack:
                stream = stack.enter_context(connection.makefile("rb"))
                connection.sendall(b"220 localhost SMTP fixture\r\n")
                encrypted = authenticated = False
                for _ in range(16):
                    line = stream.readline(4097)
                    if not line or len(line) > 4096:
                        return
                    command, _, argument = line.rstrip(b"\r\n").partition(b" ")
                    command = command.upper()
                    if command == b"EHLO":
                        reply = b"250-localhost\r\n250 AUTH PLAIN" if encrypted else b"250-localhost\r\n250 STARTTLS"
                    elif command == b"STARTTLS" and not encrypted:
                        connection.sendall(b"220 Begin TLS\r\n")
                        connection = stack.enter_context(self.server.context.wrap_socket(connection, server_side=True))
                        stream = stack.enter_context(connection.makefile("rb"))
                        encrypted = True
                        continue
                    elif command == b"AUTH" and encrypted:
                        authenticated = hmac.compare_digest(argument, self.server.auth_proof)
                        reply = b"235 Authenticated" if authenticated else b"535 Authentication failed"
                    elif command == b"MAIL" and authenticated:
                        reply = b"250 Sender accepted"
                    elif command == b"RCPT" and authenticated:
                        reply = b"550 Fixture provider rejection" if self.server.reject_recipient else b"250 Recipient accepted"
                    elif command == b"DATA" and authenticated:
                        connection.sendall(b"354 End data with <CRLF>.<CRLF>\r\n")
                        message = bytearray()
                        while len(message) <= 65536:
                            row = stream.readline(4097)
                            if not row or len(row) > 4096:
                                return
                            if row == b".\r\n":
                                self.server.messages.append(bytes(message))
                                break
                            message.extend(row[1:] if row.startswith(b"..") else row)
                        else:
                            return
                        reply = b"250 Message accepted"
                    elif command == b"RSET":
                        reply = b"250 Reset"
                    elif command == b"QUIT":
                        connection.sendall(b"221 Goodbye\r\n")
                        return
                    else:
                        reply = b"530 STARTTLS and authentication required"
                    connection.sendall(reply + b"\r\n")
        except OSError:
            # Trust verification and timeout cases close the socket before SMTP DATA.
            return


def exercise(server, ca, directory):
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.mail import get_connection
    from django.core.management import call_command
    from django.test import Client

    from core.models import MFADevice, Membership, Tenant
    from core.security import encrypt_secret, new_recovery_codes

    assert not settings.DEBUG and settings.SIMULATION
    assert settings.DATABASES["default"]["NAME"].parent == directory
    assert settings.EMAIL_HOST == "localhost" and settings.EMAIL_PORT == server.server_address[1]
    assert settings.EMAIL_USE_TLS and get_connection().timeout == 5
    call_command("migrate", verbosity=0, interactive=False)
    password, replacement = (secrets.token_urlsafe(36) for _ in range(2))
    users = get_user_model()
    owner = users.objects.create_user("owner-reset-fixture", email="reset@example.invalid", password=password)
    tenant = Tenant.objects.create(name="ISP reset fiktif")
    Membership.objects.create(user=owner, tenant=tenant, role="owner")
    codes, digests = new_recovery_codes()
    device = MFADevice.objects.create(user=owner, encrypted_secret=encrypt_secret(pyotp.random_base32()),
                                      recovery_hashes=digests)

    def browser():
        return Client(enforce_csrf_checks=True, headers={"Host": "localhost"})

    def submit(client, path, fields):
        assert client.get(path, secure=True).status_code == 200
        csrf = client.cookies["csrftoken"].value
        return client.post(path, {**fields, "csrfmiddlewaretoken": csrf}, secure=True,
                           headers={"Origin": "https://localhost"})

    def login(client, current, code):
        identify = submit(client, "/login/", {"username": owner.username, "password": current})
        assert (identify.status_code, identify.get("Location")) == (302, "/mfa/verify/")
        verify = submit(client, "/mfa/verify/", {"code": code})
        assert (verify.status_code, verify.get("Location")) == (302, "/")

    def request_reset(address=owner.email):
        response = submit(browser(), "/reset/", {"email": address})
        assert (response.status_code, response.get("Location")) == (302, "/reset/sent/")

    retained_session = browser()
    login(retained_session, password, codes[0])
    assert retained_session.get("/", secure=True).status_code == 200

    request_reset("absent@example.invalid")
    assert server.connections == 0

    logs = io.StringIO()
    logger = logging.getLogger("core.forms")
    logger.addHandler(logging.StreamHandler(logs))
    logger.propagate = False

    # The private CA is generated for this process and absent from the system trust store.
    request_reset()
    assert server.connections == 1 and not server.messages
    os.environ["SSL_CERT_FILE"] = str(ca)
    server.reject_recipient = True
    request_reset()
    assert server.connections == 2 and not server.messages
    server.reject_recipient = False
    server.stall_greeting = True
    request_reset()
    assert server.connections == 3 and not server.messages
    server.stall_greeting = False
    assert logs.getvalue().splitlines() == ["Pengiriman email reset gagal."] * 3

    request_reset()
    assert server.connections == 4 and len(server.messages) == 1
    message = BytesParser(policy=policy.default).parsebytes(server.messages[0])
    assert str(message["To"]) == owner.email
    assert str(message["From"]) == "TagihNet <noreply@example.invalid>"
    assert str(message["Subject"]) == "Pemulihan akun TagihNet"
    body = message.get_body(preferencelist=("plain",)).get_content()
    links = re.findall(r"^https://localhost(/reset/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/)$", "\n".join(body.splitlines()), re.M)
    assert len(links) == 1
    recovery = browser()
    response = recovery.get(links[0], secure=True)
    assert response.status_code == 302 and response["Location"].endswith("/set-password/")
    changed = submit(recovery, response["Location"], {"new_password1": replacement, "new_password2": replacement})
    assert (changed.status_code, changed.get("Location")) == (302, "/reset/complete/")
    replay = browser().get(links[0], secure=True)
    assert replay.status_code == 200 and b"Tautan tidak berlaku" in replay.content
    assert retained_session.get("/", secure=True).status_code == 302
    device.refresh_from_db()
    assert tuple(device.recovery_hashes) == tuple(digests[1:])
    login(browser(), replacement, codes[1])

    return {
        "scope": "isolated-loopback-starttls", "status": "passed", "delivered_messages": 1,
        "unknown_address_suppressed": True, "untrusted_certificate_rejected": True,
        "smtp_rejection_and_timeout_handled": True, "secret_free_failure_log": True,
        "single_use_link": True, "prior_session_revoked": True, "mfa_retained": True,
    }


def main():
    with tempfile.TemporaryDirectory(prefix="tagihnet-reset-smtp-") as folder:
        directory = Path(folder)
        prepare(directory / "certs")
        private = directory / "certs" / "secrets"
        password_file = private / "runtime-password"
        smtp_user = "reset-fixture"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(private / "https.crt", private / "https.key")
        with socketserver.TCPServer(("127.0.0.1", 0), ResetSMTPHandler) as server:
            server.context = context
            server.auth_proof = b"PLAIN " + base64.b64encode(f"\0{smtp_user}\0{password_file.read_text()}".encode())
            server.connections = 0
            server.messages = []
            server.reject_recipient = server.stall_greeting = False
            for key in tuple(os.environ):
                if key.startswith("TAGIHNET_") or key in {"DJANGO_SETTINGS_MODULE", "SSL_CERT_FILE", "SSL_CERT_DIR"}:
                    del os.environ[key]
            os.environ.update(
                DJANGO_SETTINGS_MODULE="config.settings", TAGIHNET_DEBUG="0", TAGIHNET_SIMULATION="1",
                TAGIHNET_DATA_DIR=str(directory), TAGIHNET_DB_ENGINE="sqlite", TAGIHNET_ALLOWED_HOSTS="localhost",
                TAGIHNET_SECRET_KEY_FILE=str(private / "session-key"), TAGIHNET_MFA_KEY_FILE=str(private / "mfa-key"),
                TAGIHNET_EMAIL_HOST="localhost", TAGIHNET_EMAIL_PORT=str(server.server_address[1]),
                TAGIHNET_EMAIL_USER=smtp_user, TAGIHNET_EMAIL_PASSWORD_FILE=str(password_file),
                TAGIHNET_FROM_EMAIL="TagihNet <noreply@example.invalid>",
            )
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                return exercise(server, private / "ca.crt", directory)
            finally:
                server.shutdown()
                worker.join(timeout=10)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except Exception as error:
        frames = [frame for frame in traceback.extract_tb(error.__traceback__) if frame.filename == __file__]
        line = frames[-1].lineno if frames else "unknown"
        raise SystemExit(f"Reset SMTP check failed ({type(error).__name__}) at line {line}.") from None
