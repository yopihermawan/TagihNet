"""A private snapshot of persisted order state, without provider requests."""
import hashlib
import json


def page_snapshot(order):
    state = [order.status, order.reason, bool(order.payment_id), bool(order.checkout_url),
        order.checkout_in_progress, str(order.updated_at), str(order.last_checked_at), str(order.last_verified_at)]
    return {"revision": hashlib.sha256(json.dumps(state).encode()).hexdigest(),
        "status": order.status, "label": order.get_status_display(), "watch": order.status in {"creating", "pending"}}
