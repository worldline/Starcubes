/* The one log viewer: every process's lines and every run, behind one filter
   panel. Used by /sources/logs/ and embedded in the ETL tab's Log sub-tab with
   the datasource fixed (sources/_log_viewer.html). Backend:
   sources/log_views.py.

   new StarcubesLogViewer(rootElement) — configured by data attributes on the
   root: data-lines-url, data-runs-url, data-fixed-datasource (optional),
   data-url-state="1" to mirror the filters into the page URL.
   API: activate(), deactivate(), showRun(runRef, fromIso, toIso), .active.

   Both views are newest first: the API returns every lines page that way
   (initial, before_id and after_id alike), so Live inserts at the top and
   "Load older" appends at the bottom.

   Needs getJSON() and fmtLocal() from sources/base.html. */
(function () {
  'use strict';

  var FOLLOW_MS = 2000;        // Lines: after_id poll
  var RUNS_MS = 5000;          // Runs: first-page reload
  // Every status the view `run` can report, for sizing the status column.
  var STATUSES = ['running', 'stopping', 'ok', 'failed', 'stopped', 'released'];
  var MAX_DOM_LINES = 5000;
  var TOP_SLACK = 40;          // px from the top that still counts as "at the top"

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // The time of day, with the date in front unless it is today: a log
  // spanning days must not read as one.
  function timeOnly(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    var t = pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
    return d.toDateString() === new Date().toDateString()
      ? t : pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '. ' + t;
  }

  // Only the clock: a run's end is read next to its start and its duration,
  // and the duration already says when it crossed a day.
  function clockOnly(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  // Pixel width of the widest string in `el`'s font, for columns sized to
  // what they actually hold rather than to a guess.
  var measureCtx = null;
  function widest(texts, el) {
    measureCtx = measureCtx || document.createElement('canvas').getContext('2d');
    var cs = window.getComputedStyle(el);
    measureCtx.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
    return texts.reduce(function (w, t) {
      return Math.max(w, Math.ceil(measureCtx.measureText(t).width));
    }, 0);
  }

  // 42s, 3m 12s, 2h 05m, 1d 3h — two units at most; a log reader wants the
  // order of magnitude, not the milliseconds.
  function fmtDuration(ms) {
    if (!(ms >= 0)) return '—';
    var s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + pad2(s % 60) + 's';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h ' + pad2(m % 60) + 'm';
    return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
  }

  // The first label of a host name, but never of an address: 10.0.0.12 must
  // not turn into "10". The full name always goes into the title.
  function shortHost(h) {
    if (!h) return '';
    h = String(h);
    if (h.indexOf(':') >= 0 || /^\d{1,3}(\.\d{1,3}){3}$/.test(h)) return h;
    return h.split('.')[0];
  }

  // Kinds become class names (.lv-k-<kind>, colour in log_viewer.css).
  function kindClass(kind) {
    return 'lv-k-' + String(kind || '').replace(/[^a-z0-9_-]/gi, '_');
  }

  function kindChip(kind, extra) {
    return '<span class="lv-kchip ' + kindClass(kind) + '"' + (extra || '') + '>' + esc(kind) + '</span>';
  }

  // datetime-local shows local time without a zone; the API wants an instant.
  function localInputToIso(v) {
    if (!v) return '';
    var d = new Date(v);
    return isNaN(d.getTime()) ? '' : d.toISOString();
  }
  function isoToLocalInput(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
      + 'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function statusBadge(status) {
    var cls = {running: 'badge-running', stopping: 'badge-running', ok: 'badge-ok',
               failed: 'badge-failed'}[status] || 'badge-none';
    return '<span class="badge ' + cls + '">' + esc(status) + '</span>';
  }

  function nowClock() {
    var d = new Date();
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function LogViewer(root) {
    this.root = root;
    this.linesUrl = root.getAttribute('data-lines-url');
    this.runsUrl = root.getAttribute('data-runs-url');
    this.fixedDs = root.getAttribute('data-fixed-datasource') || '';
    this.urlState = root.getAttribute('data-url-state') === '1';
    this.mode = 'lines';
    this.active = false;
    this.live = true;
    this.timer = null;
    this.busy = false;         // a poll request is in flight
    this.seq = 0;              // bumped by every reload; stale responses are dropped
    this.newestId = null;
    this.oldestId = null;
    this.unseen = 0;           // lines added above a scrolled-down reader
    this.runsCursor = null;
    this.runsPaged = false;    // older runs pages are loaded
    this.runsMap = {};         // run_ref -> {kind, id, ...}, for the run chip
    this.statusMsg = '';
    this.dsNames = {};
    this.kinds = [];
    this.filters = {datasource: '', kind: [], host: '', level: '', run: '',
                    from: '', to: '', q: ''};
    var self = this;
    this.el = function (name) { return root.querySelector('[data-lv="' + name + '"]'); };
    Array.prototype.forEach.call(root.querySelectorAll('[data-lv="datasource"] option'),
      function (o) { if (o.value) self.dsNames[o.value] = o.textContent; });
    Array.prototype.forEach.call(this.el('kind-add').options,
      function (o) { if (o.value) self.kinds.push(o.value); });
    // Short labels in the host filter; the value stays the full name.
    Array.prototype.forEach.call(this.el('host').options, function (o) {
      if (o.value) { o.textContent = shortHost(o.value); o.title = o.value; }
    });
    this.sizeColumns();
    if (this.urlState) this.readUrl();
    this.syncControls();
    this.wire();
  }

  // The datasource column is as wide as the longest datasource name (within
  // bounds), and the status column as wide as its widest badge: fixed widths
  // either wasted space or cut the summary.
  LogViewer.prototype.sizeColumns = function () {
    var root = this.root;
    var names = Object.keys(this.dsNames).map(function (k) { return this.dsNames[k]; }, this);
    // 78px is the DATASOURCE header itself (10px caps, letter-spaced).
    var clamp = function (w) { return Math.min(Math.max(w, 78), 220) + 'px'; };
    if (names.length) {
      root.style.setProperty('--lv-ds-line-w', clamp(widest(names, this.el('lines'))));
      root.style.setProperty('--lv-ds-run-w', clamp(widest(names, this.el('runs-body'))));
    }
    this.sizeStatus();
  };

  // Measured in a throwaway table with the real classes, so the badge gets
  // the table's padding. An embed constructed inside a hidden tab lays out
  // nothing — then this returns false and the first runs render retries.
  LogViewer.prototype.sizeStatus = function () {
    if (this.statusSized) return true;
    var probe = document.createElement('table');
    probe.className = 'lv-table';
    probe.style.cssText = 'position:absolute;visibility:hidden;width:auto;min-width:0;table-layout:auto';
    probe.innerHTML = '<tbody>' + STATUSES.map(function (st) {
      return '<tr><td>' + statusBadge(st) + '<span class="lv-alive"></span></td></tr>';
    }).join('') + '</tbody>';
    this.root.appendChild(probe);
    var w = probe.offsetWidth;
    probe.remove();
    if (!w) return false;
    this.root.style.setProperty('--lv-status-w', w + 'px');
    return (this.statusSized = true);
  };

  LogViewer.prototype.readUrl = function () {
    var q = new URLSearchParams(window.location.search);
    var f = this.filters;
    ['datasource', 'host', 'level', 'run', 'from', 'to', 'q'].forEach(function (k) {
      if (q.get(k)) f[k] = q.get(k);
    });
    f.kind = q.getAll('kind');
    if (q.get('mode') === 'runs') this.mode = 'runs';
    if (q.get('live') === '0') this.live = false;
  };

  LogViewer.prototype.writeUrl = function () {
    if (!this.urlState) return;
    var q = this.query(true);
    if (this.mode === 'runs') q.append('mode', 'runs');
    if (!this.live) q.append('live', '0');
    var s = q.toString();
    window.history.replaceState(null, '', window.location.pathname + (s ? '?' + s : ''));
  };

  LogViewer.prototype.query = function (forUrl) {
    var f = this.filters, q = new URLSearchParams();
    var ds = this.fixedDs || f.datasource;
    if (ds && !(forUrl && this.fixedDs)) q.append('datasource', ds);
    f.kind.forEach(function (k) { q.append('kind', k); });
    ['host', 'from', 'to'].forEach(function (k) { if (f[k]) q.append(k, f[k]); });
    if (forUrl || this.mode === 'lines') {
      ['level', 'run', 'q'].forEach(function (k) { if (f[k]) q.append(k, f[k]); });
    }
    return q;
  };

  LogViewer.prototype.syncControls = function () {
    var f = this.filters, el = this.el;
    if (el('datasource')) el('datasource').value = this.fixedDs || f.datasource;
    this.syncKinds();
    el('host').value = f.host;
    el('level').value = f.level;
    el('from').value = isoToLocalInput(f.from);
    el('to').value = isoToLocalInput(f.to);
    el('q').value = f.q;
    this.syncRunChip();
    this.syncLive();
    var lines = this.mode === 'lines';
    el('mode-lines').classList.toggle('active', lines);
    el('mode-runs').classList.toggle('active', !lines);
    el('mode-lines').setAttribute('aria-pressed', String(lines));
    el('mode-runs').setAttribute('aria-pressed', String(!lines));
    el('lines-wrap').hidden = !lines;
    el('runs').hidden = lines;
    el('lines-only').hidden = !lines;
    el('lines-notice').hidden = !lines;
    el('runs-notice').hidden = lines;
  };

  // The kind field: one chip per selected kind, then a select offering the rest.
  LogViewer.prototype.syncKinds = function () {
    var sel = this.filters.kind;
    this.el('kind-chips').innerHTML = sel.length
      ? sel.map(function (k) {
          return '<span class="lv-kchip lv-kchip-rm ' + kindClass(k) + '">' + esc(k)
            + '<button type="button" data-kind-remove="' + esc(k) + '" title="remove"'
            + ' aria-label="Remove kind ' + esc(k) + '">×</button></span>';
        }).join('')
      : '<span class="lv-kinds-all">all kinds</span>';
    var rest = this.kinds.filter(function (k) { return sel.indexOf(k) < 0; });
    var add = this.el('kind-add');
    add.innerHTML = '<option value="">+ kind</option>' + rest.map(function (k) {
      return '<option value="' + esc(k) + '">' + esc(k) + '</option>';
    }).join('');
    add.value = '';
    add.hidden = !rest.length;
  };

  LogViewer.prototype.syncRunChip = function () {
    var ref = this.filters.run, run = this.runsMap[ref];
    var name = this.el('run-name');
    this.el('run-chip').hidden = !ref;
    name.className = run ? 'lv-kchip ' + kindClass(run.kind) : 'lv-runref';
    name.textContent = run ? run.kind + ' #' + run.id : ref;
    name.title = ref;
  };

  LogViewer.prototype.syncLive = function () {
    var b = this.el('live');
    b.setAttribute('aria-pressed', String(this.live));
    b.classList.toggle('on', this.live);
    b.title = this.live ? 'Live — click to pause' : 'Paused — click to go live';
  };

  LogViewer.prototype.readControls = function () {
    var f = this.filters, el = this.el;
    if (el('datasource') && !this.fixedDs) f.datasource = el('datasource').value;
    f.host = el('host').value;
    f.level = el('level').value;
    f.from = localInputToIso(el('from').value);
    f.to = localInputToIso(el('to').value);
    f.q = el('q').value.trim();
  };

  LogViewer.prototype.wire = function () {
    var self = this, el = this.el;
    var onChange = function () { self.readControls(); self.changed(); };
    ['datasource', 'host', 'level', 'from', 'to'].forEach(function (n) {
      if (el(n)) el(n).addEventListener('change', onChange);
    });
    var qTimer = null;
    el('q').addEventListener('input', function () {
      clearTimeout(qTimer);
      qTimer = setTimeout(onChange, 400);
    });

    var add = el('kind-add');
    add.addEventListener('change', function () {
      if (add.value && self.filters.kind.indexOf(add.value) < 0) {
        self.filters.kind.push(add.value);
        self.changed();
        if (!add.hidden) add.focus();
      }
    });
    add.addEventListener('keydown', function (ev) {
      if (ev.key === 'Backspace' && self.filters.kind.length) {
        ev.preventDefault();
        self.filters.kind.pop();
        self.changed();
      }
    });
    el('kinds').addEventListener('click', function (ev) {
      var rm = ev.target.closest('[data-kind-remove]');
      if (rm) {
        var k = rm.getAttribute('data-kind-remove');
        self.filters.kind = self.filters.kind.filter(function (x) { return x !== k; });
        self.changed();
        if (!add.hidden) add.focus();
        return;
      }
      // A click on the field's empty space opens the picker, as an input would.
      if (ev.target === el('kinds') || ev.target === el('kind-chips')
          || ev.target.classList.contains('lv-kinds-all')) {
        if (add.hidden) return;
        add.focus();
        try { if (add.showPicker) add.showPicker(); } catch (e) { /* not allowed here */ }
      }
    });

    el('run-clear').addEventListener('click', function () {
      self.filters.run = '';
      self.changed();
    });
    el('mode-lines').addEventListener('click', function () { self.setMode('lines'); });
    el('mode-runs').addEventListener('click', function () { self.setMode('runs'); });
    el('refresh').addEventListener('click', function () { self.reload(); });
    el('live').addEventListener('click', function () { self.setLive(!self.live); });

    // A cell on a line sets that filter.
    el('lines').addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-f]');
      if (b) { self.setFilter(b.getAttribute('data-f'), b.getAttribute('data-v')); return; }
      if (ev.target.closest('[data-lv="older"]')) self.older();
    });
    el('lines').addEventListener('scroll', function () {
      if (self.unseen && el('lines').scrollTop <= TOP_SLACK) self.clearUnseen();
    });
    el('new-pill').addEventListener('click', function () {
      el('lines').scrollTop = 0;
      self.clearUnseen();
    });
    // A run row opens its lines.
    el('runs').addEventListener('click', function (ev) {
      if (ev.target.closest('[data-lv="more-runs"]')) { self.loadRuns(true); return; }
      var tr = ev.target.closest('tr[data-open]');
      if (tr) self.openRun(JSON.parse(tr.getAttribute('data-open')));
    });
    // A hidden tab polls for nobody; catch up at once when it comes back.
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { clearTimeout(self.timer); self.timer = null; }
      else if (self.active) self.poll();
    });
  };

  LogViewer.prototype.setFilter = function (name, value) {
    if (name === 'kind') this.filters.kind = [value];
    else if (name === 'datasource' && this.fixedDs) return;
    else this.filters[name] = value;
    this.changed();
  };

  LogViewer.prototype.setMode = function (mode) {
    this.mode = mode;
    this.changed();
  };

  LogViewer.prototype.setLive = function (on) {
    this.live = on;
    this.syncLive();
    this.writeUrl();
    if (on) this.poll();
    else { clearTimeout(this.timer); this.timer = null; }
    this.renderStatus();
  };

  LogViewer.prototype.changed = function () {
    this.syncControls();
    this.writeUrl();
    this.reload();
  };

  LogViewer.prototype.showRun = function (runRef, fromIso, toIso) {
    this.filters.run = runRef || '';
    this.filters.from = fromIso || '';
    this.filters.to = toIso || '';
    this.filters.level = '';
    this.filters.q = '';
    this.mode = 'lines';
    this.changed();
  };

  LogViewer.prototype.openRun = function (run) {
    // A run's lines, framed by its own time span; a few seconds either side
    // because a line's ts is when it was shipped. Audit and housekeeping have
    // no log of their own, so they frame the datasource's lines instead.
    var pad = 5000;
    var from = run.started_at ? new Date(new Date(run.started_at).getTime() - pad).toISOString() : '';
    var to = run.finished_at ? new Date(new Date(run.finished_at).getTime() + pad).toISOString() : '';
    this.filters.kind = [];
    if (run.run_ref) {
      if (run.kind) this.runsMap[run.run_ref] = {kind: run.kind, id: run.id};
      this.showRun(run.run_ref, from, to);
    } else {
      if (!this.fixedDs) this.filters.datasource = String(run.datasource_id || '');
      this.showRun('', from, to);
    }
  };

  LogViewer.prototype.activate = function () {
    this.active = true;
    this.reload();
  };

  LogViewer.prototype.deactivate = function () {
    this.active = false;
    clearTimeout(this.timer);
    this.timer = null;
  };

  // The status line: what is shown, when it was last fetched, and why a poll
  // is holding back. An error replaces it until the next success.
  LogViewer.prototype.status = function (msg) {
    this.statusMsg = msg || '';
    this.el('status').textContent = this.statusMsg;
    this.el('status').classList.toggle('lv-status-err', /^✗/.test(this.statusMsg));
  };

  LogViewer.prototype.renderStatus = function (note) {
    var n, s;
    if (this.mode === 'lines') {
      n = this.el('lines-body').querySelectorAll('.lv-ln').length;
      s = n + (n === 1 ? ' line' : ' lines');
    } else {
      n = this.el('runs-body').querySelectorAll('tr[data-open]').length;
      s = n + (n === 1 ? ' run' : ' runs');
    }
    if (this.updatedAt) s += ' · updated ' + this.updatedAt;
    if (!this.live) s += ' · live paused';
    else if (note) s += ' · ' + note;
    this.status(s);
  };

  LogViewer.prototype.fetched = function () { this.updatedAt = nowClock(); };

  LogViewer.prototype.reload = function (quiet) {
    clearTimeout(this.timer);
    this.timer = null;
    this.seq++;
    this.busy = false;
    if (this.mode === 'runs') { this.loadRuns(false, quiet); return; }
    var self = this, seq = this.seq;
    if (!quiet) this.status('loading…');
    getJSON(this.linesUrl + '?' + this.query(false).toString()).then(function (d) {
      if (seq !== self.seq) return;
      if (!d.ok) { self.status('✗ ' + d.error); self.schedule(); return; }
      self.rememberRuns(d.runs);
      var pane = self.el('lines');
      self.newestId = d.lines.length ? d.lines[0].id : null;
      self.oldestId = d.lines.length ? d.lines[d.lines.length - 1].id : null;
      self.el('lines-body').innerHTML = d.lines.length
        ? d.lines.map(function (l) { return self.lineHtml(l, d.runs); }).join('')
        : '<div class="lv-empty">No lines match.</div>';
      self.el('older-wrap').hidden = !d.more_before;
      pane.scrollTop = 0;
      self.clearUnseen();
      self.fetched();
      self.renderStatus();
      self.schedule();
    }).catch(function (e) {
      if (seq !== self.seq) return;
      self.status('✗ ' + e);
      self.schedule();
    });
  };

  LogViewer.prototype.rememberRuns = function (runs) {
    var self = this;
    Object.keys(runs || {}).forEach(function (ref) { self.runsMap[ref] = runs[ref]; });
    if (this.filters.run) this.syncRunChip();
  };

  // One timer for both views; nothing is scheduled while paused, hidden or
  // inactive — visibilitychange, setLive and activate start it again.
  LogViewer.prototype.schedule = function () {
    var self = this;
    clearTimeout(this.timer);
    this.timer = null;
    if (!this.active || !this.live || document.hidden) return;
    this.timer = setTimeout(function () { self.poll(); },
                            this.mode === 'lines' ? FOLLOW_MS : RUNS_MS);
  };

  LogViewer.prototype.poll = function () {
    if (this.mode === 'lines') this.tick();
    else this.runsTick();
  };

  // Live, lines: fetch what arrived after the newest line on screen and put
  // it on top, without moving a reader who has scrolled down.
  LogViewer.prototype.tick = function () {
    var self = this;
    if (!this.active || !this.live || this.mode !== 'lines' || document.hidden) return;
    if (this.busy) { this.schedule(); return; }
    if (this.newestId == null) { this.reload(true); return; }
    var q = this.query(false), seq = this.seq;
    q.append('after_id', this.newestId);
    this.busy = true;
    getJSON(this.linesUrl + '?' + q.toString()).then(function (d) {
      if (seq !== self.seq) return;
      self.busy = false;
      if (!d.ok) { self.status('✗ ' + d.error); self.schedule(); return; }
      self.fetched();
      if (d.lines.length) self.prependLines(d);
      self.renderStatus();
      self.schedule();
    }).catch(function (e) {
      if (seq !== self.seq) return;
      self.busy = false;
      self.status('✗ ' + e);
      self.schedule();
    });
  };

  LogViewer.prototype.prependLines = function (d) {
    var self = this, pane = this.el('lines'), body = this.el('lines-body');
    this.rememberRuns(d.runs);
    var empty = body.querySelector('.lv-empty');
    if (empty) empty.remove();
    // overflow-anchor is off on the pane (CSS), so this is the only correction.
    var atTop = pane.scrollTop <= TOP_SLACK;
    var before = pane.scrollHeight;
    body.insertAdjacentHTML('afterbegin',
      d.lines.map(function (l) { return self.lineHtml(l, d.runs); }).join(''));
    this.newestId = d.lines[0].id;
    if (atTop) {
      pane.scrollTop = 0;
    } else {
      pane.scrollTop += pane.scrollHeight - before;
      this.unseen += d.lines.length;
      var pill = this.el('new-pill');
      pill.textContent = '▲ ' + this.unseen + (this.unseen === 1 ? ' new line' : ' new lines');
      pill.hidden = false;
    }
    // The cap drops the oldest rows, which are the bottom ones now.
    var rows = body.children, extra = rows.length - MAX_DOM_LINES;
    if (extra > 0) {
      while (extra-- > 0) body.lastElementChild.remove();
      this.oldestId = Number(body.lastElementChild.getAttribute('data-id'));
      this.el('older-wrap').hidden = false;
    }
  };

  LogViewer.prototype.clearUnseen = function () {
    this.unseen = 0;
    this.el('new-pill').hidden = true;
  };

  LogViewer.prototype.older = function () {
    var self = this, seq = this.seq;
    if (this.oldestId == null || this.olderBusy) return;
    var q = this.query(false);
    q.append('before_id', this.oldestId);
    this.olderBusy = true;
    getJSON(this.linesUrl + '?' + q.toString()).then(function (d) {
      self.olderBusy = false;
      if (seq !== self.seq) return;
      if (!d.ok) { self.status('✗ ' + d.error); return; }
      self.rememberRuns(d.runs);
      self.el('lines-body').insertAdjacentHTML('beforeend',
        d.lines.map(function (l) { return self.lineHtml(l, d.runs); }).join(''));
      if (d.lines.length) self.oldestId = d.lines[d.lines.length - 1].id;
      self.el('older-wrap').hidden = !d.more_before;
      self.renderStatus();
    }).catch(function (e) { self.olderBusy = false; self.status('✗ ' + e); });
  };

  // One grid row; the column set matches .lv-hrow in the template and
  // --lv-cols in the CSS. data-f/data-v cells filter on click.
  LogViewer.prototype.lineHtml = function (l, runs) {
    var run = runs && runs[l.run_ref];
    var level = l.level || '';
    var h = '<div class="lv-ln ' + kindClass(l.kind) + (level ? ' lv-lvl-' + esc(level) : '')
      + '" data-id="' + esc(l.id) + '">';
    h += '<span class="lv-c lv-c-ts" title="' + esc(fmtLocal(l.ts)) + '">' + esc(timeOnly(l.ts)) + '</span>';
    h += '<span class="lv-c">' + (level
      ? '<span class="lv-lvl lv-lvl-b-' + esc(level) + '">' + esc(level) + '</span>'
      : '<span class="lv-dim" title="no level">—</span>') + '</span>';
    h += '<span class="lv-c">' + kindChip(l.kind, ' data-f="kind" data-v="' + esc(l.kind)
      + '" title="' + esc(l.kind) + ' — click to filter"') + '</span>';
    if (!this.fixedDs) {
      h += l.datasource_id != null
        ? '<span class="lv-c lv-f" data-f="datasource" data-v="' + esc(l.datasource_id)
          + '" title="' + esc(l.datasource || l.datasource_id) + ' — click to filter">'
          + esc(l.datasource || l.datasource_id) + '</span>'
        : '<span class="lv-c lv-dim">—</span>';
    }
    h += l.hostname
      ? '<span class="lv-c lv-f lv-c-host" data-f="host" data-v="' + esc(l.hostname)
        + '" title="' + esc(l.hostname) + ' — click to filter">' + esc(shortHost(l.hostname)) + '</span>'
      : '<span class="lv-c lv-dim">—</span>';
    h += '<span class="lv-c lv-f lv-c-run" data-f="run" data-v="' + esc(l.run_ref) + '" title="'
      + esc(l.run_ref) + (run ? ' — ' + esc(run.kind) + ' #' + esc(run.id) + ', ' + esc(run.status) : '')
      + ' — click to filter">' + (run ? (run.kind === l.kind ? '' : esc(run.kind) + ' ') + '#' + esc(run.id)
                                   : esc(l.run_ref)) + '</span>';
    h += '<span class="lv-c lv-c-msg">' + esc(l.line) + '</span></div>';
    return h;
  };

  // Live, runs: reload the first page — unless the reader is looking at
  // something a reload would take away (older pages, a scrolled table).
  LogViewer.prototype.runsTick = function () {
    if (!this.active || !this.live || this.mode !== 'runs' || document.hidden) return;
    if (this.busy) { this.schedule(); return; }
    if (this.runsPaged || this.el('runs').scrollTop > 4) {
      this.renderStatus('paused while browsing older runs');
      this.schedule();
      return;
    }
    this.loadRuns(false, true);
  };

  LogViewer.prototype.loadRuns = function (more, quiet) {
    // An older page supersedes an in-flight first-page poll, whose answer
    // would otherwise replace the rows this appends to.
    if (more) this.seq++;
    var self = this, seq = this.seq;
    var q = this.query(false);
    if (more && this.runsCursor) {
      q.append('before_started', this.runsCursor.started_at);
      q.append('before_kind', this.runsCursor.kind);
      q.append('before_id', this.runsCursor.id);
    }
    if (!quiet) this.status('loading…');
    this.busy = true;
    getJSON(this.runsUrl + '?' + q.toString()).then(function (d) {
      if (seq !== self.seq) return;
      self.busy = false;
      if (!d.ok) { self.status('✗ ' + d.error); self.schedule(); return; }
      var body = self.el('runs-body');
      var now = Date.now();
      self.sizeStatus();
      var html = d.runs.map(function (r) { return self.runHtml(r, now); }).join('');
      if (more) {
        body.insertAdjacentHTML('beforeend', html);
        self.runsPaged = true;
      } else {
        body.innerHTML = html || '<tr><td colspan="' + (self.fixedDs ? 9 : 10)
          + '" class="lv-empty">No runs match.</td></tr>';
        self.runsPaged = false;
      }
      if (d.runs.length) self.runsCursor = d.runs[d.runs.length - 1];
      else if (!more) self.runsCursor = null;
      self.el('more-runs').hidden = !d.more;
      self.fetched();
      self.renderStatus(self.runsPaged && self.live ? 'paused while browsing older runs' : '');
      self.schedule();
    }).catch(function (e) {
      if (seq !== self.seq) return;
      self.busy = false;
      self.status('✗ ' + e);
      self.schedule();
    });
  };

  LogViewer.prototype.runHtml = function (r, now) {
    var open = esc(JSON.stringify({run_ref: r.run_ref, kind: r.kind, id: r.id,
                                   started_at: r.started_at, finished_at: r.finished_at,
                                   datasource_id: r.datasource_id}));
    var start = r.started_at ? new Date(r.started_at).getTime() : NaN;
    var dur;
    if (r.finished_at) {
      var d = fmtDuration(new Date(r.finished_at).getTime() - start);
      dur = '<span title="took ' + d + '">' + d + '</span>';
    } else if (!isNaN(start)) {
      var live = fmtDuration(now - start);
      dur = '<span class="lv-dur-open' + (r.alive ? ' lv-dur-live' : '') + '" title="running for '
        + live + (r.alive ? '' : ' (process not alive)') + '">' + live + '</span>';
    } else {
      dur = '<span class="lv-dim">—</span>';
    }
    var cell = function (text, cls, title) {
      return '<td' + (cls ? ' class="' + cls + '"' : '') + ' title="' + esc(title == null ? text : title)
        + '">' + (text ? esc(text) : '<span class="lv-dim">—</span>') + '</td>';
    };
    return '<tr data-open="' + open + '" class="' + kindClass(r.kind) + '">'
      + '<td>' + kindChip(r.kind, ' title="' + esc(r.kind) + '"') + '</td>'
      + '<td class="lv-mono lv-num">' + esc(r.id) + '</td>'
      + (this.fixedDs ? '' : cell(r.datasource || '', ''))
      + '<td title="' + esc(r.status) + (r.alive ? ', process alive' : '') + '">' + statusBadge(r.status)
      + (r.alive ? '<span class="lv-alive" aria-label="process alive"></span>' : '') + '</td>'
      + '<td class="lv-mono" title="' + esc(fmtLocal(r.started_at)) + '">' + esc(timeOnly(r.started_at)) + '</td>'
      + '<td class="lv-mono" title="' + esc(r.finished_at ? fmtLocal(r.finished_at) : 'not finished') + '">'
      + (r.finished_at ? esc(clockOnly(r.finished_at)) : '<span class="lv-dim">—</span>') + '</td>'
      + '<td class="lv-mono">' + dur + '</td>'
      + cell(shortHost(r.hostname), 'lv-mono lv-muted', r.hostname || '')
      + cell(r.summary || '', 'lv-wrap')
      + '<td class="lv-err" title="' + esc(r.error || '') + '">'
      + (r.error ? '<div class="lv-clamp">' + esc(r.error) + '</div>' : '<span class="lv-dim">—</span>') + '</td>'
      + '</tr>';
  };

  window.StarcubesLogViewer = LogViewer;
})();
