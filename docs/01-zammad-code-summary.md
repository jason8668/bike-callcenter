# Zammad 代码总结（工单核心）

仓库：https://github.com/zammad/zammad
配套部署仓库：https://github.com/zammad/zammad-docker-compose
数据核验时间：2026-10-23（GitHub 实时读取）

## 一、基本盘

| 项 | 内容 |
|---|---|
| Stars / Forks | 6,028 / 1,080 |
| 提交数 | 20,413（2012 年至今持续维护） |
| 许可证 | AGPL-3.0（可自用、可商用；对外提供服务或分发修改版时须公开对应源码，交付客户时要在合同里写清） |
| 技术栈 | Ruby on Rails（后端）+ Vue.js / TypeScript（新前端）+ CoffeeScript（旧前端）+ GraphQL / REST API |
| 数据库 | PostgreSQL（唯一支持）；全文检索用 Elasticsearch/OpenSearch（可选，小团队可关，见部署文档） |
| 部署 | 官方 Docker Compose / Helm / DEB / RPM 包 |

## 二、代码结构（按职责）

- `app/models/`：核心领域模型。`Ticket`（工单）、`Ticket::Article`（工单内的每条回复/备注，附件挂在这里）、`User`（客户与坐席都是 User，用 Role 区分）、`Organization`、`Group`（技能组/部门，工单的受理队列）、`Ticket::State` / `Ticket::Priority`（状态与优先级，可扩展）。
- `app/controllers/` + `lib/`：REST API（`/api/v1/tickets`、`/api/v1/ticket_articles`、`/api/v1/users` 等）、权限校验、搜索。
- CTI 模块：`app/controllers/cti_controller.rb` 一带实现 Generic CTI 接口 —— PBX 通过 HTTP POST 把通话事件推给 `/api/v1/cti/<token>`，事件包括 `newCall / answer / hangup`，每通以唯一 `callId` 为键。Zammad 据此做四件事：来电弹屏（显示主叫客户资料）、自动打开新建工单对话框、按号码检索该客户全部历史工单、生成通话日志（caller log，含待回访标记）。
- 自动化：`Trigger`（触发器：状态/字段变化时自动动作）、`Scheduler`（定时任务：如超时巡检）、`SLA`（首响/更新/解决时限 + 升级时间）、`Macro`（坐席一键动作组）。
- 报表：Overview 概览、Time Accounting、Reporting（按组/状态/时间导出，工单列表可导出 CSV；Excel 需经 CSV 或中间件转换，见缺口文档 D6）。
- 权限：Role + Group 两级 —— 角色决定能看哪些组、组决定工单可见范围；录音等敏感附件可通过内部备注（internal article，仅坐席可见）与客户隔离。

## 三、对本项目（自行车客服）的适配度

开箱即可用：
1. 工单全生命周期、自定义状态与优先级、坐席指派、组内流转；
2. 客户档案：按手机号自动归集同一客户的全部历史工单（= 来电弹屏的数据基础）；
3. Generic CTI：来电弹屏、通话日志、黑名单（CTI 层可对指定号码返回 reject，前提是 PBX 支持按返回值挂断）；
4. SLA 与触发器：实现「超时未处理自动提醒」的主体逻辑；
5. 组织/客户/工单全文检索：手机号、工单号、时间段检索。

需要配置或二开（本仓库已提供脚本/中间件）：
1. 自行车业务字段（单车编号、故障类型、投诉等级、地点、CallID、录音地址）—— 用 Zammad 的 ObjectManager 自定义属性添加，脚本见 `zammad-setup/setup_bike_fields.py`；
2. 工单状态机「新建→受理→派单→现场处理→回传结果→回访→办结/归档」—— Zammad 状态是单层枚举，没有「派单给外部运维人员」的子流程概念，需要新增状态 + 用 Group（客服组/运维组）+ 指派人表达流转，并用触发器做节点通知；
3. 录音回放：Zammad 本身不产生录音，只接收。录音文件由话务层产生，经本项目中间件作为附件上传到工单（Ticket::Article 附件），工单页即可直接点击播放/下载 —— 这是「强绑定」的实现方式；
4. 中文界面：官方多语言含简体中文（i18n 目录），启用即可，个别业务术语需在自定义字段名里落实。

## 四、短板与风险

1. 资源占用：全套（含搜索）官方建议 4GB+ 内存；5 坐席试跑可关闭 Elasticsearch（官方支持 `ELASTICSEARCH_ENABLED=false` + disable-elasticsearch 场景文件），内存可压到约 2GB；
2. CTI 只是「日志与弹屏」接口：Zammad 不能在页面里直接打电话（无内置软电话/WebRTC），坐席接听仍靠话机或 FreeSWITCH 软电话，页面负责弹屏与建单；
3. 它不知道「录音」为何物：CallID、录音地址、通话时长等话务元数据必须由中间件写入，否则工单与录音永远是两张皮 —— 这正是本项目中间件存在的理由；
4. AGPL 合规：给客户交付时保留许可证与源码获取说明。
