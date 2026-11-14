"""Create the first ISP/owner in an empty database; never reseed or reset an account."""
from pathlib import Path
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import connection, transaction
from core.billing import audit, retry_locked
from core.models import Membership, Tenant


@retry_locked
@transaction.atomic
def bootstrap(name, username, email, password):
    # Bootstrap has no tenant row to lock yet. Serialize the initial PostgreSQL operation.
    if connection.vendor=="postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(7211687232481)")
    User = get_user_model()
    if Tenant.objects.exists() or User.objects.exists():
        raise CommandError("Bootstrap hanya untuk database kosong; gunakan pengelolaan akun yang sudah ada.")
    user = User(username=username,email=email)
    user.full_clean(exclude=["password"])
    validate_password(password,user)
    user.set_password(password)
    user.save()
    tenant = Tenant.objects.create(name=name)
    Membership.objects.create(tenant=tenant,user=user,role="owner")
    audit(tenant,user,"access.bootstrapped",user.pk,"ISP dan owner awal dibuat; MFA wajib pada login pertama.")
    return tenant


class Command(BaseCommand):
    help = "Buat ISP/owner awal pada database kosong memakai file kata sandi privat, tanpa akun demo."

    def add_arguments(self,parser):
        parser.add_argument("--isp-name",required=True)
        parser.add_argument("--username",required=True)
        parser.add_argument("--email",required=True)
        parser.add_argument("--password-file",type=Path,required=True)

    def handle(self,*args,**options):
        name=options["isp_name"].strip()
        if not 1<=len(name)<=100 or any(ord(char)<32 for char in name):
            raise CommandError("Nama ISP harus 1–100 karakter tanpa karakter kontrol.")
        try:
            validate_email(options["email"])
            with options["password_file"].open("rb") as stream:
                content=stream.read(4097)
            if len(content)>4096: raise ValueError
            password=content.decode("utf-8").rstrip("\r\n")
        except (OSError,UnicodeError,ValueError,ValidationError):
            raise CommandError("Email atau file kata sandi bootstrap tidak valid.") from None
        try:
            bootstrap(name,options["username"],options["email"],password)
        except ValidationError:
            raise CommandError("Identitas akun atau kekuatan kata sandi tidak memenuhi aturan.") from None
        self.stdout.write("ISP dan owner awal tersimpan. Daftarkan MFA pada login pertama; tidak ada akun demo dibuat.")
