#!/usr/bin/env python3
"""One-shot Zammad setup for the bicycle customer-service project.

Creates (idempotently — existing objects are skipped):
  1. Ticket groups: 客服组 / 报修组 / 投诉组 / 咨询组 / 运维组
  2. Ticket custom attributes (ObjectManager):
       call_id        通话唯一ID (text, unique-ish, searchable)
       recording_url  录音访问地址 (text)
       bike_no        单车编号 (text, searchable)
       fault_type     服务类型 (select, aligned with the legacy Kinghav
                      system's vocabulary for docked public bikes:
                      还车异常/借车异常/锁桩故障/车辆损坏/掉链/乱停放/扣费退款/其他)
       complaint_level 投诉等级 (select: 一般/重要/紧急)  [used on complaint tickets]
       service_address 服务地点 (text)
       ticket_type    工单类型 (select: 故障报修/投诉建议/业务咨询/其他)
  2b. User custom attribute:
       rent_card_no   租车卡号 (text) — the legacy system identifies
                      customers by phone / rental card no. / ID; phase 1
                      keys on phone, the card number is kept on the profile.
  3. One SLA: 首响 30 分钟 / 解决 24 小时 (工作时间日历需在后台另建后绑定)

Ticket STATES must be created in the admin UI (Admin -> Objects/States) because
the public API does not cover state creation on all versions. Required state
list for this project (in flow order):
  new 新建 -> open 受理 -> dispatched 已派单 -> field_work 现场处理中
  -> returned 已回传 -> callback 待回访 -> closed 办结 (closed) / merged 归档

Usage:
  ZAMMAD_URL=http://127.0.0.1:8899 ZAMMAD_TOKEN=xxx python3 setup_bike_fields.py
Uses only the Python standard library.
"""
import json
import os
import urllib.request
import urllib.error

BASE = os.environ.get("ZAMMAD_URL", "http://127.0.0.1:8899").rstrip("/")
TOKEN = os.environ["ZAMMAD_TOKEN"]

GROUPS = ["客服组", "报修组", "投诉组", "咨询组", "运维组"]

ATTRIBUTES = [
    ("call_id", "通话ID", "input", {"type": "text", "maxlength": 64}),
    ("recording_url", "录音地址", "input", {"type": "text", "maxlength": 255}),
    ("bike_no", "单车编号", "input", {"type": "text", "maxlength": 64}),
    ("ticket_type", "工单类型", "select",
     {"options": {"故障报修": "故障报修", "投诉建议": "投诉建议",
                  "业务咨询": "业务咨询", "其他": "其他"}}),
    ("fault_type", "服务类型", "select",
     {"options": {"还车异常": "还车异常", "借车异常": "借车异常",
                  "锁桩故障": "锁桩故障", "车辆损坏": "车辆损坏",
                  "掉链": "掉链", "乱停放": "乱停放",
                  "扣费退款": "扣费退款", "其他": "其他"}}),
    ("complaint_level", "投诉等级", "select",
     {"options": {"一般": "一般", "重要": "重要", "紧急": "紧急"}, "default": "一般"}),
    ("service_address", "服务地点", "input", {"type": "text", "maxlength": 255}),
]

USER_ATTRIBUTES = [
    ("rent_card_no", "租车卡号", "input", {"type": "text", "maxlength": 64}),
]


def call(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=body, method=method,
        headers={"Authorization": f"Token token={TOKEN}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def ensure_group(name):
    status, data = call("GET", "/api/v1/groups")
    if status == 200 and any(g.get("name") == name for g in data):
        print(f"group exists, skip: {name}")
        return
    status, _ = call("POST", "/api/v1/groups", {"name": name, "active": True})
    print(f"group {name}: HTTP {status}")


def ensure_attribute(name, display, data_type, data_option, obj="Ticket"):
    status, data = call("GET", "/api/v1/object_manager_attributes")
    if status == 200 and any(a.get("name") == name and a.get("object") == obj
                              for a in data):
        print(f"attribute exists, skip: {obj}.{name}")
        return
    if obj == "User":
        screens = {"edit": {name: 1}, "view": {name: 1}}
    else:
        screens = {"create_top": {name: 1}, "create_middle": {}, "edit": {name: 1}}
    payload = {
        "object": obj, "name": name, "display": display,
        "data_type": data_type, "data_option": data_option,
        "active": True, "screens": screens,
    }
    status, _ = call("POST", "/api/v1/object_manager_attributes", payload)
    print(f"attribute {obj}.{name}: HTTP {status}")


def main():
    for g in GROUPS:
        ensure_group(g)
    for attr in ATTRIBUTES:
        ensure_attribute(*attr)
    for attr in USER_ATTRIBUTES:
        ensure_attribute(*attr, obj="User")
    # ObjectManager changes only take effect after a DB migration run.
    status, _ = call("POST", "/api/v1/object_manager_attributes_execute_migrations")
    print(f"execute migrations: HTTP {status}")
    print("Done. Now create the ticket states listed in this file's docstring"
          " in Admin UI, and bind the SLA calendar.")


if __name__ == "__main__":
    main()
