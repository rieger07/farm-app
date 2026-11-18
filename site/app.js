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
    form.onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const endpoint = (form.dataset.endpoint || '').trim();
      const name = String(fd.get('name') || '').trim();
      const email = String(fd.get('email') || '').trim();
      const type = String(fd.get('engagement') || 'Consulting').trim();
      const message = String(fd.get('message') || '').trim();

      if (endpoint) {
        status.hidden = false;
        status.textContent = 'Sending...';
        try {
          const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Accept': 'application/json' },
            body: fd,
          });
          if (res.ok) {
            form.reset();
            status.textContent = 'Thanks — your message has been sent.';
            return;
          }
        } catch (_) {}
      }

      const subject = encodeURIComponent(`[Ironclad Systems] ${type} inquiry from ${name}`);
      const body = encodeURIComponent(`Name: ${name}\nEmail: ${email}\nEngagement: ${type}\n\n${message}`);
      window.location.href = `mailto:steve.rieger.dev@gmail.com?subject=${subject}&body=${body}`;
      status.hidden = false;
      status.textContent = 'Opening your email client to send to steve.rieger.dev@gmail.com...';
    };
  }
})();
