"""Read document identity without writes; also usable by standalone PDF verification."""
from dataclasses import dataclass

LEGACY_NOTICE = ('Identitas penerbit historis belum tersimpan; '
                 'nama dan alamat ISP saat ini ditampilkan.')


@dataclass(frozen=True)
class DocumentIssuer:
    name: str
    address: str
    historical: bool

    @property
    def notice(self):
        return '' if self.historical else LEGACY_NOTICE


def document_issuer(document):
    snapshot = getattr(document, 'issuer_snapshot', None)
    if snapshot is not None:
        return DocumentIssuer(snapshot.name, snapshot.address, True)
    tenant = getattr(document, 'tenant', None)
    if tenant is None:
        tenant = document.invoice.tenant
    return DocumentIssuer(tenant.name, tenant.address, False)


def capture_invoice_issuer(invoice):
    # Read both fields together from the database, rather than a cached batch/member tenant.
    # Caller owns the invoice transaction. No extra tenant lock after a customer lock.
    from .models import InvoiceIssuerSnapshot, Tenant
    tenant = Tenant.objects.only('name', 'address').get(pk=invoice.tenant_id)
    return InvoiceIssuerSnapshot.objects.create(invoice=invoice, name=tenant.name,
        address=tenant.address, captured_at=invoice.issued_at)


def capture_payment_issuer(payment, tenant):
    # record_payment already holds the fresh tenant row lock throughout this transaction.
    from .models import PaymentIssuerSnapshot
    return PaymentIssuerSnapshot.objects.create(payment=payment, name=tenant.name,
        address=tenant.address, captured_at=payment.created_at)
