"""Task groups expose the same server capabilities as the existing destinations."""
from django.urls import reverse

# key, label, icon, route, any required capability, active path prefixes
GROUPS = (
    ('overview','Ringkasan', (
        ('dashboard','Ringkasan','dashboard','dashboard',('dashboard',),('/#exact',)),)),
    ('customers','Pelanggan & layanan', (
        ('customers','Pelanggan & layanan','customers','customers',('customers',),('/customers/',)),
        ('plans','Paket layanan','network','plan_list',('customers',),('/plans/',)),
        ('imports','Impor pelanggan','customers','customer_import_list',('import_customers',),('/customers/imports/',)),
        ('portal','Layanan saya','home','portal',('portal',),('/portal/',)),)),
    ('billing','Penagihan', (
        ('invoices','Penagihan','invoice','invoices',('invoices',),('/invoices/',)),
        ('schedule','Jadwal penagihan','clock','billing_schedule',('manage_billing_schedule',),('/invoices/schedule/',)),
        ('reports','Laporan penagihan','invoice','billing_report',('billing_reports',),('/reports/billing/',)),
        ('reminders','Pengingat simulasi','clock','reminder_list',('reminders',),('/operations/reminders/',)),)),
    ('network','Jaringan', (
        ('network','Jaringan','network','network',('network',),('/network/#exact',)),
        ('monitoring','Monitoring simulasi','network','monitoring_list',('network',),('/network/monitoring/',)),
        ('hotspot','Hotspot simulasi','network','business_hotspot',('hotspot',),('/business/hotspot/',)),
        ('topology','Topologi ODC/ODP','network','business_topology',('topology',),('/business/topology/',)),)),
    ('operations','Operasional', (
        ('tickets','Tiket gangguan','invoice','ticket_list',('tickets','field_work','portal'),('/operations/tickets/',)),
        ('installations','Pasang baru (PSB)','network','installation_list',('installations','field_work'),('/operations/installations/',)),)),
    ('finance','Keuangan & tim', (
        ('cash','Kas & dana','invoice','business_cash',('cash',),('/business/cash/',)),
        ('team','Tim','customers','business_team',('team',),('/business/team/',)),
        ('payroll','Payroll','invoice','business_payroll',('payroll',),('/business/payroll/',)),
        ('payslips','Slip gaji saya','invoice','own_payslips',(),('/account/payslips/',)),)),
    ('marketing','Pemasaran', (
        ('sales','Prospek & komisi','customers','business_sales',('sales',),('/business/sales/',)),)),
    ('settings','Pengaturan & akun', (
        ('settings','Setelan billing','settings','billing_settings',('settings',),('/settings/billing/',)),
        ('sla','Target SLA tiket','clock','sla_settings',('settings',),('/operations/sla/',)),
        ('audit','Jejak audit','clock','audit',('audit',),('/audit/',)),
        ('access','Akun & peran','customers','access_index',('manage_access',),('/settings/access/',)),
        ('profile','Profil saya','customers','account_profile',(),('/account/profile/',)),
        ('security','Keamanan akun','settings','account_security',(),('/account/security/','/account/password/')),)),
)


def navigation(member, rights, path, action=''):
    action_module={'fund':'cash','cash':'cash','employee':'team','salary':'payroll','payroll':'payroll',
        'lead':'sales','commission':'sales','campaign':'sales','hotspot':'hotspot','voucher':'hotspot',
        'node':'topology','endpoint':'topology'}.get(action.partition('_')[0]) if path.startswith('/business/actions/') else None
    result=[]
    for key,label,entries in GROUPS:
        links=[]
        for item,title,icon,route,required,prefixes in entries:
            if required and not rights.intersection(required):continue
            if item=='payslips' and member.role=='customer':continue
            selected=any(path==prefix.removesuffix('#exact') if prefix.endswith('#exact') else path.startswith(prefix) for prefix in prefixes)
            if item=='customers' and path.startswith('/customers/imports/'):selected=False
            if item=='invoices' and path.startswith('/invoices/schedule/'):selected=False
            selected=selected or (action_module is not None and item==action_module)
            links.append({'key':item,'label':'Tiket bantuan' if item=='tickets' and member.role=='customer' else title,
                'icon':icon,'url':reverse(route),'selected':selected})
        if links:result.append({'key':key,'label':label,'links':links})
    return result
