# FreeSWITCH / FusionPBX 侧接线说明

中间件有两条入线，缺一不可：

## 1. 实时事件线（负责弹屏）—— Lua 钩子

- 文件：`zammad_bridge.lua`，拷到 FreeSWITCH 的 `/usr/share/freeswitch/scripts/`。
- FusionPBX「Dialplan Manager」在 400 呼入拨号计划里、进 IVR/队列之前加一条 action：`lua zammad_bridge.lua newCall`；坐席接通的 leg 上加 `lua zammad_bridge.lua answer`。
- IVR 按键结果要写进通道变量 `ivr_choice`（FusionPBX 的 IVR 菜单本身会把按键带到后续动作；在进队列前加 `set ivr_choice=${menu_dtmf}` 一类的赋值，按实际菜单变量名调整）。中间件据此把工单分到 报修组/投诉组/咨询组/客服组。

## 2. 挂机话单线（负责建单 + 录音绑定）—— mod_xml_cdr

在 FusionPBX 服务器的 `autoload_configs/xml_cdr.conf.xml`（或 FusionPBX 的模块设置）里启用：

```xml
<configuration name="xml_cdr.conf" description="XML CDR">
  <settings>
    <param name="url" value="http://127.0.0.1:8890/cdr"/>
    <param name="encode" value="base64"/>
    <!-- 用自定义头带密钥时改为在中间件同机部署、用内网地址+防火墙收口；
         如xml_cdr版本支持headers参数，再加 X-Bridge-Secret -->
    <param name="log-b-leg" value="false"/>
    <param name="err-log-dir" value="/var/log/freeswitch/xml_cdr"/>
  </settings>
</configuration>
```

要点：
- 每通 CDR 的 `uuid` 就是 CallID，中间件以它做幂等键，重复投递不会重复建单；
- CDR 里需要带 `record_path`/`recording_url`（FusionPBX 的录音应用会把录音路径写进通道变量 `record_path`，确认拨号计划里录音开启且变量名一致）；
- 录音目录要让中间件读得到：同机部署时把 FreeSWITCH 录音目录以只读卷挂进 bridge 容器（见 `deploy/docker-compose.yml` 的注释），或让 `recording_url` 指向 FusionPBX 的录音下载地址。

## 3. IVR 菜单（对应需求清单）

在 FusionPBX「IVR Menus」新建菜单 `bike-hotline`，提示音文案：
「欢迎致电 XX 自行车客服，故障报修请按 1，投诉建议请按 2，业务咨询请按 3，其他请按 4。」

| 按键 | 去向（队列） | 中间件落单组 |
|---|---|---|
| 1 | 报修队列 | 报修组 / 类型=故障报修 |
| 2 | 投诉队列 | 投诉组 / 类型=投诉建议 |
| 3 | 咨询队列 | 咨询组 / 类型=业务咨询 |
| 4 | 综合队列 | 客服组 / 类型=其他 |
| 超时/无效 | 综合队列 | 客服组 |

队列在「Call Centers」里建：策略先用 Ring All/空闲优先试跑，坐席 5 名逐一添加并设置登录/置忙状态操作方式，写进坐席手册。

## 4. 语音留言（客户在用的功能，必须有）

老系统有语音留言：无人接听/非工作时间，客户留言后坐席在「语音留言列表」里听录音处理。
新系统的做法：留言同样变成一张工单（类型=语音留言、组=客服组、留言录音作附件），
坐席在 Zammad 左侧概览「语音留言」里集中处理（字段脚本自动建这个概览）。

FreeSWITCH 侧接法（队列无人接听或非工作时间时转留言）：

1. 拨号计划在进队列前设置：`recordings_dir`（与中间件共享的录音目录）、
   队列振铃超时后执行 `playback` 留言提示音，然后
   `record ${recordings_dir}/${uuid}.wav 300 200`（最长 300 秒录留言）；
2. 通道挂机后由 Lua 钩子（或一个专门的 hangup 拨号计划动作）向中间件发：
   `POST http://127.0.0.1:8890/events/voicemail`，带请求头 `X-Bridge-Secret`，
   JSON 体：`{"uuid": 通道uuid, "caller_id_number": 主叫, "destination_number": "400...", "billsec": 留言秒数, "duration": 总秒数, "start_epoch": 开始时间戳, "recording_path": "${uuid}.wav"}`；
3. 中间件按 uuid 幂等建单，重复投递不会重复建单；留言工单同样带回头客
   「历史提示」（按主叫号码统计历史问题关键词）。

也可以用 mod_voicemail 做信箱，但它的留言存在自己的库里、还要再同步，
不如上面「录音文件 + 中间件入线」直接。试跑阶段优先用上面的方式。

## 5. SIP 中继与安全组提醒

- 云安全组放行：5060 UDP/TCP（SIP 信令，按运营商要求可只对中继 IP 放行）、16384–32768 UDP（RTP 媒体）、443/80（FusionPBX 管理与录音下载，仅限办公 IP）、8899（Zammad）、8890（中间件：/test 测试页与事件接口，POST 均需 X-Bridge-Secret 密钥）。
- vars.xml 里 `external_rtp_ip` / `external_sip_ip` 填云主机公网 IP，避免接通无声。
