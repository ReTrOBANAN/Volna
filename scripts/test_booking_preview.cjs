/* Run with: node --test scripts/test_booking_preview.cjs
 * Executes the shipped booking form controller against a small dependency-free DOM.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/booking_form.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const DAY = '2026-11-03';
const minute = value => Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5));

class Element {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.className = '';
    this.hidden = false;
    this.attributes = {};
    this.listeners = {};
    this.validity = {valid: true};
    this._text = '';
    this.classList = {
      contains: name => this.className.split(' ').includes(name),
      toggle: (name, force) => {
        const classes = new Set(this.className.split(' ').filter(Boolean));
        const add = force === undefined ? !classes.has(name) : force;
        add ? classes.add(name) : classes.delete(name);
        this.className = [...classes].join(' ');
      },
    };
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(_value) { throw new Error('Booking preview must render untrusted values with textContent.'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  dispatch(type) { for (const callback of this.listeners[type] || []) callback({target: this}); }
}

function selectField(values, selected) {
  const field = new Element('select');
  field.options = values.map(([value, textContent]) => ({value, textContent}));
  Object.defineProperty(field, 'value', {
    get() { return this.options[this.selectedIndex]?.value || ''; },
    set(value) { this.selectedIndex = this.options.findIndex(item => item.value === String(value)); },
  });
  field.value = selected;
  return field;
}

function saved(id, title, start, end, overrides = {}) {
  const day = overrides.day || DAY;
  const startDay = overrides.startDay || day;
  const endDay = overrides.endDay || day;
  const status = overrides.status || 'planned';
  const label = (date, value) => `${date === day ? '' : `${date.slice(8, 10)}.${date.slice(5, 7)} · `}${value}`;
  return {
    id, title, responsible: 'Анна Волкова', responsible_id: 1,
    kind: 'audio', kind_label: 'Без видео', status,
    status_label: {planned: 'Запланировано', recording: 'Идёт запись', completed: 'Завершено', cancelled: 'Отменено'}[status],
    starts_at: `${startDay}T${start}:00+07:00`, ends_at: `${endDay}T${end}:00+07:00`,
    start_time: start, end_time: end, start_label: label(startDay, start), end_label: label(endDay, end),
    start_minute: startDay < day ? 0 : minute(start), end_minute: endDay > day ? 1440 : minute(end),
    is_blocking: status === 'planned' || status === 'recording', ...overrides,
  };
}

function createHarness({bookings = [], date = DAY, studio = '1', editingId = '', title = '', responsible = '1', start = '10:00', end = '13:00'} = {}) {
  const fields = {
    studio: selectField([['', 'Выберите студию'], ['1', 'Студия №1'], ['2', 'Студия №2']], studio),
    responsible: selectField([['', 'Выберите ответственного'], ['1', 'Анна Волкова'], ['2', 'Мария Белова']], responsible),
    kind: selectField([['', 'Выберите тип'], ['audio', 'Без видео'], ['video', 'С видео']], 'audio'),
    date: Object.assign(new Element('input'), {value: date}),
    start_time: Object.assign(new Element('input'), {value: start}),
    end_time: Object.assign(new Element('input'), {value: end}),
    title: Object.assign(new Element('input'), {value: title}),
  };
  const form = new Element('form');
  form.elements = {namedItem: name => fields[name]};
  const parts = Object.fromEntries(['list', 'day-label', 'studio-label', 'status', 'retry', 'conflict'].map(key => [key, new Element()]));
  const panel = new Element('aside');
  panel.dataset = {scheduleDate: date, scheduleStudioId: String(studio), scheduleUrl: '/studios/day/', editingBookingId: String(editingId)};
  const selectors = {'[data-booking-day-list]': 'list', '[data-booking-day-label]': 'day-label', '[data-booking-studio-label]': 'studio-label', '[data-booking-day-status]': 'status', '[data-booking-day-retry]': 'retry', '[data-booking-conflict]': 'conflict'};
  panel.querySelector = selector => parts[selectors[selector]] || null;
  const embedded = Object.assign(new Element('script'), {textContent: JSON.stringify(bookings)});
  const requests = [];
  const fetch = (url, options) => new Promise((resolve, reject) => requests.push({url: String(url), options, resolve, reject}));
  vm.runInNewContext(source, {
    document: {
      querySelector: selector => selector === '[data-booking-form]' ? form : selector === '[data-booking-day-schedule]' ? panel : null,
      getElementById: id => id === 'booking-day-schedule-data' ? embedded : null,
      createElement: tag => new Element(tag),
    },
    window: {location: {origin: 'http://localhost:8000'}}, fetch, AbortController, URL, Intl, Date,
  }, {filename: 'static/js/booking_form.js'});
  return {
    fields, form, panel, parts, requests,
    change(values, event = 'input') {
      for (const [name, value] of Object.entries(values)) fields[name].value = value;
      form.dispatch(event);
    },
    entries: () => parts.list.children.filter(child => child.classList.contains('booking-day-item')),
    drafts: () => parts.list.children.filter(child => child.classList.contains('is-draft')),
    async respond(index, {date: day = DAY, studio: studioId = 1, bookings: rows = [], ok = true} = {}) {
      requests[index].resolve({ok, json: async () => ({date: day, studio: studioId, bookings: rows})});
      await flush();
    },
  };
}

const titleOf = entry => entry.children.find(child => child.tagName === 'STRONG');
const metaOf = entry => entry.children.find(child => child.className === 'booking-day-meta');
const timeOf = entry => entry.children.find(child => child.className === 'shift-day-time');
const badgeOf = entry => entry.children.find(child => child.className === 'shift-draft-badge');

test('an initially incomplete draft updates title, responsible, kind and time live using literal text', () => {
  const literal = '<img src=x onerror=alert(1)> Интервью';
  const h = createHarness({responsible: '', bookings: [saved(1, literal, '08:00', '09:00', {responsible: '<b>Ответственный</b>'})]});
  assert.equal(h.drafts().length, 1);
  assert.equal(titleOf(h.drafts()[0]).textContent, 'Новая запись');
  assert.equal(h.drafts()[0].classList.contains('is-incomplete'), true);
  assert.match(metaOf(h.drafts()[0]).textContent, /Ответственный не выбран/);
  assert.equal(titleOf(h.entries()[0]).textContent, literal);
  assert.equal(titleOf(h.entries()[0]).children.length, 0);
  assert.match(metaOf(h.entries()[0]).textContent, /<b>Ответственный<\/b>/);
  assert.equal(metaOf(h.entries()[0]).children.length, 0);
  h.change({title: ` ${literal} `, responsible: '2', kind: 'video', start_time: '07:00', end_time: '08:00'}, 'change');
  const draft = h.drafts()[0];
  assert.equal(h.drafts().length, 1);
  assert.equal(h.entries()[0], draft);
  assert.equal(titleOf(draft).textContent, literal);
  assert.equal(titleOf(draft).children.length, 0);
  assert.equal(draft.classList.contains('is-incomplete'), false);
  assert.match(metaOf(draft).textContent, /С видео · Мария Белова/);
  assert.equal(h.requests.length, 0);
});

test('same-day invalid ranges remain incomplete and invalid day or studio clears the preview', () => {
  const h = createHarness({title: 'Подкаст'});
  assert.equal('end_date' in h.fields, false, 'the form has no end-date input');
  for (const values of [{start_time: '', end_time: '13:00'}, {start_time: '25:00', end_time: '26:00'}, {start_time: '10:00', end_time: '10:00'}, {start_time: '13:00', end_time: '12:00'}]) {
    h.change(values);
    assert.equal(h.drafts().length, 1);
    assert.equal(h.drafts()[0].classList.contains('is-incomplete'), true);
    assert.equal(h.parts.conflict.hidden, true);
  }
  h.change({start_time: '', end_time: ''});
  assert.equal(timeOf(h.drafts()[0]).textContent, '--:-- — --:--');
  h.change({start_time: '23:00', end_time: '01:00'});
  assert.equal(h.drafts()[0].classList.contains('is-incomplete'), true);
  assert.equal(timeOf(h.drafts()[0]).textContent, '23:00 — 01:00');
  assert.match(badgeOf(h.drafts()[0]).textContent, /Проверьте время/);
  assert.equal(h.parts.conflict.hidden, true);
  h.change({start_time: '22:00', end_time: '23:00'});
  assert.equal(h.drafts()[0].classList.contains('is-incomplete'), false);
  for (const values of [{date: '2026-02-30'}, {studio: ''}]) {
    const invalid = createHarness({bookings: [saved(1, 'Сохранено', '08:00', '09:00')]});
    invalid.change(values);
    assert.equal(invalid.entries().length, 0);
    assert.equal(invalid.requests.length, 0);
  }
});

test('changing day or studio fetches the matching schedule with edited-record exclusion', async () => {
  const h = createHarness({editingId: '42', title: 'Изменяемая', bookings: [saved(42, 'Старый вариант', '10:00', '11:00')]});
  assert.equal(h.entries().length, 1, 'the original edited record is represented only by its draft');
  h.change({studio: '2'});
  const url = new URL(h.requests[0].url);
  assert.equal(url.pathname, '/studios/day/');
  assert.equal(url.searchParams.get('date'), DAY);
  assert.equal(url.searchParams.get('studio'), '2');
  assert.equal(url.searchParams.get('exclude'), '42');
  assert.equal(h.requests[0].options.credentials, 'same-origin');
  assert.equal(h.requests[0].options.cache, 'no-store');
  assert.equal(h.panel.attributes['aria-busy'], 'true');
  assert.equal(h.entries().length, 1);
  await h.respond(0, {studio: 2, bookings: [saved(7, 'Во второй студии', '07:00', '08:00')]});
  assert.equal(titleOf(h.entries()[0]).textContent, 'Во второй студии');
  assert.equal(h.parts['studio-label'].textContent, 'Студия №2');
  assert.equal(h.panel.attributes['aria-busy'], 'false');
  h.change({date: '2026-11-04'});
  assert.equal(new URL(h.requests[1].url).searchParams.get('date'), '2026-11-04');
  await h.respond(1, {date: '2026-11-04', studio: 2});
  assert.match(h.parts['day-label'].textContent, /4 ноября/);
});

test('late responses or abort errors cannot replace the latest date-studio pair', async () => {
  for (const abortOld of [false, true]) {
    const h = createHarness({title: 'Черновик'});
    h.change({date: '2026-11-04'});
    h.change({studio: '2'});
    assert.equal(h.requests[0].options.signal.aborted, true);
    await h.respond(1, {date: '2026-11-04', studio: 2, bookings: [saved(9, 'Актуальная студия', '08:00', '09:00', {day: '2026-11-04'})]});
    if (abortOld) {
      h.requests[0].reject(Object.assign(new Error('Request aborted'), {name: 'AbortError'}));
      await flush();
    } else {
      await h.respond(0, {date: '2026-11-04', studio: 1, bookings: [saved(8, 'Устаревшая студия', '07:00', '08:00', {day: '2026-11-04'})]});
    }
    assert.match(h.parts.list.textContent, /Актуальная студия/);
    assert.doesNotMatch(h.parts.list.textContent, /Устаревшая студия/);
    assert.equal(h.parts['studio-label'].textContent, 'Студия №2');
    assert.equal(h.drafts().length, 1);
  }
});

test('loading failures clear stale slots and retry preserves the local draft', async () => {
  const h = createHarness({title: 'Мой черновик', bookings: [saved(1, 'Старая запись', '10:00', '11:00')]});
  assert.equal(h.parts.conflict.hidden, false);
  h.change({studio: '2'});
  h.requests[0].reject(new Error('Network unavailable'));
  await flush();
  assert.doesNotMatch(h.parts.list.textContent, /Старая запись/);
  assert.equal(h.parts.status.classList.contains('is-error'), true);
  assert.equal(h.parts.retry.hidden, false);
  assert.equal(h.parts.conflict.hidden, true, 'unverified stale data cannot claim a conflict');
  h.change({title: 'Локальная правка'});
  assert.equal(titleOf(h.drafts()[0]).textContent, 'Локальная правка');
  assert.equal(h.requests.length, 1);
  h.parts.retry.dispatch('click');
  assert.equal(new URL(h.requests[1].url).searchParams.get('studio'), '2');
  await h.respond(1, {studio: 2});
  assert.equal(h.parts.retry.hidden, true);
  assert.equal(h.parts.status.classList.contains('is-error'), false);
  assert.equal(titleOf(h.drafts()[0]).textContent, 'Локальная правка');
});

test('date, studio and malformed-row mismatches produce retryable errors without hiding the draft', async () => {
  const h = createHarness({title: 'Не потерять'});
  h.change({studio: '2'});
  const invalid = [
    {date: '2026-11-04', studio: 2},
    {studio: 1},
    {studio: 2, bookings: [saved(1, 'Ошибка диапазона', '08:00', '09:00', {end_minute: 0})]},
    {studio: 2, bookings: 'not-an-array'},
  ];
  for (let index = 0; index < invalid.length; index++) {
    if (index) h.parts.retry.dispatch('click');
    await h.respond(index, invalid[index]);
    assert.equal(h.parts.retry.hidden, false);
    assert.equal(h.parts.status.classList.contains('is-error'), true);
    assert.equal(h.entries().length, 1);
    assert.equal(titleOf(h.drafts()[0]).textContent, 'Не потерять');
  }
});

test('conflicts use blocking intervals including legacy seconds, while adjacent and inactive slots remain free', async () => {
  const h = createHarness({title: 'Проверка времени', bookings: [
    saved(1, 'Запланировано', '10:00', '11:00'),
    saved(2, 'Отменено', '12:00', '13:00', {status: 'cancelled'}),
    saved(3, 'Завершено', '14:00', '15:00', {status: 'completed'}),
    saved(4, 'Идёт запись', '16:00', '17:00', {status: 'recording'}),
    saved(5, 'Исторические секунды', '09:00', '09:00', {
      starts_at: `${DAY}T09:00:15+07:00`, ends_at: `${DAY}T09:00:30+07:00`, start_minute: 540, end_minute: 541,
    }),
  ]});
  const cases = [['09:00', '09:01', true], ['08:59', '09:00', false], ['09:01', '09:30', false],
    ['10:30', '11:30', true], ['11:00', '12:00', false], ['12:00', '13:00', false], ['14:00', '15:00', false], ['16:30', '17:30', true]];
  for (const [start, end, expected] of cases) {
    h.change({start_time: start, end_time: end});
    assert.equal(h.drafts()[0].classList.contains('has-conflict'), expected, `${start}–${end}`);
    assert.equal(h.parts.conflict.hidden, !expected);
    if (expected) assert.match(h.parts.conflict.textContent, /пересекается с другой записью/);
  }
  h.change({studio: '2'});
  await h.respond(0, {studio: 2});
  assert.equal(h.parts.conflict.hidden, true);
});

test('legacy carry-in intervals remain readable while new drafts must end the same day', () => {
  const h = createHarness({title: 'Ночная запись', start: '00:30', end: '01:30', bookings: [
    saved(1, 'С прошлой ночи', '23:00', '01:00', {startDay: '2026-11-02'}),
    saved(2, 'До следующего дня', '23:00', '01:00', {endDay: '2026-11-04'}),
  ]});
  assert.equal(h.parts.conflict.hidden, false);
  assert.match(timeOf(h.entries()[0]).textContent, /02\.11 · 23:00/);
  h.change({start_time: '23:00', end_time: '23:30'});
  assert.equal(h.parts.conflict.hidden, false);
  h.change({start_time: '21:00', end_time: '23:00'});
  assert.equal(h.parts.conflict.hidden, true);
  h.change({start_time: '23:00', end_time: '01:00'});
  assert.equal(h.drafts()[0].classList.contains('is-incomplete'), true);
  assert.equal(h.parts.conflict.hidden, true, 'an invalid backwards interval is never treated as a valid reservation');
});

test('editing excludes the original booking from both duplicate display and conflict checks', () => {
  const h = createHarness({editingId: '42', title: 'Правка', start: '10:00', end: '11:00', bookings: [
    saved(42, 'Собственная запись', '10:00', '11:00'), saved(99, 'Другая запись', '13:00', '14:00'),
  ]});
  assert.equal(h.entries().length, 2);
  assert.doesNotMatch(h.parts.list.textContent, /Собственная запись/);
  assert.equal(h.parts.conflict.hidden, true);
  assert.match(badgeOf(h.drafts()[0]).textContent, /Изменяемая запись/);
  h.change({start_time: '13:00', end_time: '14:00'});
  assert.equal(h.parts.conflict.hidden, false);
});
