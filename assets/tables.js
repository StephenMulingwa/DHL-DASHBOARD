(function () {
  function normalize(text) {
    return (text || '').toString().toLowerCase();
  }

  function initTable(root) {
    var table = root.querySelector('.report-data-table');
    if (!table || table.dataset.enhanced === '1') return;
    table.dataset.enhanced = '1';

    var rows = Array.from(table.querySelectorAll('tbody tr'));
    var search = root.querySelector('.table-search');
    var pageSizeSelect = root.querySelector('.table-page-size');
    var prev = root.querySelector('.table-prev');
    var next = root.querySelector('.table-next');
    var pageLabel = root.querySelector('.table-page-label');
    var count = root.querySelector('.table-count');
    var page = 1;

    function pageSize() {
      var raw = pageSizeSelect ? parseInt(pageSizeSelect.value, 10) : parseInt(root.dataset.pageSize || '10', 10);
      return Number.isFinite(raw) && raw > 0 ? raw : 10;
    }

    function filteredRows() {
      var q = normalize(search ? search.value : '').trim();
      if (!q) return rows;
      return rows.filter(function (row) {
        return normalize(row.textContent).indexOf(q) !== -1;
      });
    }

    function render() {
      var visible = filteredRows();
      var size = pageSize();
      var pages = Math.max(1, Math.ceil(visible.length / size));
      page = Math.min(Math.max(page, 1), pages);
      var start = (page - 1) * size;
      var end = start + size;

      rows.forEach(function (row) {
        row.hidden = true;
      });
      visible.slice(start, end).forEach(function (row) {
        row.hidden = false;
      });

      if (pageLabel) pageLabel.textContent = 'Page ' + page + ' / ' + pages;
      if (prev) prev.disabled = page <= 1;
      if (next) next.disabled = page >= pages;
      if (count) {
        var shownEnd = visible.length ? Math.min(end, visible.length) : 0;
        var shownStart = visible.length ? start + 1 : 0;
        count.textContent = 'Showing ' + shownStart + '-' + shownEnd + ' of ' + visible.length + ' rows';
      }
    }

    if (search) {
      search.addEventListener('input', function () {
        page = 1;
        render();
      });
    }
    if (pageSizeSelect) {
      pageSizeSelect.addEventListener('change', function () {
        page = 1;
        render();
      });
    }
    if (prev) {
      prev.addEventListener('click', function () {
        page -= 1;
        render();
      });
    }
    if (next) {
      next.addEventListener('click', function () {
        page += 1;
        render();
      });
    }
    render();
  }

  function initAll() {
    document.querySelectorAll('.report-table').forEach(initTable);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();
