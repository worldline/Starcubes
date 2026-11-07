// log_viewer.js: die reinen Helfer (Zeit, Dauer, Host, Klassen, Status).
import { beforeAll, describe, expect, it } from 'vitest';

import { loadScript } from './load.js';

let h;

beforeAll(() => {
  loadScript('sources/static/sources/log_viewer.js');
  h = globalThis.StarcubesLogViewer.helpers;
});

describe('esc', () => {
  it('escapes every HTML-relevant character', () => {
    expect(h.esc('<a href="x">&\'</a>'))
      .toBe('&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;');
    expect(h.esc(null)).toBe('');
    expect(h.esc(42)).toBe('42');
  });
});

describe('time helpers', () => {
  it('timeOnly hides today, shows the date otherwise', () => {
    const now = new Date();
    expect(h.timeOnly(now.toISOString())).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    const old = new Date(now.getTime() - 24 * 3600 * 1000);
    expect(h.timeOnly(old.toISOString()))
      .toMatch(/^\d{2}\.\d{2}\. \d{2}:\d{2}:\d{2}$/);
    expect(h.timeOnly(null)).toBe('—');
    expect(h.timeOnly('kein-datum')).toBe('kein-datum');
  });

  it('clockOnly returns the clock or the raw value', () => {
    expect(h.clockOnly('2026-01-02T03:04:05Z')).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(h.clockOnly(null)).toBe('—');
    expect(h.clockOnly('x')).toBe('x');
  });

  it('pads to two digits', () => {
    expect(h.pad2(5)).toBe('05');
    expect(h.pad2(12)).toBe('12');
  });
});

describe('fmtDuration', () => {
  it.each([
    [0, '0s'],
    [42000, '42s'],
    [192000, '3m 12s'],
    [7500000, '2h 05m'],
    [97200000, '1d 3h'],
    [-1, '—'],
  ])('%s ms → %s', (ms, text) => {
    expect(h.fmtDuration(ms)).toBe(text);
  });
});

describe('shortHost', () => {
  it('shortens names but never addresses', () => {
    expect(h.shortHost('host.example.com')).toBe('host');
    expect(h.shortHost('host')).toBe('host');
    expect(h.shortHost('10.0.0.12')).toBe('10.0.0.12');
    expect(h.shortHost('[::1]:80')).toBe('[::1]:80');
    expect(h.shortHost('')).toBe('');
  });
});

describe('kind and status markup', () => {
  it('sanitises kind names for class names', () => {
    expect(h.kindClass('etl run')).toBe('lv-k-etl_run');
    expect(h.kindClass('rollup_build')).toBe('lv-k-rollup_build');
    expect(h.kindChip('etl')).toContain('lv-k-etl');
    expect(h.kindChip('etl')).toContain('etl');
  });

  it('maps statuses to badge classes', () => {
    expect(h.statusBadge('running')).toContain('badge-running');
    expect(h.statusBadge('stopping')).toContain('badge-running');
    expect(h.statusBadge('ok')).toContain('badge-ok');
    expect(h.statusBadge('failed')).toContain('badge-failed');
    expect(h.statusBadge('released')).toContain('badge-none');
  });
});

describe('filter comparison', () => {
  it('compares arrays by value and copies without aliasing', () => {
    expect(h.sameFilter(['etl'], ['etl'])).toBe(true);
    expect(h.sameFilter(['etl'], ['forever'])).toBe(false);
    const src = ['etl'];
    const copy = h.copyFilter(src);
    copy.push('forever');
    expect(src).toEqual(['etl']);
    expect(h.copyFilter('all')).toBe('all');
  });
});
