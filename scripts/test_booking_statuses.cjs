/* Run with: node --test scripts/test_booking_statuses.cjs
 * Executes the shipped status watcher with a monotonic fake clock and fetch events.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/booking_statuses.js'), 'utf8');
const BASE = Date.parse('2027-03-12T03:00:00Z');
const flush = () => new Promise(resolve => setImmediate(resolve));
const instant = offset => new Date(BASE + offset).toISOString();
const booking = (id, status = 'planned', start = 120000, end = 180000) => ({id, status, starts_at: instant(start), ends_at: instant(end)});

class Clock {
  constructor() { this.now = 0; this.nextId = 1; this.timers = new Map(); }
  setTimeout(callback, delay = 0) {
    const id = this.nextId++;
    this.timers.set(id, {at: this.now + Number(delay), callback});
    return id;
  }
  clearTimeout(id) { this.timers.delete(id); }
  tick(ms) {
    const target = this.now + ms;
    for (let limit = 0; limit < 10000; limit++) {
      const next = [...this.timers.entries()].filter(([, timer]) => timer.at <= target)
        .sort((left, right) => left[1].at - right[1].at || left[0] - right[0])[0];
      if (!next) { this.now = target; return; }
      this.now = next[1].at;
      this.timers.delete(next[0]);
      next[1].callback();
    }
    throw new Error('Unexpected timer loop');
  }
}

class CustomEvent {
  constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
}

class Document {
  constructor() { this.hidden = false; this.listeners = new Map(); this.events = []; }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  dispatchEvent(event) {
    this.events.push(event);
    for (const handler of this.listeners.get(event.type) || []) handler(event);
    return true;
  }
  emit(type, detail) { return this.dispatchEvent(new CustomEvent(type, {detail})); }
}

function createHarness({bookings = [booking(1)], form = false, bootstrap, hidden = false} = {}) {
  const clock = new Clock();
  const document = new Document();
  document.hidden = hidden;
  const fields = {title: {value: 'Несохранённое название'}, start_time: {value: '11:15'}};
  const formNode = form ? {fields, ...(bootstrap ? {bookingStatusSnapshot: bootstrap} : {})} : null;
  const config = {url: '/studios/statuses/', server_now: instant(0), bookings};
  document.getElementById = id => id === 'booking-status-watch-data' ? {textContent: JSON.stringify(config)} : null;
  document.querySelector = selector => selector === '[data-booking-form]' ? formNode : null;
  const requests = [];
  let reloads = 0;
  let dateNowReads = 0;
  class SkewedDate extends Date {
    static now() { dateNowReads += 1; return Date.parse('2099-01-01T00:00:00Z') + clock.now; }
  }
  const fetch = (url, options) => new Promise((resolve, reject) => requests.push({url: String(url), options, resolve, reject}));
  vm.runInNewContext(source, {
    document, window: {location: {origin: 'http://localhost:8000', reload() { reloads += 1; }}},
    performance: {now: () => clock.now}, Date: SkewedDate, CustomEvent, URL, AbortController, fetch,
    setTimeout: (callback, delay) => clock.setTimeout(callback, delay), clearTimeout: id => clock.clearTimeout(id),
  }, {filename: 'static/js/booking_statuses.js'});
  return {
    clock, document, form: formNode, requests,
    tick: ms => clock.tick(ms),
    reloads: () => reloads,
    dateNowReads: () => dateNowReads,
    changes: () => document.events.filter(event => event.type === 'booking-statuses-changed'),
    setHidden(value) { document.hidden = value; document.emit('visibilitychange'); },
    updateSchedule(rows) { document.emit('booking-schedule-updated', {bookings: rows}); },
    async respond(index, rows, serverNow = instant(clock.now), ok = true) {
      requests[index].resolve({ok, json: async () => ({bookings: rows, server_now: serverNow})});
      await flush();
    },
  };
}

test('initial check and start boundary use server time, and calendar reload waits for confirmed status', async () => {
  const row = booking(1, 'planned', 2000, 10000);
  const h = createHarness({bookings: [row]});
  assert.equal(h.requests.length, 1);
  assert.equal(new URL(h.requests[0].url).searchParams.get('ids'), '1');
  assert.equal(h.requests[0].options.credentials, 'same-origin');
  assert.equal(h.requests[0].options.cache, 'no-store');
  await h.respond(0, [row]);
  assert.equal(h.reloads(), 0);
  h.tick(2249);
  assert.equal(h.requests.length, 1);
  h.tick(1);
  assert.equal(h.requests.length, 2, 'server start boundary adds a 250 ms confirmation margin');
  assert.equal(h.reloads(), 0, 'the browser never invents a status at the boundary');
  await h.respond(1, [{...row, status: 'recording'}]);
  assert.equal(h.reloads(), 1);
  assert.equal(h.dateNowReads(), 0, 'a wildly wrong browser wall clock cannot affect scheduling');
  h.tick(60000);
  assert.equal(h.requests.length, 2);
});

test('terminal or empty snapshots never poll or automatically restart future bookings', () => {
  for (const rows of [[], [booking(1, 'completed')], [booking(1, 'cancelled')], [booking(1, 'completed'), booking(2, 'cancelled')]]) {
    const h = createHarness({bookings: rows});
    h.tick(300000);
    assert.equal(h.requests.length, 0);
    assert.equal(h.reloads(), 0);
    assert.equal(h.clock.timers.size, 0);
  }
});

test('hidden pages cancel in-flight checks and resume immediately, then poll every 15 seconds', async () => {
  const row = booking(1);
  const h = createHarness({bookings: [row]});
  h.setHidden(true);
  assert.equal(h.requests[0].options.signal.aborted, true);
  h.tick(60000);
  assert.equal(h.requests.length, 1);
  h.setHidden(false);
  assert.equal(h.requests.length, 2);
  await h.respond(1, [row]);
  h.tick(14999);
  assert.equal(h.requests.length, 2);
  h.tick(1);
  assert.equal(h.requests.length, 3);
  h.setHidden(true);
  await h.respond(0, [{...row, status: 'recording'}]);
  assert.equal(h.reloads(), 0, 'an aborted hidden-page response cannot update the visible snapshot');
  const initiallyHidden = createHarness({bookings: [row], hidden: true});
  assert.equal(initiallyHidden.requests.length, 0);
  initiallyHidden.setHidden(false);
  assert.equal(initiallyHidden.requests.length, 1);
});

test('poll and boundary timers cannot duplicate an in-flight check, and timeout retries safely', async () => {
  const row = booking(1, 'planned', 15000, 60000);
  const h = createHarness({bookings: [row]});
  h.document.emit('visibilitychange');
  assert.equal(h.requests.length, 1);
  await h.respond(0, [row]);
  h.tick(15000);
  assert.equal(h.requests.length, 2);
  h.tick(250);
  h.document.emit('visibilitychange');
  assert.equal(h.requests.length, 2);
  h.tick(9750);
  assert.equal(h.requests[1].options.signal.aborted, true);
  await h.respond(1, [{...row, status: 'recording'}]);
  assert.equal(h.reloads(), 0, 'late timed-out data is ignored');
  h.tick(14999);
  assert.equal(h.requests.length, 2);
  h.tick(1);
  assert.equal(h.requests.length, 3);
});

test('form bootstrap and changed day-studio snapshots abort stale requests and preserve local input', async () => {
  const original = booking(1);
  const latestBootstrap = booking(2);
  const h = createHarness({bookings: [original], form: true, bootstrap: [latestBootstrap]});
  assert.equal(new URL(h.requests[0].url).searchParams.get('ids'), '2');
  const changedPair = booking(3);
  h.updateSchedule([changedPair]);
  assert.equal(h.requests[0].options.signal.aborted, true);
  assert.equal(new URL(h.requests[1].url).searchParams.get('ids'), '3');
  await h.respond(1, [changedPair]);
  await h.respond(0, [{...latestBootstrap, status: 'completed'}]);
  assert.equal(h.changes().length, 0);
  assert.equal(h.reloads(), 0);
  assert.equal(h.form.fields.title.value, 'Несохранённое название');
  h.updateSchedule([]);
  h.tick(60000);
  assert.equal(h.requests.length, 2, 'clearing a form schedule suspends the previous pair');
  h.updateSchedule([booking(4)]);
  assert.equal(h.requests.length, 3);
  assert.equal(new URL(h.requests[2].url).searchParams.get('ids'), '4');
});

test('network failures and invalid server responses retry without reloading or accepting unrelated ids', async () => {
  const row = booking(1, 'planned', 300000, 360000);
  const h = createHarness({bookings: [row]});
  h.requests[0].reject(new Error('Network unavailable'));
  await flush();
  h.tick(15000);
  assert.equal(h.requests.length, 2);
  await h.respond(1, [booking(999, 'recording', -1000, 360000)]);
  assert.equal(h.reloads(), 0);
  h.tick(15000);
  await h.respond(2, [row], '2027-03-12T03:00:00');
  assert.equal(h.reloads(), 0);
  h.tick(15000);
  await h.respond(3, [row]);
  assert.equal(h.reloads(), 0);
  h.tick(14999);
  assert.equal(h.requests.length, 4);
  h.tick(1);
  assert.equal(h.requests.length, 5, 'normal polling recovers after a valid response');
});

test('confirmed changes in a form dispatch refresh events rather than reload or overwrite fields', async () => {
  const row = booking(1, 'planned', 5000, 20000);
  const h = createHarness({bookings: [row], form: true});
  await h.respond(0, [row]);
  assert.equal(h.changes().length, 0, 'unchanged snapshots are quiet');
  h.tick(5250);
  await h.respond(1, [{...row, status: 'recording'}]);
  assert.equal(h.changes().length, 1);
  assert.equal(h.reloads(), 0);
  assert.equal(h.form.fields.title.value, 'Несохранённое название');
  assert.equal(h.form.fields.start_time.value, '11:15');
});

test('a server-confirmed deletion reloads a calendar once and stops obsolete timers', async () => {
  const h = createHarness({bookings: [booking(1)]});
  await h.respond(0, []);
  assert.equal(h.reloads(), 1);
  h.tick(60000);
  assert.equal(h.reloads(), 1);
  assert.equal(h.requests.length, 1);
});
