// Group Dimensions editor — shared by the Manage page (sources/manage.html)
// and the user-facing datasource page (sources/detail.html). Both render the
// same panel markup (sources/_group_dims_panel.html), so they run the same
// code; this file exists because editing only one of two copies is what broke
// this tab before.
//
// Requirements at load time:
//   - `DS_PK` — the datasource id, defined by the including template.
//   - `csrfToken()` — from base.html (defined before <main>, so it is ready).
//   - The panel markup must already be in the DOM: the wiring below runs at
//     top level, not on DOMContentLoaded. Include this with a plain
//     <script src> placed AFTER the panel — never `defer`, because both pages
//     call grpLoadList() from inline scripts that run during parsing and
//     would execute before a deferred file.
//
// Loading the list is left to the including page: manage.html does it lazily
// on first tab click (12 tabs), detail.html eagerly on load (5 tabs).

// --- Group Dims tab ---
// A datasource has arbitrarily many group dimensions; the left-hand list
// picks one and the editor below works on that one alone. grpGroups therefore
// holds the groups of the *selected set* only — two sets on the same native
// dimension may both claim the same dim value.
var grpAllValues = [];   // [{index, source_id, name, description}]
var grpGroups = [];      // [{group_name, color, dim_indices: [int]}]
var grpSets = [];        // [{id, name, fact_column, dimension_id, group_count}]
var grpSelectedSetId = null;   // null = nothing open, 'new' = unsaved draft
var grpSelectedDimId = null;
var grpNameTouched = false;    // stop auto-filling the name once edited by hand
var GROUP_PALETTE = [
  '#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231', '#911eb4',
  '#42d4f4', '#f032e6', '#bfef45', '#fabed4', '#469990', '#dcbeff'
];

function grpReset() {
  grpAllValues = [];
  grpGroups = [];
  grpSelectedSetId = null;
  grpSelectedDimId = null;
  grpNameTouched = false;
  document.getElementById('grpDimContent').style.display = 'none';
  document.getElementById('grpEditorPrompt').style.display = '';
  document.getElementById('grpZones').innerHTML = '';
  document.getElementById('grpLeftoverChips').innerHTML = '';
  document.getElementById('grpSaveResult').style.display = 'none';
  grpRenderList();
  grpCloseAddPopover();
}

function grpDimOption(dimId) {
  return document.querySelector('#grpDimSelect option[value="' + dimId + '"]');
}

function grpPresetName(dimId) {
  var opt = grpDimOption(dimId);
  return opt ? opt.dataset.factColumn + ' (grouped)' : '';
}

function grpRenderList() {
  var ul = document.getElementById('grpList');
  var empty = document.getElementById('grpListEmpty');
  ul.innerHTML = '';
  empty.style.display = grpSets.length ? 'none' : '';
  grpSets.forEach(function(s) {
    var li = document.createElement('li');
    li.className = 'group-dim-item' + (s.id === grpSelectedSetId ? ' active' : '');
    var name = document.createElement('span');
    name.className = 'group-dim-item-name';
    name.textContent = s.name;
    li.appendChild(name);
    var sub = document.createElement('span');
    sub.className = 'group-dim-item-sub';
    sub.textContent = s.fact_column + ' · ' + s.group_count +
                      (s.group_count === 1 ? ' group' : ' groups');
    li.appendChild(sub);
    li.addEventListener('click', function() { grpOpenSet(s.id); });
    ul.appendChild(li);
  });
  if (grpSelectedSetId === 'new') {
    var li = document.createElement('li');
    li.className = 'group-dim-item active';
    var name = document.createElement('span');
    name.className = 'group-dim-item-name';
    name.textContent = document.getElementById('grpNameInput').value || 'New group dimension';
    li.appendChild(name);
    var sub = document.createElement('span');
    sub.className = 'group-dim-item-sub';
    sub.textContent = 'unsaved';
    li.appendChild(sub);
    ul.appendChild(li);
    empty.style.display = 'none';
  }
  // Keep the sub-tab's count in step with the list — the saved sets only,
  // so an unsaved draft does not bump it.
  var tabCount = document.getElementById('grpTabCount');
  if (tabCount) tabCount.textContent = grpSets.length;
}

function grpLoadList() {
  return fetch('/sources/' + DS_PK + '/group-dims/')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data.ok) throw new Error(data.error);
      grpSets = data.group_sets || [];
      grpRenderList();
      return grpSets;
    });
}

