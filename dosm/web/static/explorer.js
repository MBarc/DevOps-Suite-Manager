/* Shared org-tree explorer behaviour, used by the Pipelines / Credentials /
 * File-transfer blades. (The Hosts blade has its own proven inline script; this
 * is the same logic generalised via a per-page config so the new blades don't
 * each duplicate ~250 lines.)
 *
 * A page calls initExplorer(cfg) once, where cfg is:
 *   {
 *     lsPrefix:   'dosm.explorer.pipelines',   // localStorage namespace
 *     noun:       'pipeline',                  // singular, for the count label
 *     cardSelector:'.hcard',
 *     idAttr:     'data-card-id',
 *     searchFields: ['name','provider','target','org'],
 *     canEdit:    true,
 *     initialOrgUnitId: 5 | null,
 *     assignUrl:  function(id){ return '/pipelines/'+id+'/assign-org'; },
 *     pathPrefix: '▤ ',                   // prefix on the card's path line
 *     addHere:    function(orgId){ return '/pipelines/new'+(orgId?'?org_unit_id='+orgId:''); },
 *     addLabel:   'pipeline',                  // "Add pipeline here"
 *     cardMenu:   function(cardEl){ return [ {label,fn,danger?,sep?,head?}, ... ]; }
 *   }
 *
 * Card markup contract (reuse the .hcard shell): each card has
 *   data-card-id, data-org-id (''=unassigned), data-card-name,
 *   data-<field> for every searchField, and an optional .hcard-path span.
 */
