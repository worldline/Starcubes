// inline_edit.js: Zell-Renderer und das Aktualisieren der Zeile nach dem Save.
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { loadScript, resetDom } from './load.js';

let ROW_SPECS;
let rowCellHTML;
let rowCellClass;
let firstLineOf;
let saveRow;
let clearOtherDefaults;

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

beforeEach(() => {
  resetDom();
  globalThis.esc = esc;
  if (!globalThis.ROW_SPECS) {
    loadScript('sources/static/sources/inline_edit.js', [
      'ROW_SPECS', 'rowCellHTML', 'rowCellClass', 'firstLineOf', 'saveRow',
      'clearOtherDefaults',
    ]);
  }
  ({ ROW_SPECS, rowCellHTML, rowCellClass, firstLineOf, saveRow,
     clearOtherDefaults } = globalThis);
});

describe('ROW_SPECS', () => {
  it('describes the columns of every table', () => {
    expect(Object.keys(ROW_SPECS).sort()).toEqual(
      ['dim', 'fc', 'idx', 'measure']);
    expect(ROW_SPECS.fc.map((c) => c.f)).toEqual(
      ['ordinal', 'name', 'pg_type', 'role', 'source_expr']);
    expect(ROW_SPECS.measure.map((c) => c.f)).toEqual(
      ['ordinal', 'name', 'agg', 'graph_style', 'is_default', 'percentiles']);
  });
});

describe('rowCellHTML', () => {
  it('renders checks and dashes', () => {
    expect(rowCellHTML({ f: 'is_primary', check: 1 }, { is_primary: true })).toBe('✓');
    expect(rowCellHTML({ f: 'is_primary', check: 1 }, { is_primary: false })).toBe('—');
    expect(rowCellHTML({ f: 'x', dash: 1 }, { x: null })).toBe('—');
    expect(rowCellHTML({ f: 'x', dash: 1 }, { x: 'v' })).toBe('v');
  });

  it('wraps badges and escapes the value', () => {
    expect(rowCellHTML({ f: 'role', badge: true }, { role: 'measure' }))
      .toBe('<span class="badge badge-measure">measure</span>');
    expect(rowCellHTML({ f: 'method', badge: 'dialect' }, { method: 'b<b>r' }))
      .toBe('<span class="badge badge-dialect">b&lt;b&gt;r</span>');
  });

  it('uses a custom text renderer', () => {
    expect(rowCellHTML({ f: 'inline_values', text: firstLineOf },
                       { inline_values: 'a\nb' })).toBe('a …');
  });

  it('escapes plain values', () => {
    expect(rowCellHTML({ f: 'name' }, { name: '<x>' })).toBe('&lt;x&gt;');
  });
});

describe('rowCellClass', () => {
  it('adds mono and the spec class', () => {
    expect(rowCellClass({})).toBe('row-display');
    expect(rowCellClass({ mono: 1 })).toBe('row-display text-mono');
    expect(rowCellClass({ cls: 'x' })).toBe('row-display x');
  });
});

describe('firstLineOf', () => {
  it('shows the first line and marks a continuation', () => {
    expect(firstLineOf('a\r\nb')).toBe('a …');
    expect(firstLineOf('a\nb')).toBe('a …');
    expect(firstLineOf('a')).toBe('a');
    expect(firstLineOf('')).toBe('—');
  });
});

function makeRow(prefix) {
  const tr = document.createElement('tr');
  tr.classList.add('editing');
  for (const _spec of ROW_SPECS[prefix]) {
    const td = document.createElement('td');
    const span = document.createElement('span');
    span.className = 'row-display';
    td.appendChild(span);
    tr.appendChild(td);
  }
  const action = document.createElement('td');
  const btn = document.createElement('button');
  action.appendChild(btn);
  tr.appendChild(action);
  const input = document.createElement('input');
  input.name = 'name';
  input.value = 'neu';
  tr.appendChild(input);
  document.body.appendChild(tr);
  return { tr, btn };
}

describe('saveRow', () => {
  it('posts the row, refreshes the display spans and leaves edit mode', async () => {
    const { tr, btn } = makeRow('fc');
    globalThis.postJSON = vi.fn(() => Promise.resolve({
      ok: true,
      row: {
        ordinal: 2, name: 'neu', pg_type: 'integer',
        role: 'measure', source_expr: null,
      },
    }));

    saveRow(btn, '/x', 'fc');
    await vi.waitFor(() => {
      expect(tr.classList.contains('editing')).toBe(false);
    });

    const spans = tr.querySelectorAll('span.row-display');
    expect(spans[0].innerHTML).toBe('2');
    expect(spans[1].innerHTML).toBe('neu');
    expect(spans[3].innerHTML).toContain('badge-measure');
    expect(spans[4].innerHTML).toBe('—');
    expect(globalThis.postJSON).toHaveBeenCalledWith('/x', expect.any(FormData));
    expect(btn.disabled).toBe(false);
  });

  it('stays in edit mode when the server refuses', async () => {
    const { tr, btn } = makeRow('fc');
    globalThis.postJSON = vi.fn(() => Promise.resolve({
      ok: false, error: 'kaputt',
    }));
    globalThis.alert = vi.fn();

    saveRow(btn, '/x', 'fc');
    await vi.waitFor(() => expect(btn.disabled).toBe(false));
    expect(tr.classList.contains('editing')).toBe(true);
    expect(globalThis.alert).toHaveBeenCalledWith('Error: kaputt');
  });
});

describe('clearOtherDefaults', () => {
  it('clears the other rows when a measure becomes the default', () => {
    const body = document.createElement('div');
    body.id = 'measureBody';
    for (let i = 0; i < 2; i++) {
      const tr = document.createElement('tr');
      for (const _spec of ROW_SPECS.measure) {
        const td = document.createElement('td');
        const span = document.createElement('span');
        span.className = 'row-display';
        span.textContent = '✓';
        td.appendChild(span);
        tr.appendChild(td);
      }
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.name = 'is_default';
      cb.checked = true;
      tr.appendChild(cb);
      body.appendChild(tr);
    }
    document.body.appendChild(body);
    const [first, second] = body.querySelectorAll('tr');

    clearOtherDefaults(first);

    expect(second.querySelector('input[name="is_default"]').checked).toBe(false);
    expect(second.querySelectorAll('span.row-display')[4].textContent).toBe('—');
  });
});
