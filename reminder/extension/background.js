import { createBrowserController } from './core.mjs';

const controller = createBrowserController(chrome);
let port = null;
let commandChain = Promise.resolve();

async function connectionStatus(connected, message) {
  await chrome.storage.session.set({ connection: { connected, message } });
  await chrome.action.setBadgeText({ text: connected ? 'ON' : '' });
  await chrome.action.setBadgeBackgroundColor({ color: '#2563eb' });
}

function connect() {
  if (port) return;
  const current = chrome.runtime.connectNative('com.ics.reminder');
  port = current;
  current.onMessage.addListener(message => {
    if (message.type === 'ready') {
      connectionStatus(true, '已连接桌面提醒程序');
    } else if (message.type === 'offline') {
      connectionStatus(false, '请先启动桌面提醒程序，再点击重新连接');
      current.disconnect();
      if (port === current) port = null;
    } else if (message.type === 'command') {
      commandChain = commandChain.then(async () => {
        let result;
        try {
          result = message.action === 'probe' ? await controller.probe(message.url) :
            message.action === 'open' ? await controller.open(message.url, message.tabId) : { status: 'error' };
        } catch { result = { status: 'error' }; }
        if (port === current) current.postMessage({ type: 'reply', requestId: message.requestId, result });
      }).catch(() => {});
    }
  });
  current.onDisconnect.addListener(() => {
    const error = chrome.runtime.lastError?.message;
    if (port === current) port = null;
    connectionStatus(false, error ? '未连接：请确认安装包已安装、桌面程序已启动' : '桌面程序已断开，稍后自动重连');
  });
}

chrome.alarms.create('reconnect', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'reconnect') connect(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(() => { connect(); chrome.runtime.openOptionsPage(); });
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message.type === 'connect') { connect(); reply({ ok: true }); }
});
connect();
