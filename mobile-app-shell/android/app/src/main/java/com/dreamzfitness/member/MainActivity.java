package com.dreamzfitness.member;

import android.net.Uri;
import android.os.Bundle;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.BridgeWebViewClient;

public class MainActivity extends BridgeActivity {

    private static final String PORTAL_HOST = "dreamzfitness.app";
    private static final String MEMBER_LOGIN_URL = "https://dreamzfitness.app/login?source=native_app&audience=member";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        if (bridge != null) {
            bridge.setWebViewClient(new MemberPortalWebViewClient());
        }
    }

    private class MemberPortalWebViewClient extends BridgeWebViewClient {
        MemberPortalWebViewClient() {
            super(bridge);
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri url = request.getUrl();

            if (!request.isForMainFrame() || !isWebUrl(url)) {
                return super.shouldOverrideUrlLoading(view, request);
            }

            if (!PORTAL_HOST.equalsIgnoreCase(url.getHost())) {
                return super.shouldOverrideUrlLoading(view, request);
            }

            if (!isAllowedMemberPath(url.getPath())) {
                view.loadUrl(MEMBER_LOGIN_URL);
                return true;
            }

            return false;
        }

        private boolean isWebUrl(Uri url) {
            String scheme = url.getScheme();
            return "https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme);
        }

        private boolean isAllowedMemberPath(String rawPath) {
            String path = normalizePath(rawPath);

            if (path.equals("/staff") || path.startsWith("/staff/")) return false;
            if (path.equals("/admin") || path.startsWith("/admin/")) return false;

            return path.equals("/")
                || path.equals("/choose-language")
                || path.equals("/login")
                || path.equals("/dashboard")
                || path.equals("/coach")
                || path.equals("/nutrition")
                || path.equals("/progress")
                || path.equals("/account")
                || path.startsWith("/account/")
                || path.equals("/membership-options")
                || path.equals("/pricing")
                || path.equals("/group-classes")
                || path.equals("/apply");
        }

        private String normalizePath(String rawPath) {
            if (rawPath == null || rawPath.isBlank()) return "/";

            String path = rawPath.trim().toLowerCase();
            while (path.length() > 1 && path.endsWith("/")) {
                path = path.substring(0, path.length() - 1);
            }

            return path;
        }
    }
}
