(() => {
  const toggle = document.getElementById('nav-toggle');
  const links = document.getElementById('nav-links');
  if (toggle && links) {
    toggle.onclick = () => links.classList.toggle('open');
  }

  const copyBtn = document.getElementById('copy-email');
  if (copyBtn) {
    copyBtn.onclick = async () => {
      try {
        await navigator.clipboard.writeText('steve.rieger.dev@gmail.com');
        copyBtn.textContent = 'Copied!';
        setTimeout(() => (copyBtn.textContent = 'Copy email'), 1800);
      } catch (_) {}
    };
  }

  const form = document.getElementById('contact-form');
  if (form) {
    const status = document.getElementById('form-status');
    const btn = form.querySelector('button[type="submit"]');
    form.onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const payload = {
        name: String(fd.get('name') || '').trim(),
        email: String(fd.get('email') || '').trim(),
        engagement: String(fd.get('engagement') || 'Consulting').trim(),
        message: String(fd.get('message') || '').trim(),
        website: String(fd.get('website') || '').trim(),
      };
      status.hidden = false;
      status.textContent = 'Sending...';
      if (btn) btn.disabled = true;
      try {
        const res = await fetch('/api/contact', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (res.ok) {
          form.reset();
          status.textContent = 'Thanks — your message was sent. I will reply shortly.';
        } else {
          const err = await res.json().catch(() => ({}));
          status.textContent = err.detail || 'Could not send message. Please email steve.rieger.dev@gmail.com directly.';
        }
      } catch (_) {
        status.textContent = 'Network error. Please email steve.rieger.dev@gmail.com directly.';
      } finally {
        if (btn) btn.disabled = false;
      }
    };
  }
})();
