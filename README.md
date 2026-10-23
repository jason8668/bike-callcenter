# bike-callcenter —— 5 坐席自行车客服话务投诉工单系统（试跑包）

基于两个成熟开源项目组合，补齐它们之间缺失的中间件：

- **工单核心**：[Zammad](https://github.com/zammad/zammad)（6028★，AGPL-3，内置 Generic CTI 来电弹屏）
- **话务层**：[FusionPBX](https://github.com/fusionpbx/fusionpbx) + FreeSWITCH（IVR、队列、录音、SIP 中继）
- **中间件 bridge（本仓库自研）**：把 FreeSWITCH 的通话事件与话单送进 Zammad —— 来电弹屏、按 CallID 幂等建单、录音作为附件绑定工单、话务统计落库。核心逻辑零第三方依赖，6 个单元测试覆盖建单/幂等/未接不建单/CTI 转发。

## 文档

| 文档 | 内容 |
|---|---|
| `docs/01-zammad-code-summary.md` | Zammad 代码总结：结构、CTI、适配度、短板 |
| `docs/02-fusionpbx-code-summary.md` | FusionPBX/FreeSWITCH 代码总结：结构、队列/IVR/CDR、付费点 |
| `docs/03-gap-and-middleware-design.md` | 对照需求清单逐条缺口分析（D1–D8）+ 中间件设计 |
| `docs/04-github-upload-guide.md` | 把本包传到 GitHub 的操作步骤（GitHub Desktop 图形界面） |
| `docs/05-cloud-deploy-guide.md` | 云端部署：经 GitHub 拉取部署 + 验收清单 |

## 目录

```
middleware/     bridge 中间件 (FastAPI + 纯标准库核心逻辑, 含测试)
freeswitch/     Lua 事件钩子 + FusionPBX 接线说明 (IVR/队列/CDR)
zammad-setup/   自行车业务字段/组一键配置脚本
deploy/         docker-compose (bridge) + 服务器英文安装/更新脚本
VERIFY.md       试跑验收清单 (无真实电话线路也能验全链路)
```

## 快速验证（本地，无需服务器）

```bash
cd middleware && python3 -m unittest discover -s tests -v   # 应为 6 tests OK
```

## 许可证说明

本仓库自研代码（middleware/freeswitch/zammad-setup/deploy）按 MIT 使用；
Zammad 为 AGPL-3.0、FusionPBX/FreeSWITCH 各有其许可证，交付客户时保留各自许可证与源码说明。密钥只放在服务器 `.env`，禁止提交进 Git（见 `.gitignore`）。
