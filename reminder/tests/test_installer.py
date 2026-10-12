import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from reminder.packaging import installer_core as installer
from reminder.client.bridge import EXTENSION_ORIGIN


@pytest.fixture(autouse=True)
def isolated_installation(tmp_path, monkeypatch):
    """安装测试不访问本机的真实安装登记或设备配置。"""
    monkeypatch.setenv("APPDATA", str(tmp_path / "profile"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(), KEY_WOW64_32KEY=0x200, KEY_WOW64_64KEY=0x100,
        KEY_WRITE=0x20006, REG_SZ=1, REG_DWORD=4,
        OpenKey=MagicMock(side_effect=FileNotFoundError), QueryValueEx=MagicMock(),
        CreateKeyEx=MagicMock(), SetValueEx=MagicMock(),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    return registry


def payload(tmp_path):
    path = tmp_path / "payload.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ICSReminder.exe", b"desktop")
        archive.writestr("ICSBrowserHost.exe", b"native-host")
        archive.writestr("extension/manifest.json", '{}')
        archive.writestr("安装说明.html", "插件：{{EXTENSION_DIR}}")
    return path


def test_install_places_files_and_registers_only_this_extension(tmp_path):
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"setup")
    root = tmp_path / "含 空格的安装目录"
    with (patch.object(installer, "write_registry") as registry,
          patch.object(installer, "create_shortcut") as shortcut):
        installer.install(payload(tmp_path), root, setup)
    registry.assert_called_once_with(root)
    shortcut.assert_called_once_with(root)
    host = json.loads((root / "native-host.json").read_text(encoding="utf-8"))
    assert host["path"] == str(root / "ICSBrowserHost.exe")
    assert host["allowed_origins"] == [EXTENSION_ORIGIN]
    assert "{{EXTENSION_DIR}}" not in (root / "安装说明.html").read_text(encoding="utf-8")
    assert (root / "Uninstall.exe").read_bytes() == b"setup"


def test_failed_registration_restores_previous_install(tmp_path):
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"setup")
    root = tmp_path / "installed"
    root.mkdir()
    (root / "install.json").write_text('{"product":"ICSReminder"}')
    (root / "ICSReminder.exe").write_bytes(b"old version")
    with (patch.object(installer, "write_registry", side_effect=[OSError("registry failure"), None]),
          patch.object(installer, "remove_registry"), patch.object(installer, "create_shortcut")):
        with pytest.raises(OSError):
            installer.install(payload(tmp_path), root, setup)
    assert (root / "ICSReminder.exe").read_bytes() == b"old version"
    assert not list(tmp_path.glob("installed-install-*"))


def test_invalid_payload_cannot_escape_install_directory(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("../outside", b"bad")
    with pytest.raises(ValueError):
        installer.extract_payload(archive, tmp_path / "destination")
    assert not (tmp_path / "outside").exists()


def test_install_refuses_unrelated_directory(tmp_path):
    root = tmp_path / "another-app"
    root.mkdir()
    (root / "file").write_text("keep")
    with pytest.raises(ValueError):
        installer.install(payload(tmp_path), root, tmp_path / "setup.exe")
    assert (root / "file").read_text() == "keep"


def test_uninstall_removes_registration_and_files_but_preserves_device_config(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "profile"))
    device = Path(installer.os.environ["APPDATA"]) / "ICSReminder/device.json"
    device.parent.mkdir(parents=True)
    device.write_text("preserve")
    root = tmp_path / "installed"
    root.mkdir()
    (root / "install.json").write_text('{"product":"ICSReminder"}')
    (root / "Uninstall.exe").write_bytes(b"test")
    (root / "extension").mkdir()
    (root / "extension/manifest.json").write_text('{}')
    with patch.object(installer, "remove_registry") as registry:
        assert installer.uninstall(root) is False
    registry.assert_called_once_with(root)
    assert not root.exists()
    assert device.read_text() == "preserve"


def test_install_accepts_existing_empty_directory(tmp_path):
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"setup")
    root = tmp_path / "chosen-directory"
    root.mkdir()
    with patch.object(installer, "write_registry"), patch.object(installer, "create_shortcut"):
        installer.install(payload(tmp_path), root, setup)
    assert (root / "ICSReminder.exe").read_bytes() == b"desktop"
    assert not list(tmp_path.glob("chosen-directory-previous-*"))