// Load the dim table's values for the dimension currently in the dropdown.
function grpLoadValues(dimId) {
  return fetch('/sources/' + DS_PK + '/dimensions/' + dimId + '/values/')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data.ok) throw new Error(data.error);
      grpAllValues = data.values || [];
    });
}

function grpShowEditor() {
  document.getElementById('grpEditorPrompt').style.display = 'none';
  document.getElementById('grpDimContent').style.display = 'block';
  document.getElementById('grpSaveResult').style.display = 'none';
}

function grpOpenSet(setId) {
  fetch('/sources/' + DS_PK + '/group-dims/' + setId + '/')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data.ok) throw new Error(data.error);
      var gs = data.group_set;
      grpSelectedSetId = gs.id;
      grpSelectedDimId = gs.dimension_id;
      grpNameTouched = true;   // an existing name is never auto-overwritten
      document.getElementById('grpNameInput').value = gs.name;
      document.getElementById('grpDimSelect').value = String(gs.dimension_id);
      grpGroups = (gs.groups || []).map(function(g) {
        return { group_name: g.group_name, color: g.color || '',
                 dim_indices: (g.members || []).slice() };
      });
      return grpLoadValues(gs.dimension_id);
    })
    .then(function() {
      grpShowEditor();
      grpRenderList();
      grpRender();
    })
    .catch(function(e) { alert('Error: ' + e.message); });
}

function grpCloseAddPopover() {
  var existing = document.getElementById('grpAddPopover');
  if (existing) existing.remove();
}

function grpGroupCount(g) {
  var total = 0;
  g.dim_indices.forEach(function(idx) {
    var v = grpAllValues.find(function(x) { return x.index === idx; });
    if (v && v.count) total += v.count;
  });
  return total;
}

function grpRender() {
  // Build set of assigned indices
  var assigned = {};
  grpGroups.forEach(function(g, gi) {
    g.dim_indices.forEach(function(idx) { assigned[idx] = gi; });
  });
  // Unassigned
  var leftover = grpAllValues.filter(function(v) { return !(v.index in assigned); });
  // Render groups
  var zonesEl = document.getElementById('grpZones');
  zonesEl.innerHTML = '';
  grpGroups.forEach(function(g, gi) {
    var zone = document.createElement('div');
    zone.className = 'group-zone';
    zone.dataset.groupIndex = gi;
    zone.addEventListener('dragover', function(e) { e.preventDefault(); zone.classList.add('drag-over'); });
    zone.addEventListener('dragleave', function() { zone.classList.remove('drag-over'); });
    zone.addEventListener('drop', function(e) {
      e.preventDefault();
      zone.classList.remove('drag-over');
      var idx = parseInt(e.dataTransfer.getData('text/plain'), 10);
      if (isNaN(idx)) return;
      // Remove from whatever group it's in
      grpRemoveFromGroups(idx);
      g.dim_indices.push(idx);
      grpRender();
    });
    var header = document.createElement('div');
    header.className = 'group-zone-header';
    var colorBtn = document.createElement('button');
    colorBtn.type = 'button';
    colorBtn.className = 'group-color-swatch';
    colorBtn.style.background = g.color || GROUP_PALETTE[gi % GROUP_PALETTE.length];
    colorBtn.title = 'Pick color';
    colorBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      grpTogglePalette(gi, colorBtn);
    });
    header.appendChild(colorBtn);
    var nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'form-control';
    nameInput.value = g.group_name;
    nameInput.placeholder = 'Group name';
    nameInput.addEventListener('input', function() { g.group_name = this.value; });
    header.appendChild(nameInput);
    var countSpan = document.createElement('span');
    countSpan.className = 'group-zone-count text-muted';
    countSpan.textContent = '(' + grpGroupCount(g).toLocaleString() + ')';
    header.appendChild(countSpan);
    var delBtn = document.createElement('button');
    delBtn.className = 'group-zone-delete';
    delBtn.textContent = '✕';
    delBtn.title = 'Delete group';
    delBtn.addEventListener('click', function() {
      grpGroups.splice(gi, 1);
      grpRender();
    });
    header.appendChild(delBtn);
    zone.appendChild(header);
    var chips = document.createElement('div');
    chips.className = 'group-chips';
    g.dim_indices.forEach(function(idx) {
      var v = grpAllValues.find(function(x) { return x.index === idx; });
      if (!v) return;
      chips.appendChild(grpMakeChip(v, gi));
    });
    var addWrap = document.createElement('span');
    addWrap.className = 'group-zone-add-wrap';
    var addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'group-zone-add-btn';
    addBtn.textContent = '+ Add value';
    addBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      grpOpenAddPopover(gi, addWrap);
    });
    addWrap.appendChild(addBtn);
    chips.appendChild(addWrap);
    zone.appendChild(chips);
    zonesEl.appendChild(zone);
  });
  // Update target dropdown
  var targetSel = document.getElementById('grpSearchTarget');
  targetSel.innerHTML = '';
  if (grpGroups.length === 0) {
    targetSel.innerHTML = '<option value="">— No groups yet —</option>';
  } else {
    grpGroups.forEach(function(g, gi) {
      var opt = document.createElement('option');
      opt.value = gi;
      opt.textContent = g.group_name || ('Group ' + (gi + 1));
      targetSel.appendChild(opt);
    });
  }
  // Render leftover (collapsed summary + chip list)
  var leftoverChipsEl = document.getElementById('grpLeftoverChips');
  var leftoverLabel = document.getElementById('grpLeftoverLabel');
  leftoverChipsEl.innerHTML = '';
  var leftoverCount = leftover.reduce(function(s, v) { return s + (v.count || 0); }, 0);
  leftoverLabel.textContent = 'Unassigned (' + leftover.length + ' values, ' + leftoverCount.toLocaleString() + ' rows)';
  leftover.forEach(function(v) {
    leftoverChipsEl.appendChild(grpMakeChip(v, null));
  });
  // Refresh any open Add-value popover so newly-unassigned values appear
  var openPop = document.getElementById('grpAddPopover');
  if (openPop) {
    var gi = parseInt(openPop.dataset.groupIndex, 10);
    if (isNaN(gi) || !grpGroups[gi]) {
      grpCloseAddPopover();
    } else {
      grpRenderAddPopoverList(gi);
    }
  }
}

