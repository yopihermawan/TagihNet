from django import forms
from .models import Customer, Installation, Membership, Plan, Tenant, Ticket
from .validation import normalize_phone


def technicians(tenant):
    return Membership.objects.filter(tenant=tenant, role="technician", user__is_active=True).select_related("user")


class TechnicianChoice(forms.ModelChoiceField):
    def label_from_instance(self, member):
        return member.user.get_full_name() or member.user.username


class RequestForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class VersionForm(RequestForm):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class TicketCreationForm(RequestForm):
    customer = forms.ModelChoiceField(label="Pelanggan", queryset=Customer.objects.none())
    title = forms.CharField(label="Judul gangguan", max_length=140)
    description = forms.CharField(label="Penjelasan gangguan", max_length=4000, widget=forms.Textarea(attrs={"rows": 5}))
    category = forms.ChoiceField(label="Kategori", choices=Ticket.CATEGORIES)
    priority = forms.ChoiceField(label="Prioritas", choices=Ticket.PRIORITIES)
    assigned_to = TechnicianChoice(label="Teknisi (opsional)", queryset=Membership.objects.none(), required=False)

    def __init__(self, *args, member, **kwargs):
        super().__init__(*args, **kwargs)
        if member.role == "customer":
            for name in ["customer", "priority", "assigned_to"]:
                del self.fields[name]
        else:
            self.fields["customer"].queryset = Customer.objects.filter(tenant=member.tenant)
            self.fields["assigned_to"].queryset = technicians(member.tenant)


class TicketManageForm(VersionForm):
    priority = forms.ChoiceField(label="Prioritas", choices=Ticket.PRIORITIES)
    category = forms.ChoiceField(label="Kategori", choices=Ticket.CATEGORIES)
    assigned_to = TechnicianChoice(label="Teknisi (opsional)", queryset=Membership.objects.none(), required=False)

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = technicians(tenant)


class TicketTransitionForm(VersionForm):
    status = forms.ChoiceField(label="Status berikutnya", choices=Ticket.STATES)
    note = forms.CharField(label="Catatan penanganan (terlihat pelanggan, minimal 10 karakter)", min_length=10, max_length=2000,
                           widget=forms.Textarea(attrs={"rows": 3}))


class CommentForm(RequestForm):
    body = forms.CharField(label="Komentar", max_length=4000, widget=forms.Textarea(attrs={"rows": 3}))
    internal = forms.BooleanField(label="Catatan internal (tidak terlihat pelanggan)", required=False)

    def __init__(self, *args, customer=False, **kwargs):
        super().__init__(*args, **kwargs)
        if customer:
            del self.fields["internal"]


class InstallationCreationForm(RequestForm):
    name = forms.CharField(label="Nama calon pelanggan", max_length=100)
    email = forms.EmailField(label="Email (opsional)", required=False)
    phone = forms.CharField(label="Telepon", max_length=20)
    username = forms.SlugField(label="Username layanan", max_length=64)
    address = forms.CharField(label="Alamat pemasangan", max_length=250, widget=forms.Textarea(attrs={"rows": 3}))
    area = forms.CharField(label="Wilayah", max_length=80)
    plan = forms.ModelChoiceField(label="Paket", queryset=Plan.objects.none())
    assigned_to = TechnicianChoice(label="Teknisi (opsional saat prospek)", queryset=Membership.objects.none(), required=False)

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = Plan.objects.filter(tenant=tenant)
        self.fields["assigned_to"].queryset = technicians(tenant)

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])


class InstallationAssignmentForm(VersionForm):
    assigned_to = TechnicianChoice(label="Teknisi", queryset=Membership.objects.none(), required=False)

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = technicians(tenant)


class InstallationEditForm(InstallationCreationForm):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = forms.CharField(label="Alasan perubahan (minimal 10 karakter)", min_length=10, max_length=2000,
                             widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields["assigned_to"]


class InstallationRescheduleForm(VersionForm):
    scheduled_for = forms.DateTimeField(label="Jadwal baru (WIB)", input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}))
    reason = forms.CharField(label="Alasan penjadwalan ulang (minimal 10 karakter)", min_length=10, max_length=2000,
                             widget=forms.Textarea(attrs={"rows": 3}))


class InstallationTransitionForm(VersionForm):
    status = forms.ChoiceField(label="Tahap berikutnya", choices=[choice for choice in Installation.STATES if choice[0] != "activated"])
    note = forms.CharField(label="Catatan / alasan (minimal 10 karakter)", min_length=10, max_length=2000,
                          widget=forms.Textarea(attrs={"rows": 3}))
    scheduled_for = forms.DateTimeField(label="Jadwal pemasangan (WIB, wajib saat Terjadwal)", required=False,
        input_formats=["%Y-%m-%dT%H:%M"], widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}))


class ActivationForm(forms.Form):
    period = forms.DateField(label="Bulan layanan awal", input_formats=["%Y-%m"],
                            widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))


class ActivationConfirmationForm(forms.Form):
    quote = forms.CharField(widget=forms.HiddenInput, max_length=2000)
    reviewed = forms.BooleanField(label="Saya telah meninjau identitas pelanggan, paket, periode, pajak, total, dan jatuh tempo faktur awal.")


class SLASettingsForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = ["sla_low_hours", "sla_normal_hours", "sla_high_hours", "sla_urgent_hours"]
        labels = {"sla_low_hours": "SLA rendah (jam)", "sla_normal_hours": "SLA normal (jam)",
                  "sla_high_hours": "SLA tinggi (jam)", "sla_urgent_hours": "SLA mendesak (jam)"}
