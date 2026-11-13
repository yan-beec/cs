"""离线留档双包：HTML 按轮次分块只含文字，图片/视频原文件进附件 ZIP，正文不作为活动内容执行。"""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

import pytest

from quality.offline.bundle import build_bundle, BundleLimit, page_shell
from quality.offline.config import OfflineSettings
from quality.offline.media import MediaDownloader, MediaTooLarge, MediaUnavailable

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
VIEWER = Path(__file__).parents[1] / "offline/web/viewer.js"


def settings(tmp_path, **kwargs):
    return OfflineSettings(_env_file=None, directory=str(tmp_path), min_free_bytes=0, **kwargs)


def message(number, **changes):
    return {"message_id": str(number), "customer_id": "c1", "customer_name": "客户",
            "contact_id": "42", "h5_user_id": "8570", "conversation_id": "10",
            "round_id": "r1", "round_no": 1, "sent_at": "2026-10-01T00:00:00+00:00",
            "sender_role": "contact", "content": f"正文 {number}", **changes}


def parse(path):
    """按浏览器同样的方式取出数据块：目录、每轮消息；附件从同目录的附件包按“文件夹/文件名”读取。"""
    page = Path(path).read_text(encoding="utf-8")
    blocks = dict(re.findall(r'<script type="application/json" id="([^"]+)">(.*?)</script>', page, re.S))
    catalog = json.loads(blocks.pop("catalog"))
    assert '<script type="text/plain"' not in page  # 附件不再以 base64 进页面
    media = {}
    archive = Path(path).parent / "media.zip"
    if archive.exists():
        folder = catalog["meta"]["media_folder"]
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                assert info.compress_type == zipfile.ZIP_STORED and info.filename.startswith(folder + "/")
                media[info.filename[len(folder) + 1:]] = bundle.read(info)
    else:
        assert catalog["meta"].get("media_folder") is None
    rounds = []
    for entry in catalog["entries"]:
        rows = []
        for part in range(entry["parts"]):
            rows += json.loads(blocks[f"g{entry['index']}.{part}"])
        rounds.append(rows)
    return page, catalog, rounds, media


def build(tmp_path, rows, source=None, **options):
    return build_bundle(tmp_path / "work", rows, settings(tmp_path, **options), source or MediaDownloader([]),
                        lambda **v: None, lambda: None, scope={"mode": "month", "month": "2026-10"})


def test_single_html_with_all_messages_and_safe_data_blocks(tmp_path):
    body = '</script><img src=x onerror=alert(1)>\x00😀'
    result = build(tmp_path, (message(i, content=body) for i in range(5101)))
    assert result.name == "archive.html"
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == ["archive.html"]
    page, catalog, rounds, _ = parse(result)
    assert catalog["meta"]["messages"] == 5101 and catalog["meta"]["rounds"] == 1
    assert catalog["meta"]["title"] == "聊天记录2026年10月" and "<title>聊天记录2026年10月</title>" in page
    assert len(rounds[0]) == 5101 and rounds[0][-1]["message_id"] == "5100" and rounds[0][0]["content"] == body
    # 正文只能以转义形式出现在数据块中，不能闭合标签或成为活动内容。
    assert "onerror=alert" not in page.replace("\\u003cimg src=x onerror=alert(1)\\u003e", "")
    assert page.count("</script>") == page.count("<script")


