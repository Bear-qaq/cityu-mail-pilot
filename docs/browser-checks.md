# 浏览器检查：20 个套件各自查什么

> 这份表原来挤在 `AGENTS.md` §5 里。那张卡有**硬字节上限**，每加一条事实就要挤掉一条旧的，
> 而这一块是**纯清单**（而且和 `tools/run_browser_checks.sh` 里的套件名重复），
> 所以它搬到这里，`AGENTS.md` 只留一句「怎么跑」。

## 怎么跑

```bash
# 一次跑完（推荐）：给每个套件起一个独立的干净库和端口
bash tools/run_browser_checks.sh              # 全部
bash tools/run_browser_checks.sh admin_edit_check   # 只跑一个
```

**不要手工共用同一个库跑两个套件**：前面的套件会把内测名额填满，后面的就报「名额已满」，
看起来像注册坏了。运行器自己申请空闲端口，所以下面那些端口号只是**示意**，
不是必须照着填的。

需要 Playwright（不在仓库里，因为它只用于开发）：

```bash
npm install --no-save playwright@1.63.0   # 装在仓库根目录
npx playwright install chromium           # 光装 npm 包不够，还要下浏览器本体
```

> **`/tmp/pw` 那份老装法仍然能用**：套件不再写死路径，而是问 `tools/pw.js`，
> 它先找普通的 `node_modules`，再找 `/tmp/pw/node_modules`。
> 以前 23 个文件里都写着 `require('/tmp/pw/node_modules/playwright')`——
> **那一行是整个检查体系无法在别的机器（包括 CI）上跑的唯一原因**，
> 而且它报的错（`Cannot find module '/tmp/pw/...'`）指的是一台笔记本的事实，不是产品的问题。

## CI 里也会跑

`.github/workflows/ci.yml` 三个作业（推上去就自动跑）：

| 作业 | 查什么 | 为什么值得存在 |
|---|---|---|
| `unit` | 单测，**Python 3.9 与 3.14 各一遍** | 生产是 3.14、开发机是 3.9，而 3.9 能跑**只因为每个模块都写了 `from __future__ import annotations`**。两台机器都不会在有人写下 3.10 专有语法时报警——只有矩阵会。 |
| `browser` | 20 个套件（chromium） | 这些套件此前只在 macOS 上绿过，而 `metrics_check` 在 macOS 上**跳过**三条读 `/proc` 的断言。在 Linux runner 上那三条**是真跑的**。 |
| `release` | 打包 → 解开 → **在包里面把单测跑一遍** | 「仓库里能跑」和「下载下来能跑」是两件事。包少带一个文件，只有这一步会发现。 |

CI **不部署、也不跑开源导出**：那两件事需要 SSH 私钥与 `publish-private.json`（替换生产域名的规则），
而它们**按设计不能进公开仓库**。需要秘密的检查，贡献者跑不了，所以它留在运营者手上。

### 红了怎么查（公开仓库的日志要管理员权限）

`Actions → 运行 → 作业`的**日志下载需要仓库管理员权限**（匿名 API 返回
`403 Must have admin rights`），所以「红了但看不到为什么」是默认状态。
运行器因此会在套件失败时多发一条 **check-run 注释**，它是**公开可读**的：

```bash
# 匿名读注释（把 <sha> 换成那次运行的头提交）
curl -s "https://api.github.com/repos/JennieCN/cityu-mail-pilot/commits/<sha>/check-runs" \
  | python3 -c "import json,sys;[print(c['name'],c['id']) for c in json.load(sys.stdin)['check_runs']]"
curl -s "https://api.github.com/repos/JennieCN/cityu-mail-pilot/check-runs/<id>/annotations" \
  | python3 -c "import json,sys;[print(a['title'],a['message']) for a in json.load(sys.stdin)]"
```

两个坑：**注释体必须是单行**——`%0A` 要同时**去掉真实换行**，否则 workflow command
在第一行就结束，注释永远只显示第一行（而那行从来不是失败的那行）；
以及**套件输出要重定向到文件**——每个套件都以 `process.exit()` 收尾，而在 Linux 上
Node 写**管道**是异步的，`process.exit` 会把还没刷出去的丢掉，丢的恰好是 `FAILED (N): …`。

