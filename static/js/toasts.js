(() => {
  const toasts = document.querySelectorAll('[data-toast]');
  if (!toasts.length) return;

  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

  toasts.forEach(toast => {
    const configuredDuration = Number(toast.dataset.toastDuration);
    let remaining = Number.isFinite(configuredDuration) && configuredDuration > 0
      ? Math.min(30000, Math.max(1000, configuredDuration)) : 5000;
    let timer = null;
    let removalTimer = null;
    let startedAt = 0;
    let hovered = false;
    let focused = false;
    let leaving = false;
    let removed = false;

    const pause = () => {
      if (timer === null) return;
      window.clearTimeout(timer);
      timer = null;
      remaining = Math.max(0, remaining - Math.max(0, Date.now() - startedAt));
    };

    const finish = () => {
      if (removed) return;
      removed = true;
      window.clearTimeout(removalTimer);
      document.removeEventListener('visibilitychange', visibilityChanged);
      toast.remove();
    };

    const dismiss = () => {
      if (leaving) return;
      leaving = true;
      pause();
      toast.classList.remove('is-entering', 'is-visible');
      toast.classList.add('is-leaving');
      if (reducedMotion) {
        finish();
        return;
      }
      toast.addEventListener('transitionend', event => {
        if (event.target === toast && event.propertyName === 'transform') finish();
      });
      // Also remove when transitions are disabled or the end event never fires.
      removalTimer = window.setTimeout(finish, 450);
    };

    const resume = () => {
      if (leaving || removed || timer !== null || hovered || focused || document.hidden) return;
      startedAt = Date.now();
      timer = window.setTimeout(() => {
        timer = null;
        remaining = 0;
        dismiss();
      }, remaining);
    };

    function visibilityChanged() {
      if (document.hidden) pause();
      else resume();
    }

    toast.addEventListener('mouseenter', () => { hovered = true; pause(); });
    toast.addEventListener('mouseleave', () => { hovered = false; resume(); });
    toast.addEventListener('focusin', () => { focused = true; pause(); });
    toast.addEventListener('focusout', event => {
      focused = toast.contains(event.relatedTarget);
      if (!focused) resume();
    });
    toast.querySelector('[data-toast-close]')?.addEventListener('click', dismiss);
    document.addEventListener('visibilitychange', visibilityChanged);

    if (reducedMotion) {
      toast.classList.add('is-visible');
      resume();
    } else {
      toast.classList.add('is-entering');
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        if (leaving || removed) return;
        toast.classList.remove('is-entering');
        toast.classList.add('is-visible');
        resume();
      }));
    }
  });
})();
