# 浏览器检查：18 个套件各自查什么

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
mkdir -p /tmp/pw && cd /tmp/pw && npm init -y && npm install playwright@1.63.0
```

## 逐套件

| 套件 | 断言 | 它盯住的东西 |
|---|---|---|
| `landing_check` | 34 | 落地→申请→批准→码真的能注册；禁 JS 可读；standalone 直通；右上角下载入口真的跳到位；安装步骤两个平台都在；没有安装包时绝不给死链 |
| `shell_check` | 41 | 标签栏/侧栏/抽屉/hash 路由/后退/书签/非管理员 |
| `refresh_feedback_check` | 16 | 含「后台轮询必须安静」与 360px 无溢出 |
| `install_hint_check` | 11 | iPhone/安卓/桌面各自文案、关闭后不再出现、已安装则不显示 |
| `capacity_check` | 16 | 建议值/改名额落库/低于账号数被拒/360px |
| `compliance_check` | 24 | 隐私/条款 360px 可读、采集点告知、未勾选不发注册请求、页脚可达 |
| `tasks_check` | 25 | 收起/恢复/计数同步/按天回看/360px（需要今天的报告） |
| `browser_check` | — | 主流程走查 |
| `appearance_check` | — | 主题与背景控件只存在于「外观」板块 |
| `background_photo_check` | — | 只在浏览器里重编码、方向正确 |
| `metrics_check` | — | 主机指标面板；**macOS 上把三条读 `/proc` 的断言明确标成「跳过」并计数**（跳过 ≠ 通过），Linux 那一侧由 `manage check-metrics` 在服务器上真机验证 |
| `security_ui_check` | — | 改密码 / 退出所有设备 |
| `admin_edit_check` | — | 管理员代改另一个用户；健康卡；巡检面板与**「已知晓」**（点的是**带冒号**的那条 key）；邮件面板；token 用量；审计列表。**需要 `--admin-fixtures`** |
| `usage_click_check` | 9 | **运营者看得见的那两个控件就是接上线的那两个**；全文档 id 唯一；正常加载时不喊「页面没有完整加载」 |
| `admin_grant_check` | 25 | 环境变量那份不可移除/授权不建号/403 与 422/授权立刻生效/360px |
| `bulletin_check` | 23 | 未登录访客看得到/默认不公开/撤下后消失/三条上限/360px |
| `agent_action_check` | 16 | 只有建议了动作的行才有按钮/按钮标签来自目录/**请求体里没有动作名**/确认后原地变已确认且不再给按钮/360px。**需要 `--admin-fixtures`** |
| `setup_guide_check` | 23 | 步骤顺序=任务顺序/进度四格/「为什么不能用登录密码」默认可见/**打字过程中服务商与教程就已更新**（那个 `onchange` bug 的回归测试）/360px |

（「—」= 这个套件没有在 `AGENTS.md` 里记过断言条数；不是零条。）

## 两件容易忘的事

- **本机是 macOS，生产是 Ubuntu。** `metrics_check` 里读 `/proc` 的三条在 macOS 上必然跳过，
  所以**通过数要按「跳过」的条数打折看**；Linux 那一侧另有一条真机命令。
- **夹具里要挑真实形状的那一条。** `admin_edit_check` 曾经只点 `disk`（夹具里唯一不带冒号的巡检 key），
  于是「已知晓」在真实 key 上全都 404 而套件一直绿。选样本时先问一句：
  **生产上最常出现的那个形状，我点到了吗？**
