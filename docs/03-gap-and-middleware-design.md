# 缺口分析与中间件设计

对照需求：《5 坐席自行车客服话务投诉工单系统》功能清单（2026-10-23）
图例：✅ 原生/配置可用｜🧩 本项目中间件补齐｜🔧 需要业务二开（已在包内给脚本/设计，未含页面开发）｜⚠️ 原生缺陷/付费点

## 一、逐条对照

### 话务模块（CTI / IPPBX）

| # | 需求 | 结论 | 落点 |
|---|---|---|---|
| 1 | 400 接入、SIP 中继、呼入排队 | ✅ | FusionPBX 网关 + FreeSWITCH mod_callcenter 队列 |
| 2 | IVR（1 报修 / 2 投诉 / 3 咨询 / 4 其他） | ✅ | FusionPBX IVR 菜单；按键结果经中间件写入工单 `ticket_type`，决定工单进入报修组还是投诉组 |
| 3 | 来电弹屏（用户信息/历史工单/历史通话） | 🧩 | Zammad CTI 有弹屏能力，但事件必须有人推：中间件/ Lua 钩子把 newCall 事件推到 Zammad CTI 接口（缺陷 D1） |
| 4 | 坐席分配、队列策略、坐席状态 | ✅ | mod_callcenter 策略 + FusionPBX 坐席管理；状态同步到 Zammad 仅作展示时需另推（非必需） |
| 5 | 保持、转接、三方、内线互拨 | ✅ | FreeSWITCH 基础能力 |
| 6 | 全程录音、唯一 CallID、录音元数据同步工单 | 🧩 | 本项目核心缺口（缺陷 D2）：FreeSWITCH 产生录音与 UUID，Zammad 不认识录音 —— 中间件在挂机后建单/补单并把录音作为附件上传，实现强绑定 |
| 7 | 话务统计报表（呼入量/接通率/等待/坐席量/放弃/高峰） | ⚠️🧩 | FusionPBX 增强报表是付费会员应用（缺陷 D3）；中间件把每通 CDR 落入本地 SQLite/Postgres 并提供 `/stats/daily` 汇总接口与 CSV 导出，后续可接 BI |
| 8 | 黑名单拦截 | ✅ | FusionPBX 拨号计划拦截 + Zammad CTI 层拦截名单，两处任选其一，建议放话务层（未接通不计费） |

### 工单模块（自行车报修投诉）

| # | 需求 | 结论 | 落点 |
|---|---|---|---|
| 1 | 来电自动新建工单，回填号码/时间/CallID/录音地址 | 🧩 | 中间件：newCall 时可选预建单，hangup 时以 CallID 幂等建单/补录音（同一 CallID 永远只生成一张工单） |
| 2 | 工单字段自定义（手机号/单车编号/故障类型/投诉等级/地点/描述/照片） | 🔧 | `zammad-setup/setup_bike_fields.py` 一键创建全部自定义属性；照片走工单附件 |
| 3 | 工单流转（新建→受理→派单→现场→回传→回访→办结/归档） | 🔧 | Zammad 无「派单子流程」概念（缺陷 D4）：用新增状态集 + 客服组/运维组 + 指派表达流转，状态清单已写进配置脚本说明，需在 Zammad 后台按清单建状态与触发器 |
| 4 | 状态跟踪、超时预警 | ✅🔧 | Zammad SLA + 触发器 + 定时巡检；中间件另提供 `/tickets/overdue` 思路（按 SLA 字段查），页面提醒样式需二开 |
| 5 | 工单与录音强绑定、页内播放器回放 | 🧩 | 录音以附件形式进入工单文章，浏览器可直接播放 wav/mp3（缺陷 D2 的交付标准）；同时工单上保存 `recording_url` 原始地址双保险 |
| 6 | 工单检索（手机号/单车编号/工单号/时间/故障类型） | ✅ | Zammad 搜索 + 自定义属性可检索（单车编号、故障类型建为可搜索属性） |
| 7 | 办结后自动回访外呼 | ⚠️🔧 | 两套开源系统都没有「按工单状态自动外呼并回收结果」的成品功能（缺陷 D5）：设计为中间件监听工单办结 → 调 FreeSWITCH `originate` 发起外呼 → 结果写回工单备注；本包给出接口设计与 originate 参数，自动外呼的拨打策略（时段/重试）需按客户要求再定 |
| 8 | 权限管理（管理员/坐席/运维分级，录音权限） | ✅🔧 | Zammad Role+Group 原生支持；录音附件放在 internal 文章里，运维角色默认不可见录音、客服可见 —— 按角色清单配置 |
| 9 | 导出报表（工单汇总/故障统计/投诉分析，Excel） | ⚠️🧩 | Zammad 原生以 CSV 为主，Excel .xlsx 需转换（缺陷 D6）：中间件提供 `/export/tickets.xlsx` 的实现思路与依赖（openpyxl 可选），试跑阶段先用 CSV 验证数据完整性 |

