/* Run with: node --test scripts/test_shift_notes.cjs
 * Runs the shipped delegated notes controller with native-disclosure/popover adapters.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/shift_notes.js'), 'utf8');

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  dispatch(type, values = {}) {
    const event = {target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...values};
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
    this.open = false;
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
  const document = new Events();
  const window = Object.assign(new Events(), {innerWidth: width, innerHeight: height});
  // No notes exist when the shipped script initializes: all test notes are added later.
  vm.runInNewContext(source, {document, window}, {filename: 'static/js/shift_notes.js'});
  const addNote = ({cardRect, triggerRect, popupRect} = {}) => {
    const card = new Element(document, 'li', {class: 'shift-day-item'});
    const details = new Element(document, 'details', {'data-shift-note': ''});
    const trigger = new Element(document, 'summary', {class: 'shift-note-toggle'});
    const label = new Element(document, 'span');
    const popup = new Element(document, 'div', {'data-shift-note-popup': '', class: 'shift-note-popup', role: 'dialog'});
    const close = new Element(document, 'button', {'data-shift-note-close': ''});
    const closeIcon = new Element(document, 'span');
    const paragraph = new Element(document, 'p', {class: 'shift-note'});
    card.rect = cardRect || {left: 100, right: 240, top: 100, bottom: 180, width: 140, height: 80};
    trigger.rect = triggerRect || trigger.rect;
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
    close.append(closeIcon);
    trigger.append(label);
    popup.append(close, paragraph);
    details.append(trigger, popup);
    card.append(details);
    return {card, details, trigger, label, popup, close, closeIcon, paragraph};
  };
  return {document, window, addNote, click: target => document.dispatch('click', {target})};
}

test('delegated clicks open dynamically added note disclosures and keep only one popup open', () => {
  const h = createHarness();
  const first = h.addNote();
  const second = h.addNote();
  assert.equal(h.click(first.label).defaultPrevented, true);
  assert.equal(first.details.open, true);
  assert.equal(first.popup.popoverOpen, true);
  assert.equal(first.popup.attributes.popover, 'manual');
  assert.equal(first.popup.source, first.trigger);
  assert.equal(first.trigger.attributes['aria-controls'], first.popup.id);
  h.click(second.trigger);
  assert.equal(first.details.open, false);
  assert.equal(first.popup.popoverOpen, false);
  assert.equal(first.trigger.attributes['aria-expanded'], 'false');
  assert.equal(second.details.open, true);
  assert.notEqual(first.popup.id, second.popup.id);
  h.click(second.trigger);
  assert.equal(second.details.open, false);
});

test('ArrowDown focuses the close control and Escape or the close button returns focus to the note control', () => {
  const h = createHarness();
  const entry = h.addNote();
  const down = h.document.dispatch('keydown', {key: 'ArrowDown', target: entry.label});
  assert.equal(down.defaultPrevented, true);
  assert.equal(entry.details.open, true);
  assert.equal(h.document.activeElement, entry.close);
  h.document.dispatch('keydown', {key: 'Escape'});
  assert.equal(entry.details.open, false);
  assert.equal(h.document.activeElement, entry.trigger);
  h.click(entry.trigger);
  assert.equal(h.click(entry.closeIcon).defaultPrevented, true);
  assert.equal(entry.details.open, false);
  assert.equal(entry.popup.popoverOpen, false);
  assert.equal(h.document.activeElement, entry.trigger);
});

test('reading or clicking inside the popup does not close it, while outside click or focus does', () => {
  const h = createHarness();
  const entry = h.addNote();
  const outside = new Element(h.document);
  h.click(entry.trigger);
  h.click(entry.paragraph);
  assert.equal(entry.details.open, true);
  entry.close.focus();
  assert.equal(entry.details.open, true);
  h.click(outside);
  assert.equal(entry.details.open, false);
  h.click(entry.trigger);
  outside.focus();
  assert.equal(entry.details.open, false);
  assert.equal(h.document.activeElement, outside);
});

test('notes open beside the card where possible and stay viewport-bounded on narrow screens', () => {
  const right = createHarness();
  const first = right.addNote();
  right.click(first.trigger);
  assert.equal(first.popup.style.left, '248px');
  assert.equal(first.popup.style.top, '100px');
  const left = createHarness({width: 900});
  const second = left.addNote({cardRect: {left: 600, right: 740, top: 100, bottom: 180, width: 140, height: 80}});
  left.click(second.trigger);
  assert.equal(second.popup.style.left, '332px');
  const small = createHarness({width: 320, height: 300});
  const third = small.addNote({
    cardRect: {left: 100, right: 230, top: 170, bottom: 230, width: 130, height: 60},
    triggerRect: {left: 120, right: 220, top: 190, bottom: 210, width: 100, height: 20},
  });
  small.click(third.trigger);
  const x = Number.parseFloat(third.popup.style.left);
  const y = Number.parseFloat(third.popup.style.top);
  assert.ok(x >= 12 && x + 260 <= 308);
  assert.ok(y >= 12 && y + 160 <= 288);
  assert.ok(y + 160 < third.trigger.rect.top);
});

test('scrolling a long note preserves reading position; page scroll or resize dismisses the popup', () => {
  const h = createHarness();
  const entry = h.addNote();
  h.click(entry.trigger);
  h.window.dispatch('scroll', {target: entry.paragraph});
  assert.equal(entry.details.open, true);
  h.window.dispatch('scroll', {target: entry.popup});
  assert.equal(entry.details.open, true);
  h.window.dispatch('scroll', {target: h.document});
  assert.equal(entry.details.open, false);
  h.click(entry.trigger);
  h.window.dispatch('resize');
  assert.equal(entry.details.open, false);
});

test('missing or failing popover support leaves note disclosures usable through the fallback', () => {
  for (const options of [{support: false}, {support: true, throwOnShow: true}]) {
    const h = createHarness(options);
    const entry = h.addNote();
    h.click(entry.trigger);
    assert.equal(entry.details.open, true);
    assert.equal(entry.popup.attributes.popover, undefined);
    h.click(entry.close);
    assert.equal(entry.details.open, false);
    assert.equal(h.document.activeElement, entry.trigger);
    h.click(entry.trigger);
    assert.equal(entry.details.open, true, 'fallback can be opened again after a failed enhancement');
  }
});
