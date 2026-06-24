(function () {
  var STORAGE_KEY = 'dhl-sidebar-collapsed';

  function isMobile() {
    return window.matchMedia('(max-width: 900px)').matches;
  }

  function setCollapsed(collapsed) {
    document.body.classList.toggle('sidebar-collapsed', collapsed && !isMobile());
    var btn = document.getElementById('sidebar-toggle');
    if (btn) {
      btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      btn.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
    }
  }

  function init() {
    var btn = document.getElementById('sidebar-toggle');
    if (!btn) return;

    var saved = localStorage.getItem(STORAGE_KEY) === '1';
    setCollapsed(saved);

    btn.addEventListener('click', function () {
      if (isMobile()) return;
      var next = !document.body.classList.contains('sidebar-collapsed');
      localStorage.setItem(STORAGE_KEY, next ? '1' : '0');
      setCollapsed(next);
    });

    window.addEventListener('resize', function () {
      var collapsed = localStorage.getItem(STORAGE_KEY) === '1';
      setCollapsed(collapsed);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