function grpMakeChip(v, groupIndex) {
  var chip = document.createElement('span');
  chip.className = 'group-chip';
  chip.textContent = (v.name || v.source_id) + ' ';
  chip.draggable = true;
  chip.dataset.dimIndex = v.index;
  var label = v.name || v.source_id;
  if (v.source_id !== v.name && v.source_id) label = v.source_id + ': ' + v.name;
  chip.title = label;
  chip.addEventListener('dragstart', function(e) {
    e.dataTransfer.setData('text/plain', String(v.index));
    chip.classList.add('dragging');
  });
  chip.addEventListener('dragend', function() { chip.classList.remove('dragging'); });
  if (groupIndex !== null) {
    var x = document.createElement('span');
    x.className = 'chip-remove';
    x.textContent = '×';
    x.addEventListener('click', function() {
      grpRemoveFromGroups(v.index);
      grpRender();
    });
    chip.appendChild(x);
  }
  return chip;
}

function grpRemoveFromGroups(idx) {
  grpGroups.forEach(function(g) {
    var i = g.dim_indices.indexOf(idx);
    if (i >= 0) g.dim_indices.splice(i, 1);
  });
}

function grpTogglePalette(gi, anchorBtn) {
  // Close any existing palette
  var existing = document.getElementById('grpPalette');
  if (existing) { existing.remove(); return; }
  var palette = document.createElement('div');
  palette.id = 'grpPalette';
  palette.className = 'group-palette';
  GROUP_PALETTE.forEach(function(c) {
    var swatch = document.createElement('button');
    swatch.type = 'button';
    swatch.className = 'group-palette-color';
    swatch.style.background = c;
    swatch.addEventListener('click', function() {
      grpGroups[gi].color = c;
      anchorBtn.style.background = c;
      palette.remove();
    });
    palette.appendChild(swatch);
  });
  anchorBtn.parentElement.appendChild(palette);
}

// Close palette on outside click
document.addEventListener('click', function(e) {
  var palette = document.getElementById('grpPalette');
  if (palette && !palette.contains(e.target) && !e.target.classList.contains('group-color-swatch')) {
    palette.remove();
  }
});

