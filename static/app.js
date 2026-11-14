const sidebarToggle = document.querySelector('#sidebar-toggle');
const sidebar = document.querySelector('#sidebar');
const mobileNavigation = window.matchMedia('(max-width: 850px)');
function syncSidebar() {
  if (sidebar) sidebar.inert = mobileNavigation.matches && !document.body.classList.contains('nav-open');
}
mobileNavigation.addEventListener('change', syncSidebar);
syncSidebar();
sidebarToggle?.addEventListener('click', () => {
  const open = document.body.classList.toggle('nav-open');
  sidebarToggle.setAttribute('aria-expanded', String(open));
  syncSidebar();
  if (open) document.querySelector('.sidebar a')?.focus();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && document.body.classList.contains('nav-open')) {
    document.body.classList.remove('nav-open');
    syncSidebar();
    sidebarToggle.setAttribute('aria-expanded', 'false');
    sidebarToggle.focus();
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
document.querySelector('#menu-search')?.addEventListener('input', (event) => {
  document.querySelectorAll('.nav-link').forEach(link => {
    link.hidden = !link.textContent.toLocaleLowerCase('id').includes(event.target.value.toLocaleLowerCase('id'));
  });
});
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
