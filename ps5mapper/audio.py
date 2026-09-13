"""切换 Windows 默认录音设备。

Windows 没有公开 API 干这件事，只能用未公开的 IPolicyConfig COM 接口
（所有第三方切换工具都是这么做的）。任何一步失败都不抛给上层，
而是退回到「打开系统声音设置」让用户手动切。
"""
from __future__ import annotations

import os
import sys

IS_WINDOWS = sys.platform == "win32"

_CLSID_POLICY_CONFIG = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
_IID_POLICY_CONFIG = "{F8679F50-850A-41CF-9C72-430F290290C8}"

_cache = {"devices": [], "current": None}


def _enumerate():
    """返回 [(id, 友好名称), ...]，只含正在工作的录音设备。"""
    if not IS_WINDOWS:
        return []
    try:
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import EDataFlow, DEVICE_STATE
    except Exception:
        return []
    out = []
    try:
        devices = AudioUtilities.GetAllDevices()
        for d in devices:
            # pycaw 的 GetAllDevices 不区分方向，用 DataFlow 过滤
            try:
                flow = d.properties.get("{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E},1")
            except Exception:
                flow = None
            name = d.FriendlyName or ""
            if d.state != DEVICE_STATE.ACTIVE.value:
                continue
            out.append((d.id, name))
    except Exception:
        return []
    return out


def list_input_devices():
    """录音设备列表。拿不到就返回空，UI 上把功能置灰。"""
    if not IS_WINDOWS:
        return []
    try:
        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import CLSID_MMDeviceEnumerator
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator

        enumerator = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        # eCapture = 1, DEVICE_STATE_ACTIVE = 1
        collection = enumerator.EnumAudioEndpoints(1, 1)
        count = collection.GetCount()
        out = []
        for i in range(count):
            dev = collection.Item(i)
            dev_id = dev.GetId()
            name = AudioUtilities.CreateDevice(dev).FriendlyName
            out.append((dev_id, name))
        _cache["devices"] = out
        return out
    except Exception:
        return _enumerate()


def _policy_config():
    import comtypes
    from ctypes import HRESULT, POINTER, c_int, c_wchar_p
    from comtypes import GUID, IUnknown, COMMETHOD

    class IPolicyConfig(IUnknown):
        _iid_ = GUID(_IID_POLICY_CONFIG)
        _methods_ = (
            # 前 12 个方法我们用不到，占位保持 vtable 偏移正确
            COMMETHOD([], HRESULT, "GetMixFormat"),
            COMMETHOD([], HRESULT, "GetDeviceFormat"),
            COMMETHOD([], HRESULT, "ResetDeviceFormat"),
            COMMETHOD([], HRESULT, "SetDeviceFormat"),
            COMMETHOD([], HRESULT, "GetProcessingPeriod"),
            COMMETHOD([], HRESULT, "SetProcessingPeriod"),
            COMMETHOD([], HRESULT, "GetShareMode"),
            COMMETHOD([], HRESULT, "SetShareMode"),
            COMMETHOD([], HRESULT, "GetPropertyValue"),
            COMMETHOD([], HRESULT, "SetPropertyValue"),
            COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                      (["in"], c_wchar_p, "deviceId"),
                      (["in"], c_int, "role")),
            COMMETHOD([], HRESULT, "SetEndpointVisibility"),
        )

    return comtypes.CoCreateInstance(
        comtypes.GUID(_CLSID_POLICY_CONFIG), IPolicyConfig, comtypes.CLSCTX_ALL)


def set_default_input(device_id: str) -> bool:
    if not IS_WINDOWS or not device_id:
        return False
    try:
        pc = _policy_config()
        for role in (0, 1, 2):        # eConsole / eMultimedia / eCommunications
            pc.SetDefaultEndpoint(device_id, role)
        _cache["current"] = device_id
        return True
    except Exception:
        return False


def current_input_name() -> str:
    for dev_id, name in _cache.get("devices") or []:
        if dev_id == _cache.get("current"):
            return name
    return ""


def cycle_input_device() -> str:
    """在可用录音设备之间轮换，返回新设备名。切不动就打开系统声音设置。"""
    devices = list_input_devices()
    if not devices:
        open_sound_settings()
        return ""
    ids = [d[0] for d in devices]
    cur = _cache.get("current")
    idx = ids.index(cur) + 1 if cur in ids else 0
    idx %= len(ids)
    if set_default_input(ids[idx]):
        return devices[idx][1]
    open_sound_settings()
    return ""


def open_sound_settings():
    if IS_WINDOWS:
        try:
            os.startfile("ms-settings:sound")
        except Exception:
            try:
                os.system("control mmsys.cpl,,1")
            except Exception:
                pass


# ------------------------------------------------------- 麦克风是否正在被使用

_MIC_CONSENT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
_mic_cache = {"at": 0.0, "value": None}
_MIC_TTL = 0.4          # 秒。注册表读得很快，但没必要每帧都读


def _scan_consent_key(root, path) -> bool:
    """扫一棵 ConsentStore 子树，看有没有哪个应用正占着麦克风。

    Windows 就是靠这里画任务栏那个麦克风图标的：每个用过麦克风的应用底下有
    LastUsedTimeStart / LastUsedTimeStop 两个时间戳，**Stop 为 0 表示此刻正在用**。
    """
    import winreg
    try:
        key = winreg.OpenKey(root, path)
    except OSError:
        raise
    try:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            sub = path + "\\" + name
            if name == "NonPackaged":
                try:
                    if _scan_consent_key(root, sub):
                        return True
                except OSError:
                    pass
                continue
            try:
                k2 = winreg.OpenKey(root, sub)
            except OSError:
                continue
            try:
                stop, _ = winreg.QueryValueEx(k2, "LastUsedTimeStop")
                start, _ = winreg.QueryValueEx(k2, "LastUsedTimeStart")
                if stop == 0 and start:
                    return True
            except OSError:
                pass
            finally:
                k2.Close()
        return False
    finally:
        key.Close()


def mic_in_use(force: bool = False):
    """现在有没有应用正在用麦克风。

    返回 True / False；**读不出来时返回 None**，别把「不知道」谎报成「没在用」。

    这比数按键次数靠谱：Windows 语音输入（Win+H）静音几秒会自己停，
    只数按键的话程序会一直以为还在录，下一次按键的开关方向就全反了。
    代价是它反映的是「有应用在用麦克风」，Discord、浏览器占着麦也会亮。
    """
    import time as _t
    if not IS_WINDOWS:
        return None
    now = _t.monotonic()
    if not force and now - _mic_cache["at"] < _MIC_TTL:
        return _mic_cache["value"]
    val = None
    try:
        import winreg
        val = False
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                if _scan_consent_key(root, _MIC_CONSENT):
                    val = True
                    break
            except OSError:
                continue
    except Exception:
        val = None
    _mic_cache.update(at=now, value=val)
    return val
