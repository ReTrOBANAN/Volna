(() => {
  const toggle = document.querySelector('[data-sidebar-toggle]');
  const closeMenu = () => { document.body.classList.remove('menu-open'); if (toggle) toggle.setAttribute('aria-expanded', 'false'); };
  if (toggle) toggle.addEventListener('click', () => { const open = document.body.classList.toggle('menu-open'); toggle.setAttribute('aria-expanded', String(open)); });
  document.querySelectorAll('[data-sidebar-close]').forEach(el => el.addEventListener('click', closeMenu));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeMenu(); });
  document.querySelectorAll('[data-dismiss]').forEach(el => el.addEventListener('click', () => el.parentElement.remove()));
  document.addEventListener('submit', event => { const form = event.target.closest('[data-confirm]'); if (form && !window.confirm(form.dataset.confirm)) event.preventDefault(); });
  [
    {dialogSelector: '[data-booking-dialog]', linkSelector: '[data-booking-preview]', loadingText: 'Загружаем информацию о записи…'},
    {dialogSelector: '[data-event-dialog]', linkSelector: '[data-event-preview]', loadingText: 'Загружаем информацию об инфоповоде…'},
  ].forEach(({dialogSelector, linkSelector, loadingText}) => {
    const dialog = document.querySelector(dialogSelector);
    if (!dialog || typeof dialog.showModal !== 'function') return;
    const body = dialog.querySelector('[data-dialog-body]');
    const closeButton = dialog.querySelector('[data-dialog-close]');
    if (!body || !closeButton) return;
    let previewRequest = 0;
    closeButton.addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
    });
    // Delegation also handles links inserted by the calendar's live navigation.
    document.addEventListener('click', async event => {
      const link = event.target?.closest?.(linkSelector);
      if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const currentRequest = ++previewRequest;
      const loading = document.createElement('p');
      loading.className = 'dialog-loading';
      loading.setAttribute('role', 'status');
      loading.textContent = loadingText;
      body.replaceChildren(loading);
      if (!dialog.open) dialog.showModal();
      try {
        const response = await fetch(link.href, {credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok) throw new Error('Unable to load details');
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const heading = page.querySelector('main .page-heading');
        const detail = page.querySelector('main .detail-layout');
        if (!heading || !detail) throw new Error('Details unavailable');
        if (dialog.open && currentRequest === previewRequest) body.replaceChildren(heading, detail);
      } catch (_) {
        if (dialog.open && currentRequest === previewRequest) window.location.assign(link.href);
      }
    });
  });
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