## 二、缺陷清单（汇总）

- D1 弹屏无人触发：Zammad CTI 是被动接口，FusionPBX 默认不向它推事件。
- D2 录音与工单两张皮：录音在话务服务器、工单在 Zammad，无任何原生关联字段。
- D3 话务报表付费：FusionPBX 的 Call Center 报表/Wallboard 是会员应用，开源版只有原始 CDR。
- D4 无派单子流程：通用工单的状态机表达不了「派给外部运维 + 现场回传」的行业流程，需要状态集与组配合建模。
- D5 无自动回访外呼：工单办结与外呼系统之间没有原生联动。
- D6 Excel 导出不原生：Zammad 以 CSV 为主，客户明确要 Excel 时需加转换层。
- D7 坐席软电话不在 Zammad 内：页面不能直接接听，坐席需另配软电话/话机（FusionPBX 侧解决）。
- D8 中文与行业术语：故障类型、投诉等级等字典要在部署时按自行车业务落值，不能用系统默认字典交付。

其中 D1、D2、D3（数据落库部分）由本包中间件直接补齐；D4、D8 由配置脚本 + 后台清单补齐；D5、D6 给出可运行的接口骨架与明确的实现边界；D7 在部署文档中说明坐席终端方案。

## 三、中间件设计（bridge）

```
来电 ──► FreeSWITCH/FusionPBX ──(Lua 钩子/mod_xml_cdr, HTTP)──► bridge 中间件 ──► Zammad
              │  newCall/answer/hangup 事件                         │ 1) 转推 Zammad CTI 接口 → 弹屏+通话日志
              │  挂机 CDR: uuid, 主被叫, 时长, 录音路径            │ 2) 以 uuid 为 CallID 幂等建单, 回填字段
              └─ 录音文件 (wav) ─────────────────────────────────► │ 3) 下载/读取录音, 作为附件上传工单 → 页内回放
                                                                   │ 4) CDR 落库 → /stats 话务汇总
```

设计要点：
1. 幂等：以 CallID（FreeSWITCH uuid）为唯一键，事件重推、CDR 重发都只对应一张工单（本地 SQLite 记录 call_id → ticket_id 映射）；
2. 时序容错：newCall 时客户可能还没建档，先推 CTI 弹屏；hangup 后才正式建单，避免大量未接通来电生成垃圾工单（未接通只进通话日志与话务统计，不建单，可配置）；
3. 客户归集：按主叫号码在 Zammad 查客户（`users/search`），查不到自动建档（姓名先用号码占位），保证历史工单按人聚合、弹屏有内容；
4. 录音双通道：优先把录音文件作为附件上传（强绑定、权限随工单）；上传失败时降级为在工单写入 `recording_url`（FusionPBX 录音访问地址），并在中间件日志告警；
5. 安全：Zammad API 用 Token 认证（Token 只放服务器 .env，不进 Git）；中间件入站用共享密钥头 `X-Bridge-Secret` 校验，防止伪造话单；
6. 部署形态：单个 Python 服务（FastAPI），Docker 运行，与 Zammad 同机；无状态部分可重启，映射表用 SQLite 卷持久化。

接口一览（实现见 `middleware/app.py`，核心逻辑 `middleware/bridge_core.py`）：
- `POST /events/call`：接收 Lua 钩子事件 {event, call_id, from, to, direction, agent}
- `POST /cdr`：接收 mod_xml_cdr / FusionPBX 话单（form 或 JSON），触发建单+录音绑定
- `GET  /health`：存活检查
- `GET  /stats/daily?date=YYYY-MM-DD`：当日话务汇总（呼入量、接通、放弃、平均等待/通话时长）
- `POST /simulate/call`：试跑专用 —— 模拟一通完整来电（newCall→answer→hangup+假录音），用于无真实线路时验证弹屏、建单、录音附件全链路
