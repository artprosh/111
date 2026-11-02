package ru.relaxgas.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.GeolocationPermissions;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

public final class MainActivity extends Activity {
    private static final int LOCATION_REQUEST = 42;
    private static final int FILE_REQUEST = 43;
    private static final String PREFS = "relaxgas";
    private static final String KEY_URL = "server_url";

    private WebView web;
    private GeolocationPermissions.Callback pendingGeo;
    private String pendingGeoOrigin;
    private ValueCallback<Uri[]> pendingFile;
    private String baseHost;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        String saved = getPreferences(MODE_PRIVATE).getString(KEY_URL, "");
        if (saved.isEmpty()) showServerForm();
        else openServer(saved);
    }

    private void showServerForm() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(36, 48, 36, 36);
        box.setGravity(Gravity.CENTER_VERTICAL);
        box.setBackgroundColor(0xff0f172a);

        TextView title = new TextView(this);
        title.setText("Relax Gas");
        title.setTextColor(0xffffffff);
        title.setTextSize(28);
        box.addView(title);

        TextView note = new TextView(this);
        note.setText("Введите HTTPS-адрес сервера приложения. APK подключится к его заказам.");
        note.setTextColor(0xffcbd5e1);
        note.setTextSize(16);
        LinearLayout.LayoutParams noteParams = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        noteParams.topMargin = 18;
        box.addView(note, noteParams);

        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint("https://app.example.com");
        input.setTextColor(0xffffffff);
        input.setHintTextColor(0xff94a3b8);
        input.setInputType( Uri.parse("https://example.com").getScheme().equals("https")
            ? android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI
            : android.text.InputType.TYPE_CLASS_TEXT);
        LinearLayout.LayoutParams inputParams = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        inputParams.topMargin = 20;
        box.addView(input, inputParams);

        Button save = new Button(this);
        save.setText("Подключиться");
        LinearLayout.LayoutParams saveParams = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        saveParams.topMargin = 18;
        box.addView(save, saveParams);
        save.setOnClickListener(v -> {
            String url = input.getText().toString().trim();
            if (!url.matches("^https://[^\\s/]+(:[0-9]{1,5})?(/.*)?$")) {
                Toast.makeText(this, "Укажите корректный адрес, начинающийся с https://", Toast.LENGTH_LONG).show();
                return;
            }
            getPreferences(MODE_PRIVATE).edit().putString(KEY_URL, url).apply();
            openServer(url);
        });
        setContentView(box);
    }

    private void openServer(String url) {
        Uri uri = Uri.parse(url);
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null) {
            getPreferences(MODE_PRIVATE).edit().remove(KEY_URL).apply();
            showServerForm();
            return;
        }
        baseHost = uri.getHost();
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setGeolocationEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri target = request.getUrl();
                if (target.getHost() != null && target.getHost().equalsIgnoreCase(baseHost)
                    && "https".equalsIgnoreCase(target.getScheme())) return false;
                return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
                if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
                    || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
                    callback.invoke(origin, true, false);
                } else {
                    pendingGeo = callback;
                    pendingGeoOrigin = origin;
                    requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION}, LOCATION_REQUEST);
                }
            }

            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (pendingFile != null) pendingFile.onReceiveValue(null);
                pendingFile = callback;
                Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("image/*");
                startActivityForResult(Intent.createChooser(intent, "Выберите изображение"), FILE_REQUEST);
                return true;
            }
        });
        setContentView(web);
        web.loadUrl(url);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == LOCATION_REQUEST && pendingGeo != null) {
            boolean granted = false;
            for (int result : results) if (result == PackageManager.PERMISSION_GRANTED) granted = true;
            pendingGeo.invoke(pendingGeoOrigin, granted, false);
            pendingGeo = null;
            pendingGeoOrigin = null;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_REQUEST && pendingFile != null) {
            Uri[] result = resultCode == RESULT_OK && data != null && data.getData() != null
                ? new Uri[]{data.getData()} : null;
            pendingFile.onReceiveValue(result);
            pendingFile = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
