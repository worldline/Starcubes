/* Anchored dropdowns, shared by the sources shell (sources/base.html) and the
 * chart page (viz/chart.html).
 *
 * One mechanic for every anchored dropdown: open/close, outside click, Escape,
 * keyboard roving, positioning under the trigger and the aria-expanded
 * bookkeeping. The chart's preferences popover and both pages' top-bar menus
 * run on it.
 *
 * Deliberately NOT the chart's `.fpanel` contract. A dropdown is anchored to
 * its button, is never dragged or resized and is not part of the click-order
 * stack; giving it the panel contract would hand every future menu a drag
 * handle it must not have. The two mechanics stay separate on purpose.
 *
 * `_popovers` is module-private via the IIFE; makePopover and closeTopPopover
 * are the exports. */
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
    // The preferences trigger lives inside the menu that opening it closes, so
    // by close time it is display:none and cannot take focus back.
    const returnFocus = opts.returnFocus || btn;
    const closeOnClick = opts.closeOnClick === undefined ? !!opts.menu : opts.closeOnClick;
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-haspopup', opts.menu ? 'menu' : 'dialog');
    if (panel.id) btn.setAttribute('aria-controls', panel.id);

    // Recomputed on every open: entries can be hidden per login state.
    function items() {
      return [...panel.querySelectorAll('[role="menuitem"]')]
        .filter(el => !el.disabled && el.offsetParent !== null);
    }

    // Anchored below, flipped above when the viewport has no room, and clamped
    // to 8 px from either edge. Called while the panel is visible so
    // offsetWidth/offsetHeight are real.
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
      // `focusFirst` is passed only when the keyboard opened it. A mouse click
      // that lands a focus ring on the first entry looks like a selection the
      // user did not make; arrow keys still enter the menu from the trigger.
      open(focusFirst) {
        if (api.isOpen()) return;
        // Only ever one dropdown at a time — two anchored boxes overlapping is
        // never what the user asked for.
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

    // detail === 0 marks a click the browser synthesized from Enter or Space on
    // the button, i.e. a keyboard activation.
    btn.addEventListener('click', e => {
      e.preventDefault(); e.stopPropagation(); api.toggle(e.detail === 0);
    });

    // pointerdown, not click: a press that starts outside already dismisses,
    // which is the rule the date picker follows too.
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
        // Stops here: the document-level Escape handler would otherwise close
        // the top-most floating panel with the same key press.
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
      else if (e.key === 'Tab')     { api.close(); }   // let focus leave normally
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

  // Closes the front-most open dropdown, if any. The chart's shared Escape
  // handler calls this before it reaches for the floating panels: a dropdown is
  // drawn over them, so it is what "the top-most thing" means to the user.
  function closeTopPopover() {
    for (let i = _popovers.length - 1; i >= 0; i--) {
      if (_popovers[i].isOpen()) { _popovers[i].close(true); return true; }
    }
    return false;
  }

  // Exposed for the inline wiring script in base.html's {% block extra_js %}
  // and for chart.html's Escape handler.
  window.makePopover = makePopover;
  window.closeTopPopover = closeTopPopover;
})();
