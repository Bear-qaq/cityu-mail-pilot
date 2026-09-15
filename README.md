# CityU Mail Pilot

把学校邮箱的来信变成一份中文摘要，用邮件发回给你。

学校的邮箱通常只在电脑上开着 Outlook 才看得到，手机上不方便。这个工具让你**在城大邮箱里设一条自动转发**，
它把转发到你私人邮箱的**本校来信**读出来、用大模型生成中文报告、再发回给你——所以你在手机邮件 App 里
就能看到「这封邮件要你做什么」。

**只读、只处理本校邮件。模型 key 用你自己的，或者由部署这个程序的人统一提供。**

---

## 它是怎么工作的

```
城大邮箱 ──自动转发──▶ 你的私人邮箱 ──只读 IMAP──▶ 本程序 ──▶ 中文报告 ──邮件──▶ 你
                                                    │
                                             只保留 @cityu.edu.hk 来信
                                             用你的（或这台部署的）API key 生成
```

- **只读**：全程 `BODY.PEEK[]`，绝不标记已读、绝不删除、绝不改动你的邮件
- **只处理本校来信**：其它邮件记为「已跳过」并写进日报，**不会静默丢弃**
- **两种 key 模式，账单跟着 key 走**：
  - **用户自带**（BYOK，默认）：每个账号在「AI 模型」里填自己的 key，模型费用你直接付给供应商，不经过部署者
  - **部署者统一提供**：配一把实例级 key（`INFE_PILOT_DEFAULT_MODEL_KEY`，装法见
    `docs/platform-key-2026-09-14.md`），**没配 key 的账号自动用它**，账单落在部署者名下。
    这是**兜底不是覆盖**——用户自己填了 key 就还是用他自己的。
    （CityU 的内测实例就是这样：内测期间不用自己配 key。）
- **报告用邮件发回**：不用装 App，报告就在你手机邮件 App 里

---

## 需要什么

| | |
|---|---|
| 一台服务器 | Ubuntu / Debian，能 SSH。最低 1 核 1 GB（本项目的生产环境是 2 核 2 GB） |
| 一个私人邮箱 | QQ / Gmail / 163 都行，需要开启 IMAP 并生成**授权码**（不是登录密码） |
| 一个大模型 API key | 例如 DeepSeek；推荐用**非推理型**模型（见下方「已知限制」） |
| 一个域名 | **可以不要**：没有域名时安装器会用你的公网 IP 生成一个能签证书的主机名 |

---

## 两个网址

装好之后有两个入口，别搞混：

| 路径 | 是什么 |
|---|---|
| `/` | **介绍页**——给陌生访客看这是什么、怎么工作、隐私上做了什么，并接受内测申请 |
| `/app` | **应用本体**——登录、设置、报告 |

装到手机主屏幕时记录的是 `/app`。如果之前装过旧版本（图标里记的是 `/`），
`landing.js` 检测到以独立窗口运行时会直接跳进 `/app`，所以旧安装不会坏。

---

## 快速开始

```bash
# 1) 解包
tar -xzf cityu-mail-pilot-<版本>.tar.gz
cd cityu-mail-pilot-<版本>

# 2) 先看它要做什么（什么都不改）
sudo bash pilot_app/deploy_pilot.sh --dry-run

# 3) 安装。不给 --origin 时会用公网 IP 自动生成一个域名
sudo bash pilot_app/deploy_pilot.sh --admin-email 你的邮箱@example.com --proxy

# 4) 生成邀请码
sudo -u cityumail /opt/cityu-mail-pilot/.venv/bin/python -m pilot_app.manage create-invite
```

装完打开安装器打印的地址，用邀请码注册，然后在「邮箱设置」里填私人邮箱的 IMAP 授权码、
在「AI 模型」里填你的 API key。

**装到手机上**：网页里会有一步安装引导。安卓用 Chrome 的「安装应用」，iOS 用 Safari 的
「分享 → 添加到主屏幕」。**不需要应用商店**——理由见 `docs/app-distribution-decision-2026-09-14.md`。

### 安装器支持的选项

```
--origin URL         对外访问地址；不填则用公网 IP 推导
--admin-email EMAIL  管理员邮箱（首次安装写入配置）
--proxy              配置 Nginx 并申请 Let's Encrypt 证书
--dry-run            只检查并打印将要做的事
--upgrade            只换代码并重启；绝不碰配置与数据，升级前自动备份
--uninstall          停止并移除单元，保留配置与数据
--purge              配合 --uninstall：连配置和数据一起删（需手动确认）
```

**重复运行是安全的**：已经存在 `pilot.env` 时不会被覆盖，主密钥和域名不会重新生成。

---

## 运维

