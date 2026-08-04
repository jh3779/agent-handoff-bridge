// Design System Docs — 공통 스크립트 (테마 토글 · 저장)
// styles.css 는 [data-ds-theme="dark"] 로 다크 토큰을 정의한다.
(function () {
  var KEY = 'ds-theme';
  var root = document.documentElement;
  try {
    if (localStorage.getItem(KEY) === 'dark') root.setAttribute('data-ds-theme', 'dark');
  } catch (e) {}
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-theme-toggle]');
    if (!b) return;
    var dark = root.getAttribute('data-ds-theme') === 'dark';
    root.setAttribute('data-ds-theme', dark ? '' : 'dark');
    try { localStorage.setItem(KEY, dark ? 'light' : 'dark'); } catch (e) {}
  });
})();
