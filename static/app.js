document.documentElement.dataset.enhanced = 'true';
const sidebarToggle = document.querySelector('#sidebar-toggle');
const sidebar = document.querySelector('#sidebar');
const app = document.querySelector('.app');
const mobileNavigation = window.matchMedia('(max-width: 850px)');
function syncSidebar() {
  const open = mobileNavigation.matches && document.body.classList.contains('nav-open');
  if (sidebar) {
    sidebar.inert = mobileNavigation.matches && !open;
    if (open) { sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', 'true'); }
    else { sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal'); }
  }
  if (app) app.inert = open;
  sidebarToggle?.setAttribute('aria-expanded', String(open));
}
function closeSidebar(returnFocus = true) {
  document.body.classList.remove('nav-open');
  syncSidebar();
  if (returnFocus && mobileNavigation.matches) sidebarToggle?.focus({preventScroll: true});
}
mobileNavigation.addEventListener('change', () => {
  if (!mobileNavigation.matches) document.body.classList.remove('nav-open');
  syncSidebar();
});
syncSidebar();
sidebarToggle?.addEventListener('click', () => {
  document.body.classList.toggle('nav-open');
  syncSidebar();
  if (document.body.classList.contains('nav-open')) document.querySelector('#sidebar-close')?.focus();
});
document.querySelector('#sidebar-close')?.addEventListener('click', () => closeSidebar());
document.addEventListener('click', event => {
  if (mobileNavigation.matches && document.body.classList.contains('nav-open') && !sidebar.contains(event.target) && !sidebarToggle.contains(event.target)) closeSidebar();
});
document.addEventListener('keydown', (event) => {
  if (!mobileNavigation.matches || !document.body.classList.contains('nav-open') || document.querySelector('dialog[open]')) return;
  if (event.key === 'Escape') { event.preventDefault(); closeSidebar(); }
  if (event.key === 'Tab') {
    const controls = [...sidebar.querySelectorAll('a[href], button, input, summary, [tabindex="0"]')]
      .filter(element => !element.disabled && element.getClientRects().length > 0);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
});
try {
  document.documentElement.dataset.theme = localStorage.getItem('tagihnet-theme') || 'light';
} catch { /* Storage may be unavailable in a private context. */ }
document.querySelector('#theme-toggle')?.addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('tagihnet-theme', theme); } catch { /* Optional preference. */ }
});
document.querySelector('#password-toggle')?.addEventListener('click', (event) => {
  const input = document.querySelector('#id_password');
  const showing = input.type === 'password';
  input.type = showing ? 'text' : 'password';
  event.target.textContent = showing ? 'Sembunyikan' : 'Tampilkan';
  event.target.setAttribute('aria-pressed', String(showing));
});
const menuInputs = [...document.querySelectorAll('[data-menu-search]')];
const menuGroups = [...document.querySelectorAll('[data-nav-group]')];
const menuPreferenceKey = 'tagihnet-navigation-v1';
let menuPreferences = {}, beforeSearch = null, searching = false;
try {
  const saved = JSON.parse(localStorage.getItem(menuPreferenceKey) || '{}');
  if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
    for (const group of menuGroups) if (typeof saved[group.dataset.navGroup] === 'boolean') menuPreferences[group.dataset.navGroup] = saved[group.dataset.navGroup];
  }
} catch { /* Groups stay usable without browser storage. */ }
for (const group of menuGroups) {
  const choice = menuPreferences[group.dataset.navGroup];
  group.open = !!group.querySelector('.selected') || choice !== false;
  group.addEventListener('toggle', () => {
    if (searching) return;
    menuPreferences[group.dataset.navGroup] = group.open;
    try { localStorage.setItem(menuPreferenceKey, JSON.stringify(menuPreferences)); } catch { /* Optional preference. */ }
  });
}
function filterMenus(value) {
  const term = value.trim().toLocaleLowerCase('id');
  if (term && !searching) beforeSearch = new Map(menuGroups.map(group => [group, group.open]));
  searching = !!term;
  for (const input of menuInputs) input.value = value;
  let count = 0;
  for (const group of menuGroups) {
    const groupMatches = group.querySelector('summary').textContent.toLocaleLowerCase('id').includes(term);
    const links = [...group.querySelectorAll('.nav-link')];
    for (const link of links) {
      link.hidden = !!term && !groupMatches && !link.textContent.toLocaleLowerCase('id').includes(term);
      if (!link.hidden) count += 1;
    }
    group.hidden = !links.some(link => !link.hidden);
    if (term) group.open = !group.hidden;
    else if (beforeSearch) group.open = beforeSearch.get(group);
  }
  if (!term) beforeSearch = null;
  const empty = document.querySelector('[data-nav-search-empty]');
  if (empty) empty.hidden = !term || count > 0;
  const status = document.querySelector('[data-menu-search-status]');
  if (status) status.textContent = term ? `${count} menu ditemukan.` : '';
}
for (const input of menuInputs) input.addEventListener('input', event => filterMenus(event.target.value));
document.querySelector('#show-username')?.addEventListener('change', (event) => {
  document.querySelectorAll('.username-column').forEach(cell => { cell.hidden = !event.target.checked; });
});
document.querySelectorAll('[data-confirm]').forEach(button => {
  button.addEventListener('click', event => {
    if (!window.confirm(button.dataset.confirm)) event.preventDefault();
  });
});
document.querySelectorAll('form').forEach(form => {
  form.addEventListener('submit', (event) => {
    if (event.submitter?.name) {
      const action = document.createElement('input');
      action.type = 'hidden';
      action.name = event.submitter.name;
      action.value = event.submitter.value;
      form.append(action);
    }
    // Native constraint validation has already succeeded before submit fires.
    form.querySelectorAll('button[type="submit"], button:not([type])').forEach(button => {
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
    });
  });
});
const updateConnection = () => { document.querySelector('#offline-notice').hidden = navigator.onLine; };
window.addEventListener('online', updateConnection);
window.addEventListener('offline', updateConnection);
updateConnection();
if (location.pathname === '/customers/') {
  const listURL = location.pathname + location.search;
  try {
    sessionStorage.setItem('tagihnet-customer-list', listURL);
    const position = JSON.parse(sessionStorage.getItem('tagihnet-customer-scroll') || 'null');
    if (position?.url === listURL && Number.isFinite(position.y) && position.y >= 0) {
      requestAnimationFrame(() => window.scrollTo(0, position.y));
    }
  } catch { /* Navigation still works without storage. */ }
  window.addEventListener('pagehide', () => {
    try { sessionStorage.setItem('tagihnet-customer-scroll', JSON.stringify({url: listURL, y: window.scrollY})); }
    catch { /* Scroll restoration is optional. */ }
  });
}
const backToList = document.querySelector('#back-to-list');
if (backToList) {
  try {
    const url = sessionStorage.getItem('tagihnet-customer-list');
    if (url?.startsWith('/customers/?') || url === '/customers/') backToList.href = url;
  } catch { /* Default return URL still works. */ }
}