function grpOpenAddPopover(gi, anchorEl) {
  var existing = document.getElementById('grpAddPopover');
  if (existing) {
    var prevGi = parseInt(existing.dataset.groupIndex, 10);
    existing.remove();
    if (prevGi === gi) return;
  }
  var pop = document.createElement('div');
  pop.id = 'grpAddPopover';
  pop.className = 'group-add-popover visible';
  pop.dataset.groupIndex = String(gi);
  pop.addEventListener('click', function(e) { e.stopPropagation(); });

  var searchWrap = document.createElement('div');
  searchWrap.className = 'group-add-popover-search';
  var searchInput = document.createElement('input');
  searchInput.type = 'text';
  searchInput.className = 'form-control';
  searchInput.placeholder = 'search unassigned…';
  searchInput.id = 'grpAddPopoverSearch';
  searchInput.addEventListener('input', function() { grpRenderAddPopoverList(gi); });
  searchWrap.appendChild(searchInput);
  pop.appendChild(searchWrap);

  var list = document.createElement('div');
  list.className = 'group-add-popover-list';
  list.id = 'grpAddPopoverList';
  pop.appendChild(list);

  var actions = document.createElement('div');
  actions.className = 'group-add-popover-actions';
  var cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'btn btn-secondary btn-sm';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', grpCloseAddPopover);
  var addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'btn btn-primary btn-sm';
  addBtn.textContent = 'Add';
  addBtn.addEventListener('click', function() {
    var picked = [];
    pop.querySelectorAll('input[type="checkbox"]:checked').forEach(function(cb) {
      picked.push(parseInt(cb.value, 10));
    });
    if (picked.length === 0) { grpCloseAddPopover(); return; }
    picked.forEach(function(idx) {
      if (grpGroups[gi].dim_indices.indexOf(idx) < 0) {
        grpGroups[gi].dim_indices.push(idx);
      }
    });
    grpCloseAddPopover();
    grpRender();
  });
  actions.appendChild(cancelBtn);
  actions.appendChild(addBtn);
  pop.appendChild(actions);

  anchorEl.appendChild(pop);
  grpRenderAddPopoverList(gi);
  searchInput.focus();
}

function grpRenderAddPopoverList(gi) {
  var list = document.getElementById('grpAddPopoverList');
  if (!list) return;
  var search = document.getElementById('grpAddPopoverSearch');
  var query = ((search && search.value) || '').toLowerCase();
  var assigned = {};
  grpGroups.forEach(function(g) { g.dim_indices.forEach(function(idx) { assigned[idx] = true; }); });
  var matches = grpAllValues.filter(function(v) {
    if (assigned[v.index]) return false;
    if (!query) return true;
    var field = ((v.source_id || '') + ' ' + (v.name || '')).toLowerCase();
    return field.indexOf(query) >= 0;
  }).slice(0, 100);
  list.innerHTML = '';
  if (matches.length === 0) {
    var empty = document.createElement('div');
    empty.className = 'group-add-popover-empty';
    empty.textContent = query ? 'No matches.' : 'No unassigned values.';
    list.appendChild(empty);
    return;
  }
  matches.forEach(function(v) {
    var item = document.createElement('label');
    item.className = 'group-add-popover-item';
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = v.index;
    var label = (v.name || v.source_id);
    if (v.source_id !== v.name && v.source_id) label = v.source_id + ': ' + v.name;
    item.appendChild(cb);
    item.appendChild(document.createTextNode(' ' + label));
    list.appendChild(item);
  });
}

// Close add-value popover on outside click
document.addEventListener('click', function(e) {
  var pop = document.getElementById('grpAddPopover');
  if (!pop) return;
  if (pop.contains(e.target)) return;
  if (e.target.classList && e.target.classList.contains('group-zone-add-btn')) return;
  grpCloseAddPopover();
});

// New group dimension: an unsaved draft. The name is preset from the chosen
// dimension and keeps following it until the user types their own.
document.getElementById('grpNewBtn').addEventListener('click', function() {
  var sel = document.getElementById('grpDimSelect');
  if (!sel.options.length) { alert('This datasource has no dimension with a dim table.'); return; }
  grpSelectedSetId = 'new';
  grpNameTouched = false;
  grpGroups = [];
  sel.selectedIndex = 0;
  grpSelectedDimId = parseInt(sel.value, 10);
  document.getElementById('grpNameInput').value = grpPresetName(grpSelectedDimId);
  grpShowEditor();
  grpRenderList();
  grpLoadValues(grpSelectedDimId)
    .then(grpRender)
    .catch(function(e) { alert('Error loading values: ' + e.message); });
});

document.getElementById('grpNameInput').addEventListener('input', function() {
  grpNameTouched = true;
  if (grpSelectedSetId === 'new') grpRenderList();
});

