package com.timefitness.app;

import android.app.Activity;
import android.content.SharedPreferences;
import android.content.Intent;
import android.net.Uri;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.ValueCallback;
import org.json.JSONObject;

public class MainActivity extends Activity {
    private WebView web;
    private SharedPreferences prefs;
    private static final int EXPORT_FILE=101, IMPORT_FILE=102;
    private static final int MAX_BACKUP_BYTES=32*1024*1024;
    public class Bridge {
        @JavascriptInterface public String load() { return prefs.getString("records", ""); }
        @JavascriptInterface public boolean save(String json) {
            try {
                JSONObject obj = new JSONObject(json);
                if (obj.getInt("version") != 1 || !obj.has("days")) return false;
                return prefs.edit().putString("records", json).commit();
            } catch (Exception e) { return false; }
        }
        @JavascriptInterface public boolean exportBackup(String json) {
            try {
                if (json.getBytes(StandardCharsets.UTF_8).length>MAX_BACKUP_BYTES) return false;
                JSONObject obj=new JSONObject(json);
                if (!"time-fitness-backup".equals(obj.getString("format"))) return false;
                if (!prefs.edit().putString("pendingExport",json).commit()) return false;
                runOnUiThread(new Runnable(){@Override public void run(){
                    try {
                        Intent intent=new Intent(Intent.ACTION_CREATE_DOCUMENT);
                        intent.addCategory(Intent.CATEGORY_OPENABLE); intent.setType("application/json");
                        intent.putExtra(Intent.EXTRA_TITLE,"TimeFitness-backup-"+new SimpleDateFormat("yyyy-MM-dd",Locale.US).format(new Date())+".json");
                        startActivityForResult(intent,EXPORT_FILE);
                    } catch(Exception e){fileResult("error","파일 저장 화면을 열지 못했습니다.");}
                }});
                return true;
            } catch(Exception e){return false;}
        }
        @JavascriptInterface public void importBackup() {
            runOnUiThread(new Runnable(){@Override public void run(){
                try {
                    Intent intent=new Intent(Intent.ACTION_OPEN_DOCUMENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);intent.setType("*/*");
                    startActivityForResult(intent,IMPORT_FILE);
                }catch(Exception e){fileResult("error","파일 선택 화면을 열지 못했습니다.");}
            }});
        }
        @JavascriptInterface public synchronized String takeFileResult() {
            String value=prefs.getString("fileResult","");
            if (!value.isEmpty()) prefs.edit().remove("fileResult").commit();
            return value;
        }
    }
    private void fileResult(String kind,String content){
        try {JSONObject value=new JSONObject();value.put("kind",kind);value.put("content",content);
            prefs.edit().putString("fileResult",value.toString()).remove("pendingExport").commit();
        }catch(Exception ignored){}
    }
    @Override protected void onActivityResult(int request,int result,Intent data){
        super.onActivityResult(request,result,data);
        if(request!=EXPORT_FILE&&request!=IMPORT_FILE)return;
        if(result!=RESULT_OK||data==null||data.getData()==null){fileResult("cancel","");return;}
        final Uri uri=data.getData(); final int action=request;
        new Thread(new Runnable(){@Override public void run(){
            try {
                if(action==EXPORT_FILE){
                    String json=prefs.getString("pendingExport","");
                    if(json.isEmpty())throw new Exception("No pending backup");
                    try(OutputStream stream=getContentResolver().openOutputStream(uri,"wt")){
                        if(stream==null)throw new Exception("No output stream");
                        stream.write(json.getBytes(StandardCharsets.UTF_8));stream.flush();
                    }
                    fileResult("export","");
                }else{
                    try(InputStream stream=getContentResolver().openInputStream(uri);ByteArrayOutputStream buffer=new ByteArrayOutputStream()){
                        if(stream==null)throw new Exception("No input stream");
                        byte[] block=new byte[8192];int size;
                        while((size=stream.read(block))!=-1){if(buffer.size()+size>MAX_BACKUP_BYTES){fileResult("error","32MB 이하의 백업 파일을 선택하세요.");return;}buffer.write(block,0,size);}
                        fileResult("import",new String(buffer.toByteArray(),StandardCharsets.UTF_8));
                    }
                }
            }catch(Exception e){fileResult("error",action==EXPORT_FILE?"백업 파일을 저장하지 못했습니다.":"백업 파일을 읽지 못했습니다.");}
        }},"TimeFitnessFiles").start();
    }
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("time-fitness", MODE_PRIVATE);
        getWindow().setStatusBarColor(Color.rgb(48,50,56));
        getWindow().setNavigationBarColor(Color.rgb(48,50,56));
        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(48,50,56));
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setAllowFileAccessFromFileURLs(false);
        s.setAllowUniversalAccessFromFileURLs(false);
        s.setSupportZoom(false);
        s.setTextZoom(100);
        s.setMediaPlaybackRequiresUserGesture(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, String url) { return true; }
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { return true; }
        });
        web.addJavascriptInterface(new Bridge(), "Native");
        setContentView(web);
        web.loadUrl("file:///android_asset/index.html");
    }
    @Override public void onBackPressed() {
        web.evaluateJavascript("window.onNativeBack ? window.onNativeBack() : false", new ValueCallback<String>() {
            @Override public void onReceiveValue(String result) {
                if (!"true".equals(result)) finish();
            }
        });
    }
    @Override protected void onDestroy() {
        if (web != null) { web.removeJavascriptInterface("Native"); web.destroy(); }
        super.onDestroy();
    }
}
