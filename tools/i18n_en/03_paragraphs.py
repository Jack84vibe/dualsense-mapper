# -*- coding: utf-8 -*-
EN = {
"低于这个角速度的转动会被按比例削弱。照着上面的实时读数调":
  "Rotation slower than this gets scaled down proportionally. Set it from the live readout above",
"正常应该在 ±10 以内。数值很大又不回落，说明零点坏了":
  "Normally within ±10. A large value that never settles back means the zero has gone bad",
"笔记本上的「一次半」手势：点一下，紧接着再按住不放就能拖":
  "The laptop “one-and-a-half” gesture: tap once, then immediately hold to drag",
"抬起 / 压下多少度，光标就从屏幕顶走到底。角度越小越灵敏":
  "How many degrees of tilt take the cursor from the top of the screen to the bottom. Smaller is more sensitive",
"按一下停掉全部映射，再按一下恢复。不能删除，但组合可以改。":
  "Press once to stop all mapping, press again to resume. It can't be removed, but the combo can be changed.",
"慢速转动降增益、快速转动给全增益。0 = 固定增益（和以前一样）":
  "Low gain for slow rotation, full gain for fast. 0 = fixed gain (the old behaviour)",
"插着 USB 时手柄一直在充电，所以这个显示主要在蓝牙下有意义。":
  "Over USB the controller is always charging, so this display really only means something over Bluetooth.",
"按下后要滑过这么远才开始拖。0 = 按下就能立刻拖，但点击会更容易飘":
  "How far you must slide after pressing before a drag starts. 0 = drag immediately, but clicks drift more easily",
"取最近几帧的加权平均。手柄每秒上报约 586 帧，4 帧约等于 7 毫秒":
  "A weighted average of the last few frames. The controller reports about 586 frames a second, so 4 frames is roughly 7 ms",
"摇杆是连续量，只能整根绑定一种用途。摇杆按下（L3/R3）是另外的键位。":
  "A stick is an analogue input, so the whole stick takes one role. Clicking it in (L3/R3) is a separate button.",
"绿色免安装：配置就存在程序旁边的一个文件里，整个文件夹拷走就能换电脑用。":
  "Portable, no installer: the config lives in a file next to the program, so copy the folder and it runs on another PC.",
"混合模式：手指落下不动，滑动才移动指针。物理按下是独立键位，在映射页里配。":
  "Hybrid mode: putting your finger down does nothing, only sliding moves the pointer. The physical click is a separate button, set on the mapping page.",
"阈值调到 0 就是完全不压制。平滑帧数调大会更稳，但也会开始觉得跟手变慢。":
  "A threshold of 0 means no suppression at all. More smoothing frames is steadier, but it starts to feel less responsive.",
"手柄指向正上或正下时会自动退回手柄坐标系 —— 那个姿势下本来也没有「屏幕水平」":
  "Points straight up or straight down and it falls back to controller space — in that pose there is no “screen horizontal” to speak of",
"<b>黄点 = 重复绑定。</b>两个键绑同一个功能是允许的，这里只提醒，不拦你。":
  "<b>Amber dot = duplicate binding.</b> Two buttons may share one action; this is only a reminder, not a block.",
"默认关着。陀螺仪一开就是手一动光标就跑，所以用组合键随时开关——要放下手柄之前记得关掉。":
  "Off by default. With the gyro on, every hand movement moves the cursor, so it's toggled by a button combo — remember to switch it off before you put the controller down.",
"玩家灯是触摸板下方、PS 键上方那一排 5 个白色小灯。亮几个就是还剩几格电，对照表如下：":
  "The player lights are the row of 5 small white LEDs below the touchpad and above the PS button. The number lit is the battery level:",
"阻力墙的位置就是触发点——按穿墙的那一刻发出按键。下面的进度条是 R2/L2 的实时按压值。":
  "The wall's position is the fire point — the key is sent the moment you break through it. The bars below show live R2/L2 travel.",
"靠重力方向判断手柄到底有没有在转。显示「静止」时，陀螺仪读到的任何角速度都会被当成零点偏差减掉":
  "Uses the direction of gravity to tell whether the controller is really rotating. While it reads “still”, any rate the gyro reports is treated as zero-point error and subtracted",
"和笔记本触摸板一致：轻点一下就是左键，不用真的把板子压下去。轻点手指几乎不滚动，天生没有下压漂移的问题。":
  "Just like a laptop trackpad: a light tap is a left click, no need to actually press the pad down. A tap barely slides your finger, so it has no click-drift problem to begin with.",
"<b>耗电提醒。</b>扳机阻力长期开启会明显加快耗电，右上角总开关可以一键关掉所有效果，按键绑定不受影响。":
  "<b>Battery note.</b> Leaving trigger resistance on drains the battery noticeably faster. The master switch top-right kills every effect at once; button bindings are unaffected.",
"切档时灯条先亮成那一档的提示色，到时间自动回到上面的常驻灯效。下面就是「哪个档对应什么颜色」，点色块直接改。":
  "When you switch profiles the light bar flashes that profile's colour, then returns to the resting effect above. Below is which profile maps to which colour — click a swatch to change it.",
"手柄转多少度，光标走多少像素。和摇杆、触摸板<b>叠加</b>：摇杆负责大范围移动，陀螺仪负责最后的精确定位。":
  "Degrees of controller rotation become pixels of cursor movement. It <b>adds to</b> the sticks and touchpad: the stick covers distance, the gyro does the final precise aim.",
"手柄上有三盏灯：彩色灯条、触摸板下方 5 个白色玩家灯、麦克风键上的橙灯。灯条没有硬件动画，呼吸是程序逐帧画出来的。":
  "The controller has three lights: the colour bar, the 5 white player lights under the touchpad, and the amber light on the mic button. The bar has no hardware animation — the breathing effect is drawn frame by frame by this program.",
"映射暂停时灯条变暗红且停掉动效，另两盏灯熄灭——让「停了」这件事不可能被看错。退出程序时灯条恢复成 PS5 出厂那个蓝。":
  "While mapping is paused the bar goes dark red and stops animating, and the other two lights go out — so “it's stopped” is impossible to misread. On exit the bar returns to the PS5 factory blue.",
"每套配置档包含全部按键映射、扳机、触摸板和摇杆参数。灯条的常驻颜色是全局的；每档这里设的是<b>切到它时闪一下的提示色</b>。":
  "Each profile holds every button mapping, trigger, touchpad and stick setting. The light bar's resting colour is global; what you set here per profile is <b>the colour it flashes when you switch to it</b>.",
"「只倒漂移」= 只有按下前那段位移又慢又短、看着就是压手指压出来的，才把它撤销；你要是正快速划过去顺手点一下，那段位移是真的，不动它。":
  "“Drift only” = the movement just before the click is undone only if it was slow and short, i.e. it looks like it came from your finger flattening. If you were sliding quickly and clicked on the way past, that movement was real and is left alone.",
"默认值不是拍脑袋定的：实测你一次舒服的横扫大约转 75 度，25 像素/度正好让这个动作扫过整个屏幕宽度。要更快就往上调，上限 150。":
  "The default isn't a guess: a comfortable sweep of yours was measured at about 75 degrees, and 25 px per degree makes exactly that gesture cross the full screen width. Raise it for more speed, up to 150.",
"亮度靠缩放颜色实现——灯条没有独立的亮度寄存器。拉到 0 就是全灭。你也可以把「开关手柄灯光」绑到任意一个手柄按键上，在按键映射页里选内置功能。":
  "Brightness works by scaling the colour — the bar has no separate brightness register. At 0 it is fully off. You can also bind “Controller lighting on/off” to any controller button; pick it from the built-in actions on the mapping page.",
"这是手柄<b>此刻</b>的角速度。把手柄照平常那样握在手里别动，看偏航那一行跳到多少——那就是你的手抖幅度，抖动压制的阈值定在它上面一点即可。":
  "This is the controller's angular rate <b>right now</b>. Hold it the way you normally would and keep still, then watch how far the yaw row jumps — that is your hand tremor. Set the tremor threshold just above it.",
"<b>强烈建议开着。</b>两个值不等会把斜向的方向感弄歪 —— 水平 80、垂直 50 时，一个标准的 45° 斜向动作走出来只有 32°，比你想的平得多":
  "<b>Strongly recommended.</b> Unequal values skew every diagonal — at 80 horizontal and 50 vertical, a clean 45° gesture comes out as 32°, much flatter than you intended",
"手是会抖的，陀螺仪会忠实记录下来。这里不用硬死区（那会让人明显感到「有一块区域是死的」），而是阈值以下按比例缩小，两段在阈值处是连续的，不会有跨过某个点突然跳一下的感觉。":
  "Hands shake, and the gyro records it faithfully. Rather than a hard dead zone — which you can feel as a patch where nothing happens — anything below the threshold is scaled down proportionally. The two halves meet exactly at the threshold, so there is no sudden jump as you cross it.",
"<b>方向反了就点一下。</b>我的探针没能测出正负——每一段测试动作都是「来回转」，两个方向都做了，峰值只取绝对值大的那个，符号根本分不出来。所以这两个开关的默认值是猜的，你试一下就知道。":
  "<b>If a direction is backwards, just flip it.</b> My probe couldn't measure the signs — every test motion was “back and forth”, covering both directions, and the peak only kept whichever was larger in magnitude, so the sign was indeterminable. These two defaults are therefore guesses; one try will tell you.",
"<b>灵敏度调得高却嫌抖，先调这里的加速强度，别再往上堆阈值。</b>固定增益下「够快」和「不抖」是绑死的——灵敏度拉到 3 倍，手抖也跟着放大 3 倍，阈值只能决定「多小算抖」，决定不了「抖起来有多大」。加速把两者拆开：慢速（手抖都在这一段）降增益，快速（有意的大幅横扫）给全增益。":
  "<b>High sensitivity but too shaky? Adjust acceleration here first, don't keep piling on threshold.</b> With fixed gain, “fast enough” and “steady” are locked together — triple the sensitivity and you triple the tremor too. The threshold only decides what counts as tremor, not how big it gets. Acceleration separates them: low gain where tremor lives, full gain for deliberate sweeps.",
"触摸板是整块压下去的按键，<b>开关在按压行程的最后才闭合</b>，而手指在这段行程里已经压扁打滑了一两毫米——所以光标是在你按下去<b>之前</b>就飘走的，单纯「收到点击就冻住指针」根本来不及。这里照抄笔记本驱动的做法：缓存最近几十毫秒的位移，收到点击就倒回去，再在按住期间设一个拖动门槛。":
  "The touchpad is one big button, and <b>the switch only closes at the very end of the travel</b> — by which point your finger has already flattened and slipped a millimetre or two. So the cursor drifts <b>before</b> the click arrives, and simply freezing the pointer on click is far too late. This copies what laptop drivers do: buffer the last few tens of milliseconds of movement, rewind it when the click arrives, then hold a drag threshold while the button is down.",
"「系统麦克风状态」读的是 Windows 自己那套「有没有应用正在用麦克风」（任务栏那个麦克风图标就是靠它画的），所以 Win+H 静音几秒自动停掉时灯会跟着灭。代价是别的应用占着麦克风（Discord、浏览器、会议软件）它也会亮。<br>「按键计数」只数你按了几次 Win+H，会和自动停止对不上——按一次停、再按一次开的方向会整个反过来。":
  "“System mic state” reads Windows' own record of whether any app is using the microphone (the same thing that draws the taskbar mic icon), so when Win+H stops itself after a few seconds of silence the light goes out with it. The cost is that it also lights up when anything else holds the mic — Discord, a browser, meeting software.<br>“Key presses” only counts how many times you pressed Win+H, so it drifts out of step with those automatic stops, and the on/off sense ends up completely reversed.",
"<b>相对</b>：和水平一样，看你转得多快，光标就走多少——转一圈回来光标不会回到原处，像鼠标一样可以「抬手复位」。<br><b>绝对</b>：用加速度计感知重力，手柄的俯仰角直接对应光标在屏幕上的高度，永远不会漂。更接近「遥控器」的感觉，但手一晃光标就跟着晃，而且和水平方向手感不统一。<br>水平方向<b>只能</b>用相对——绕垂直轴转的时候重力方向不变，加速度计什么都感觉不到。":
  "<b>Relative</b>: like the horizontal axis, how fast you turn is how far the cursor goes — turn a full circle back to where you started and the cursor won't be where it began, so you can “lift and reposition” as with a mouse.<br><b>Absolute</b>: the accelerometer senses gravity, so the controller's pitch angle maps directly to cursor height on screen and never drifts. Closer to a TV-remote feel, but every wobble of your hand moves the cursor, and it doesn't match how the horizontal axis feels.<br>The horizontal axis <b>can only</b> be relative — turning about the vertical axis leaves gravity unchanged, so the accelerometer senses nothing at all.",
"偏航和俯仰是相对<b>手柄自己</b>定义的。手柄端得正时它们正好对应屏幕的左右上下；一旦侧倾（握着时很难完全端平，转手腕时侧倾角还一直在变），手柄的「偏航轴」就不再对着屏幕水平方向了 —— 侧倾 30° 就能明显感到「我明明往右上转，光标却偏右」。<br><b>屏幕坐标系</b>用重力方向把转动换算回屏幕的左右上下，手柄歪着拿也对。端正状态下两者<b>完全一致</b>，所以切换不会影响你已经调好的任何参数。":
  "Yaw and pitch are defined relative to <b>the controller itself</b>. Held level they line up with the screen's left/right and up/down exactly; but once it rolls — and it is hard to hold perfectly level, with the roll angle shifting constantly as your wrist turns — the controller's yaw axis no longer points along the screen's horizontal. At 30° of roll you can clearly feel it: “I'm turning up and to the right, but the cursor goes right.”<br><b>Screen space</b> uses the direction of gravity to convert rotation back into the screen's left/right and up/down, so it is correct however you hold it. Held level the two are <b>exactly identical</b>, so switching cannot disturb any setting you have already tuned.",
}
