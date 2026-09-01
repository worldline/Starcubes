/* Sources-app nav menu popover.
 *
 * Mirrors viz/templates/viz/chart.html:2789 makePopover() — a near-verbatim
 * copy so the two pages can render an identical dropdown without dragging
 * in chart-specific globals (ts, TimeSeries.themes, _openPicker). The
 * `_popovers` array is module-private via the IIFE wrapper; it never
 * collides with the chart's own copy. */
(function () {
  const _popovers = [];

  // opts.anchor       element to position under (default: the trigger itself)
  // opts.align        'right' (default) or 'left' edge, aligned with the anchor
  // opts.menu         true → role=menuitem roving focus with the arrow keys
  // opts.closeOnClick a click on a link/button inside closes it (default: menu);
  //                   an element carrying [data-keep-open] is exempt
  // opts.returnFocus  element focused when Escape closes it (default: trigger)
  // opts.onOpen / opts.onClose   hooks
  function makePopover(btn, panel, opts) {
    opts = opts || {};
    const align = opts.align || 'right';
    const returnFocus = opts.returnFocus || btn;
    const closeOnClick = opts.closeOnClick === undefined ? !!opts.menu : opts.closeOnClick;
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-haspopup', opts.menu ? 'menu' : 'dialog');
    if (panel.id) btn.setAttribute('aria-controls', panel.id);

    function items() {
      return [...panel.querySelectorAll('[role="menuitem"]')]
        .filter(el => !el.disabled && el.offsetParent !== null);
    }

    function place() {
      const r = (opts.anchor || btn).getBoundingClientRect();
      const w = panel.offsetWidth, h = panel.offsetHeight;
      const GAP = 6, EDGE = 8;
      let left = align === 'left' ? r.left : r.right - w;
      left = Math.max(EDGE, Math.min(window.innerWidth - w - EDGE, left));
      let top = r.bottom + GAP;
      if (top + h > window.innerHeight - EDGE && r.top - GAP - h > EDGE) top = r.top - GAP - h;
      panel.style.left   = left + 'px';
      panel.style.top    = Math.max(EDGE, top) + 'px';
      panel.style.right  = 'auto';
      panel.style.bottom = 'auto';
    }

    const api = {
      btn, panel,
      isOpen() { return panel.classList.contains('pop-open'); },
      open(focusFirst) {
        if (api.isOpen()) return;
        for (const other of _popovers) if (other !== api) other.close();
        panel.classList.add('pop-open');
        btn.setAttribute('aria-expanded', 'true');
        if (opts.onOpen) opts.onOpen();
        place();
        if (opts.menu && focusFirst) { const it = items(); if (it.length) it[0].focus(); }
      },
      close(refocus) {
        if (!api.isOpen()) return;
        panel.classList.remove('pop-open');
        btn.setAttribute('aria-expanded', 'false');
        if (opts.onClose) opts.onClose();
        if (refocus) returnFocus.focus();
      },
      toggle(focusFirst) { if (api.isOpen()) api.close(); else api.open(focusFirst); },
    };

    btn.addEventListener('click', e => {
      e.preventDefault(); e.stopPropagation(); api.toggle(e.detail === 0);
    });

    document.addEventListener('pointerdown', e => {
      if (!api.isOpen()) return;
      if (panel.contains(e.target) || btn.contains(e.target)) return;
      api.close();
    });

    if (closeOnClick) panel.addEventListener('click', e => {
      if (e.target.closest('[data-keep-open]')) return;
      if (e.target.closest('a, button')) api.close();
    });

    panel.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation(); api.close(true); return;
      }
      if (!opts.menu) return;
      const it = items();
      if (!it.length) return;
      const i = it.indexOf(document.activeElement);
      if (e.key === 'ArrowDown')    { e.preventDefault(); it[i < 0 ? 0 : (i + 1) % it.length].focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); it[i < 0 ? it.length - 1 : (i - 1 + it.length) % it.length].focus(); }
      else if (e.key === 'Home')    { e.preventDefault(); it[0].focus(); }
      else if (e.key === 'End')     { e.preventDefault(); it[it.length - 1].focus(); }
      else if (e.key === 'Tab')     { api.close(); }
    });

    btn.addEventListener('keydown', e => {
      if (opts.menu && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
        e.preventDefault();
        if (!api.isOpen()) api.open(true);
        else { const it = items(); if (it.length) it[e.key === 'ArrowDown' ? 0 : it.length - 1].focus(); }
      } else if (e.key === 'Escape' && api.isOpen()) {
        e.preventDefault(); e.stopPropagation(); api.close(true);
      }
    });

    window.addEventListener('resize', () => { if (api.isOpen()) place(); });

    _popovers.push(api);
    return api;
  }

  // Expose for the inline wiring script in base.html's {% block extra_js %}.
  window.makePopover = makePopover;
})();
