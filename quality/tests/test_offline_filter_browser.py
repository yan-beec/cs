"""真实 Chrome 在断网 file:// 页面验证离线控件、历史命中与附件安全。"""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import wave
from urllib.request import urlopen

import pytest

from quality.offline.bundle import build_bundle
from quality.offline.config import OfflineSettings


EVIDENCE = Path(__file__).resolve().parents[2] / "outputs/manual-sync-20261113"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jH1sAAAAASUVORK5CYII=")


class Chrome:
    """用现有浏览器调试协议驱动真实 DOM，不安装浏览器依赖。"""

    def __init__(self, root):
        import websocket

        candidates = [os.environ.get("CHROME_BINARY", ""), shutil.which("google-chrome"),
                      "C:/Program Files/Google/Chrome/Application/chrome.exe",
                      "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"]
        executable = next((str(p) for p in candidates if p and Path(p).is_file()), None)
        if not executable:
            pytest.skip("真实浏览器验收需要 Chrome 或 Edge")
        self.profile = root / "profile"
        self.profile.mkdir()
        self.log = (root / "chrome.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen([
            executable, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
            f"--user-data-dir={self.profile}", "about:blank",
        ], stdout=self.log, stderr=self.log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        port_file = self.profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not port_file.exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.close()
                raise AssertionError("Chrome 未启动，请查看 chrome.log")
            time.sleep(0.1)
        port = port_file.read_text().splitlines()[0]
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            targets = json.load(response)
        target = next(item for item in targets if item["type"] == "page")
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10,
                                              suppress_origin=True)
        self.sequence = 0
        self.events = []
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Network.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=1440, height=1080,
                  deviceScaleFactor=1, mobile=False)
        self.call("Network.emulateNetworkConditions", offline=True, latency=0,
                  downloadThroughput=0, uploadThroughput=0)

    def call(self, method, **params):
        self.sequence += 1
        self.ws.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        while True:
            packet = json.loads(self.ws.recv())
            if packet.get("id") == self.sequence:
                assert "error" not in packet, packet
                return packet.get("result", {})
            self.events.append(packet)

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True,
                           awaitPromise=True)
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def until(self, expression):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = self.evaluate(expression)
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"浏览器等待超时：{expression}")

    def open(self, path):
        self.call("Page.navigate", url=path.resolve().as_uri())
        self.until("document.readyState === 'complete' && document.querySelectorAll('#toc .entry').length === 2")
        assert self.evaluate("navigator.onLine") is False

    def submit(self, values):
        self.evaluate("""(() => {
          const form = document.getElementById('filters');
          for (const [name, value] of Object.entries(%s)) {
            const field = form.elements.namedItem(name);
            if (!field) throw new Error('缺少控件 ' + name);
            field.value = value;
          }
          form.requestSubmit();
        })()""" % json.dumps(values))

    def close(self):
        if hasattr(self, "ws"):
            self.ws.close()
        if hasattr(self, "process"):
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if hasattr(self, "log"):
            self.log.close()


def row(number, **changes):
    """两轮已导出事实，避免接触数据库或远程附件。"""
    return {"source_key": "browser-source", "account_id": "9007199254740993",
            "customer_id": "c1", "customer_name": "浏览器客户", "contact_id": "42",
            "h5_user_id": "h5-browser", "business_user_id": "biz-browser",
            "blacklist_ever": True, "blacklist_active": False,
            "conversation_id": "10", "round_id": "r1", "round_no": 1,
            "message_id": str(number), "sender_role": "contact", "is_private": False,
            "is_human_reply": False, "has_original_attachment": False,
            "sent_at": "2020-10-01T00:00:00Z", "round_started_at": "2020-10-01T00:00:00Z",
            "round_resolved_at": "2020-10-01T01:00:00Z", "round_status": "resolved",
            "inbox_id": "3", "channel_key": "web", "reception_type": "human",
            "responsible_agent_id": "7", "responsibility_basis": "ics_assignment",
            "replied_agent_ids": ["8"], "assigned_agent_ids": ["9"], "current_assignee_id": "10",
            "visitor_messages": 2, "assisted": False, "is_ai_only": False,
            "is_handoff": True, "busy_refused": False, "content": "当前正文", **changes}


