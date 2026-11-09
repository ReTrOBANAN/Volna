/* Run with: node --test scripts/test_shift_calendar.cjs
 * Executes the shipped calendar controller with asynchronous responses and DOM adapters.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/shift_calendar.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor() { this.listeners = {}; this.attributes = {}; this.childNodes = []; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type) {
    const event = {type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
    for (const listener of this.listeners[type] || []) listener(event);
    return event;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  replaceChildren(...children) { this.childNodes = children; }
  before(element) { this.error = element; }
  remove() { this.removed = true; }
}

function createHarness() {
  const input = Object.assign(new Element(), {
    value: '2027-03-12', min: '1970-01-01', max: '2200-12-31', validity: {valid: true},
  });
  const form = Object.assign(new Element(), {action: 'http://localhost/radio/shifts/'});
  input.form = form;
  const navigation = Object.assign(new Element(), {childNodes: ['initial navigation']});
  const calendars = [];
  const page = new Element();
  const makeCalendar = date => {
    const calendar = Object.assign(new Element(), {date, scroller: {scrollLeft: 0}});
    calendar.querySelector = selector => selector === '.two-week-scroll' ? calendar.scroller : null;
    calendar.replaceWith = replacement => { page.calendar = replacement; calendars.push(replacement); };
    return calendar;
  };
  page.calendar = makeCalendar(input.value);
  page.calendar.scroller.scrollLeft = 164;
  calendars.push(page.calendar);
  page.querySelector = selector => selector === '.calendar-nav' ? navigation : selector === '.calendar-block' ? page.calendar : null;

  const document = Object.assign(new Element(), {activeElement: input, updates: 0});
  document.querySelector = selector => selector === '.shift-schedule-page' ? page : null;
  document.getElementById = id => id === 'shift-calendar-date' ? input : null;
  document.createElement = () => new Element();
  document.dispatchEvent = event => { if (event.type === 'shift-calendar-updated') document.updates++; };
  const assignments = [];
  const history = {state: {existing: 'state'}, updates: [], replaceState(state, _, url) {
    this.updates.push({state, url}); location.href = url;
  }};
  const location = {href: 'http://localhost/radio/shifts/?date=2027-03-12', assign: url => assignments.push(url)};
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  const responses = new Map();
  const fetch = (url, options) => new Promise((resolve, reject) => {
    // Resolve aborted requests too, to verify that stale results are independently ignored.
    requests.push({url: String(url), options, resolve, reject});
  });
  class DOMParser {
    parseFromString(html) { return responses.get(html); }
  }
  vm.runInNewContext(source, {
    document, window: {location, history}, fetch, DOMParser, AbortController, URL, Date,
    Event: class { constructor(type) { this.type = type; } },
    setTimeout(callback, delay) { timers.set(++timerId, {callback, delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  }, {filename: 'static/js/shift_calendar.js'});

  return {
    input, form, page, navigation, document, requests, timers, history, location, assignments, calendars,
    change(date, type = 'input') { input.value = date; return input.dispatch(type); },
    runTimers() {
      const pending = [...timers.values()];
      timers.clear();
      pending.forEach(timer => timer.callback());
    },
    async respond(index, date, {ok = true, redirected = false, missingCalendar = false, selectedDate = date} = {}) {
      const calendar = makeCalendar(date);
      const nextNavigation = Object.assign(new Element(), {childNodes: [`navigation:${date}`, `previous:${date}`, `next:${date}`]});
      const token = `response:${index}`;
      responses.set(token, {
        querySelector: selector => selector.endsWith('.calendar-block') ? (missingCalendar ? null : calendar) : nextNavigation,
        getElementById: () => ({value: selectedDate}),
      });
      requests[index].resolve({ok, redirected, text: async () => token});
      await flush();
      return calendar;
    },
  };
}

test('typing updates the calendar, period navigation and URL without replacing the focused date input', async () => {
  const h = createHarness();
  const field = h.input;
  h.change('2026-08-12');
  assert.equal(h.requests.length, 0, 'typing is briefly debounced');
  assert.equal([...h.timers.values()][0].delay, 250);
  h.runTimers();
  assert.equal(new URL(h.requests[0].url).searchParams.get('date'), '2026-08-12');
  assert.equal(h.requests[0].options.cache, 'no-store');
  assert.equal(h.requests[0].options.credentials, 'same-origin');
  assert.equal(h.page.calendar.attributes['aria-busy'], 'true');
  const loaded = await h.respond(0, '2026-08-12');
  assert.equal(h.page.calendar, loaded);
  assert.equal(loaded.scroller.scrollLeft, 164);
  assert.equal(h.navigation.childNodes[0], 'navigation:2026-08-12');
  assert.equal(h.input, field);
  assert.equal(h.document.activeElement, field);
  assert.equal(h.input.value, '2026-08-12');
  assert.equal(h.document.updates, 1);
  assert.equal(h.assignments.length, 0);
  assert.equal(h.history.updates.length, 1);
  assert.equal(h.history.updates[0].state.existing, 'state');
  assert.equal(new URL(h.location.href).searchParams.get('date'), '2026-08-12');
});

test('partial, impossible and out-of-range dates leave the last calendar and URL intact', () => {
  const h = createHarness();
  for (const date of ['', '202', '2027-03-', '1969-12-31', '2201-01-01', '2027-02-30']) {
    h.change(date);
    h.runTimers();
  }
  h.input.validity.valid = false;
  h.change('2027-05-10');
  h.runTimers();
  assert.equal(h.requests.length, 0);
  assert.equal(h.calendars.length, 1);
  assert.equal(h.history.updates.length, 0);
});

test('rapid edits coalesce and native selection or Enter works without a separate submit button', async () => {
  const h = createHarness();
  h.change('2027-03-13');
  h.change('2027-03-14');
  h.change('2027-03-15', 'change');
  assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0].delay, 0);
  h.runTimers();
  await h.respond(0, '2027-03-15');
  assert.equal(h.requests.length, 1);
  h.input.value = '2027-03-16';
  assert.equal(h.form.dispatch('submit').defaultPrevented, true);
  h.runTimers();
  assert.equal(h.requests.length, 2);
  assert.equal(new URL(h.requests[1].url).searchParams.get('date'), '2027-03-16');
});

test('out-of-order responses cannot overwrite a more recently selected date', async () => {
  const h = createHarness();
  h.change('2027-03-13'); h.runTimers();
  h.change('2027-03-14'); h.runTimers();
  assert.equal(h.requests[0].options.signal.aborted, true);
  await h.respond(1, '2027-03-14');
  await h.respond(0, '2027-03-13');
  assert.equal(h.page.calendar.date, '2027-03-14');
  assert.equal(h.document.updates, 1);
  assert.equal(h.history.updates.length, 1);
});

test('clearing the input or returning to the displayed date cancels an in-flight update', async () => {
  for (const date of ['', '2027-03-12']) {
    const h = createHarness();
    h.change('2027-03-13'); h.runTimers();
    h.change(date); h.runTimers();
    assert.equal(h.requests[0].options.signal.aborted, true);
    await h.respond(0, '2027-03-13');
    assert.equal(h.page.calendar.date, '2027-03-12');
    assert.equal(h.requests.length, 1);
    assert.equal(h.page.calendar.attributes['aria-busy'], undefined);
    assert.equal(h.history.updates.length, 0);
  }
});

test('a response body still downloading after a newer selection cannot update the page', async () => {
  const h = createHarness();
  h.change('2027-03-13'); h.runTimers();
  let finishBody;
  h.requests[0].resolve({ok: true, text: () => new Promise(resolve => { finishBody = resolve; })});
  await flush();
  h.change('2027-03-14'); h.runTimers();
  await h.respond(1, '2027-03-14');
  finishBody('stale response');
  await flush();
  assert.equal(h.page.calendar.date, '2027-03-14');
  assert.equal(h.document.updates, 1);
  assert.equal(h.history.updates.length, 1);
});

test('a network failure keeps the calendar and keyboard input usable for retry', async () => {
  const h = createHarness();
  h.change('2027-03-13'); h.runTimers();
  h.requests[0].reject(new Error('Offline'));
  await flush();
  const error = h.page.calendar.error;
  assert.equal(error.attributes.role, 'alert');
  assert.equal(h.page.calendar.date, '2027-03-12');
  assert.equal(h.history.updates.length, 0);
  assert.equal(h.assignments.length, 0);
  assert.equal(h.document.activeElement, h.input);
  h.form.dispatch('submit'); h.runTimers();
  assert.equal(error.removed, true);
  await h.respond(1, '2027-03-13');
  assert.equal(h.page.calendar.date, '2027-03-13');
});

test('invalid markup, an unexpected date or denied access never replaces the displayed schedule', async () => {
  for (const options of [{missingCalendar: true}, {selectedDate: '2027-03-12'}, {ok: false}]) {
    const h = createHarness();
    h.change('2027-03-13'); h.runTimers();
    await h.respond(0, '2027-03-13', options);
    assert.equal(h.page.calendar.date, '2027-03-12');
    assert.equal(h.navigation.childNodes[0], 'initial navigation');
    assert.equal(h.history.updates.length, 0);
    assert.equal(h.document.updates, 0);
    assert.equal(h.page.calendar.error.attributes.role, 'alert');
  }
});

test('an expired session falls back to normal navigation for the latest requested date', async () => {
  const h = createHarness();
  h.change('2027-03-13'); h.runTimers();
  await h.respond(0, '2027-03-13', {redirected: true});
  assert.equal(h.assignments.length, 1);
  assert.equal(new URL(h.assignments[0]).searchParams.get('date'), '2027-03-13');
  assert.equal(h.document.updates, 0);
});