def test_rounds_become_table_of_contents_with_summary(tmp_path):
    rows = [message(1, content="  我想咨询\n退款  ", round_rating_status="rated",
                    round_rating=3, round_rating_feedback="慢"), message(2, sender_role="agent", sender_name="小王", content="好的"),
            message(3, sender_role="contact", is_rating=True, content="请评价",
                    visitor_rating={"rating": 3, "feedback": "慢"}),
            message(4, round_id="r2", round_no=2, customer_round_seq=2, content="第二次"),
            message(5, round_id=None, round_no=None, content="未归轮"),
            message(6, customer_id="c2", customer_name="乙", conversation_id="20", round_id="r3", content="乙的问题")]
    _, catalog, rounds, _ = parse(build(tmp_path, rows))
    entries = catalog["entries"]
    assert [len(r) for r in rounds] == [3, 1, 1, 1]
    assert [c["name"] for c in catalog["customers"]] == ["客户", "乙"]
    assert [e["customer"] for e in entries] == [0, 0, 0, 1]
    first = entries[0]
    assert first["preview"] == "我想咨询 退款" and first["messages"] == 3 and first["has_agent"]
    assert first["rating"] == 3 and first["rating_feedback"] == "慢"
    assert entries[1]["customer_round_seq"] == 2 and entries[2]["round_id"] is None
    assert "customer_name" not in rounds[0][0]  # 轮次与客户信息只在目录中保存一次


def test_long_round_is_split_into_parts(tmp_path, monkeypatch):
    import quality.offline.bundle as bundle
    monkeypatch.setattr(bundle, "PART_BYTES", 2000)
    _, catalog, rounds, _ = parse(build(tmp_path, [message(i, content="x" * 200) for i in range(50)]))
    assert catalog["entries"][0]["parts"] > 1 and len(rounds[0]) == 50


def test_images_and_videos_embedded_once_other_files_linked(tmp_path):
    class Source:
        calls = []
        def download(self, item, destination, check, limit=None):
            self.calls.append((item["source_url"], limit))
            if item["source_url"].endswith("gone"):
                raise MediaUnavailable("附件已失效")
            destination.write_bytes(MP4 if item["source_url"].endswith("movie") else PNG)
            return "video/mp4" if item["source_url"].endswith("movie") else "image/png"
    source = Source()
    attachments = [{"source_url": "https://chat.test/pic", "filename": "../../a.png", "media_type": "image"},
                   {"source_url": "https://chat.test/movie", "filename": "movie.mp4", "media_type": "video", "expected_byte_count": 9},
                   {"source_url": "https://chat.test/gone", "filename": "lost.png", "media_type": "image/png"},
                   {"source_url": "javascript:alert(1)", "filename": "doc.docx", "media_type": "file"}]
    result = build(tmp_path, [message(1, attachments=attachments), message(2, attachments=attachments)], source)
    _, catalog, rounds, media = parse(result)
    first, second = rounds[0][0]["attachments"], rounds[0][1]["attachments"]
    # 文件不下载；图片、视频按 URL 去重，且分别使用图片 / 视频的单个上限。
    assert source.calls == [("https://chat.test/pic", 50 * 1024**2), ("https://chat.test/movie", 500 * 1024**2),
                            ("https://chat.test/gone", 50 * 1024**2)]
    assert first[0]["status"] == "embedded" and second[0]["file"] == first[0]["file"] == "000000.png"
    assert media[first[0]["file"]] == PNG and "media" not in first[0]
    assert first[0]["checksum_sha256"] == hashlib.sha256(PNG).hexdigest()
    assert first[1]["status"] == "embedded" and first[1]["kind"] == "video" and first[1]["byte_count"] == len(MP4)
    assert media[first[1]["file"]] == MP4 and second[1]["file"] == first[1]["file"] == "000001.mp4"
    assert first[1]["source_url"] == "https://chat.test/movie"
    assert first[2]["status"] == "unavailable"
    # 非图片/音视频：不下载、不打包，也不保留任何可点击地址。
    assert first[3]["status"] == "blocked" and first[3]["source_url"] is None and "防范病毒" in first[3]["reason"]
    assert catalog["meta"]["blocked_attachments"] == 2 and catalog["meta"]["linked_attachments"] == 0
    assert len(media) == 2 and not (tmp_path / "work" / "media").exists()
    assert catalog["meta"]["media_folder"] == "ics-attachments-2026-10" and catalog["meta"]["media_files"] == 2
    assert catalog["meta"]["media_archive"] == "聊天记录2026年10月-附件.zip"
    entry = catalog["entries"][0]
    assert entry["images"] == 2 and entry["videos"] == 2 and entry["unavailable"] == 2
    assert catalog["meta"]["images"] == 2 and catalog["meta"]["videos"] == 2


