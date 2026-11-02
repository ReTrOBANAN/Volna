(() => {
  const toggle = document.querySelector('[data-sidebar-toggle]');
  const closeMenu = () => { document.body.classList.remove('menu-open'); if (toggle) toggle.setAttribute('aria-expanded', 'false'); };
  if (toggle) toggle.addEventListener('click', () => { const open = document.body.classList.toggle('menu-open'); toggle.setAttribute('aria-expanded', String(open)); });
  document.querySelectorAll('[data-sidebar-close]').forEach(el => el.addEventListener('click', closeMenu));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeMenu(); });
  document.querySelectorAll('[data-dismiss]').forEach(el => el.addEventListener('click', () => el.parentElement.remove()));
  document.addEventListener('submit', event => { const form = event.target.closest('[data-confirm]'); if (form && !window.confirm(form.dataset.confirm)) event.preventDefault(); });
  const bookingDialog = document.querySelector('[data-booking-dialog]');
  if (bookingDialog && typeof bookingDialog.showModal === 'function') {
    const body = bookingDialog.querySelector('[data-dialog-body]');
    let previewRequest = 0;
    bookingDialog.querySelector('[data-dialog-close]').addEventListener('click', () => bookingDialog.close());
    bookingDialog.addEventListener('click', event => {
      if (event.target !== bookingDialog) return;
      const box = bookingDialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) bookingDialog.close();
    });
    document.querySelectorAll('[data-booking-preview]').forEach(link => link.addEventListener('click', async event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const currentRequest = ++previewRequest;
      body.innerHTML = '<p class="dialog-loading" role="status">Загружаем информацию о записи…</p>';
      bookingDialog.showModal();
      try {
        const response = await fetch(link.href, {credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok) throw new Error('Unable to load booking');
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const heading = page.querySelector('main .page-heading');
        const detail = page.querySelector('main .detail-layout');
        if (!heading || !detail) throw new Error('Booking unavailable');
        if (bookingDialog.open && currentRequest === previewRequest) body.replaceChildren(heading, detail);
      } catch (_) {
        if (bookingDialog.open && currentRequest === previewRequest) window.location.assign(link.href);
      }
    }));
  }
  const refreshNotifications = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch('/notifications/poll/', {headers: {'Accept': 'application/json'}, credentials: 'same-origin'});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
      const data = await response.json();
      const count = data.unread_count ?? data.count ?? 0;
      document.querySelectorAll('[data-notification-count]').forEach(el => { el.textContent = count > 99 ? '99+' : count; el.classList.toggle('hidden', count === 0); });
    } catch (_) { /* Current page remains usable while the connection is unavailable. */ }
  };
  if (document.querySelector('[data-notification-count]')) { window.setInterval(refreshNotifications, 30000); document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshNotifications(); }); }
})();
