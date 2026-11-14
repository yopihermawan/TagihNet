from datetime import date
from django import forms
from django.core.exceptions import ValidationError
from .models import Customer, Tenant
from .validation import normalize_phone

class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "email", "phone", "username", "plan", "nas", "area", "address"]
        labels = {"name": "Nama pelanggan", "email": "Email", "phone": "Telepon", "username": "Username layanan",
                  "plan": "Paket", "nas": "NAS simulasi (opsional)", "area": "Wilayah", "address": "Alamat"}
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.tenant = tenant
        self.fields["plan"].queryset = tenant.plan_set.all()
        self.fields["nas"].queryset = tenant.monitoringdevice_set.all()
        self.fields["nas"].empty_label = "Belum dipetakan"
        self.fields["nas"].help_text = "Pemetaan administratif; tidak menghubungkan sesi PPPoE atau mengubah router."

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])

class InvoiceForm(forms.Form):
    customer = forms.ModelChoiceField(queryset=Customer.objects.none(), label="Pelanggan")
    period = forms.DateField(label="Bulan layanan", input_formats=["%Y-%m"], widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))
    discount = forms.IntegerField(label="Diskon (rupiah)", min_value=0, max_value=100000000, initial=0)

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = tenant.customer_set.all()

class PaymentForm(forms.Form):
    amount = forms.IntegerField(label="Nominal pembayaran (rupiah)", min_value=1, max_value=200000000)
    event_key = forms.UUIDField(widget=forms.HiddenInput)
    note = forms.CharField(label="Catatan (opsional)", max_length=250, required=False)

class BillingSettingsForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = ["name", "address", "due_day", "tax_bp", "invoice_footer"]
        labels = {"name": "Nama ISP", "address": "Alamat ISP", "due_day": "Tanggal jatuh tempo (1–31)",
                  "tax_bp": "Pajak dalam basis poin (100 bp = 1%)", "invoice_footer": "Footer faktur (teks biasa)"}
        widgets = {"invoice_footer": forms.Textarea(attrs={"rows": 3})}