// Switching the native dimension invalidates every assignment: dim indices
// are dimension-specific, so a carried-over group would mislabel rows.
document.getElementById('grpDimSelect').addEventListener('change', function() {
  var dimId = parseInt(this.value, 10);
  if (!dimId || dimId === grpSelectedDimId) return;
  var assigned = grpGroups.reduce(function(n, g) { return n + g.dim_indices.length; }, 0);
  if (assigned && !confirm('Switching the dimension clears all ' + assigned +
                           ' assigned values of this group dimension. Continue?')) {
    this.value = String(grpSelectedDimId);
    return;
  }
  grpSelectedDimId = dimId;
  grpGroups = [];
  if (!grpNameTouched) {
    document.getElementById('grpNameInput').value = grpPresetName(dimId);
    grpRenderList();
  }
  grpLoadValues(dimId)
    .then(grpRender)
    .catch(function(e) { alert('Error loading values: ' + e.message); });
});

document.getElementById('grpDeleteBtn').addEventListener('click', function() {
  if (grpSelectedSetId === 'new' || grpSelectedSetId === null) { grpReset(); return; }
  var name = document.getElementById('grpNameInput').value;
  if (!confirm('Delete group dimension "' + name + '" and all its groups?')) return;
  fetch('/sources/' + DS_PK + '/group-dims/' + grpSelectedSetId + '/delete/', {
    method: 'POST', headers: {'X-CSRFToken': csrfToken()}
  }).then(function(r) { return r.json(); }).then(function(data) {
    if (!data.ok) { alert('Error: ' + data.error); return; }
    return grpLoadList().then(grpReset);
  }).catch(function(e) { alert('Error: ' + e); });
});

document.getElementById('grpAddGroupBtn').addEventListener('click', function() {
  var name = 'Group ' + (grpGroups.length + 1);
  var color = GROUP_PALETTE[grpGroups.length % GROUP_PALETTE.length];
  grpGroups.push({ group_name: name, color: color, dim_indices: [] });
  grpRender();
});

// Filter apply
document.getElementById('grpFilterBtn').addEventListener('click', function() {
  if (grpGroups.length === 0) { alert('Create a group first.'); return; }
  var targetGi = parseInt(document.getElementById('grpSearchTarget').value, 10);
  if (isNaN(targetGi) || !grpGroups[targetGi]) { alert('Select a target group.'); return; }
  var op = document.getElementById('grpFilterOp').value;
  var text = document.getElementById('grpFilterText').value.trim();
  if (!text) return;
  var assigned = {};
  grpGroups.forEach(function(g) { g.dim_indices.forEach(function(idx) { assigned[idx] = true; }); });
  var matches = grpAllValues.filter(function(v) {
    if (assigned[v.index]) return false;
    var field = (v.name || v.source_id || '').toLowerCase();
    if (op === 'starts_with') return field.startsWith(text.toLowerCase());
    return field.indexOf(text.toLowerCase()) >= 0;
  });
  matches.forEach(function(v) { grpGroups[targetGi].dim_indices.push(v.index); });
  document.getElementById('grpFilterText').value = '';
  grpRender();
});

// Search & add
var grpSearchVisible = false;
document.getElementById('grpSearchInput').addEventListener('focus', function() { grpRenderSearch(); });
document.getElementById('grpSearchInput').addEventListener('input', function() { grpRenderSearch(); });
document.addEventListener('click', function(e) {
  var results = document.getElementById('grpSearchResults');
  var input = document.getElementById('grpSearchInput');
  if (!input.contains(e.target) && !results.contains(e.target)) {
    results.classList.remove('visible');
    grpSearchVisible = false;
  }
});

function grpRenderSearch() {
  var results = document.getElementById('grpSearchResults');
  var query = (document.getElementById('grpSearchInput').value || '').toLowerCase();
  var assigned = {};
  grpGroups.forEach(function(g) { g.dim_indices.forEach(function(idx) { assigned[idx] = true; }); });
  var visible = grpAllValues.filter(function(v) {
    if (assigned[v.index]) return false;
    var field = ((v.source_id || '') + ' ' + (v.name || '')).toLowerCase();
    return !query || field.indexOf(query) >= 0;
  }).slice(0, 50);
  results.innerHTML = '';
  visible.forEach(function(v) {
    var item = document.createElement('div');
    item.className = 'group-search-item';
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = v.index;
    var label = (v.name || v.source_id);
    if (v.source_id !== v.name && v.source_id) label = v.source_id + ': ' + v.name;
    item.appendChild(cb);
    item.appendChild(document.createTextNode(' ' + label));
    item.addEventListener('click', function(e) {
      if (e.target !== cb) cb.checked = !cb.checked;
    });
    results.appendChild(item);
  });
  results.classList.add('visible');
  grpSearchVisible = true;
}

