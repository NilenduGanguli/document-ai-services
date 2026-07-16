// Set the theme before first paint so there is no flash of the wrong theme.
//
// Lives as an external same-origin file (not an inline <script> in index.html) so the
// production Content-Security-Policy can use `script-src 'self'` without an `'unsafe-inline'`
// carve-out — see di/app.py::_csp_header.
(function () {
  try {
    var stored = localStorage.getItem('di.theme');
    var theme =
      stored === 'light' || stored === 'dark'
        ? stored
        : window.matchMedia('(prefers-color-scheme: dark)').matches
          ? 'dark'
          : 'light';
    document.documentElement.classList.add('theme-' + theme);
  } catch (e) {
    document.documentElement.classList.add('theme-light');
  }
})();
