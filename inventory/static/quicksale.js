(() => {
  const SKUS = JSON.parse(document.getElementById('skus').textContent);
  const QKEY = 'hh_cash_queue'; // legacy key name kept so queued sales survive the rename
  const cart = {}; // sku_id -> qty
  let cat = '', query = '';
  const $ = id => document.getElementById(id);
  const money = c => '$' + (c / 100).toFixed(2);
  const byId = Object.fromEntries(SKUS.map(s => [s.id, s]));

  const total = () => Object.entries(cart).reduce((t, [id, q]) => t + byId[id].price_cents * q, 0);
  const count = () => Object.values(cart).reduce((a, b) => a + b, 0);
  const cashCents = () => {
    const v = parseFloat(($('cash').value || '').replace(',', '.'));
    return isNaN(v) ? null : Math.round(v * 100);
  };

  function setQty(id, q) {
    if (q <= 0) delete cart[id]; else cart[id] = q;
    render();
  }

  function renderTiles() {
    const t = $('tiles'); t.innerHTML = '';
    SKUS.filter(s => (!cat || s.category === cat) &&
        (s.name + ' ' + s.variant).toLowerCase().includes(query)).forEach(s => {
      const q = cart[s.id] || 0;
      const el = document.createElement('div');
      el.className = 'tile' + (q ? ' sel' : '');
      el.innerHTML = `<b></b><small></small><div class="pr"><span>${money(s.price_cents)}</span></div>`;
      el.querySelector('b').textContent = s.name;
      const sub = el.querySelector('small');
      const parts = [];
      if (s.variant) parts.push(s.variant);
      if (typeof s.on_hand === 'number') {
        const left = s.on_hand - q;
        parts.push(left <= 0 ? '0 left' : `${left} left`);
        if (left <= 0) sub.classList.add('lowhint');
      }
      sub.textContent = parts.join(' · ');
      const pr = el.querySelector('.pr');
      const step = document.createElement('div'); step.className = 'step';
      const plus = Object.assign(document.createElement('button'), {textContent: '+'});
      plus.onclick = e => { e.stopPropagation(); setQty(s.id, q + 1); };
      if (q) {
        const minus = Object.assign(document.createElement('button'), {textContent: '−'});
        minus.onclick = e => { e.stopPropagation(); setQty(s.id, q - 1); };
        step.append(minus, Object.assign(document.createElement('span'), {textContent: q}));
      }
      step.append(plus); pr.append(step);
      el.onclick = () => setQty(s.id, q + 1);
      t.append(el);
    });
  }

  function renderCart() {
    const ids = Object.keys(cart);
    $('lines').innerHTML = ids.length ? '' : '<small class="muted">Tap a product to add it.</small>';
    ids.forEach(id => {
      const s = byId[id], d = document.createElement('div');
      d.innerHTML = '<span></span><span></span>';
      d.children[0].textContent = `${cart[id]} × ${s.name}${s.variant ? ' · ' + s.variant : ''}`;
      d.children[1].textContent = money(s.price_cents * cart[id]);
      $('lines').append(d);
    });
    $('itemcount').textContent = count() + (count() === 1 ? ' item' : ' items');
    const t = total(), c = cashCents();
    $('total').textContent = money(t);
    const diff = c === null ? 0 : c - t;
    $('change').textContent = money(Math.max(diff, 0));
    $('change').parentElement.classList.toggle('short', c !== null && diff < 0);
    const btn = $('record');
    btn.disabled = !ids.length;
    btn.textContent = ids.length ? `Record cash sale · ${money(t)}` : 'Record cash sale';
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
      items: Object.entries(cart).map(([id, q]) => ({sku_id: +id, quantity: q})),
    };
    const q = queue(); q.push(sale); saveQueue(q);
    Object.keys(cart).forEach(k => delete cart[k]); $('cash').value = '';
    render(); banner(); flush();
  };

  $('cash').addEventListener('input', renderCart);
  $('q').addEventListener('input', e => { query = e.target.value.toLowerCase(); renderTiles(); });
  $('chips').addEventListener('click', e => {
    const b = e.target.closest('.chip'); if (!b) return;
    cat = b.dataset.cat;
    document.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c === b));
    renderTiles();
  });
  addEventListener('online', flush); addEventListener('offline', banner);

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
  render(); banner(); flush();
})();
