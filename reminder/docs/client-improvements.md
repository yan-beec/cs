# 提醒客户端优化

提醒弹窗默认 `500×360`，可拖动窗口边框调整，最小 `400×300`。本次运行内收起和再次提醒沿用调整后的尺寸；重新启动恢复默认大小。长消息可滚动查看，底部操作按钮固定保留。

“打开 Chatwoot 会话”先检测当前 Windows 用户已打开的 Google Chrome / Microsoft Edge 窗口和标签页，查找与目标会话协议、域名、端口和 Chatwoot 账户一致的工作台页面（`/app/accounts/<账户>`）。找到后复用原标签页并收起提醒弹窗；确认仍需单独点击“确认收到”。登录页和其他账户的页面不复用；明确未找到时才使用默认浏览器打开。检测期间可能短暂切换标签，未匹配的窗口恢复原标签。

检测使用 Windows 自带 UI Automation，无需浏览器插件、远程调试端口或新增依赖，不读取浏览器 Cookie 或密码。工作台地址用于识别已登录页面；若登录会话已经过期，Chatwoot 仍会要求重新登录。其他浏览器使用默认打开方式。辅助接口不可用或检测超时时，弹窗提示重试，避免重复开页。

在项目根目录使用指定环境运行：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m reminder.client.main --demo
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m reminder.client.main
```

使用 exe 时先从系统托盘退出旧客户端，再启动 `reminder/dist/ICSReminder.exe`。只关窗口会隐藏到托盘，不能切换到新版。

真实浏览器验收：先在 Chrome/Edge 登录目标 Chatwoot 账户，将其标签切到后台，收到真实会话提醒后点击“打开 Chatwoot 会话”，确认原标签变为目标会话且标签数量不增加；再检查没有工作台、只有登录页、其他账户和检测异常的情况。