def test_oversized_or_unplayable_downloads_fall_back_to_links(tmp_path):
    bodies = {"noext": MP4 * 100, "huge.png": PNG * 100, "blob": b"plain text", "clip.mp4": MP4}
    class Source:
        def download(self, item, destination, check, limit=None):
            name = item["source_url"].rsplit("/", 1)[1]
            if name == "big":
                raise MediaTooLarge("附件超过单文件大小限制")
            destination.write_bytes(bodies[name])
            return "application/octet-stream"
    attachments = [{"source_url": "https://chat.test/big", "media_type": "image"},
                   {"source_url": "https://chat.test/noext", "media_type": ""},
                   {"source_url": "https://chat.test/huge.png", "media_type": "image"},
                   {"source_url": "https://chat.test/blob", "media_type": ""},
                   {"source_url": "https://chat.test/clip.mp4", "media_type": "video"}]
    _, _, rounds, media = parse(build(tmp_path, [message(1, attachments=attachments)], Source(),
                                      max_embed_bytes=1024, max_embed_video_bytes=2048))
    saved = rounds[0][0]["attachments"]
    # 无扩展名又无类型声明的附件无法确认是图片/视频，按安全口径直接拦截。
    assert [a["status"] for a in saved] == ["linked", "blocked", "linked", "blocked", "embedded"]
    assert "图片超过 1 KB 单个附件上限" in saved[0]["reason"]
    assert saved[1]["source_url"] is None and saved[3]["source_url"] is None
    assert "图片超过 1 KB" in saved[2]["reason"]
    assert saved[4]["kind"] == "video" and len(media) == 1


def test_non_media_files_are_never_downloaded_and_disguised_media_is_blocked(tmp_path):
    bodies = {"photo.png": b"MZ\x90\x00 not an image", "clip.mp4": MP4}
    class Source:
        calls = []
        def download(self, item, destination, check, limit=None):
            name = item["source_url"].rsplit("/", 1)[1]
            self.calls.append(name)
            destination.write_bytes(bodies[name])
            return "application/octet-stream"
    source = Source()
    attachments = [{"source_url": "https://chat.test/a/%E6%8A%A5%E4%BB%B7.exe", "filename": "报价.exe", "media_type": "file"},
                   {"source_url": "https://chat.test/a/run.bat", "filename": "截图.png", "media_type": "image"},
                   {"source_url": "https://chat.test/a/x.pdf", "filename": "x.pdf", "media_type": "application/pdf"},
                   {"source_url": "https://chat.test/a/photo.png", "filename": "photo.png", "media_type": "image"},
                   {"source_url": "https://chat.test/a/clip.mp4", "filename": "clip.mp4", "media_type": "video"}]
    page, catalog, rounds, media = parse(build(tmp_path, [message(1, attachments=attachments)], source))
    saved = rounds[0][0]["attachments"]
    assert source.calls == ["photo.png", "clip.mp4"]  # exe/bat/pdf 根本不下载
    assert [a["status"] for a in saved] == ["blocked", "blocked", "blocked", "blocked", "embedded"]
    assert all(a["source_url"] is None for a in saved[:4])
    assert saved[0]["filename"] == "报价.exe"  # 文件名保留，便于质检知道客户发过什么
    for leaked in ("a/run.bat", "a/x.pdf", "a/photo.png", "%E6%8A%A5"):
        assert leaked not in page
    assert catalog["meta"]["blocked_attachments"] == 4 and len(media) == 1
    assert not (tmp_path / "work" / "media").exists() or not any((tmp_path / "work" / "media").iterdir())


