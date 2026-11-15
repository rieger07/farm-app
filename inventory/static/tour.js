(function () {
  const root = document.getElementById("tour-root");
  if (!root) return;

  const userId = root.dataset.userId || "0";
  const role = root.dataset.role || "";
  const isLeader = role === "owner" || role === "manager";
  const doneKey = "ironledger_tour_done_" + userId;
  const stepKey = "ironledger_tour_step_" + userId;

  const STEPS = [
    {
      path: "/inventory",
      target: ".pagehead",
      title: "Welcome to iron ledger",
      body: "This is your single place for stock counts, Zettle sales, and cash market sales. Zettle manages your product catalog and prices; iron ledger tracks your true on-hand counts."
    },
    {
      path: "/inventory",
      target: "#attn-btn, .cards.three",
      title: "Spot what needs restocking",
      body: "Your summary cards show total products, units on hand, and items needing attention. Tap 'Needs attention' anytime to filter straight to low-stock or out-of-stock items."
    },
    {
      path: "/inventory",
      target: ".inv-adjust-btn, .c-act .btn",
      title: "Update counts & Quick Sale visibility",
      body: "Tap Update on any product to set an exact count, log a restock batch, change its low-stock alert, view recent stock history, or uncheck 'Show on Quick Sale screen' to hide seasonal items."
    },
    {
      path: "/sales",
      target: 'a[href^="/sales/export.csv"], .pagehead',
      title: "Zettle + Cash sales in one place",
      body: "Every Zettle purchase syncs here automatically alongside your Quick Sale cash orders. Filter by date or source, and click Export CSV whenever you need a spreadsheet for bookkeeping."
    },
    {
      path: "/quick-sale",
      target: "#tiles, .qs",
      title: "Fast cash checkout (works offline)",
      body: "Tap product tiles to ring up cash sales and calculate change. Each tile shows how many units are left. If cell signal drops at the market, sales queue safely and sync when you're back online."
    },
    {
      path: "/settings",
      target: ".content",
      title: "Zettle sync & team logins",
      body: "Connect or sync Zettle here, and create staff logins for market cashiers (staff only see Quick Sale). You can replay this tour anytime from the 'Tour' button in the top bar."
    }
  ];

  let activeEl = null;
  let cardEl = null;

  function clearFocus() {
    if (activeEl) {
      activeEl.classList.remove("tour-focus");
      activeEl = null;
    }
  }

  function findVisibleTarget(selector) {
    if (!selector) return null;
    const parts = selector.split(",");
    for (const sel of parts) {
      const list = document.querySelectorAll(sel.trim());
      for (const el of list) {
        if (el.offsetParent !== null || el.getClientRects().length > 0) {
          return el;
        }
      }
    }
    return null;
  }

  function endTour(markDone) {
    clearFocus();
    if (cardEl) {
      cardEl.remove();
      cardEl = null;
    }
    localStorage.removeItem(stepKey);
    if (markDone) localStorage.setItem(doneKey, "1");
  }

  function goToStep(idx) {
    if (idx < 0 || idx >= STEPS.length) {
      endTour(true);
      return;
    }
    const step = STEPS[idx];
    localStorage.setItem(stepKey, String(idx));
    if (window.location.pathname !== step.path) {
      window.location.href = step.path;
      return;
    }
    renderStep(idx);
  }

  function renderStep(idx) {
    const step = STEPS[idx];
    clearFocus();
    const target = findVisibleTarget(step.target);
    if (target) {
      activeEl = target;
      activeEl.classList.add("tour-focus");
      activeEl.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    if (!cardEl) {
      cardEl = document.createElement("aside");
      cardEl.id = "tour-card";
      cardEl.className = "tour-card";
      cardEl.setAttribute("role", "dialog");
      cardEl.setAttribute("aria-label", "Guided tour");
      document.body.appendChild(cardEl);
    }

    const isFirst = idx === 0;
    const isLast = idx === STEPS.length - 1;
    cardEl.innerHTML =
      '<div class="tour-head">' +
        '<span class="tour-step">Step ' + (idx + 1) + ' of ' + STEPS.length + '</span>' +
        '<button type="button" class="tour-close" aria-label="Skip tour">Skip</button>' +
      '</div>' +
      '<h3 class="tour-title"></h3>' +
      '<p class="tour-body"></p>' +
      '<div class="tour-actions">' +
        (isFirst ? '<span></span>' : '<button type="button" class="btn tour-back">Back</button>') +
        '<button type="button" class="btn primary tour-next">' + (isLast ? 'Finish' : 'Next →') + '</button>' +
      '</div>';

    cardEl.querySelector(".tour-title").textContent = step.title;
    cardEl.querySelector(".tour-body").textContent = step.body;
    cardEl.querySelector(".tour-close").onclick = function () { endTour(true); };
    const backBtn = cardEl.querySelector(".tour-back");
    if (backBtn) backBtn.onclick = function () { goToStep(idx - 1); };
    cardEl.querySelector(".tour-next").onclick = function () {
      if (isLast) endTour(true);
      else goToStep(idx + 1);
    };
  }

  window.startIronLedgerTour = function () {
    localStorage.removeItem(doneKey);
    goToStep(0);
  };

  // Start or resume tour
  const params = new URLSearchParams(window.location.search);
  if (params.get("tour") === "1") {
    localStorage.removeItem(doneKey);
    localStorage.setItem(stepKey, "0");
    goToStep(0);
    return;
  }

  const saved = localStorage.getItem(stepKey);
  if (saved !== null) {
    const idx = parseInt(saved, 10);
    if (!Number.isNaN(idx) && idx >= 0 && idx < STEPS.length) {
      if (window.location.pathname === STEPS[idx].path) {
        renderStep(idx);
      }
      return;
    }
    localStorage.removeItem(stepKey);
  }

  if (isLeader && !localStorage.getItem(doneKey) && window.location.pathname === "/inventory") {
    renderStep(0);
  }
})();
