package com.noticehub.app;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

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
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final String PREFS = "notice_hub";
    private static final String BASE = "base";
    private static final String TOKEN = "token";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private WebView webView;
    private EditText addressInput;
    private TextView statusView;
    private TextView codeView;
    private String pairCode, pairSecret, currentBase;
    private int retryCount;
    private boolean pairing;
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

    private boolean handleDeepLink(android.content.Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data == null || !"noticehub".equalsIgnoreCase(data.getScheme()) || !"connect".equalsIgnoreCase(data.getHost())) return false;
        String base = data.getQueryParameter("base");
        String token = data.getQueryParameter("token");
        Uri parsed = base == null ? null : Uri.parse(base);
        if (parsed == null || parsed.getHost() == null || !("http".equalsIgnoreCase(parsed.getScheme()) || "https".equalsIgnoreCase(parsed.getScheme())) || token == null || token.isEmpty()) return false;
        currentBase = base.endsWith("/") ? base.substring(0, base.length() - 1) : base;
        prefs().edit().putString(BASE, currentBase).putString(TOKEN, token).apply();
        showClient(); return true;
    }

    private SharedPreferences prefs() { return getSharedPreferences(PREFS, MODE_PRIVATE); }

    private void showPairing() {
        stopPairing();
        setContentView(com.noticehub.app.R.layout.activity_main);
        addressInput = findViewById(R.id.address);
        statusView = findViewById(R.id.status);
        codeView = findViewById(R.id.pair_code);
        addressInput.setText(currentBase == null ? "" : currentBase);
        codeView.setText("");
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
        webView = new WebView(this);
        WebSettings s = webView.getSettings(); s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true); s.setBuiltInZoomControls(false); s.setDisplayZoomControls(false);
        webView.addJavascriptInterface(new Bridge(), "AndroidBridge");
        setContentView(webView); webView.loadUrl("file:///android_asset/client.html");
        webView.setWebViewClient(new android.webkit.WebViewClient() { @Override public void onPageFinished(WebView v, String url) { fetchSnapshot(); } });
    }

    private void fetchSnapshot() {
        worker.execute(() -> {
            try {
                String token = prefs().getString(TOKEN, "");
                JSONObject payload = new JSONObject(); payload.put("tasks", new JSONObject(request("GET", currentBase + "/api/tasks", token))); payload.put("meta", new JSONObject(request("GET", currentBase + "/api/meta", token))); payload.put("fetchedAt", new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).format(new Date())); payload.put("offline", false);
                saveSnapshot(payload.toString()); inject(payload.toString());
            } catch (Exception e) { String cached = readSnapshot(); if (cached != null) { try { JSONObject p = new JSONObject(cached); p.put("offline", true); inject(p.toString()); } catch (Exception ignored) {} } runOnUiThread(() -> Toast.makeText(this, "连不上电脑，正在显示上次的数据", Toast.LENGTH_LONG).show()); }
        });
    }

    private void inject(String json) { runOnUiThread(() -> { if (webView != null) webView.evaluateJavascript("window.NH_render(" + json + ");", null); }); }
    private void saveSnapshot(String s) throws Exception { try (FileOutputStream out = new FileOutputStream(new File(getFilesDir(), "snapshot.json"))) { out.write(s.getBytes(StandardCharsets.UTF_8)); } }
    private String readSnapshot() { try (FileInputStream in = new FileInputStream(new File(getFilesDir(), "snapshot.json")); BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) { StringBuilder b = new StringBuilder(); String x; while ((x = r.readLine()) != null) b.append(x); return b.toString(); } catch (Exception e) { return null; } }

    private static final class HttpFailure extends Exception { final int code; HttpFailure(int code) { this.code = code; } }
    private String request(String method, String url, String token) throws Exception { HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection(); c.setRequestMethod(method); c.setConnectTimeout(15000); c.setReadTimeout(15000); if (token != null) c.setRequestProperty("X-Token", token); if ("POST".equals(method)) { c.setDoOutput(true); c.setRequestProperty("Content-Type", "application/json; charset=utf-8"); c.getOutputStream().write("{}".getBytes(StandardCharsets.UTF_8)); } int code = c.getResponseCode(); BufferedReader r = new BufferedReader(new InputStreamReader(code >= 400 ? c.getErrorStream() : c.getInputStream(), StandardCharsets.UTF_8)); StringBuilder b = new StringBuilder(); String x; while ((x = r.readLine()) != null) b.append(x); if (code >= 400) throw new HttpFailure(code); return b.toString(); }

    @Override protected void onResume() { super.onResume(); if (pairing) handler.postDelayed(poller, 2000); }
    @Override protected void onPause() { stopPairing(); super.onPause(); }
    private void stopPairing() { pairing = false; handler.removeCallbacks(poller); }
    @Override public void onBackPressed() { if (webView != null && webView.canGoBack()) webView.goBack(); else super.onBackPressed(); }
    @Override public boolean onKeyLongPress(int keyCode, KeyEvent event) { if (keyCode == KeyEvent.KEYCODE_BACK) { currentBase = prefs().getString(BASE, BuildConfig.PAIR_SERVER); prefs().edit().remove(TOKEN).apply(); showPairing(); return true; } return super.onKeyLongPress(keyCode, event); }
    @Override protected void onDestroy() { stopPairing(); worker.shutdownNow(); if (webView != null) webView.destroy(); super.onDestroy(); }

    public final class Bridge {
        @JavascriptInterface public void refresh() { fetchSnapshot(); }
        @JavascriptInterface public void reconnect() { runOnUiThread(() -> { prefs().edit().remove(TOKEN).apply(); currentBase = prefs().getString(BASE, BuildConfig.PAIR_SERVER); showPairing(); }); }
        @JavascriptInterface public String getBase() { return currentBase == null ? "" : currentBase; }
    }
}
