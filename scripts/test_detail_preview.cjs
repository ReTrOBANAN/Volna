/* Run with: node --test scripts/test_detail_preview.cjs */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor() { this.listeners = {}; this.children = []; this.attributes = {}; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  dispatch(type, values = {}) {
    const event = {target: this, button: 0, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...values};
    for (const handler of this.listeners[type] || []) handler(event);
    return event;
  }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  set innerHTML(_) { throw new Error('Use nodes and textContent rather than executable HTML.'); }
}

function createHarness({available = ['booking', 'event'], supported = true} = {}) {
  const document = new Element();
  document.body = {classList: {remove() {}, toggle() { return false; }}};
  const dialogs = Object.fromEntries(available.map(type => {
    const body = new Element();
    const close = new Element();
    const dialog = Object.assign(new Element(), {body, closeButton: close, open: false, shows: 0});
    dialog.querySelector = selector => selector === '[data-dialog-body]' ? body : selector === '[data-dialog-close]' ? close : null;
    dialog.getBoundingClientRect = () => ({left: 100, top: 100, right: 500, bottom: 500});
    dialog.close = () => { dialog.open = false; };
    if (supported) dialog.showModal = () => { dialog.open = true; dialog.shows++; };
    return [type, dialog];
  }));
  document.querySelector = selector => dialogs[selector.match(/^\[data-(booking|event)-dialog\]$/)?.[1]] || null;
  document.querySelectorAll = () => [];
  document.createElement = () => new Element();
  const requests = [];
  const assignments = [];
  const responses = new Map();
  const fetch = (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
  class DOMParser { parseFromString(token) { return responses.get(token); } }
  vm.runInNewContext(source, {
    document, window: {location: {assign: url => assignments.push(url)}}, fetch, DOMParser,
  }, {filename: 'static/js/app.js'});

  return {
    document, dialogs, requests, assignments,
    link(type, id) {
      const link = {href: `http://localhost/${type === 'booking' ? 'studios' : 'events'}/${id}/`};
      link.closest = selector => selector === `[data-${type}-preview]` ? link : null;
      const child = {closest: link.closest};
      return {link, child};
    },
    click(target, values) { return document.dispatch('click', {target, ...values}); },
    async respond(index, {ok = true, missingHeading = false, missingDetail = false} = {}) {
      const heading = Object.assign(new Element(), {name: `heading:${index}`});
      const detail = Object.assign(new Element(), {name: `detail:${index}`});
      const token = `response:${index}`;
      responses.set(token, {querySelector: selector => selector === 'main .page-heading'
        ? (missingHeading ? null : heading) : (missingDetail ? null : detail)});
      requests[index].resolve({ok, text: async () => token});
      await flush();
      return {heading, detail};
    },
  };
}

test('booking and event links open the appropriate dialog and load its heading and details', async () => {
  for (const type of ['booking', 'event']) {
    const h = createHarness();
    const {link, child} = h.link(type, 42);
    assert.equal(h.click(child).defaultPrevented, true);
    const dialog = h.dialogs[type];
    assert.equal(dialog.open, true);
    assert.equal(dialog.body.children[0].attributes.role, 'status');
    assert.equal(dialog.body.children[0].textContent, type === 'event'
      ? 'Загружаем информацию об инфоповоде…' : 'Загружаем информацию о записи…');
    assert.equal(h.requests[0].url, link.href);
    assert.equal(h.requests[0].options.cache, 'no-store');
    assert.equal(h.requests[0].options.credentials, 'same-origin');
    const loaded = await h.respond(0);
    assert.deepEqual(dialog.body.children, [loaded.heading, loaded.detail]);
    assert.equal(h.dialogs[type === 'event' ? 'booking' : 'event'].open, false);
    assert.equal(h.assignments.length, 0);
  }
});

test('delegated handlers support links created after calendar replacement', async () => {
  const h = createHarness({available: ['event']});
  const first = h.link('event', 1);
  h.click(first.link);
  await h.respond(0);
  const newlyInserted = h.link('event', 2);
  h.click(newlyInserted.child);
  const loaded = await h.respond(1);
  assert.equal(h.dialogs.event.shows, 1, 'an already open modal is reused');
  assert.equal(h.requests[1].url, newlyInserted.link.href);
  assert.equal(h.dialogs.event.body.children[1], loaded.detail);
});

test('modified and middle clicks, missing dialogs and unsupported modals retain native navigation', () => {
  const h = createHarness();
  for (const type of ['booking', 'event']) {
    for (const values of [{button: 1}, {button: 2}, {ctrlKey: true}, {metaKey: true},
      {shiftKey: true}, {altKey: true}, {defaultPrevented: true}]) {
      const event = h.click(h.link(type, 1).child, values);
      assert.equal(event.defaultPrevented, Boolean(values.defaultPrevented));
    }
  }
  assert.equal(h.requests.length, 0);
  for (const options of [{available: []}, {supported: false}]) {
    const native = createHarness(options);
    assert.equal(native.click(native.link('event', 2).link).defaultPrevented, false);
    assert.equal(native.requests.length, 0);
  }
});

test('newer selections win and closed dialogs ignore late successful or failed responses', async () => {
  const h = createHarness();
  h.click(h.link('event', 1).link);
  h.click(h.link('event', 2).link);
  const latest = await h.respond(1);
  await h.respond(0);
  assert.equal(h.dialogs.event.body.children[1], latest.detail);
  h.click(h.link('event', 3).link);
  h.dialogs.event.closeButton.dispatch('click');
  await h.respond(2);
  assert.equal(h.dialogs.event.open, false);
  assert.equal(h.dialogs.event.body.children.length, 1, 'late content does not replace the loading state');
  h.click(h.link('booking', 4).link);
  h.dialogs.booking.closeButton.dispatch('click');
  h.requests[3].reject(new Error('Offline'));
  await flush();
  assert.equal(h.assignments.length, 0);
});

test('failed or incomplete detail loads fall back to the requested page only for the current open request', async () => {
  for (const options of [{ok: false}, {missingHeading: true}, {missingDetail: true}]) {
    const h = createHarness();
    const {link} = h.link('event', 7);
    h.click(link);
    await h.respond(0, options);
    assert.deepEqual(h.assignments, [link.href]);
  }
  const h = createHarness();
  h.click(h.link('booking', 8).link);
  const latest = h.link('booking', 9).link;
  h.click(latest);
  h.requests[0].reject(new Error('Stale failure'));
  await flush();
  assert.equal(h.assignments.length, 0);
  h.requests[1].reject(new Error('Current failure'));
  await flush();
  assert.deepEqual(h.assignments, [latest.href]);
});

test('the cross and outside clicks close either modal while clicks inside its bounds do not', () => {
  for (const type of ['booking', 'event']) {
    const h = createHarness();
    const dialog = h.dialogs[type];
    h.click(h.link(type, 1).link);
    dialog.dispatch('click', {clientX: 150, clientY: 150});
    assert.equal(dialog.open, true);
    dialog.dispatch('click', {clientX: 90, clientY: 150});
    assert.equal(dialog.open, false);
    h.click(h.link(type, 2).link);
    dialog.closeButton.dispatch('click');
    assert.equal(dialog.open, false);
  }
});
