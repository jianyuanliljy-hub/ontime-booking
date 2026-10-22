# 参与贡献

欢迎改进准点。优先处理可复现的缺陷、清楚的状态反馈和文档错误；保持 Python 标准库即可运行、前端无需构建的使用方式。

## 开始前

阅读 [README](README.md) 和 [预约设计](BOOKING_DESIGN.md)。支持原生 Windows、macOS 和 Linux；Python 需为 3.9+，前端测试使用 Node.js 22+。无需安装第三方包。Windows 命令行可将本文的 `python3` 换为 `py -3 -X utf8`。

复现页面问题时，优先运行离线服务：

```bash
python3 "ui_test_server.py" --port 8766
```

模拟入口、测试日期及运行方式见 README。不要为了验证软件改动向官网创建真实订单、反复尝试登录或进行并发压测。

## 变更约束

- 库存缺失、日期缺失、异常响应保留为未知；不得默认为 0 或有票。
- 保留日期、时段、票价、须知版本及已有订单校验。
- 创建请求最多一次。超时后只读核对，不自动重发，也不删除提交记录强制重试。
- 保留本机文件锁、提交意图持久化、Host/Origin/CSRF 校验和静态资源白名单。
- 凭据不进入日志、浏览器状态响应或持久存储；示例只使用明确的虚构数据。
- 更改查询频率、截止时间、订单识别或放票规则时，在 PR 中解释行为影响和证据。
- 样式和文案修改尽量保持现有业务流程；遵循附近代码的注释语言。

## 提交前验证

```bash
python3 -B -m unittest discover -v
node --test "test_web_ui.cjs"
node --check "web/app.js"
```

测试均离线运行；Python HTTP 测试需要本机临时端口权限。修复缺陷时，增加能重现问题、验证行为的回归测试。纯文档或样式调整不需要为了凑数量新增测试。

跨平台修改需保留两种原生锁：Windows 的 `msvcrt` 字节锁、macOS/Linux 的 `fcntl` 文件锁。共享相同的锁文件命名与提交记录，不删除旧锁文件。`test_platform_support.py` 会启动真实子进程核验互斥、正常退出及异常退出后的释放；两个 `.cmd` 测试只在 Windows 运行。

`.cmd` 文件由 `.gitattributes` 指定 CRLF；脚本正文保持 ASCII，Python 进程显式启用 UTF-8。不要依赖系统代码页，也不要假定 Windows 的文件模式位等价于 POSIX `0600`。CI 使用 `python -B -S -m unittest discover -v`，避免第三方时区包掩盖标准库运行问题。

涉及页面时，用离线服务检查相关操作、刷新后的状态、错误提示与窄屏布局。写明实际测试过的浏览器和视口，不把模拟出票描述为真实成功。

## 报告问题与 Pull Request

普通问题使用仓库的 Bug 模板，说明系统、Python/浏览器版本、复现步骤、预期和实际结果。截图或诊断只保留必要部分，并先移除个人入口、`userId`、token、订单号和个人信息。

每个 PR 尽量聚焦一个问题，描述触发条件、原因、改动后的行为及验证结果。安全问题不要开公开 Issue，请遵循 [SECURITY.md](SECURITY.md)。