## 逐套件

| 套件 | 断言 | 它盯住的东西 |
|---|---|---|
| `landing_check` | 45 | 落地→申请→批准→码真的能注册；禁 JS 可读；standalone 直通；右上角下载入口真的跳到位；安装步骤两个平台都在；没有安装包时绝不给死链；**首屏那句「装成一个应用」真的可见且加粗、且排在 `#how` 之前**（标记顺序证明不了一个访客看不看得见）；**`ol.steps` 每一步的第一个子元素都是 `<b>`**（顺序就是内容，得能扫） |
| `shell_check` | 41 | 标签栏/侧栏/抽屉/hash 路由/后退/书签/非管理员 |
| `admin_edit_check`（广播那一段） | — | **点「确认收到」必须点坐标，不许 `node.click()`**：后者绕过命中测试，"
      "「按钮是禁用的」这种真故障在它面前照样绿（2026-09-16 就是如此）。断言里要包含 `elementFromPoint` 命中的就是那颗按钮，"
      "以及**第二条公告的 `disabled === false`**。 |
| （通用） | — | **元素截图不许把套件判红**：面板背后有轮询，`locator.screenshot` 会在取景框与快门之间遇到重渲染，于是 `Element is not attached to the DOM`（2026-09-16 CI 就红在这里）。用 `elementShot()`：重试一次、再不行整页截，并打一行 `note`。**存档失败 ≠ 断言失败。** |
| `refresh_feedback_check` | 42 | 含「后台轮询必须安静」与 360px 无溢出 |
| `install_hint_check` | 11 | iPhone/安卓/桌面各自文案、关闭后不再出现、已安装则不显示 |
| `capacity_check` | 16 | 建议值/改名额落库/低于账号数被拒/360px |
| `compliance_check` | 24 | 隐私/条款 360px 可读、采集点告知、未勾选不发注册请求、页脚可达 |
| `tasks_check` | 45 | 收起/恢复/计数同步/按天回看/360px（需要今天的报告）；**自设轻重缓急**（改完当场重排、刷新后还在）；**导出 `.ics`**（真的下载、读文件内容、断言响应头是 `text/calendar`）与**复制成清单** |
| `browser_check` | — | 主流程走查 |
| `appearance_check` | — | 主题与背景控件只存在于「外观」板块 |
| `background_photo_check` | — | 只在浏览器里重编码、方向正确 |
| `metrics_check` | — | 主机指标面板；**macOS 上把三条读 `/proc` 的断言明确标成「跳过」并计数**（跳过 ≠ 通过），Linux 那一侧由 `manage check-metrics` 在服务器上真机验证 |
| `security_ui_check` | — | 改密码 / 退出所有设备 |
| `admin_edit_check` | 144 | 管理员代改另一个用户；健康卡；巡检面板与**「已知晓」**（点的是**带冒号**的那条 key）；邮件面板；token 用量；审计列表；**替用户刷新状态**（全选/单个人都打同一个接口，夹具连不上就必须如实报 ✗，且不许点亮「出报告」）；**第三种提醒的模板与预览**（编辑框里是 `{steps}`，预览里才是学校那边的步骤）；设置向导第 2 步的转发结论**是画出来的**（不是藏 tooltip）。**需要 `--admin-fixtures`** |
| `usage_click_check` | 9 | **运营者看得见的那两个控件就是接上线的那两个**；全文档 id 唯一；正常加载时不喊「页面没有完整加载」 |
| `admin_grant_check` | 25 | 环境变量那份不可移除/授权不建号/403 与 422/授权立刻生效/360px |
| `bulletin_check` | 23 | 未登录访客看得到/默认不公开/撤下后消失/三条上限/360px |
| `guestbook_check` | 17 | 匿名访客能写/写完**不自己上墙**/后台看得到/刊登后官网真的出现/撤下后立刻消失/删除后后台也没有/**蜜罐填了照样回「收到了」但根本没入库**。用**另一个没有 cookie 的会话**读官网，所以它不会因为运营者自己能看见就误判成公开了 |
| `demo_check` | 17 | 免登录打开 `/demo` 直接进首页/横幅写明数据是编的/待办条目真的渲染出来/没数据的板块**禁用**而不是报错页/报告列表来自夹具/点「处理好了」明说这是只读演示/**整个过程零次 `/api/` 请求**/robots.txt 没挡它/390px 无溢出 |
| `agent_action_check` | 16 | 只有建议了动作的行才有按钮/按钮标签来自目录/**请求体里没有动作名**/确认后原地变已确认且不再给按钮/360px。**需要 `--admin-fixtures`** |
| `setup_guide_check` | 34 | 步骤顺序=任务顺序/进度四格/「为什么不能用登录密码」默认可见/**打字过程中服务商与教程就已更新**（那个 `onchange` bug 的回归测试）/360px；**死路要有出路**：选到用不了的服务商时出现「换一个邮箱」那一格、**用真实坐标点一下**就切过去（`element.click()` 放过这个 bug：点按钮会让输入框失焦，那一格在 mousedown 与 mouseup 之间被重画，click 于是派发到 `div` 上）、旧地址被清空、教程跟着换、反馈里提醒改 CityU 转发地址、那一格自己收起来 |

（「—」= 这个套件没有在 `AGENTS.md` 里记过断言条数；不是零条。）

## 两件容易忘的事

- **本机是 macOS，生产是 Ubuntu。** `metrics_check` 里读 `/proc` 的三条在 macOS 上必然跳过，
  所以**通过数要按「跳过」的条数打折看**；Linux 那一侧另有一条真机命令，
  现在还有 CI 的 `browser` 作业（它跑在 Linux 上，那三条会真的执行）。
  **那三条第一次真跑就抓到了两条「写测试时想当然」的假设**（2026-09-16）：
  ① `metrics_check` 要求**首屏就有 CPU 进度条**——而 CPU 是**速率**，需要相隔一秒的两次采样，
  web 刚起来时那一格诚实地显示「—」，下一次轮询才补上。断言改成「首屏有不依赖基线的读数、
  **一次轮询后三类齐全**」，比原来更强（它钉的是「自己补上」这个行为）；
  ② `refresh_feedback_check` 拿**浏览器本地时间**当参照——而面板用的是**用户配置的时区**。
  这两个时区在这台香港的 Mac 上恰好相同，在 UTC 的 runner 上差 480 分钟。
  断言现在从应用里读 `state.profile.timezone` 并按那个时区算期望值。
  **复现方法**：`TZ=UTC bash tools/run_browser_checks.sh refresh_feedback_check`

> **2026-09-16 补：那条「面板数字与服务器一致」在 CI 上偶发红（`面板 4 / 服务器 8`）的根因找到了。**
> 断言原来分两次读：先在**刷新那一刻**读面板（T0），再自己 fetch 一次服务器（T1）。「今天的人数」是
> **只增不减**的计数器，而这个套件自己就在制造访问（管理端 + 手机端两个上下文），两次读之间落进来一次，
> 面板上那个旧快照就永远追不上。**本机确定性复现**：把旧写法装回去、在两次读之间塞一次访问 ⇒ 立刻红成 `面板 8 / 服务器 11`。
> 现在期望值取**喂给面板的那一次响应**（`page.waitForResponse` 先挂、再点刷新），面板与期望同源、窗口为零；
> **反向验证**：把面板渲染改成 +1，这条断言当场红（`面板 9 / 服务器 8`）。所以它不是被放宽，是被钉紧了。
  （实测：本机默认 `GMT+0800`，`TZ=UTC` 下变成 `GMT+0000`）。
  **教训**：只在一台机器上跑过的断言，很可能钉的是那台机器。
- **夹具里要挑真实形状的那一条。** `admin_edit_check` 曾经只点 `disk`（夹具里唯一不带冒号的巡检 key），
  于是「已知晓」在真实 key 上全都 404 而套件一直绿。选样本时先问一句：
  **生产上最常出现的那个形状，我点到了吗？**
