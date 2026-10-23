# 云端部署指南（经 GitHub 部署，和排课系统同一模式）

## 一、部署位置：与排课系统同机共存（2026-10-23 Jason 拍板：只是测试环境）

- 目标机：现有移动云 ECS **36.133.38.128**（4C/8G，已跑排课系统 6 容器，演示地址 :8888）。
- 为什么 Zammad 可以「加容器」：它官方就是 Docker Compose 形态。试跑配置下（关闭 Elasticsearch）整套内存上限约 3.7G（已在 `deploy/zammad-override.yml` 里给每个容器写死 mem_limit），排课系统日常占用之外还留有余量；再加一个 2G swap 文件兜底突发。风险实话：两边同时高负载时机器会慢，但不会互相把数据搞坏；排课演示真到了正式场合前，可再把 Zammad 停掉（`docker compose stop`，一条命令的事）。
- 端口选择：**Zammad 用 8899**。不能用 8080 —— 这台未备案实例的备案端口 {80, 8080, 443, 8443} 公网入站被拦（排课系统当初就是这样被迫搬到 8888 的）。云安全组记得放行 8899。
- 为什么 FusionPBX 不能「加个容器」：它没有官方 Docker 镜像，官方装法是安装脚本接管整台宿主机（装 FreeSWITCH、Nginx 占 80/443、改系统包），在这台 Ubuntu 26.04 的共用机上跑有把排课演示搞挂的真实风险，而且它解决的是「电话交换」不是「页面」。所以分两期：
  - 一期（现在）：Zammad + 中间件上同机，先用模拟来电验通弹屏/建单/录音绑定全链路 —— 这是项目成立与否的关键；
  - 二期（验电话时）：先用一个纯 FreeSWITCH 容器（host 网络模式，占 SIP 5060 + RTP 端口段）接 400 线路验真实通话，中间件只认 FreeSWITCH 的话单和钩子，不依赖 FusionPBX 的页面；FusionPBX 管理界面等项目转正式时再上独立机器装。

## 二、部署顺序

1. **仓库可见性先处理**：本仓库现在是 Private，服务器上 `git clone` 会要求 GitHub 用户名 + Personal Access Token。两个办法二选一：把仓库改 Public（包里没有任何密钥，.env 已确认不在仓库里，改公开风险低，和排课仓库一样好拉）；或建一个只读该仓库的 Token，clone 时粘贴。部署前定好。
2. **跑安装脚本**：服务器以 root 登录 VNC，把 `deploy/install-server.sh` 整段粘贴执行（脚本为全英文输出）。它会：检查内存与现有容器 → 无 swap 时建 2G swap → clone 本仓库 → 拉官方 zammad-docker-compose 并写入共存配置（8899 端口、禁用 ES 服务、内存上限）→ 启动 Zammad → 生成中间件密钥 → 构建启动 bridge（只绑 127.0.0.1:8090）。
3. **Zammad 首次初始化**：浏览器开 `http://36.133.38.128:8899` 建管理员；然后在后台：
   - 设置 → 集成 → CTI (generic) 打开，记下 CTI 的 token；
   - Token Access 建一个 API token（admin 权限）；
   - 两个 token 填进服务器 `/opt/bike-callcenter/middleware/.env`，跑 `deploy/update-from-github.sh` 让 bridge 重读；
   - 跑字段脚本：`ZAMMAD_URL=http://127.0.0.1:8899 ZAMMAD_TOKEN=<api token> python3 /opt/bike-callcenter/zammad-setup/setup_bike_fields.py`；
   - 后台按脚本文件头清单建工单状态（新建→受理→已派单→现场处理中→已回传→待回访→办结）。
4. **模拟来电验收**：按 VERIFY.md A 组，用 `/simulate/call` 造一通假电话，确认 Zammad 自动建单且录音附件可播放。
5. **二期电话**（一期通过后另行安排）：FreeSWITCH 容器 + IVR/队列/Lua 钩子/mod_xml_cdr 接线 + 400 中继，按 VERIFY.md B 组验真实来电。

## 三、安全组清单（同机新增部分）

| 端口 | 用途 | 建议来源 |
|---|---|---|
| 8899 TCP | Zammad（本次新增放行） | 办公 IP 优先，测试期可临时全开 |
| 8090 TCP | 中间件 | 不对外（只绑 127.0.0.1） |
| 5060 UDP/TCP | SIP 信令（二期） | 运营商中继 IP 优先 |
| 16384–32768 UDP | RTP 语音媒体（二期） | 对端/运营商 |

排课系统的 8888 放行规则不动。

## 四、更新方式

代码更新：本仓库 push 到 GitHub → 服务器以 root 跑 `deploy/update-from-github.sh`（git pull + 重建 bridge）。Zammad 本体升级走官方 compose 说明，不要和业务代码混在一次更新里。要临时停掉 Zammad 给排课演示让路：`cd /opt/bike-callcenter/deploy/zammad && docker compose stop`，恢复用 `docker compose start`。
