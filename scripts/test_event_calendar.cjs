/* Run with: node --test scripts/test_event_calendar.cjs
 * Exercises the shipped controller with DOM adapters and controllable network timing.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/event_calendar.js'), 'utf8');
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
  getAttribute(name) { return this.attributes[name] ?? (name === 'href' ? this.href : null); }
  removeAttribute(name) { delete this.attributes[name]; }
  replaceChildren(...children) { this.childNodes = children; }
  before(element) { this.error = element; }
  remove() { this.removed = true; }
}

function createHarness({hasCreate = true} = {}) {
  const input = Object.assign(new Element(), {
    value: '2027-03-12', min: '1970-01-01', max: '2200-12-31', validity: {valid: true},
  });
  const filter = Object.assign(new Element(), {value: ''});
  const form = Object.assign(new Element(), {action: 'http://localhost/events/'});
  input.form = filter.form = form;
  const navigation = Object.assign(new Element(), {childNodes: ['initial navigation']});
  const currentMonth = Object.assign(new Element(), {href: 'http://localhost/events/'});
  const create = hasCreate ? Object.assign(new Element(), {href: 'http://localhost/events/new/'}) : null;
  const page = new Element();
  const makeSection = (kind, date, status) => {
    const section = Object.assign(new Element(), {date, status, scroller: {scrollLeft: 0}});
    section.querySelector = selector => selector === '.table-wrap' ? section.scroller : null;
    section.replaceWith = next => { page[kind] = next; };
    return section;
  };
  page.calendar = makeSection('calendar', input.value, filter.value);
  page.list = makeSection('list', input.value, filter.value);
  page.calendar.scroller.scrollLeft = 164;
  page.list.scroller.scrollLeft = 63;
  page.querySelector = selector => ({
    '.calendar-nav': navigation, '[data-event-calendar]': page.calendar,
    '[data-event-list]': page.list, '[data-current-month]': currentMonth, '[data-event-create]': create,
  })[selector] || null;

  const document = Object.assign(new Element(), {activeElement: input, updates: 0});
  document.querySelector = selector => selector === '.event-schedule-page' ? page : null;
  document.getElementById = id => ({'event-calendar-date': input, 'status-filter': filter})[id] || null;
  document.createElement = () => new Element();
  document.dispatchEvent = event => { if (event.type === 'event-calendar-updated') document.updates++; };
  const assignments = [];
  const location = {href: 'http://localhost/events/?date=2027-03-12', assign: url => assignments.push(url)};
  const history = {state: {existing: 'state'}, updates: [], replaceState(state, _, url) {
    this.updates.push({state, url}); location.href = url;
  }};
  const requests = [];
  const timers = new Map();
  const responses = new Map();
  let timerId = 0;
  const fetch = (url, options) => new Promise((resolve, reject) => {
    // Allow aborted requests to resolve too: sequence protection must stand on its own.
    requests.push({url: String(url), options, resolve, reject});
  });
  class DOMParser { parseFromString(html) { return responses.get(html); } }
  vm.runInNewContext(source, {
    document, window: {location, history}, fetch, DOMParser, AbortController, URL, Date,
    Event: class { constructor(type) { this.type = type; } },
    setTimeout(callback, delay) { timers.set(++timerId, {callback, delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  }, {filename: 'static/js/event_calendar.js'});

  return {
    input, filter, form, page, navigation, currentMonth, create, document, requests, timers, history, location, assignments,
    date(value, event = 'input') { input.value = value; input.dispatch(event); },
    status(value) { filter.value = value; filter.dispatch('change'); },
    runTimers() { const pending = [...timers.values()]; timers.clear(); pending.forEach(timer => timer.callback()); },
    async respond(index, options = {}) {
      const url = new URL(requests[index].url);
      const date = url.searchParams.get('date');
      const status = url.searchParams.get('status') || '';
      const calendar = makeSection('calendar', date, status);
      const list = makeSection('list', date, status);
      const nextNavigation = Object.assign(new Element(), {childNodes: [`navigation:${date}:${status}`]});
      const nextMonth = Object.assign(new Element(), {href: `/events/${status ? `?status=${status}` : ''}`});
      if (options.badMonthLink) nextMonth.href = 'http://[';
      const nextCreate = Object.assign(new Element(), {href: `/events/new/?date=${date}`});
      const parts = {'.calendar-nav': nextNavigation, '[data-event-calendar]': calendar,
        '[data-event-list]': list, '[data-current-month]': nextMonth, '[data-event-create]': nextCreate};
      if (options.missing) parts[options.missing] = null;
      const nextRoot = {querySelector: selector => parts[selector] || null};
      const token = `response:${index}`;
      responses.set(token, {
        querySelector: () => nextRoot,
        getElementById: id => id === 'event-calendar-date' ? {value: options.date ?? date} : {value: options.status ?? status},
      });
      requests[index].resolve({ok: options.ok ?? true, redirected: options.redirected ?? false, text: async () => token});
      await flush();
      return {calendar, list};
    },
  };
}

test('date edits replace calendar and list together while preserving focus, scroll and navigation context', async () => {
  const h = createHarness();
  const field = h.input;
  h.date('2026-08-12');
  assert.equal(h.requests.length, 0);
  assert.equal([...h.timers.values()][0].delay, 250);
  h.runTimers();
  assert.equal(h.requests[0].options.cache, 'no-store');
  assert.equal(h.requests[0].options.credentials, 'same-origin');
  assert.equal(h.page.calendar.attributes['aria-busy'], 'true');
  assert.equal(h.page.list.attributes['aria-busy'], 'true');
  const next = await h.respond(0);
  assert.equal(h.page.calendar, next.calendar);
  assert.equal(h.page.list, next.list);
  assert.equal(next.calendar.scroller.scrollLeft, 164);
  assert.equal(next.list.scroller.scrollLeft, 63);
  assert.equal(h.navigation.childNodes[0], 'navigation:2026-08-12:');
  assert.equal(h.create.href, 'http://localhost/events/new/?date=2026-08-12');
  assert.equal(h.input, field);
  assert.equal(h.document.activeElement, field);
  assert.equal(h.document.updates, 1);
  assert.equal(h.history.updates[0].state.existing, 'state');
  assert.equal(new URL(h.location.href).searchParams.get('date'), '2026-08-12');
  assert.equal(h.assignments.length, 0);
});

test('status selection retains the date and updates both results and the current-month link', async () => {
  const h = createHarness({hasCreate: false});
  h.document.activeElement = h.filter;
  h.status('shot'); h.runTimers();
  const url = new URL(h.requests[0].url);
  assert.equal(url.searchParams.get('date'), '2027-03-12');
  assert.equal(url.searchParams.get('status'), 'shot');
  await h.respond(0);
  assert.equal(h.page.calendar.status, 'shot');
  assert.equal(h.page.list.status, 'shot');
  assert.equal(h.currentMonth.href, 'http://localhost/events/?status=shot');
  assert.equal(h.document.activeElement, h.filter);
  h.status(''); h.runTimers();
  assert.equal(new URL(h.requests[1].url).searchParams.has('status'), false);
  await h.respond(1);
  assert.equal(h.currentMonth.href, 'http://localhost/events/');
});

test('rapid date and status changes coalesce; calendar selection and Enter submit without separate buttons', async () => {
  const h = createHarness();
  h.date('2027-03-13'); h.date('2027-03-14'); h.status('edited');
  assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0].delay, 0);
  h.runTimers();
  await h.respond(0);
  assert.equal(h.page.calendar.date, '2027-03-14');
  assert.equal(h.page.calendar.status, 'edited');
  h.date('2027-03-15', 'change'); h.runTimers();
  await h.respond(1);
  h.input.value = '2027-03-16';
  assert.equal(h.form.dispatch('submit').defaultPrevented, true);
  h.runTimers();
  assert.equal(new URL(h.requests[2].url).searchParams.get('status'), 'edited');
});

test('partial, impossible, invalid native and out-of-range dates keep the last results and URL', () => {
  const h = createHarness();
  for (const date of ['', '202', '2027-03-', '1969-12-31', '2201-01-01', '2027-02-30']) {
    h.date(date); h.status('shot'); h.runTimers();
  }
  h.input.validity.valid = false;
  h.date('2027-05-10'); h.runTimers();
  assert.equal(h.requests.length, 0);
  assert.equal(h.page.calendar.date, '2027-03-12');
  assert.equal(h.page.list.date, '2027-03-12');
  assert.equal(h.history.updates.length, 0);
});

test('stale date or filter responses cannot overwrite the latest pair or trigger a stale login navigation', async () => {
  const h = createHarness();
  h.date('2027-03-13'); h.runTimers();
  h.status('approved'); h.runTimers();
  assert.equal(h.requests[0].options.signal.aborted, true);
  await h.respond(1);
  await h.respond(0, {redirected: true});
  assert.equal(h.page.calendar.date, '2027-03-13');
  assert.equal(h.page.list.status, 'approved');
  assert.equal(h.history.updates.length, 1);
  assert.equal(h.assignments.length, 0);
  h.date('2027-03-14'); h.runTimers();
  h.date(''); h.runTimers();
  await h.respond(2);
  assert.equal(h.page.calendar.date, '2027-03-13');
  assert.equal(h.page.calendar.attributes['aria-busy'], undefined);
  assert.equal(h.page.list.attributes['aria-busy'], undefined);
});

test('a slow response body is ignored after a newer filter and date have loaded', async () => {
  const h = createHarness();
  h.date('2027-03-13'); h.runTimers();
  let finishBody;
  h.requests[0].resolve({ok: true, text: () => new Promise(resolve => { finishBody = resolve; })});
  await flush();
  h.date('2027-04-01'); h.status('published'); h.runTimers();
  await h.respond(1);
  finishBody('obsolete body'); await flush();
  assert.equal(h.page.calendar.date, '2027-04-01');
  assert.equal(h.page.list.status, 'published');
  assert.equal(h.document.updates, 1);
});

test('invalid responses cannot partially replace navigation, calendar or monthly list', async () => {
  for (const options of [{missing: '[data-event-calendar]'}, {missing: '[data-event-list]'},
    {missing: '.calendar-nav'}, {missing: '[data-current-month]'}, {badMonthLink: true},
    {date: '2027-03-12'}, {status: 'shot'}, {ok: false}]) {
    const h = createHarness();
    const oldCalendar = h.page.calendar;
    const oldList = h.page.list;
    h.date('2027-04-01'); h.runTimers();
    await h.respond(0, options);
    assert.equal(h.page.calendar, oldCalendar);
    assert.equal(h.page.list, oldList);
    assert.equal(h.navigation.childNodes[0], 'initial navigation');
    assert.equal(h.history.updates.length, 0);
    assert.equal(h.document.updates, 0);
    assert.equal(h.page.calendar.error.attributes.role, 'alert');
  }
});

test('offline failures keep input usable and allow a retry with the same selected date and status', async () => {
  const h = createHarness();
  h.date('2027-04-01'); h.status('shot'); h.runTimers();
  h.requests[0].reject(new Error('Offline')); await flush();
  const error = h.page.calendar.error;
  assert.equal(error.attributes.role, 'alert');
  assert.equal(h.page.calendar.date, '2027-03-12');
  assert.equal(h.page.list.status, '');
  assert.equal(h.document.activeElement, h.input);
  assert.equal(h.history.updates.length, 0);
  h.form.dispatch('submit'); h.runTimers();
  assert.equal(error.removed, true);
  await h.respond(1);
  assert.equal(h.page.calendar.date, '2027-04-01');
  assert.equal(h.page.list.status, 'shot');
});

test('an expired session uses normal navigation with the requested date and filter', async () => {
  const h = createHarness();
  h.date('2027-04-01'); h.status('approved'); h.runTimers();
  await h.respond(0, {redirected: true});
  assert.equal(h.assignments.length, 1);
  const url = new URL(h.assignments[0]);
  assert.equal(url.pathname, '/events/');
  assert.equal(url.searchParams.get('date'), '2027-04-01');
  assert.equal(url.searchParams.get('status'), 'approved');
  assert.equal(h.document.updates, 0);
});