def test_failed_install_restores_empty_directory_without_registering_missing_program(tmp_path):
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"setup")
    root = tmp_path / "chosen-directory"
    root.mkdir()
    with (patch.object(installer, "write_registry", side_effect=OSError("registry failure")) as registry,
          patch.object(installer, "remove_registry") as remove,
          patch.object(installer, "create_shortcut") as shortcut):
        with pytest.raises(OSError, match="registry failure"):
            installer.install(payload(tmp_path), root, setup)
    registry.assert_called_once_with(root)
    remove.assert_called_once_with(root)
    shortcut.assert_not_called()
    assert root.is_dir()
    assert not list(root.iterdir())
    assert not list(tmp_path.glob("chosen-directory-previous-*"))


@pytest.mark.parametrize("value", ["", "   ", "relative-folder", "C:relative-folder"])
def test_invalid_install_path_is_rejected_before_writing(tmp_path, value):
    with pytest.raises(ValueError):
        installer.validate_install_dir(value)
    assert not list(tmp_path.iterdir())


def test_disk_root_and_device_config_overlap_are_rejected(tmp_path):
    device = tmp_path / "profile" / installer.PRODUCT
    for root in (Path(tmp_path.anchor), device, device.parent, device / "client"):
        with pytest.raises(ValueError):
            installer.validate_install_dir(root)
    assert not list(tmp_path.iterdir())


def test_file_and_invalid_ownership_marker_are_rejected(tmp_path):
    file = tmp_path / "file"
    file.write_text("keep")
    with pytest.raises(ValueError):
        installer.validate_install_dir(file)
    root = tmp_path / "unrelated"
    root.mkdir()
    for marker in ('{"product":"OtherApp"}', '[]', 'not json'):
        (root / "install.json").write_text(marker)
        with pytest.raises(ValueError):
            installer.validate_install_dir(root)
        assert (root / "install.json").read_text() == marker
    assert file.read_text() == "keep"


def test_registered_custom_directory_is_used_for_upgrade(tmp_path, isolated_installation):
    root = tmp_path / "客服 客户端"
    root.mkdir()
    (root / "install.json").write_text('{"product":"ICSReminder"}')
    isolated_installation.OpenKey.side_effect = None
    isolated_installation.QueryValueEx.return_value = (str(root), isolated_installation.REG_SZ)
    assert installer.preferred_install_dir() == root
    assert installer.validate_install_dir(root) == root
    with pytest.raises(ValueError, match="先卸载旧版"):
        installer.install(payload(tmp_path), tmp_path / "new-location", tmp_path / "setup.exe")
    assert not (tmp_path / "new-location").exists()
    assert (root / "install.json").exists()


@pytest.mark.parametrize("marker", [None, '{"product":"OtherApp"}', '[]', 'not json'])
def test_stale_install_registration_does_not_choose_unrelated_directory(tmp_path, isolated_installation, marker):
    root = tmp_path / "stale"
    root.mkdir()
    if marker is not None:
        (root / "install.json").write_text(marker)
    isolated_installation.OpenKey.side_effect = None
    isolated_installation.QueryValueEx.return_value = (str(root), isolated_installation.REG_SZ)
    assert installer.preferred_install_dir() == installer.default_install_dir()


def test_custom_path_is_used_in_browser_and_uninstall_registration(tmp_path, isolated_installation):
    root = tmp_path / "客服 安装目录"
    installer.write_registry(root)
    values = [call.args[1:] for call in isolated_installation.SetValueEx.call_args_list]
    assert values.count(("", 0, isolated_installation.REG_SZ, str(root / "native-host.json"))) == 4
    assert ("InstallLocation", 0, isolated_installation.REG_SZ, str(root)) in values
    assert ("UninstallString", 0, isolated_installation.REG_SZ, f'"{root / "Uninstall.exe"}" --uninstall') in values


def test_custom_path_is_used_in_shortcut_arguments(tmp_path):
    root = tmp_path / "客服 安装目录"
    with patch.object(installer.subprocess, "run") as run:
        installer.create_shortcut(root)
    config = json.loads(run.call_args.kwargs["input"])
    assert config["executable"] == str(root / "ICSReminder.exe")
    assert config["directory"] == str(root)


