"""Read-only payment evidence; no receipt issuance or ledger mutation."""
from html import escape
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from .pdf import rupiah

WIB = ZoneInfo("Asia/Jakarta")
SIMULATION_NOTICE = "SIMULASI - BUKAN BUKTI PEMBAYARAN NYATA"
SOURCE_NOTICE = ("Nama dan alamat pelanggan mengikuti snapshot faktur. Identitas ISP dan nama akun pencatat "
                 "mengikuti data saat dicetak. Status koreksi diperiksa saat dicetak. "
                 "Kuitansi satu transaksi ini tidak menyatakan faktur telah lunas.")

# Fonts ship with the pinned ReportLab package; no user path, download or system font dependency.
for name, filename in (("Receipt", "Vera.ttf"), ("ReceiptBold", "VeraBd.ttf"),
                       ("ReceiptItalic", "VeraIt.ttf"), ("ReceiptBoldItalic", "VeraBI.ttf")):
    pdfmetrics.registerFont(TTFont(name, str(Path(reportlab.__file__).parent/"fonts"/filename)))
pdfmetrics.registerFontFamily("Receipt", normal="Receipt", bold="ReceiptBold",
                              italic="ReceiptItalic", boldItalic="ReceiptBoldItalic")


def terbilang(amount):
    """Exact integer rupiah, covering the database's positive signed BIGINT range."""
    if type(amount) is not int or not 1 <= amount <= 9223372036854775807:
        raise ValueError("Nominal harus bilangan bulat positif dalam rentang BIGINT.")
    units = ("", "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan", "sembilan", "sepuluh", "sebelas")

    def spell(number):
        if number < 12:
            return units[number]
        if number < 20:
            return spell(number-10) + " belas"
        if number < 100:
            return spell(number//10) + " puluh " + spell(number % 10)
        if number < 200:
            return "seratus " + spell(number-100)
        if number < 1000:
            return spell(number//100) + " ratus " + spell(number % 100)
        if number < 2000:
            return "seribu " + spell(number-1000)
        for power, name in ((18, "kuintiliun"), (15, "kuadriliun"), (12, "triliun"),
                            (9, "miliar"), (6, "juta"), (3, "ribu")):
            factor = 10**power
            if number >= factor:
                return spell(number//factor) + " " + name + " " + spell(number % factor)

    return " ".join(spell(amount).split()) + " rupiah"


def receipt_context(payment, printed_at):
    created = payment.created_at.astimezone(WIB)
    return {"payment": payment, "invoice": payment.invoice,
            "receipt_number": f"TN-KW-{created:%Y%m%d}-{payment.pk:06d}",
            "amount_words": terbilang(payment.amount), "printed_at": printed_at,
            "simulation_notice": SIMULATION_NOTICE, "source_notice": SOURCE_NOTICE}


def render_receipt(context):
    """Draw only escaped local text; never interpret user input as PDF markup or assets."""
    payment, invoice = context["payment"], context["invoice"]
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=20*mm, title=context["receipt_number"], author="TagihNet")
    styles = getSampleStyleSheet()
    for name in ("Normal", "Title", "Heading1", "Heading2", "Heading3"):
        style = styles[name]
        style.wordWrap = "CJK"
        style.fontName = "ReceiptBold" if "Bold" in style.fontName else "Receipt"
    styles["Normal"].leading = 15
    styles["Heading3"].textColor = colors.HexColor("#13796b")

    def text(value, style="Normal"):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), styles[style])

    def stamp(value):
        return value.astimezone(WIB).strftime("%d/%m/%Y %H:%M:%S WIB")

    reversal = getattr(payment, "reversal", None)
    flow = [text("TagihNet", "Title"), text(context["simulation_notice"], "Heading3"),
        text("KUITANSI PEMBAYARAN", "Heading1"), text(context["receipt_number"]), Spacer(1, 4*mm),
        text("DIREVERSAL - TRANSAKSI TIDAK BERLAKU" if reversal else "TERCATAT - SIMULASI", "Heading2"),
        text(invoice.tenant.name, "Heading2"), text(invoice.tenant.address), Spacer(1, 3*mm),
        text("Diterima dari: " + invoice.customer_name), text(invoice.customer_address),
        text("Nominal transaksi: " + rupiah(payment.amount), "Heading2"),
        text("Terbilang: " + context["amount_words"]), Spacer(1, 3*mm),
        text("Untuk faktur: " + invoice.number), text(f"Periode layanan: {invoice.period:%m/%Y}"),
        text("Dibayar: " + stamp(payment.created_at)), text("Dicatat oleh: " + payment.recorded_by.username),
        text("Catatan: " + (payment.note or "Tanpa catatan")), Spacer(1, 4*mm)]
    if reversal:
        flow.extend([text("Rincian koreksi", "Heading2"),
            text("Dikoreksi: " + stamp(reversal.created_at)),
            text("Pencatat koreksi: " + reversal.recorded_by.username), text("Alasan: " + reversal.reason)])
    flow.extend([Spacer(1, 4*mm), text("Dicetak: " + stamp(context["printed_at"])),
                 text(context["source_notice"], "Normal")])

    def footer(canvas, doc):
        canvas.setFont("Receipt", 8)
        canvas.drawString(20*mm, 12*mm, f"TagihNet | {context['receipt_number']} | Halaman {doc.page}")
        canvas.drawRightString(190*mm, 12*mm, "SIMULASI | DIREVERSAL" if reversal else "SIMULASI")

    document.build(flow, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
