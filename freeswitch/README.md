# FreeSWITCH 侧接线说明（Docker 版，内部测试先行）

话务层跑在服务器上的一个容器里（`bike-freeswitch`，host 网络），
配置由 `deploy/install-freeswitch.sh` 从 `conf-templates/` 生成到
`conf-live/`（密码与密钥只在服务器上，不进 Git）。

> 安装前提（2026-10-24 排障结论）：Debian 官方仓库已下架整套
> FreeSWITCH（所有版本都搜不到），所以镜像改用 SignalWire 官方
> 仓库（Debian 12 底包）+ 显式包名 + 装完硬校验（缺二进制或缺
> 模块就让构建直接失败，不再出现“0.5 秒假成功”。SignalWire
> 仓库需要一个免费 Personal Access Token：去 signalwire.com 注
> 册免费账号，在账号设置里建一个 Token，只存一行到服务器文件
> `freeswitch/signalwire-token.txt`（不要发给任何人），再跑安装
> 脚本；脚本会先测仓库连通性，连不上会明说停下（那时再改
> 从源码编译的后备方案）。

```
客服系统  http://36.139.118.227:8899   (Zammad)
中间件    http://36.139.118.227:8890   (测试页 / 建坐席 / 来电看板)
话务层    36.139.118.227:5060 UDP     (SIP，无网页；状态用 fs_cli 看)
```

## 0. 内部测试先行，400 中继在后

- 现在装的是：3 个坐席分机 8001–8003、客服队列 8000（接得少的
  先分，见 §3）、语音留言 8009。坐席用软电话（MicroSIP / Zoiper /
  Linphone）注册到 36.139.118.227，内部互打和进队列都能测。
- 400 中继还没接：客户给中继参数（SIP 服务器/账号/密码/域名）后，
  再加一个网关 + 一个 DID 转队列的拨号计划，队列和工单逻辑不用改。
- 本阶段没有 IVR：按 2026-10-24 的定稿，来电是普通电话直接进队列，
  不做前置类别选择；故障类型、问题类型由客服接听时自己判断填写。

## 1. 实时事件线（负责弹屏）—— Lua 钩子

- 文件：`zammad_bridge.lua`，镜像里拷到 `/usr/share/freeswitch/scripts/`。
- 队列拨号计划（`conf-templates/dialplan/default.xml`）里：
  进队列前 `lua zammad_bridge.lua newCall`；`execute_on_answer` 在
  呼叫被坐席接通时再跑一次 `lua zammad_bridge.lua answer`。
- 拨号计划同时设三个通道变量：`bridge_event_url`
  （http://127.0.0.1:8890/events/call，host 网络下直达中间件）、
  `bridge_secret`（与中间件 `.env` 一致，安装脚本注入）、
  `bridge_ticket=1`（中间件认这个标记才建单；坐席互打没有它，
  只进通话统计，不建单）。

## 2. 挂机话单线（负责建单 + 录音绑定）—— mod_json_cdr

`conf-templates/json_cdr.conf.xml`：

```xml
<configuration name="json_cdr.conf" description="JSON CDR">
  <settings>
    <param name="url" value="http://127.0.0.1:8890/cdr?secret=__BRIDGE_SECRET__"/>
    <param name="log-b-leg" value="false"/>
  </settings>
</configuration>
```

要点：
- 用 JSON 不用 XML：中间件 `/cdr` 直接读 JSON；`log-b-leg=false`
  只发主叫（A）腿的话单，队列的坐席腿不会到中间件，避免重复建单。
- json_cdr 发不了自定义请求头，所以密钥放 URL 的 `?secret=` 里
  （只走本机回环，密钥与 `.env` 文件同机）。
- 每通 CDR 的 `uuid` 就是 CallID，中间件以它做幂等键。
- 录音目录与中间件共享：FreeSWITCH 录到 `/recordings`（宿主机
  `recordings/`），中间件按文件名在同一目录找录音挂到工单上。
- 语音留言腿另有 `bridge_voicemail=1` 标记，话单只记统计，
  工单由留言专线负责建（见 §4），两边顺序不影响结果。

## 3. 队列与分配原则