const previewDialog = document.querySelector('#customer-preview-dialog');
if (previewDialog && typeof previewDialog.showModal === 'function' && typeof AbortController === 'function' && typeof fetch === 'function') {
  const content = previewDialog.querySelector('#customer-preview-content');
  const detail = previewDialog.querySelector('[data-drawer-detail]');
  let controller = null, version = 0, trigger = null, scrollPosition = 0;
  const finish = () => {
    version += 1;
    controller?.abort(); controller = null;
    content.replaceChildren(); content.setAttribute('aria-busy', 'false');
    document.documentElement.classList.remove('drawer-open');
    window.scrollTo(0, scrollPosition);
    if (trigger?.isConnected) trigger.focus({preventScroll: true});
  };
  previewDialog.addEventListener('close', () => { if (!previewDialog.open) finish(); });
  previewDialog.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const controls = [...previewDialog.querySelectorAll('a[href], button:not([disabled]), [tabindex="0"]')]
      .filter(element => element.getClientRects().length > 0);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  for (const button of previewDialog.querySelectorAll('[data-drawer-close]')) button.addEventListener('click', () => previewDialog.close());
  previewDialog.addEventListener('click', event => {
    const rect = previewDialog.getBoundingClientRect();
    if (event.target === previewDialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) previewDialog.close();
    if (event.target.closest('a[href]')) previewDialog.close();
  });
  document.addEventListener('click', async event => {
    const link = event.target.closest('[data-customer-preview]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const endpoint = new URL(link.dataset.previewUrl, location.origin);
    if (endpoint.origin !== location.origin || !/^\/customers\/[0-9]+\/preview\/$/.test(endpoint.pathname) || endpoint.search || endpoint.hash) return;
    event.preventDefault();
    controller?.abort(); controller = new AbortController();
    const current = ++version, signal = controller.signal;
    trigger = link; scrollPosition = window.scrollY;
    detail.href = endpoint.pathname.replace(/preview\/$/, '');
    content.textContent = 'Memuat ringkasan pelanggan…'; content.setAttribute('aria-busy', 'true');
    document.documentElement.classList.add('drawer-open');
    if (!previewDialog.open) previewDialog.showModal();
    previewDialog.querySelector('[data-drawer-close]').focus({preventScroll: true});
    window.scrollTo(0, scrollPosition);
    try {
      const response = await fetch(endpoint, {signal, credentials: 'same-origin', headers: {'Accept': 'text/html'}});
      if (!response.ok || response.redirected || !response.headers.get('Content-Type')?.includes('text/html')) throw new Error('Preview unavailable');
      const html = await response.text();
      if (current !== version || !previewDialog.open) return;
      // Only a scoped, escaped server template from our fixed same-origin route is inserted.
      content.innerHTML = html;
    } catch (error) {
      if (error.name === 'AbortError' || current !== version || !previewDialog.open) return;
      const message = document.createElement('p'); message.setAttribute('role', 'alert');
      message.textContent = 'Ringkasan tidak dapat dimuat. Buka detail pelanggan untuk melanjutkan.';
      content.replaceChildren(message);
    } finally { if (current === version) content.setAttribute('aria-busy', 'false'); }
  });
}
