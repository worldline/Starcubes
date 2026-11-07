// nav.js: ein Popover öffnen/schließen, außerhalb klicken, Escape, Roving.
import { beforeEach, describe, expect, it } from 'vitest';

import { loadScript, resetDom } from './load.js';

beforeEach(() => {
  resetDom();
  if (!globalThis.makePopover) {
    loadScript('sources/static/sources/nav.js');
  }
});

function fixture({ menu = false, closeOnClick } = {}) {
  const btn = document.createElement('button');
  btn.textContent = 'open';
  const panel = document.createElement('div');
  panel.id = 'panel';
  const a = document.createElement('button');
  a.setAttribute('role', 'menuitem');
  a.textContent = 'eins';
  const b = document.createElement('button');
  b.setAttribute('role', 'menuitem');
  b.textContent = 'zwei';
  panel.append(a, b);
  document.body.append(btn, panel);
  // jsdom lays nothing out, so offsetParent is null for every element; the
  // roving code deliberately filters hidden entries on it.
  for (const item of [a, b]) {
    Object.defineProperty(item, 'offsetParent', {
      value: document.body, configurable: true,
    });
  }
  const pop = globalThis.makePopover(btn, panel,
    { menu, closeOnClick });
  return { btn, panel, a, b, pop };
}

function click(el, detail = 1) {
  el.dispatchEvent(new MouseEvent('click',
    { bubbles: true, cancelable: true, detail }));
}

describe('makePopover', () => {
  it('toggles on the trigger and keeps aria-expanded in step', () => {
    const { btn, panel, pop } = fixture();
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    click(btn);
    expect(pop.isOpen()).toBe(true);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(panel.classList.contains('pop-open')).toBe(true);
    click(btn);
    expect(pop.isOpen()).toBe(false);
    expect(btn.getAttribute('aria-expanded')).toBe('false');
  });

  it('closes on an outside pointerdown', () => {
    const { panel, pop } = fixture();
    pop.open();
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }));
    expect(pop.isOpen()).toBe(false);
  });

  it('does not close on a pointerdown inside the panel', () => {
    const { panel, a, pop } = fixture();
    pop.open();
    a.dispatchEvent(new Event('pointerdown', { bubbles: true }));
    expect(pop.isOpen()).toBe(true);
  });

  it('Escape closes and returns focus to the trigger', () => {
    const { btn, panel, pop } = fixture();
    pop.open();
    panel.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'Escape', bubbles: true, cancelable: true }));
    expect(pop.isOpen()).toBe(false);
    expect(document.activeElement).toBe(btn);
  });

  it('closeTopPopover closes the open one', () => {
    const { pop } = fixture();
    pop.open();
    expect(globalThis.closeTopPopover()).toBe(true);
    expect(pop.isOpen()).toBe(false);
    expect(globalThis.closeTopPopover()).toBe(false);
  });

  it('arrow keys rove through menu items', () => {
    const { panel, a, b, pop } = fixture({ menu: true });
    pop.open(false);
    panel.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'ArrowDown', bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(a);
    panel.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'ArrowDown', bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(b);
    panel.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'Home', bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(a);
    panel.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'End', bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(b);
  });

  it('closeOnClick closes on a button inside, but honours data-keep-open', () => {
    const { panel, a, pop } = fixture({ closeOnClick: true });
    pop.open();
    click(a);
    expect(pop.isOpen()).toBe(false);

    a.setAttribute('data-keep-open', '');
    pop.open();
    click(a);
    expect(pop.isOpen()).toBe(true);
  });

  it('only one popover is open at a time', () => {
    const first = fixture();
    const second = fixture();
    first.pop.open();
    second.pop.open();
    expect(first.pop.isOpen()).toBe(false);
    expect(second.pop.isOpen()).toBe(true);
  });
});
