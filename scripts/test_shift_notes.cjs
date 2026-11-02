/* Run with: node --test scripts/test_shift_notes.cjs
 * Runs the shipped card-note controller with delegated events and a fake timer.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/shift_notes.js'), 'utf8');

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

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  dispatch(type, values = {}) {
    const event = {target: this, relatedTarget: null, pointerType: 'mouse', defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...values};
    for (const handler of this.listeners.get(type) || []) handler(event);
    return event;
  }
}

class Element extends Events {
  constructor(document, tagName = 'div', attributes = {}) {
    super();
    this.document = document;
    this.tagName = tagName.toUpperCase();
    this.nodeType = 1;
    this.attributes = {...attributes};
    this.className = attributes.class || '';
    this.id = attributes.id || '';
    this.children = [];
    this.parentElement = null;
    this.hidden = 'hidden' in attributes;
    this.style = {};
    this.rect = {left: 120, right: 220, top: 120, bottom: 144, width: 100, height: 24};
  }
  append(...children) {
    for (const child of children) { child.parentElement = this; this.children.push(child); }
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  matches(selector) {
    if (selector === ':popover-open') return Boolean(this.popoverOpen);
    return selector.split(',').some(part => {
      part = part.trim();
      if (part.startsWith('.')) return this.className.split(' ').includes(part.slice(1));
      if (part.startsWith('[')) return part.slice(1, -1) in this.attributes;
      return this.tagName.toLowerCase() === part;
    });
  }
  closest(selector) {
    for (let element = this; element; element = element.parentElement) if (element.matches(selector)) return element;
    return null;
  }
  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const match = child.querySelector(selector);
      if (match) return match;
    }
    return null;
  }
  contains(element) {
    for (let node = element; node; node = node.parentElement) if (node === this) return true;
    return false;
  }
  getBoundingClientRect() { return this.rect; }
  focus() { this.document.activeElement = this; this.document.dispatch('focusin', {target: this}); }
}

function createHarness({support = true, throwOnShow = false, width = 1024, height = 768} = {}) {
  const clock = new Clock();
  const document = new Events();
  const window = Object.assign(new Events(), {innerWidth: width, innerHeight: height});
  vm.runInNewContext(source, {document, window,
    setTimeout: (callback, delay) => clock.setTimeout(callback, delay),
    clearTimeout: id => clock.clearTimeout(id),
  }, {filename: 'static/js/shift_notes.js'});
  // Every card is added after controller initialization, as happens in the live preview.
  const addNote = ({cardRect, popupRect, hasNote = true} = {}) => {
    const card = new Element(document, 'li', {class: 'shift-day-item', ...(hasNote ? {'data-shift-note': ''} : {})});
    const trigger = new Element(document, 'div', hasNote ? {'data-shift-note-trigger': '', role: 'button', tabindex: '0', 'aria-expanded': 'false'} : {});
    const label = new Element(document, 'strong');
    const popup = new Element(document, 'div', {'data-shift-note-popup': '', class: 'shift-note-popup', role: 'tooltip', hidden: ''});
    const paragraph = new Element(document, 'p', {class: 'shift-note'});
    card.rect = cardRect || {left: 100, right: 240, top: 100, bottom: 180, width: 140, height: 80};
    popup.rect = popupRect || {left: 0, top: 0, width: 260, height: 160};
    popup.showCount = 0;
    if (support) {
      popup.showPopover = options => {
        popup.showCount += 1;
        if (throwOnShow) throw new Error('Popover unavailable');
        popup.popoverOpen = true;
        popup.source = options?.source;
      };
      popup.hidePopover = () => { popup.popoverOpen = false; };
    }
    trigger.append(label);
    popup.append(paragraph);
    card.append(trigger);
    if (hasNote) card.append(popup);
    return {card, trigger, label, popup, paragraph};
  };
  return {document, window, clock, addNote,
    click: target => document.dispatch('click', {target}),
    tick: ms => clock.tick(ms),
  };
}

const assertOpen = entry => {
  assert.equal(entry.popup.hidden, false);
  assert.equal(entry.trigger.attributes['aria-expanded'], 'true');
  assert.equal(entry.trigger.attributes['aria-describedby'], entry.popup.id);
};
const assertClosed = entry => {
  assert.equal(entry.popup.hidden, true);
  assert.equal(entry.trigger.attributes['aria-expanded'], 'false');
  assert.equal(entry.trigger.attributes['aria-describedby'], undefined);
};

test('clicking dynamically added cards toggles one note window at a time', () => {
  const h = createHarness();
  const first = h.addNote();
  const second = h.addNote();
  assert.equal(h.click(first.label).defaultPrevented, true);
  assertOpen(first);
  assert.equal(first.popup.popoverOpen, true);
  assert.equal(first.popup.attributes.popover, 'manual');
  assert.equal(first.popup.source, first.trigger);
  assert.equal(first.trigger.attributes['aria-controls'], first.popup.id);
  h.click(second.card);
  assertClosed(first);
  assertOpen(second);
  assert.notEqual(first.popup.id, second.popup.id);
  h.click(second.label);
  assertClosed(second);
});

test('Enter and space toggle the focused card, while Escape closes and returns focus', () => {
  const h = createHarness();
  const entry = h.addNote();
  entry.trigger.focus();
  assert.equal(h.document.dispatch('keydown', {key: 'Enter', target: entry.trigger}).defaultPrevented, true);
  assertOpen(entry);
  h.document.dispatch('keydown', {key: 'Enter', target: entry.trigger, repeat: true});
  assertOpen(entry);
  assert.equal(h.document.dispatch('keydown', {key: ' ', target: entry.trigger}).defaultPrevented, true);
  assertClosed(entry);
  h.document.dispatch('keydown', {key: ' ', target: entry.trigger});
  assertOpen(entry);
  h.document.dispatch('keydown', {key: 'Escape'});
  assertClosed(entry);
  assert.equal(h.document.activeElement, entry.trigger);
});

test('action controls keep their defaults, popup text is not a toggle, and cards without notes are inert', () => {
  const h = createHarness();
  const entry = h.addNote();
  const controls = ['a', 'button', 'input', 'select', 'textarea', 'summary', 'form'].map(tag => new Element(h.document, tag));
  controls.push(new Element(h.document, 'details', {'data-shift-actions': ''}));
  controls.push(new Element(h.document, 'div', {contenteditable: 'true'}));
  entry.card.append(...controls);
  for (const control of controls) {
    const child = new Element(h.document, 'span');
    control.append(child);
    assert.equal(h.click(child).defaultPrevented, false);
    assertClosed(entry);
    h.click(entry.label);
    assertOpen(entry);
    assert.equal(h.click(child).defaultPrevented, false, 'edit/delete controls retain their normal behavior');
    assertClosed(entry);
  }
  h.click(entry.label);
  assert.equal(h.click(entry.paragraph).defaultPrevented, false);
  assertOpen(entry);
  h.click(entry.label);
  const blank = h.addNote({hasNote: false});
  assert.equal(h.click(blank.label).defaultPrevented, false);
  assert.equal(blank.trigger.attributes.role, undefined);
  assert.equal(blank.trigger.attributes['aria-controls'], undefined);
});

test('leaving both card and note waits exactly 150 ms; touch pointerout does not dismiss', () => {
  const h = createHarness();
  const entry = h.addNote();
  const outside = new Element(h.document);
  h.document.dispatch('pointerover', {target: entry.card});
  h.tick(1000);
  assertClosed(entry); // Hover alone does not open notes.
  h.click(entry.label);
  h.document.dispatch('pointerout', {target: entry.card, relatedTarget: outside, pointerType: 'touch'});
  h.tick(1000);
  assertOpen(entry);
  h.document.dispatch('pointerout', {target: entry.card, relatedTarget: outside});
  h.tick(149);
  assertOpen(entry);
  h.tick(1);
  assertClosed(entry);
});

test('crossing between card and popup or reentering across their gap cancels pending dismissal', () => {
  const h = createHarness();
  const entry = h.addNote();
  h.click(entry.label);
  h.document.dispatch('pointerout', {target: entry.card, relatedTarget: entry.popup});
  h.tick(500);
  assertOpen(entry);
  h.document.dispatch('pointerout', {target: entry.popup, relatedTarget: entry.card});
  h.tick(500);
  assertOpen(entry);
  h.document.dispatch('pointerout', {target: entry.card, relatedTarget: null});
  h.tick(100);
  h.document.dispatch('pointerover', {target: entry.paragraph, relatedTarget: null});
  h.tick(1000);
  assertOpen(entry);
  h.document.dispatch('pointerout', {target: entry.paragraph, relatedTarget: null});
  h.tick(100);
  h.document.dispatch('pointerover', {target: entry.card, relatedTarget: null});
  h.tick(1000);
  assertOpen(entry);
});

test('a previous card timer cannot close a newly opened note and outside clicks close immediately', () => {
  const h = createHarness();
  const first = h.addNote();
  const second = h.addNote();
  h.click(first.label);
  h.document.dispatch('pointerout', {target: first.card, relatedTarget: null});
  h.tick(100);
  h.click(second.label);
  h.tick(1000);
  assertClosed(first);
  assertOpen(second);
  h.click(new Element(h.document, 'button'));
  assertClosed(second);
});

test('focus stays usable for popup text selection and closes on outside focus or window blur', () => {
  const h = createHarness();
  const entry = h.addNote();
  const outside = new Element(h.document);
  h.click(entry.label);
  h.document.dispatch('focusout', {target: entry.trigger, relatedTarget: entry.paragraph});
  assertOpen(entry);
  h.document.dispatch('focusout', {target: entry.paragraph, relatedTarget: entry.trigger});
  assertOpen(entry);
  h.document.dispatch('focusout', {target: entry.trigger, relatedTarget: outside});
  assertClosed(entry);
  h.click(entry.label);
  h.document.dispatch('focusout', {target: entry.trigger, relatedTarget: null});
  assertOpen(entry); // Clicking non-focusable note text must not close the reader.
  h.window.dispatch('blur');
  assertClosed(entry);
  h.click(entry.label);
  outside.focus();
  assertClosed(entry);
  assert.equal(h.document.activeElement, outside);
});

test('note windows use fixed positioning beside the card and stay inside narrow viewports', () => {
  const right = createHarness();
  const first = right.addNote();
  right.click(first.label);
  assert.equal(first.popup.style.position, 'fixed');
  assert.equal(first.popup.style.left, '248px');
  assert.equal(first.popup.style.top, '100px');
  const left = createHarness({width: 900});
  const second = left.addNote({cardRect: {left: 600, right: 740, top: 100, bottom: 180, width: 140, height: 80}});
  left.click(second.label);
  assert.equal(second.popup.style.left, '332px');
  const small = createHarness({width: 320, height: 340});
  const third = small.addNote({cardRect: {left: 100, right: 230, top: 190, bottom: 250, width: 130, height: 60}});
  small.click(third.label);
  const x = Number.parseFloat(third.popup.style.left);
  const y = Number.parseFloat(third.popup.style.top);
  assert.ok(x >= 12 && x + 260 <= 308);
  assert.ok(y >= 12 && y + 160 <= 328);
  assert.ok(y + 160 < third.card.rect.top);
});

test('scrolling inside a long note keeps it open; page scroll and resize dismiss it', () => {
  const h = createHarness();
  const entry = h.addNote();
  h.click(entry.label);
  h.window.dispatch('scroll', {target: entry.paragraph});
  assertOpen(entry);
  h.window.dispatch('scroll', {target: entry.popup});
  assertOpen(entry);
  h.window.dispatch('scroll', {target: h.document});
  assertClosed(entry);
  h.click(entry.label);
  h.window.dispatch('resize');
  assertClosed(entry);
});

test('missing or failing popover support retains a fixed, dismissible hidden-element fallback', () => {
  for (const options of [{support: false}, {support: true, throwOnShow: true}]) {
    const h = createHarness(options);
    const entry = h.addNote();
    h.click(entry.label);
    assertOpen(entry);
    assert.equal(entry.popup.attributes.popover, undefined);
    assert.equal(entry.popup.style.position, 'fixed');
    h.document.dispatch('pointerout', {target: entry.card, relatedTarget: null});
    h.tick(150);
    assertClosed(entry);
    h.click(entry.label);
    assertOpen(entry);
    h.document.dispatch('keydown', {key: 'Escape'});
    assertClosed(entry);
  }
});