def test_embedded_total_limit_keeps_the_rest_as_links(tmp_path):
    class Source:
        calls = 0
        def download(self, item, destination, check, limit=None):
            self.calls += 1
            destination.write_bytes(PNG)
            return "image/png"
    source = Source()
    rows = [message(i, attachments=[{"source_url": f"https://chat.test/{i}.png", "media_type": "image"}]) for i in range(3)]
    _, _, rounds, media = parse(build(tmp_path, rows, source, max_embedded_total_bytes=len(PNG) * 2))
    assert [r["attachments"][0]["status"] for r in rounds[0]] == ["embedded", "embedded", "linked"]
    assert len(media) == 2 and source.calls == 2  # 达到总量后不再下载


def test_disk_budget_shortage_links_media_instead_of_failing(tmp_path):
    class Source:
        calls = 0
        def download(self, item, destination, check, limit=None):
            self.calls += 1
            destination.write_bytes(PNG)
            return "image/png"
    source = Source()
    rows = [message(i, attachments=[{"source_url": f"https://chat.test/{i}.png", "media_type": "image",
                                     "expected_byte_count": 300 * 1024}]) for i in range(3)]
    _, catalog, rounds, media = parse(build(tmp_path, rows, source, disk_budget_bytes=512 * 1024))
    saved = [r["attachments"][0] for r in rounds[0]]
    assert [a["status"] for a in saved] == ["linked"] * 3 and "DISK_BUDGET" in saved[0]["reason"]
    assert source.calls == 0 and media == {} and not (tmp_path / "work" / "media.zip").exists()


def test_media_does_not_count_toward_max_bytes(tmp_path):
    big = PNG + b"\x00" * 4096
    class Source:
        def download(self, item, destination, check, limit=None):
            destination.write_bytes(big)
            check()
            return "image/png"
    rows = [message(i, attachments=[{"source_url": f"https://chat.test/{i}.png", "media_type": "image"}]) for i in range(4)]
    _, _, rounds, media = parse(build(tmp_path, rows, Source(), max_bytes=8 * 1024))
    assert [r["attachments"][0]["status"] for r in rounds[0]] == ["embedded"] * 4 and len(media) == 4


def test_budget_failure_never_leaves_a_successful_page(tmp_path):
    with pytest.raises(BundleLimit):
        build(tmp_path, [message(1, content="x" * 3000)], max_bytes=1024)
    assert not (tmp_path / "work" / "archive.html").exists()


def test_disk_budget_includes_existing_files(tmp_path):
    (tmp_path / "older-archive.zip").write_bytes(b"x" * 1024 * 1024)
    with pytest.raises(BundleLimit, match="DISK_BUDGET"):
        build(tmp_path, [message(i, content="y" * 1000) for i in range(2000)], disk_budget_bytes=2 * 1024**2)
    assert not (tmp_path / "work/archive.html").exists()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://evil.test/file", "http://user:pass@chat.test/file"])
def test_untrusted_media_sources_are_never_requested(url, tmp_path):
    with pytest.raises(MediaUnavailable):
        MediaDownloader(["https://chat.test"]).download({"source_url": url}, tmp_path / "file", lambda: None)
    assert not (tmp_path / "file").exists()


