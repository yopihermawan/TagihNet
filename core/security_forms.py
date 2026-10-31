from django import forms
from django.contrib.auth import get_user_model, password_validation
from .models import AccessRole, Customer, Membership
from .permissions import CAPABILITY_LABELS


class FactorForm(forms.Form):
    code = forms.CharField(label="Kode authenticator atau kode pemulihan", max_length=40,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "autocapitalize": "off", "spellcheck": "false"}))


class EnrollmentForm(forms.Form):
    code = forms.RegexField(regex=r"^[0-9]{6}$", label="Kode authenticator (6 digit)",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "maxlength": 6}))


class ReauthenticationForm(FactorForm):
    password = forms.CharField(label="Kata sandi saat ini", widget=forms.PasswordInput, max_length=256)
    field_order = ["password", "code"]


class PasswordProofForm(forms.Form):
    password = forms.CharField(label="Kata sandi saat ini", widget=forms.PasswordInput, max_length=256)


class RoleForm(forms.Form):
    name = forms.CharField(label="Nama peran", max_length=80)
    capabilities = forms.MultipleChoiceField(label="Izin", choices=list(CAPABILITY_LABELS.items()),
        widget=forms.CheckboxSelectMultiple, required=False)


class MemberForm(forms.Form):
    role = forms.ChoiceField(label="Preset akun", choices=Membership.ROLES)
    access_role = forms.ModelChoiceField(label="Peran kustom (opsional)", queryset=AccessRole.objects.none(), required=False)
    customer = forms.ModelChoiceField(label="Pelanggan portal (khusus akun pelanggan)", queryset=Customer.objects.none(), required=False)
    active = forms.BooleanField(label="Akun aktif", required=False, initial=True)

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["access_role"].queryset = AccessRole.objects.filter(tenant=tenant)
        self.fields["customer"].queryset = Customer.objects.filter(tenant=tenant)


class NewMemberForm(MemberForm):
    username = forms.CharField(label="Username", max_length=150, validators=[get_user_model().username_validator])
    email = forms.EmailField(label="Email pemulihan")
    password = forms.CharField(label="Kata sandi awal (minimal 12 karakter)", widget=forms.PasswordInput, max_length=256)
    field_order = ["username", "email", "password", "role", "access_role", "customer"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields["active"]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password"):
            user = get_user_model()(username=cleaned.get("username", ""), email=cleaned.get("email", ""))
            try:
                password_validation.validate_password(cleaned["password"], user)
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned
