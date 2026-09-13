# DualSense Mapper

Map a PS5 DualSense controller to Windows keyboard and mouse input.

**English** · [中文](#中文说明)

---

Every button is remappable. Adaptive trigger resistance, touchpad gestures, gyro
aiming, and full lighting control. Portable — no installer, no autostart.
Close the app and the controller instantly goes back to being an ordinary gamepad,
so it does not interfere with actually playing games.

Built for desktop use and chat typing rather than gaming: the original motivation
was driving a Windows desktop from the couch, with voice dictation bound to the
mic-mute button.

## Requirements

| | |
|---|---|
| OS | Windows 10 / 11 |
| Python | **3.10 – 3.13, a normal build.** Free-threaded builds (3.13t, 3.14t) will not work — the C extensions have no matching wheels. **3.12 recommended** |
| Runtime | Edge WebView2 — ships with Windows 10/11, nothing to install |
| Hardware | DualSense controller (`0x054C:0x0CE6` or `0x0DF2`), USB or Bluetooth |

> **All dependencies install into a project-local `.venv\`. Your system Python is never touched.**
> This is not fastidiousness — an early version installed globally and overwrote a
> user's Pillow, breaking unrelated software on their machine.

## Quick start

Double-click, from the project root:

| Script | What it does |
|---|---|
| **`run.bat`** | Start the app. First run creates `.venv\` and installs dependencies — takes a few minutes |
| **`check.bat`** | Run this first when something breaks. Lists every Python on the machine, which one was picked, what is installed in `.venv`, and whether the controller is detected |
| **`release.bat`** | Build the portable package into `output\DualSenseMapper\` plus a matching `.zip` |

From a shell instead:

```bat
.venv\Scripts\python.exe main.py
```

End-user documentation (how the UI works, every feature explained) is in
[`使用说明.md`](./使用说明.md) — bilingual, English first.

## Project layout

```
.
├── main.py                   entry point
├── run.bat / check.bat / release.bat
├── requirements.txt
├── 使用说明.md               end-user guide (EN + 中文)
│
├── ps5mapper/                the package
│   ├── app.py                pywebview shell + JS API bridge
│   ├── engine.py             main loop: read HID -> parse -> dispatch actions
│   ├── dualsense.py          HID report parsing and assembly (USB + Bluetooth)
│   ├── config.py             config layering: factory / baked / user
│   ├── triggers.py           adaptive triggers: wall position and fire point
│   ├── gyro.py               gyro aiming
│   ├── lights.py             lightbar / player LEDs / mic LED (pure functions)
│   ├── winput.py             SendInput wrapper
│   ├── audio.py              recording device switching, mic-in-use detection
│   ├── default_config.json   factory defaults
│   └── ui/
│       ├── index.html        the entire UI, single file
│       ├── i18n.js           i18n runtime
│       └── lang/             zh-TW / en string tables
│
├── tests/                    unit tests — pure logic, no controller needed
└── tools/
    ├── make_bats.py          the single source for every .bat, plus its validator
    ├── bake_defaults.py      bake a config file into the factory defaults
    ├── build_i18n.py         generate language files
    └── audit_i18n.py         find untranslated strings
```

## Running the tests

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

All tests are pure logic and **do not need a controller connected**. Run them
before and after any change.

## Read this before you start changing things

These are all real bugs that cost real time. Look here before touching the
relevant area.

### 1. The `Api` class must have no public non-method attributes

pywebview 6 walks `js_api` recursively, treating any public non-method attribute
as a nested API to descend into. Once `Api.app → App.api → Api` forms a cycle,
it recurses forever: the main thread locks up, the window goes "not responding",
and the frontend receives an empty object — **with no error message pointing at
the actual cause.** A test locks this rule in place.

### 2. Three functions in `ui/index.html` mirror Python

`wallSpan()` / `firePercent()` / `hasBreakthrough()` correspond to the identically
named functions in `ps5mapper/triggers.py`. **Change one, you must change the
other**, or the trigger resistance wall drawn in the UI will not match where the
key actually fires.

### 3. The lightbar must be claimed before it responds

After power-on the firmware holds the lightbar and player LEDs, and ignores every
RGB value the host sends. You must first send a lightbar setup report
(`dualsense.build_lightbar_reset()`). The symptom of forgetting is distinctive:
**the mic LED works, but the lightbar and player LEDs do nothing at all.**

### 4. The touchpad reports ghost contacts

After a finger lifts, roughly a quarter to a third of frames still report "touched",
with the coordinates frozen. The 3-frame debounce and 300 ms ghost filter
**cannot be removed** — without them, a one-finger swipe gets paired into a
phantom second contact and is misread as a two-finger scroll.

### 5. Never hand-write a `.bat` — go through `tools/make_bats.py`

cmd.exe does not report syntax errors. It does not warn. It exits silently, and
what the user sees is "double-clicking does nothing". The hard rules:

- CRLF line endings throughout, **pure ASCII** (non-ASCII becomes mojibake)
- Flat `goto` structure, no nested `( ... )` blocks
- No unescaped parentheses inside `echo`
- Open a folder with `explorer "%~dp0subdir"`, **never `start "" "barename"`** —
  `start` resolves bare names against PATHEXT and may pick a same-named `.bat`,
  relaunching the script itself

`make_bats.py` is both the generator and the validator; it checks every rule above
automatically.

### 6. Windows timing precision

`time.sleep` is only accurate to 15.6 ms, and Windows ignores `timeBeginPeriod`
while the process is not in the foreground. So the pointer loop uses a
high-resolution waitable timer (`CreateWaitableTimerExW` +
`CREATE_WAITABLE_TIMER_HIGH_RESOLUTION`), and HID reads use the blocking
`dev.read(size, timeout_ms)` to follow the controller's native cadence.

### Where the magic numbers come from

Constants like the 8% stick deadzone, the 300 ms ghost filter, `wander = 12`,
and the gyro scale factors were all measured on real hardware. **The measurement
that justifies each one is in a comment right next to it** — see `gyro.py`,
`dualsense.py`, `lights.py`, `engine.py`. Read the comment before changing a value.

If you need to measure something new: write a throwaway probe script, put it in
the project root, add one line to `make_bats.py` (see the docstring on
`probe_script`), run it, write the conclusion into a code comment next to the
constant, then delete the script. Do not accumulate historical reports.

## Known limitations

- The controller's built-in microphone is only enumerated as an audio input device
  **over USB**. Bluetooth audio uses a proprietary Sony protocol that Windows does
  not support. Adaptive triggers, rumble, and lighting all work over both.
- Games with anti-cheat block synthetic input. If the target window runs elevated,
  this app must run elevated too.
- Steam tends to capture the PS button, so it is a poor choice for a global hotkey.

## Not built yet

- Macro sequences (the backend can execute them; the UI has no editor)
- Brightness control (Windows has no universal hotkey for it)
- Tray icon

---

# 中文说明

把 PS5 手柄（DualSense）映射成 Windows 键鼠。

[English](#dualsense-mapper) · **中文**

---

全部按键可自定义，带自适应扳机阻力、触摸板手势、陀螺仪体感、灯光控制。
免安装（portable），不开机自启 —— 关掉程序，手柄立刻恢复成普通手柄，
不影响正常打游戏。

定位是日常桌面操作和聊天打字，不是打游戏：最初的动机是从沙发上操作
Windows 桌面，语音输入绑在麦克风静音键上。

## 环境需求

| | |
|---|---|
| 系统 | Windows 10 / 11 |
| Python | **3.10 ~ 3.13，必须是一般版本。** 不能用 3.13t / 3.14t 这种 free-threaded 无 GIL 版，C 扩展没有对应 wheel。**推荐 3.12** |
| 运行时 | Edge WebView2 —— Win10/11 自带，不用另外装 |
| 硬件 | DualSense 手柄（`0x054C:0x0CE6` 或 `0x0DF2`），USB 和蓝牙都支持 |

> **所有依赖都装在项目目录下的 `.venv\` 里，绝不碰你的全局 Python。**
> 这不是洁癖 —— 早期版本直接装进全局，把用户的 Pillow 顶掉了，
> 弄坏了他机器上别的软件。

## 快速开始

在项目根目录直接双击：

| 脚本 | 作用 |
|---|---|
| **`run.bat`** | 启动程序。第一次会自动建 `.venv\` 并装依赖，要等几分钟 |
| **`check.bat`** | 出问题先跑这个：列出机器上所有 Python、选中了哪一个、`.venv` 里装了什么、手柄有没有被认到 |
| **`release.bat`** | 打包成免安装版，产出 `output\DualSenseMapper\` 和同名 `.zip` |

命令行跑也行：

```bat
.venv\Scripts\python.exe main.py
```

面向终端用户的完整说明（界面怎么用、每个功能的细节）在
[`使用说明.md`](./使用说明.md)，双语，英文在前。

## 目录结构

```
.
├── main.py                   程序入口
├── run.bat / check.bat / release.bat
├── requirements.txt
├── 使用说明.md               终端用户手册（英文 + 中文）
│
├── ps5mapper/                核心包
│   ├── app.py                pywebview 外壳 + JS API 桥接
│   ├── engine.py             主循环：读 HID -> 解析 -> 派发动作
│   ├── dualsense.py          HID 报告的解析与组装（USB / 蓝牙两种布局）
│   ├── config.py             配置分层：工厂 / 烘焙 / 用户
│   ├── triggers.py           自适应扳机：阻力墙位置与触发点计算
│   ├── gyro.py               陀螺仪体感瞄准
│   ├── lights.py             灯条 / 玩家灯 / 麦克风灯（纯函数，好单测）
│   ├── winput.py             SendInput 封装
│   ├── audio.py              录音设备切换、麦克风占用检测
│   ├── default_config.json   出厂默认值
│   └── ui/
│       ├── index.html        整个界面，单文件
│       ├── i18n.js           多语言运行时
│       └── lang/             zh-TW / en 语言文件
│
├── tests/                    单元测试 —— 纯逻辑，不需要手柄
└── tools/
    ├── make_bats.py          所有 .bat 的唯一生成来源 + 校验器
    ├── bake_defaults.py      把某份配置烘焙成出厂默认
    ├── build_i18n.py         生成语言文件
    └── audit_i18n.py         查有没有漏翻译的字符串
```

## 跑测试

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试全是纯逻辑，**不需要接手柄**。改动前后都跑一次。

## 动手改之前必读

下面每一条都是真实踩出来的坑，花过实打实的时间。改到相关区域前先看一眼。

### 1. `Api` 类上不准有任何公开的非方法属性

pywebview 6 会递归遍历 `js_api`，把公开的非方法属性当成嵌套 API 继续往下钻。
一旦 `Api.app → App.api → Api` 形成循环引用，就会无限套娃：主线程卡死、
窗口「未响应」、前端拿到空对象，**而且完全没有错误信息指向真正的原因**。
已经有测试锁死这条规则。

### 2. `ui/index.html` 里有三个函数是 Python 的镜像

`wallSpan()` / `firePercent()` / `hasBreakthrough()` 对应
`ps5mapper/triggers.py` 里的同名函数。**改一边必须同步改另一边**，
否则界面上画的扳机阻力墙跟实际触发点会对不上。

### 3. 灯条必须先「抢灯」

手柄上电后固件会一直占着灯条和玩家灯，主机发的 RGB 全被无视。
必须先送一次 lightbar setup 报告（`dualsense.build_lightbar_reset()`）。
漏掉的症状很好认：**麦克风灯正常，但灯条和玩家灯完全没反应**。

### 4. 触摸板有幽灵触点

手指离开后，约四分之一到三分之一的帧仍在报告「按下」，坐标卡住不动。
去抖 3 帧 + 幽灵过滤 300ms 的逻辑**不能删** —— 否则单指滑动会被凑出一个
幻影的第二触点，误判成双指滚动。

### 5. 绝不手写 `.bat`，一律走 `tools/make_bats.py`

cmd.exe 遇到语法错误不报错、不提示、直接静默退出，用户看到的永远是
「双击没反应」。硬性规则：

- 全 CRLF 换行、**纯 ASCII**（中文会变乱码）
- 扁平的 `goto` 结构，不要嵌套 `( ... )` 代码块
- `echo` 里不准有未转义的括号
- 开文件夹用 `explorer "%~dp0子目录"`，**不要用 `start "" "裸名字"`** ——
  `start` 会按 PATHEXT 解析裸名字，可能挑中同名的 `.bat` 把脚本自己重启

`make_bats.py` 同时是生成器和校验器，上面每一条它都会自动检查。

### 6. Windows 计时精度

`time.sleep` 精度只有 15.6ms，而且程序不在前台时系统会忽略 `timeBeginPeriod`。
所以指针循环用的是高精度可等待定时器（`CreateWaitableTimerExW` +
`CREATE_WAITABLE_TIMER_HIGH_RESOLUTION`），HID 读取用阻塞式
`dev.read(size, timeout_ms)`，跟着手柄原生速率走。

### 那些魔法数字是哪来的

死区 8%、幽灵过滤 300ms、`wander = 12`、陀螺仪刻度这些常量，全是在真手柄上
量出来的。**每个常量为什么是这个值，都写在它自己旁边的注释里** ——
见 `gyro.py`、`dualsense.py`、`lights.py`、`engine.py`。改值之前先读注释。

要测新东西：写一个一次性探测脚本放项目根目录，在 `make_bats.py` 里加一行
（看 `probe_script` 的文档字符串），跑完把结论写进常量旁边的注释，然后把脚本删掉。
不要攒历史报告。

## 已知限制

- 手柄内置麦克风**只在 USB 连接时**才会被 Windows 枚举为音频输入设备。
  蓝牙用的是 Sony 私有协议，Windows 不支持。自适应扳机、震动、灯光两种连接都能用。
- 带反作弊的游戏会拦截模拟输入；目标窗口若是管理员权限，本程序也需要管理员权限。
- PS 键容易被 Steam 抢走，不适合当全局热键。

## 还没做的

- 宏序列（后端能执行，UI 没有编辑器入口）
- 亮度调节（Windows 没有通用快捷键）
- 托盘图标
