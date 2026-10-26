// 手机 App 配对 / 公网入口（Tailscale Funnel）/ 防火墙一键放行。
// 原样从 page.html 的内联脚本搬出，逻辑一字未改；依赖页面全局 api()、safeUrl()、
// downloadFirewallScript()、token 与页面 DOM（#pair-*、#pair-tunnel-*）。
// 这里只声明变量和函数，加载时不执行任何代码。
var pairTimer = null;
// 公网入口（Tailscale Funnel）是几分钟的长流程：页面只 POST 一次 start，随后每 2 秒轮询
// /api/tunnel/status，最长等 12 分钟。全程必须能点「关闭/取消」停下轮询并退出。
var TUNNEL_POLL_MS = 2000;
var TUNNEL_MAX_POLL_MS = 12 * 60 * 1000;
var tunnelTimer = null;
var tunnelStartedAt = 0;
var tunnelState = 'idle';
var tunnelUrl = '';
function stopTunnelPolling() {
  if (tunnelTimer !== null) { window.clearTimeout(tunnelTimer); tunnelTimer = null; }
}
// 防火墙面板：allowed=true 才收起；checked=false 只是「非提权读不到规则清单」，不是「没放行」，
// 这时也必须露出「一键放行」入口（点它会在一次管理员授权里顺便查出结果）。
function renderFirewallPanel(firewall) {
  firewall = firewall || {};
  var box = document.getElementById('pair-firewall');
  var text = document.getElementById('pair-firewall-text');
  var command = document.getElementById('pair-firewall-cmd');
  var allow = document.getElementById('pair-firewall-allow');
  if (!box || !text) return;
  if (allow) allow.disabled = false;
  box.dataset.port = String(firewall.port || '');// 下载放行脚本用：端口必须来自 /api/pair/info，不写死
  var port = firewall.port ? String(firewall.port) : '这个端口';
  box.hidden = !!firewall.allowed;
  if (firewall.allowed) return;
  text.textContent = firewall.checked
    ? '本机防火墙还没放行端口 ' + port + '：手机用局域网地址连不上，常见原因就是没放行这个端口。点下面「一键放行防火墙」，会弹一次管理员权限。'
    : '需要管理员才能确认是否已放行：非提权进程读不到本机防火墙规则，这里查不出结果不等于没放行。点下面「一键放行防火墙」，会在一次管理员授权里查出结果并放行。';
  command.textContent = firewall.command || '';
}
function renderTunnelStatus(snapshot) {
  var box = document.getElementById('pair-tunnel');
  if (!box) return;
  snapshot = snapshot || {};
  var state = String(snapshot.state || '');
  if (['idle', 'installing', 'waiting_login', 'enabling', 'ready', 'error'].indexOf(state) < 0) state = 'idle';
  tunnelState = state;
  tunnelUrl = safeUrl(snapshot.url || '');
  var busy = state === 'installing' || state === 'waiting_login' || state === 'enabling';
  box.hidden = false;
  box.dataset.state = state;
  var text = document.getElementById('pair-tunnel-text');
  text.textContent = String(snapshot.message || ({
    idle: '公网入口还没开通。',
    installing: '正在下载/安装 Tailscale…会弹一次管理员权限，请点「是」。',
    waiting_login: '请在浏览器里登录 Tailscale，登录完会自动继续。',
    enabling: '正在开启公网入口…',
    ready: '公网入口已开通，手机不连 Wi-Fi 也能用。',
    error: '公网入口开通失败。'
  })[state] || '');
  text.dataset.busy = busy ? 'true' : '';
  var auth = document.getElementById('pair-tunnel-auth');
  var authUrl = safeUrl(snapshot.auth_url || '');
  auth.hidden = !(state === 'waiting_login' && authUrl);
  if (authUrl) auth.href = authUrl; else auth.removeAttribute('href');
  var urlNode = document.getElementById('pair-tunnel-url');
  var showUrl = state === 'ready' && !!tunnelUrl;
  urlNode.hidden = !showUrl;
  urlNode.textContent = showUrl ? tunnelUrl : '';
  document.getElementById('pair-tunnel-copy').hidden = !showUrl;
  document.getElementById('pair-tunnel-retry').hidden = !(state === 'error' && snapshot.retryable !== false);
  document.getElementById('pair-tunnel-cancel').textContent = busy || state === 'idle' ? '取消' : '关闭';
  var enable = document.getElementById('pair-enable');
  if (enable) {
    enable.disabled = busy || state === 'ready';
    enable.textContent = state === 'ready' ? '公网入口已开通' : (busy ? '正在开通…' : '开通公网入口');
  }
}
function scheduleTunnelPoll() {
  stopTunnelPolling();
  if (tunnelState === 'ready' || tunnelState === 'error') return;
  if (Date.now() - tunnelStartedAt >= TUNNEL_MAX_POLL_MS) {
    renderTunnelStatus({state: 'error', message: '等了 12 分钟还没开通成功。可以点「重试」，或先关掉这里稍后再来。', retryable: true});
    return;
  }
  tunnelTimer = window.setTimeout(pollTunnelOnce, TUNNEL_POLL_MS);
}
function pollTunnelOnce() {
  tunnelTimer = null;
  api('/api/tunnel/status', {cache: 'no-store'}).then(function (snapshot) {
    renderTunnelStatus(snapshot);
    if (tunnelState === 'ready') {
      setPairStatus('公网入口已开通，手机用移动网络也能连。', 'ok');
      pollPairInfo();
    }
    scheduleTunnelPoll();
  }).catch(function () {
    // 单次读取失败不结束：下一轮再试；到 12 分钟才认输，给「重试」。
    if (Date.now() - tunnelStartedAt < TUNNEL_MAX_POLL_MS) {
      tunnelTimer = window.setTimeout(pollTunnelOnce, TUNNEL_POLL_MS * 2);
    } else {
      renderTunnelStatus({state: 'error', message: '读取开通进度失败，请刷新页面后重试。', retryable: true});
    }
  });
}
function startTunnel() {
  stopTunnelPolling();
  tunnelStartedAt = Date.now();
  renderTunnelStatus({state: 'installing', message: '正在请求开通公网入口…'});
  setPairStatus('正在开通公网入口，下面会显示进度，可以随时关掉。', '');
  api('/api/tunnel/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: true})}).then(function (snapshot) {
    renderTunnelStatus(snapshot);
    scheduleTunnelPoll();
  }).catch(function (error) {
    renderTunnelStatus({state: 'error', message: (error && error.message) || '开通失败，请重试。', retryable: true});
    setPairStatus((error && error.message) || '开通失败，请重试。', 'error');
  });
}
function closeTunnelPanel() {
  stopTunnelPolling();
  var box = document.getElementById('pair-tunnel');
  if (box) box.hidden = true;
  var enable = document.getElementById('pair-enable');
  if (enable) { enable.disabled = tunnelState === 'ready'; enable.textContent = tunnelState === 'ready' ? '公网入口已开通' : '开通公网入口'; }
}
function pairRow(item) {
  var row = el('div', 'preference');
  var text = el('span', '');
  text.appendChild(el('strong', '', item.device || '未知设备'));
  text.appendChild(el('small', '', '配对码 ' + item.code + ' · 已等待 ' + item.waiting_seconds + ' 秒'));
  row.appendChild(text);
  var allow = el('button', 'btn', '允许');
  allow.type = 'button';
  allow.addEventListener('click', function () { decidePair(item.code, true, allow); });
  var deny = el('button', 'btn', '拒绝');
  deny.type = 'button';
  deny.addEventListener('click', function () { decidePair(item.code, false, deny); });
  row.appendChild(allow);
  row.appendChild(deny);
  return row;
}
function setPairState(text, state) {
  var pill = document.getElementById('pair-state');
  if (!pill) return;
  pill.textContent = text;
  pill.dataset.state = state || '';
}
function setPairStatus(text, state) {
  var node = document.getElementById('pair-result');
  if (!node) return;
  node.textContent = text || '';
  node.dataset.state = state || '';
}
function decidePair(code, approved, button) {
  button.disabled = true;
  api('/api/pair/' + (approved ? 'approve' : 'deny'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code: code})}).then(function () {
    setPairStatus(approved ? '已允许这台手机，它马上就会连上。' : '已拒绝，手机上会提示重新配对。', approved ? 'ok' : '');
    refreshPair();
  }).catch(function (error) {
    setPairStatus((error && error.message) || '操作失败，请重试。', 'error');
    button.disabled = false;
  });
}
function refreshPair() {
  if (!document.getElementById('pair-pending')) return;
  api('/api/pair/pending', {cache: 'no-store'}).then(function (data) {
    var list = document.getElementById('pair-pending');
    if (!list) return;
    list.textContent = '';
    var items = (data && data.pending) || [];
    if (!items.length) list.appendChild(el('div', 'preference', '现在没有手机在等待配对，手机上打开 App 就会出现。'));
    items.forEach(function (item) { list.appendChild(pairRow(item)); });
    setPairState(items.length ? items.length + ' 台手机等待确认' : '没有等待中的手机', items.length ? 'warn' : 'ok');
  }).catch(function () {
    setPairState('读取失败', 'error');
  });
}
function pairCard() {
  var card = el('section', 'surface hosting-settings');
  card.setAttribute('aria-labelledby', 'pair-title');
  card.innerHTML = '<div class="section-head"><h2 id="pair-title" class="section-title">手机 App</h2><span id="pair-state" class="count-pill">读取中…</span></div>'
    + '<p class="setting-note">最省事：用手机相机扫下面的二维码，手机会直接打开「群务台」App 并自动连好，地址和令牌都不用输。</p>'
    + '<img id="pair-qr" class="sync-qr" alt="手机配对二维码" hidden>'
    + '<p id="pair-qr-msg" class="sync-status" role="status" aria-live="polite"></p>'
    + '<p class="setting-note">扫不动就手动配对：手机上打开 App，屏幕上会出现 6 位数字，在下面点「允许」也一样。以后想换一台电脑：手机上长按返回键，或点手机页面上的「重新配对」。</p>'
    + '<ol id="pair-steps" class="pair-steps"></ol>'
    + '<label for="pair-candidate">连接方式与地址</label>'
    + '<select id="pair-candidate" class="sync-url"></select>'
    + '<input id="pair-url" class="sync-url" type="text" readonly value="" aria-label="当前配对地址">'
    + '<div id="pair-firewall" class="setting-note" hidden><span id="pair-firewall-text" data-state="warn"></span><div class="firewall-actions"><button id="pair-firewall-allow" class="btn primary" type="button">一键放行防火墙</button></div><span id="pair-firewall-result" class="setting-note" role="status" aria-live="polite"></span><details id="pair-firewall-manual" class="firewall-manual"><summary>手动办法</summary><p class="setting-note">一键放行没成功（例如管理员权限被拒）时，可以用「以管理员身份运行」的 PowerShell 执行下面这条命令，或者下载脚本双击运行。</p><code id="pair-firewall-cmd" class="sync-url"></code><div class="firewall-actions"><button id="pair-firewall-copy" class="btn" type="button">复制这条命令</button><button id="pair-firewall-script" class="btn" type="button">下载放行脚本（双击运行）</button></div><span id="pair-firewall-script-hint" class="setting-note" role="status" aria-live="polite"></span></details></div>'
    + '<div class="hosting-actions"><button id="pair-copy" class="btn" type="button">复制地址</button><button id="pair-enable" class="btn" type="button">开通公网入口</button><button id="pair-refresh" class="btn" type="button">刷新</button></div>'
    + '<p id="pair-hint" class="setting-note"></p>'
    + '<div id="pair-tunnel" class="pair-tunnel" hidden><span id="pair-tunnel-text" class="pair-tunnel-text" role="status" aria-live="polite"></span><code id="pair-tunnel-url" class="pair-tunnel-url" hidden></code><div class="pair-tunnel-actions"><a id="pair-tunnel-auth" class="btn" target="_blank" rel="noopener noreferrer" hidden>打开登录页</a><button id="pair-tunnel-copy" class="btn primary" type="button" hidden>复制公网地址</button><button id="pair-tunnel-retry" class="btn primary" type="button" hidden>重试</button><button id="pair-tunnel-cancel" class="btn ghost" type="button">关闭</button></div></div>'
    + '<div id="pair-pending" class="preference-list"></div>'
    + '<p id="pair-result" class="sync-status" role="status" aria-live="polite"></p>';
  var qr = card.querySelector('#pair-qr');
  var qrMessage = card.querySelector('#pair-qr-msg');
  qr.onerror = function () {
    qr.hidden = true;
    qrMessage.dataset.state = 'error';
    qrMessage.textContent = '二维码加载失败，请检查访问令牌和手机可访问地址后刷新。';
  };
  return card;
}
function pollPairInfo() {
  var input = document.getElementById('pair-url');
  if (!input) return;
  api('/api/pair/info', {cache: 'no-store'}).then(function (data) {
    data = data || {};
    var candidates = (Array.isArray(data.candidates) ? data.candidates : []).filter(function (item) {
      return item && item.url;
    });
    var recommended = candidates.find(function (item) { return item.recommended; });
    var pairUrl = (recommended && recommended.url) || data.base || '';
    var candidateSelect = document.getElementById('pair-candidate');
    candidateSelect.textContent = '';
    candidates.forEach(function (item) {
      var option = document.createElement('option');
      option.value = item.url;
      var label = item.kind === 'lan' ? '局域网（同一 Wi-Fi）' :
        (item.url.indexOf('https://') === 0 ? '公网 Funnel' :
          (item.kind === 'tailscale' ? 'Tailscale VPN' :
            (item.kind === 'cellular' ? '热点网卡' :
              (item.kind === 'virtual' ? '虚拟网卡' : '其他地址'))));
      option.textContent = label + ' · ' + item.url;
      candidateSelect.appendChild(option);
    });
    candidateSelect.hidden = !candidates.length;
    if (candidates.some(function (item) { return item.url === pairUrl; })) candidateSelect.value = pairUrl;
    input.value = pairUrl;
    var steps = document.getElementById('pair-steps');
    steps.textContent = '';
    (data.steps || []).forEach(function (step) { steps.appendChild(el('li', '', step)); });
    var hint = document.getElementById('pair-hint');
    var qr = document.getElementById('pair-qr');
    var qrMessage = document.getElementById('pair-qr-msg');
    if (data.qr_error) {
      qr.removeAttribute('src');
      qr.hidden = true;
      qrMessage.dataset.state = 'error';
      qrMessage.textContent = data.qr_error;
    } else if (pairUrl) {
      qrMessage.dataset.state = '';
      qr.src = '/api/app/qr.png?base=' + encodeURIComponent(pairUrl) + '&token=' + encodeURIComponent(token);
      qr.hidden = false;
      qrMessage.textContent = '扫码后手机就连上了；不想扫码也可以照着下面三步做。';
    } else {
      qr.removeAttribute('src');
      qr.hidden = true;
      qrMessage.dataset.state = 'error';
      qrMessage.textContent = '没有检测到可供手机访问的地址。';
    }
    candidateSelect.onchange = function () {
      input.value = candidateSelect.value;
      if (data.qr_error || !candidateSelect.value) return;
      qr.src = '/api/app/qr.png?base=' + encodeURIComponent(candidateSelect.value) + '&token=' + encodeURIComponent(token);
      qr.hidden = false;
      qrMessage.dataset.state = '';
      qrMessage.textContent = '扫码后手机就连上了；不想扫码也可以照着下面三步做。';
    };
    if (data.public_enabled) hint.textContent = '默认选同一 Wi-Fi 下的局域网地址；异地时可在上方选择 Tailscale VPN 或公网 Funnel。公网 Funnel 会让服务可从公网访问。';
    else if (data.no_public_hint) hint.textContent = data.no_public_hint;
    else hint.textContent = '同一 Wi-Fi 下用局域网地址。异地请让电脑和手机加入同一 Tailscale VPN，或安装 Tailscale 后开通公网 Funnel。';
    renderFirewallPanel(data.firewall);
    var enable = document.getElementById('pair-enable');
    // 新契约会自己下载/安装 Tailscale，所以没装也不该把按钮锁死；只有已开通才禁用。
    enable.disabled = !!data.public_enabled;
    enable.textContent = data.public_enabled ? '公网入口已开通' : '开通公网入口';
  }).catch(function (error) {
    var qr = document.getElementById('pair-qr');
    var qrMessage = document.getElementById('pair-qr-msg');
    if (qr) qr.hidden = true;
    if (qrMessage) {
      qrMessage.dataset.state = 'error';
      qrMessage.textContent = (error && error.message) || '无法读取二维码配对信息，请刷新页面后重试。';
    }
    setPairStatus((error && error.message) || '读取失败', 'error');
  });
}
function setupPairCard() {
  if (!document.getElementById('pair-url')) return;
  document.getElementById('pair-copy').addEventListener('click', function () {
    var input = document.getElementById('pair-url');
    var text = input.value || '';
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { setPairStatus('地址已复制，发到手机上即可。', 'ok'); }, function () { input.select(); });
    } else {
      input.select();
    }
  });
  // 开通公网是后台长流程：点一次就只起任务，进度由 renderTunnelStatus 轮询显示。
  document.getElementById('pair-enable').addEventListener('click', function () {
    startTunnel();
  });
  var tunnelCopy = document.getElementById('pair-tunnel-copy');
  if (tunnelCopy) tunnelCopy.addEventListener('click', function () {
    if (!tunnelUrl) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(tunnelUrl).then(function () { setPairStatus('公网地址已复制，手机不连 Wi-Fi 也能用。', 'ok'); }, function () {});
    }
  });
  var tunnelRetry = document.getElementById('pair-tunnel-retry');
  if (tunnelRetry) tunnelRetry.addEventListener('click', function () { startTunnel(); });
  var tunnelCancel = document.getElementById('pair-tunnel-cancel');
  if (tunnelCancel) tunnelCancel.addEventListener('click', function () {
    closeTunnelPanel();
    setPairStatus('已停止查看开通进度；后台如果还在装，装完再点一次「开通公网入口」即可。', '');
  });
  var firewallAllow = document.getElementById('pair-firewall-allow');
  if (firewallAllow) firewallAllow.addEventListener('click', function () {
    var result = document.getElementById('pair-firewall-result');
    firewallAllow.disabled = true;
    if (result) { result.dataset.state = ''; result.textContent = '正在请托盘程序请求管理员权限…'; }
    api('/api/firewall/allow', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: true})}).then(function () {
      // 服务不再自己弹 UAC：请求写到 data/firewall.request，由托盘执行；结果靠轮询 status。
      var waited = 0;
      var tick = function () {
        waited += 2000;
        api('/api/firewall/status').then(function (s) {
          var state = (s && s.state) || 'unknown';
          if (state === 'granted') {
            if (result) { result.dataset.state = 'ok'; result.textContent = (s && s.message) || '已放行'; }
            firewallAllow.disabled = false;
            pollPairInfo();
            return;
          }
          if (state === 'failed' || state === 'unavailable') {
            if (result) {
              result.dataset.state = 'error';
              result.textContent = state === 'failed'
                ? ('失败：' + ((s && s.message) || '未知原因'))
                : ((s && s.message) || '请先启动托盘程序');
            }
            firewallAllow.disabled = false;
            pollPairInfo();
            return;
          }
          if (result) { result.dataset.state = ''; result.textContent = (s && s.message) || '正在请求管理员权限，请看托盘弹窗'; }
          if (waited >= 60000) {
            if (result) { result.dataset.state = 'error'; result.textContent = '60 秒还没结果：请确认托盘程序在运行，或用下面的手动办法。'; }
            firewallAllow.disabled = false;
            return;
          }
          window.setTimeout(tick, 2000);
        }).catch(function () {
          if (waited >= 60000) { firewallAllow.disabled = false; return; }
          window.setTimeout(tick, 2000);
        });
      };
      window.setTimeout(tick, 2000);
    }).catch(function (error) {
      if (result) { result.dataset.state = 'error'; result.textContent = (error && error.message) || '请求失败，请用下面的手动办法。'; }
      firewallAllow.disabled = false;
    });
  });
  document.getElementById('pair-refresh').addEventListener('click', function () { pollPairInfo(); refreshPair(); });
  var firewallScriptButton = document.getElementById('pair-firewall-script');
  if (firewallScriptButton) firewallScriptButton.addEventListener('click', function () {
    var box = document.getElementById('pair-firewall');
    downloadFirewallScript(box && box.dataset ? box.dataset.port : '');
  });
  document.getElementById('pair-firewall-copy').addEventListener('click', function () {
    var command = document.getElementById('pair-firewall-cmd').textContent || '';
    if (!command) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(command).then(function () { setPairStatus('命令已复制，粘贴到管理员 PowerShell 里运行即可。', 'ok'); }, function () { setPairStatus('复制失败，请手动选中上面的命令复制。', 'error'); });
    } else {
      setPairStatus('请手动选中上面的命令复制。', 'error');
    }
  });
  pollPairInfo();
  refreshPair();
  if (pairTimer) window.clearInterval(pairTimer);
  pairTimer = window.setInterval(refreshPair, 4000);
}
