/* The one log viewer: every process's lines and every run, behind one filter
   bar. Used by /sources/logs/ and embedded in the ETL tab's Log sub-tab with
   the datasource fixed (sources/_log_viewer.html). Backend:
   sources/log_views.py.

   new StarcubesLogViewer(rootElement) — configured by data attributes on the
   root: data-lines-url, data-runs-url, data-fixed-datasource (optional),
   data-url-state="1" to mirror the filters into the page URL.
   API: activate(), deactivate(), showRun(runRef, fromIso, toIso).

   Needs getJSON() and fmtLocal() from sources/base.html. */
(function () {
  'use strict';

  var FOLLOW_MS = 2000;
  var MAX_DOM_LINES = 5000;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  // The time of day, with the date in front unless it is today: a log
  // spanning days must not read as one.
  function timeOnly(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    var t = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    return d.toDateString() === new Date().toDateString()
      ? t : p(d.getDate()) + '.' + p(d.getMonth() + 1) + '. ' + t;
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
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
      + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function statusBadge(status) {
    var cls = {running: 'badge-running', stopping: 'badge-running', ok: 'badge-ok',
               failed: 'badge-failed'}[status] || 'badge-none';
    return '<span class="badge ' + cls + '">' + esc(status) + '</span>';
  }

  function LogViewer(root) {
    this.root = root;
    this.linesUrl = root.getAttribute('data-lines-url');
    this.runsUrl = root.getAttribute('data-runs-url');
    this.fixedDs = root.getAttribute('data-fixed-datasource') || '';
    this.urlState = root.getAttribute('data-url-state') === '1';
    this.mode = 'lines';
    this.active = false;
    this.timer = null;
    this.busy = false;
    this.newestId = null;
    this.oldestId = null;
    this.runsCursor = null;
    this.dsNames = {};
    this.filters = {datasource: '', kind: [], host: '', level: '', run: '',
                    from: '', to: '', q: ''};
    var self = this;
    this.el = function (name) { return root.querySelector('[data-lv="' + name + '"]'); };
    Array.prototype.forEach.call(root.querySelectorAll('[data-lv="datasource"] option'),
      function (o) { if (o.value) self.dsNames[o.value] = o.textContent; });
    if (this.urlState) this.readUrl();
    this.syncControls();
    this.wire();
  }

  LogViewer.prototype.readUrl = function () {
    var q = new URLSearchParams(window.location.search);
    var f = this.filters;
    ['datasource', 'host', 'level', 'run', 'from', 'to', 'q'].forEach(function (k) {
      if (q.get(k)) f[k] = q.get(k);
    });
    f.kind = q.getAll('kind');
    if (q.get('mode') === 'runs') this.mode = 'runs';
  };

  LogViewer.prototype.writeUrl = function () {
    if (!this.urlState) return;
    var q = this.query(true);
    if (this.mode === 'runs') q.append('mode', 'runs');
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
    Array.prototype.forEach.call(this.root.querySelectorAll('[data-lv="kind"]'),
      function (cb) { cb.checked = f.kind.indexOf(cb.value) >= 0; });
    el('host').value = f.host;
    el('level').value = f.level;
    el('from').value = isoToLocalInput(f.from);
    el('to').value = isoToLocalInput(f.to);
    el('q').value = f.q;
    el('run-chip').hidden = !f.run;
    el('run-name').textContent = f.run;
    var lines = this.mode === 'lines';
    el('mode-lines').classList.toggle('active', lines);
    el('mode-runs').classList.toggle('active', !lines);
    el('lines').hidden = !lines;
    el('runs').hidden = lines;
    el('lines-only').hidden = !lines;
  };

  LogViewer.prototype.readControls = function () {
    var f = this.filters, el = this.el;
    if (el('datasource') && !this.fixedDs) f.datasource = el('datasource').value;
    f.kind = Array.prototype.filter.call(this.root.querySelectorAll('[data-lv="kind"]'),
      function (cb) { return cb.checked; }).map(function (cb) { return cb.value; });
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
    Array.prototype.forEach.call(this.root.querySelectorAll('[data-lv="kind"]'),
      function (cb) { cb.addEventListener('change', onChange); });
    var qTimer = null;
    el('q').addEventListener('input', function () {
      clearTimeout(qTimer);
      qTimer = setTimeout(onChange, 400);
    });
    el('run-clear').addEventListener('click', function () {
      self.filters.run = '';
      self.changed();
    });
    el('mode-lines').addEventListener('click', function () { self.setMode('lines'); });
    el('mode-runs').addEventListener('click', function () { self.setMode('runs'); });
    el('refresh').addEventListener('click', function () { self.reload(); });
    el('follow').addEventListener('change', function () {
      if (el('follow').checked) self.tick();
    });
    // A badge on a line sets that filter; a run row opens its lines.
    el('lines').addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-f]');
      if (b) { self.setFilter(b.getAttribute('data-f'), b.getAttribute('data-v')); return; }
      if (ev.target.closest('[data-lv="older"]')) self.older();
    });
    el('runs').addEventListener('click', function (ev) {
      if (ev.target.closest('[data-lv="more-runs"]')) { self.loadRuns(true); return; }
      var tr = ev.target.closest('tr[data-open]');
      if (tr) self.openRun(JSON.parse(tr.getAttribute('data-open')));
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
    if (run.run_ref) {
      this.filters.kind = [];
      this.showRun(run.run_ref, from, to);
    } else {
      if (!this.fixedDs) this.filters.datasource = String(run.datasource_id || '');
      this.filters.kind = [];
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

  LogViewer.prototype.status = function (msg) {
    this.el('status').textContent = msg || '';
  };

  LogViewer.prototype.reload = function () {
    clearTimeout(this.timer);
    this.timer = null;
    if (this.mode === 'runs') { this.loadRuns(false); return; }
    var self = this;
    this.newestId = null;
    this.oldestId = null;
    this.status('loading…');
    getJSON(this.linesUrl + '?' + this.query(false).toString()).then(function (d) {
      if (!d.ok) { self.status('✗ ' + d.error); return; }
      var pane = self.el('lines');
      pane.innerHTML = (d.more_before
        ? '<div class="lv-older"><button type="button" class="btn btn-secondary btn-sm" data-lv="older">Load older</button></div>' : '')
        + (d.lines.length ? '' : '<div class="lv-empty">No lines match.</div>')
        + d.lines.map(function (l) { return self.lineHtml(l, d.runs); }).join('');
      if (d.lines.length) {
        self.oldestId = d.lines[0].id;
        self.newestId = d.lines[d.lines.length - 1].id;
      }
      pane.scrollTop = pane.scrollHeight;
      self.status(d.lines.length + ' line(s)');
      self.schedule();
    }).catch(function (e) { self.status('✗ ' + e); self.schedule(); });
  };

  LogViewer.prototype.schedule = function () {
    var self = this;
    clearTimeout(this.timer);
    if (this.active && this.mode === 'lines') {
      this.timer = setTimeout(function () { self.tick(); }, FOLLOW_MS);
    }
  };

  // Follow: fetch what arrived after the newest line on screen.
  LogViewer.prototype.tick = function () {
    var self = this;
    if (!this.active || this.mode !== 'lines' || this.busy) return;
    if (!this.el('follow').checked) { this.schedule(); return; }
    var q = this.query(false);
    if (this.newestId != null) q.append('after_id', this.newestId);
    else { this.reload(); return; }
    this.busy = true;
    getJSON(this.linesUrl + '?' + q.toString()).then(function (d) {
      self.busy = false;
      if (!d.ok || !d.lines.length) { self.schedule(); return; }
      var pane = self.el('lines');
      var atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40;
      var empty = pane.querySelector('.lv-empty');
      if (empty) empty.remove();
      pane.insertAdjacentHTML('beforeend',
        d.lines.map(function (l) { return self.lineHtml(l, d.runs); }).join(''));
      self.newestId = d.lines[d.lines.length - 1].id;
      var rows = pane.querySelectorAll('.lv-line');
      for (var i = 0; i < rows.length - MAX_DOM_LINES; i++) rows[i].remove();
      if (atBottom) pane.scrollTop = pane.scrollHeight;
      self.schedule();
    }).catch(function () { self.busy = false; self.schedule(); });
  };

  LogViewer.prototype.older = function () {
    var self = this;
    if (this.oldestId == null) return;
    var q = this.query(false);
    q.append('before_id', this.oldestId);
    getJSON(this.linesUrl + '?' + q.toString()).then(function (d) {
      if (!d.ok) { self.status('✗ ' + d.error); return; }
      var pane = self.el('lines');
      var btn = pane.querySelector('.lv-older');
      var before = pane.scrollHeight;
      if (btn) btn.remove();
      pane.insertAdjacentHTML('afterbegin',
        (d.more_before ? '<div class="lv-older"><button type="button" class="btn btn-secondary btn-sm" data-lv="older">Load older</button></div>' : '')
        + d.lines.map(function (l) { return self.lineHtml(l, d.runs); }).join(''));
      if (d.lines.length) self.oldestId = d.lines[0].id;
      pane.scrollTop += pane.scrollHeight - before;
    });
  };

  LogViewer.prototype.lineHtml = function (l, runs) {
    var run = runs && runs[l.run_ref];
    var badges = '<span class="lv-b" data-f="kind" data-v="' + esc(l.kind) + '" title="filter: kind">' + esc(l.kind) + '</span>';
    if (!this.fixedDs && l.datasource_id != null) {
      badges += '<span class="lv-b" data-f="datasource" data-v="' + esc(l.datasource_id) + '" title="filter: datasource">'
        + esc(l.datasource || l.datasource_id) + '</span>';
    }
    badges += '<span class="lv-b" data-f="host" data-v="' + esc(l.hostname) + '" title="filter: host">' + esc(l.hostname) + '</span>';
    badges += '<span class="lv-b" data-f="run" data-v="' + esc(l.run_ref) + '" title="'
      + esc(l.run_ref) + (run ? ' — ' + esc(run.kind) + ' #' + run.id + ', ' + esc(run.status) : '')
      + '">' + (run ? esc(run.kind) + ' #' + run.id : 'run') + '</span>';
    return '<div class="lv-line lv-lvl-' + esc(l.level || 'RAW') + '">'
      + '<span class="lv-ts" title="' + esc(fmtLocal(l.ts)) + '">' + esc(timeOnly(l.ts)) + '</span>'
      + badges + '<span class="lv-text">' + esc(l.line) + '</span></div>';
  };

  LogViewer.prototype.loadRuns = function (more) {
    var self = this;
    var q = this.query(false);
    if (more && this.runsCursor) {
      q.append('before_started', this.runsCursor.started_at);
      q.append('before_kind', this.runsCursor.kind);
      q.append('before_id', this.runsCursor.id);
    }
    this.status('loading…');
    getJSON(this.runsUrl + '?' + q.toString()).then(function (d) {
      if (!d.ok) { self.status('✗ ' + d.error); return; }
      var body = self.el('runs-body');
      var html = d.runs.map(function (r) {
        var open = esc(JSON.stringify({run_ref: r.run_ref, started_at: r.started_at,
                                       finished_at: r.finished_at, datasource_id: r.datasource_id}));
        return '<tr data-open="' + open + '">'
          + '<td>' + esc(r.kind) + ' <span class="text-muted">#' + r.id + '</span></td>'
          + (self.fixedDs ? '' : '<td>' + esc(r.datasource || '—') + '</td>')
          + '<td>' + statusBadge(r.status) + (r.alive ? ' <span class="lv-live" title="process alive">●</span>' : '') + '</td>'
          + '<td class="text-mono">' + esc(fmtLocal(r.started_at)) + '</td>'
          + '<td class="text-mono">' + (r.finished_at ? esc(fmtLocal(r.finished_at)) : '—') + '</td>'
          + '<td>' + esc(r.hostname || '—') + '</td>'
          + '<td>' + esc(r.summary || '') + '</td>'
          + '<td class="text-muted">' + esc(r.error || '') + '</td>'
          + '</tr>';
      }).join('');
      if (more) body.insertAdjacentHTML('beforeend', html);
      else body.innerHTML = html || '<tr><td colspan="8" class="empty-state">No runs match.</td></tr>';
      self.runsCursor = d.runs.length ? d.runs[d.runs.length - 1] : self.runsCursor;
      self.el('more-runs').hidden = !d.more;
      self.status(body.querySelectorAll('tr[data-open]').length + ' run(s)');
    }).catch(function (e) { self.status('✗ ' + e); });
  };

  window.StarcubesLogViewer = LogViewer;
})();
