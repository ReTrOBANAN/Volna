(() => {
  const page = document.querySelector('.shift-schedule-page');
  const input = document.getElementById('shift-calendar-date');
  const form = input?.form;
  const navigation = page?.querySelector('.calendar-nav');
  let calendar = page?.querySelector('.calendar-block');
  if (!form || !navigation || !calendar) return;

  let loadedDate = input.value;
  let sequence = 0;
  let timer;
  let controller;
  let error;

  const validDate = () => {
    const value = input.value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !input.validity.valid) return false;
    if (value < input.min || value > input.max) return false;
    const parsed = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(parsed.getTime()) && parsed.getUTCFullYear() === Number(value.slice(0, 4))
      && parsed.getUTCMonth() + 1 === Number(value.slice(5, 7)) && parsed.getUTCDate() === Number(value.slice(8, 10));
  };

  const load = async (date, request) => {
    const url = new URL(form.action, window.location.href);
    url.searchParams.set('date', date);
    controller = new AbortController();
    calendar.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      if (request !== sequence) return;
      if (response.redirected) {
        // A full navigation lets the normal login flow preserve the requested date.
        window.location.assign(url.href);
        return;
      }
      if (!response.ok) throw new Error('Unable to load calendar');
      const html = await response.text();
      if (request !== sequence) return;
      const nextPage = new DOMParser().parseFromString(html, 'text/html');
      const nextCalendar = nextPage.querySelector('.shift-schedule-page .calendar-block');
      const nextNavigation = nextPage.querySelector('.shift-schedule-page .calendar-nav');
      const nextInput = nextPage.getElementById('shift-calendar-date');
      if (!nextCalendar || !nextNavigation || nextInput?.value !== date) throw new Error('Invalid calendar response');

      const scroll = calendar.querySelector('.two-week-scroll')?.scrollLeft || 0;
      navigation.replaceChildren(...nextNavigation.childNodes);
      calendar.replaceWith(nextCalendar);
      calendar = nextCalendar;
      const scroller = calendar.querySelector('.two-week-scroll');
      if (scroller) scroller.scrollLeft = scroll;
      loadedDate = date;
      window.history.replaceState(window.history.state, '', url.href);
      document.dispatchEvent(new Event('shift-calendar-updated'));
    } catch (failure) {
      if (request !== sequence || failure.name === 'AbortError') return;
      error = document.createElement('p');
      error.className = 'message message-error';
      error.setAttribute('role', 'alert');
      error.textContent = 'Не удалось обновить расписание. Повторите выбор даты или нажмите Enter.';
      calendar.before(error);
    } finally {
      if (request === sequence) {
        controller = undefined;
        calendar.removeAttribute('aria-busy');
      }
    }
  };

  const update = (delay = 250) => {
    clearTimeout(timer);
    const request = ++sequence;
    controller?.abort();
    controller = undefined;
    calendar.removeAttribute('aria-busy');
    error?.remove();
    error = undefined;
    if (!validDate() || input.value === loadedDate) return;
    const date = input.value;
    timer = setTimeout(() => load(date, request), delay);
  };

  input.addEventListener('input', () => update());
  input.addEventListener('change', () => update(0));
  form.addEventListener('submit', event => {
    event.preventDefault();
    update(0);
  });
})();
