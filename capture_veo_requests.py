import json
import time
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

OUTPUT = Path("veo_network_log.jsonl")
URL = "https://geminigen.ai/app/video-gen/veo"


def main() -> int:
    options = uc.ChromeOptions()
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    driver = uc.Chrome(options=options, use_subprocess=True)
    try:
        driver.get(URL)
        WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        driver.execute_script(
            """
window.__veoLogs = [];
(function() {
  if (window.__veoHooked) return;
  window.__veoHooked = true;

  const pushLog = (item) => {
    try {
      window.__veoLogs.push({ ts: Date.now(), ...item });
    } catch (e) {}
  };

  const normalizeHeaders = (headersLike) => {
    const out = {};
    try {
      if (!headersLike) return out;
      if (headersLike instanceof Headers) {
        headersLike.forEach((v, k) => { out[String(k)] = String(v); });
        return out;
      }
      if (Array.isArray(headersLike)) {
        for (const pair of headersLike) {
          if (Array.isArray(pair) && pair.length >= 2) {
            out[String(pair[0])] = String(pair[1]);
          }
        }
        return out;
      }
      if (typeof headersLike === 'object') {
        for (const k of Object.keys(headersLike)) {
          out[String(k)] = String(headersLike[k]);
        }
      }
    } catch (e) {}
    return out;
  };

  const origFetch = window.fetch;
  window.fetch = async function(...args) {
    const url = args[0];
    const opts = args[1] || {};
    const requestHeaders = normalizeHeaders(opts.headers);
    let body = opts.body;
    let bodyPreview = null;
    try {
      if (body instanceof FormData) {
        bodyPreview = [];
        for (const pair of body.entries()) {
          const key = pair[0];
          const value = pair[1];
          if (value instanceof File) {
            bodyPreview.push([key, {name: value.name, size: value.size, type: value.type}]);
          } else {
            bodyPreview.push([key, String(value)]);
          }
        }
      } else if (typeof body === 'string') {
        bodyPreview = body.slice(0, 5000);
      }
    } catch (e) {
      bodyPreview = 'BODY_PARSE_ERROR: ' + String(e);
    }
    pushLog({
      kind: 'fetch_request',
      url: String(url),
      method: opts.method || 'GET',
      headers: requestHeaders,
      body: bodyPreview
    });
    const res = await origFetch.apply(this, args);
    let responsePreview = null;
    try {
      const clone = res.clone();
      responsePreview = (await clone.text()).slice(0, 3000);
    } catch (e) {
      responsePreview = 'RESPONSE_READ_ERROR: ' + String(e);
    }
    pushLog({
      kind: 'fetch_response',
      url: String(url),
      status: res.status,
      ok: !!res.ok,
      contentType: res.headers ? res.headers.get('content-type') : null,
      bodyPreview: responsePreview
    });
    return res;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  const origSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__veoMethod = method;
    this.__veoUrl = url;
    this.__veoHeaders = {};
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
    try {
      if (!this.__veoHeaders) this.__veoHeaders = {};
      this.__veoHeaders[String(name)] = String(value);
    } catch (e) {}
    return origSetRequestHeader.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    let bodyPreview = null;
    try {
      if (body instanceof FormData) {
        bodyPreview = [];
        for (const pair of body.entries()) {
          const key = pair[0];
          const value = pair[1];
          if (value instanceof File) {
            bodyPreview.push([key, {name: value.name, size: value.size, type: value.type}]);
          } else {
            bodyPreview.push([key, String(value)]);
          }
        }
      } else if (typeof body === 'string') {
        bodyPreview = body.slice(0, 5000);
      }
    } catch (e) {
      bodyPreview = 'BODY_PARSE_ERROR: ' + String(e);
    }
    const xhr = this;
    const onDone = function() {
      if (xhr.readyState !== 4) return;
      let preview = null;
      try {
        preview = String(xhr.responseText || '').slice(0, 3000);
      } catch (e) {
        preview = 'RESPONSE_READ_ERROR: ' + String(e);
      }
      pushLog({
        kind: 'xhr_response',
        url: String(xhr.__veoUrl),
        method: xhr.__veoMethod || 'GET',
        status: xhr.status,
        bodyPreview: preview
      });
      try { xhr.removeEventListener('readystatechange', onDone); } catch (e) {}
    };
    try { xhr.addEventListener('readystatechange', onDone); } catch (e) {}
    pushLog({
      kind: 'xhr_request',
      url: String(this.__veoUrl),
      method: this.__veoMethod || 'GET',
      headers: this.__veoHeaders || {},
      body: bodyPreview
    });
    return origSend.apply(this, arguments);
  };
})();
"""
        )
        print("已打开 Veo 页面，并注入请求拦截。")
        print("请你在浏览器里手动执行一次 Veo 生成。完成后回到终端按 Enter。")
        input()
        logs = driver.execute_script("return window.__veoLogs || []")
        with OUTPUT.open("w", encoding="utf-8") as f:
            for item in logs:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"已保存日志: {OUTPUT}")
        print(f"日志条数: {len(logs)}")
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
