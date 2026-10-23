# FusionPBX + FreeSWITCH 代码总结（话务层）

仓库：https://github.com/fusionpbx/fusionpbx
底层交换：FreeSWITCH（https://github.com/signalwire/freeswitch）
数据核验时间：2026-10-23（GitHub 实时读取）

## 一、基本盘

| 项 | 内容 |
|---|---|
| Stars / Forks | 1,077 / 792，提交 24,522 |
| 许可证 | FusionPBX 主体 MPL-1.1 等（以仓库内各文件头为准）；注意：官方的 Call Center Wallboard、Call Center 报表、REST API 等「会员应用」是付费会员内容，不在开源仓库里 |
| 技术栈 | PHP（Web 管理与业务逻辑）+ PostgreSQL/SQLite + FreeSWITCH（C 语言软交换，SIP/RTP 媒体与呼叫控制） |
| 定位 | 多租户 PBX / 呼叫中心服务器 / 语音应用服务器的 Web 管理面；真正的呼叫、录音、IVR 引擎是 FreeSWITCH |

## 二、代码结构（按职责）

- `core/`：用户、域（多租户）、权限、菜单、系统设置等平台能力。
- `app/`：功能应用集，每个应用一个目录。与本项目相关的：
  - `app/call_centers/`：呼叫中心队列 —— 坐席（Agent）、队列（Queue）、分层（Tier），支持空闲优先/轮询等分配策略、坐席状态；
  - `app/ivr_menus/`：IVR 菜单 —— 录制/上传提示音，按键路由（如 1 报修、2 投诉、3 咨询、4 其他分别进不同队列/分机）；
  - `app/ring_groups/`、`app/call_flows/`、`app/time_conditions/`：振铃组、呼叫流、时间条件（工作时间/非工作时间不同走向）；
  - `app/call_recordings/`：通话录音的索引与回放入口（录音本体由 FreeSWITCH 写到磁盘，一般为 `/var/lib/freeswitch/recordings/<domain>/...`）；
  - `app/xml_cdr/`：话单（CDR）—— 每通一个唯一 `xml_cdr_uuid`（即本项目的 CallID 来源），含主被叫、起止时间、时长、挂断原因、录音文件路径字段；支持把 CDR 以 HTTP POST 推送到外部 URL（mod_xml_cdr）；
  - `app/gateways/`、`app/sip_profiles/`：SIP 中继网关（对接运营商 400 线路）、SIP Profile（5060/5061 等）；
  - `app/destinations/`、`app/dialplans/`：号码目的地与拨号计划（呼叫的实际路由规则，最终编译给 FreeSWITCH 执行）。
- FreeSWITCH 侧关键模块：`mod_sofia`（SIP）、`mod_callcenter`（队列引擎）、`mod_fifo`、`mod_av`/`mod_sndfile`（录音）、`mod_xml_cdr`（话单推送）、`mod_curl`/`mod_lua`（事件回调与脚本钩子，本项目中间件对接点）、`mod_event_socket`（ESL 事件总线）。

## 三、对本项目的适配度

开箱即可用：
1. 400 号码经 SIP 中继网关接入、呼入排队（mod_callcenter）；
2. IVR 按键菜单与按时段路由；
3. 坐席分配策略、坐席状态（FusionPBX 只是状态的管理面之一，实际登录/置忙在队列引擎里）；
4. 保持、转接、三方、内线互拨 —— FreeSWITCH 基础能力；
5. 全程录音 + 每通唯一 UUID + CDR 话务统计原始数据（呼入量、接通、等待、放弃都可从 CDR 算出）；
6. 黑名单：拨号计划层面对主叫号码做拦截规则。

需要配置或二开（本仓库已提供）：
1. 录音与工单绑定：原系统录音只躺在话务服务器上，Zammad 侧看不见 —— 由中间件在挂机后把「UUID + 录音」送进工单（见 03 号文档）；
2. 话务统计报表：开源版只有 CDR 原始数据与简单列表，官方报表是付费会员应用 —— 本项目用中间件落库 + Zammad/Excel 导出补齐，或按 CDR 自行做汇总脚本；
3. 来电弹屏：FusionPBX 自己没有 CRM 弹屏，弹屏发生在 Zammad 侧，靠 CTI 事件推送实现。

## 四、短板与风险

1. 安装方式：官方推荐在裸机/虚机上跑安装脚本（Debian 优先，Ubuntu 有对应脚本），不是 Docker 原生应用；与 Zammad 的 Docker 部署要错开端口（FusionPBX 的 Nginx 占用 80/443，Zammad 试跑用 8080）；
2. 版本匹配：FreeSWITCH 与 FusionPBX 要用其安装脚本配套的版本组合，不要单独升级其中一个；
3. SIP/RTP 网络：5060（UDP/TCP）与 RTP 媒体端口段（默认 16384–32768 UDP）要在云安全组放行，NAT 场景要正确配置 external_rtp_ip，否则会出现「接通无声」；
4. 会员应用付费点：Wallboard、增强报表、REST API 属会员功能，报价时不要默认包含。
