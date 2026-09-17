// Date/time picker shared by the chart page (viz/chart.html) and the sources
// shell (sources/base.html, used by the ETL and Audit dialogs on manage.html).
//
// The two apps carried separate copies that had each grown a feature the other
// lacked: the chart's knew about a `trigger` button and clamped the popup
// inside the viewport, the shell's had getUTCISO(). This file is the union —
// dropping either half would have been a regression for one of the pages.
//
// Requirements: none beyond the DOM. Include with a plain <script src> before
// the code that constructs a picker.
//
// Markup contract: the input the picker is attached to, plus an optional
// `trigger` element (a calendar button drawn inside the field). Styling comes
// from sources/static/sources/datetimepicker.css; each page supplies its own
// field chrome (.dtp-label in the shell, .dtp-field in the chart).

let _openPicker = null;

class DateTimePicker {
  // `trigger` is the calendar button drawn inside the field. It is not part
  // of the picker's own DOM, so it has to be excluded from the outside-click
  // dismiss below — otherwise pressing it would close and immediately reopen.
  constructor(input, { onChange, nextFocus, trigger }) {
    this.input = input;
    this.onChange = onChange;
    this._nextFocus = nextFocus || null;
    this._trigger = trigger || null;
    this._value = null;
    this._pending = null;
    this._pendingH = 0;
    this._pendingM = 0;
    this._viewY = 0;
    this._viewM = 0;
    this._isOpen = false;
    this._popup = document.createElement('div');
    this._popup.className = 'dtp-popup';
    this._popup.innerHTML = this._html();
    this._wire();
    input.parentElement.style.position = 'relative';
    input.parentElement.appendChild(this._popup);
    input.addEventListener('focus', () => this.open());
    input.addEventListener('keydown', e => {
      if (e.key === 'Escape') { this.close(true); e.preventDefault(); }
      if (e.key === 'Enter') {
        this._commitInput();
        if (this._nextFocus) this._nextFocus.focus();
        e.preventDefault();
      }
    });
    input.addEventListener('blur', () => {
      // Give popup clicks time to fire before closing on blur
      setTimeout(() => { if (!this._isOpen) this._commitInput(); }, 100);
    });
    document.addEventListener('mousedown', e => {
      if (this._isOpen && !this._popup.contains(e.target) && e.target !== this.input
          && !(this._trigger && this._trigger.contains(e.target))) {
        this.close(true);
        this._commitInput();
      }
    });
    if (this._trigger) {
      // Keep the caret where it is: the button must not steal focus from the
      // input, or every click would blur-commit a half-typed date.
      this._trigger.addEventListener('mousedown', e => e.preventDefault());
      this._trigger.addEventListener('click', () => {
        if (this._isOpen) { this.close(true); this._commitInput(); }
        else { this.input.focus(); this.open(); }
      });
    }
  }

  _commitInput() {
    const s = this.input.value.trim();
    if (!s) return;
    const d = this._parseUserInput(s);
    if (!d) return;
    const ms = d.getTime();
    // Skip if the popup just set this exact value (avoids double-fire)
    if (this._value && this._value.getTime() === ms) return;
    this._value = d;
    this.onChange(ms);
  }

