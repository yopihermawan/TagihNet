(() => {
  const panel = document.querySelector('#gateway-watch');
  if (!panel) return;
  const message = panel.querySelector('[data-watch-message]');
  const reload = panel.querySelector('[data-watch-reload]');
  const button = panel.querySelector('[data-watch-button]');
  const endpoint = new URL(panel.dataset.url, location.origin);
  if (endpoint.origin !== location.origin) return;
  const interval = 5000;
  const duration = 120000;
  let active = false;
  let deadline = 0;
  let timer;
  let controller;
  let generation = 0;

  function stop(text, changed = false) {
    active = false;
    generation += 1;
    clearTimeout(timer);
    controller?.abort();
    controller = undefined;
    message.textContent = text;
    reload.hidden = !changed;
    button.hidden = changed;
    button.textContent = 'Pantau lagi';
  }

  function schedule() {
    clearTimeout(timer);
    if (!active) return;
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      stop('Pemantauan dua menit selesai. Ini tidak memastikan status di Midtrans. Muat ulang halaman atau pilih Pantau lagi.');
      return;
    }
    timer = setTimeout(poll, Math.min(interval, remaining));
  }

  async function poll() {
    if (!active || controller) return;
    if (Date.now() >= deadline) { schedule(); return; }
    if (document.hidden || !navigator.onLine) { schedule(); return; }
    const current = generation;
    const pending = new AbortController();
    controller = pending;
    const timeout = setTimeout(() => pending.abort(), Math.min(8000, deadline - Date.now()));
    try {
      const response = await fetch(endpoint, {credentials: 'same-origin', cache: 'no-store',
        redirect: 'error', headers: {'Accept': 'application/json'}, signal: pending.signal});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error();
      const data = await response.json();
      if (!/^[a-f0-9]{64}$/.test(data.revision) || typeof data.label !== 'string' || typeof data.watch !== 'boolean') throw new Error();
      if (!active || current !== generation) return;
      if (data.revision !== panel.dataset.revision || !data.watch) {
        stop(`Ada pembaruan tersimpan: ${data.label}. Pilih Tampilkan pembaruan untuk melihat rincian.`, true);
        return;
      }
    } catch {
      if (active && current === generation) {
        stop('Pemantauan berhenti karena koneksi atau akses belum dapat dipastikan. Muat ulang halaman untuk memeriksa sesi dan status tersimpan.');
      }
    } finally {
      clearTimeout(timeout);
      if (current === generation) { controller = undefined; schedule(); }
    }
  }

  function start() {
    generation += 1;
    active = true;
    deadline = Date.now() + duration;
    message.textContent = 'Pemantauan halaman aktif. Perubahan yang terdeteksi akan ditampilkan di sini.';
    button.textContent = 'Hentikan pemantauan';
    schedule();
  }

  button.addEventListener('click', () => {
    if (active) stop('Pemantauan halaman dihentikan. Anda dapat memulai lagi atau memuat ulang halaman.');
    else start();
  });
  document.addEventListener('visibilitychange', () => {
    if (!active) return;
    if (document.hidden) {
      generation += 1;
      controller?.abort();
      controller = undefined;
    }
    schedule();
  });
  window.addEventListener('pagehide', () => stop('Pemantauan halaman dihentikan.'));
  panel.hidden = false;
  start();
})();
