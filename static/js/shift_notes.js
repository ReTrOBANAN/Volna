(() => {
  const entries = new WeakMap();
  let active = null;
  let closeTimer;
  let nextId = 0;

  const entryFor = card => {
    if (!card) return null;
    if (entries.has(card)) return entries.get(card);
    const trigger = card.querySelector('[data-shift-note-trigger]');
    const popup = card.querySelector('[data-shift-note-popup]');
    if (!trigger || !popup) return null;
    const enhanced = typeof popup.showPopover === 'function' && typeof popup.hidePopover === 'function';
    if (enhanced) popup.setAttribute('popover', 'manual');
    if (!popup.id) popup.id = `shift-note-popup-${++nextId}`;
    trigger.setAttribute('aria-controls', popup.id);
    const entry = {card, trigger, popup, enhanced, open: false};
    entries.set(card, entry);
    return entry;
  };

  const cancelClose = () => {
    clearTimeout(closeTimer);
    closeTimer = undefined;
  };

  const contains = (entry, target) => Boolean(target && (entry.card.contains(target) || entry.popup.contains(target)));

  const close = (entry, restoreFocus = false) => {
    cancelClose();
    entry.open = false;
    entry.trigger.setAttribute('aria-expanded', 'false');
    entry.trigger.removeAttribute('aria-describedby');
    if (entry.enhanced && entry.popup.matches(':popover-open')) entry.popup.hidePopover();
    entry.popup.hidden = true;
    if (active === entry) active = null;
    if (restoreFocus) entry.trigger.focus();
  };

  const position = entry => {
    entry.popup.style.position = 'fixed';
    const card = entry.card.getBoundingClientRect();
    const popup = entry.popup.getBoundingClientRect();
    const edge = 12;
    const gap = 8;
    let left;
    let top = card.top;
    if (card.right + gap + popup.width <= window.innerWidth - edge) left = card.right + gap;
    else if (card.left - gap - popup.width >= edge) left = card.left - gap - popup.width;
    else {
      left = card.right - popup.width;
      top = card.bottom + gap;
      if (top + popup.height > window.innerHeight - edge) top = card.top - popup.height - gap;
    }
    left = Math.max(edge, Math.min(left, window.innerWidth - popup.width - edge));
    top = Math.max(edge, Math.min(top, window.innerHeight - popup.height - edge));
    entry.popup.style.left = `${Math.round(left)}px`;
    entry.popup.style.top = `${Math.round(top)}px`;
  };

  const open = entry => {
    cancelClose();
    if (active && active !== entry) close(active);
    entry.open = true;
    entry.trigger.setAttribute('aria-expanded', 'true');
    entry.trigger.setAttribute('aria-describedby', entry.popup.id);
    entry.popup.hidden = false;
    active = entry;
    if (entry.enhanced) {
      try {
        if (!entry.popup.matches(':popover-open')) entry.popup.showPopover({source: entry.trigger});
      } catch (_) {
        entry.enhanced = false;
        entry.popup.removeAttribute('popover');
      }
    }
    position(entry);
  };

  document.addEventListener('click', event => {
    if (active && active.popup.contains(event.target)) return;
    const card = event.target?.closest?.('[data-shift-note]');
    const control = event.target?.closest?.('a, button, input, select, textarea, summary, form, [data-shift-actions], [contenteditable]');
    const entry = card && !control ? entryFor(card) : null;
    if (entry) {
      event.preventDefault();
      if (entry.open) close(entry);
      else open(entry);
    } else if (active) close(active);
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && active) {
      event.preventDefault();
      close(active, true);
    } else if ((event.key === 'Enter' || event.key === ' ') && !event.repeat) {
      const trigger = event.target?.closest?.('[data-shift-note-trigger]');
      const entry = entryFor(trigger?.closest('[data-shift-note]'));
      if (!entry || trigger !== entry.trigger) return;
      event.preventDefault();
      if (entry.open) close(entry);
      else open(entry);
    }
  });

  document.addEventListener('pointerout', event => {
    if (!active || event.pointerType === 'touch' || !contains(active, event.target) || contains(active, event.relatedTarget)) return;
    cancelClose();
    const entry = active;
    // Allow the cursor to cross the small gap between the shift and its note.
    closeTimer = setTimeout(() => { if (active === entry) close(entry); }, 150);
  });
  document.addEventListener('pointerover', event => {
    if (active && contains(active, event.target)) cancelClose();
  });
  document.addEventListener('focusin', event => {
    if (active && !contains(active, event.target)) close(active);
  });
  document.addEventListener('focusout', event => {
    // Clicking note text can blur the card without focusing another element.
    if (active && event.relatedTarget && contains(active, event.target) && !contains(active, event.relatedTarget)) close(active);
  });
  document.addEventListener('shift-calendar-updated', () => { if (active) close(active); });
  window.addEventListener('blur', () => { if (active) close(active); });
  window.addEventListener('resize', () => { if (active) close(active); });
  window.addEventListener('scroll', event => {
    if (active && event.target?.nodeType && active.popup.contains(event.target)) return;
    if (active) close(active);
  }, true);
})();