(function () {
  function plural(n, noun) { return n + ' ' + noun + (n === 1 ? '' : 's'); }

  // Irreversible-delete confirmation with a "don't ask again" opt-out.
  // opts: { name, typeLabel, onConfirm }. Once the user ticks the box, all
  // future deletes skip the modal (localStorage flag).
  var SKIP_DELETE = 'dosm.skipDeleteConfirm';
  window.explorerConfirmDelete = function (opts) {
    try { if (localStorage.getItem(SKIP_DELETE) === '1') { opts.onConfirm(); return; } } catch (e) {}
    var overlay = document.createElement('div');
    overlay.className = 'ex-modal-overlay';
    overlay.innerHTML =
      '<div class="ex-modal" role="dialog" aria-modal="true">' +
        '<h3>Delete ' + (opts.typeLabel || 'item') + '?</h3>' +
        '<p class="ex-modal-name"></p>' +
        '<p class="ex-modal-warn">This permanently removes it and <strong>cannot be undone.</strong></p>' +
        '<label class="ex-modal-check" style="display:flex;flex-wrap:nowrap;align-items:center;justify-content:flex-start;gap:8px;text-align:left">' +
          '<input type="checkbox" style="flex:none;width:auto;min-width:0;margin:0" />' +
          '<span>I understand, don\'t ask me again</span>' +
        '</label>' +
        '<div class="ex-modal-actions">' +
          '<button type="button" class="btn btn-ghost ex-modal-cancel">Cancel</button>' +
          '<button type="button" class="btn btn-danger ex-modal-ok">Delete</button>' +
        '</div>' +
      '</div>';
    overlay.querySelector('.ex-modal-name').textContent = opts.name || '';  // textContent = no HTML injection
    document.body.appendChild(overlay);
    function close() { overlay.remove(); document.removeEventListener('keydown', onKey); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    overlay.querySelector('.ex-modal-cancel').addEventListener('click', close);
    overlay.querySelector('.ex-modal-ok').addEventListener('click', function () {
      if (overlay.querySelector('input[type=checkbox]').checked) {
        try { localStorage.setItem(SKIP_DELETE, '1'); } catch (e) {}
      }
      close();
      opts.onConfirm();
    });
    overlay.querySelector('.ex-modal-ok').focus();
  };

  window.initExplorer = function (cfg) {
    var root = document.querySelector('.explorer');
    if (!root) return;
    var tree = root.querySelector('.ex-tree');
    var cardsWrap = root.querySelector('.ex-cards');
    var cards = Array.prototype.slice.call(root.querySelectorAll(cfg.cardSelector));
    var bc = root.querySelector('#ex-breadcrumb');
    var empty = root.querySelector('#ex-empty');
    var search = root.querySelector('#ex-search');
    var LS_EXP = cfg.lsPrefix + '.expanded', LS_SEL = cfg.lsPrefix + '.sel';
    var sel = { mode: 'all', ids: null, label: 'All' };
    var activeField = '';

    function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
    function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

    function matchSearch(c, q) {
      if (!q) return true;
      if (activeField) return (c.getAttribute('data-' + activeField) || '').indexOf(q) >= 0;
      return cfg.searchFields.some(function (f) { return (c.getAttribute('data-' + f) || '').indexOf(q) >= 0; });
    }

    // Optional type filter (the Inventory blade): cards carry data-type and a
    // set of pill toggles (.ex-type-toggle[data-type]) turn each type on/off.
    var activeTypes = (cfg.typeFilter && cfg.typeFilter.length) ? new Set(cfg.typeFilter) : null;
    function matchType(c) {
      if (!activeTypes) return true;
      return activeTypes.has(c.getAttribute('data-type') || '');
    }

    var nodes = Array.prototype.slice.call(tree.querySelectorAll('li.tree-node[data-id]'));
    var savedExp = lsGet(LS_EXP), expanded = null;
    if (savedExp) { try { expanded = {}; JSON.parse(savedExp).forEach(function (i) { expanded[i] = 1; }); } catch (e) { expanded = null; } }
    nodes.forEach(function (li) {
      if (!li.querySelector(':scope > .tree-children')) return;
      var id = li.getAttribute('data-id');
      li.classList.toggle('collapsed', expanded ? !expanded[id] : false);
    });

    function persistExpanded() {
      var open = [];
      nodes.forEach(function (li) {
        if (li.querySelector(':scope > .tree-children') && !li.classList.contains('collapsed'))
          open.push(li.getAttribute('data-id'));
      });
      lsSet(LS_EXP, JSON.stringify(open));
    }
    function descendantIds(li) {
      var ids = {}; ids[li.getAttribute('data-id')] = 1;
      li.querySelectorAll('li.tree-node[data-id]').forEach(function (d) { ids[d.getAttribute('data-id')] = 1; });
      return ids;
    }
    function ancestorNames(li) {
      var names = [], cur = li;
      while (cur && cur.classList && cur.classList.contains('tree-node')) {
        var nm = cur.getAttribute('data-name'); if (nm) names.unshift(nm);
        cur = cur.parentElement ? cur.parentElement.closest('li.tree-node') : null;
      }
      return names;
    }

    function selectRow(li) {
      if (!li) return;
      tree.querySelectorAll('li.tree-node.selected').forEach(function (n) { n.classList.remove('selected'); });
      li.classList.add('selected');
      var key = li.getAttribute('data-node');
      if (key === 'all') { sel = { mode: 'all', ids: null, label: 'All' }; lsSet(LS_SEL, 'all'); }
      else if (key === 'unassigned') { sel = { mode: 'unassigned', ids: null, label: 'Unassigned' }; lsSet(LS_SEL, 'unassigned'); }
      else {
        var p = li.parentElement ? li.parentElement.closest('li.tree-node') : null;
        while (p) { p.classList.remove('collapsed'); p = p.parentElement ? p.parentElement.closest('li.tree-node') : null; }
        sel = { mode: 'node', ids: descendantIds(li), label: ancestorNames(li).join(' / ') };
        lsSet(LS_SEL, 'id:' + li.getAttribute('data-id'));
      }
      persistExpanded();
      render();
    }

    function render() {
      var q = (search.value || '').toLowerCase().trim();
      var shown = 0;
      cards.forEach(function (c) {
        var oid = c.getAttribute('data-org-id');
        var inSel = sel.mode === 'all' ? true
          : sel.mode === 'unassigned' ? (oid === '')
          : (oid !== '' && !!sel.ids[oid]);
        var vis = inSel && matchSearch(c, q) && matchType(c);
        c.style.display = vis ? '' : 'none';
        if (vis) shown++;
      });
      // Optionally hide the per-card org path when it's redundant: a specific
      // folder is selected and there's no cross-folder search (the breadcrumb
      // already shows where you are). Shown in the All view + search results.
      if (cfg.pathInAllOnly) cardsWrap.classList.toggle('hide-path', sel.mode === 'node' && !q);
      bc.innerHTML = '';
      var parts = sel.mode === 'node' && sel.label ? sel.label.split(' / ') : [sel.label];
      parts.forEach(function (p, i) {
        if (i) bc.appendChild(document.createTextNode(' / '));
        var el = document.createElement(i === parts.length - 1 ? 'b' : 'span');
        el.textContent = p; bc.appendChild(el);
      });
      bc.appendChild(document.createTextNode('  ·  '));
      var cnt = document.createElement('span');
      cnt.className = 'ex-bc-count';
      cnt.textContent = plural(shown, cfg.noun);
      bc.appendChild(cnt);
      empty.hidden = shown !== 0;
    }

    tree.addEventListener('click', function (e) {
      var caret = e.target.closest('.tree-caret');
      if (caret) { e.stopPropagation(); caret.closest('li.tree-node').classList.toggle('collapsed'); persistExpanded(); return; }
      if (e.target.closest('.tree-menu')) return;
      var row = e.target.closest('.tree-row');
      if (row) selectRow(row.closest('li.tree-node'));
    });
    tree.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var row = e.target.closest('.tree-row');
      if (row) { e.preventDefault(); selectRow(row.closest('li.tree-node')); }
    });

    // ---- search field selector + live filter ----
    var filterBtn = root.querySelector('#ex-filter-btn');
    var filterLabel = root.querySelector('#ex-filter-label');
    var filterMenu = root.querySelector('#ex-filter-menu');
    var clearBtn = root.querySelector('#ex-clear');
    if (filterBtn) {
      filterBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        filterMenu.style.display = filterMenu.style.display === 'block' ? 'none' : 'block';
      });
      Array.prototype.forEach.call(filterMenu.children, function (li) {
        li.addEventListener('click', function () {
          activeField = li.getAttribute('data-field');
          filterLabel.textContent = li.textContent;
          filterBtn.style.color = activeField ? 'var(--accent)' : 'var(--fg-muted)';
          filterMenu.style.display = 'none';
          search.value = ''; clearBtn.style.display = 'none';
          render(); search.focus();
        });
      });
      document.addEventListener('click', function (e) {
        if (!filterBtn.contains(e.target) && !filterMenu.contains(e.target)) filterMenu.style.display = 'none';
      });
    }
    search.addEventListener('input', function () {
      clearBtn.style.display = search.value ? 'block' : 'none';
      render();
    });
    clearBtn.addEventListener('click', function () {
      search.value = ''; clearBtn.style.display = 'none'; render(); search.focus();
    });

    // ---- type filter pills (inventory) ----
    if (activeTypes) {
      root.querySelectorAll('.ex-type-toggle').forEach(function (btn) {
        btn.classList.toggle('active', activeTypes.has(btn.getAttribute('data-type')));
        btn.addEventListener('click', function () {
          var t = btn.getAttribute('data-type');
          if (activeTypes.has(t)) { activeTypes.delete(t); btn.classList.remove('active'); }
          else { activeTypes.add(t); btn.classList.add('active'); }
          recomputeCounts(); render();
        });
      });
    }

    // ---- initial selection: ?org_unit_id wins, else last saved, else All ----
    var start = null;
    if (cfg.initialOrgUnitId != null) start = tree.querySelector('li.tree-node[data-id="' + cfg.initialOrgUnitId + '"]');
    if (!start) {
      var s = lsGet(LS_SEL);
      if (s === 'unassigned') start = tree.querySelector('li.tree-node[data-node="unassigned"]');
      else if (s && s.indexOf('id:') === 0) start = tree.querySelector('li.tree-node[data-id="' + s.slice(3) + '"]');
    }
    if (!start) start = tree.querySelector('li.tree-node[data-node="all"]');
    selectRow(start);

    function recomputeCounts() {
      // Folder counts reflect the currently-enabled types (search is transient).
      var shownCards = cards.filter(matchType);
      var unassigned = 0;
      shownCards.forEach(function (c) { if ((c.getAttribute('data-org-id') || '') === '') unassigned++; });
      var allEl = tree.querySelector('li[data-node="all"] .tree-count'); if (allEl) allEl.textContent = shownCards.length;
      var unEl = tree.querySelector('li[data-node="unassigned"] .tree-count'); if (unEl) unEl.textContent = unassigned;
      nodes.forEach(function (li) {
        var ids = descendantIds(li), cnt = 0;
        shownCards.forEach(function (c) { var oid = c.getAttribute('data-org-id'); if (oid && ids[oid]) cnt++; });
        var el = li.querySelector(':scope > .tree-row .tree-count'); if (el) el.textContent = cnt;
      });
    }

    // ---- drag & drop: drag a card onto a folder (or Unassigned) to reassign ----
    if (cfg.canEdit) {
      function clearDropHi() { tree.querySelectorAll('.drop-target').forEach(function (n) { n.classList.remove('drop-target'); }); }
      function dropLiFor(el) {
        var li = el.closest ? el.closest('li.tree-node') : null;
        if (!li || li.getAttribute('data-node') === 'all') return null;
        return li;
      }
      cardsWrap.addEventListener('dragstart', function (e) {
        var card = e.target.closest(cfg.cardSelector); if (!card) return;
        e.dataTransfer.setData('text/plain', card.getAttribute(cfg.idAttr));
        e.dataTransfer.effectAllowed = 'move';
        card.classList.add('dragging');
      });
      cardsWrap.addEventListener('dragend', function (e) {
        var card = e.target.closest(cfg.cardSelector); if (card) card.classList.remove('dragging');
        clearDropHi();
      });
      tree.addEventListener('dragover', function (e) {
        var li = dropLiFor(e.target); if (!li) return;
        e.preventDefault(); e.dataTransfer.dropEffect = 'move';
        clearDropHi(); li.querySelector(':scope > .tree-row').classList.add('drop-target');
      });
      tree.addEventListener('dragleave', function (e) { if (!tree.contains(e.relatedTarget)) clearDropHi(); });
      tree.addEventListener('drop', function (e) {
        var li = dropLiFor(e.target); if (!li) return;
        e.preventDefault(); clearDropHi();
        var id = e.dataTransfer.getData('text/plain'); if (!id) return;
        var orgId = li.getAttribute('data-node') === 'unassigned' ? '' : li.getAttribute('data-id');
        moveCard(id, orgId, li);
      });
      async function moveCard(id, orgId, li) {
        var card = cardsWrap.querySelector(cfg.cardSelector + '[' + cfg.idAttr + '="' + id + '"]');
        var res = await fetch(cfg.assignUrl(id, card), {
          method: 'POST', body: new URLSearchParams({ org_unit_id: orgId || '' }),
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
        });
        if (!res.ok) { alert('Could not move.'); return; }
        var data = await res.json();
        if (!data.ok) { alert(data.error || 'Could not move.'); return; }
        if (card) {
          card.setAttribute('data-org-id', data.org_unit_id || '');
          card.setAttribute('data-org', (data.path || '').toLowerCase());
          var meta = card.querySelector('.hcard-meta');
          var pathEl = card.querySelector('.hcard-path');
          if (data.path && meta) {
            if (!pathEl) { pathEl = document.createElement('span'); pathEl.className = 'hcard-path'; meta.insertBefore(pathEl, meta.firstChild); }
            pathEl.textContent = (cfg.pathPrefix || '') + data.path;
          } else if (pathEl) { pathEl.remove(); }
        }
        recomputeCounts(); render();
        var row = li.querySelector(':scope > .tree-row');
        row.classList.add('drop-done');
        setTimeout(function () { row.classList.remove('drop-done'); }, 650);
      }
    }

    // ---- context menus: folder CRUD (shared /applications routes) + card menu ----
    if (!cfg.canEdit) return;
    var menu = root.querySelector('#tree-ctx');
    if (!menu) return;
    var CHILD = { application: 'environment', environment: 'unit', unit: null };

    function expandLs(id) {
      var arr = []; try { arr = JSON.parse(lsGet(LS_EXP) || '[]'); } catch (e) {}
      if (arr.indexOf(String(id)) < 0) arr.push(String(id));
      lsSet(LS_EXP, JSON.stringify(arr));
    }
    async function post(url, data) {
      var res = await fetch(url, {
        method: 'POST', body: new URLSearchParams(data || {}),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
      });
      if (!res.ok) { alert('Could not complete that action - the name may already exist or be invalid.'); return false; }
      return true;
    }
    async function createChild(parentId, childTier, parentName) {
      var name = prompt('New ' + childTier + ' under "' + parentName + '":', '');
      if (!name || !name.trim()) return;
      expandLs(parentId);
      if (await post('/applications/units', { tier: childTier, parent_id: parentId, name: name.trim() })) location.reload();
    }
    async function createApp() {
      var name = prompt('New application:', '');
      if (!name || !name.trim()) return;
      if (await post('/applications/units', { tier: 'application', name: name.trim() })) location.reload();
    }
    async function renameUnit(id, cur, desc) {
      var name = prompt('Rename to:', cur);
      if (!name || !name.trim() || name.trim() === cur) return;
      if (await post('/applications/units/' + id + '/edit', { name: name.trim(), description: desc || '' })) location.reload();
    }
    async function deleteUnit(id, name, tier) {
      if (!confirm('Delete ' + tier + ' "' + name + '" and everything under it? Items become unassigned.')) return;
      if (lsGet(LS_SEL) === 'id:' + id) lsSet(LS_SEL, 'all');
      if (await post('/applications/units/' + id + '/delete', {})) location.reload();
    }
    function folderItems(li) {
      if (!li || li.classList.contains('tree-special'))
        return [{ head: 'Folders' }, { label: 'New application', icon: '+', fn: createApp }];
      var id = li.getAttribute('data-id'), tier = li.getAttribute('data-tier'),
          name = li.getAttribute('data-name'), desc = li.getAttribute('data-desc') || '';
      var items = [{ head: tier.charAt(0).toUpperCase() + tier.slice(1) + ': ' + name }];
      if (CHILD[tier]) items.push({ label: 'New ' + CHILD[tier], icon: '+', fn: function () { createChild(id, CHILD[tier], name); } });
      if (cfg.addHere) items.push({ label: 'Add ' + cfg.addLabel + ' here', icon: '+', fn: function () { window.location = cfg.addHere(id); } });
      items.push({ label: 'Rename…', fn: function () { renameUnit(id, name, desc); } });
      items.push({ sep: true });
      items.push({ label: 'Delete', danger: true, fn: function () { deleteUnit(id, name, tier); } });
      return items;
    }

    function closeMenu() { menu.hidden = true; menu.innerHTML = ''; }
    function openMenu(x, y, items) {
      menu.innerHTML = '';
      items.forEach(function (it) {
        if (it.sep) { var s = document.createElement('div'); s.className = 'ctx-sep'; menu.appendChild(s); return; }
        if (it.head) { var h = document.createElement('div'); h.className = 'ctx-head'; h.textContent = it.head; menu.appendChild(h); return; }
        var b = document.createElement('button');
        b.type = 'button'; b.textContent = (it.icon ? it.icon + '  ' : '') + it.label;
        if (it.danger) b.className = 'danger';
        b.addEventListener('click', function () { closeMenu(); it.fn(); });
        menu.appendChild(b);
      });
      menu.hidden = false;
      var w = menu.offsetWidth, h = menu.offsetHeight;
      menu.style.left = Math.min(x, window.innerWidth - w - 8) + 'px';
      menu.style.top = Math.min(y, window.innerHeight - h - 8) + 'px';
    }

    tree.addEventListener('contextmenu', function (e) {
      var row = e.target.closest('.tree-row');
      e.preventDefault();
      openMenu(e.clientX, e.clientY, folderItems(row ? row.closest('li.tree-node') : null));
    });
    tree.addEventListener('click', function (e) {
      var btn = e.target.closest('.tree-menu');
      if (!btn) return;
      e.preventDefault(); e.stopPropagation();
      var r = btn.getBoundingClientRect();
      openMenu(r.left, r.bottom + 2, folderItems(btn.closest('li.tree-node')));
    });

    var detail = root.querySelector('.ex-detail');
    function emptyItems() {
      var selLi = tree.querySelector('li.tree-node.selected');
      var oid = selLi && selLi.getAttribute('data-id') ? selLi.getAttribute('data-id') : null;
      // createMenu(oid) lets a blade offer several "New …" options (Inventory);
      // addHere is the single-type shortcut used by the per-resource blades.
      if (cfg.createMenu) return cfg.createMenu(oid);
      if (!cfg.addHere) return [];
      return [{ head: cfg.noun.charAt(0).toUpperCase() + cfg.noun.slice(1) + 's' },
        { label: 'Add ' + cfg.addLabel + (oid ? ' here' : ''), icon: '+', fn: function () { window.location = cfg.addHere(oid); } }];
    }
    if (detail && (cfg.cardMenu || cfg.addHere || cfg.createMenu)) {
      detail.addEventListener('contextmenu', function (e) {
        var card = e.target.closest(cfg.cardSelector);
        var items = card && cfg.cardMenu ? cfg.cardMenu(card) : emptyItems();
        if (!items.length) return;          // nothing to offer (e.g. empty space, no addHere)
        e.preventDefault();
        openMenu(e.clientX, e.clientY, items);
      });
    }
    document.addEventListener('click', function (e) { if (!menu.contains(e.target) && !e.target.closest('.tree-menu')) closeMenu(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenu(); });
    window.addEventListener('blur', closeMenu);
    tree.addEventListener('scroll', closeMenu);
  };
})();
