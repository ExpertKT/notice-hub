/* Installation uses the browser's own prompt; iOS exposes its Share menu instead. */
(function () {
  'use strict';
  var networkFetch = window.fetch;
  window.fetch = function (input, options) {
    var url = new URL(input instanceof Request ? input.url : input, location.href);
    var method = options && options.method || (input instanceof Request ? input.method : 'GET');
    if (url.origin === location.origin && (url.pathname === '/api' || url.pathname.indexOf('/api/') === 0) &&
        !/^(GET|HEAD)$/i.test(method) && (navigator.onLine === false || document.body.classList.contains('net-down'))) {
      return Promise.reject(new Error('当前只读：服务没连上，这次修改没有发送。连上后点「重试」。'));
    }
    return networkFetch.call(window, input, options);
  };
  var entry = document.getElementById('pwa-install');
  var dialog = document.getElementById('pwa-help');
  var deferred = null;
  var manifestURL = '';
  var standalone = window.matchMedia('(display-mode: standalone)');
  var ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var safari = /Safari/.test(navigator.userAgent) && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(navigator.userAgent);
  function installed() { return standalone.matches || navigator.standalone === true; }
  function update() { entry.hidden = installed() || !window.isSecureContext || !(deferred || ios && safari); }
  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();
    deferred = event;
    update();
  });
  window.addEventListener('appinstalled', function () { deferred = null; entry.hidden = true; });
  standalone.addEventListener('change', update);
  entry.addEventListener('click', async function () {
    if (!deferred) { dialog.showModal(); return; }
    var prompt = deferred;
    deferred = null;
    update();
    try { await prompt.prompt(); await prompt.userChoice; }
    catch (error) {
      document.getElementById('pwa-help-text').textContent = '安装窗口没打开。请刷新页面后重试，或在浏览器菜单中选「安装应用」。';
      dialog.showModal();
    }
  });
  update();

  // The server exchanges token for an HttpOnly cookie. Keep only the query still
  // present after that exchange; never put a token back after the page removed it.
  if (location.search && window.isSecureContext) {
    fetch('/manifest.webmanifest', { cache: 'no-store' }).then(function (response) {
      if (!response.ok) throw new Error('manifest');
      return response.json();
    }).then(function (manifest) {
      manifest.id = location.origin + '/';
      manifest.scope = location.origin + '/';
      manifest.start_url = location.origin + '/' + location.search;
      manifest.icons.forEach(function (icon) { icon.src = new URL(icon.src, location.origin).href; });
      manifestURL = URL.createObjectURL(new Blob([JSON.stringify(manifest)], { type: 'application/manifest+json' }));
      document.querySelector('link[rel="manifest"]').href = manifestURL;
    }).catch(function () { /* The bundled manifest remains available. */ });
  }
  window.addEventListener('pagehide', function () { if (manifestURL) URL.revokeObjectURL(manifestURL); });
})();
