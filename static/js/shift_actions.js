(() => {
  const actions = Array.from(document.querySelectorAll('[data-shift-actions]')).map(details => {
    const trigger = details.querySelector('summary');
    const menu = details.querySelector('[data-shift-actions-menu]');
    if (!trigger || !menu) return null;
    const enhanced = typeof menu.showPopover === 'function' && typeof menu.hidePopover === 'function';
    if (enhanced) menu.setAttribute('popover', 'manual');
    trigger.setAttribute('aria-expanded', 'false');
    return {details, trigger, menu, enhanced};
  }).filter(Boolean);
  if (!actions.length) return;

  let active = null;

  const close = (entry, restoreFocus = false) => {
    entry.details.open = false;
    entry.trigger.setAttribute('aria-expanded', 'false');
    if (entry.enhanced && entry.menu.matches(':popover-open')) entry.menu.hidePopover();
    if (active === entry) active = null;
    if (restoreFocus) entry.trigger.focus();
  };

  const position = entry => {
    const anchor = entry.trigger.getBoundingClientRect();
    const popup = entry.menu.getBoundingClientRect();
    const gap = 6;
    const edge = 8;
    const left = Math.max(edge, Math.min(anchor.right - popup.width, window.innerWidth - popup.width - edge));
    let top = anchor.bottom + gap;
    if (top + popup.height > window.innerHeight - edge) top = anchor.top - popup.height - gap;
    top = Math.max(edge, Math.min(top, window.innerHeight - popup.height - edge));
    entry.menu.style.left = `${Math.round(left)}px`;
    entry.menu.style.top = `${Math.round(top)}px`;
  };

  const open = entry => {
    if (active && active !== entry) close(active);
    entry.details.open = true;
    entry.trigger.setAttribute('aria-expanded', 'true');
    active = entry;
    if (entry.enhanced) {
      try {
        if (!entry.menu.matches(':popover-open')) entry.menu.showPopover({source: entry.trigger});
        position(entry);
      } catch (_) {
        // Older browsers retain the native disclosure inside the shift card.
        entry.enhanced = false;
        entry.menu.removeAttribute('popover');
      }
    }
  };

  actions.forEach(entry => {
    entry.trigger.addEventListener('click', event => {
      event.preventDefault();
      if (entry.details.open) close(entry);
      else open(entry);
    });
    entry.trigger.addEventListener('keydown', event => {
      if (event.key !== 'ArrowDown') return;
      event.preventDefault();
      open(entry);
      entry.menu.querySelector('a, button')?.focus();
    });
  });

  document.addEventListener('click', event => {
    if (active && !active.details.contains(event.target) && !active.menu.contains(event.target)) close(active);
  });
  document.addEventListener('focusin', event => {
    if (active && !active.details.contains(event.target) && !active.menu.contains(event.target)) close(active);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && active) {
      event.preventDefault();
      close(active, true);
    }
  });
  window.addEventListener('resize', () => { if (active) close(active); });
  window.addEventListener('scroll', () => { if (active) close(active); }, true);
})();