  _parseUserInput(s) {
    // Accept "YYYY-MM-DD HH:MM" or "YYYY-MM-DD"
    const m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?$/);
    if (!m) return null;
    const y = +m[1], mo = +m[2] - 1, dy = +m[3], h = m[4] != null ? +m[4] : 0, mi = m[5] != null ? +m[5] : 0;
    if (mo < 0 || mo > 11 || dy < 1 || dy > 31 || h < 0 || h > 23 || mi < 0 || mi > 59) return null;
    const d = new Date(y, mo, dy, h, mi, 0, 0);
    if (d.getMonth() !== mo || d.getDate() !== dy) return null; // rejects e.g. Feb 30
    return d;
  }

  _html() {
    return '<div class="dtp-header">'
      + '<button type="button" class="dtp-arrow" data-dir="-1">&larr;</button>'
      + '<span class="dtp-month-year"></span>'
      + '<button type="button" class="dtp-arrow" data-dir="1">&rarr;</button>'
      + '</div>'
      + '<div class="dtp-grid"><span class="dtp-dow">Mo</span><span class="dtp-dow">Tu</span>'
      + '<span class="dtp-dow">We</span><span class="dtp-dow">Th</span>'
      + '<span class="dtp-dow">Fr</span><span class="dtp-dow">Sa</span>'
      + '<span class="dtp-dow">Su</span></div>'
      + '<div class="dtp-grid dtp-days"></div>'
      + '<div class="dtp-time-row"><label>Time '
      + '<input type="number" class="dtp-time-input" data-f="h" min="0" max="23">'
      + ' : <input type="number" class="dtp-time-input" data-f="m" min="0" max="59">'
      + '</label></div>'
      + '<div class="dtp-apply-row"><button type="button" class="dtp-apply">OK</button></div>';
  }

  _wire() {
    // Addressed by data-dir rather than by position, so reordering the
    // markup cannot silently swap the arrows.
    const prev = this._popup.querySelector('.dtp-arrow[data-dir="-1"]');
    const next = this._popup.querySelector('.dtp-arrow[data-dir="1"]');
    prev.addEventListener('click', () => this._nav(-1));
    next.addEventListener('click', () => this._nav(1));
    this._myEl = this._popup.querySelector('.dtp-month-year');
    this._daysEl = this._popup.querySelector('.dtp-days');
    this._hIn = this._popup.querySelector('[data-f="h"]');
    this._mIn = this._popup.querySelector('[data-f="m"]');
    this._hIn.addEventListener('change', () => {
      this._pendingH = this._clamp(this._hIn.value, 0, 23);
      this._hIn.value = this._pad(this._pendingH);
    });
    this._mIn.addEventListener('change', () => {
      this._pendingM = this._clamp(this._mIn.value, 0, 59);
      this._mIn.value = this._pad(this._pendingM);
    });
    this._popup.querySelector('.dtp-apply').addEventListener('click', () => this.close(false));
  }

  _clamp(v, lo, hi) {
    const n = parseInt(v, 10);
    return isNaN(n) ? lo : Math.max(lo, Math.min(hi, n));
  }

  _pad(n) { return String(n).padStart(2, '0'); }

  _nav(dir) {
    this._viewM += dir;
    if (this._viewM < 0) { this._viewM = 11; this._viewY--; }
    if (this._viewM > 11) { this._viewM = 0; this._viewY++; }
    this._render();
  }

  _render() {
    const M = ['January','February','March','April','May','June',
               'July','August','September','October','November','December'];
    this._myEl.textContent = M[this._viewM] + ' ' + this._viewY;
    const y = this._viewY, m = this._viewM;
    const off = (new Date(y, m, 1).getDay() + 6) % 7; // Mon=0
    const dim = new Date(y, m + 1, 0).getDate();
    const pdim = new Date(y, m, 0).getDate();
    const now = new Date();
    const tk = now.getFullYear() * 1e4 + now.getMonth() * 100 + now.getDate();
    const sk = this._pending
      ? this._pending.getFullYear() * 1e4 + this._pending.getMonth() * 100 + this._pending.getDate()
      : -1;
    this._daysEl.innerHTML = '';
    for (let i = off - 1; i >= 0; i--) this._cell(pdim - i, true, -1);
    for (let d = 1; d <= dim; d++) {
      const k = y * 1e4 + m * 100 + d;
      this._cell(d, false, k, k === tk, k === sk);
    }
    const n = this._daysEl.children.length;
    for (let d = 1, r = (7 - n % 7) % 7; d <= r; d++) this._cell(d, true, -1);
  }

  _cell(day, other, _key, today, sel) {
    const el = document.createElement('span');
    el.className = 'dtp-day';
    if (other) el.classList.add('other-month');
    if (today) el.classList.add('today');
    if (sel) el.classList.add('selected');
    el.textContent = day;
    if (!other) el.addEventListener('click', () => {
      this._pending = new Date(this._viewY, this._viewM, day);
      this._render();
    });
    this._daysEl.appendChild(el);
  }

  open() {
    if (this._isOpen) return;
    if (_openPicker && _openPicker !== this) _openPicker.close(true);
    _openPicker = this;
    this._isOpen = true;
    const src = this._value || new Date();
    this._pending = this._value ? new Date(this._value) : null;
    this._pendingH = src.getHours();
    this._pendingM = src.getMinutes();
    this._viewY = src.getFullYear();
    this._viewM = src.getMonth();
    this._render();
    this._hIn.value = this._pad(this._pendingH);
    this._mIn.value = this._pad(this._pendingM);
    this._popup.classList.add('open');
    // Anchored to the field's left edge, which a narrow window can leave past
    // the viewport's right edge — same clamp the popovers' place() applies:
    // flip to the field's right edge when the popup would cross it.
    this._popup.style.left = '';
    this._popup.style.right = '';
    const r = this._popup.getBoundingClientRect();
    if (r.right > window.innerWidth - 8) {
      this._popup.style.left = 'auto';
      this._popup.style.right = '0';
    }
  }

  close(revert) {
    if (!this._isOpen) return;
    this._isOpen = false;
    _openPicker = null;
    this._popup.classList.remove('open');
    if (revert) return;
    const d = this._pending ? new Date(this._pending) : new Date();
    d.setHours(this._pendingH, this._pendingM, 0, 0);
    const prev = this._value ? this._value.getTime() : null;
    this._value = d;
    this.input.value = this._fmt(d);
    if (prev !== d.getTime()) this.onChange(d.getTime());
  }

  setValue(ms) {
    if (ms == null) return;
    const d = new Date(ms);
    this._value = d;
    this.input.value = this._fmt(d);
  }

  // Empties the field and forgets the value, without firing onChange: the
  // caller that clears it already knows. Emptying the text by hand does not
  // reach _commitInput (it ignores an empty field), so a page that treats an
  // empty field as "no bound" listens for the input's change and calls this.
  clear() {
    this._value = null;
    this.input.value = '';
  }

  getValue() { return this._value ? this._value.getTime() : null; }

  // The picked instant as an ISO UTC string, or null.
  //
  // Everything the user sees is local time -- the picker formats local parts,
  // _parseUserInput builds a local Date, and the tables render with
  // toLocaleString(). But the server's _parse_dt reads a naive string as UTC,
  // so sending the raw field text made every ETL dialog off by the UTC
  // offset: type 15:00, get 15:00 UTC. Send the actual instant instead and
  // input finally agrees with display.
  //
  // _commitInput() first as a belt-and-braces: the document mousedown handler
  // already commits synchronously before a button's click fires, but the blur
  // path runs behind a setTimeout(.., 100) and would not be in time on its
  // own. Calling it twice is harmless -- it returns early when nothing moved.
  //
  // Milliseconds are sliced off because the picker always zeroes them, and
  // the shorter form reads better in logs and as a CLI argument. _parse_dt
  // handles the Z suffix explicitly.
  getUTCISO() {
    this._commitInput();
    return this._value ? this._value.toISOString().slice(0, 19) + 'Z' : null;
  }

  _fmt(d) {
    return d.getFullYear() + '-' + this._pad(d.getMonth() + 1) + '-' + this._pad(d.getDate())
      + ' ' + this._pad(d.getHours()) + ':' + this._pad(d.getMinutes());
  }
}
