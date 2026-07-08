(function () {
  function normalize(text) {
    return (text || '').toString().toLowerCase();
  }

  function cellValue(row, colIdx) {
    var cell = row.cells[colIdx];
    if (!cell) return '';
    return (cell.textContent || '').trim();
  }

  function compareValues(a, b, type) {
    if (type === 'num') {
      var na = parseFloat(String(a).replace(/,/g, ''));
      var nb = parseFloat(String(b).replace(/,/g, ''));
      if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
    }
    return normalize(a).localeCompare(normalize(b), undefined, { numeric: true, sensitivity: 'base' });
  }

  function initTable(root) {
    var table = root.querySelector('.report-data-table');
    if (!table || table.dataset.enhanced === '1') return;
    table.dataset.enhanced = '1';

    var tbody = table.querySelector('tbody');
    var headers = Array.from(table.querySelectorAll('thead th'));
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var search = root.querySelector('.table-search');
    var pageSizeSelect = root.querySelector('.table-page-size');
    var prev = root.querySelector('.table-prev');
    var next = root.querySelector('.table-next');
    var pageLabel = root.querySelector('.table-page-label');
    var count = root.querySelector('.table-count');
    var page = 1;
    var sortCol = null;
    var sortDir = 1;

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

    function applySort(list) {
      if (sortCol === null) return list;
      var type = headers[sortCol] && headers[sortCol].dataset.sortType === 'num' ? 'num' : 'text';
      return list.slice().sort(function (ra, rb) {
        var cmp = compareValues(cellValue(ra, sortCol), cellValue(rb, sortCol), type);
        return cmp * sortDir;
      });
    }

    function render() {
      var visible = applySort(filteredRows());
      var size = pageSize();
      var pages = Math.max(1, Math.ceil(visible.length / size));
      page = Math.min(Math.max(page, 1), pages);
      var start = (page - 1) * size;
      var end = start + size;

      rows.forEach(function (row) {
        tbody.appendChild(row);
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

    headers.forEach(function (th, idx) {
      th.classList.add('sortable-th');
      th.setAttribute('role', 'columnheader');
      th.setAttribute('tabindex', '0');
      if (idx === 0) th.dataset.sortType = 'num';
      th.addEventListener('click', function () {
        if (sortCol === idx) {
          sortDir = sortDir * -1;
        } else {
          sortCol = idx;
          sortDir = 1;
        }
        headers.forEach(function (h) {
          h.classList.remove('sort-asc', 'sort-desc');
        });
        th.classList.add(sortDir > 0 ? 'sort-asc' : 'sort-desc');
        page = 1;
        render();
      });
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          th.click();
        }
      });
    });

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

  function init(root) {
    (root || document).querySelectorAll('.report-table').forEach(initTable);
  }

  window.DashboardTables = { init: init };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();