```bash
# 服务状态
sudo systemctl status cityu-mail-pilot-web cityu-mail-pilot-worker

# 日志
sudo journalctl -u cityu-mail-pilot-worker -f

# 验证备份能不能真的恢复（随时可跑，不改动线上数据）
sudo bash -c 'cd /opt/cityu-mail-pilot && set -a && . /etc/cityu-mail-pilot/pilot.env && set +a \
  && .venv/bin/python -m pilot_app.manage restore-drill'

# 手动检查告警阈值
sudo bash -c 'cd /opt/cityu-mail-pilot && set -a && . /etc/cityu-mail-pilot/pilot.env && set +a \
  && .venv/bin/python -m pilot_app.manage check-alerts --dry-run'
```

**恢复步骤、以及这套备份的边界，写在 `docs/restore-drill.md`。** 上线前请至少读一遍——
"有备份"和"能恢复"是两件事。

### 出问题时会通知你

- **巡检哨兵**：worker 每 5 分钟检查收信失败/停顿、队列堆积、报告失败、磁盘、证书到期，
  发邮件给管理员，带静默期去重与恢复通知
- **systemd `OnFailure=`**：`certbot.service` 或备份单元失败时会真的发邮件（页面挂了也能告警）

---

## 隐私

- 邮件**只读**，处理后**正文从数据库清空**（不长期留存原文）
- 邮箱授权码与 API key 用 **AES-256-GCM 信封加密**存放，按用户与用途分上下文
- 只有 `@cityu.edu.hk` 的来信会被送去模型，其余只记录「已跳过」
- **没有任何遥测**，不向本项目作者发送任何数据

⚠️ **你必须知道**：报告的生成会把**邮件正文发送给你选择的大模型供应商**。选哪家、它的条款如何，
由你决定——请自行确认这符合你所在地区的规定与学校的政策。

### 如果你要给别人注册用

仓库自带两份文书：`/privacy`（隐私政策）与 `/terms`（服务条款），注册页也强制勾选同意。
**其中联系邮箱是从环境变量填进模板的**——`INFE_PILOT_CONTACT_EMAIL`。
不设置的话会退回用管理员邮箱；如果连管理员邮箱也没有，页面上会明写「尚未配置联系邮箱」
（而不是印一个假的地址）。给别人用之前请务必设好：

```bash
sudo sed -i 's|^INFE_PILOT_CONTACT_EMAIL=.*|INFE_PILOT_CONTACT_EMAIL=you@example.com|' /etc/cityu-mail-pilot/pilot.env
sudo systemctl restart cityu-mail-pilot-web
```

文书里写明了数据保留期、第三方接收方、导出与删除途径——**改了产品行为请一并改文书**，
`pilot_app/tests/test_compliance.py` 会检查文书里的关键承诺还在不在。

---

## 已知限制

- **要关的是「思考」，不是换模型名**：DeepSeek 的官方模型名是 `deepseek-flash`
  （旧的 `deepseek-chat` 仍被接受，但响应体回的 `model` 是 `deepseek-flash`，`/models` 里已经没有它了）。
  `deepseek-flash` **默认开着思考模式**，隐藏推理会把输出预算烧光、正文几乎为空——
  本项目在请求里显式发 `thinking: {"type": "disabled"}`，所以用得是好的。
  自带 key 的人遇到「报告全是无」，先查这个开关，别去换模型名。
- **Gmail 每 15 分钟才轮询一次**：Google 官方要求如此（超限会临时封停账号 1–24 小时），
  所以 Gmail 的报告会比其它邮箱慢一些。
- **iOS 没有可下载的 App**，只能「添加到主屏幕」。Apple 的审核指南拒绝网页封装的 App
  （第 4.2 条），这不是可以绕开的。
- 备份与数据库在同一台机器上，**没有异地副本**。

---

## 开发

```bash
# 测试（409 个，改动后必须全过）
.venv-pilot/bin/python -m unittest discover -s pilot_app/tests -p 'test_*.py'

# 本地起网页（用临时库，绝不碰生产）
INFE_PILOT_DB=/tmp/preview.sqlite3 \
INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
INFE_PILOT_COOKIE_SECURE=0 INFE_PILOT_ADMIN_EMAILS=you@example.com \
.venv-pilot/bin/python -m pilot_app.web --host 127.0.0.1 --port 8787
```

整个产品线只有**一个**第三方运行时依赖（`cryptography`）。想加依赖之前请先看
`docs/open-source-recon-2026-09-14.md`——那里记录了每一次"评估后决定不加"的理由。

---

## 许可证

**AGPL-3.0**，见 `LICENSE`。

简单说：你可以自由使用、修改、分发；但如果你把它改成网络服务提供给别人用，
**你也必须公开你的修改**。这是为了确保这个工具和它的改进一直留在公共领域。

```
Copyright (C) 2026 余剑篪

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>.
```
