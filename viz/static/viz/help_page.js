/* Reading aids for one help document.
 *
 * Two jobs, both of which have to happen after the rendered Markdown is in
 * the DOM: put every table in its own scroll container, and keep the sidebar
 * contents in step with what is on screen. */
(function () {
  var doc = document.getElementById('help-doc');
  if (!doc) return;

  /* Wide tables must scroll inside their own box — the corpus has several
   * far wider than the measure, and without this they widen the page and
   * push the whole layout sideways. Done in JS rather than CSS because the
   * markup comes from Markdown, which has nowhere to hang a wrapper. */
  Array.prototype.forEach.call(doc.querySelectorAll('table'), function (table) {
    if (table.parentNode.classList.contains('help-table-wrap')) return;
    var wrap = document.createElement('div');
    wrap.className = 'help-table-wrap';
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
  });

  /* <picture> answers to the OS colour scheme; this app answers to its own
   * theme toggle. Where the two disagree — theme set to dark on a machine
   * running light, which is exactly what the toggle is for — the browser
   * picks the light asset and paints it on a dark page. README.md keeps its
   * logo in such a block, so the one image on the page that is meant to
   * adapt is the one that gets it wrong.
   *
   * Resolve the choice here instead: read the alternatives off the <source>
   * elements once, then set the <img> from the app's theme and follow it. */
  var pictures = [];
  Array.prototype.forEach.call(doc.querySelectorAll('picture'), function (pic) {
    var img = pic.querySelector('img');
    var darkSource = pic.querySelector('source[media*="dark"]');
    if (!img || !darkSource) return;
    var dark = (darkSource.getAttribute('srcset') || '').split(/[,\s]+/)[0];
    if (!dark) return;
    pictures.push({img: img, dark: dark, light: img.getAttribute('src')});
    /* The <source> would keep overriding whatever we set on the <img>. */
    darkSource.remove();
  });

  function applyTheme() {
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    pictures.forEach(function (p) {
      var want = isDark ? p.dark : p.light;
      if (p.img.getAttribute('src') !== want) p.img.setAttribute('src', want);
    });
  }

  if (pictures.length) {
    applyTheme();
    /* The toggle lives in the shell and rewrites the attribute in place. */
    new MutationObserver(applyTheme).observe(document.documentElement, {
      attributes: true, attributeFilter: ['data-theme']
    });
  }

  /* Scroll spy. IntersectionObserver rather than a scroll handler: no
   * throttling to tune, and it stays quiet while the page is still. */
  var links = Array.prototype.slice.call(
    document.querySelectorAll('#help-toc a'));
  if (!links.length || !window.IntersectionObserver) return;

  var byId = {};
  links.forEach(function (a) { byId[a.getAttribute('href').slice(1)] = a; });

  var headings = Object.keys(byId)
    .map(function (id) { return document.getElementById(id); })
    .filter(Boolean);
  if (!headings.length) return;

  var visible = {};
  var current = null;

  function update() {
    /* The topmost heading currently on screen wins; when none is (mid-way
     * through a long section) the last one to leave upwards keeps the mark. */
    var best = null;
    headings.forEach(function (h) {
      if (visible[h.id] && (best === null || h.offsetTop < best.offsetTop)) best = h;
    });
    if (!best || best === current) return;
    if (current && byId[current.id]) byId[current.id].classList.remove('active');
    byId[best.id].classList.add('active');
    current = best;
  }

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) { visible[e.target.id] = e.isIntersecting; });
    update();
  }, {
    /* Only the top slice of the viewport counts as "here", so the mark moves
     * with reading position instead of racing ahead. */
    rootMargin: '-56px 0px -70% 0px',
    threshold: 0
  });
  headings.forEach(function (h) { observer.observe(h); });
})();
