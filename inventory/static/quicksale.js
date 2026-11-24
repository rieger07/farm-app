(() => {
  const SKUS = JSON.parse(document.getElementById('skus').textContent);
  const HANDLES = JSON.parse((document.getElementById('pay-handles') || { textContent: '{}' }).textContent);
  const BOOTH = JSON.parse((document.getElementById('active-booth') || { textContent: '{"active":false}' }).textContent);
  const QKEY = 'hh_cash_queue'; // legacy key name kept so queued sales survive the rename
  const lines = []; // [{ sku_id, qty }]
  let cat = '', query = '', hideOos = false, boothOnly = !!(BOOTH && BOOTH.active), payType = 'cash';
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

  function effectiveStock(s) {
    if (BOOTH && BOOTH.active && s.booth_packed && typeof s.booth_left === 'number') {
      return s.booth_left;
    }
    return typeof s.on_hand === 'number' ? s.on_hand : null;
  }

  function renderTiles() {
    const t = $('tiles'); t.innerHTML = '';
    const list = SKUS.filter(s => {
      if (BOOTH && BOOTH.active && boothOnly && !s.booth_packed && !unitQty(s.id) && !lines.some(l => l.sku_id === s.id)) return false;
      if (cat && s.category !== cat) return false;
      if (!(s.name + ' ' + s.variant).toLowerCase().includes(query)) return false;
      const eff = effectiveStock(s);
      if (hideOos && typeof eff === 'number' && round3(eff - usedForStock(s)) <= 0) return false;
      return true;
    }).sort((a, b) => {
      if (BOOTH && BOOTH.active && a.booth_packed !== b.booth_packed) {
        return a.booth_packed ? -1 : 1;
      }
      const effA = effectiveStock(a), effB = effectiveStock(b);
      const aOos = (typeof effA === 'number' && round3(effA - usedForStock(a)) <= 0) ? 1 : 0;
      const bOos = (typeof effB === 'number' && round3(effB - usedForStock(b)) <= 0) ? 1 : 0;
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
      const unitSuffix = s.sell_mode === 'lb_stock_lb' ? ' lb' : '';
      if (BOOTH && BOOTH.active && s.booth_packed && typeof s.booth_left === 'number') {
        const left = round3(s.booth_left - usedForStock(s));
        parts.push(left <= 0 ? `0${unitSuffix} at booth` : `${left}${unitSuffix} at booth`);
        if (left <= 0) sub.classList.add('lowhint');
      } else if (BOOTH && BOOTH.active && !s.booth_packed) {
        const left = typeof s.on_hand === 'number' ? round3(s.on_hand - usedForStock(s)) : null;
        parts.push(left !== null ? `Not packed · ${Math.max(0, left)}${unitSuffix} home` : 'Not packed');
      } else if (typeof s.on_hand === 'number') {
        const left = round3(s.on_hand - usedForStock(s));
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
      if (q) {
        const minus = Object.assign(document.createElement('button'), { textContent: '−', title: lbMode ? 'Remove last weighed item' : 'Decrease quantity' });
        minus.onclick = e => {
          e.stopPropagation();
          if (lbMode) {
            for (let i = lines.length - 1; i >= 0; i--) {
              if (lines[i].sku_id === s.id) { lines.splice(i, 1); break; }
            }
            render();
          } else {
            setUnitQty(s.id, q - 1);
          }
        };
        step.append(minus, Object.assign(document.createElement('span'), { textContent: q }));
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
      d.innerHTML = '<span></span><span class="line-right"><b></b></span>';
      const qtyLabel = isLb(s) ? `${l.qty} lb` : `${l.qty}`;
      d.children[0].textContent = `${qtyLabel} × ${s.name}${s.variant ? ' · ' + s.variant : ''}`;
      const right = d.querySelector('.line-right');
      right.querySelector('b').textContent = money(lineCents(l));
      const rm = Object.assign(document.createElement('button'), {
        type: 'button',
        className: 'line-rm',
        textContent: '×',
        title: 'Remove line',
      });
      rm.setAttribute('aria-label', `Remove ${s.name}`);
      rm.onclick = e => {
        e.stopPropagation();
        lines.splice(idx, 1);
        render();
      };
      right.append(rm);
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
    const payLabel = payType === 'venmo' ? 'Venmo' : (payType === 'paypal' ? 'PayPal' : 'cash');
    btn.textContent = lines.length ? `Record ${payLabel} sale · ${money(t)}` : `Record ${payLabel} sale`;
    const cf = $('cash-fields');
    if (cf) cf.hidden = payType !== 'cash';
    renderDigitalPay(t);
    const sticky = $('sticky-cart');
    if (sticky) {
      sticky.hidden = !lines.length;
      $('sticky-summary').textContent = `${cnt} ${cnt === 1 ? 'item' : 'items'} · ${money(t)}`;
    }
  }

  function cartNote() {
    const parts = lines.map(l => {
      const s = byId[l.sku_id];
      const q = isLb(s) ? `${l.qty}lb` : `${l.qty}x`;
      return `${q} ${s.name}`;
    });
    const full = parts.join(', ');
    return full.length > 90 ? full.slice(0, 87) + '...' : full;
  }

  function digitalPayInfo(cents) {
    const amountStr = (Math.round(cents) / 100).toFixed(2);
    const note = cartNote();
    if (payType === 'venmo') {
      const handle = (HANDLES.venmo_handle || '').trim();
      const qs = `amount=${encodeURIComponent(amountStr)}${note ? '&note=' + encodeURIComponent(note) : ''}`;
      return {
        provider: 'venmo',
        label: 'Venmo',
        handle,
        displayHandle: handle ? '@' + handle : '',
        payUrl: handle ? `https://venmo.com/${encodeURIComponent(handle)}?txn=pay&${qs}` : '',
        chargeUrl: `https://venmo.com/?txn=charge&${qs}`,
        amountStr,
        note,
      };
    }
    if (payType === 'paypal') {
      const handle = (HANDLES.paypal_handle || '').trim();
      return {
        provider: 'paypal',
        label: 'PayPal',
        handle,
        displayHandle: handle ? `paypal.me/${handle}` : '',
        payUrl: handle ? `https://paypal.me/${encodeURIComponent(handle)}/${amountStr}USD` : '',
        chargeUrl: '',
        amountStr,
        note,
      };
    }
    return null;
  }

  function renderDigitalPay(cents) {
    const box = $('digital-pay-box');
    if (!box) return;
    const info = digitalPayInfo(cents);
    if (!info) { box.hidden = true; return; }
    box.hidden = false;
    const hasItems = lines.length > 0 && cents > 0;
    const showQr = $('show-qr');
    const reqLink = $('open-venmo-req');
    const copyBtn = $('copy-pay-link');
    const hint = $('no-handle-hint');

    if (info.handle) {
      showQr.hidden = false;
      showQr.disabled = !hasItems;
      showQr.textContent = hasItems
        ? `Show ${info.label} QR · $${info.amountStr}`
        : `Show ${info.label} QR`;
      copyBtn.hidden = !hasItems;
      hint.hidden = true;
    } else {
      showQr.hidden = true;
      copyBtn.hidden = true;
      hint.hidden = false;
      $('no-handle-name').textContent = info.label === 'PayPal' ? 'PayPal.Me' : info.label;
    }

    if (info.chargeUrl && hasItems) {
      reqLink.hidden = false;
      reqLink.href = info.chargeUrl;
    } else {
      reqLink.hidden = true;
    }
  }

  function openQrModal() {
    const t = total();
    const info = digitalPayInfo(t);
    if (!info || !info.payUrl || t <= 0) return;
    $('qr-title').textContent = `Pay with ${info.label}`;
    $('qr-handle').textContent = info.displayHandle;
    $('qr-amount').textContent = `$${info.amountStr}`;
    $('qr-note').textContent = info.note || '';
    const stage = $('qr-svg');
    if (typeof window.qrcode === 'function') {
      const qr = window.qrcode(0, 'M');
      qr.addData(info.payUrl);
      qr.make();
      stage.innerHTML = qr.createSvgTag({ scalable: true, margin: 2 });
    } else {
      stage.innerHTML = '<small class="muted">QR library unavailable</small>';
    }
    const smsBody = info.note
      ? `Here is your ${info.label} payment link for $${info.amountStr} (${info.note}): ${info.payUrl}`
      : `Here is your ${info.label} payment link for $${info.amountStr}: ${info.payUrl}`;
    $('qr-sms').href = `sms:?&body=${encodeURIComponent(smsBody)}`;
    const chargeBtn = $('qr-charge');
    if (info.chargeUrl) {
      chargeBtn.hidden = false;
      chargeBtn.href = info.chargeUrl;
    } else {
      chargeBtn.hidden = true;
    }
    $('qr-record').textContent = `Payment received · Record ${info.label} sale`;
    $('qrm').showModal();
  }

  async function copyPayLink(btn) {
    const info = digitalPayInfo(total());
    if (!info || !info.payUrl) return;
    const orig = btn.textContent;
    try {
      await navigator.clipboard.writeText(info.payUrl);
      btn.textContent = 'Copied!';
    } catch (e) {
      window.prompt('Copy payment link:', info.payUrl);
      return;
    }
    setTimeout(() => { btn.textContent = orig; }, 1600);
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
    const saleTotal = total();
    const sale = {
      client_uuid: crypto.randomUUID ? crypto.randomUUID()
        : 'x' + Date.now() + Math.random().toString(16).slice(2),
      occurred_at: new Date().toISOString().slice(0, 19),
      payment_type: payType,
      cash_received_cents: payType === 'cash' ? cashCents() : null,
      items: lines.map(l => ({ sku_id: l.sku_id, quantity: l.qty })),
    };
    const seen = new Set(lines.map(l => l.sku_id));
    seen.forEach(sid => {
      const s = byId[sid];
      if (!s) return;
      const used = usedForStock(s);
      if (typeof s.on_hand === 'number') s.on_hand = round3(s.on_hand - used);
      if (typeof s.booth_left === 'number') s.booth_left = Math.max(0, round3(s.booth_left - used));
    });
    if (BOOTH && BOOTH.active && $('qs-booth-meta')) {
      BOOTH.gross_sales_cents = (BOOTH.gross_sales_cents || 0) + saleTotal;
      let html = `<b>${money(BOOTH.gross_sales_cents)}</b> gross`;
      if (BOOTH.booth_cost_cents > 0) {
        html += BOOTH.gross_sales_cents >= BOOTH.booth_cost_cents
          ? ' · Covered booth!'
          : ` · ${money(BOOTH.booth_cost_cents)} booth`;
      }
      $('qs-booth-meta').innerHTML = html;
    }
    const q = queue(); q.push(sale); saveQueue(q);
    lines.length = 0; $('cash').value = '';
    render(); banner(); flush();
  };

  $('cash').addEventListener('input', renderCart);
  if ($('pay-chips')) {
    $('pay-chips').addEventListener('click', e => {
      const b = e.target.closest('[data-pay]'); if (!b) return;
      payType = b.dataset.pay;
      document.querySelectorAll('#pay-chips [data-pay]').forEach(c => c.classList.toggle('on', c === b));
      renderCart();
    });
  }
  if ($('show-qr')) $('show-qr').onclick = openQrModal;
  if ($('copy-pay-link')) $('copy-pay-link').onclick = () => copyPayLink($('copy-pay-link'));
  if ($('qr-copy')) $('qr-copy').onclick = () => copyPayLink($('qr-copy'));
  if ($('qr-record')) {
    $('qr-record').onclick = () => {
      $('qrm').close();
      $('record').click();
    };
  }
  document.querySelectorAll('.fast-cash [data-cash]').forEach(btn => {
    btn.onclick = () => {
      const val = btn.dataset.cash;
      $('cash').value = val === 'exact' ? (total() / 100).toFixed(2) : Number(val).toFixed(2);
      renderCart();
    };
  });
  if ($('booth-scope')) {
    $('booth-scope').onclick = () => {
      boothOnly = !boothOnly;
      $('booth-scope').classList.toggle('on', boothOnly);
      $('booth-scope').setAttribute('aria-pressed', boothOnly ? 'true' : 'false');
      $('booth-scope').textContent = boothOnly
        ? `At booth (${BOOTH.packed_sku_count || 0})`
        : 'All catalog';
      renderTiles();
    };
  }
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
