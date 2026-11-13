(() => {
  const page = document.querySelector('.event-schedule-page');
  const input = document.getElementById('event-calendar-date');
  const filter = document.getElementById('status-filter');
  const form = input?.form;
  const navigation = page?.querySelector('.calendar-nav');
  const currentMonth = page?.querySelector('[data-current-month]');
  const create = page?.querySelector('[data-event-create]');
  let calendar = page?.querySelector('[data-event-calendar]');
  let list = page?.querySelector('[data-event-list]');
  if (!form || !filter || !navigation || !currentMonth || !calendar || !list) return;

  let loadedDate = input.value;
  let loadedStatus = filter.value;
  let sequence = 0;
  let timer;
  let controller;
  let error;

  const validDate = () => {
    const value = input.value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !input.validity.valid) return false;
    if (value < (input.min || '1970-01-01') || value > (input.max || '2200-12-31')) return false;
    const parsed = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(parsed.getTime()) && parsed.getUTCFullYear() === Number(value.slice(0, 4))
      && parsed.getUTCMonth() + 1 === Number(value.slice(5, 7)) && parsed.getUTCDate() === Number(value.slice(8, 10));
  };

  const load = async (date, status, request) => {
    const url = new URL(form.action, window.location.href);
    url.searchParams.set('date', date);
    if (status) url.searchParams.set('status', status);
    else url.searchParams.delete('status');
    controller = new AbortController();
    calendar.setAttribute('aria-busy', 'true');
    list.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      if (request !== sequence) return;
      if (response.redirected) {
        window.location.assign(url.href);
        return;
      }
      if (!response.ok) throw new Error('Unable to load events');
      const html = await response.text();
      if (request !== sequence) return;
      const nextPage = new DOMParser().parseFromString(html, 'text/html');
      const nextRoot = nextPage.querySelector('.event-schedule-page');
      const nextCalendar = nextRoot?.querySelector('[data-event-calendar]');
      const nextList = nextRoot?.querySelector('[data-event-list]');
      const nextNavigation = nextRoot?.querySelector('.calendar-nav');
      const nextCurrentMonth = nextRoot?.querySelector('[data-current-month]');
      const nextInput = nextPage.getElementById('event-calendar-date');
      const nextFilter = nextPage.getElementById('status-filter');
      if (!nextCalendar || !nextList || !nextNavigation || !nextCurrentMonth
          || nextInput?.value !== date || nextFilter?.value !== status) throw new Error('Invalid event response');
      const monthHref = nextCurrentMonth.getAttribute('href');
      if (!monthHref) throw new Error('Invalid current-month link');
      const monthUrl = new URL(monthHref, url);
      const nextCreate = nextRoot.querySelector('[data-event-create]');
      const createHref = nextCreate?.getAttribute('href');
      const createUrl = create && createHref ? new URL(createHref, url) : null;

      const calendarScroll = calendar.querySelector('.table-wrap')?.scrollLeft || 0;
      const listScroll = list.querySelector('.table-wrap')?.scrollLeft || 0;
      navigation.replaceChildren(...nextNavigation.childNodes);
      calendar.replaceWith(nextCalendar);
      list.replaceWith(nextList);
      calendar = nextCalendar;
      list = nextList;
      const calendarScroller = calendar.querySelector('.table-wrap');
      const listScroller = list.querySelector('.table-wrap');
      if (calendarScroller) calendarScroller.scrollLeft = calendarScroll;
      if (listScroller) listScroller.scrollLeft = listScroll;
      currentMonth.href = monthUrl.href;
      if (createUrl) create.href = createUrl.href;
      loadedDate = date;
      loadedStatus = status;
      window.history.replaceState(window.history.state, '', url.href);
      document.dispatchEvent(new Event('event-calendar-updated'));
    } catch (failure) {
      if (request !== sequence || failure.name === 'AbortError') return;
      error = document.createElement('p');
      error.className = 'message message-error';
      error.setAttribute('role', 'alert');
      error.textContent = 'Не удалось обновить инфоповоды. Повторите выбор даты или этапа либо нажмите Enter.';
      calendar.before(error);
    } finally {
      if (request === sequence) {
        controller = undefined;
        calendar.removeAttribute('aria-busy');
        list.removeAttribute('aria-busy');
      }
    }
  };

  const update = (delay = 250) => {
    clearTimeout(timer);
    const request = ++sequence;
    controller?.abort();
    controller = undefined;
    calendar.removeAttribute('aria-busy');
    list.removeAttribute('aria-busy');
    error?.remove();
    error = undefined;
    if (!validDate() || (input.value === loadedDate && filter.value === loadedStatus)) return;
    const date = input.value;
    const status = filter.value;
    timer = setTimeout(() => load(date, status, request), delay);
  };

  input.addEventListener('input', () => update());
  input.addEventListener('change', () => update(0));
  filter.addEventListener('change', () => update(0));
  form.addEventListener('submit', event => {
    event.preventDefault();
    update(0);
  });
})();
