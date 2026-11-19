from datetime import date
from django import forms
from django.core.exceptions import ValidationError
from .models import Customer, Fund
from .validation import normalize_phone
from .plan_selection import selectable_plans

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
        self.fields["plan"].queryset = selectable_plans(tenant, self.instance.plan_id if self.instance.pk else None)
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
    note = forms.CharField(label="Catatan (opsional)", max_length=250, required=False,
                          help_text="Tampil pada kuitansi, termasuk di portal pelanggan.")
    fund = forms.ModelChoiceField(label='Dana penerimaan (opsional)',queryset=Fund.objects.none(),required=False,
        empty_label='Belum dipetakan',help_text='Pemetaan kas lokal; tidak memindahkan uang atau menghubungi bank.')

    def __init__(self,*args,tenant=None,**kwargs):
        super().__init__(*args,**kwargs)
        if tenant:self.fields['fund'].queryset=Fund.objects.filter(tenant=tenant,active=True)

class BillingSettingsForm(forms.Form):
    name = forms.CharField(label="Nama ISP", max_length=100, strip=False)
    address = forms.CharField(label="Alamat ISP", max_length=250, strip=False,
        widget=forms.Textarea(attrs={"rows":3}))
    due_day = forms.IntegerField(label="Tanggal jatuh tempo (1–31)", min_value=1, max_value=31)
    tax_bp = forms.IntegerField(label="Pajak dalam basis poin (100 bp = 1%)", min_value=0, max_value=10000)
    invoice_footer = forms.CharField(label="Footer faktur (teks biasa)", max_length=1000, required=False,
        strip=False, widget=forms.Textarea(attrs={"rows":3}))
    token = forms.CharField(max_length=65536, widget=forms.HiddenInput)

    def clean(self):
        from .billing_settings import FIELDS, validate_values
        values = super().clean()
        if not self.errors:
            values.update(validate_values({key:values[key] for key in FIELDS}))
        return values
