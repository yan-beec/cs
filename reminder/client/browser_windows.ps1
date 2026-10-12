# Windows 自带 UI Automation；不读取 Cookie、密码或浏览器配置目录。
# 输入仅为 JSON 会话地址，输出仅为状态；地址绝不拼接到脚本或按键表达式。
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    Add-Type -ReferencedAssemblies UIAutomationClient,UIAutomationTypes,WindowsBase,System.Windows.Forms -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Automation;
using System.Windows.Forms;

public static class ReminderBrowser {
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr handle);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr handle, int command);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr handle);
    static readonly Stopwatch Timer = new Stopwatch();

    // 只遍历浏览器外壳，跳过网页 Document，避免读取客户消息或网页输入框。
    static List<AutomationElement> Shell(AutomationElement root) {
        var result = new List<AutomationElement>();
        var pending = new Queue<AutomationElement>();
        pending.Enqueue(root);
        while (pending.Count > 0 && result.Count < 600 && Timer.ElapsedMilliseconds < 10000) {
            var element = pending.Dequeue();
            if (element.Current.ControlType == ControlType.Document) continue;
            result.Add(element);
            var child = TreeWalker.ControlViewWalker.GetFirstChild(element);
            while (child != null) {
                pending.Enqueue(child);
                child = TreeWalker.ControlViewWalker.GetNextSibling(child);
            }
        }
        return result;
    }

    static AutomationElement Address(List<AutomationElement> shell) {
        foreach (var element in shell) {
            if (element.Current.ControlType != ControlType.Edit) continue;
            // Chrome 的本地化名称、Edge 的稳定 AutomationId；不碰页面表单。
            string name = element.Current.Name ?? "";
            string id = element.Current.AutomationId ?? "";
            if (id == "addressEditBox" || name.IndexOf("Address and search", StringComparison.OrdinalIgnoreCase) >= 0
                || name.IndexOf("Search or enter web address", StringComparison.OrdinalIgnoreCase) >= 0
                || name.IndexOf("地址", StringComparison.Ordinal) >= 0) return element;
        }
        return null;
    }

    static string Read(AutomationElement address) {
        object pattern;
        if (address != null && address.TryGetCurrentPattern(ValuePattern.Pattern, out pattern))
            return ((ValuePattern)pattern).Current.Value;
        return "";
    }

    static string AccountPath(Uri uri) {
        string path = uri.AbsolutePath;
        string marker = "/app/accounts/";
        int start = path.IndexOf(marker, StringComparison.Ordinal);
        if (start < 0) return "";
        int end = path.IndexOf('/', start + marker.Length);
        return end < 0 ? path.TrimEnd('/') : path.Substring(0, end);
    }

    static bool Matches(string value, Uri target) {
        Uri current;
        // Chromium 地址栏未聚焦时可能省略 http(s) 前缀；补回目标协议再比较。
        if (!value.Contains("://")) value = target.Scheme + "://" + value;
        if (!Uri.TryCreate(value, UriKind.Absolute, out current)) return false;
        return current.Scheme == target.Scheme && current.Host.Equals(target.Host, StringComparison.OrdinalIgnoreCase)
            && current.Port == target.Port && String.IsNullOrEmpty(current.UserInfo)
            && AccountPath(target) != "" && AccountPath(current) == AccountPath(target);
    }

    static bool Selected(AutomationElement tab) {
        object pattern;
        return tab.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern)
            && ((SelectionItemPattern)pattern).Current.IsSelected;
    }

    static bool Select(AutomationElement tab) {
        object pattern;
        if (tab.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern)) {
            ((SelectionItemPattern)pattern).Select();
            return true;
        }
        if (tab.TryGetCurrentPattern(InvokePattern.Pattern, out pattern)) {
            ((InvokePattern)pattern).Invoke();
            return true;
        }
        return false;
    }

    static bool Navigate(AutomationElement window, Uri target) {
        IntPtr handle = new IntPtr(window.Current.NativeWindowHandle);
        if (IsIconic(handle)) ShowWindow(handle, 9);
        SetForegroundWindow(handle);
        Thread.Sleep(100);
        if (GetForegroundWindow() != handle) return false;
        var address = Address(Shell(window));
        // 选中后重新核对实际 URL，防止标签在检测期间被关闭/切换。
        if (!Matches(Read(address), target)) return false;
        object pattern;
        if (!address.TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) return false;
        address.SetFocus();
        ((ValuePattern)pattern).SetValue(target.AbsoluteUri);
        if (GetForegroundWindow() != handle || Read(address) != target.AbsoluteUri) return false;
        SendKeys.SendWait("{ENTER}");
        return true;
    }

    public static string Open(string url) {
        Timer.Restart();
        var target = new Uri(url);
        bool unavailable = false;
        var windows = AutomationElement.RootElement.FindAll(TreeScope.Children, Condition.TrueCondition);
        foreach (AutomationElement window in windows) {
            IntPtr handle = IntPtr.Zero;
            bool minimized = false;
            bool kept = false;
            try {
                var process = Process.GetProcessById(window.Current.ProcessId);
                if (process.ProcessName != "chrome" && process.ProcessName != "msedge") continue;
                if (window.Current.ClassName != "Chrome_WidgetWin_1") continue;
                handle = new IntPtr(window.Current.NativeWindowHandle);
                minimized = IsIconic(handle);
                // 最小化的 Chromium 窗口可能不暴露地址栏；临时恢复后检测。
                if (minimized) { ShowWindow(handle, 4); Thread.Sleep(150); }
                var shell = Shell(window);
                var address = Address(shell);
                if (address == null) { unavailable = true; continue; }
                if (Matches(Read(address), target)) {
                    kept = Navigate(window, target);
                    return kept ? "reused" : "error";
                }

                var tabs = shell.FindAll(e => e.Current.ControlType == ControlType.TabItem);
                var original = tabs.Find(Selected);
                // 无法恢复原标签的窗口不遍历，以免改变用户正在处理的页面。
                if (tabs.Count > 0 && original == null) { unavailable = true; continue; }
                try {
                    foreach (var tab in tabs) {
                        if (Selected(tab)) continue;
                        if (Timer.ElapsedMilliseconds >= 10000) { unavailable = true; break; }
                        if (!Select(tab)) { unavailable = true; continue; }
                        Thread.Sleep(120);
                        if (Matches(Read(Address(Shell(window))), target)) {
                            kept = Navigate(window, target);
                            return kept ? "reused" : "error";
                        }
                    }
                } finally {
                    if (!kept && original != null) Select(original);
                }
            } catch (ElementNotAvailableException) { unavailable = true; }
              catch (InvalidOperationException) { unavailable = true; }
              catch (ArgumentException) { /* 关闭的进程/窗口：继续检测其他窗口。 */ }
              finally { if (minimized && !kept) ShowWindow(handle, 6); }
        }
        return unavailable ? "error" : "not_found";
    }
}
'@
    $status = [ReminderBrowser]::Open([string]$request.url)
    @{status = $status} | ConvertTo-Json -Compress
} catch {
    # 不把浏览器页面地址或异常明细写到客户端输出。
    '{"status":"error"}'
    exit 1
}