def test_timestamps_are_iso_8601_for_every_browser(tmp_path):
    from datetime import datetime, timedelta, timezone
    at = datetime(2026, 10, 1, 16, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
    _, catalog, rounds, _ = parse(build(tmp_path, [message(1, sent_at=at)]))
    assert rounds[0][0]["sent_at"] == "2026-10-01T16:00:00.123456+08:00"
    assert catalog["entries"][0]["first_at"] == "2026-10-01T16:00:00.123456+08:00"


def test_csp_only_allows_the_inline_viewer():
    head, tail = page_shell("聊天记录<2026>")
    script = re.search(r"<script>(.*)</script>", tail, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in head and "unsafe-inline" not in head
    assert "connect-src 'none'" in head and "<title>聊天记录&lt;2026&gt;</title>" in head


def run_node(code):
    run = subprocess.run(["node", "-e", code, str(VIEWER.resolve())], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr + run.stdout


def test_viewer_filters_labels_and_legacy_time_format():
    run_node(r'''
const assert = require('node:assert/strict');
const {matches,messageMatches,time,roundTitle,ratingText} = require(process.argv[1]);
assert.equal(time('2026-10-01 16:00:00.123456+08:00'), Date.parse('2026-10-01T08:00:00.123Z'));
assert.equal(time('2026-10-01T08:00'), Date.parse('2026-10-01T00:00:00Z'));
assert.equal(roundTitle({round_id:'r',conversation_id:'22',round_no:1,customer_round_seq:3}), '用户第 3 次咨询 · 会话 22 · 会话内第 1 轮');
assert.equal(roundTitle({round_id:null,conversation_id:'22'}), '会话 22 · 未归轮消息');
assert.equal(ratingText({rating:3,feedback:'慢'}), '访客评价：★★★☆☆ 3 分 · 慢');
assert.equal(ratingText({rating:null,status:'pending'}), '访客评价：待评价');
const customer = {name:'客户甲',id:'c1',contact_id:'42',h5_user_id:'8570'};
const entry = {conversation_id:'10',round_id:'r1',reception_type:'human_involved',ai_review_status:'passed',
  round_started_at:'2026-10-01T01:30:00Z',rated:true,first_at:'2026-10-01T01:00:00Z',last_at:'2026-10-01T02:00:00Z',rating:3,attachments:1,images:1,unavailable:0};
assert(matches(entry,customer,{customer:'8570',conversation:'10',reception:'human',ai:'passed',human:'pending',rating:'low',media:'image',start:'2026-10-01T09:30',end:'2026-10-01T10:00'}));
for (const f of [{customer:'99'},{conversation:'11'},{reception:'ai_only'},{ai:'failed'},{start:'2026-10-01T10:30'},{end:'2026-10-01T09:00'}])
  assert(!matches(entry,customer,f), JSON.stringify(f));
assert(!matches({...entry,rating:5},customer,{rating:'low'}) && !matches({...entry,rating:null,rated:false},customer,{rating:'rated'}));
const row = {content:'HELLO\n咨询',sender_name:'小王',sender_id:'7',attachments:[{filename:'发票.png'}]};
assert(messageMatches(row,{keyword:'hello',sender:'小王'}) && messageMatches(row,{keyword:'发票'}));
assert(!messageMatches(row,{keyword:'missing'}) && !messageMatches(row,{sender:'老李'}));
''')


def test_viewer_renders_toc_and_chat_from_a_generated_page(tmp_path):
    """用生成的真实页面驱动查看脚本：目录按用户分组、点击后以气泡展示、关键词筛选读取正文、图片和视频按附件包相对路径加载。"""
    class Source:
        def download(self, item, destination, check, limit=None):
            video = item["source_url"].endswith(".mp4")
            destination.write_bytes(MP4 if video else PNG)
            return "video/mp4" if video else "image/png"
    rows = [message(1, content="退款问题"), message(2, sender_role="agent", sender_name="小王", content="请提供订单号"),
            message(3, attachments=[{"source_url": "https://chat.test/a.png", "media_type": "image", "filename": "订单.png"},
                                    {"source_url": "https://chat.test/clip.mp4", "media_type": "video", "filename": "录屏.mp4"}],
                    content=""),
            message(4, round_id="r2", round_no=2, customer_round_seq=2, content="发票怎么开"),
            message(5, customer_id="c2", customer_name="乙", conversation_id="20", round_id="r3", content="<b>你好</b>")]
    versions = [{"_type": "search_version", "source_key": row.get("source_key"),
                 "account_id": row.get("account_id"), "message_id": row["message_id"],
                 "version": row["message_id"], "offset": 0, "content": row["content"], "last": True}
                for row in rows]
    page_path = build(tmp_path, [*rows, *versions], Source())
    run_node(r'''
const assert = require('node:assert/strict'), fs = require('fs'), vm = require('vm');
const page = fs.readFileSync(process.argv[2], 'utf8');
const blocks = {};
for (const m of page.matchAll(/<script type="(?:application\/json|text\/plain)" id="([^"]+)"(?: data-type="([^"]+)")?>([\s\S]*?)<\/script>/g))
  blocks[m[1]] = {textContent: m[3], dataset: {type: m[2]}};
const script = page.match(/<script>([\s\S]*)<\/script><\/body>/)[1];
const els = {};
const el = id => blocks[id] || (els[id] ||= {id, innerHTML:'', textContent:'', disabled:false, hidden:true, handlers:{},
  addEventListener(t, fn){ this.handlers[t] = fn; }, querySelector(){ return null; },
  querySelectorAll(sel){ if (sel !== 'img[data-file]') return []; return [...this.innerHTML.matchAll(/data-file="([^"]+)"/g)].map(m => ({dataset:{file:m[1]}, handlers:{}, addEventListener(t, fn){ this.handlers[t] = fn; }, replaceWith(n){ els.__missing = n; }, set src(v){ (els.__srcs ||= []).push(v); if (els.__srcs.length < 3) els.__img = this; }})); },
  removeAttribute(){} });
const bodyClasses = new Set();
global.document = {readyState:'loading', getElementById: el, body:{classList:{add:c=>bodyClasses.add(c), remove:c=>bodyClasses.delete(c)}},
  createElement: tag => ({tag, dataset:{}, handlers:{}, addEventListener(t, fn){ this.handlers[t] = fn; }, play(){ return Promise.resolve(); }}), addEventListener(t, fn){ this['on' + t] = fn; }};
global.window = global; global.module = undefined;
global.FormData = function(form){ return Object.entries(form.values || {}); };
vm.runInThisContext(script);
const settle = () => new Promise(r => setTimeout(r, 10));
(async () => {
  document.onDOMContentLoaded(); await settle();
  assert.match(els.summary.textContent, /^2 位用户 · 3 轮对话 · 未归轮消息 0 条 · 5 条消息 · 附件包图片 1 · 附件包视频\/音频 1/);
  const toc = els.toc.innerHTML;
  assert.equal((toc.match(/class="who"/g) || []).length, 2);
  assert.ok(toc.includes('第 2 次咨询 · 会话 10 第 2 轮') && toc.includes('退款问题') && toc.includes('视频/音频 1'));
  // 宽屏默认打开第一轮：客户在左、客服在右，图片按附件包相对路径加载，找不到时再试 Windows 多套一层的位置。
  let chat = els.chat.innerHTML;
  assert.ok(chat.includes('msg left contact') && chat.includes('msg right agent') && chat.includes('请提供订单号'));
  const dir = 'ics-attachments-2026-10', zip = encodeURIComponent('聊天记录2026年10月-附件');
  assert.deepEqual(els.__srcs, [`${dir}/000000.png`]);
  els.__img.handlers.error(); els.__img.handlers.error();
  assert.deepEqual(els.__srcs, [`${dir}/000000.png`, `${zip}/${dir}/000000.png`]);
  assert.ok(els.__missing.textContent.includes('聊天记录2026年10月-附件.zip'));
  // 视频先显示播放按钮，点击后才按相对路径加载。
  assert.ok(chat.includes('data-play-file="000001.mp4"') && chat.includes('▶ 播放视频') && chat.includes('录屏.mp4'));
  let replaced = null;
  const box = {dataset:{name:'录屏.mp4'}, classList:{contains:c => c === 'video'}, querySelector:() => null, appendChild(){}};
  const button = {dataset:{playFile:'000001.mp4'}, closest:() => box, replaceWith(node){ replaced = node; }};
  els.chat.handlers.click({target:{closest: sel => sel.includes('button[data-play-file]') ? button : null}});
  assert.equal(replaced.tag, 'video'); assert.equal(replaced.controls, true);
  assert.equal(replaced.src, `${dir}/000001.mp4`);
  els.toc.handlers.click({target:{closest: () => ({dataset:{at:'2'}})}});
  chat = els.chat.innerHTML;
  assert.ok(chat.includes('&lt;b&gt;你好&lt;/b&gt;') && !chat.includes('<b>你好'));
  els.filters.values = {keyword:'发票'};
  els.filters.handlers.submit({preventDefault(){}}); await settle();
  assert.equal(els.status.textContent, '共 1 轮对话 · 未归轮消息 0 条 · 1 位用户');
  assert.ok(els.chat.innerHTML.includes('<mark>发票</mark>'));
})().catch(e => { console.error(e); process.exit(1); });
''' .replace("process.argv[2]", json.dumps(str(page_path))).replace("MP4_HEX", MP4.hex()))
    assert '<script type="text/plain"' not in Path(page_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("status,score,feedback,want", [
    ("rated", 5, "满意", (True, 5, "满意")),
    ("pending", None, None, (False, None, None)),
    (None, None, None, (False, None, None)),
])
def test_catalog_uses_round_rating_instead_of_bubble_rating(tmp_path, status, score, feedback, want):
    # 消息中的低分评价仅作为聊天内容，不能覆盖轮次评价或制造已评分状态。
    bubble = {"rating": 1, "feedback": "旧消息评价"}
    rows = [message(1, round_rating_status=status, round_rating=score,
                    round_rating_feedback=feedback, visitor_rating=bubble)]
    _, catalog, rounds, _ = parse(build(tmp_path, rows))
    entry = catalog["entries"][0]
    assert (entry["rated"], entry["rating"], entry["rating_feedback"]) == want
    assert rounds[0][0]["visitor_rating"] == bubble


def test_catalog_metrics_use_whole_round_and_statistics_facts(tmp_path, monkeypatch):
    import quality.offline.bundle as bundle
    monkeypatch.setattr(bundle, "PART_BYTES", 1)
    common = dict(round_started_at="2026-10-01T00:00:00Z", round_status="resolved",
                  closed_at="2026-10-01T00:10:00Z", last_message_at="2026-10-01T00:02:00Z",
                  responsibility_basis="ics_assignment", responsible_assigned_at="2026-10-01T00:00:00Z",
                  responsible_agent_id="7", responsible_agent_name="小王", assigned_agent_id="7",
                  assigned_agent_name="小王", assisted=True, responding_seats=2, is_lost=False,
                  handoff_at="2026-10-01T00:00:00Z", human_replied=True,
                  first_response_seconds=None, average_response_seconds=None, response_samples=0)
    rows = [message(1, direction="incoming", valid_content=True, **common),
            message(2, sender_role="agent", sender_id="7", direction="outgoing", valid_content=True,
                    sent_at="2026-10-01T00:02:00Z", **common)]
    _, catalog, _, _ = parse(build(tmp_path, rows))
    entry = catalog["entries"][0]
    metrics = entry["service_metrics"]
    assert entry["parts"] == 2
    assert metrics["duration_seconds"] == 600
    assert metrics["human_phase_message_count"] == 2
    assert metrics["human_phase_customer_count"] == metrics["human_phase_agent_count"] == 1
    assert metrics["last_message_by_agent"] is True
    assert metrics["first_response_status"] == "assisted"
    assert metrics["first_response_seconds"] is None and metrics["average_response_seconds"] is None
    assert metrics["responding_seat_count"] == 2 and metrics["assessed_agent_id"] == "7"
    assert entry["responsible_agent_name"] == "小王" and entry["assigned_agent_name"] == "小王"
    assert entry["assisted"] is True and entry["is_lost"] is False
    assert catalog["meta"]["rounds"] == 1 and catalog["meta"]["groups"] == 1


def test_unrounded_groups_do_not_count_as_rounds_or_receive_metrics(tmp_path):
    rows = [message(1), message(2, round_id=None), message(3, round_id=None),
            message(4, round_id=None, conversation_id="20")]
    _, catalog, _, _ = parse(build(tmp_path, rows))
    assert catalog["meta"]["rounds"] == 1
    assert catalog["meta"]["groups"] == 3
    assert catalog["meta"]["unrounded_messages"] == 3
    assert [e["index"] for e in catalog["entries"]] == [0, 1, 2]
    assert all(e.get("service_metrics") is None for e in catalog["entries"][1:])


def test_bundle_meta_uses_snapshot_and_requested_filter_context(tmp_path):
    scope = {"mode": "rounds", "summary": "关键词：退款", "view": "customers",
             "filters": {"as_of": "2026-10-01T00:00:00Z"}}
    rows = [message(1, snapshot_at="2026-10-01T00:05:00Z")]
    path = build_bundle(tmp_path / "work", rows, settings(tmp_path), MediaDownloader([]),
                        lambda **v: None, lambda: None, scope=scope)
    _, catalog, _, _ = parse(path)
    assert catalog["meta"]["snapshot_at"] == "2026-10-01T00:05:00Z"
    assert {key: catalog["meta"][key] for key in ("summary", "view", "as_of")} == {
        "summary": "关键词：退款", "view": "customers", "as_of": "2026-10-01T00:00:00Z"}



def test_empty_bundle_keeps_snapshot_metadata_after_extraction(tmp_path):
    metadata = {}
    def rows():
        # 快照在迭代时建立；空结果也必须保留该时刻。
        metadata["snapshot_at"] = "2026-10-01T00:05:00Z"
        yield from ()
    path = build_bundle(tmp_path / "work", rows(), settings(tmp_path), MediaDownloader([]),
                        lambda **v: None, lambda: None, metadata=metadata)
    _, catalog, _, _ = parse(path)
    assert catalog["meta"]["snapshot_at"] == "2026-10-01T00:05:00Z"
    assert catalog["meta"]["rounds"] == catalog["meta"]["unrounded_messages"] == 0
    assert catalog["entries"] == []


def test_metrics_are_built_after_extraction_without_database_reads(tmp_path, monkeypatch):
    import psycopg
    from quality.query.service_metrics import RoundServiceMetrics
    original = RoundServiceMetrics.observe
    extracted = False
    def forbid_database(*args, **kwargs):
        pytest.fail("生成页面阶段不应连接数据库")
    def observe(self, row):
        assert extracted
        return original(self, row)
    def rows():
        nonlocal extracted
        try:
            yield message(1, round_started_at="2026-10-01T00:00:00Z", round_status="resolved",
                          closed_at="2026-10-01T00:10:00Z", direction="incoming", valid_content=True)
        finally:
            extracted = True
            monkeypatch.setattr(psycopg, "connect", forbid_database)
    monkeypatch.setattr(RoundServiceMetrics, "observe", observe)
    _, catalog, _, _ = parse(build(tmp_path, rows()))
    assert catalog["entries"][0]["service_metrics"]["duration_seconds"] == 600



def test_progress_counts_true_rounds_and_unrounded_messages(tmp_path):
    updates = []
    build_bundle(tmp_path / "work", [message(1), message(2, round_id=None)], settings(tmp_path),
                 MediaDownloader([]), lambda **values: updates.append(values), lambda: None)
    assert updates[-1]["stage"] == "finishing"
    assert updates[-1]["rounds"] == 1
    assert updates[-1]["unrounded_messages"] == 1
