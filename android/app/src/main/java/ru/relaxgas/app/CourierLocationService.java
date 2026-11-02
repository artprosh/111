package ru.relaxgas.app;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Build;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Looper;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class CourierLocationService extends Service implements LocationListener {
    private static final String CHANNEL_ID = "courier_location";
    private static final int NOTIFICATION_ID = 8401;
    private static final String ACTION_STOP = "ru.relaxgas.app.STOP_COURIER_LOCATION";
    private static final String EXTRA_URL = "server_url";
    private static final String EXTRA_COOKIE = "session_cookie";

    private final ExecutorService sender = Executors.newSingleThreadExecutor();
    private LocationManager locations;
    private String apiUrl;
    private String sessionCookie;

    public static Intent startIntent(Context context, String serverUrl, String cookie) {
        Intent intent = new Intent(context, CourierLocationService.class);
        intent.putExtra(EXTRA_URL, serverUrl);
        intent.putExtra(EXTRA_COOKIE, cookie);
        return intent;
    }

    public static Intent stopIntent(Context context) {
        Intent intent = new Intent(context, CourierLocationService.class);
        intent.setAction(ACTION_STOP);
        return intent;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        locations = (LocationManager) getSystemService(LOCATION_SERVICE);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (intent != null) {
            apiUrl = intent.getStringExtra(EXTRA_URL);
            sessionCookie = intent.getStringExtra(EXTRA_COOKIE);
        }
        if (apiUrl == null || sessionCookie == null || sessionCookie.isEmpty()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        Notification notification = buildNotification();
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
        beginUpdates();
        return START_NOT_STICKY;
    }

    private void beginUpdates() {
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED
            && checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            stopSelf();
            return;
        }
        try {
            if (locations.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                locations.requestLocationUpdates(LocationManager.GPS_PROVIDER, 10000L, 15f, this, Looper.getMainLooper());
                Location last = locations.getLastKnownLocation(LocationManager.GPS_PROVIDER);
                if (last != null) onLocationChanged(last);
            } else {
                locations.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 15000L, 30f, this, Looper.getMainLooper());
                Location last = locations.getLastKnownLocation(LocationManager.NETWORK_PROVIDER);
                if (last != null) onLocationChanged(last);
            }
        } catch (SecurityException | IllegalArgumentException error) {
            stopSelf();
        }
    }

    @Override
    public void onLocationChanged(Location location) {
        if (location == null || apiUrl == null || sessionCookie == null) return;
        final String url = apiUrl;
        final String cookie = sessionCookie;
        final String json = "{\"lat\":" + location.getLatitude() + ",\"lon\":" + location.getLongitude() + "}";
        sender.execute(() -> postLocation(url, cookie, json));
    }

    private void postLocation(String base, String cookie, String json) {
        HttpURLConnection connection = null;
        try {
            String endpoint = base.endsWith("/") ? base + "api/courier/live" : base + "/api/courier/live";
            connection = (HttpURLConnection) new URL(endpoint).openConnection();
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(10000);
            connection.setReadTimeout(10000);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setRequestProperty("Cookie", cookie);
            byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream out = connection.getOutputStream()) { out.write(bytes); }
            connection.getResponseCode();
        } catch (Exception ignored) {
            // Network loss is expected during a delivery; the next point retries.
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private Notification buildNotification() {
        Intent stop = stopIntent(this);
        PendingIntent stopPending = PendingIntent.getService(
            this, 1, stop, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = new Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentTitle("Relax Gas — геопозиция включена")
            .setContentText("Клиент и администратор видят точку доставки. Нажмите «Остановить», чтобы выключить.")
            .setOngoing(true)
            .addAction(new Notification.Action.Builder(
                android.graphics.drawable.Icon.createWithResource(this, android.R.drawable.ic_menu_close_clear_cancel),
                "Остановить", stopPending).build());
        return builder.build();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "Геопозиция курьера", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("Показывает, когда Relax Gas передаёт геопозицию курьера.");
            ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(channel);
        }
    }

    @Override public void onProviderEnabled(String provider) { }
    @Override public void onProviderDisabled(String provider) { }
    @Override public void onStatusChanged(String provider, int status, Bundle extras) { }

    @Override
    public void onDestroy() {
        try { if (locations != null) locations.removeUpdates(this); } catch (SecurityException ignored) { }
        sender.shutdownNow();
        stopForeground(true);
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
