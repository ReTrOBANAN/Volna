(() => {
  const data = document.getElementById('booking-status-watch-data');
  if (!data || typeof performance === 'undefined' || typeof performance.now !== 'function') return;

  const statuses = new Set(['planned', 'recording', 'completed', 'cancelled']);
  const activeStatuses = new Set(['planned', 'recording']);
  const form = document.querySelector('[data-booking-form]');
  const tick = () => performance.now();
  const readInstant = value => {
    if (typeof value !== 'string') throw new Error('Invalid booking timestamp');
    const parts = value.match(/^(\d{4})-(\d{2})-(\d{2})T([01]\d|2[0-3]):([0-5]\d):([0-5]\d)(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/);
    if (!parts) throw new Error('Invalid booking timestamp');
    const year = Number(parts[1]);
    const month = Number(parts[2]);
    const day = Number(parts[3]);
    const lastDay = new Date(Date.UTC(year < 100 ? year + 400 : year, month, 0)).getUTCDate();
    const instant = Date.parse(value);
    if (month < 1 || month > 12 || day < 1 || day > lastDay || !Number.isFinite(instant)) throw new Error('Invalid booking timestamp');
    return instant;
  };
  const readBookings = values => {
    if (!Array.isArray(values)) throw new Error('Invalid booking status snapshot');
    const ids = new Set();
    return values.map(value => {
      if (!value || !Number.isSafeInteger(value.id) || value.id <= 0 || ids.has(value.id) || !statuses.has(value.status)) throw new Error('Invalid booking status snapshot');
      const start = readInstant(value.starts_at);
      const end = readInstant(value.ends_at);
      if (end <= start) throw new Error('Invalid booking interval');
      ids.add(value.id);
      return {id: value.id, status: value.status, start, end};
    }).sort((left, right) => left.id - right.id);
  };
  const fingerprint = values => values.map(value => `${value.id}:${value.status}:${value.start}:${value.end}`).join('|');

  let endpoint;
  let bookings;
  let serverAnchor;
  let localAnchor = tick();
  try {
    const config = JSON.parse(data.textContent);
    endpoint = new URL(config.url, window.location.origin);
    if (endpoint.origin !== window.location.origin) return;
    serverAnchor = readInstant(config.server_now);
    bookings = readBookings(form && Array.isArray(form.bookingStatusSnapshot) ? form.bookingStatusSnapshot : config.bookings);
  } catch (_) { return; }

  let generation = 0;
  let pollTimer;
  let boundaryTimer;
  let activeRequest;
  let reloading = false;
  const serverNow = () => serverAnchor + tick() - localAnchor;
  const isWatching = () => !document.hidden && !reloading && bookings.some(value => activeStatuses.has(value.status));
  const clearTimers = () => {
    clearTimeout(pollTimer);
    clearTimeout(boundaryTimer);
    pollTimer = undefined;
    boundaryTimer = undefined;
  };
  const cancelRequest = () => {
    if (!activeRequest) return;
    const previous = activeRequest;
    activeRequest = undefined;
    clearTimeout(previous.timeout);
    previous.controller.abort();
  };
  const schedule = (allowBoundary = true) => {
    clearTimers();
    if (!isWatching() || activeRequest) return;
    pollTimer = setTimeout(check, 15000);
    if (!allowBoundary) return;
    const current = serverNow();
    let nearest = Infinity;
    bookings.forEach(value => {
      if (!activeStatuses.has(value.status)) return;
      if (value.status === 'planned' && value.start > current) nearest = Math.min(nearest, value.start);
      if (value.end > current) nearest = Math.min(nearest, value.end);
    });
    if (Number.isFinite(nearest)) boundaryTimer = setTimeout(check, Math.min(2147483647, nearest - current + 250));
  };

  async function check() {
    if (!isWatching() || activeRequest) return;
    clearTimers();
    const request = {generation, controller: new AbortController(), timeout: undefined};
    activeRequest = request;
    request.timeout = setTimeout(() => {
      if (activeRequest !== request) return;
      activeRequest = undefined;
      request.controller.abort();
      schedule(false);
    }, 10000);
    const ids = bookings.map(value => value.id);
    let failed = false;
    try {
      const refreshed = [];
      let clock;
      let receivedAt;
      for (let index = 0; index < ids.length; index += 1000) {
        const batch = ids.slice(index, index + 1000);
        const expected = new Set(batch);
        const url = new URL(endpoint.href);
        url.searchParams.set('ids', batch.join(','));
        const response = await fetch(url, {
          credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}, signal: request.controller.signal,
        });
        if (!response.ok) throw new Error('Unable to check booking statuses');
        const payload = await response.json();
        const next = readBookings(payload.bookings);
        if (next.some(value => !expected.has(value.id))) throw new Error('Unexpected booking identifier');
        clock = readInstant(payload.server_now);
        receivedAt = tick();
        if (activeRequest !== request || request.generation !== generation || request.controller.signal.aborted || document.hidden) return;
        refreshed.push(...next);
      }
      refreshed.sort((left, right) => left.id - right.id);
      const changed = fingerprint(refreshed) !== fingerprint(bookings);
      bookings = refreshed;
      serverAnchor = clock;
      localAnchor = receivedAt;
      if (changed) {
        if (form) document.dispatchEvent(new CustomEvent('booking-statuses-changed'));
        else {
          reloading = true;
          clearTimers();
          window.location.reload();
        }
      }
    } catch (_) {
      failed = true;
    } finally {
      clearTimeout(request.timeout);
      if (activeRequest === request) {
        activeRequest = undefined;
        if (request.generation === generation) schedule(!failed);
      }
    }
  }

  if (form) document.addEventListener('booking-schedule-updated', event => {
    let next;
    try { next = readBookings(event.detail.bookings); } catch (_) { return; }
    if (fingerprint(next) === fingerprint(bookings)) return;
    generation += 1;
    cancelRequest();
    bookings = next;
    clearTimers();
    if (isWatching()) check();
  });
  document.addEventListener('visibilitychange', () => {
    clearTimers();
    if (document.hidden) cancelRequest();
    else check();
  });
  if (isWatching()) check();
})();
