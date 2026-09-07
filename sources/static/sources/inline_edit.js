// Inline row editing for the data tables — shared by sources/manage.html
// (fact columns, dimensions, indices, measures) and sources/detail.html
// (measures). A row carries `span.row-display` and `.row-edit` siblings per
// cell; `startEdit` flips the row into `.editing` and `saveRow` writes the
// submitted values back into the display spans.
//
// Requirements at load time:
//   - `csrfToken()` and `postJSON()` — from base.html.
//   - `esc()` — defined by the including page. Only the renderers need it, and
//     they run on click, so it need not exist when this file loads. Keep the
//     two pages' definitions identical (both are the null-safe variant).
//   - Include with a plain <script src> AFTER the tables, before any inline
//     script that wraps these functions. manage.html wraps `startEdit` to
//     re-sync the dimension source fields; that works because these stay
//     global function declarations.

// One description of each table's display cells, in column order. saveRow
// refreshes them after a save and manage.html's row builders create them, so
// both read the same list instead of each carrying its own copy of the column
// order. That order used to live as bare indices — `row-display')[4]` meant
// source_expr in one place and is_default in two others, and nothing but
// counting the columns by hand said which.
//
// The edit controls are NOT described here: a number field, a select filled
// from a server-rendered option list, a checkbox and a textarea have too
// little in common to express as flags. They stay written out in the builders.
//
//   f      field on the row object
//   mono   display span carries .text-mono
//   cls    extra class on the display span
//   badge  wrap in a badge — a string names the modifier, true uses the value
//   check  render ✓ / —
//   dash   render — when the value is empty
//   text   custom renderer for the value
var ROW_SPECS = {
  fc: [
    {f: 'ordinal'},
    {f: 'name', mono: 1},
    {f: 'pg_type', mono: 1},
    {f: 'role', badge: true},
    {f: 'source_expr', mono: 1, dash: 1},
  ],
  dim: [
    {f: 'fact_column', mono: 1},
    {f: 'dim_table', mono: 1},
    {f: 'source_table', mono: 1, dash: 1},
    {f: 'source_key', mono: 1, dash: 1},
    {f: 'source_name', mono: 1, dash: 1},
    {f: 'source_description', mono: 1, dash: 1},
    {f: 'is_primary', check: 1},
    {f: 'inline_values', mono: 1, cls: 'dim-inline-display', text: firstLineOf},
    {f: 'filter_group', mono: 1, dash: 1},
  ],
  idx: [
    {f: 'column_name', mono: 1},
    {f: 'method', badge: 'dialect'},
  ],
  measure: [
    {f: 'ordinal'},
    {f: 'name'},
    {f: 'agg', mono: 1},
    {f: 'graph_style', badge: 'dialect'},
    {f: 'is_default', check: 1},
    {f: 'percentiles', mono: 1, dash: 1},
  ],
};

// Inline values are stored multi-line but shown as their first line. A textarea
// submits CRLF, so the split has to take the \r with it — otherwise the stray
// carriage return travels into the display span.
function firstLineOf(v) {
  var s = v || '';
  if (!s) return '—';
  return s.split(/\r?\n/)[0] + (/\r?\n/.test(s) ? ' …' : '');
}

// The inner HTML of one display span. Always escaped, so callers can assign it
// with innerHTML whether or not the column renders a badge.
function rowCellHTML(spec, row) {
  var v = row[spec.f];
  if (spec.check) return v ? '✓' : '—';
  if (spec.text) return esc(spec.text(v));
  if (spec.badge) {
    var mod = spec.badge === true ? v : spec.badge;
    return '<span class="badge badge-' + mod + '">' + esc(v) + '</span>';
  }
  return esc(spec.dash ? (v || '—') : v);
}

// The class list of one display span, so the builders and the server-rendered
// markup cannot disagree about it.
function rowCellClass(spec) {
  return 'row-display' + (spec.mono ? ' text-mono' : '') + (spec.cls ? ' ' + spec.cls : '');
}

// Only one measure may be the default; the server clears the others on save,
// and this mirrors that in the table without reloading it.
function clearOtherDefaults(exceptTr) {
  var i = 0;
  ROW_SPECS.measure.forEach(function(c, n) { if (c.f === 'is_default') i = n; });
  document.querySelectorAll('#measureBody tr').forEach(function(other) {
    if (other === exceptTr) return;
    var cb = other.querySelector('input[name="is_default"]');
    var disp = other.querySelectorAll('span.row-display')[i];
    if (cb) cb.checked = false;
    if (disp) disp.textContent = '—';
  });
}

// --- Inline edit helpers ---
function startEdit(btn, prefix) {
  btn.closest('tr').classList.add('editing');
}
function cancelEdit(btn) {
  btn.closest('tr').classList.remove('editing');
}
function saveRow(btn, url, prefix) {
  var tr = btn.closest('tr');
  var fd = new FormData();
  tr.querySelectorAll('input[name], select[name], textarea[name]').forEach(function(el) {
    if (el.type === 'checkbox') fd.append(el.name, el.checked ? 'on' : '');
    else fd.append(el.name, el.value);
  });
  btn.disabled = true;
  postJSON(url, fd).then(function(d) {
    if (!d.ok) { alert('Error: ' + d.error); return; }
    var spans = tr.querySelectorAll('span.row-display');
    spans.forEach(function(span) { span.style.color = ''; });
    // The actions cell holds a span.row-display of its own and therefore sits
    // one past the last described column, which is why this walks the spec
    // rather than the spans.
    (ROW_SPECS[prefix] || []).forEach(function(spec, i) {
      if (spans[i]) spans[i].innerHTML = rowCellHTML(spec, d.row);
    });
    if (prefix === 'measure' && d.row.is_default) clearOtherDefaults(tr);
    tr.classList.remove('editing');
  }).catch(function(e){ alert('Error: ' + e); }).finally(function(){ btn.disabled = false; });
}
function deleteRow(btn, url) {
  if (!confirm('Delete this row?')) return;
  postJSON(url, {}).then(function(d) {
    if (!d.ok) { alert('Error: ' + d.error); return; }
    btn.closest('tr').remove();
  }).catch(function(e){ alert('Error: ' + e); });
}