@pytest.fixture
def wizard(monkeypatch):
    """替换图形控件验证真实按钮流程，不打开窗口或写入系统登记。"""
    from reminder.packaging import setup_entrypoint as setup

    root, entry, status = MagicMock(), MagicMock(), MagicMock()
    value = [""]
    entry.insert.side_effect = lambda index, text: value.__setitem__(0, text)
    entry.delete.side_effect = lambda *args: value.__setitem__(0, "")
    entry.get.side_effect = lambda: value[0]
    buttons = {}

    def button(*args, **kwargs):
        control = MagicMock()
        buttons[kwargs["text"]] = (control, kwargs["command"])
        return control

    monkeypatch.setattr(setup.ctk, "set_appearance_mode", MagicMock())
    for name in ("CTkFrame", "CTkLabel", "CTkFont"):
        monkeypatch.setattr(setup.ctk, name, MagicMock())
    monkeypatch.setattr(setup.ctk, "CTk", MagicMock(return_value=root))
    monkeypatch.setattr(setup.ctk, "CTkEntry", MagicMock(return_value=entry))
    monkeypatch.setattr(setup.ctk, "StringVar", MagicMock(return_value=status))
    monkeypatch.setattr(setup.ctk, "CTkButton", button)
    monkeypatch.setattr(setup, "browser_executable", lambda browser: None)
    monkeypatch.setattr(setup.sys, "argv", ["setup.exe"])
    monkeypatch.setattr(setup.threading, "Thread", lambda target, daemon: SimpleNamespace(start=target))
    install = MagicMock()
    launch, guide = MagicMock(), MagicMock()
    monkeypatch.setattr(setup, "install", install)
    monkeypatch.setattr(setup.subprocess, "Popen", launch)
    monkeypatch.setattr(setup.webbrowser, "open", guide)
    return SimpleNamespace(setup=setup, root=root, entry=entry, status=status, buttons=buttons,
                           install=install, launch=launch, guide=guide)


def test_wizard_browsed_directory_is_used_for_install_launch_and_guide(tmp_path, wizard, monkeypatch):
    selected = tmp_path / "客服 程序"
    monkeypatch.setattr(wizard.setup.filedialog, "askdirectory", MagicMock(return_value=str(selected)))
    wizard.setup.main()
    wizard.buttons["选择安装目录"][1]()
    assert wizard.entry.get() == str(selected)
    wizard.buttons["安装到本机"][1]()
    assert wizard.install.call_args.args[1] == selected
    wizard.entry.configure.assert_called_with(state="disabled")
    wizard.buttons["选择安装目录"][0].configure.assert_called_with(state="disabled")
    wizard.root.after.call_args.args[1]()
    wizard.buttons["启动提醒程序"][1]()
    wizard.launch.assert_called_once_with([str(selected / "ICSReminder.exe")], cwd=selected)
    wizard.buttons["查看插件安装说明"][1]()
    wizard.guide.assert_called_once_with((selected / "安装说明.html").as_uri())


def test_wizard_failed_install_allows_new_directory_and_retry(tmp_path, wizard):
    wizard.setup.main()
    wizard.entry.delete(0, "end")
    wizard.entry.insert(0, str(tmp_path / "first"))
    wizard.install.side_effect = [PermissionError("denied"), None]
    wizard.buttons["安装到本机"][1]()
    wizard.root.after.call_args.args[1]()
    wizard.entry.configure.assert_called_with(state="normal")
    wizard.buttons["选择安装目录"][0].configure.assert_called_with(state="normal")
    assert "写入权限" in wizard.status.set.call_args.args[0]
    wizard.entry.delete(0, "end")
    wizard.entry.insert(0, str(tmp_path / "second"))
    wizard.buttons["安装到本机"][1]()
    wizard.root.after.call_args.args[1]()
    assert wizard.install.call_args.args[1] == tmp_path / "second"
    assert "安装完成" in wizard.status.set.call_args.args[0]


def test_wizard_rejects_relative_input_without_starting_install(wizard):
    wizard.setup.main()
    wizard.entry.delete(0, "end")
    wizard.entry.insert(0, "relative-path")
    wizard.buttons["安装到本机"][1]()
    wizard.install.assert_not_called()
    assert "完整的安装目录" in wizard.status.set.call_args.args[0]
