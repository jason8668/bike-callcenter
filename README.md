# bike-callcenter —— 3 坐席自行车客服电话记录系统（试跑包）

> 方向说明（2026-10-27 定）：这是一套**电话记录系统**，不是工单系统。
> 没有流程、没有派单/回访/办结。一通电话 = 一条记录：谁打来的、
> 什么时候、多长时间、哪个坐席接的、问的什么问题、录音。
> 2026-10-27 之前按工单系统（Zammad）做的部分已经作废，见文末说明。

基于 FreeSWITCH 话务层 + 自研记录服务组合：

- **话务层**：FreeSWITCH（队列、录音、SIP 中继、语音留言）+ 3 台 IP 话机
- **记录服务 bridge（本仓库自研）**：把 FreeSWITCH 的通话事件与话单存成记录 ——
  接通时记录就弹出来（坐席通话中可补填问题）、按 CallID 幂等建记录、
  录音绑在记录上可播放、接得少先分、置忙跳过、按坐席统计与历史、Excel 导出。
  核心逻辑零第三方依赖，29 个单元测试覆盖建记录/幂等/未接留记录/分流/置忙/补填不被冲掉。

## 文档

| 文档 | 内容 |
|---|---|
| `docs/03-gap-and-middleware-design.md` | 对照需求清单逐条缺口分析（D1–D8）+ 中间件设计（工单版，仅供参考，以记录系统为准） |
| `docs/04-github-upload-guide.md` | 把本包传到 GitHub 的操作步骤（GitHub Desktop 图形界面） |
| `docs/05-cloud-deploy-guide.md` | 云端部署：经 GitHub 拉取部署 + 验收清单 |
| `docs/06-operation-guide.md` | 日常运维指南（部分章节仍按工单版写，以 VERIFY.md 为准） |

## 目录

```
middleware/     记录服务 (FastAPI + 纯标准库核心逻辑, 含测试)
freeswitch/     Lua 事件钩子 + 队列/留言/录音配置模板
deploy/         docker-compose (记录服务 + FreeSWITCH) + 服务器英文安装/更新脚本
VERIFY.md       试跑验收清单 (无真实电话线路也能验全链路)
```

## 快速验证（本地，无需服务器）

```bash
cd middleware && python3 -m unittest discover -s tests -v   # 应为 29 tests OK
```

## 退役说明（2026-10-27，工单方向作废后保留）

以下文件/目录属于已作废的工单（Zammad）方向，留着仅供参考，
不要再按它们部署：`deploy/zammad-*.rb`、`deploy/zammad-override.yml`、
`deploy/apply-cn-setup.sh`、`deploy/grant_agent_access.rb`、
`deploy/simplify_roles_groups.rb`、`deploy/delete_test_users.rb`、
`deploy/diag_translations.rb`、`deploy/create_agent_users.rb`、
`zammad-setup/`、`docs/01-zammad-code-summary.md`、
`docs/02-fusionpbx-code-summary.md`。生产部署只用
`deploy/docker-compose.yml`（记录服务 + FreeSWITCH）与
`deploy/install-freeswitch.sh`。

## 许可证说明

本仓库自研代码（middleware/freeswitch/deploy）按 MIT 使用；
FreeSWITCH 各有其许可证，交付客户时保留各自许可证与源码说明。
密钥只放在服务器 `.env`，禁止提交进 Git（见 `.gitignore`）。
