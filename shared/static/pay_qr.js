// Shared Venmo / PayPal.Me payment link & QR code SVG helpers.
(function (root) {
  function buildPaymentInfo(provider, handle, amountDollars, note) {
    const cleanHandle = String(handle || '').trim();
    const amountStr = Math.max(0, Number(amountDollars) || 0).toFixed(2);
    const cleanNote = String(note || '').trim();

    if (provider === 'venmo') {
      const qs = `amount=${encodeURIComponent(amountStr)}${cleanNote ? '&note=' + encodeURIComponent(cleanNote) : ''}`;
      return {
        provider: 'venmo',
        label: 'Venmo',
        handle: cleanHandle,
        displayHandle: cleanHandle ? '@' + cleanHandle : '',
        payUrl: cleanHandle ? `https://venmo.com/${encodeURIComponent(cleanHandle)}?txn=pay&${qs}` : '',
        chargeUrl: `https://venmo.com/?txn=charge&${qs}`,
        amountStr,
        note: cleanNote,
      };
    }

    if (provider === 'paypal') {
      return {
        provider: 'paypal',
        label: 'PayPal',
        handle: cleanHandle,
        displayHandle: cleanHandle ? `paypal.me/${cleanHandle}` : '',
        payUrl: cleanHandle ? `https://paypal.me/${encodeURIComponent(cleanHandle)}/${amountStr}USD` : '',
        chargeUrl: '',
        amountStr,
        note: cleanNote,
      };
    }

    return null;
  }

  function renderQrSvg(containerEl, url, opts) {
    if (!containerEl) return false;
    const options = opts || {};
    if (typeof root.qrcode === 'function' && url) {
      const qr = root.qrcode(0, 'M');
      qr.addData(url);
      qr.make();
      const margin = options.margin != null ? options.margin : 2;
      containerEl.innerHTML = qr.createSvgTag({ scalable: true, margin });
      if (options.maxWidth) {
        const svg = containerEl.querySelector('svg');
        if (svg) {
          svg.style.width = '100%';
          svg.style.maxWidth = options.maxWidth;
          svg.style.height = 'auto';
        }
      }
      return true;
    }
    if (!options.silentFallback) {
      containerEl.innerHTML = '<small class="muted">QR library unavailable</small>';
    }
    return false;
  }

  root.PayQR = { buildPaymentInfo, renderQrSvg };
})(window);
