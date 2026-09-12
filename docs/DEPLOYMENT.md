# V6 架构与 Render 部署

## 技术决策

保留 React 19 + TypeScript + Vite，以及 Python + FastAPI。项目已经是同源前后端，Python 后端已有网页提取、来源约束、证据核对、可取消工具循环和 SQLite 调度；换 Node.js 不会直接增强 Agent 能力，反而需要重写这些边界。因此本轮把运行环境从“本机单用户”扩展成“私人服务器 + 多设备浏览器”，继续复用现有实现。

```
浏览器（动态 / 阅读 / 助手）
    │ HTTPS + HttpOnly 会话
Render Docker Web Service（单实例、单 worker）
    ├── React 静态页面与 96 队本地素材
    ├── FastAPI：登录、设置、资料、Agent API
    ├── Agent：工具循环 → 实际阅读 → 引用 / 核对
    ├── 定时采集与主题跟进
    └── /var/lib/touchline：SQLite + 凭据加密 key
```

这是一个私人空间：不同设备看到同一套球队、偏好与历史；不是多用户 SaaS。不要把同一实例给多个独立用户当作隔离账户使用。

## Render 配置

项目根目录 `render.yaml` 配置 Docker Web Service、Starter 计划、1 GB 持久磁盘以及 `/api/health` 健康检查。保持一个实例 / 一个 Uvicorn worker，避免重复执行调度任务。[Render 持久磁盘文档](https://render.com/docs/disks)说明磁盘只供单实例在运行时使用，附盘重新部署会有短暂中断。

用户逐项准备指南见 [USER_PREPARATION.md](USER_PREPARATION.md)。Blueprint 与 Web Service 都要使用 `codex/five-leagues-reports` 分支，当前默认分支可能仍是历史基线。

部署步骤：

1. 将当前分支推送到自己的 GitHub/GitLab/Bitbucket 仓库。推荐私有仓库。检查 `data/`、`.env`、`output/` 和凭据没有提交。
2. Render 中选择 **New → Blueprint**，连接仓库并选择要部署的分支。确认平台识别本项目 `render.yaml`。
3. 检查页面列出的套餐与磁盘费用后创建。持久磁盘需付费服务，因此此配置不是免费站点方案。
4. 等待服务构建、启动和健康检查成功。Render 生成 `https://…onrender.com` 网址。
5. 打开 Environment，查看自动生成的 `TOUCHLINE_ACCESS_PASSWORD`，作为私人网站访问密码登录。它与模型 API 密钥不同。
6. 在网页设置保存模型、关注球队和个性化偏好，选择真实资料，采集一队消息并打开正文。
7. 重新部署一次，确认文章、偏好与模型“已配置”状态仍在。重新部署会使登录会话失效，需要重新登录。

依据：[Render Docker](https://render.com/docs/docker)、[Blueprint 字段参考](https://render.com/docs/blueprint-spec)、[FastAPI 部署](https://render.com/docs/deploy-fastapi)。这里交付了配置与流程；只有 Render 返回成功运行的地址才算实际上线。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `TOUCHLINE_SERVER_MODE=true` | 开启私人网站访问保护；Render 环境也自动启用 |
| `TOUCHLINE_ACCESS_PASSWORD` | 至少 16 位，由 Blueprint 自动生成；轮换后重启会撤销所有旧会话 |
| `TOUCHLINE_PUBLIC_URL` | 网站根 origin，例如 `https://football.example.com`；不能包含子路径。Render 默认域名可自动发现 |
| `TOUCHLINE_DATA_DIR` | 必须落在持久磁盘内，默认部署值 `/var/lib/touchline` |
| `PORT` | 监听端口，遵循 Render 注入值；本地容器默认 8765 |
| `FORWARDED_ALLOW_IPS` | Render 入口代理设为 `*`；自己托管时改成实际可信反向代理 IP，避免信任任意外部请求 |

自定义域名在 Render 绑定后，设置 `TOUCHLINE_PUBLIC_URL` 为该 HTTPS 域名并通过它使用网站。同源写入校验按 scheme / host / port 完整比较。绑定域名时检查 DNS 与证书，不要把访问密码放进 URL。

模型 API 地址、协议、模型名称和 API 密钥由网页设置提供。模型密钥在服务端加密保存，不回传明文。登录 cookie 为 HttpOnly、SameSite=Strict，公网使用 Secure。服务重启撤销会话，失败登录会限流。

## 自己的服务器 / 本机 Docker

复制 `.env.example` 为 `.env`，填写访问密码后：

```powershell
docker compose up --build -d
```

打开 `http://localhost:8765`。默认 Compose 只映射本机环回地址；在自己的服务器上用 HTTPS 反向代理转发到这个端口，并把 `TOUCHLINE_PUBLIC_URL` 改成实际域名。

`docker compose down` 保留 named volume；不要使用 `down -v`，后者会删除数据卷。容器使用非 root 用户，使用已有宿主机绑定目录时需让 UID 10001 可以写入该目录及迁入的数据库与 key。

无 Docker 也可构建前端、安装 Python 依赖、设置相同环境变量后执行 `python -m server.serve`。此入口默认开启服务器模式，未设置密码 / URL 时会停止启动。开发 / 旧本机方式仍是 `python -m uvicorn server.main:app --host 127.0.0.1 --port 8765`。

## 现有数据迁移与备份

不要把开发电脑的 `data/` 打进镜像或 Git。新网站首次启动默认是新的私人空间，不会自动上传本机资料或模型密钥。

如需迁移：先备份并停止原服务，确保没有数据库写入，再把 `data/` 中的数据库与同名 `.key` 文件一起通过平台支持的安全文件传输送到服务器持久磁盘。SQLite 的 WAL / SHM 若仍存在也必须随停机快照保留；不要在运行中只复制 `.sqlite3` 文件。服务器停止写入期间替换数据，确认 UID 10001 可读写，再启动验证。保持本机备份，确认新网站正常后再自行决定是否清理旧副本。

持续备份可用 SQLite backup API 建立一致性数据库副本，并同时备份同名 `.key`。在线复制原数据库文件不能保证一致；丢失 key 将无法解密已保存的模型密钥。不要把备份放在 `public/` 或 `dist/`。来源、偏好、球队、任务等都在 SQLite 文档存储中；球队素材本身由版本化的 `public/assets/` 提供。

## 运维与扩展边界

- 服务器在运行且功能启用时，定时采集和跟进继续执行，不依赖个人电脑开机。
- 一个实例最多同时运行 3 个 Agent 任务；工具、来源与正文仍有超时 / 大小限制。
- 96 队属于已核对的 2026/27 五大联赛。ESPN 公开站点适配器读取五联赛全季赛程与积分，4 队俱乐部官网适配器保留；两类来源身份明确区分。
- 免费订阅、官网改版与限流会影响正文取得；界面必须如实显示摘要 / 部分正文 / 错误。
- 后续若要公众注册、账户隔离、多实例或大规模资料库，再引入用户模型、PostgreSQL、外部任务队列及对象存储；这不需要先把 Agent 改写成 Node.js。
