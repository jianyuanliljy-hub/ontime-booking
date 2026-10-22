<div align="center">

# 准点 · ON TIME

**把时间，留给运动。**

本地场馆预约助手 · 余票总览、定时预约、订单核对

[![Tests](https://github.com/jianyuanliljy-hub/ontime-booking/actions/workflows/tests.yml/badge.svg)](https://github.com/jianyuanliljy-hub/ontime-booking/actions/workflows/tests.yml)
![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![零第三方依赖](https://img.shields.io/badge/运行依赖-仅_Python_标准库-14866D)
![平台](https://img.shields.io/badge/平台-Windows_%7C_macOS_%7C_Linux-555555)
[![License: MIT](https://img.shields.io/badge/License-MIT-14866D)](LICENSE)

[快速开始](#快速开始) · [Windows 版本](#windows-版本) · [使用指南](#使用指南) · [命令行](#命令行) · [常见问题](#常见问题) · [参与贡献](CONTRIBUTING.md)

</div>

准点将连接账号、查看余票、选择场次和预约进度放在一个本地页面里。无需安装 Python 包，无需配置数据库，也无需把个人入口交给其他服务。启动后，浏览器负责操作，Python 后端负责查询、计时与订单核对。

<p align="center">
  <img src="docs/assets/desktop.png" alt="准点当前版本的离线演示界面：账号连接、场次选择及预约摘要" width="1000">
</p>

<p align="center"><sub>截图来自项目自带的离线演示，未连接真实账号。页面支持窄屏布局。</sub></p>

> **当前状态**：个人使用工具，非官方服务。使用本人官方预约入口，首次运行需填写本机站点配置。目前仅适配现有票务流程，真实出票和放票高峰成功率仍待验证。实际规则、资格与闭馆安排以官方预约平台为准。

## 能做什么

| 功能 | 使用方式 |
| --- | --- |
| 一屏查看余票 | 连接后读取默认日期的全部健身时段；支持今天、明天和指定日期 |
| 明确区分库存状态 | 分别显示有票、0 张和余量未知，缺失日期不会被当成售罄 |
| 定时预约 | 选择一个场次，阅读须知并确认后，在预计放票时间附近进行限时查询 |
| 控制重复提交 | 同一账号、日期和票种在本机加锁，持久化提交意图；创建请求最多一次 |
| 处理不确定结果 | 网络超时后只读核对订单，支持重启后恢复核对 |
| 本地运行 | 只监听 `127.0.0.1`，无第三方字体、统计脚本或云端服务依赖 |
| 命令行查询 | 支持多时段筛选、指定日期、低频持续检查与 JSON 输出 |

预约范围固定为：**本人账号、配置的目标场馆、一个健身时段、一张 0 元票**。不支持付费票支付、多账号、代理轮换、并发刷接口或微信/短信推送。

## 快速开始

需要 **Python 3.9 或更新版本**和现代浏览器，支持原生 Windows、macOS 和 Linux。运行时只用 Python 标准库，不需要 `pip install`、Node.js 或前端构建。

### 1. 下载项目

```bash
git clone https://github.com/jianyuanliljy-hub/ontime-booking.git
cd "ontime-booking"
```

也可以点击仓库的 **Code → Download ZIP**，解压后在项目目录打开终端。想先看看界面，可以直接运行[离线演示](#先试用离线演示)。

### 2. 复制入口，填写一次本机配置

在企业微信中打开场馆官方预约入口，进入 **体育场馆预约 → 个人锻炼**。到达场馆选择页后，先不要选择场馆或时段，点右上角 **⋯ → 复制链接**。

将项目中的 `site.example.json` 复制一份，命名为 `site.local.json`，用文本编辑器填写下面三项：

| 配置项 | 从哪里取得 |
| --- | --- |
| `origin` | 复制链接开头的 `https://` 和域名，到第一个 `/` 路径前为止 |
| `venue` | 预约页面显示的场馆完整名称，按原文填写 |
| `club_id` | 复制链接中 `clubId=` 后面的数字，填写为带引号的字符串 |

例如，下面这条**虚构链接仅用于说明结构**：

```text
https://tickets.example.org/easyserpApp/index.html#/branch?clubId=26&userId=YOUR_PRIVATE_ID
```

它对应的配置如下；`venue` 需要替换为你实际预约的场馆名：

```json
{
  "origin": "https://tickets.example.org",
  "venue": "示例体育馆",
  "club_id": "26"
}
```

**不要把完整个人链接填进配置，也不要把 `userId` 或 token 写进去。** 完整入口稍后只粘贴到本机页面。配置文件只保存在你的电脑上，已被 Git 忽略；通常只需填写一次。示例域名无法查询真实余票，需要替换为本人官方入口中的域名。

### 3. 启动并连接

```bash
python3 "ticket_web.py" --open
```

浏览器会打开 **[http://127.0.0.1:8765](http://127.0.0.1:8765)**。粘贴刚才复制的完整入口，点击「连接」，然后选择使用日期和时段。后续操作见[使用指南](#使用指南)。

Windows 可以双击 `start-ontime.cmd`；macOS 可以双击 `启动准点.command`。预约期间保持启动窗口运行，按 `Ctrl+C` 关闭服务。

端口被其他程序占用时，可换一个端口：

```bash
python3 "ticket_web.py" --port 8766 --open
```

当前程序只适配已实现的票务接口。其他系统即使填写了域名，也可能无法使用。

## Windows 版本

可在 Windows 上直接运行，无需 WSL，也无需安装额外 Python 包。

1. 安装 [Python 3.9 或更新版本](https://www.python.org/downloads/windows/)，确保 `py` 启动器或 `python` 命令可用。
2. 下载仓库 ZIP，**先完整解压**到本机目录。路径中可以包含空格或中文，建议放在本人用户目录内。
3. 按上面的说明，复制并填写一次 `site.local.json`。
4. 双击 **`start-ontime.cmd`**。它会检查 Python 版本、启动本地服务并打开浏览器；网页中粘贴本人入口即可继续。

启动文件优先使用 `py -3`，不可用时尝试 `python`。报错会留在窗口中，便于查看；不要只关闭浏览器就以为预约已停止。结束服务后，窗口提示按键即可关闭。

也可以在 PowerShell 中进入项目目录后运行：

```powershell
py -3 -X utf8 "ticket_web.py" --open
```

先试用离线演示：

```powershell
py -3 -X utf8 "ui_test_server.py" --port 8766
```

随后打开 [http://127.0.0.1:8766](http://127.0.0.1:8766)。本文其他命令中的 `python3`，在 Windows 下同样可换成 `py -3 -X utf8`。启动文件会自动启用 UTF-8，避免中文输出受系统默认编码影响。

Windows 的预约互斥锁使用系统自带的 `msvcrt`；macOS/Linux 继续使用 `fcntl`。如果系统缺少 `Asia/Shanghai` 时区数据库，程序按现代北京时间 **UTC+8** 计算当前预约，无需安装 `tzdata`；这一回退不用于历史夏令时日期换算。

## 使用指南

### 1. 复制本人入口

按上面的路径，从个人锻炼的场馆选择页复制本人入口，保持未选择场馆和时段的状态。将完整链接粘贴到准点的账号连接框，点击「连接」。

入口含有个人账号标识，应当按登录凭据保管。直接在本地页面粘贴，不要把完整链接发到 Issue、截图、命令行参数或公开仓库。有效期通过实际登录判断；提示失效时重新取得入口。

### 2. 查看余票，选择日期和时段

连接成功后，页面自动读取默认日期的全部时段。当天最后一个时段结束前默认查今天，结束后切到明天。也可以点「明天」或选择日期，批量读取该日余票。

点选目标时段，再点击「检查余票」。有票时继续核对须知、规则和已有订单；当前无票或余量未知时会快速返回。切换日期、时段或检查结果过期后，需要重新检查。

**0 张**只表示接口当前返回零库存，无法单独区分售罄、闭馆或未放票。**余量未知**表示目标日期缺失、字段异常或查询未完成，不能据此判断是否有票。

### 3. 阅读须知，确认预约

点击「开始抢票」，阅读官网须知并明确勾选同意。预计放票时间已过时，页面显示「立即开始抢票」。放票前的预检查允许保留当前无票的预约计划，方便提前等待。

程序在放票前准备登录和已有订单检查；到时默认在 **30 秒**窗口内，以至少 **0.5 秒**的起点间隔串行查询。发现余票后，再核对规则、须知和余量，遵守官网返回的须知倒计时，仅提交一次订单请求，随后最多约 **90 秒**核对订单。

等待阶段可以停止。进入提交或订单核对阶段后，页面不再提供取消按钮；停止本地程序无法撤销官网订单。结果未知时，使用「核对订单」继续只读确认。

**预约期间保持电脑唤醒、网络连接和本地服务运行。** 关闭网页后，当前进程中的任务继续；重启服务后，等待任务不会自动恢复，已提交的记录仍保留用于防重复和核对。

## 先试用离线演示

无需真实入口即可查看页面和演练操作：

```bash
python3 "ui_test_server.py" --port 8766
```

打开 [http://127.0.0.1:8766](http://127.0.0.1:8766)，使用以下**虚构测试入口**连接：

```text
https://tickets.example.org/easyserpApp/index.html#/branch?clubId=26&userId=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
```

演示服务使用临时目录和模拟接口，不连接官网、不创建真实订单。选择 `2099-09-17` 可看到模拟日期库存；预检查和预约结果也均为模拟。它用于了解界面与验证状态流转，不能用来判断真实余票或预约能力。

## 命令行

### 只读余票查询

```bash
# 查看全部健身时段；按提示粘贴本人入口，输入不回显
python3 "ticket_monitor.py"

# 只看指定时段
python3 "ticket_monitor.py" --slot "06:00-07:30"

# 每轮完成后等待 120 秒，关注两个时段
python3 "ticket_monitor.py" --watch --interval 120 --slot "06:00-07:30" --slot "19:30-21:30"

# 输出 JSON，便于后续处理
python3 "ticket_monitor.py" --json
```

`--date YYYY-MM-DD` 可限定实际使用日期；未指定时保留服务器返回的各日期。持续模式仅在首次检查、状态变化、错误及恢复时输出，按 `Ctrl+C` 停止。最小轮询间隔为 60 秒，逐票种详情查询间隔为 1 秒；网络连续失败 3 次后停止。指定时段仍会先读取全部健身票详情，再按实际使用时间筛选。

### 预约预检查与恢复

下列命令中的日期是占位符，运行前替换成实际日期。默认仅预检查，只有明确使用 `--execute` 才可能创建订单。

```bash
# 只读预检查：查看目标票种、当前须知及版本摘要
python3 "ticket_booking.py" --date "YYYY-MM-DD" --slot "06:00-07:30"

# 接受刚刚阅读的须知后，等待放票并尝试预约
python3 "ticket_booking.py" --date "YYYY-MM-DD" --slot "06:00-07:30" --execute --accept-notice "当前须知的SHA256摘要"

# 提交结果未知时，仅核对原场次，不创建订单
python3 "ticket_booking.py" --date "YYYY-MM-DD" --slot "06:00-07:30" --reconcile-only
```

| 预约参数 | 默认值 / 作用 |
| --- | --- |
| `--date` | 必填，实际使用日期 |
| `--slot` | `06:00-07:30` |
| `--execute` | 显式启用实际预约，与 `--reconcile-only` 互斥 |
| `--accept-notice` | 已阅读并接受的当前须知 SHA256 摘要 |
| `--window` | `30` 秒，可设 1–60 秒 |
| `--poll` | `0.5` 秒，可设 0.5–10 秒，保持串行 |
| `--state-dir` | 项目下的 `booking-state`，保存提交意图与结果 |
| `--diagnostics-dir` | 项目下的 `booking-diagnostics`，保存脱敏诊断 |

三个入口均支持 `--help`。预约时序、失败恢复及时间预算详见 [预约设计](BOOKING_DESIGN.md) 和 [网络拥堵处理](BACKEND_RESILIENCE.md)。

## 工作原理

```mermaid
flowchart LR
    A[连接本人入口] --> B[查询日期与时段]
    B --> C[预检查与阅读须知]
    C --> D[用户确认]
    D --> E[等待预计放票]
    E --> F[限时串行查余票]
    F --> G[核对规则与倒计时]
    G --> H[记录意图并提交一次]
    H --> I[只读核对本人订单]
    I --> J[确认出票或保留未知]
```

前端只请求本机 Python 服务，由后端通过 HTTPS 与官网交互。预约后端复用连接；临时查询错误采用有限重试，并遵守服务器的 `Retry-After`。下单请求不自动重放，响应丢失时改为核对本人订单。

预计放票时间按历史观察到的规则计算：使用日的前一天、对应时段的结束时刻，时区为 `Asia/Shanghai`。规则变化或临时闭馆可能使预测失效；程序没有证明与业务服务器完成精确校时。**接口返回成功后，仍需查到对应未使用订单才报告确认出票。**

## 隐私与本地文件

站点身份保存在被 Git 忽略的 `site.local.json` 中。私人入口和登录 token 由本机进程使用，不写入配置文件、浏览器本地存储或 Cookie，不发送到其他第三方服务。正常查询和预约会向本机配置的目标站点发送必要登录及订单参数。页面提交入口后清空输入框；更换入口或重启服务后需要重新粘贴。

| 本地位置 | 内容与处理方式 |
| --- | --- |
| `site.local.json` | 目标站点来源、场馆及组织标识；仅留在本机 |
| `booking-state/` | 提交意图、核对结果，可能包含本人订单号；保留在本机，不提交仓库 |
| `booking-diagnostics/` | 接口路径、阶段、状态及耗时，不记录入口、token 或响应正文；默认不提交 |
| `*-probe-*.json`、`verification-*.json` | 本地历史检查快照，不代表实时状态；默认不提交 |

服务限制 Host、Origin 和 CSRF 请求标记，仅开放固定静态资源与业务入口，单个进程同时只允许一个操作。当前设计面向个人本机使用，不提供公网部署或多人共享模式。本机文件锁无法阻止手机或其他电脑同时下单。

**不要删除未知状态的 `booking-state` 记录来强制重试。** 先在官方预约平台核对订单；入口标识变化后，程序也无法安全推断新旧入口属于同一账号。

## 常见问题

<details>
<summary><strong>需要安装什么？为什么没有 requirements.txt？</strong></summary>

运行时只使用 Python 标准库。Python 3.9+ 和浏览器即可；Node.js 仅用于开发时运行前端状态测试。没有需要通过 pip 安装的运行依赖。

</details>

<details>
<summary><strong>能在手机上用吗？</strong></summary>

页面适配窄屏，但服务只监听本机回环地址，手机无法直接连接电脑上的服务。完整预约流程请在 Windows、macOS 或 Linux 电脑上运行。

</details>

<details>
<summary><strong>能保证抢到票吗？</strong></summary>

不能。余票、资格、限购、闭馆安排、服务器拥堵和他人同时预约都会影响结果。当前兼容性依赖目标站点，实际出票和高峰成功率尚未验证。离线测试通过不构成出票保证。

</details>

<details>
<summary><strong>为什么显示结果未知，却不再提交？</strong></summary>

请求超时后，官网仍可能已经创建订单。自动重发有重复预约风险，因此保留记录并只读核对；超过核对预算后，需要先去官方预约平台确认实际订单。

</details>

<details>
<summary><strong>关闭网页、终端或电脑后会怎样？</strong></summary>

关闭网页不影响仍在运行的后端；关闭终端、本地服务、电脑休眠或断网会影响任务。刷新网页可恢复当前进程状态。重启服务后不会恢复等待任务，已提交的场次可通过原账号入口继续核对。

</details>

## 开发与测试

```bash
# Python 回归测试：含本机 HTTP、网络故障、订单恢复与凭据保护
python3 -B -m unittest discover -v

# 前端状态测试：需要 Node.js 22+，无需 npm install
node --test "test_web_ui.cjs"

# 前端语法检查
node --check "web/app.js"
```

测试不调用真实官网、不创建真实订单；Python HTTP 测试需要允许绑定本机临时端口。GitHub Actions 在 Windows、macOS 和 Linux 上运行 Python 3.9/3.13 测试，并独立运行前端检查。CI 使用 `-S` 禁用第三方包目录，验证标准库即可运行。

Python 测试共 **122 项**，其中 2 项启动文件测试仅在原生 Windows 执行，其他系统会跳过；前端状态测试共 **20 项**。覆盖站点配置、跨进程锁冲突与释放、中文路径、无时区数据库、未知库存、错误日期、凭据不泄露、限时重试、单次提交、跨重启核对、旧页面状态隔离、Host/Origin/CSRF 校验等行为。各平台实际结果以本次 GitHub Actions 记录为准。

```text
.
├── ticket_web.py             # 本机 HTTP 服务与任务状态
├── ticket_booking.py         # 预约、限时查询、提交记录与订单核对
├── ticket_monitor.py         # 独立的只读余票查询
├── ticket_config.py          # 本机站点配置与校验
├── site.example.json         # 虚构站点配置示例
├── start-ontime.cmd          # Windows 双击启动入口
├── web/                      # 无构建步骤的 HTML、CSS、JavaScript
├── test_*.py                 # Python 回归测试
├── test_web_ui.cjs            # 前端状态测试
├── ui_test_server.py          # 完全离线的界面演示
├── docs/assets/              # README 演示截图
└── .github/                  # 自动测试与协作模板
```

## 更多文档

- [预约设计与验证边界](BOOKING_DESIGN.md)：接口流程、时序与去重。
- [网络拥堵与订单恢复](BACKEND_RESILIENCE.md)：超时、重试、`Retry-After` 与未知订单处理。
- [购票须知处理](BOOKING_NOTICE.md)：执行时仍需读取并接受官网当前版本。
- [界面与交互验收记录](UI_REVIEW.md)：桌面、窄屏和状态反馈检查方法。
- [缺陷复查记录](BUG_REVIEW.md)：已修正的问题与对应回归验证。
- [贡献指南](CONTRIBUTING.md)：本地验证、变更约束和提交说明。
- [安全问题反馈](SECURITY.md)：凭据保护与私密报告方式。

欢迎通过 [Issue](https://github.com/jianyuanliljy-hub/ontime-booking/issues) 报告可复现的问题，或提交小而明确的改进。提供截图和日志前，请移除个人入口、账号标识与订单信息。

## 许可证

项目代码以 [MIT License](LICENSE) 发布。使用本工具仍需遵守目标站点当前规则。
