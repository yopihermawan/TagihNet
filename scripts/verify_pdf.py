"""Generate a synthetic multi-page invoice and verify text, totals, and encoding."""
import os
import sys
from datetime import date
from io import BytesIO
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.db import transaction
from pypdf import PdfReader
from core.models import Customer, Invoice, InvoiceLine, Plan, Tenant
from core.pdf import render_invoice

with transaction.atomic():
    tenant = Tenant.objects.create(name="ISP Fiktif PDF")
    plan = Plan.objects.create(tenant=tenant, name="Uji PDF", speed_mbps=10, price=1000)
    customer = Customer.objects.create(tenant=tenant, plan=plan, code="PDF", name="Pelanggan Fiktif Multipage",
        phone="081234567890", username="pdf-test", address="Alamat fiktif", area="Contoh")
    invoice = Invoice.objects.create(tenant=tenant, customer=customer, period=date(2026, 10, 1),
        due_date=date(2026, 10, 31), customer_name=customer.name, customer_address=customer.address,
        subtotal=100000, total=100000, footer='<script src="https://example.invalid/no-fetch">Penanda teks aman</script> ' + 'Terima kasih. ' * 35)
    InvoiceLine.objects.bulk_create([InvoiceLine(invoice=invoice, description=f"Layanan fiktif baris {index:03d} — " + "teks panjang " * 10,
        unit_price=1000) for index in range(100)])
    content = render_invoice(invoice, True)
    pdf = PdfReader(BytesIO(content))
    text = "\n".join(page.extract_text() for page in pdf.pages)
    assert len(pdf.pages) >= 3
    assert "Rp100.000" in text and "Penanda teks aman" in text and "SIMULASI" in text
    assert "baris 000" in text and "baris 099" in text
    for page in pdf.pages:
        assert abs(float(page.mediabox.width) - 595.276) < 1
        assert abs(float(page.mediabox.height) - 841.89) < 1
    output = Path("data/evidence/invoice-multipage.pdf")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    transaction.set_rollback(True)
print(f"PASS: A4, {len(pdf.pages)} pages, 100 lines, long footer rendered as text, total Rp100.000, simulation watermark; fixture rolled back.")
