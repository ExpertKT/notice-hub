// 手机 App 配对 / 防火墙一键放行。公网入口全站只有一套，家安在本卡内，
// 由 settings.js 的双通道实现（#tunnel-card）驱动；本文件不再有独立的公网状态机或轮询。
// 依赖页面全局 api()、downloadFirewallScript()、token 与页面 DOM（#pair-*）。
// 这里只声明变量和函数，加载时不执行任何代码。
var pairTimer = null;
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
    : '要有管理员权限才能确认放行没放行：没提权时读不到本机防火墙规则，这里查不出结果不等于没放行。点下面「一键放行防火墙」，会在一次管理员授权里查出结果并放行。';
  command.textContent = firewall.command || '';
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
    // 有挂起请求才露出「清空待配对」：平时不占地方，被陌生人塞满时一眼能找到。
    var clear = document.getElementById('pair-pending-clear');
    if (clear) clear.hidden = !items.length;
    if (!items.length) list.appendChild(el('div', 'preference', '现在没有手机在等待配对，手机上打开 App 就会出现。'));
    items.forEach(function (item) { list.appendChild(pairRow(item)); });
    setPairState(items.length ? items.length + ' 台手机等待确认' : '没有等待中的手机', items.length ? 'warn' : 'ok');
  }).catch(function () {
    setPairState('读取失败', 'error');
  });
}
// 红队 S6：挂起的配对请求会把槽位占满，机主要能一键清掉（旁边没有手机时也能脱身）。
function clearPendingPairs(button) {
  if (!button) return;
  button.disabled = true;
  return api('/api/pair/pending/clear', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}).then(function (data) {
    var cleared = data && typeof data.cleared === 'number' ? data.cleared : 0;
    setPairStatus(cleared ? '已清空 ' + cleared + ' 条待配对请求' : '没有可清空的待配对请求', 'ok');
    refreshPair();
  }).catch(function (error) {
    var message = String((error && error.message) || '');
    // 后端没有这个路由就是版本问题：说清楚，别让人反复点。
    setPairStatus(/404|405|不支持/.test(message) ? '当前版本不支持，请升级' : ('清空失败：' + (message || '请重试')), 'error');
  }).then(function () { button.disabled = false; });
}
function pairCard() {
  var card = el('section', 'surface hosting-settings');
  card.dataset.settingAnchor = 'mobile';
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
    + '<div id="pair-firewall" class="setting-note" hidden><span id="pair-firewall-text" data-state="warn"></span><div class="firewall-actions"><button id="pair-firewall-allow" class="btn primary" type="button">一键放行防火墙</button></div><span id="pair-firewall-result" class="setting-note" role="status" aria-live="polite"></span><p id="pair-firewall-note" class="setting-note">放行只对「专用 / 域」网络生效，这是刻意的安全限制：Windows 把当前 WiFi 认成「公用网络」（咖啡厅、酒店、宿舍公共 WiFi）时会被拦，手机就连不上。需要时把网络改成「专用」，或点上面的「一键放行防火墙」手动放行全部网络。</p><details id="pair-firewall-manual" class="firewall-manual"><summary>手动办法</summary><p class="setting-note">一键放行没成功（例如管理员权限被拒）时，可以用「以管理员身份运行」的 PowerShell 执行下面这条命令，或者下载脚本双击运行。</p><code id="pair-firewall-cmd" class="sync-url"></code><div class="firewall-actions"><button id="pair-firewall-copy" class="btn" type="button">复制这条命令</button><button id="pair-firewall-script" class="btn" type="button">下载放行脚本（双击运行）</button></div><span id="pair-firewall-script-hint" class="setting-note" role="status" aria-live="polite"></span></details></div>'
    + '<div class="hosting-actions"><button id="pair-copy" class="btn" type="button">复制地址</button><button id="pair-refresh" class="btn" type="button">刷新</button></div>'
    + '<p id="pair-hint" class="setting-note"></p>'
     + '<div id="sync-public-block" class="sync-public-block"><h3 class="settings-group-title">不在同一个 Wi-Fi 下时：开通公网入口</h3><p class="setting-note">手机与电脑不在同一个 Wi-Fi 时，才需要开通公网入口；在同一个 Wi-Fi 下用局域网地址即可。当前地址与端口由群务台显示，开通后在下方复制或扫码。</p><div class="tunnel-compare"><div class="tunnel-compare-head"><span>对比项</span><span>Cloudflare 快速通道</span><span>Tailscale</span></div><div class="tunnel-compare-row"><span class="tunnel-compare-key">要不要注册账号</span><span><b class="tunnel-compare-tag">Cloudflare</b>不需要注册账号，也不需要域名。</span><span><b class="tunnel-compare-tag">Tailscale</b>需要登录 Tailscale 账号。</span></div><div class="tunnel-compare-row"><span class="tunnel-compare-key">地址重启后是否变化</span><span><b class="tunnel-compare-tag">Cloudflare</b>每次重启服务都会更换。</span><span><b class="tunnel-compare-tag">Tailscale</b>固定不变，适合长期订阅。</span></div><div class="tunnel-compare-row"><span class="tunnel-compare-key">要不要装客户端</span><span><b class="tunnel-compare-tag">Cloudflare</b>手机与电脑都不需要安装客户端。</span><span><b class="tunnel-compare-tag">Tailscale</b>电脑需安装 Tailscale 并登录；手机不需要。</span></div><div class="tunnel-compare-row"><span class="tunnel-compare-key">首次开通大约多久</span><span><b class="tunnel-compare-tag">Cloudflare</b>首次下载约 19 MB 通道程序，国内网络一般半分钟内。</span><span><b class="tunnel-compare-tag">Tailscale</b>需安装客户端、登录账号，可能还要在管理后台开启 Funnel，通常数分钟。</span></div><div class="tunnel-compare-row"><span class="tunnel-compare-key">哪个更快</span><span><b class="tunnel-compare-tag">Cloudflare</b>首次开通较快，地址会变。</span><span><b class="tunnel-compare-tag">Tailscale</b>首次步骤较多，但地址固定。</span></div><div class="tunnel-compare-row"><span class="tunnel-compare-key">失败怎么办</span><span><b class="tunnel-compare-tag">Cloudflare</b>检查网络后点「重试」，或切换另一条通道。</span><span><b class="tunnel-compare-tag">Tailscale</b>按提示登录账号、开启 Funnel 后点「重试」。</span></div></div><div class="settings-rows" id="tunnel-card" role="status" aria-live="polite">正在读取公网状态…</div></div>'
    + '<div id="pair-pending" class="preference-list"></div>'
    + '<div class="hosting-actions"><button id="pair-pending-clear" class="btn" type="button" hidden>清空待配对</button></div>'
    + '<p id="pair-result" class="sync-status" role="status" aria-live="polite"></p>';
  var clearPending = card.querySelector('#pair-pending-clear');
  if (clearPending) clearPending.onclick = function () { clearPendingPairs(clearPending); };
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
    hint.textContent = '手机与电脑在同一个 Wi-Fi 下时，用局域网地址即可；不在同一个 Wi-Fi 下时，请在下方「公网入口」中选择一条通道开通。';
    renderFirewallPanel(data.firewall);
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
