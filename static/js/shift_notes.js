(() => {
  const entries = new WeakMap();
  let active = null;
  let nextId = 0;

  const entryFor = details => {
    if (!details) return null;
    if (entries.has(details)) return entries.get(details);
    const trigger = details.querySelector('summary');
    const popup = details.querySelector('[data-shift-note-popup]');
    if (!trigger || !popup) return null;
    const enhanced = typeof popup.showPopover === 'function' && typeof popup.hidePopover === 'function';
    if (enhanced) popup.setAttribute('popover', 'manual');
    if (!popup.id) popup.id = `shift-note-popup-${++nextId}`;
    trigger.setAttribute('aria-controls', popup.id);
    trigger.setAttribute('aria-expanded', String(details.open));
    const entry = {details, trigger, popup, enhanced};
    entries.set(details, entry);
    return entry;
  };

  const close = (entry, restoreFocus = false) => {
    entry.details.open = false;
    entry.trigger.setAttribute('aria-expanded', 'false');
    if (entry.enhanced && entry.popup.matches(':popover-open')) entry.popup.hidePopover();
    if (active === entry) active = null;
    if (restoreFocus) entry.trigger.focus();
  };

  const position = entry => {
    const trigger = entry.trigger.getBoundingClientRect();
    const card = entry.details.closest('.shift-entry, .shift-day-item')?.getBoundingClientRect() || trigger;
    const popup = entry.popup.getBoundingClientRect();
    const edge = 12;
    const gap = 8;
    let left;
    let top = card.top;
    if (card.right + gap + popup.width <= window.innerWidth - edge) left = card.right + gap;
    else if (card.left - gap - popup.width >= edge) left = card.left - gap - popup.width;
    else {
      left = trigger.right - popup.width;
      top = trigger.bottom + gap;
      if (top + popup.height > window.innerHeight - edge) top = trigger.top - popup.height - gap;
    }
    left = Math.max(edge, Math.min(left, window.innerWidth - popup.width - edge));
    top = Math.max(edge, Math.min(top, window.innerHeight - popup.height - edge));
    entry.popup.style.left = `${Math.round(left)}px`;
    entry.popup.style.top = `${Math.round(top)}px`;
  };

  const open = entry => {
    if (active && active !== entry) close(active);
    entry.details.open = true;
    entry.trigger.setAttribute('aria-expanded', 'true');
    active = entry;
    if (entry.enhanced) {
      try {
        if (!entry.popup.matches(':popover-open')) entry.popup.showPopover({source: entry.trigger});
        position(entry);
      } catch (_) {
        entry.enhanced = false;
        entry.popup.removeAttribute('popover');
      }
    }
  };

  const triggerEntry = target => {
    const trigger = target?.closest?.('.shift-note-toggle');
    const details = trigger?.closest('[data-shift-note]');
    return details && details.querySelector('summary') === trigger ? entryFor(details) : null;
  };

  document.addEventListener('click', event => {
    const closeButton = event.target?.closest?.('[data-shift-note-close]');
    if (closeButton) {
      const entry = entryFor(closeButton.closest('[data-shift-note]'));
      if (entry) { event.preventDefault(); close(entry, true); }
      return;
    }
    const entry = triggerEntry(event.target);
    if (entry) {
      event.preventDefault();
      if (entry.details.open) close(entry);
      else open(entry);
      return;
    }
    if (active && !active.details.contains(event.target)) close(active);
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && active) {
      event.preventDefault();
      close(active, true);
    } else if (event.key === 'ArrowDown') {
      const entry = triggerEntry(event.target);
      if (!entry) return;
      event.preventDefault();
      open(entry);
      entry.popup.querySelector('[data-shift-note-close]')?.focus();
    }
  });
  document.addEventListener('focusin', event => {
    if (active && !active.details.contains(event.target)) close(active);
  });
  window.addEventListener('resize', () => { if (active) close(active); });
  window.addEventListener('scroll', event => {
    if (active && event.target?.closest && active.popup.contains(event.target)) return;
    if (active) close(active);
  }, true);
})();
