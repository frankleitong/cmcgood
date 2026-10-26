# cmcgood · 开发部代码仓管理

管理开发部在 GitHub / GitCode 上的所有代码仓：

1. **代码仓登记表**：所有仓库的平台、组织、负责人、团队、状态、最近合入时间一览，可搜索、筛选。
2. **新建仓库审批流程**：开发人员在网站上提交申请 → 审批人通过/驳回 → 通过后自动登记到登记表（可选：用 token 自动在平台上创建仓库）。
3. **登记一致性扫描**（每天）：对比登记表与 GitHub / GitCode 上的实际仓库，找出「平台上有但没登记」「登记了但平台上没有」「平台上已归档但登记未同步」的仓库，可一键导入或忽略。
4. **活跃度检查与催促归档**（每周）：读取每个仓库默认分支的最近提交，超过 N 天（默认 180）无合入的标记为「不活跃」，通过邮件 / 群机器人提醒负责人；负责人可在网站上「申请归档」（走审批）或「暂不归档」（填理由，暂停提醒 90 天）。

技术栈刻意保持简单：Python + Flask + SQLite + gunicorn，systemd 管进程和定时任务，nginx 反向代理。**不依赖 Docker**，所有代码在 git 里，数据只有一个 SQLite 文件 + 一个配置文件，换服务器时 10 分钟可恢复。

```
┌────────┐   ┌─────────────────────────┐   ┌──────────────────────────────┐
│ 浏览器 │──▶│ nginx (已有)             │──▶│ gunicorn :8000 (cmcgood)      │
└────────┘   │  /cmc/ 或独立域名        │   │  Flask + SQLite               │
             └─────────────────────────┘   └──────────────┬───────────────┘
                                                          │
             systemd timers ── 每天 02:00 sync ───────────┤──▶ GitHub / GitCode API
                            ── 每周一 09:30 check-activity┤──▶ 邮件 / 企业微信·飞书·钉钉
                            ── 每天 03:30 backup ─────────┘
```

## 目录与文件位置

| 路径 | 说明 |
| --- | --- |
| `/opt/cmcgood/` | 代码（git clone 的目录，含 `venv/`） |
| `/etc/cmcgood/cmcgood.env` | 配置文件（token、扫描范围、邮件等），**不进 git** |
| `/var/lib/cmcgood/cmcgood.db` | SQLite 数据库 |
| `/var/lib/cmcgood/backups/` | 每日备份（保留 30 份），每份含数据库 + 配置 |
| `/etc/systemd/system/cmcgood*.{service,timer}` | 由 `install.sh` 从 `deploy/systemd/*.in` 生成 |

## 部署（首次）

服务器要求：Linux + systemd + Python 3.8+（带 venv 模块）+ git + nginx。以下命令以 root 执行。

```bash
# 0. 准备（按发行版二选一）
apt install -y git python3 python3-venv        # Debian / Ubuntu
yum install -y git python3                     # CentOS / RHEL / openEuler

# 1. 拉代码
git clone https://github.com/frankleitong/cmcgood.git /opt/cmcgood

# 2. 安装：建 cmcgood 系统用户、venv、依赖、配置文件、数据库、systemd 服务和定时器
#    国内服务器 pip 慢可加：PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
bash /opt/cmcgood/deploy/install.sh

# 3. 创建管理员
bash /opt/cmcgood/deploy/manage.sh create-admin admin --email you@example.com

# 4. 填配置：token、扫描范围（GITHUB_NAMESPACES / GITCODE_NAMESPACES）、SITE_URL、邮件、群机器人
vi /etc/cmcgood/cmcgood.env
systemctl restart cmcgood
```

### 接入 nginx

二选一，示例在 `deploy/nginx/`：

- **挂在现有站点的子路径**（如 `https://dev.example.com/cmc/`）：把 `deploy/nginx/cmcgood-subpath.conf` 里的两个 `location` 粘贴进已有的 `server {}`。
- **独立域名**（如 `repos.example.com`）：复制 `deploy/nginx/cmcgood-server.conf` 到 `/etc/nginx/conf.d/` 并改 `server_name`。

```bash
nginx -t && systemctl reload nginx
```

然后把配置里的 `SITE_URL` 改成对外地址（通知邮件里的链接用它），HTTPS 站点再设 `SESSION_COOKIE_SECURE=1`，`systemctl restart cmcgood`。

