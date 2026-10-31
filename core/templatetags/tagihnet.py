from django import template
from django.utils.html import format_html
register = template.Library()

@register.filter
def idr(value):
    return "Rp" + f"{int(value or 0):,}".replace(",", ".")

@register.simple_tag(takes_context=True)
def querystring(context, **kwargs):
    query = context["request"].GET.copy()
    for key, value in kwargs.items():
        query[key] = str(value)
    return "?" + query.urlencode()

@register.simple_tag
def icon(name):
    paths = {
        "dashboard": "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
        "customers": "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M16 4a4 4 0 0 1 0 8 M22 21v-2a4 4 0 0 0-3-3.87 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
        "invoice": "M6 3h12v18l-3-2-3 2-3-2-3 2z M9 7h6 M9 11h6 M9 15h4",
        "network": "M8 3h8v6H8z M3 16h6v5H3z M15 16h6v5h-6z M12 9v4 M6 16v-3h12v3",
        "settings": "M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8 M12 2v3 M12 19v3 M2 12h3 M19 12h3 M5 5l2 2 M17 17l2 2 M5 19l2-2 M17 7l2-2",
        "clock": "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M12 7v5l3 2",
        "arrow": "M7 17 17 7 M7 7h10v10",
        "right": "M4 12h16 M14 6l6 6-6 6",
        "logout": "M9 3H4v18h5 M12 12h9 M16 7l5 5-5 5",
        "received": "M17 5 7 15 M7 5v10h10",
        "home": "M3 11 12 3l9 8 M5 10v11h14V10 M9 21v-7h6v7",
    }
    return format_html('<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="{}"/></svg>', paths.get(name, paths["arrow"]))