document.getElementById('grpSearchAddBtn').addEventListener('click', function() {
  if (grpGroups.length === 0) { alert('Create a group first.'); return; }
  var targetGi = parseInt(document.getElementById('grpSearchTarget').value, 10);
  if (isNaN(targetGi) || !grpGroups[targetGi]) { alert('Select a target group.'); return; }
  var indices = [];
  document.querySelectorAll('#grpSearchResults input[type="checkbox"]:checked').forEach(function(cb) {
    indices.push(parseInt(cb.value, 10));
  });
  if (indices.length === 0) { alert('Select values to add.'); return; }
  indices.forEach(function(idx) {
    if (grpGroups[targetGi].dim_indices.indexOf(idx) < 0) {
      grpGroups[targetGi].dim_indices.push(idx);
    }
  });
  grpRender();
  grpRenderSearch();
});

// Save. A draft is created first (POST add/, which only takes name +
// dimension), then saved like any other set so both paths share one code
// path for the groups themselves.
document.getElementById('grpSaveBtn').addEventListener('click', function() {
  var btn = this;
  if (!grpSelectedDimId || grpSelectedSetId === null) return;
  var name = document.getElementById('grpNameInput').value.trim();
  if (!name) { alert('The group dimension needs a name.'); return; }
  for (var i = 0; i < grpGroups.length; i++) {
    if (!grpGroups[i].group_name.trim()) {
      alert('Group ' + (i + 1) + ' needs a name.');
      return;
    }
  }
  btn.disabled = true;
  btn.textContent = 'Saving…';

  var payload = {
    name: name,
    dimension_id: grpSelectedDimId,
    groups: grpGroups.map(function(g) {
      return { group_name: g.group_name.trim(), color: g.color || '', dim_indices: g.dim_indices.slice() };
    })
  };

  var created = grpSelectedSetId === 'new'
    ? fetch('/sources/' + DS_PK + '/group-dims/add/', {
        method: 'POST',
        headers: {'X-CSRFToken': csrfToken(), 'Content-Type': 'application/json'},
        body: JSON.stringify({ name: name, dimension_id: grpSelectedDimId })
      }).then(function(r) { return r.json(); }).then(function(data) {
        if (!data.ok) throw new Error(data.error);
        grpSelectedSetId = data.group_set.id;
      })
    : Promise.resolve();

  created.then(function() {
    return fetch('/sources/' + DS_PK + '/group-dims/' + grpSelectedSetId + '/save/', {
      method: 'POST',
      headers: {'X-CSRFToken': csrfToken(), 'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(function(r) { return r.json(); });
  }).then(function(data) {
    if (!data.ok) throw new Error(data.error);
    var gs = data.group_set;
    grpSelectedSetId = gs.id;
    grpGroups = (gs.groups || []).map(function(g) {
      return { group_name: g.group_name, color: g.color || '', dim_indices: (g.members || []).slice() };
    });
    document.getElementById('grpNameInput').value = gs.name;
    grpRender();
    var el = document.getElementById('grpSaveResult');
    el.className = 'action-result ok';
    el.textContent = '✓ Saved as ' + gs.token;
    el.style.display = 'block';
    return grpLoadList();
  }).catch(function(e) {
    var el = document.getElementById('grpSaveResult');
    el.className = 'action-result err'; el.textContent = '✗ ' + e.message; el.style.display = 'block';
  }).finally(function() {
    btn.disabled = false;
    btn.textContent = 'Save';
  });
});

// Allow drop on leftover zone (drag back from a group)
document.getElementById('grpLeftover').addEventListener('dragover', function(e) { e.preventDefault(); this.classList.add('drag-over'); });
document.getElementById('grpLeftover').addEventListener('dragleave', function() { this.classList.remove('drag-over'); });
document.getElementById('grpLeftover').addEventListener('drop', function(e) {
  e.preventDefault();
  this.classList.remove('drag-over');
  var idx = parseInt(e.dataTransfer.getData('text/plain'), 10);
  if (isNaN(idx)) return;
  grpRemoveFromGroups(idx);
  grpRender();
});

// Expand/collapse the unassigned summary
document.getElementById('grpLeftoverToggle').addEventListener('click', function() {
  var zone = document.getElementById('grpLeftover');
  var collapsed = zone.classList.toggle('leftover-collapsed');
  this.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  this.querySelector('.leftover-toggle-chevron').textContent = collapsed ? 'show' : 'hide';
});
