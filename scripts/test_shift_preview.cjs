/* Run with: node --test scripts/test_shift_preview.cjs
 * Executes the shipped browser script in a small DOM adapter; no browser/dependencies.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/shift_form.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.nodeType = 1;
    this.open = false;
    this.parentElement = null;
    this.children = [];
    this.className = '';
    this.hidden = false;
    this.attributes = {};
    this.listeners = {};
    this._text = '';
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(' ').filter(Boolean));
        const add = force === undefined ? !names.has(name) : force;
        add ? names.add(name) : names.delete(name);
        this.className = [...names].join(' ');
      },
      contains: name => this.className.split(' ').includes(name),
    };
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(_value) { throw new Error('Preview content must use textContent, never HTML.'); }
  append(...children) { for (const child of children) child.parentElement = this; this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'hidden') this.hidden = true; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type) { for (const listener of this.listeners[type] || []) listener({target: this}); }
}

const saved = (id, employee, start, end, notes = '') => ({id, employee, start_time: start, end_time: end, notes});

const findWithin = (root, predicate) => {
  if (predicate(root)) return root;
  for (const child of root.children) {
    const match = findWithin(child, predicate);
    if (match) return match;
  }
  return undefined;
};
const noteOf = entry => findWithin(entry, child => child.tagName === 'P' && child.className === 'shift-note');
const nameOf = entry => findWithin(entry, child => child.tagName === 'STRONG');
const timeOf = entry => findWithin(entry, child => child.className.split(' ').includes('shift-day-time'));
const badgeOf = entry => findWithin(entry, child => child.className === 'shift-draft-badge');

function createHarness({shifts = [], date = '2026-11-02', editingId = '', initialJson, notes = '', employeeValue = '1'} = {}) {
  const employee = new Element();
  employee.options = [{value: '', textContent: 'Выберите сотрудника'}, {value: '1', textContent: 'Анна Волкова'}, {value: '2', textContent: 'Мария Белова'}];
  employee.selectedIndex = 1;
  Object.defineProperty(employee, 'value', {
    get() { return this.options[this.selectedIndex]?.value || ''; },
    set(value) { this.selectedIndex = this.options.findIndex(option => option.value === value); },
  });
  employee.value = employeeValue;
  const dateField = Object.assign(new Element(), {value: date, validity: {valid: true}});
  const start = Object.assign(new Element(), {value: '10:00'});
  const end = Object.assign(new Element(), {value: '11:00'});
  const fields = {employee, date: dateField, start_time: start, end_time: end, notes: Object.assign(new Element(), {value: notes})};
  const form = new Element();
  form.elements = {namedItem: name => fields[name]};
  const parts = Object.fromEntries(['list', 'label', 'status', 'retry'].map(key => [key, new Element()]));
  const panel = new Element();
  panel.dataset = {scheduleDate: date, scheduleUrl: '/radio/shifts/day/', editingShiftId: String(editingId)};
  panel.querySelector = selector => {
    const key = {'[data-shift-day-list]': 'list', '[data-shift-day-label]': 'label', '[data-shift-day-status]': 'status', '[data-shift-day-retry]': 'retry'}[selector];
    return parts[key] || null;
  };
  const embedded = Object.assign(new Element(), {textContent: initialJson ?? JSON.stringify(shifts)});
  const requests = [];
  const fetch = (url, options) => new Promise((resolve, reject) => {
    // Intentionally do not reject on abort: stale-response protection must also work
    // when a network layer resolves a request after its cancellation.
    requests.push({url: String(url), options, resolve, reject});
  });
  vm.runInNewContext(source, {
    document: {
      querySelector: selector => selector === '[data-shift-form]' ? form : selector === '[data-shift-day-schedule]' ? panel : null,
      getElementById: id => id === 'shift-day-schedule-data' ? embedded : null,
      createElement: tagName => new Element(tagName),
    },
    window: {location: {origin: 'http://localhost:8000'}},
    fetch, AbortController, URL, Intl, Date,
  }, {filename: 'static/js/shift_form.js'});
  return {
    fields, form, panel, parts, requests,
    change(values, event = 'input') {
      for (const [name, value] of Object.entries(values)) fields[name].value = value;
      form.dispatch(event);
    },
    entries: () => parts.list.children.filter(child => child.className.split(' ').includes('shift-day-item')),
    drafts: () => parts.list.children.filter(child => child.className.split(' ').includes('is-draft')),
    async respond(index, day, values, ok = true) {
      requests[index].resolve({ok, json: async () => ({date: day, shifts: values})});
      await flush();
    },
  };
}

test('employee and time edits immediately update a single chronologically placed unsaved draft', () => {
  const h = createHarness({shifts: [saved(1, 'Ранний эфир', '08:00', '09:00'), saved(2, 'Поздний эфир', '12:00', '13:00')]});
  assert.deepEqual(h.entries().map(entry => nameOf(entry).textContent), ['Ранний эфир', 'Анна Волкова', 'Поздний эфир']);
  assert.match(h.drafts()[0].textContent, /Новый эфир.*Не сохранён/);
  h.change({employee: '2', start_time: '07:00', end_time: '08:00'}, 'change');
  assert.equal(h.drafts().length, 1);
  assert.equal(nameOf(h.entries()[0]).textContent, 'Мария Белова');
  assert.match(h.entries()[0].textContent, /07:00 — 08:00/);
  assert.equal(h.requests.length, 0, 'same-date input changes must not refetch saved entries');
});

test('missing or invalid intervals remain incomplete previews while saved entries stay visible', () => {
  const h = createHarness({shifts: [saved(1, 'Сохранённый эфир', '08:00', '09:00')]});
  for (const values of [
    {employee: '1', start_time: '10:00', end_time: '10:00'},
    {employee: '1', start_time: '11:00', end_time: '10:00'},
    {employee: '1', start_time: '', end_time: '12:00'},
    {employee: '1', start_time: '25:00', end_time: '26:00'},
    {employee: '', start_time: '10:00', end_time: '11:00'},
  ]) {
    h.change(values);
    assert.equal(h.drafts().length, 1, 'incomplete input keeps a live draft visible');
    assert.equal(h.drafts()[0].classList.contains('is-incomplete'), true);
    const badge = badgeOf(h.drafts()[0]);
    assert.match(badge.textContent, /^Предпросмотр/);
    assert.equal(h.entries().length, 2, 'saved entries remain alongside the incomplete preview');
    assert.equal(nameOf(h.entries().filter(entry => !entry.classList.contains('is-draft'))[0]).textContent, 'Сохранённый эфир');
  }
});

test('date edits fetch the selected date and exclude the edited saved shift', async () => {
  const h = createHarness({editingId: '42', shifts: [saved(42, 'Старый вариант', '10:00', '11:00'), saved(7, 'Другой эфир', '08:00', '09:00')]});
  assert.equal(h.entries().length, 2, 'edited shift is represented once as the draft');
  assert.match(h.drafts()[0].textContent, /Изменяемый эфир/);
  h.change({date: '2026-11-03'});
  assert.equal(h.requests.length, 1);
  const request = h.requests[0];
  const url = new URL(request.url);
  assert.equal(url.pathname, '/radio/shifts/day/');
  assert.equal(url.searchParams.get('date'), '2026-11-03');
  assert.equal(url.searchParams.get('exclude'), '42');
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal(request.options.cache, 'no-store');
  assert.equal(h.panel.attributes['aria-busy'], 'true');
  assert.equal(h.entries().length, 1, 'stale saved entries disappear while loading; draft stays');
  await h.respond(0, '2026-11-03', [saved(9, 'Новый день', '07:00', '09:00')]);
  assert.equal(h.panel.attributes['aria-busy'], 'false');
  assert.equal(nameOf(h.entries()[0]).textContent, 'Новый день');
  assert.equal(h.parts.status.textContent, 'Всего эфиров: 1');
  assert.match(h.parts.label.textContent, /3 ноября/);
});

test('late responses cannot replace the most recently requested day', async () => {
  const h = createHarness();
  h.change({date: '2026-11-03'});
  h.change({date: '2026-11-04'});
  assert.equal(h.requests[0].options.signal.aborted, true);
  await h.respond(1, '2026-11-04', [saved(2, 'Актуальный день', '09:00', '10:00')]);
  await h.respond(0, '2026-11-03', [saved(1, 'Устаревший ответ', '07:00', '08:00')]);
  assert.match(h.parts.list.textContent, /Актуальный день/);
  assert.doesNotMatch(h.parts.list.textContent, /Устаревший ответ/);
  assert.match(h.parts.label.textContent, /4 ноября/);
});

test('failed loading clears stale saved entries and retry recovers the selected day', async () => {
  const h = createHarness({shifts: [saved(1, 'Устаревшая запись', '08:00', '09:00')]});
  h.change({date: '2026-11-03'});
  h.requests[0].reject(new Error('Network unavailable'));
  await flush();
  assert.doesNotMatch(h.parts.list.textContent, /Устаревшая запись/);
  assert.match(h.parts.status.textContent, /Не удалось загрузить/);
  assert.equal(h.parts.status.classList.contains('is-error'), true);
  assert.equal(h.parts.retry.hidden, false);
  assert.equal(h.drafts().length, 1, 'the local unsaved draft is still available');
  h.parts.retry.dispatch('click');
  assert.equal(h.requests.length, 2);
  assert.equal(new URL(h.requests[1].url).searchParams.get('date'), '2026-11-03');
  await h.respond(1, '2026-11-03', [saved(2, 'Загружено после повтора', '09:00', '10:00')]);
  assert.match(h.parts.list.textContent, /Загружено после повтора/);
  assert.equal(h.parts.retry.hidden, true);
  assert.equal(h.parts.status.classList.contains('is-error'), false);
});

test('invalid dates clear the preview without requesting an invalid endpoint', () => {
  const h = createHarness({shifts: [saved(1, 'Вчерашняя запись', '08:00', '09:00')]});
  h.change({date: '2026-02-30'});
  assert.equal(h.requests.length, 0);
  assert.equal(h.entries().length, 0);
  assert.equal(h.parts.label.textContent, 'Дата не выбрана');
  assert.match(h.parts.status.textContent, /корректную дату/);
});

test('mismatched dates and malformed endpoint payloads produce retryable errors', async () => {
  const h = createHarness();
  h.change({date: '2026-11-03'});
  await h.respond(0, '2026-11-02', [saved(1, 'Чужой день', '08:00', '09:00')]);
  assert.doesNotMatch(h.parts.list.textContent, /Чужой день/);
  assert.equal(h.parts.retry.hidden, false);
  h.parts.retry.dispatch('click');
  await h.respond(1, '2026-11-03', [{employee: 'Ошибка', start_time: 'invalid', end_time: '09:00'}]);
  assert.equal(h.parts.retry.hidden, false);
  assert.equal(h.entries().length, 1);
  assert.equal(h.drafts().length, 1);
});


test('multiline notes update live and remain literal text for saved, draft and fetched shifts', async () => {
  const literal = '<img src=x onerror=alert(1)>\nВопросы гостю';
  const h = createHarness({
    shifts: [saved(1, 'Утренний эфир', '08:00', '09:00', literal)],
    notes: 'Первый вопрос\nВторой вопрос',
  });
  assert.equal(noteOf(h.entries()[0]).textContent, literal);
  assert.equal(noteOf(h.entries()[0]).parentElement.className, 'shift-note-popup');
  assert.equal(noteOf(h.entries()[0]).parentElement.parentElement, h.entries()[0]);
  assert.equal(noteOf(h.entries()[0]).parentElement.hidden, true, 'saved note text starts in a hidden popup');
  assert.equal(noteOf(h.entries()[0]).parentElement.attributes.role, 'tooltip');
  assert.equal(h.entries()[0].attributes['data-shift-note'], '');
  const noteTrigger = findWithin(h.entries()[0], child => 'data-shift-note-trigger' in child.attributes);
  assert.equal(noteTrigger.attributes.role, 'button');
  assert.equal(noteTrigger.attributes.tabindex, '0');
  assert.equal(noteTrigger.attributes['aria-expanded'], 'false');
  assert.equal(findWithin(h.entries()[0], child => child.tagName === 'SUMMARY' || child.tagName === 'DETAILS'), undefined);
  assert.equal(findWithin(h.entries()[0], child => 'data-shift-note-close' in child.attributes), undefined);
  assert.equal(noteOf(h.entries()[0]).children.length, 0, 'saved note markup is not interpreted');
  assert.equal(noteOf(h.drafts()[0]).textContent, 'Первый вопрос\nВторой вопрос');
  assert.equal(noteOf(h.drafts()[0]).parentElement.hidden, true, 'new draft note stays hidden until requested');
  h.change({notes: `  ${literal}  `});
  assert.equal(noteOf(h.drafts()[0]).textContent, literal, 'draft trims surrounding whitespace and retains line breaks');
  assert.equal(noteOf(h.drafts()[0]).children.length, 0, 'draft note markup is not interpreted');
  assert.equal(h.drafts().length, 1);
  assert.equal(h.requests.length, 0, 'notes edit must not refetch the saved schedule');
  h.change({date: '2026-11-03'});
  await h.respond(0, '2026-11-03', [saved(2, 'Другой день', '08:00', '09:00', literal)]);
  assert.equal(noteOf(h.entries()[0]).textContent, literal);
  assert.equal(noteOf(h.entries()[0]).children.length, 0);
});

test('empty notes add no blank paragraph and legacy saved entries without notes still render', () => {
  const h = createHarness({shifts: [
    {id: 1, employee: 'Прежний эфир', start_time: '08:00', end_time: '09:00'},
    saved(2, 'Без примечания', '12:00', '13:00', ' \n '),
  ]});
  assert.equal(h.entries().length, 3);
  assert.equal(h.entries().filter(noteOf).length, 0);
  for (const entry of h.entries()) {
    assert.equal(entry.attributes['data-shift-note'], undefined);
    assert.equal(findWithin(entry, child => 'data-shift-note-trigger' in child.attributes), undefined);
  }
  h.change({notes: 'План эфира'});
  assert.equal(noteOf(h.drafts()[0]).textContent, 'План эфира');
  h.change({notes: ' \n '});
  assert.equal(noteOf(h.drafts()[0]), undefined);
  assert.equal(h.entries().filter(noteOf).length, 0);
});


test('a new form with no employee immediately shows one draft and edits complete that same preview', () => {
  const h = createHarness({employeeValue: '', shifts: [saved(1, 'Сохранённый эфир', '08:00', '09:00')]});
  assert.equal(h.drafts().length, 1);
  let draft = h.drafts()[0];
  assert.equal(nameOf(draft).textContent, 'Новый эфир');
  assert.equal(timeOf(draft).textContent, '10:00 — 11:00');
  assert.equal(draft.classList.contains('is-incomplete'), true);
  assert.equal(badgeOf(draft).textContent, 'Предпросмотр • Не сохранён');
  h.change({employee: '2', start_time: '07:00', end_time: '08:00', notes: 'Вопрос гостю\nМузыкальная пауза'}, 'change');
  assert.equal(h.drafts().length, 1);
  draft = h.drafts()[0];
  assert.equal(nameOf(draft).textContent, 'Мария Белова');
  assert.equal(draft.classList.contains('is-incomplete'), false);
  assert.equal(h.entries()[0], draft, 'completed preview moves to its chronological position');
  assert.equal(noteOf(draft).textContent, 'Вопрос гостю\nМузыкальная пауза');
  assert.equal(badgeOf(draft).textContent, 'Новый эфир • Не сохранён');
  assert.equal(h.requests.length, 0);
});

test('missing times use neutral placeholders, sort last, and never appear as a valid edit', () => {
  const h = createHarness({employeeValue: '', editingId: '42', shifts: [saved(1, 'Вечерний эфир', '18:00', '19:00')]});
  h.change({start_time: '25:00', end_time: ''});
  assert.equal(h.drafts().length, 1);
  const draft = h.drafts()[0];
  assert.equal(timeOf(draft).textContent, '--:-- — --:--');
  assert.equal(nameOf(draft).textContent, 'Изменяемый эфир');
  assert.equal(draft.classList.contains('is-incomplete'), true);
  assert.equal(h.entries().at(-1), draft);
  h.change({employee: '1', start_time: '13:00', end_time: '12:00'});
  assert.equal(h.drafts().length, 1);
  assert.equal(badgeOf(h.drafts()[0]).textContent, 'Предпросмотр • Проверьте время');
  h.change({date: '2026-02-30'});
  assert.equal(h.drafts().length, 0, 'an invalid day cannot host a preview');
  assert.equal(h.entries().length, 0);
  assert.equal(h.requests.length, 0);
});