`conf-templates/callcenter.conf.xml`：队列名「客服队列」，策略
`agent-with-fewest-calls`（接得少的先分）：队列自动把下一通
电话派给累计接听最少的坐席，三个人的数量追平后又重新轮流；
置忙（On Break）和未登录的坐席直接跳过，不分单。

坐席状态有三种：`Available`（接单中）、`On Break`（置忙）、
`Logged Out`（未登录）。坐席自己置忙有两条路：
- 看板页（8890/board）「我的坐席状态」里点「置忙」/「置闲」；
- 话机上直拨 *51 置忙、*52 置闲（拨号计划里的功能码）。
状态查询与手工设置见 §6 的 fs_cli 命令。
以后要的「指定一人接听」就是队列里只留那一个坐席 Available。

留言的分配用同一原则（中间件算）：留言工单派给当前接单中
且（已接电话＋已派留言）合计最少的坐席，置忙的跳过；三个
人的留言计数存在中间件数据库里，重启不丢。电话的计数在话
务层、留言的计数在中间件，两边口径一致（坐席在 Zammad 里
的账号名请用分机号 8001/8002/8003，这样话单里的接听人才
能对上号）。

## 4. 语音留言（客户在用的功能，必须有）

队列 60 秒无人接听 → 转 8009 留言分机：接通、录音
（最长 300 秒、静音 5 秒停），挂机钩子 `zammad_voicemail.lua`
向中间件发 `POST /events/voicemail`，带 `X-Bridge-Secret`：

```json
{"uuid": "通道uuid", "caller_id_number": "主叫",
 "destination_number": "8009", "billsec": "留言秒数",
 "duration": "总秒数", "start_epoch": "开始时间戳",
 "recording_path": "uuid.wav"}
```

中间件按 uuid 幂等建单（类型=语音留言、组=客服组、留言录音作
附件），坐席在 Zammad 左侧概览「语音留言」里集中处理。
不用 mod_voicemail：留言存在自己的库里还要再同步，不如
「录音文件 + 中间件入线」直接。

## 5. 安全组与软电话

云安全组放行（入方向）：
- UDP 5060（SIP 信令；接 400 中继后可收紧到中继 IP）
- UDP 16384–16884（RTP 媒体；配置里已收窄，不是默认的 3 万多端口）

软电话配置：服务器 36.139.118.227、账号 8001–8003、密码见
服务器上 `/opt/bike-callcenter/freeswitch/extensions.txt`、
传输 UDP、编码 PCMA/PCMU。事件端口 8021 只绑 127.0.0.1，
不要在安全组放行（fs_cli 只在服务器本机用；中间件看板页
的置忙按钮走同一端口，密钥存在服务器文件里，不进 Git）。

## 6. 怎么看话务层（没有网页）

FreeSWITCH 没有网页管理面，状态都在服务器命令行看：

```bash
cd /opt/bike-callcenter/deploy
docker compose exec freeswitch fs_cli -p $(cat /opt/bike-callcenter/freeswitch/esl-password.txt)
```

进去以后常用命令：

```
status                    # 运行状态与当前通话数
show calls                # 实时通话
show channels             # 实时通道
sofia status              # SIP profile 与注册情况
sofia status profile internal reg   # 哪些分机已注册
callcenter_config queue list        # 队列与等待情况
callcenter_config agent list        # 坐席状态
callcenter_config agent set status 8001 'On Break'   # 置忙
callcenter_config agent set status 8001 'Available'  # 置闲
```

容器日志：`docker compose logs -f freeswitch`。
业务侧的通话记录照样在 Zammad 通话日志和中间件统计
（`/stats/daily`、Excel 导出）里看。

## 7. 已知待验证点（内部测试就是用来验这些的）

- `execute_on_answer` 在队列接通时是否触发：若不触发，工单会
  晚到挂机时才由 CDR 建（看板不弹），那时把队列拨号计划改回
  直桥振铃（ring-all）方案。
- 提示音：队列等待音目前是单音 beep，不是语音；正式上线前换
  成录好的中文提示音文件。
- 400 中继参数到齐后补网关配置与 DID 路由，队列与工单不动。
