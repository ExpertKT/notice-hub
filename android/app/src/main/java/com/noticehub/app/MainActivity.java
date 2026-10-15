package com.noticehub.app;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.view.inputmethod.EditorInfo;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;
import android.view.View;

import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final String PREFS = "notice_hub";
    private static final String BASE = "base";
    private static final String TOKEN = "token";
    private static final String BASES = "bases";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private WebView webView;
    private EditText addressInput;
    private View manualForm;
    private Button manualToggle;
    private TextView statusView;
    private TextView codeView;
    private String pairCode, pairSecret, currentBase;
    private int retryCount;
    private boolean pairing;
    private boolean scanInProgress;
    /** 是否停在 client 页、client 页是否已经加载完 —— 回到前台要不要自动刷新看这两个。 */
    private boolean showingClient, clientLoaded;
    /** 本地地址全不通时区分「连不上」和「令牌失效」：后者重试多少次都没用，得重新配对。 */
    private boolean lastAuthFailed;
    /** worker 是单线程队列，连点刷新会把任务排成一长队，用户看到的就是「点了半天没反应」。 */
    private boolean refreshBusy;
    private final Runnable poller = () -> pollPair();

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        SharedPreferences p = prefs();
        currentBase = p.getString(BASE, BuildConfig.PAIR_SERVER);
        if (handleDeepLink(getIntent())) return;
        if (!p.getString(TOKEN, "").isEmpty()) showClient(); else showPairing();
    }

    @Override protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent); setIntent(intent);
        handleDeepLink(intent);
    }

    private void scanPairingCode() {
        if (scanInProgress) return;
        scanInProgress = true;
        try {
            IntentIntegrator scanner = new IntentIntegrator(this);
            scanner.setDesiredBarcodeFormats(java.util.Collections.singletonList("QR_CODE"));
            scanner.setPrompt("扫描电脑群务台「手机 App」页面里的二维码");
            scanner.setBeepEnabled(false);
            scanner.setOrientationLocked(false);
            scanner.initiateScan();
        } catch (android.content.ActivityNotFoundException error) {
            scanInProgress = false;
            showManualPairingMessage("手机没有可用的扫码器，请手动填写电脑地址。");
        } catch (SecurityException error) {
            scanInProgress = false;
            showManualPairingMessage("无法使用相机扫码，请在系统设置中允许相机权限，或手动填写电脑地址。");
        } catch (RuntimeException error) {
            scanInProgress = false;
            showManualPairingMessage("无法启动相机扫码，请检查相机权限，或手动填写电脑地址。");
        }
    }

    @Override protected void onActivityResult(int requestCode, int resultCode, android.content.Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        IntentResult result;
        try {
            result = IntentIntegrator.parseActivityResult(requestCode, resultCode, data);
        } catch (RuntimeException error) {
            scanInProgress = false;
            showManualPairingMessage("扫码结果读取失败，请重试或手动填写电脑地址。");
            return;
        }
        if (result == null) {
            if (scanInProgress) {
                scanInProgress = false;
                showManualPairingMessage("扫码器没有返回结果，请重试或手动填写电脑地址。");
            }
            return;
        }
        scanInProgress = false;
        String contents = result.getContents();
        if (contents == null) {
            showManualPairingMessage("扫码已取消或未完成。若相机权限被拒绝，请在系统设置中允许；也可手动填写电脑地址后用配对码连接。");
            return;
        }
        Uri scanned;
        try {
            scanned = Uri.parse(contents);
        } catch (RuntimeException error) {
            showManualPairingMessage("二维码内容无法读取，请重试或手动填写电脑地址。");
            return;
        }
        if (!"noticehub".equalsIgnoreCase(scanned.getScheme()) || !"connect".equalsIgnoreCase(scanned.getHost())) {
            showManualPairingMessage("这不是群务台的二维码，请扫描电脑端群务台「手机 App」页面里的二维码。");
            return;
        }
        if (!handleDeepLink(scanned)) {
            showManualPairingMessage("群务台二维码信息无效，请刷新电脑端二维码后重试，或手动填写地址。");
        }
    }

    private void showManualPairingMessage(String message) {
        if (manualForm != null) {
            manualForm.setVisibility(View.VISIBLE);
            if (manualToggle != null) manualToggle.setText("收起手动填写");
        }
        if (statusView != null) statusView.setText(message);
    }

    private boolean handleDeepLink(android.content.Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        return handleDeepLink(data);
    }

    private boolean handleDeepLink(Uri data) {
        if (data == null || !"noticehub".equalsIgnoreCase(data.getScheme()) || !"connect".equalsIgnoreCase(data.getHost())) return false;
        String base = data.getQueryParameter("base");
        String token = data.getQueryParameter("token");
        Uri parsed = base == null ? null : Uri.parse(base);
        if (parsed == null || parsed.getHost() == null || !("http".equalsIgnoreCase(parsed.getScheme()) || "https".equalsIgnoreCase(parsed.getScheme())) || token == null || token.isEmpty()) return false;
        currentBase = base.endsWith("/") ? base.substring(0, base.length() - 1) : base;
        prefs().edit().putString(BASE, currentBase).putString(TOKEN, token).apply();
        rememberCandidate(currentBase);
        showClient(); return true;
    }

    private SharedPreferences prefs() { return getSharedPreferences(PREFS, MODE_PRIVATE); }

    private void showPairing() {
        stopPairing();
        showingClient = false; clientLoaded = false;
        setContentView(com.noticehub.app.R.layout.activity_main);
        addressInput = findViewById(R.id.address);
        manualForm = findViewById(R.id.manual_form);
        manualToggle = findViewById(R.id.manual_toggle);
        statusView = findViewById(R.id.status);
        codeView = findViewById(R.id.pair_code);
        addressInput.setText(currentBase == null ? "" : currentBase);
        codeView.setText("");
        addressInput.setImeOptions(EditorInfo.IME_ACTION_GO);
        addressInput.setOnEditorActionListener((view, actionId, event) -> {
            boolean keyboardSubmit = actionId == EditorInfo.IME_ACTION_GO
                || actionId == EditorInfo.IME_ACTION_DONE
                || (event != null && event.getKeyCode() == KeyEvent.KEYCODE_ENTER
                    && event.getAction() == KeyEvent.ACTION_DOWN);
            if (!keyboardSubmit) return false;
            startPairing();
            return true;
        });
        manualToggle.setOnClickListener(v -> {
            boolean show = manualForm.getVisibility() != View.VISIBLE;
            manualForm.setVisibility(show ? View.VISIBLE : View.GONE);
            manualToggle.setText(show ? "收起手动填写" : "手动填写地址");
        });
        ((Button) findViewById(R.id.scan)).setOnClickListener(v -> scanPairingCode());
        ((Button) findViewById(R.id.open)).setOnClickListener(v -> startPairing());
    }

    private void startPairing() {
        String base = addressInput.getText().toString().trim();
        Uri uri = Uri.parse(base);
        if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme())) || uri.getHost() == null) {
            statusView.setText("地址应类似 http://电脑IP:8766 或 https://域名"); return;
        }
        currentBase = base.endsWith("/") ? base.substring(0, base.length() - 1) : base;
        prefs().edit().putString(BASE, currentBase).remove(TOKEN).apply();
        retryCount = 0; pairing = true; statusView.setText("正在连接电脑…");
        worker.execute(() -> {
            try {
                // 先把这台电脑自己能提供的所有候选地址存下来，配对成功后刷新时会挨个试，
                // 避免「电脑上有多个网卡、第一个地址手机连不上」时用户卡死没法自救。
                cacheCandidatesFromServer();
                JSONObject o = new JSONObject(request("POST", currentBase + "/api/pair/start", null));
                runOnUiThread(() -> {
                    if (o.optBoolean("ok")) {
                        pairCode = o.optString("code"); pairSecret = o.optString("secret");
                        codeView.setText(pairCode); statusView.setText("请在电脑上核对这 6 位数字并点“允许”\n正在等待电脑确认…");
                        handler.postDelayed(poller, 2000);
                    } else statusView.setText(o.optString("error", "电脑拒绝了配对请求"));
                });
            } catch (Exception e) { runOnUiThread(() -> statusView.setText("连不上电脑，请确认群务台正在运行。")); }
        });
    }

    /** 读 /api/pair/info 的 candidates，缓存备用；老服务端不报这个字段就静默跳过。 */
    private void cacheCandidatesFromServer() {
        try {
            JSONObject info = new JSONObject(request("GET", currentBase + "/api/pair/info", null));
            JSONArray candidates = info.optJSONArray("candidates");
            if (candidates == null || candidates.length() == 0) {
                // 老服务端只报 base，没有候选列表：把它当作唯一候选，行为跟以前一致。
                String only = normalize(info.optString("base"));
                if (only.isEmpty()) return;
                JSONArray single = new JSONArray();
                single.put(new JSONObject().put("url", only));
                candidates = single;
            }
            cacheCandidates(candidates);
        } catch (Exception ignored) {
            // 老服务端或网络抖动都走这里：不影响配对本身，只是没有候选可用。
        }
    }

    private void pollPair() {
        if (!pairing || pairCode == null) return;
        worker.execute(() -> {
            try {
                String u = currentBase + "/api/pair/status?code=" + Uri.encode(pairCode) + "&secret=" + Uri.encode(pairSecret);
                JSONObject o = new JSONObject(request("GET", u, null));
                String s = o.optString("status");
                runOnUiThread(() -> {
                    if ("approved".equals(s)) { pairing = false; prefs().edit().putString(TOKEN, o.optString("token")).apply(); Toast.makeText(this, "连接成功", Toast.LENGTH_SHORT).show(); showClient(); }
                    else if ("denied".equals(s)) { pairing = false; statusView.setText("电脑拒绝了配对，请重新点击连接电脑。"); }
                    else if ("expired".equals(s)) restartPairing();
                    else if (pairing) handler.postDelayed(poller, 2000);
                });
            } catch (Exception e) { if (pairing) runOnUiThread(() -> { if (e instanceof HttpFailure && ((HttpFailure)e).code == 404) restartPairing(); else handler.postDelayed(poller, 2000); }); }
        });
    }

    private void restartPairing() { if (++retryCount <= 2) startPairing(); else { pairing = false; statusView.setText("配对已过期，请重新点击连接电脑。"); } }

    private void showClient() {
        stopPairing();
        showingClient = true; clientLoaded = false;
        webView = new WebView(this);
        WebSettings s = webView.getSettings(); s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true); s.setBuiltInZoomControls(false); s.setDisplayZoomControls(false);
        webView.addJavascriptInterface(new Bridge(), "AndroidBridge");
        setContentView(webView);
        webView.setWebViewClient(new android.webkit.WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, android.webkit.WebResourceRequest request) {
                if (request == null || !request.isForMainFrame()) return false;
                return routeWebViewUrl(request.getUrl());
            }
            @SuppressWarnings("deprecation")
            @Override public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return routeWebViewUrl(Uri.parse(url));
            }
            @Override public void onPageFinished(WebView v, String url) { clientLoaded = true; refreshClient(false); }
            // 文档（docs/安卓App.md）承诺「网页加载失败会自动回到设置页，不会让你卡死在白屏里」，
            // 但此前只有 onPageFinished，外壳加载不出来时页面就真白着不动了。这里补上。
            @Override public void onReceivedError(WebView v, android.webkit.WebResourceRequest req, android.webkit.WebResourceError error) {
                if (req != null && req.isForMainFrame()) showPairing();
            }
            // 老签名（API 23 以下才会走到）没有主框架标志，只在失败地址就是外壳本体时才回退，
            // 否则一张图加载失败也会把用户踢出主界面。
            @Override public void onReceivedError(WebView v, int code, String description, String failingUrl) {
                if (failingUrl != null && failingUrl.startsWith("file:///android_asset/client.html")) showPairing();
            }
        });
        webView.loadUrl("file:///android_asset/client.html");
    }

    private boolean routeWebViewUrl(Uri uri) {
        if (uri == null) return true;
        Uri base = Uri.parse(currentBase == null ? "" : currentBase);
        String scheme = uri.getScheme();
        String baseScheme = base.getScheme();
        boolean sameHost = uri.getHost() != null && base.getHost() != null
            && uri.getHost().equalsIgnoreCase(base.getHost())
            && (("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme))
                && ("http".equalsIgnoreCase(baseScheme) || "https".equalsIgnoreCase(baseScheme)));
        if (sameHost) return false;
        try {
            startActivity(new android.content.Intent(android.content.Intent.ACTION_VIEW, uri));
        } catch (android.content.ActivityNotFoundException error) {
            Toast.makeText(this, "无法打开外部链接，请检查是否安装了浏览器。", Toast.LENGTH_LONG).show();
        } catch (SecurityException error) {
            Toast.makeText(this, "无法安全地打开此链接。", Toast.LENGTH_LONG).show();
        }
        return true;
    }

    /** 手机能用的地址不止一个：服务端把候选都报回来，这里按顺序试到第一个通的。
     *  老服务端只报 base 一个地址，读不到 bases 时就退回只用 currentBase，
     *  行为跟以前完全一样，不会把能用的旧部署连坏。 */
    private List<String> candidates() {
        List<String> list = new ArrayList<>();
        String saved = prefs().getString(BASES, "");
        for (String item : saved.split("\n")) {
            String url = normalize(item);
            if (!url.isEmpty() && !list.contains(url)) list.add(url);
        }
        String current = normalize(currentBase);
        if (!current.isEmpty() && !list.contains(current)) list.add(0, current);
        return list;
    }

    private void rememberCandidate(String url) {
        List<String> list = candidates();
        list.remove(url); list.add(0, url);
        prefs().edit().putString(BASES, String.join("\n", list)).apply();
    }

    private static String normalize(String base) {
        String text = base == null ? "" : base.trim();
        if (text.isEmpty()) return "";
        Uri uri = Uri.parse(text);
        if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme())) || uri.getHost() == null) return "";
        return text.endsWith("/") ? text.substring(0, text.length() - 1) : text;
    }

    /** 把服务端 /api/pair/info 报回来的候选地址存进本地，供 fetchSnapshot 依次重试。
     *  服务端给的顺序是有意义的（推荐地址在前），必须整批一次写入 —— 逐个 rememberCandidate
     *  会把每个地址都插到队首，顺序被反转，刷新时白等前几个连不上的地址超时。 */
    private void cacheCandidates(JSONArray candidates) {
        List<String> list = new ArrayList<>();
        String current = normalize(currentBase);
        if (!current.isEmpty()) list.add(current);
        if (candidates != null) {
            for (int i = 0; i < candidates.length(); i++) {
                JSONObject item = candidates.optJSONObject(i);
                String url = normalize(item == null ? null : item.optString("url"));
                if (!url.isEmpty() && !list.contains(url)) list.add(url);
            }
        }
        if (list.isEmpty()) return;
        prefs().edit().putString(BASES, String.join("\n", list)).apply();
    }

    private void fetchSnapshot() {
        worker.execute(() -> {
            try {
                fetchSnapshotBody();
            } finally {
                runOnUiThread(() -> { refreshBusy = false; });
            }
        });
    }

    /** 手动/自动刷新都走这里：同一时间只允许一次刷新在跑 —— worker 是单线程队列，
     *  连点会把整串任务排到后面，用户看到的就是「点了半天没反应」。
     *  refreshBusy 只在 UI 线程上读写（Bridge.refresh 先 runOnUiThread 回到 UI 线程）。 */
    private void refreshClient(boolean userInitiated) {
        if (refreshBusy) {
            if (userInitiated) Toast.makeText(this, "正在刷新，请稍候…", Toast.LENGTH_SHORT).show();
            return;
        }
        refreshBusy = true;
        fetchSnapshot();
    }

    private void fetchSnapshotBody() {
        lastAuthFailed = false;
        List<String> targets = candidates();
        if (targets.isEmpty()) {
            // 区分「没配过」和「配了但格式不对」：用户忘写 http:// 是最常见的输入错误，
            // 只说「没有可用地址」他会一直以为是自己网络的问题。
            String raw = prefs().getString(BASE, "");
            String message = (raw == null || raw.trim().isEmpty())
                ? "还没有配过电脑地址，请点「连接电脑」扫码或手填。"
                : "保存的地址「" + raw + "」格式不对，需要以 http:// 或 https:// 开头。请长按返回键重新填写。";
            runOnUiThread(() -> Toast.makeText(this, message, Toast.LENGTH_LONG).show());
            return;
        }
        String token = prefs().getString(TOKEN, "");
        String hit = tryTargets(targets, token, false);
        if (hit == null) {
            // 本地候选全不通。扫码进场的用户本地只有一个地址（扫码那一刻电脑报的那个网卡），
            // 电脑换了网络或那个网卡不在同一网段时，他怎么刷新都是这一个连不上的地址。
            //
            // 补救只有一个前提：电脑的**另一个**地址得先答话，才能把完整候选表拿回来。
            // 所以这里必须用「还没试过的地址」去问，不能用刚失败的那批 —— 用同一批问
            // 必然也全不通，extra 永远是空的，这个分支就等于没写。（此前正是这么写的。）
            List<String> probe = probeHosts(targets);
            List<String> extra = probe.isEmpty() ? new ArrayList<>() : fetchCandidatesFromFirst(probe);
            if (!extra.isEmpty()) hit = tryTargets(extra, token, true);
        }
        if (hit != null) return;
        // 从没成功连上过时本地没有快照。此前这里什么都不注入，页面就永远停在「加载中…」；
        // 现在补一个空的离线载荷，让页面显示「还没拿到任务 + 怎么办」而不是假加载。
        final boolean authFailed = lastAuthFailed;
        String cached = readSnapshot();
        if (cached != null) {
            try { JSONObject p = new JSONObject(cached); p.put("offline", true); p.put("stale", true); p.put("authFailed", authFailed); inject(p.toString()); } catch (Exception ignored) {}
        } else {
            try { JSONObject p = new JSONObject(); p.put("tasks", new JSONObject()); p.put("meta", new JSONObject()); p.put("offline", true); p.put("authFailed", authFailed); inject(p.toString()); } catch (Exception ignored) {}
        }
        runOnUiThread(() -> Toast.makeText(this, authFailed
            ? "这台手机已经不被电脑认可（配对已失效），请点页面上的「重新配对」"
            : (cached != null ? "连不上电脑，正在显示上次的数据" : "连不上电脑，请检查手机和电脑是否在同一个网络"), Toast.LENGTH_LONG).show());
    }

    /** 依次试这些地址，成功就固定下来并返回它；全不通返回 null。 */
    private String tryTargets(List<String> targets, String token, boolean announce) {
        for (String target : targets) {
            try {
                JSONObject payload = new JSONObject(); payload.put("tasks", new JSONObject(request("GET", target + "/api/tasks", token))); payload.put("meta", new JSONObject(request("GET", target + "/api/meta", token))); payload.put("fetchedAt", new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).format(new Date())); payload.put("offline", false);
                boolean moved = !target.equals(currentBase);
                currentBase = target;
                prefs().edit().putString(BASE, target).apply();
                rememberCandidate(target);
                saveSnapshot(payload.toString()); inject(payload.toString());
                if (moved && announce) runOnUiThread(() -> Toast.makeText(this, "已切到能用的地址：" + target, Toast.LENGTH_SHORT).show());
                return target;
            } catch (Exception failure) {
                // 这个地址不通，试下一个。401/403 说明地址是通的、只是这台手机没被认可。
                if (failure instanceof HttpFailure) {
                    int code = ((HttpFailure) failure).code;
                    if (code == 401 || code == 403) lastAuthFailed = true;
                }
            }
        }
        return null;
    }

    private static List<String> minus(List<String> all, List<String> tried) {
        List<String> rest = new ArrayList<>();
        for (String url : all) if (!tried.contains(url)) rest.add(url);
        return rest;
    }

    /** 本地地址全不通时，还能去哪儿问候选表？
     *
     *  本地那份是「扫码/手填时电脑报的地址」+「上次成功连上的地址」。这两个都可能全过期：
     *  用户换了 Wi-Fi、电脑换了网卡，两个都指向不存在的地方，此时唯一的出路是电脑的**另一个**
     *  地址。但本地根本没有那个地址 —— 死循环。
     *
     *  破解点：扫同网段的邻居。命中场景是「手机和电脑其实在同一个网段，只是本地存的那个地址
     *  属于另一个网段」（例如电脑有多个网卡，扫码时记的是热点地址，现在手机连的是家里 Wi-Fi）。
     *  /api/pair/info 免鉴权，问它不需要 token。
     *
     *  **这不是主修，只是兜底**：真正解决「推荐了一个手机到不了的地址」的是服务端 —— 它现在会把
     *  候选分类（真实局域网 / 虚拟网卡 / 手机热点段 / Tailscale）并把真实局域网排在前面推荐。
     *  如果本地存的地址属于一个手机根本不在的网段（比如记的是手机热点段，而手机现在连家里
     *  Wi-Fi），扫描同样扫不到，此时只能靠用户重扫码或手填。
     *
     *  限定：只扫私有网段，且只扫少量邻居（绝不朝公网地址发请求）；连不上就是连不上，不重试。 */
    private static List<String> probeHosts(List<String> tried) {
        List<String> out = new ArrayList<>();
        for (String url : tried) {
            Uri uri = Uri.parse(url);
            String host = uri.getHost();
            if (host == null) continue;
            String[] parts = host.split("\\.");
            if (parts.length != 4) continue;
            try {
                int a = Integer.parseInt(parts[0]), b = Integer.parseInt(parts[1]), c = Integer.parseInt(parts[2]);
                // 只扫常见家用/办公网段，避免对公网地址乱发请求。
                boolean lan = (a == 192 && b == 168) || a == 10 || (a == 172 && b >= 16 && b <= 31);
                if (!lan) continue;
                String scheme = uri.getScheme() == null ? "http" : uri.getScheme();
                int port = uri.getPort();
                String tail = port <= 0 ? "" : ":" + port;
                for (int last : new int[] { 1, 2, 100, 101, 102, 103, 104, 105, 106, 107 }) {
                    String candidate = scheme + "://" + a + "." + b + "." + c + "." + last + tail;
                    if (!tried.contains(candidate) && !out.contains(candidate)) out.add(candidate);
                }
            } catch (NumberFormatException ignored) {
                // 不是点分十进制 IPv4（域名等），跳过。
            }
        }
        return out;
    }

    /** 挨个问这些地址，谁先答话就用谁报回来的候选表；返回本次新增（本地还没有的）地址。
     *  全部问不到就返回空 —— 调用方据此走「显示上次数据」的兜底。 */
    private List<String> fetchCandidatesFromFirst(List<String> probes) {
        List<String> before = candidates();
        for (String target : probes) {
            try {
                JSONObject info = new JSONObject(request("GET", target + "/api/pair/info", null, 1500, 1500));
                JSONArray candidates = info.optJSONArray("candidates");
                if (candidates == null || candidates.length() == 0) continue;
                // 这次问到的地址本身也要留下 —— 它已经证明自己是通的。
                rememberCandidate(target);
                cacheCandidates(candidates);
                return minus(candidates(), before);
            } catch (Exception ignored) {
                // 这个地址问不到，问下一个。
            }
        }
        return new ArrayList<>();
    }

    private void inject(String json) { runOnUiThread(() -> { if (webView != null) webView.evaluateJavascript("window.NH_render(" + json + ");", null); }); }
    private void saveSnapshot(String s) throws Exception { try (FileOutputStream out = new FileOutputStream(new File(getFilesDir(), "snapshot.json"))) { out.write(s.getBytes(StandardCharsets.UTF_8)); } }
    private String readSnapshot() { try (FileInputStream in = new FileInputStream(new File(getFilesDir(), "snapshot.json")); BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) { StringBuilder b = new StringBuilder(); String x; while ((x = r.readLine()) != null) b.append(x); return b.toString(); } catch (Exception e) { return null; } }

    private static final class HttpFailure extends Exception { final int code; HttpFailure(int code) { this.code = code; } }
    private String request(String method, String url, String token) throws Exception { return request(method, url, token, 15000, 15000); }
    /** 探测邻居地址时用短超时：那是「猜」出来的地址，一个不理会的网段会白等满 15 秒，
     *  十个邻居就是两分半，用户只会看到刷新没反应。 */
    private String request(String method, String url, String token, int connectMs, int readMs) throws Exception { HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection(); c.setRequestMethod(method); c.setConnectTimeout(connectMs); c.setReadTimeout(readMs); if (token != null) c.setRequestProperty("X-Token", token); if ("POST".equals(method)) { c.setDoOutput(true); c.setRequestProperty("Content-Type", "application/json; charset=utf-8"); c.getOutputStream().write("{}".getBytes(StandardCharsets.UTF_8)); } int code = c.getResponseCode(); BufferedReader r = new BufferedReader(new InputStreamReader(code >= 400 ? c.getErrorStream() : c.getInputStream(), StandardCharsets.UTF_8)); StringBuilder b = new StringBuilder(); String x; while ((x = r.readLine()) != null) b.append(x); if (code >= 400) throw new HttpFailure(code); return b.toString(); }

    @Override protected void onResume() { super.onResume(); if (pairing) handler.postDelayed(poller, 2000); else if (showingClient && clientLoaded) refreshClient(false); }
    @Override protected void onPause() { stopPairing(); super.onPause(); }
    private void stopPairing() { pairing = false; handler.removeCallbacks(poller); }
    @Override public void onBackPressed() { if (webView != null && webView.canGoBack()) webView.goBack(); else super.onBackPressed(); }
    @Override public boolean onKeyLongPress(int keyCode, KeyEvent event) { if (keyCode == KeyEvent.KEYCODE_BACK) { currentBase = prefs().getString(BASE, BuildConfig.PAIR_SERVER); prefs().edit().remove(TOKEN).apply(); showPairing(); return true; } return super.onKeyLongPress(keyCode, event); }
    @Override protected void onDestroy() { stopPairing(); worker.shutdownNow(); if (webView != null) webView.destroy(); super.onDestroy(); }

    public final class Bridge {
        @JavascriptInterface public void refresh() { runOnUiThread(() -> refreshClient(true)); }
        @JavascriptInterface public void reconnect() { runOnUiThread(() -> { prefs().edit().remove(TOKEN).apply(); currentBase = prefs().getString(BASE, BuildConfig.PAIR_SERVER); showPairing(); }); }
        @JavascriptInterface public String getBase() { return currentBase == null ? "" : currentBase; }
    }
}
