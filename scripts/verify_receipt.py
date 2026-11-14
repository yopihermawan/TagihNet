"""Standalone fictional receipt readback; no database, account, or network access."""
import sys
from copy import deepcopy
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as Row

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pypdf import PdfReader
from core.receipts import receipt_context, render_receipt

output = Path(__file__).resolve().parent.parent/"data/evidence"
output.mkdir(parents=True, exist_ok=True)
created = datetime(2026, 11, 14, 8, 15, tzinfo=timezone.utc)
payment = Row(pk=123, created_at=created, amount=100000, note="Pembayaran sebagian simulasi.",
    recorded_by=Row(username="kasir-fiktif"), reversal=None,
    invoice=Row(number="TN-202611-00123", period=date(2026, 11, 1),
        customer_name="Pelanggan Contoh", customer_address="Jl. Contoh No. 10, Bandung",
        tenant=Row(name="ISP Fiktif", address="Jl. Kantor Contoh No. 1")))
for stress in (False, True):
    current = deepcopy(payment)
    if stress:
        current.invoice.customer_address = "Alamat\n"*35 + "SELESAI-ALAMAT"
        current.invoice.tenant.address = "Kantor\n"*35 + "SELESAI-KANTOR"
        current.note = "Baris\n"*35 + "SELESAI-CATATAN"
        current.reversal = Row(created_at=created, recorded_by=Row(username="owner-fiktif"),
                               reason="Alasan\n"*30 + "SELESAI-KOREKSI")
    content = render_receipt(receipt_context(current, created))
    pdf = PdfReader(BytesIO(content))
    text = "\n".join(page.extract_text() for page in pdf.pages)
    for marker in ("TN-KW-20261114-000123", "Rp100.000", "seratus ribu rupiah", "14/11/2026 15:15:00 WIB", "SIMULASI"):
        assert marker in text, marker
    assert len(pdf.pages) >= (3 if stress else 1)
    if not stress:
        assert len(pdf.pages) == 1
    else:
        for marker in ("SELESAI-ALAMAT", "SELESAI-KANTOR", "SELESAI-CATATAN", "SELESAI-KOREKSI", "DIREVERSAL"):
            assert marker in text, marker
    for index, page in enumerate(pdf.pages, 1):
        assert abs(float(page.mediabox.width)-595.276)<1 and abs(float(page.mediabox.height)-841.89)<1
        assert f"Halaman {index}" in page.extract_text() and "SIMULASI" in page.extract_text()
        if stress:
            assert "DIREVERSAL" in page.extract_text()
        assert not page.get("/Annots")
    filename = "receipt-reversed-multipage.pdf" if stress else "receipt-payment.pdf"
    (output/filename).write_bytes(content)
    print(f"PASS: {filename}: A4, {len(pdf.pages)} pages, transaction/words/WIB/simulation/footer/text readback; no database accessed.")
