// DateTimePicker: Eingabe-Parsing, UTC-Ausgabe, Öffnen/Schließen, Navigation.
import { beforeEach, describe, expect, it } from 'vitest';

import { loadScript, resetDom } from './load.js';

let DateTimePicker;

beforeEach(() => {
  resetDom();
  if (!globalThis.DateTimePicker) {
    loadScript('sources/static/sources/datetimepicker.js', ['DateTimePicker']);
  }
  DateTimePicker = globalThis.DateTimePicker;
});

function mount(onChange = () => {}) {
  const wrap = document.createElement('div');
  const input = document.createElement('input');
  wrap.appendChild(input);
  document.body.appendChild(wrap);
  return { input, picker: new DateTimePicker(input, { onChange }) };
}

describe('DateTimePicker', () => {
  it('parses "YYYY-MM-DD HH:MM" and fires onChange', () => {
    const changes = [];
    const { input, picker } = mount((ms) => changes.push(ms));
    input.value = '2026-01-02 03:04';
    picker._commitInput();
    expect(changes).toEqual([Date.UTC(2026, 0, 2, 3, 4)]);
    expect(picker.getValue()).toBe(Date.UTC(2026, 0, 2, 3, 4));
  });

  it('accepts a date without a time as midnight', () => {
    const changes = [];
    const { input, picker } = mount((ms) => changes.push(ms));
    input.value = '2026-05-07';
    picker._commitInput();
    expect(changes).toEqual([Date.UTC(2026, 4, 7, 0, 0)]);
  });

  it('rejects impossible dates instead of rolling them over', () => {
    const changes = [];
    const { input, picker } = mount((ms) => changes.push(ms));
    input.value = '2026-02-30 10:00';
    picker._commitInput();
    expect(changes).toEqual([]);
    expect(picker._parseUserInput('2026-02-30')).toBeNull();
    expect(picker._parseUserInput('31.12.2026')).toBeNull();
  });

  it('formats the picked instant as UTC ISO without milliseconds', () => {
    const { picker } = mount();
    picker.setValue(Date.UTC(2026, 4, 7, 13, 45, 30, 999));
    // Die Anzeige kennt nur Minuten; Sekunden gehen beim Setzen verloren.
    expect(picker.getUTCISO()).toBe('2026-05-07T13:45:00Z');
  });

  it('clear empties the field without firing onChange', () => {
    const changes = [];
    const { input, picker } = mount((ms) => changes.push(ms));
    picker.setValue(Date.UTC(2026, 0, 1));
    input.value = '2026-01-01 00:00';
    picker.clear();
    expect(input.value).toBe('');
    expect(picker.getValue()).toBeNull();
    expect(changes).toEqual([]);
  });

  it('setValue formats the local field text', () => {
    const { input, picker } = mount();
    picker.setValue(Date.UTC(2026, 4, 7, 13, 45));
    expect(input.value).toBe('2026-05-07 13:45');
  });

  it('open/close applies the pending value', () => {
    const { input, picker } = mount();
    picker.setValue(Date.UTC(2026, 4, 7, 13, 45));
    picker.open();
    expect(picker._isOpen).toBe(true);
    picker.close(false);
    expect(picker._isOpen).toBe(false);
    expect(input.value).toBe('2026-05-07 13:45');
  });

  it('navigates months across year boundaries', () => {
    const { picker } = mount();
    picker.open();
    picker._viewY = 2026;
    picker._viewM = 0;
    picker._nav(-1);
    expect([picker._viewY, picker._viewM]).toEqual([2025, 11]);
    picker._nav(1);
    picker._nav(1);
    expect([picker._viewY, picker._viewM]).toEqual([2026, 1]);
  });

  it('pads and clamps the time inputs', () => {
    const { picker } = mount();
    expect(picker._pad(5)).toBe('05');
    picker._hIn.value = '99';
    picker._hIn.dispatchEvent(new Event('change'));
    expect(picker._pendingH).toBe(23);
    picker._mIn.value = '-4';
    picker._mIn.dispatchEvent(new Event('change'));
    expect(picker._pendingM).toBe(0);
  });
});
