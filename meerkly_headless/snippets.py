"""JavaScript injected into the page to implement the wait conditions.

The semantics here are the protocol's, not this worker's: api-gateway/spec
defines what `stable`, a selector wait, and a wait rule mean, and the desktop
and Android workers implement the same behavior. Changing anything here means
changing the spec first.

These run in the page's MAIN WORLD — invisible_playwright drives Firefox over
Juggler, which has no isolated-context guarantee. browser.exec_js's outer
deadline is what keeps a hostile page from stalling a job.
"""

# Probe every guard selector at once; resolve the index of the first visible
# one BY LIST ORDER, or -1 at the budget. A selector that throws is treated as
# not-found and polling continues.
PROBE_RULES = """
({ sels, budget }) =>
  new Promise((resolve) => {
    const vis = (el) =>
      !!(
        el &&
        (el.offsetWidth || el.offsetHeight || el.getClientRects().length) &&
        getComputedStyle(el).visibility !== 'hidden'
      );
    let done = false;
    const finish = (idx) => {
      if (done) return;
      done = true;
      try { mo.disconnect(); } catch (e) { /* ignore */ }
      clearInterval(iv);
      clearTimeout(to);
      resolve(idx);
    };
    const check = () => {
      for (let i = 0; i < sels.length; i++) {
        let el;
        try { el = document.querySelector(sels[i]); } catch (e) { el = null; }
        if (vis(el)) { finish(i); return; }
      }
    };
    const mo = new MutationObserver(check);
    const iv = setInterval(check, 200);
    const to = setTimeout(() => finish(-1), budget);
    try {
      mo.observe(document.documentElement || document, { childList: true, subtree: true, attributes: true });
    } catch (e) { /* ignore */ }
    check();
  })
"""

# Resolve false once the selector is visible, true on timeout. A selector that
# throws resolves true immediately — it can never become visible.
WAIT_SELECTOR_VISIBLE = """
({ sel, budget }) =>
  new Promise((resolve) => {
    const vis = (el) =>
      !!(
        el &&
        (el.offsetWidth || el.offsetHeight || el.getClientRects().length) &&
        getComputedStyle(el).visibility !== 'hidden'
      );
    let done = false;
    const finish = (timedOut) => {
      if (done) return;
      done = true;
      try { mo.disconnect(); } catch (e) { /* ignore */ }
      clearInterval(iv);
      clearTimeout(to);
      resolve(timedOut);
    };
    const check = () => {
      let el;
      try { el = document.querySelector(sel); } catch (e) { finish(true); return; }
      if (vis(el)) finish(false);
    };
    const mo = new MutationObserver(check);
    const iv = setInterval(check, 200);
    const to = setTimeout(() => finish(true), budget);
    try {
      mo.observe(document.documentElement || document, { childList: true, subtree: true, attributes: true });
    } catch (e) { /* ignore */ }
    check();
  })
"""

# Resolve once the DOM has been structurally quiet for QUIET ms, or at `cap`.
# childList + characterData + subtree, and deliberately NOT attributes: that
# exclusion is what stops CSS animations and class churn from blocking settle
# forever. Always resolves, so it never reports a timeout.
SETTLE_STABLE = """
(cap) =>
  new Promise((resolve) => {
    const QUIET = 500;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      try { mo.disconnect(); } catch (e) { /* ignore */ }
      clearTimeout(quiet);
      clearTimeout(capTimer);
      resolve();
    };
    const mo = new MutationObserver(() => {
      clearTimeout(quiet);
      quiet = setTimeout(finish, QUIET);
    });
    try {
      mo.observe(document.documentElement || document, { childList: true, subtree: true, characterData: true });
    } catch (e) { /* ignore */ }
    let quiet = setTimeout(finish, QUIET);
    const capTimer = setTimeout(finish, cap);
  })
"""

EXTRACT_HTML = """
(cap) => {
  const h = document.documentElement.outerHTML;
  return h.length > cap ? h.slice(0, cap) : h;
}
"""