@pytest.fixture
def archive_browser(tmp_path):
    audio = io.BytesIO()
    with wave.open(audio, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(8000)
        sound.writeframes(b"\0\0" * 1600)

    class LocalMedia:
        def download(self, item, destination, check, limit=None):
            assert item["source_url"].startswith("https://browser.invalid/")
            if item["source_url"].endswith(".wav"):
                destination.write_bytes(audio.getvalue())
                return "audio/wav"
            destination.write_bytes(PNG)
            return "image/png"

    rows = [row(1, attachments=[
        {"source_url": "https://browser.invalid/pixel.png", "filename": "pixel.png", "media_type": "image"},
        {"source_url": "https://browser.invalid/silence.wav", "filename": "silence.wav", "media_type": "audio/wav"},
        {"source_url": "https://browser.invalid/blocked.pdf", "filename": "blocked.pdf", "media_type": "application/pdf"},
    ], has_original_attachment=True),
        row(2, sender_role="agent", is_human_reply=True, content="本轮完整回复"),
        row(3, customer_id="c2", customer_name="另一客户", round_id="r2", round_no=2,
            conversation_id="11", content="另一轮独立正文"),
        {"_type": "search_version", "source_key": "browser-source", "account_id": "9007199254740993",
         "message_id": "1", "version": "101", "offset": 0, "content": "编辑前旧关键词", "last": True}]
    settings = OfflineSettings(_env_file=None, directory=str(tmp_path), min_free_bytes=0)
    path = build_bundle(tmp_path / "bundle", iter(rows), settings, LocalMedia(), lambda **kw: None,
                        lambda: None, scope={"mode": "month", "month": "2020-10", "filters": {
                            "source_key": "browser-source", "account_id": "9007199254740993",
                            "channel_key": "web", "as_of": "2020-11-01T00:00:00Z"}})
    shutil.copyfile(path, EVIDENCE / "C2-browser-archive.html")
    browser = Chrome(tmp_path)
    try:
        browser.open(path)
        yield browser
    finally:
        if hasattr(browser, "ws"):
            snapshot = browser.evaluate("({url:location.href,online:navigator.onLine,status:document.getElementById('status').textContent,scope:document.getElementById('exportScope').textContent,controls:Array.from(document.querySelectorAll('#filters [name]')).map(n=>n.name)})")
            (EVIDENCE / "C2-browser-last-state.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()


def test_real_offline_controls_reset_and_fixed_snapshot(archive_browser):
    """旧控件名或重置施加近月时间，会丢失历史月全量及固定来源。"""
    browser = archive_browser
    names = browser.evaluate("Array.from(document.querySelectorAll('#filters [name]')).map(n=>n.name)")
    required = {"customer_query", "keyword", "agent_selection", "agent_scope", "inbox_selection", "reception_type",
                "ai_review_status", "human_review_status", "blacklist", "conversation_id", "round_no",
                "time_field", "start_date", "end_date", "start_hour", "start_minute", "end_hour", "end_minute",
                "rating_status", "rating", "rating_op", "lost", "outcome", "attribution", "dialogue", "message_scope"}
    assert required <= set(names), f"缺少后台同名控件：{sorted(required - set(names))}"
    assert not {"sender", "media", "customer", "conversation"} & set(names)
    assert browser.evaluate("Array.from(document.querySelector('[name=agent_scope]').options).map(n=>n.value).filter(Boolean)") == ["responsible", "replied", "assigned", "current"]
    assert browser.evaluate("Array.from(document.querySelector('[name=time_field]').options).map(n=>n.value).filter(Boolean)") == ["started_at", "message_at", "resolved_at", "rating_at", "handoff_at", "reply_at", "assigned_at"]
    scope_before = browser.evaluate("document.getElementById('exportScope').textContent")
    browser.submit({"customer_query": "另一客户"})
    browser.until("document.querySelectorAll('#toc .entry').length === 1 && document.getElementById('toc').textContent.includes('另一')")
    browser.evaluate("document.querySelector('#filters button[type=reset]').click()")
    browser.until("document.querySelectorAll('#toc .entry').length === 2")
    assert browser.evaluate("document.querySelector('[name=start_date]').value") == ""
    assert browser.evaluate("document.querySelector('[name=end_date]').value") == ""
    assert browser.evaluate("document.querySelector('[name=customer_query]').value") == ""
    assert browser.evaluate("document.getElementById('exportScope').textContent") == scope_before
    text = browser.evaluate("document.body.innerText")
    assert "快照" in text and "browser-source" in text and "9007199254740993" in text
    assert "2020-10" in text
    browser.evaluate("document.querySelector('#filters > details').open=true")
    screenshot = browser.call("Page.captureScreenshot", format="png")
    (EVIDENCE / "C2-browser-controls.png").write_bytes(base64.b64decode(screenshot["data"]))


def test_real_date_controls_validate_range_and_reset_historical_month(archive_browser):
    """非法区间必须阻止更新，近月快捷条件清空后必须恢复历史导出全集。"""
    browser = archive_browser
    browser.submit({"start_date": "2020-10-03", "end_date": "2020-10-01"})
    browser.until("/早于|晚于|起止|结束.*开始/.test(document.getElementById('status').textContent)")
    assert browser.evaluate("document.querySelectorAll('#toc .entry').length") == 2
    browser.evaluate("document.querySelector('#filters button[type=reset]').click()")
    browser.until("document.querySelector('[name=start_date]').value === ''")
    browser.evaluate("document.querySelector('[data-days=\"30\"]').click()")
    browser.until("document.querySelector('[name=start_date]').value !== ''")
    browser.submit({})
    browser.until("document.querySelectorAll('#toc .entry').length === 0")
    browser.evaluate("document.querySelector('#filters button[type=reset]').click()")
    browser.until("document.querySelectorAll('#toc .entry').length === 2")
    assert browser.evaluate("document.querySelector('[name=start_date]').value") == ""
    assert browser.evaluate("document.querySelector('[name=end_date]').value") == ""
    browser.evaluate("document.querySelector('[name=start_hour]').closest('details').open=true; document.querySelector('[name=end_hour]').closest('details').open=true")
    browser.submit({"time_field": "message_at", "start_date": "2020-10-01", "end_date": "2020-10-01",
                    "start_hour": "08", "start_minute": "00", "end_hour": "08", "end_minute": "00"})
    browser.until("document.querySelectorAll('#toc .entry').length === 2")
    browser.submit({"start_hour": "08", "start_minute": "01", "end_hour": "08", "end_minute": "01"})
    browser.until("document.querySelectorAll('#toc .entry').length === 0")


def test_real_history_keyword_keeps_full_round_without_false_highlight(archive_browser):
    """只搜索当前正文、把历史命中高亮到现正文或裁掉整轮回复，均应失败。"""
    browser = archive_browser
    browser.submit({"keyword": "旧关键词"})
    browser.until("document.querySelectorAll('#toc .entry').length === 1")
    browser.evaluate("document.querySelector('#toc .entry').click()")
    browser.until("document.getElementById('chat').textContent.includes('历史版本')")
    chat = browser.evaluate("document.getElementById('chat').innerText")
    assert "当前正文" in chat and "本轮完整回复" in chat
    assert browser.evaluate("document.querySelectorAll('#chat mark').length") == 0


def test_real_media_and_csp_remain_offline_and_inert(archive_browser):
    """媒体加载不应联网，PDF 不可打开，动态注入内联脚本和 iframe 应被 CSP 阻止。"""
    browser = archive_browser
    browser.evaluate("document.querySelector('#toc .entry').click()")
    browser.until("document.querySelector('#chat img.thumb')?.complete && document.querySelector('#chat img.thumb')?.naturalWidth === 1")
    assert browser.evaluate("document.querySelector('#chat img.thumb').src.startsWith('data:image/png;base64,')")
    assert browser.evaluate("Array.from(document.querySelectorAll('#chat a')).every(a=>!a.href.includes('blocked.pdf'))")
    assert browser.evaluate("document.querySelectorAll('#chat audio').length") == 0
    browser.evaluate("document.querySelector('#chat [data-play]').click()")
    browser.until("document.querySelector('#chat audio')?.readyState >= 2")
    assert browser.evaluate("document.querySelector('#chat audio').src.startsWith('blob:')")
    browser.evaluate("window.previousAudio=document.querySelector('#chat audio')")
    browser.evaluate("""(() => {
      window.cspViolations=[];
      document.addEventListener('securitypolicyviolation',e=>window.cspViolations.push(e.violatedDirective));
      const script=document.createElement('script'); script.textContent='window.untrustedExecuted=true';document.body.append(script);
      const frame=document.createElement('iframe');frame.src='https://browser.invalid/frame';document.body.append(frame);
    })()""")
    browser.until("window.cspViolations.length >= 2")
    assert browser.evaluate("window.untrustedExecuted === undefined")
    directives = browser.evaluate("window.cspViolations")
    assert any(item.startswith("script-src") for item in directives)
    assert any(item.startswith(("frame-src", "child-src", "default-src")) for item in directives)
    requests = [event["params"]["request"]["url"] for event in browser.events
                if event.get("method") == "Network.requestWillBeSent"]
    assert not [url for url in requests if url.startswith(("http://", "https://"))]
    browser.evaluate("document.querySelectorAll('#toc .entry')[1].click()")
    assert browser.evaluate("document.querySelectorAll('#chat img.thumb').length") == 0
    assert browser.evaluate("""new Promise(resolve => {
      const audio = new Audio(window.previousAudio.src);
      audio.onloadeddata = () => resolve(false);
      audio.onerror = () => resolve(true);
      audio.load();
    })""")


@pytest.fixture
def streaming_browser(tmp_path):
    """一百个版本块触发真正的异步让出；微秒与字符映射为明确的合成契约。"""
    rows = [row(1, content="边界", sent_at="2020-10-01T00:00:00.000500Z"),
            row(3, customer_id="c2", customer_name="另一客户", round_id="r2", round_no=2,
                conversation_id="11", content="边界", sent_at="2020-10-01T00:00:00.000501Z")]

    def chunk(message_id, version, offset, content, last):
        return {"_type": "search_version", "source_key": "browser-source", "account_id": "9007199254740993",
                "message_id": message_id, "version": version, "offset": offset, "content": content, "last": last}

    rows.extend(chunk("1", "101", i, "旧关键词" if i == 95 else "x", i == 95) for i in range(96))
    rows.extend([chunk("1", "201", 0, "Ο", False), chunk("1", "201", 1, "Σİ", True),
                 chunk("1", "202", 0, "边界", True), chunk("3", "203", 0, "边界", True)])
    casefold_map = {str(n): chr(n + 32) for n in range(65, 91)}
    casefold_map.update({"927": "ο", "931": "σ", "304": "i"})
    settings = OfflineSettings(_env_file=None, directory=str(tmp_path), min_free_bytes=0)
    path = build_bundle(tmp_path / "bundle", iter(rows), settings, object(), lambda **kw: None,
                        lambda: None, metadata={"casefold_map": casefold_map},
                        scope={"mode": "month", "month": "2020-10", "filters": {
                            "source_key": "browser-source", "account_id": "9007199254740993",
                            "as_of": "2020-10-01T00:00:00.000501Z"}})
    browser = Chrome(tmp_path)
    try:
        browser.open(path)
        assert browser.evaluate("JSON.parse(document.getElementById('catalog').textContent).meta.search_version_parts") == 100
        yield browser
    finally:
        browser.close()


@pytest.mark.parametrize("replacement", ["query", "reset"])
def test_real_long_search_late_result_cannot_replace_new_directory(streaming_browser, replacement):
    """旧搜索让出后立刻更换条件或重置，迟到计算不得恢复旧目录。"""
    browser = streaming_browser
    result = browser.evaluate("""new Promise(resolve => {
      const form=document.getElementById('filters'), status=document.getElementById('status');
      const snapshot=()=>({items:Array.from(document.querySelectorAll('#toc .entry strong')).map(n=>n.textContent),status:status.textContent});
      const observer=new MutationObserver(() => {
        if(!status.textContent.startsWith('正在搜索历史正文'))return;
        const progress=status.textContent;
        observer.disconnect();
        form.elements.namedItem('keyword').value='';
        if(%s==='reset')form.querySelector('button[type=reset]').click();
        else {form.elements.namedItem('customer_query').value='另一客户';form.requestSubmit();}
        setTimeout(()=>{const early=snapshot();setTimeout(()=>resolve({progress,early,late:snapshot()}),120);},0);
      });
      observer.observe(status,{childList:true,subtree:true,characterData:true});
      form.elements.namedItem('keyword').value='旧关键词';
      form.requestSubmit();
    })""" % json.dumps(replacement))
    assert "32 / 100" in result["progress"]
    expected = ["会话 11 · 会话内第 2 轮"] if replacement == "query" else ["会话 10 · 会话内第 1 轮", "会话 11 · 会话内第 2 轮"]
    # 目录文案允许继续优化，身份集合以可见会话与轮次核对。
    expected_ids = [("11", "2")] if replacement == "query" else [("10", "1"), ("11", "2")]
    for state in [result["early"], result["late"]]:
        assert len(state["items"]) == len(expected_ids), (expected, result)
        for text, (conversation, number) in zip(state["items"], expected_ids):
            assert f"会话 {conversation}" in text and f"{number} 轮" in text, result
        assert state["status"].startswith("共 "), result
    (EVIDENCE / f"C2-browser-race-{replacement}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def test_real_unicode_chunk_boundary_and_microsecond_cutoff(streaming_browser):
    """逐块小写不能引入末尾 sigma，微秒截止不能截为毫秒。"""
    browser = streaming_browser
    mapping = browser.evaluate("JSON.parse(document.getElementById('catalog').textContent).meta.filter_contract.casefold_map")
    assert mapping and mapping["927"] == "ο" and mapping["931"] == "σ" and mapping["304"] == "i"
    for keyword, expected in [("οσ", 1), ("ος", 0), ("ΟΣ", 1), ("οσi", 1)]:
        browser.submit({"keyword": keyword})
        browser.until(f"document.getElementById('status').textContent.startsWith('共 ') && document.querySelectorAll('#toc .entry').length === {expected}")
        if expected:
            browser.evaluate("document.querySelector('#toc .entry').click()")
            assert "历史版本" in browser.evaluate("document.getElementById('chat').textContent")
    browser.submit({"keyword": "边界"})
    browser.until("document.getElementById('status').textContent.startsWith('共 ') && document.querySelectorAll('#toc .entry').length === 1")
    assert "会话 10" in browser.evaluate("document.getElementById('toc').textContent")
    assert "会话 11" not in browser.evaluate("document.getElementById('toc').textContent")


@pytest.mark.parametrize("metric_seat", ["", "9007199254740993"])
def test_real_metric_export_window_survives_local_filter_and_reset(tmp_path, metric_seat):
    """统计固定窗口仅描述导出集合，不能二次施加到临时消息时间筛选或重置。"""
    rows = [row(1, content="第一轮", round_started_at="2020-09-30T00:00:00Z"),
            row(3, customer_id="c2", customer_name="另一客户", round_id="r2", round_no=2,
                conversation_id="11", content="独立", sent_at="2020-10-02T00:00:00Z")]
    for message_id, body in [("1", "第一轮"), ("3", "独立")]:
        rows.append({"_type": "search_version", "source_key": "browser-source", "account_id": "9007199254740993",
                     "message_id": message_id, "version": "101", "offset": 0, "content": body, "last": True})
    fixed = {"metric": "inquiries", "metric_seat": metric_seat, "source_key": "browser-source",
             "account_id": "9007199254740993", "as_of": "2020-11-01T00:00:00Z",
             "start": "2020-10-03T00:00:00Z", "end": "2020-10-04T00:00:00Z"}
    settings = OfflineSettings(_env_file=None, directory=str(tmp_path), min_free_bytes=0)
    path = build_bundle(tmp_path / "bundle", iter(rows), settings, object(), lambda **kw: None,
                        lambda: None, scope={"mode": "rounds", "filters": fixed})
    browser = Chrome(tmp_path)
    try:
        browser.open(path)
        before = browser.evaluate("document.getElementById('exportScope').textContent")
        assert "inquiries" in before and "2020-10-03T00:00:00Z" in before and "2020-10-04T00:00:00Z" in before
        assert browser.evaluate("JSON.parse(document.getElementById('catalog').textContent).meta.filter_contract.fixed_context") == fixed
        browser.submit({"time_field": "message_at", "start_date": "2020-10-01", "end_date": "2020-10-01"})
        browser.until("document.querySelectorAll('#toc .entry').length === 1 && document.getElementById('toc').textContent.includes('会话 10')")
        browser.evaluate("document.querySelector('#filters button[type=reset]').click()")
        browser.until("document.querySelectorAll('#toc .entry').length === 2")
        browser.submit({"keyword": "独立"})
        browser.until("document.getElementById('status').textContent.startsWith('共 ') && document.querySelectorAll('#toc .entry').length === 1 && document.getElementById('toc').textContent.includes('会话 11')")
        browser.evaluate("document.querySelector('#filters button[type=reset]').click()")
        browser.until("document.querySelectorAll('#toc .entry').length === 2")
        assert browser.evaluate("document.getElementById('exportScope').textContent") == before
        assert browser.evaluate("JSON.parse(document.getElementById('catalog').textContent).meta.filter_contract.fixed_context") == fixed
        assert browser.evaluate("document.querySelector('[name=start_date]').value") == ""
        assert browser.evaluate("document.querySelector('[name=end_date]').value") == ""
    finally:
        browser.close()
