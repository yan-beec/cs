import unittest
from unittest.mock import patch

from ics_agent.app.chatwoot.client import ChatwootClient
from ics_agent.app.chatwoot.message_format import normalize_chatwoot_message
from ics_agent.app.core.config import AppSettings


class ChatwootImageFormatTest(unittest.TestCase):
    def test_original_faq_image_becomes_a_separate_markdown_image(self):
        source = '请先截图：<img src="https://qn-video.oxing.ren/20260526-194945.jpg">，再打开微信扫一扫。'
        self.assertEqual(
            normalize_chatwoot_message(source),
            '请先截图：\n\n![图片](https://qn-video.oxing.ren/20260526-194945.jpg)\n\n，再打开微信扫一扫。',
        )

    def test_copied_escaped_tag_smart_quotes_and_markdown_url(self):
        source = r'二维码：\<img src=“[https://example.com/qr.jpg](https://example.com/qr.jpg)”> &#x20;'
        result = normalize_chatwoot_message(source)
        self.assertIn('![图片](https://example.com/qr.jpg)', result)
        self.assertNotIn('<img', result)
        self.assertNotIn('&#x20;', result)
        self.assertNotIn('“', result)

    def test_entity_encoded_tag_and_alt_are_supported(self):
        source = '&lt;img src=&quot;https://example.com/q.jpg?a=1&amp;b=2&quot; alt=&quot;服务号二维码&quot;&gt;'
        self.assertEqual(
            normalize_chatwoot_message(source),
            '\n\n![服务号二维码](https://example.com/q.jpg?a=1&b=2)\n\n',
        )

    def test_chinese_path_spaces_and_parentheses_are_encoded(self):
        result = normalize_chatwoot_message('<img src="https://example.com/会员权益 (1).jpg">')
        self.assertIn('/%E4%BC%9A%E5%91%98%E6%9D%83%E7%9B%8A%20%281%29.jpg)', result)

    def test_multiple_images_preserve_order_and_strip_html_attributes(self):
        result = normalize_chatwoot_message(
            '<IMG SRC="https://example.com/a.jpg" alt="A" onclick="alert(1)" />'
            '说明'
            "<img src='https://example.com/b.jpg' alt='B'>"
        )
        self.assertLess(result.index('![A]'), result.index('说明'))
        self.assertLess(result.index('说明'), result.index('![B]'))
        self.assertNotIn('onclick', result)

    def test_ordinary_text_and_existing_markdown_are_unchanged(self):
        for source in ['普通回复。', '[新人指南](https://example.com/)', '![二维码](https://example.com/qr.jpg)', '![photo](https://example.com/a&#32;b.jpg)', '[link](https://example.com/a&#x20;b)']:
            with self.subTest(source=source):
                self.assertEqual(normalize_chatwoot_message(source), source)

    def test_code_samples_are_unchanged(self):
        for source in [
            '`<img src="https://example.com/a.jpg">`',
            '```html\n<img src="https://example.com/a.jpg"> &#x20;\n```',
            '~~~html\n<img src="https://example.com/a.jpg">\n~~~',
            '    <img src="https://example.com/a.jpg">',
            '``code with ` and <img src="https://example.com/a.jpg">``',
        ]:
            with self.subTest(source=source):
                self.assertEqual(normalize_chatwoot_message(source), source)

    def test_invalid_and_non_web_sources_do_not_become_images(self):
        for url in ['', 'javascript:alert(1)', 'data:image/png;base64,AA', 'file:///x.jpg', '//example.com/a.jpg', 'https:///a.jpg', 'https://user:pass@example.com/a.jpg', 'https://example.com:bad/a.jpg', 'https://example.com:65536/a.jpg', 'https://exa mple.com/a.jpg', 'https://999.999.999.999/a.jpg']:
            source = f'<img src="{url}">'
            with self.subTest(url=url):
                self.assertEqual(normalize_chatwoot_message(source), source)

    def test_quoted_url_entities_do_not_truncate_the_address(self):
        result = normalize_chatwoot_message(
            '<img src="https://example.com/image.png?text=&quot;hello&quot;">'
        )
        self.assertIn('https://example.com/image.png?text=%22hello%22)', result)

    def test_normalization_is_idempotent(self):
        once = normalize_chatwoot_message('<img src="https://example.com/a.jpg"> &#32;')
        self.assertEqual(normalize_chatwoot_message(once), once)

    def test_public_message_api_receives_image_markdown(self):
        client = ChatwootClient(AppSettings(_env_file=None))
        with patch.object(client, '_request_json', return_value={'id': 9}) as request:
            result = client.send_message(
                account_id='1', conversation_id='2',
                content='扫码：<img src="https://example.com/qr.jpg">',
            )
        self.assertEqual(result['id'], 9)
        payload = request.call_args.args[2]
        self.assertEqual(payload['content_type'], 'text')
        self.assertFalse(payload['private'])
        self.assertIn('![图片](https://example.com/qr.jpg)', payload['content'])

    def test_private_note_is_not_reformatted(self):
        client = ChatwootClient(AppSettings(_env_file=None, chatwoot_api_token='test-token'))
        source = '<img src="https://example.com/a.jpg">'
        with patch.object(client, '_request_json', return_value={}) as request:
            client.create_private_note(account_id='1', conversation_id='2', content=source)
        self.assertEqual(request.call_args.args[2]['content'], source)

    def test_markdown_renderer_builds_an_actual_image_element(self):
        try:
            from markdown_it import MarkdownIt
        except ImportError:
            self.skipTest('Optional Markdown renderer is unavailable.')
        renderer = MarkdownIt('commonmark', {'html': False})
        result = renderer.render(normalize_chatwoot_message(
            '<img src="https://example.com/qr.jpg" alt="服务号[二维码]">'
        ))
        self.assertIn('<img src="https://example.com/qr.jpg"', result)
        self.assertIn('alt="服务号', result)
        self.assertIn('二维码', result)
        self.assertNotIn('&lt;img', result)


if __name__ == '__main__':
    unittest.main()
