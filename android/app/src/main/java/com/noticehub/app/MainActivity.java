package com.noticehub.app;

import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.view.KeyEvent;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import android.app.Activity;

public final class MainActivity extends Activity {
    private static final String PREFS = "notice_hub";
    private static final String ADDRESS = "address";
    private static final String TOKEN = "token";
    private WebView webView;
    private String appHost;

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        String address = prefs.getString(ADDRESS, "");
        if (address.isEmpty()) showSettings(address, prefs.getString(TOKEN, ""));
        else openWeb(address, prefs.getString(TOKEN, ""));
    }

    private void showSettings(String address, String token) {
        setContentView(R.layout.activity_main);
        EditText addressInput = findViewById(R.id.address);
        EditText tokenInput = findViewById(R.id.token);
        TextView status = findViewById(R.id.status);
        addressInput.setText(address);
        tokenInput.setText(token);
        Button open = findViewById(R.id.open);
        open.setOnClickListener(v -> {
            String base = addressInput.getText().toString().trim();
            String secret = tokenInput.getText().toString().trim();
            Uri uri = Uri.parse(base);
            if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme())) || uri.getHost() == null) {
                status.setText("地址应类似 http://电脑IP:8766 或 https://域名");
                return;
            }
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(ADDRESS, base).putString(TOKEN, secret).apply();
            openWeb(base, secret);
        });
    }

    private void openWeb(String base, String token) {
        Uri parsed = Uri.parse(base);
        appHost = parsed.getHost();
        String url = base;
        if (!token.isEmpty()) url = parsed.buildUpon().appendQueryParameter("token", token).build().toString();
        webView = new WebView(this);
        webView.setWebViewClient(new HubWebViewClient());
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setBuiltInZoomControls(false);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        setContentView(webView);
        webView.loadUrl(url);
    }

    @Override public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack(); else super.onBackPressed();
    }

    @Override public boolean onKeyLongPress(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
            showSettings(prefs.getString(ADDRESS, ""), prefs.getString(TOKEN, ""));
            return true;
        }
        return super.onKeyLongPress(keyCode, event);
    }

    private final class HubWebViewClient extends WebViewClient {
        @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            if (appHost != null && appHost.equalsIgnoreCase(uri.getHost())) return false;
            try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); } catch (Exception ignored) { }
            return true;
        }
        @Override public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError error) {
            if (req.isForMainFrame()) {
                showError();
                SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
                showSettings(prefs.getString(ADDRESS, ""), prefs.getString(TOKEN, ""));
            }
        }
        @Override public void onPageStarted(WebView view, String url, Bitmap icon) { super.onPageStarted(view, url, icon); }
        private void showError() {
            Toast.makeText(MainActivity.this, "连接不上：请确认电脑上的群务台正在运行，且手机与电脑在同一 Wi-Fi 或已开 Tailscale。", Toast.LENGTH_LONG).show();
        }
    }
}
