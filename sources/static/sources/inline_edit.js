// Inline row editing for the data tables — shared by sources/manage.html
// (fact columns, dimensions, indices, measures) and sources/detail.html
// (measures). A row carries `span.row-display` and `.row-edit` siblings per
// cell; `startEdit` flips the row into `.editing` and `saveRow` writes the
// submitted values back into the display spans.
//
// Requirements at load time:
//   - `csrfToken()` and `postJSON()` — from base.html.
//   - `esc()` — defined by the including page. Only `saveRow` needs it, and
//     that runs on click, so it need not exist when this file loads. Keep the
//     two pages' definitions identical (both are the null-safe variant).
//   - Include with a plain <script src> AFTER the tables, before any inline
//     script that wraps these functions. manage.html wraps `startEdit` to
//     re-sync the dimension source fields; that works because these stay
//     global function declarations.

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
    // Update display cells
    tr.querySelectorAll('span.row-display').forEach(function(span) { span.style.color = ''; });
    var row = d.row;
    if (prefix === 'fc') {
      tr.querySelectorAll('span.row-display')[0].textContent = row.ordinal;
      tr.querySelectorAll('span.row-display')[1].textContent = row.name;
      tr.querySelectorAll('span.row-display')[2].textContent = row.pg_type;
      tr.querySelectorAll('span.row-display')[3].innerHTML = '<span class="badge badge-' + row.role + '">' + row.role + '</span>';
      tr.querySelectorAll('span.row-display')[4].textContent = row.source_expr || '—';
    } else if (prefix === 'dim') {
      var spans = tr.querySelectorAll('span.row-display');
      spans[0].textContent = row.fact_column;
      spans[1].textContent = row.dim_table;
      spans[2].textContent = row.source_table || '—';
      spans[3].textContent = row.source_key || '—';
      spans[4].textContent = row.source_name || '—';
      spans[5].textContent = row.source_description || '—';
      spans[6].textContent = row.is_primary ? '✓' : '—';
      // inline_values display — show first line + "…" if multiline
      var iv = row.inline_values || '';
      spans[7].textContent = iv ? iv.split('\n')[0] + (iv.includes('\n') ? ' …' : '') : '—';
      spans[8].textContent = row.filter_group || '—';
    } else if (prefix === 'idx') {
      tr.querySelectorAll('span.row-display')[0].textContent = row.column_name;
      tr.querySelectorAll('span.row-display')[1].innerHTML = '<span class="badge badge-dialect">' + row.method + '</span>';
    } else if (prefix === 'measure') {
      var ms = tr.querySelectorAll('span.row-display');
      ms[0].textContent = row.ordinal;
      ms[1].textContent = row.name;
      ms[2].textContent = row.agg;
      ms[3].innerHTML = '<span class="badge badge-dialect">' + esc(row.graph_style) + '</span>';
      ms[4].textContent = row.is_default ? '✓' : '—';
      if (ms[5]) ms[5].textContent = row.percentiles || '—';
      // Setting one default clears the others server-side; reflect that here.
      if (row.is_default) {
        document.querySelectorAll('#measureBody tr').forEach(function(other) {
          if (other === tr) return;
          var cb = other.querySelector('input[name="is_default"]');
          var disp = other.querySelectorAll('span.row-display')[4];
          if (cb) cb.checked = false;
          if (disp) disp.textContent = '—';
        });
      }
    }
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