### 首次上线建议顺序

1. 管理员登录 → 「扫描」→「立即扫描」，把组织下已有仓库「全部导入登记表」；个人/测试仓库点「忽略」。
2. 在「用户」里给领导设为「审批人」；让大家自助注册（或由管理员建号），然后在仓库详情页补上负责人和团队。
3. 「扫描」→「立即检查」（先不勾「发送提醒」）看一下活跃度结果，确认阈值合适后再让定时任务自动提醒。

## 日常运维

```bash
# 升级到 GitHub 上最新代码（拉代码 + 重新执行 install.sh，幂等）
bash /opt/cmcgood/deploy/update.sh

# 状态 / 日志
systemctl status cmcgood
journalctl -u cmcgood -f                       # 网站日志
journalctl -u 'cmcgood-task@*' -n 100          # 定时任务日志
systemctl list-timers 'cmcgood*'               # 定时任务下次执行时间

# 手动执行任务（以 cmcgood 用户身份）
bash /opt/cmcgood/deploy/manage.sh sync
bash /opt/cmcgood/deploy/manage.sh check-activity --no-remind
bash /opt/cmcgood/deploy/manage.sh backup
bash /opt/cmcgood/deploy/manage.sh create-admin <用户名>   # 也可用来重置管理员密码
```

修改定时任务时间：编辑 `deploy/systemd/cmcgood-*.timer.in` 后提交到 git，服务器执行 `update.sh`。

## 换服务器 / 灾备

数据只有两样：数据库和配置文件，每天自动打包到 `/var/lib/cmcgood/backups/cmcgood-*.tar.gz`。建议定期把这个目录同步到别的机器（如 `rsync` 到 NAS）。

在新服务器上：

```bash
git clone https://github.com/frankleitong/cmcgood.git /opt/cmcgood
bash /opt/cmcgood/deploy/install.sh
bash /opt/cmcgood/deploy/restore.sh /path/to/cmcgood-YYYYmmdd-HHMMSS.tar.gz
# 再按上文配置 nginx
```

## 配置说明

完整配置项及注释见 [`config.example.env`](config.example.env)，常用的：

| 配置 | 说明 |
| --- | --- |
| `GITHUB_TOKEN` / `GITCODE_TOKEN` | 平台访问令牌。只做扫描时只读权限即可；`AUTO_CREATE_REPO=1` 时需要建仓权限 |
| `GITHUB_NAMESPACES` / `GITCODE_NAMESPACES` | 扫描范围，逗号分隔的组织名；个人账号写 `user:用户名` |
| `AUTO_CREATE_REPO` | 审批通过后是否自动在平台上建仓，默认 0（只登记，管理员手动建） |
| `INACTIVE_DAYS` | 多少天无合入算不活跃，默认 180 |
| `REMIND_INTERVAL_DAYS` | 同一仓库两次提醒的间隔，默认 14 |
| `SMTP_*` / `MAIL_FROM` | 邮件通知，不配置则不发 |
| `WEBHOOK_TYPE` / `WEBHOOK_URL` | 群机器人（wecom / feishu / dingtalk / slack），不配置则不发 |
| `HTTPS_PROXY` | 服务器访问 GitHub 需要走代理时配置 |

## 角色

| 角色 | 权限 |
| --- | --- |
| 开发人员 | 查看登记表、提交新建申请、对自己负责的仓库申请归档 / 暂不归档 / 编辑描述 |
| 审批人 | 以上 + 审批申请（不能审批自己提交的）、查看扫描结果 |
| 管理员 | 以上 + 用户管理、手工登记 / 导入 / 删除登记记录、触发扫描 |

系统里第一个注册的用户自动成为管理员；之后注册的都是开发人员。不想开放注册可设 `ALLOW_REGISTER=0`。

## 本地开发

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
mkdir -p instance && cp config.example.env instance/cmcgood.env   # 把 DATA_DIR 那行删掉或改成 instance
venv/bin/flask --app wsgi run --debug
```

数据默认放在 `instance/`（已在 `.gitignore` 中）。数据库结构变更请在 `cmcgood/db.py` 的 `MIGRATIONS` 末尾**追加**新版本，启动时自动升级。
