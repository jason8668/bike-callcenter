# 云端部署指南（经 GitHub 部署，和排课系统同一模式）

## 一、服务器怎么选（重要，别踩坑）

- **不要**装在排课系统的那台 ECS（36.133.38.128）上：它正给学校演示用，且 4C/8G 已跑 6 个容器，再塞 Zammad 会互相抢内存。
- 建议：一台**新开的云主机**做试跑 —— 配置 4C/8G/100G 起步；系统优先 **Debian 12**（FusionPBX 官方安装脚本的首选系统），Zammad 的 Docker 部署在 Debian/Ubuntu 都可以。
- 本项目我的运行环境内存不足且没有 Docker，所以试跑必须落在你的云主机上，我负责脚本与验证清单，你负责在控制台粘贴执行（和排课系统一样，VNC 终端贴的脚本全部是英文版，见 `deploy/install-server.sh`）。

## 二、部署顺序

1. **先 Zammad + 中间件**（一台机即可验证 80% 链路）：
   服务器以 root 登录，把 `deploy/install-server.sh` 里的 `REPO_URL` 改成你第 04 号文档建好的仓库地址，整段粘贴执行。
   它会：装 Docker → clone 本仓库 → 拉官方 zammad-docker-compose（关 Elasticsearch 省内存，Zammad 开在 8080）→ 生成中间件密钥 → 构建并启动 bridge（只绑 127.0.0.1:8090）。
2. **Zammad 首次初始化**：浏览器开 `http://<服务器IP>:8080` 建管理员；然后在后台：
   - 设置 → 集成 → CTI (generic) 打开，记下 CTI 的 token；
   - 个人设置/管理员 → Token Access 建一个 API token（权限给 admin）；
   - 把两个 token 填进服务器 `/opt/bike-callcenter/middleware/.env`，跑 `deploy/update-from-github.sh` 让 bridge 重读；
   - 跑字段脚本：`ZAMMAD_URL=http://127.0.0.1:8080 ZAMMAD_TOKEN=<api token> python3 /opt/bike-callcenter/zammad-setup/setup_bike_fields.py`；
   - 后台按脚本文件头清单建工单状态（新建→受理→已派单→现场处理中→已回传→待回访→办结）。
3. **模拟来电验收**（无电话线路先验全链路）：按 VERIFY.md 第 1–4 项，用 `/simulate/call` 造一通假电话，确认 Zammad 里自动生成工单且录音附件能播放。
4. **再装 FusionPBX**：在同一台（Debian 时可同机，注意 80/443 归 FusionPBX、8080 归 Zammad、SIP/RTP 端口放行）或第二台机器上跑 FusionPBX 官方安装脚本；按 `freeswitch/README.md` 完成 IVR 菜单、5 个坐席队列、Lua 钩子、mod_xml_cdr 三件接线；接 400 中继后按 VERIFY.md 第 5–8 项做真实来电验收。

## 三、安全组清单

| 端口 | 用途 | 建议来源 |
|---|---|---|
| 8080 TCP | Zammad | 办公 IP |
| 80/443 TCP | FusionPBX 管理/录音 | 办公 IP |
| 5060 UDP/TCP | SIP 信令 | 运营商中继 IP 优先 |
| 16384–32768 UDP | RTP 语音媒体 | 对端/运营商 |
| 8090 TCP | 中间件 | 不对外（只绑 127.0.0.1） |

## 四、更新方式

代码更新：本仓库 push 到 GitHub → 服务器以 root 跑 `deploy/update-from-github.sh`（git pull + 重建 bridge）。Zammad 本体升级走官方 compose 的升级说明，不要和业务代码混在一次更新里。
