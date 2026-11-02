/* Run with: node --test scripts/test_toasts.cjs
 * Executes the shipped toast controller with DOM events and a deterministic clock.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/toasts.js'), 'utf8');

class Clock {
  constructor() { this.now = 0; this.nextId = 1; this.timers = new Map(); }
  setTimeout(callback, delay = 0) {
    const id = this.nextId++;
    this.timers.set(id, {at: this.now + Math.max(0, Number(delay) || 0), callback});
    return id;
  }
  clearTimeout(id) { this.timers.delete(id); }
  tick(ms) {
    const target = this.now + ms;
    let limit = 10000;
    while (limit--) {
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

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(handler);
  }
  removeEventListener(type, handler) { this.listeners.get(type)?.delete(handler); }
  dispatch(type, values = {}) {
    const event = {target: this, relatedTarget: null, ...values};
    for (const handler of [...(this.listeners.get(type) || [])]) handler(event);
  }
}

class Toast extends Events {
  constructor(duration) {
    super();
    this.dataset = duration === undefined ? {} : {toastDuration: String(duration)};
    this.classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => this.classes.add(name)),
      remove: (...names) => names.forEach(name => this.classes.delete(name)),
      contains: name => this.classes.has(name),
    };
    this.isConnected = true;
    this.removalCount = 0;
    this.close = new Events();
    this.close.parent = this;
  }
  querySelector(selector) { return selector === '[data-toast-close]' ? this.close : null; }
  contains(element) {
    for (let node = element; node; node = node.parent) if (node === this) return true;
    return false;
  }
  remove() { this.isConnected = false; this.removalCount += 1; }
}

function createHarness({durations = [undefined], reducedMotion = false, hidden = false} = {}) {
  const clock = new Clock();
  const toasts = durations.map(duration => new Toast(duration));
  const document = new Events();
  document.hidden = hidden;
  document.querySelectorAll = selector => selector === '[data-toast]' ? toasts : [];
  const window = {
    matchMedia: () => ({matches: reducedMotion}),
    setTimeout: (callback, delay) => clock.setTimeout(callback, delay),
    clearTimeout: id => clock.clearTimeout(id),
    requestAnimationFrame: callback => clock.setTimeout(() => callback(clock.now), 16),
  };
  vm.runInNewContext(source, {document, window, Date: {now: () => clock.now}}, {filename: 'static/js/toasts.js'});
  return {
    toasts, document, clock,
    tick: ms => clock.tick(ms),
    enter: () => clock.tick(32),
    setHidden(value) { document.hidden = value; document.dispatch('visibilitychange'); },
  };
}

const has = (toast, name) => toast.classList.contains(name);

test('toasts enter, remain for their configured duration, then dismiss and remove', () => {
  const h = createHarness({durations: [undefined, 8000]});
  const [success, error] = h.toasts;
  assert.equal(has(success, 'is-entering'), true);
  assert.equal(has(success, 'is-visible'), false);
  h.enter();
  assert.equal(has(success, 'is-entering'), false);
  assert.equal(has(success, 'is-visible'), true);
  h.tick(4999);
  assert.equal(has(success, 'is-leaving'), false);
  h.tick(1);
  assert.equal(has(success, 'is-leaving'), true);
  assert.equal(has(error, 'is-visible'), true, 'longer error duration is preserved');
  h.tick(450);
  assert.equal(success.isConnected, false, 'fallback removes a toast without transition events');
  h.tick(2550);
  assert.equal(has(error, 'is-leaving'), true);
  error.dispatch('transitionend', {propertyName: 'transform'});
  assert.equal(error.isConnected, false);
  assert.equal(h.document.listeners.get('visibilitychange').size, 0);
});

test('hover pauses the remaining duration and leaving resumes the remainder', () => {
  const h = createHarness();
  const [toast] = h.toasts;
  h.enter();
  h.tick(2000);
  toast.dispatch('mouseenter');
  h.tick(10000);
  assert.equal(has(toast, 'is-visible'), true);
  toast.dispatch('mouseleave');
  h.tick(2999);
  assert.equal(has(toast, 'is-leaving'), false);
  h.tick(1);
  assert.equal(has(toast, 'is-leaving'), true);
});

test('focus and hover must both leave before countdown resumes, including internal focus moves', () => {
  const h = createHarness();
  const [toast] = h.toasts;
  h.enter();
  h.tick(1000);
  toast.dispatch('mouseenter');
  toast.dispatch('focusin');
  toast.dispatch('mouseleave');
  h.tick(10000);
  assert.equal(has(toast, 'is-leaving'), false, 'focused toast remains paused after mouse leaves');
  toast.dispatch('focusout', {relatedTarget: toast.close});
  h.tick(10000);
  assert.equal(has(toast, 'is-leaving'), false, 'focus moving inside the toast remains paused');
  toast.dispatch('focusout', {relatedTarget: null});
  h.tick(3999);
  assert.equal(has(toast, 'is-leaving'), false);
  h.tick(1);
  assert.equal(has(toast, 'is-leaving'), true);
});

test('hidden documents pause timers and visibility does not override active hover', () => {
  const h = createHarness();
  const [toast] = h.toasts;
  h.enter();
  h.tick(1200);
  h.setHidden(true);
  h.tick(10000);
  assert.equal(has(toast, 'is-leaving'), false);
  toast.dispatch('mouseenter');
  h.setHidden(false);
  h.tick(10000);
  assert.equal(has(toast, 'is-leaving'), false, 'visibility alone cannot resume a hovered toast');
  toast.dispatch('mouseleave');
  h.tick(3799);
  assert.equal(has(toast, 'is-leaving'), false);
  h.tick(1);
  assert.equal(has(toast, 'is-leaving'), true);
});

test('manual close works while paused and only the toast transform transition completes removal', () => {
  const h = createHarness();
  const [toast] = h.toasts;
  h.enter();
  toast.dispatch('mouseenter');
  toast.dispatch('focusin');
  toast.close.dispatch('click');
  assert.equal(has(toast, 'is-leaving'), true);
  toast.dispatch('transitionend', {target: toast.close, propertyName: 'transform'});
  toast.dispatch('transitionend', {propertyName: 'opacity'});
  assert.equal(toast.isConnected, true);
  toast.dispatch('transitionend', {propertyName: 'transform'});
  assert.equal(toast.isConnected, false);
  h.tick(10000);
  toast.dispatch('transitionend', {propertyName: 'transform'});
  assert.equal(toast.removalCount, 1, 'transition and fallback cannot remove twice');
});

test('reduced motion skips animations and removes closed or expired toasts immediately', () => {
  const h = createHarness({durations: [5000, 8000], reducedMotion: true});
  const [automatic, manual] = h.toasts;
  assert.equal(has(automatic, 'is-visible'), true);
  assert.equal(has(automatic, 'is-entering'), false);
  manual.dispatch('mouseenter');
  h.tick(5000);
  assert.equal(automatic.isConnected, false);
  assert.equal(manual.isConnected, true);
  manual.close.dispatch('click');
  assert.equal(manual.isConnected, false);
  assert.equal(h.document.listeners.get('visibilitychange').size, 0);
});

test('closing before entrance animation cannot resurrect the toast', () => {
  const h = createHarness();
  const [toast] = h.toasts;
  toast.close.dispatch('click');
  h.enter();
  assert.equal(has(toast, 'is-visible'), false);
  assert.equal(has(toast, 'is-leaving'), true);
  h.tick(450);
  assert.equal(toast.isConnected, false);
});
