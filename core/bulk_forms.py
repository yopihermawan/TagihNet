from django import forms
from .models import Customer, Plan


class BatchPreviewForm(forms.Form):
    period = forms.DateField(label="Bulan layanan", input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))
    state = forms.ChoiceField(label="Status layanan", choices=[("active", "Aktif"), ("isolated", "Diisolir"), ("all", "Semua layanan")])
    plan = forms.ModelChoiceField(label="Paket (opsional)", queryset=Plan.objects.none(), required=False)
    area = forms.ChoiceField(label="Wilayah (opsional)", required=False)
    discount = forms.IntegerField(label="Diskon per faktur baru (rupiah)", min_value=0, max_value=100000000, initial=0)

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = Plan.objects.filter(tenant=tenant)
        areas = Customer.objects.filter(tenant=tenant).values_list("area", flat=True).distinct().order_by("area")
        self.fields["area"].choices = [("", "Semua wilayah"), *((area, area) for area in areas)]


class BatchConfirmationForm(forms.Form):
    reviewed = forms.BooleanField(label="Saya telah meninjau pelanggan, periode, diskon, pajak, jatuh tempo, dan total faktur baru.")
