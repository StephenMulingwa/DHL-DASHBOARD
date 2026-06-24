(function () {
  function summaryText(checkboxes) {
    const checked = Array.from(checkboxes).filter(function (cb) { return cb.checked; });
    if (!checked.length) return 'All';
    if (checked.length === 1) return checked[0].value;
    return checked.length + ' selected';
  }

  function closeAll(except) {
    document.querySelectorAll('.filter-multi').forEach(function (wrap) {
      if (wrap === except) return;
      var menu = wrap.querySelector('.filter-multi-menu');
      var btn = wrap.querySelector('.filter-multi-toggle');
      if (menu) menu.hidden = true;
      if (btn) btn.setAttribute('aria-expanded', 'false');
    });
  }

  document.querySelectorAll('.filter-multi').forEach(function (wrap) {
    var toggle = wrap.querySelector('.filter-multi-toggle');
    var menu = wrap.querySelector('.filter-multi-menu');
    var summary = wrap.querySelector('.filter-multi-summary');
    var boxes = wrap.querySelectorAll('input[type="checkbox"]');
    if (!toggle || !menu || !summary) return;

    function refreshSummary() {
      summary.textContent = summaryText(boxes);
    }

    refreshSummary();
    boxes.forEach(function (cb) {
      cb.addEventListener('change', refreshSummary);
    });

    toggle.addEventListener('click', function (e) {
      e.preventDefault();
      var open = menu.hidden;
      closeAll(open ? wrap : null);
      menu.hidden = !open;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  });

  document.addEventListener('click', function (e) {
    if (e.target.closest('.filter-multi')) return;
    closeAll(null);
  });
})();
