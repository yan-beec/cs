export function chatwootTarget(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('无效的 Chatwoot 会话地址');
  }
  const match = url.pathname.match(/^(.*?)\/app\/accounts\/(\d+)\/(?:dashboard|conversations\/\d+)\/?$/);
  if (!match) throw new Error('地址不是 Chatwoot 工作台或会话');
  return { url, prefix: match[1], accountId: match[2], accountPath: `${match[1]}/app/accounts/${match[2]}` };
}

export function sameAccount(value, target) {
  try {
    const current = new URL(value);
    return current.origin === target.url.origin && !current.username && !current.password &&
      (current.pathname === target.accountPath || current.pathname.startsWith(`${target.accountPath}/`));
  } catch { return false; }
}

export function permissionPattern(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('请输入 HTTP/HTTPS 地址');
  // 浏览器的站点权限不区分端口；实际检测仍严格比较 origin（含端口）。
  return `${url.protocol}//${url.hostname}/*`;
}

// 此函数只在已授权的 Chatwoot 页面内执行。凭据留在页面，不返回给扩展或桌面端。
export async function verifyChatwootSession(origin, accountId, prefix) {
  if (location.origin !== origin) return { status: 'error' };
  const apiBase = new URL(window.chatwootConfig?.apiHost || `${prefix}/`, origin);
  if (apiBase.origin !== origin) return { status: 'error' };
  const endpoint = new URL(`${apiBase.pathname.replace(/\/$/, '')}/auth/validate_token`, origin).href;
  try {
    let data;
    if (typeof window.axios?.get === 'function') {
      const response = await window.axios.get(endpoint, { timeout: 3500 });
      data = response.data;
    } else {
      const cookie = document.cookie.split('; ').find(value => value.startsWith('cw_d_session_info='));
      if (!cookie) return { status: 'logged_out' };
      const auth = JSON.parse(decodeURIComponent(cookie.slice(cookie.indexOf('=') + 1)));
      const headers = {};
      for (const key of ['access-token', 'token-type', 'client', 'uid', 'expiry']) {
        if (typeof auth[key] === 'string') headers[key] = auth[key];
      }
      if (!headers['access-token'] || !headers.client || !headers.uid) return { status: 'logged_out' };
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3500);
      try {
        const response = await fetch(endpoint, { credentials: 'same-origin', redirect: 'error', headers, signal: controller.signal });
        if (response.status === 401 || response.status === 403) return { status: 'logged_out' };
        if (!response.ok) return { status: 'error' };
        data = await response.json();
      } finally { clearTimeout(timer); }
    }
    const user = data?.payload?.data || data?.data;
    return { status: user?.id && user.accounts?.some(account => String(account.id) === accountId) ? 'logged_in' : 'logged_out' };
  } catch (error) {
    return { status: [401, 403].includes(error?.response?.status) ? 'logged_out' : 'error' };
  }
}

export function createBrowserController(api) {
  async function targetFor(value) {
    const target = chatwootTarget(value);
    const settings = await api.storage.local.get('site');
    if (settings.site && settings.site !== target.url.origin) return { failure: { status: 'site_mismatch' } };
    if (!settings.site || !await api.permissions.contains({ origins: [permissionPattern(value)] })) return { failure: { status: 'no_permission' } };
    return { target };
  }

  async function loggedIn(tab, target) {
    const results = await api.scripting.executeScript({
      target: { tabId: tab.id }, world: 'MAIN', func: verifyChatwootSession,
      args: [target.url.origin, target.accountId, target.prefix],
    });
    return results[0]?.result?.status || 'error';
  }

  return {
    async probe(value) {
      const { target, failure } = await targetFor(value);
      if (failure) return failure;
      const tabs = (await api.tabs.query({})).filter(tab => !tab.incognito && sameAccount(tab.url, target));
      const checked = await Promise.all(tabs.map(async tab => {
        try { return { tab, status: await loggedIn(tab, target) }; }
        catch { return { tab, status: 'error' }; }
      }));
      const candidates = checked.filter(item => item.status === 'logged_in');
      candidates.sort((a, b) => Number(b.tab.url === target.url.href) - Number(a.tab.url === target.url.href) || Number(b.tab.active) - Number(a.tab.active));
      if (candidates.length) {
        const tab = candidates[0].tab;
        return { status: 'candidate', tabId: tab.id, exact: tab.url === target.url.href, active: tab.active };
      }
      if (checked.some(item => item.status === 'error')) return { status: 'error' };
      return { status: tabs.length ? 'login_required' : 'no_tab' };
    },
    async open(value, tabId) {
      const { target, failure } = await targetFor(value);
      if (failure) return failure;
      let tab, reused = false;
      if (Number.isInteger(tabId)) {
        tab = await api.tabs.get(tabId);
        // 导航前再次核验，避免检测后用户关闭、改地址或退出登录。
        if (!sameAccount(tab.url, target) || await loggedIn(tab, target) !== 'logged_in') return { status: 'error' };
        tab = await api.tabs.update(tabId, { url: target.url.href, active: true });
        reused = true;
      } else {
        // 检测阶段之后如果又出现工作台，不再创建重复标签，让用户重试。
        const latest = await this.probe(value);
        if (latest.status !== 'no_tab') return { status: 'error' };
        tab = await api.tabs.create({ url: target.url.href, active: true });
      }
      const window = await api.windows.get(tab.windowId);
      await api.windows.update(tab.windowId, { focused: true, ...(window.state === 'minimized' ? { state: 'normal' } : {}) });
      return { status: reused ? 'reused' : 'opened' };
    },
  };
}
