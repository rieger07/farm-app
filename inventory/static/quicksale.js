(() => {
  const SKUS = JSON.parse(document.getElementById('skus').textContent);
  const QKEY = 'hh_cash_queue'; // legacy key name kept so queued sales survive the rename
  const lines = []; // [{ sku_id, qty }]
  let cat = '', query = '', hideOos = false;
  let wmState = null; // { sku, editIdx }
  const $ = id => document.getElementById(id);
  const money = c => '$' + (Math.round(c) / 100).toFixed(2);
  const round3 = n => Math.round(n * 1000) / 1000;
  const isLb = s => s.sell_mode === 'lb_stock_unit' || s.sell_mode === 'lb_stock_lb';
  const byId = Object.fromEntries(SKUS.map(s => [s.id, s]));

  const lineCents = l => Math.round(byId[l.sku_id].price_cents * l.qty);
  const total = () => lines.reduce((t, l) => t + lineCents(l), 0);
  const itemCount = () => lines.reduce((a, l) => a + (isLb(byId[l.sku_id]) ? 1 : l.qty), 0);
  const cashCents = () => {
    const v = parseFloat(($('cash').value || '').replace(',', '.'));
    return isNaN(v) ? null : Math.round(v * 100);
  };

  function unitQty(id) {
    const l = lines.find(x => x.sku_id === id);
    return l ? l.qty : 0;
  }

  function setUnitQty(id, q) {
    const idx = lines.findIndex(x => x.sku_id === id);
    if (q <= 0) {
      if (idx >= 0) lines.splice(idx, 1);
    } else if (idx >= 0) {
      lines[idx].qty = q;
    } else {
      lines.push({ sku_id: id, qty: q });
    }
    render();
  }

  function usedForStock(s) {
    const mine = lines.filter(l => l.sku_id === s.id);
    if (s.sell_mode === 'lb_stock_unit') return mine.length;
    return mine.reduce((a, b) => a + b.qty, 0);
  }

  function openWeightModal(sku, editIdx = -1) {
    wmState = { sku, editIdx };
    $('wm-title').textContent = sku.name + (sku.variant ? ' · ' + sku.variant : '');
    $('wm-rate').textContent = money(sku.price_cents) + ' / lb';
    $('wm-lbs').value = editIdx >= 0 ? String(lines[editIdx].qty) : '';
    $('wm-remove').hidden = editIdx < 0;
    $('wm-save').textContent = editIdx >= 0 ? 'Save weight' : 'Add to sale';
    syncWeightPreview();
    $('weightm').showModal();
    setTimeout(() => $('wm-lbs').focus(), 20);
  }

  function syncWeightPreview() {
    if (!wmState) return;
    const v = parseFloat(($('wm-lbs').value || '').replace(',', '.'));
    const valid = !isNaN(v) && v > 0;
    $('wm-preview').textContent = valid ? money(wmState.sku.price_cents * v) : '$0.00';
    $('wm-save').disabled = !valid;
  }

  $('wm-lbs').addEventListener('input', syncWeightPreview);
  $('wm-lbs').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !$('wm-save').disabled) {
      e.preventDefault();
      $('wm-save').click();
    }
  });
  document.querySelectorAll('#weightm [data-lb]').forEach(btn => {
    btn.onclick = () => {
      const cur = parseFloat(($('wm-lbs').value || '').replace(',', '.')) || 0;
      const add = parseFloat(btn.dataset.lb);
      $('wm-lbs').value = String(round3(cur + add));
      syncWeightPreview();
      $('wm-lbs').focus();
    };
  });
  $('wm-save').onclick = () => {
    if (!wmState) return;
    const v = round3(parseFloat(($('wm-lbs').value || '').replace(',', '.')));
    if (isNaN(v) || v <= 0) return;
    if (wmState.editIdx >= 0) lines[wmState.editIdx].qty = v;
    else lines.push({ sku_id: wmState.sku.id, qty: v });
    $('weightm').close();
    wmState = null;
    render();
  };
  $('wm-remove').onclick = () => {
    if (!wmState || wmState.editIdx < 0) return;
    lines.splice(wmState.editIdx, 1);
    $('weightm').close();
    wmState = null;
    render();
  };

  function renderTiles() {
    const t = $('tiles'); t.innerHTML = '';
    const list = SKUS.filter(s => {
      if (cat && s.category !== cat) return false;
      if (!(s.name + ' ' + s.variant).toLowerCase().includes(query)) return false;
      if (hideOos && typeof s.on_hand === 'number' && round3(s.on_hand - usedForStock(s)) <= 0) return false;
      return true;
    }).sort((a, b) => {
      const aOos = (typeof a.on_hand === 'number' && round3(a.on_hand - usedForStock(a)) <= 0) ? 1 : 0;
      const bOos = (typeof b.on_hand === 'number' && round3(b.on_hand - usedForStock(b)) <= 0) ? 1 : 0;
      return aOos - bOos;
    });
    list.forEach(s => {
      const lbMode = isLb(s);
      const mineCount = lines.filter(l => l.sku_id === s.id).length;
      const q = lbMode ? mineCount : unitQty(s.id);
      const el = document.createElement('div');
      el.className = 'tile' + (q ? ' sel' : '');
      const rateSuffix = lbMode ? '/lb' : '';
      el.innerHTML = `<b></b><small></small><div class="pr"><span>${money(s.price_cents)}${rateSuffix}</span></div>`;
      el.querySelector('b').textContent = s.name;
      const sub = el.querySelector('small');
      const parts = [];
      if (s.variant) parts.push(s.variant);
      if (typeof s.on_hand === 'number') {
        const left = round3(s.on_hand - usedForStock(s));
        const unitSuffix = s.sell_mode === 'lb_stock_lb' ? ' lb' : '';
        parts.push(left <= 0 ? `0${unitSuffix} left` : `${left}${unitSuffix} left`);
        if (left <= 0) sub.classList.add('lowhint');
      }
      sub.textContent = parts.join(' · ');
      const pr = el.querySelector('.pr');
      const step = document.createElement('div'); step.className = 'step';
      const plus = Object.assign(document.createElement('button'), { textContent: '+' });
      plus.onclick = e => {
        e.stopPropagation();
        if (lbMode) openWeightModal(s);
        else setUnitQty(s.id, q + 1);
      };
      if (q && !lbMode) {
        const minus = Object.assign(document.createElement('button'), { textContent: '−' });
        minus.onclick = e => { e.stopPropagation(); setUnitQty(s.id, q - 1); };
        step.append(minus, Object.assign(document.createElement('span'), { textContent: q }));
      } else if (q && lbMode) {
        step.append(Object.assign(document.createElement('span'), { textContent: q }));
      }
      step.append(plus); pr.append(step);
      el.onclick = () => {
        if (lbMode) openWeightModal(s);
        else setUnitQty(s.id, q + 1);
      };
      t.append(el);
    });
  }

  function renderCart() {
    $('lines').innerHTML = lines.length ? '' : '<small class="muted">Tap a product to add it.</small>';
    lines.forEach((l, idx) => {
      const s = byId[l.sku_id], d = document.createElement('div');
      d.innerHTML = '<span></span><span></span>';
      const qtyLabel = isLb(s) ? `${l.qty} lb` : `${l.qty}`;
      d.children[0].textContent = `${qtyLabel} × ${s.name}${s.variant ? ' · ' + s.variant : ''}`;
      d.children[1].textContent = money(lineCents(l));
      if (isLb(s)) {
        d.style.cursor = 'pointer';
        d.title = 'Tap to edit weight';
        d.onclick = () => openWeightModal(s, idx);
      }
      $('lines').append(d);
    });
    const cnt = itemCount();
    $('itemcount').textContent = cnt + (cnt === 1 ? ' item' : ' items');
    const t = total(), c = cashCents();
    $('total').textContent = money(t);
    const diff = c === null ? 0 : c - t;
    $('change').textContent = money(Math.max(diff, 0));
    $('change').parentElement.classList.toggle('short', c !== null && diff < 0);
    const btn = $('record');
    btn.disabled = !lines.length;
    btn.textContent = lines.length ? `Record cash sale · ${money(t)}` : 'Record cash sale';
    const sticky = $('sticky-cart');
    if (sticky) {
      sticky.hidden = !lines.length;
      $('sticky-summary').textContent = `${cnt} ${cnt === 1 ? 'item' : 'items'} · ${money(t)}`;
    }
  }
  const render = () => { renderTiles(); renderCart(); };

  // ---- offline queue ----
  const queue = () => JSON.parse(localStorage.getItem(QKEY) || '[]');
  const saveQueue = q => localStorage.setItem(QKEY, JSON.stringify(q));
  function banner() {
    const n = queue().length, el = $('offline');
    el.hidden = !n && navigator.onLine;
    el.textContent = !navigator.onLine
      ? `Offline. ${n} sale${n === 1 ? '' : 's'} will sync when you're back online.`
      : `${n} sale${n === 1 ? '' : 's'} waiting to sync...`;
  }
  async function flush() {
    let q = queue();
    while (q.length && navigator.onLine) {
      try {
        const r = await fetch('/api/cash-sale', {method: 'POST',
          headers: {'Content-Type': 'application/json'}, body: JSON.stringify(q[0])});
        if (r.status === 401) { location.href = '/login'; return; }
        if (r.ok || r.status === 400) q.shift(); // 400 = invalid, drop to avoid a stuck queue
        else break;
      } catch (e) { break; }
      saveQueue(q);
    }
    saveQueue(q); banner();
  }

  $('record').onclick = () => {
    const sale = {
      client_uuid: crypto.randomUUID ? crypto.randomUUID()
        : 'x' + Date.now() + Math.random().toString(16).slice(2),
      occurred_at: new Date().toISOString().slice(0, 19),
      cash_received_cents: cashCents(),
      items: lines.map(l => ({ sku_id: l.sku_id, quantity: l.qty })),
    };
    const q = queue(); q.push(sale); saveQueue(q);
    lines.length = 0; $('cash').value = '';
    render(); banner(); flush();
  };

  $('cash').addEventListener('input', renderCart);
  document.querySelectorAll('.fast-cash [data-cash]').forEach(btn => {
    btn.onclick = () => {
      const val = btn.dataset.cash;
      $('cash').value = val === 'exact' ? (total() / 100).toFixed(2) : Number(val).toFixed(2);
      renderCart();
    };
  });
  if ($('hide-oos')) {
    $('hide-oos').onclick = () => {
      hideOos = !hideOos;
      $('hide-oos').classList.toggle('on', hideOos);
      $('hide-oos').setAttribute('aria-pressed', hideOos ? 'true' : 'false');
      renderTiles();
    };
  }
  if ($('sticky-checkout')) {
    $('sticky-checkout').onclick = () => {
      $('cart-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
      setTimeout(() => $('cash').focus(), 250);
    };
  }
  $('q').addEventListener('input', e => { query = e.target.value.toLowerCase(); renderTiles(); });
  $('chips').addEventListener('click', e => {
    const b = e.target.closest('.chip'); if (!b) return;
    cat = b.dataset.cat;
    document.querySelectorAll('#chips .chip').forEach(c => c.classList.toggle('on', c === b));
    renderTiles();
  });
  addEventListener('online', flush); addEventListener('offline', banner);

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
  render(); banner(); flush();
})();
