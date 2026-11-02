/* Run with: node --test scripts/test_shift_actions.cjs
 * Executes the shipped controller against small DOM/event adapters, without a browser.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/js/shift_actions.js'), 'utf8');

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
  constructor(document, parent = null) {
    super();
    this.document = document;
    this.parent = parent;
    this.attributes = {};
    this.style = {};
    this.open = false;
    this.rect = {left: 100, top: 100, right: 130, bottom: 124, width: 30, height: 24};
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  getBoundingClientRect() { return this.rect; }
  contains(element) {
    for (let node = element; node; node = node.parent) if (node === this) return true;
    return false;
  }
  focus() { this.document.activeElement = this; this.document.dispatch('focusin', {target: this}); }
  matches(selector) { return selector === ':popover-open' && Boolean(this.popoverOpen); }
}

function createHarness({count = 2, support = true, throwOnShow = false, width = 1024, height = 768, anchor, popup} = {}) {
  const document = new Events();
  const window = Object.assign(new Events(), {innerWidth: width, innerHeight: height});
  const entries = Array.from({length: count}, () => {
    const details = new Element(document);
    const trigger = new Element(document, details);
    const menu = new Element(document, details);
    const action = new Element(document, menu);
    trigger.rect = anchor || trigger.rect;
    menu.rect = popup || {left: 0, top: 0, width: 176, height: 100};
    menu.showCount = 0;
    menu.hideCount = 0;
    if (support) {
      menu.showPopover = () => {
        menu.showCount += 1;
        if (throwOnShow) throw new Error('Popover unavailable');
        menu.popoverOpen = true;
      };
      menu.hidePopover = () => { menu.hideCount += 1; menu.popoverOpen = false; };
    }
    menu.querySelector = selector => selector === 'a, button' ? action : null;
    details.querySelector = selector => selector === 'summary' ? trigger : selector === '[data-shift-actions-menu]' ? menu : null;
    return {details, trigger, menu, action};
  });
  document.querySelectorAll = selector => selector === '[data-shift-actions]' ? entries.map(entry => entry.details) : [];
  vm.runInNewContext(source, {document, window}, {filename: 'static/js/shift_actions.js'});
  return {
    entries, document, window,
    click(element) {
      const event = element.dispatch('click');
      document.dispatch('click', event);
      return event;
    },
  };
}

test('three-dot controls toggle manual popovers and keep only one shift menu open', () => {
  const h = createHarness();
  const [first, second] = h.entries;
  assert.equal(first.menu.attributes.popover, 'manual');
  assert.equal(first.trigger.attributes['aria-expanded'], 'false');
  assert.equal(h.click(first.trigger).defaultPrevented, true);
  assert.equal(first.details.open, true);
  assert.equal(first.menu.popoverOpen, true);
  assert.equal(first.trigger.attributes['aria-expanded'], 'true');
  h.click(second.trigger);
  assert.equal(first.details.open, false);
  assert.equal(first.menu.popoverOpen, false);
  assert.equal(first.trigger.attributes['aria-expanded'], 'false');
  assert.equal(second.details.open, true);
  h.click(second.trigger);
  assert.equal(second.details.open, false);
  assert.equal(second.menu.popoverOpen, false);
});

test('ArrowDown opens and focuses the first action; Escape closes and returns focus to the control', () => {
  const h = createHarness();
  const [entry] = h.entries;
  const down = entry.trigger.dispatch('keydown', {key: 'ArrowDown'});
  assert.equal(down.defaultPrevented, true);
  assert.equal(entry.details.open, true);
  assert.equal(h.document.activeElement, entry.action);
  const escape = h.document.dispatch('keydown', {key: 'Escape'});
  assert.equal(escape.defaultPrevented, true);
  assert.equal(entry.details.open, false);
  assert.equal(entry.menu.popoverOpen, false);
  assert.equal(h.document.activeElement, entry.trigger);
});

test('clicking or focusing outside closes the menu while interaction inside stays available', () => {
  const h = createHarness();
  const [entry] = h.entries;
  const outside = new Element(h.document);
  h.click(entry.trigger);
  h.click(entry.action);
  assert.equal(entry.details.open, true, 'menu actions are not mistaken for outside clicks');
  h.document.dispatch('click', {target: outside});
  assert.equal(entry.details.open, false);
  h.click(entry.trigger);
  entry.action.focus();
  assert.equal(entry.details.open, true);
  outside.focus();
  assert.equal(entry.details.open, false);
  assert.equal(h.document.activeElement, outside, 'outside focus is not stolen back');
});

test('popup placement stays inside the viewport and flips above a low control', () => {
  const h = createHarness({
    width: 320, height: 240,
    anchor: {left: 286, right: 310, top: 200, bottom: 224, width: 24, height: 24},
    popup: {left: 0, top: 0, width: 160, height: 100},
  });
  const [entry] = h.entries;
  h.click(entry.trigger);
  const left = Number.parseFloat(entry.menu.style.left);
  const top = Number.parseFloat(entry.menu.style.top);
  assert.ok(left >= 8 && left + 160 <= 312);
  assert.ok(top >= 8 && top + 100 <= 232);
  assert.ok(top + 100 < entry.trigger.rect.top, 'insufficient bottom space opens the menu above the control');
  h.click(entry.trigger);
  entry.trigger.rect = {left: 0, right: 24, top: 2, bottom: 26, width: 24, height: 24};
  h.click(entry.trigger);
  assert.equal(entry.menu.style.left, '8px', 'leftmost controls cannot position the popup offscreen');
  assert.equal(entry.menu.style.top, '32px');
});

test('scroll and resize dismiss an open menu rather than leaving it detached from the shift', () => {
  const h = createHarness();
  const [entry] = h.entries;
  for (const type of ['scroll', 'resize']) {
    h.click(entry.trigger);
    assert.equal(entry.details.open, true);
    h.window.dispatch(type);
    assert.equal(entry.details.open, false);
    assert.equal(entry.menu.popoverOpen, false);
    assert.equal(entry.trigger.attributes['aria-expanded'], 'false');
  }
});

test('unsupported or failing popover APIs leave a usable native disclosure fallback', () => {
  for (const options of [{support: false}, {support: true, throwOnShow: true}]) {
    const h = createHarness(options);
    const [entry] = h.entries;
    h.click(entry.trigger);
    assert.equal(entry.details.open, true);
    assert.equal(entry.trigger.attributes['aria-expanded'], 'true');
    assert.equal(entry.menu.attributes.popover, undefined, 'fallback content is not hidden by a popover attribute');
    entry.trigger.dispatch('keydown', {key: 'ArrowDown'});
    assert.equal(h.document.activeElement, entry.action);
    h.document.dispatch('keydown', {key: 'Escape'});
    assert.equal(entry.details.open, false);
    assert.equal(h.document.activeElement, entry.trigger);
  }
});
