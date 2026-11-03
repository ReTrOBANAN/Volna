(() => {
  const form = document.querySelector('[data-booking-form]');
  const panel = document.querySelector('[data-booking-day-schedule]');
  const initialData = document.getElementById('booking-day-schedule-data');
  if (!form || !panel || !initialData) return;

  const fields = Object.fromEntries(['studio', 'date', 'start_time', 'end_time', 'title', 'responsible', 'kind']
    .map(name => [name, form.elements.namedItem(name)]));
  const list = panel.querySelector('[data-booking-day-list]');
  const dayLabel = panel.querySelector('[data-booking-day-label]');
  const studioLabel = panel.querySelector('[data-booking-studio-label]');
  const status = panel.querySelector('[data-booking-day-status]');
  const retry = panel.querySelector('[data-booking-day-retry]');
  const conflict = panel.querySelector('[data-booking-conflict]');
  if (Object.values(fields).some(field => !field) || !list || !dayLabel || !studioLabel || !status || !retry || !conflict) return;

  const editingId = panel.dataset.editingBookingId;
  const formatter = new Intl.DateTimeFormat('ru-RU', {weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'});
  const isTime = value => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
  const isDate = value => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const parsed = new Date(`${value}T12:00:00`);
    return Number(value.slice(0, 4)) >= 1970 && Number(value.slice(0, 4)) <= 2200
      && !Number.isNaN(parsed.getTime()) && parsed.getFullYear() === Number(value.slice(0, 4))
      && parsed.getMonth() + 1 === Number(value.slice(5, 7)) && parsed.getDate() === Number(value.slice(8, 10));
  };
  const option = field => field.options[field.selectedIndex];
  const isStudio = () => /^\d+$/.test(fields.studio.value) && Boolean(option(fields.studio)) && fields.studio.validity.valid;
  const isDay = () => isDate(fields.date.value) && fields.date.validity.valid;
  const minutes = value => Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5));
  const localISO = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value)
    && isTime(value.slice(11, 16)) && Number.isFinite(Date.parse(value));
  const readBookings = values => {
    const strings = ['title', 'responsible', 'kind', 'kind_label', 'status', 'status_label', 'start_label', 'end_label'];
    if (!Array.isArray(values) || values.some(item => !item || !Number.isInteger(item.id)
      || strings.some(key => typeof item[key] !== 'string') || typeof item.is_blocking !== 'boolean'
      || !localISO(item.starts_at) || !localISO(item.ends_at) || Date.parse(item.ends_at) <= Date.parse(item.starts_at)
      || !Number.isInteger(item.start_minute) || !Number.isInteger(item.end_minute)
      || item.start_minute < 0 || item.end_minute > 1440 || item.end_minute <= item.start_minute)) {
      throw new Error('Invalid booking schedule');
    }
    return values.filter(item => !editingId || String(item.id) !== editingId);
  };

  let saved = [];
  let initialValid = false;
  try {
    saved = readBookings(JSON.parse(initialData.textContent));
    initialValid = true;
  } catch (_) { /* The server-rendered schedule remains until the request starts. */ }
  let requestedDay = panel.dataset.scheduleDate;
  let requestedStudio = panel.dataset.scheduleStudioId;
  let loadState = 'ready';
  let sequence = 0;
  let pending;
  const announceSchedule = () => {
    const snapshot = saved.slice();
    form.bookingStatusSnapshot = snapshot;
    document.dispatchEvent(new CustomEvent('booking-schedule-updated', {detail: {bookings: snapshot}}));
  };

  const buildItem = item => {
    const entry = document.createElement('li');
    entry.className = `shift-day-item booking-day-item${item.isDraft ? ' is-draft' : ''}${item.isIncomplete ? ' is-incomplete' : ''}${item.hasConflict ? ' has-conflict' : ''}${item.status === 'cancelled' ? ' is-cancelled' : ''}`;
    const time = document.createElement('span');
    time.className = 'shift-day-time';
    time.textContent = `${item.start_label} — ${item.end_label}`;
    const title = document.createElement('strong');
    title.textContent = item.title;
    const meta = document.createElement('p');
    meta.className = 'booking-day-meta';
    meta.textContent = [item.kind_label, item.responsible, item.status_label].filter(Boolean).join(' · ');
    entry.append(time, title, meta);
    if (item.isDraft) {
      const badge = document.createElement('span');
      badge.className = 'shift-draft-badge';
      badge.textContent = item.hasConflict ? 'Предпросмотр • Время занято'
        : item.hasInvalidTime ? 'Предпросмотр • Проверьте время'
        : item.isIncomplete ? 'Предпросмотр • Не сохранена'
        : editingId ? 'Изменяемая запись • Не сохранена' : 'Новая запись • Не сохранена';
      entry.append(badge);
    }
    return entry;
  };

  const render = () => {
    const day = fields.date.value;
    const validDay = isDay();
    const validStudio = isStudio();
    dayLabel.textContent = validDay ? formatter.format(new Date(`${day}T12:00:00`)) : 'Дата не выбрана';
    studioLabel.textContent = validStudio ? option(fields.studio).textContent.trim() : 'Студия не выбрана';
    retry.hidden = loadState !== 'error';
    panel.setAttribute('aria-busy', String(loadState === 'loading'));
    status.classList.toggle('is-error', loadState === 'error');
    if (!validDay) status.textContent = 'Укажите корректную дату для просмотра расписания.';
    else if (!validStudio) status.textContent = 'Выберите студию для просмотра расписания.';
    else if (loadState === 'loading') status.textContent = 'Загружаем сохранённые записи…';
    else if (loadState === 'error') status.textContent = 'Не удалось загрузить расписание. Повторите загрузку, чтобы проверить занятость студии.';
    else status.textContent = `Всего записей: ${saved.length}`;

    const entries = loadState === 'ready' && validDay && validStudio ? saved.slice() : [];
    let hasConflict = false;
    if (validDay && validStudio) {
      const validStart = isTime(fields.start_time.value) && (fields.start_time.validity?.valid ?? true);
      const validEnd = isTime(fields.end_time.value) && (fields.end_time.validity?.valid ?? true);
      const validRange = validStart && validEnd && fields.end_time.value > fields.start_time.value;
      const startMinute = validStart ? minutes(fields.start_time.value) : 9999;
      const endMinute = validEnd ? minutes(fields.end_time.value) : 9999;
      hasConflict = loadState === 'ready' && validRange && saved.some(item => item.is_blocking
        && item.start_minute < endMinute && item.end_minute > startMinute);
      const hasResponsible = Boolean(fields.responsible.value && option(fields.responsible));
      const hasKind = Boolean(fields.kind.value && option(fields.kind));
      const title = fields.title.value.trim();
      entries.push({
        title: title || (editingId ? 'Изменяемая запись' : 'Новая запись'),
        responsible: hasResponsible ? option(fields.responsible).textContent.trim() : 'Ответственный не выбран',
        kind_label: hasKind ? option(fields.kind).textContent.trim() : 'Тип записи не выбран',
        start_label: validStart ? fields.start_time.value : '--:--',
        end_label: validEnd ? fields.end_time.value : '--:--',
        start_minute: startMinute,
        end_minute: endMinute,
        isDraft: true,
        isIncomplete: !title || !hasResponsible || !hasKind || !validRange,
        hasInvalidTime: Boolean((fields.start_time.value && !validStart) || (fields.end_time.value && !validEnd)
          || (validStart && validEnd && !validRange)),
        hasConflict,
      });
    }
    conflict.hidden = !hasConflict;
    conflict.textContent = hasConflict ? 'Выбранное время пересекается с другой записью. Измените время или выберите другую студию.' : '';
    entries.sort((left, right) => left.start_minute - right.start_minute || left.end_minute - right.end_minute
      || Number(left.isDraft || false) - Number(right.isDraft || false));
    list.replaceChildren(...entries.map(buildItem));
    if (!entries.length && loadState === 'ready' && validDay && validStudio) {
      const empty = document.createElement('li');
      empty.className = 'shift-day-empty';
      empty.textContent = 'На выбранную дату записей пока нет.';
      list.append(empty);
    }
  };

  const loadSchedule = async (day, studio) => {
    if (pending) pending.abort();
    const current = ++sequence;
    saved = [];
    announceSchedule();
    if (!isDay() || !isStudio()) {
      loadState = 'invalid';
      render();
      return;
    }
    loadState = 'loading';
    render();
    pending = new AbortController();
    try {
      const url = new URL(panel.dataset.scheduleUrl, window.location.origin);
      url.searchParams.set('date', day);
      url.searchParams.set('studio', studio);
      if (editingId) url.searchParams.set('exclude', editingId);
      const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}, signal: pending.signal,
      });
      if (!response.ok) throw new Error('Unable to load booking schedule');
      const payload = await response.json();
      if (payload.date !== day || String(payload.studio) !== studio) throw new Error('Booking schedule mismatch');
      const bookings = readBookings(payload.bookings);
      if (current !== sequence) return;
      saved = bookings;
      loadState = 'ready';
      render();
      announceSchedule();
    } catch (error) {
      if (error.name === 'AbortError' || current !== sequence) return;
      saved = [];
      loadState = 'error';
      render();
      announceSchedule();
    }
  };

  const update = () => {
    if (fields.date.value !== requestedDay || fields.studio.value !== requestedStudio) {
      requestedDay = fields.date.value;
      requestedStudio = fields.studio.value;
      loadSchedule(requestedDay, requestedStudio);
    } else render();
  };
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  retry.addEventListener('click', () => loadSchedule(requestedDay, requestedStudio));
  document.addEventListener('booking-statuses-changed', () => {
    requestedDay = fields.date.value;
    requestedStudio = fields.studio.value;
    loadSchedule(requestedDay, requestedStudio);
  });
  if (!initialValid || !isDay() || !isStudio() || fields.date.value !== requestedDay || fields.studio.value !== requestedStudio) {
    requestedDay = fields.date.value;
    requestedStudio = fields.studio.value;
    loadSchedule(requestedDay, requestedStudio);
  } else {
    render();
    announceSchedule();
  }
})();
