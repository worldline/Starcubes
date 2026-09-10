/* Search box for the help section, on both the index and a document page.
 *
 * Wires itself to #help-q / #help-results if they exist and does nothing
 * otherwise, so both templates can load it unconditionally. The server does
 * the searching (viz/help.py:help_search); this only debounces, renders and
 * gets out of the way. */
(function () {
  var input = document.getElementById('help-q');
  var out = document.getElementById('help-results');
  if (!input || !out) return;

  var timer = null;
  var lastQuery = '';

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  /* Highlight the query inside an already-escaped snippet. Escaping first and
   * marking second is the order that matters: the other way round would let
   * the corpus's own angle brackets close the <mark>. */
  function mark(snippet, query) {
    var safe = esc(snippet);
    var needle = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return safe.replace(new RegExp(needle, 'gi'), function (m) {
      return '<mark>' + m + '</mark>';
    });
  }

  /* Snippets are whole source lines and can be long; show the neighbourhood
   * of the hit rather than the first N characters, which often miss it. */
  function trim(snippet, query, width) {
    if (snippet.length <= width) return snippet;
    var at = snippet.toLowerCase().indexOf(query.toLowerCase());
    if (at < 0) return snippet.slice(0, width) + '…';
    var start = Math.max(0, at - Math.floor(width / 3));
    var end = Math.min(snippet.length, start + width);
    return (start > 0 ? '…' : '') + snippet.slice(start, end) +
           (end < snippet.length ? '…' : '');
  }

  function render(data) {
    if (!data.results.length) {
      out.innerHTML = '<div class="help-empty">No matches for “' +
                      esc(data.query) + '”.</div>';
      return;
    }
    var html = '';
    data.results.forEach(function (r) {
      html += '<div class="help-result">';
      html += '<div class="help-result-doc">' + esc(r.title) + '</div>';
      r.hits.forEach(function (h) {
        var href = '/help/' + encodeURIComponent(r.slug) +
                   (h.anchor ? '#' + h.anchor : '');
        html += '<a href="' + href + '">' +
                mark(trim(h.snippet, data.query, 110), data.query) + '</a>';
      });
      if (r.count > r.hits.length) {
        html += '<a href="/help/' + encodeURIComponent(r.slug) + '">' +
                '+ ' + (r.count - r.hits.length) + ' more in this document</a>';
      }
      html += '</div>';
    });
    out.innerHTML = html;
  }

  function run() {
    var q = input.value.trim();
    if (q === lastQuery) return;
    lastQuery = q;
    if (q.length < 2) { out.innerHTML = ''; return; }
    fetch('/help/search.json?q=' + encodeURIComponent(q))
      .then(function (r) { return r.json(); })
      .then(function (data) { if (data.query === input.value.trim()) render(data); })
      .catch(function () { out.innerHTML = ''; });
  }

  input.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(run, 180);
  });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { input.value = ''; lastQuery = ''; out.innerHTML = ''; }
    /* Enter opens the first hit — the common case is "search, jump, read". */
    if (e.key === 'Enter') {
      var first = out.querySelector('a');
      if (first) { e.preventDefault(); first.click(); }
    }
  });

  /* "/" focuses the search from anywhere on a help page, unless the user is
   * already typing somewhere. */
  document.addEventListener('keydown', function (e) {
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    e.preventDefault();
    input.focus();
    input.select();
  });
})();
