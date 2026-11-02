(() => {
  const form = document.querySelector('[data-shift-form]');
  const panel = document.querySelector('[data-shift-day-schedule]');
  const initialData = document.getElementById('shift-day-schedule-data');
  if (!form || !panel || !initialData) return;

  const employeeField = form.elements.namedItem('employee');
  const dateField = form.elements.namedItem('date');
  const startField = form.elements.namedItem('start_time');
  const endField = form.elements.namedItem('end_time');
  const notesField = form.elements.namedItem('notes');
  const list = panel.querySelector('[data-shift-day-list]');
  const dayLabel = panel.querySelector('[data-shift-day-label]');
  const status = panel.querySelector('[data-shift-day-status]');
  const retryButton = panel.querySelector('[data-shift-day-retry]');
  if (!employeeField || !dateField || !startField || !endField || !list || !dayLabel || !status || !retryButton) return;

  const editingId = panel.dataset.editingShiftId;
  const draftLabel = editingId ? 'Изменяемый эфир • Не сохранён' : 'Новый эфир • Не сохранён';
  const dayFormatter = new Intl.DateTimeFormat('ru-RU', {weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'});
  const isTime = value => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
  const isDate = value => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const year = Number(value.slice(0, 4));
    if (year < 1970 || year > 2200) return false;
    const parsed = new Date(`${value}T12:00:00`);
    return !Number.isNaN(parsed.getTime()) && parsed.getFullYear() === Number(value.slice(0, 4)) && parsed.getMonth() + 1 === Number(value.slice(5, 7)) && parsed.getDate() === Number(value.slice(8, 10));
  };
  const readShifts = values => {
    if (!Array.isArray(values) || values.some(item => !item || typeof item.employee !== 'string' || !isTime(item.start_time) || !isTime(item.end_time) || (item.notes !== undefined && typeof item.notes !== 'string'))) {
      throw new Error('Invalid schedule response');
    }
    return values.filter(item => !editingId || String(item.id) !== editingId);
  };

  let savedShifts = [];
  let initialDataValid = false;
  try {
    savedShifts = readShifts(JSON.parse(initialData.textContent));
    initialDataValid = true;
  } catch (_) { /* The server-rendered list remains available until the API returns. */ }
  let requestedDate = panel.dataset.scheduleDate;
  let loadState = 'ready';
  let requestSequence = 0;
  let pendingRequest;

  const buildItem = item => {
    const entry = document.createElement('li');
    entry.className = `shift-day-item${item.isDraft ? ' is-draft' : ''}${item.isIncomplete ? ' is-incomplete' : ''}`;
    const time = document.createElement('span');
    time.className = 'shift-day-time';
    time.textContent = `${item.start_time || '--:--'} — ${item.end_time || '--:--'}`;
    const name = document.createElement('strong');
    name.textContent = item.employee;
    let content = entry;
    if (item.notes && item.notes.trim()) {
      entry.setAttribute('data-shift-note', '');
      const trigger = document.createElement('div');
      trigger.className = 'shift-note-trigger';
      trigger.setAttribute('data-shift-note-trigger', '');
      trigger.setAttribute('role', 'button');
      trigger.setAttribute('tabindex', '0');
      trigger.setAttribute('aria-expanded', 'false');
      trigger.setAttribute('aria-label', `Показать примечание к эфиру ${item.employee}`);
      trigger.append(time, name);
      content = trigger;
      const popup = document.createElement('div');
      popup.className = 'shift-note-popup';
      popup.setAttribute('data-shift-note-popup', '');
      popup.setAttribute('role', 'tooltip');
      popup.hidden = true;
      const note = document.createElement('p');
      note.className = 'shift-note';
      note.textContent = item.notes;
      popup.append(note);
      entry.append(trigger, popup);
    } else entry.append(time, name);
    if (item.isDraft) {
      const label = document.createElement('span');
      label.className = 'shift-draft-badge';
      label.textContent = item.hasInvalidTime ? 'Предпросмотр • Проверьте время'
        : item.isIncomplete ? 'Предпросмотр • Не сохранён' : draftLabel;
      content.append(label);
    }
    return entry;
  };

  const render = () => {
    const day = dateField.value;
    const validDay = isDate(day) && dateField.validity.valid;
    dayLabel.textContent = validDay ? dayFormatter.format(new Date(`${day}T12:00:00`)) : 'Дата не выбрана';
    retryButton.hidden = loadState !== 'error';
    panel.setAttribute('aria-busy', String(loadState === 'loading'));
    status.classList.toggle('is-error', loadState === 'error');
    if (loadState === 'loading') status.textContent = 'Загружаем сохранённые эфиры…';
    else if (loadState === 'error') status.textContent = 'Не удалось загрузить расписание. Повторите загрузку, чтобы свериться с сохранёнными эфирами.';
    else if (loadState === 'invalid') status.textContent = 'Укажите корректную дату для просмотра расписания.';
    else status.textContent = `Всего эфиров: ${savedShifts.length}`;

    const entries = loadState === 'ready' ? savedShifts.slice() : [];
    if (validDay) {
      const selectedEmployee = employeeField.options[employeeField.selectedIndex];
      const hasEmployee = Boolean(employeeField.value && selectedEmployee);
      const validStart = isTime(startField.value) && (startField.validity?.valid ?? true);
      const validEnd = isTime(endField.value) && (endField.validity?.valid ?? true);
      const validRange = validStart && validEnd && endField.value > startField.value;
      entries.push({
        id: editingId || '',
        employee: hasEmployee ? selectedEmployee.textContent.trim() : editingId ? 'Изменяемый эфир' : 'Новый эфир',
        start_time: validStart ? startField.value : '',
        end_time: validEnd ? endField.value : '',
        notes: notesField ? notesField.value.trim() : '',
        isDraft: true,
        isIncomplete: !hasEmployee || !validRange,
        hasInvalidTime: Boolean((startField.value && !validStart) || (endField.value && !validEnd) || (validStart && validEnd && !validRange)),
      });
    }
    entries.sort((left, right) => (left.start_time || '99:99').localeCompare(right.start_time || '99:99') || (left.end_time || '99:99').localeCompare(right.end_time || '99:99') || Number(left.isDraft || false) - Number(right.isDraft || false));
    list.replaceChildren(...entries.map(buildItem));
    if (!entries.length && loadState === 'ready') {
      const empty = document.createElement('li');
      empty.className = 'shift-day-empty';
      empty.textContent = 'На выбранную дату эфиров пока нет.';
      list.append(empty);
    }
  };

  const loadSchedule = async day => {
    if (pendingRequest) pendingRequest.abort();
    const currentRequest = ++requestSequence;
    savedShifts = [];
    if (!isDate(day) || !dateField.validity.valid) {
      loadState = 'invalid';
      render();
      return;
    }
    loadState = 'loading';
    render();
    pendingRequest = new AbortController();
    try {
      const url = new URL(panel.dataset.scheduleUrl, window.location.origin);
      url.searchParams.set('date', day);
      if (editingId) url.searchParams.set('exclude', editingId);
      const response = await fetch(url, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'application/json'},
        signal: pendingRequest.signal,
      });
      if (!response.ok) throw new Error('Unable to load schedule');
      const payload = await response.json();
      if (payload.date !== day) throw new Error('Schedule date mismatch');
      const shifts = readShifts(payload.shifts);
      if (currentRequest !== requestSequence) return;
      savedShifts = shifts;
      loadState = 'ready';
      render();
    } catch (error) {
      if (error.name === 'AbortError' || currentRequest !== requestSequence) return;
      savedShifts = [];
      loadState = 'error';
      render();
    }
  };

  const update = () => {
    if (dateField.value !== requestedDate) {
      requestedDate = dateField.value;
      loadSchedule(requestedDate);
    } else render();
  };
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  retryButton.addEventListener('click', () => loadSchedule(requestedDate));
  if (!initialDataValid || dateField.value !== requestedDate) {
    requestedDate = dateField.value;
    loadSchedule(requestedDate);
  } else render();
})();
