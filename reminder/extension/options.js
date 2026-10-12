import { permissionPattern } from './core.mjs';
const site = document.querySelector('#site');
const message = document.querySelector('#message');

document.querySelector('#save').addEventListener('click', async () => {
  try {
    const url = new URL(site.value.trim());
    const pattern = permissionPattern(url.href);
    const granted = await chrome.permissions.request({ origins: [pattern] });
    if (!granted) { message.textContent = '站点授权未完成，请再次点击授权。'; return; }
    const previous = (await chrome.storage.local.get('site')).site;
    await chrome.storage.local.set({ site: url.origin });
    if (previous && permissionPattern(previous) !== pattern) {
      await chrome.permissions.remove({ origins: [permissionPattern(previous)] });
    }
    site.value = url.origin;
    message.textContent = '地址已保存。请登录 Chatwoot，收到提醒后点击“打开 Chatwoot 会话”。';
    await chrome.runtime.sendMessage({ type: 'connect' });
  } catch { message.textContent = '设置未完成，请检查地址或浏览器站点权限后重试。'; }
});
document.querySelector('#connect').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'connect' }));
function showConnection(value) { document.querySelector('#connection').textContent = value?.message || '尚未连接桌面程序'; }
chrome.storage.onChanged.addListener((changes, area) => { if (area === 'session' && changes.connection) showConnection(changes.connection.newValue); });
site.value = (await chrome.storage.local.get('site')).site || '';
showConnection((await chrome.storage.session.get('connection')).connection);
