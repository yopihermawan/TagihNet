from html import escape
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

def rupiah(value):
    return "Rp" + f"{value:,}".replace(",", ".")

def render_invoice(invoice, simulation):
    # ReportLab draws local text. No HTML renderer, file lookup or URL fetch.
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm,
                                topMargin=18*mm, bottomMargin=20*mm, title=invoice.number, author="TagihNet")
    styles = getSampleStyleSheet()
    styles["Normal"].leading = 15
    styles["Normal"].wordWrap = "CJK"
    def text(value, style="Normal"):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), styles[style])
    flow = [text("TagihNet", "Title"), text("Billing rapi, jaringan terkendali."), Spacer(1, 6*mm)]
    if simulation:
        flow.extend([text("SIMULASI — BUKAN BUKTI PEMBAYARAN NYATA", "Heading3"), Spacer(1, 3*mm)])
    flow.extend([text(invoice.tenant.name, "Heading2"), text(invoice.tenant.address),
                 text(f"FAKTUR {invoice.number}", "Heading2"), text(f"Status: {invoice.status_label}"),
                 text(f"Pelanggan: {invoice.customer_name} ({invoice.customer.code})"), text(invoice.customer_address),
                 text(f"Periode layanan: {invoice.period:%m/%Y} | Jatuh tempo: {invoice.due_date:%d/%m/%Y}"), Spacer(1, 6*mm)])
    rows = [[text("Rincian layanan"), text("Qty"), text("Harga"), text("Jumlah")]]
    for line in invoice.lines.order_by("id"):
        rows.append([text(line.description), text(line.quantity), text(rupiah(line.unit_price)), text(rupiah(line.total))])
    table = Table(rows, colWidths=[85*mm, 12*mm, 30*mm, 34*mm], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
        ("LINEBELOW", (0, 0), (-1, -1), .4, colors.HexColor("#E2E8F0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    flow.append(table)
    summary = [Spacer(1, 5*mm), text(f"Subtotal: {rupiah(invoice.subtotal)}"),
               text(f"Diskon: {rupiah(invoice.discount)} | Pajak ({invoice.tax_bp / 100:g}%): {rupiah(invoice.tax_amount)}"),
               text(f"Total: {rupiah(invoice.total)}", "Heading2"),
               text(f"Pembayaran sah: {rupiah(invoice.paid)} | Sisa: {rupiah(invoice.balance)}"), Spacer(1, 4*mm)]
    flow.append(KeepTogether(summary))
    flow.append(text(invoice.footer))
    def page_footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.drawString(18*mm, 12*mm, f"TagihNet | {invoice.number} | Halaman {doc.page}")
    document.build(flow, onFirstPage=page_footer, onLaterPages=page_footer)
    return buffer.getvalue()
