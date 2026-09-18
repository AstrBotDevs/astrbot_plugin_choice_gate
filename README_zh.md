# astrbot_plugin_choice_gate

用 [TypeSafe Jev](https://docs.typesafe.ai/introduction) 的选择概率决定一条消息要不要触发
LLM。思路来自 [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)：
一次请求扇出所有 choice head，答案严格校验，只消费被选中的那个 head。

被拒的消息根本不会进入 LLM 请求：不占并发、不花 token、不写上下文。这是它和在 system
prompt 里写"别乱回复"的区别。

[English](README.md)

## 工作方式

```
消息 -> 会触发 LLM 吗？
   |- 指令 / @我 / 引用我 / 管理员            -> 直接放行
   |- 本轮的回复额度已用完                    -> 静默
   '- 一次 TypeSafe 请求：
        decision:     {RESPOND, IGNORE}
        reply_target: {1: ..., 2: ...}     <- 投机 head，同一个请求
        校验：id 集合完全相等、和为 1±0.02、argmax 一致
        P(RESPOND) >= 阈值 ?  -> 放行 : stop_event()（不排队）
```

放行时，选中的 `reply_target` 会作为 transient 内容挂到本轮请求上，告诉模型该回哪条。

## 准备

需要 TypeSafe API Key（typesafe.ai 获取）。Jev 很便宜（输入约 $0.042/MTok），这是它
能对每条候选消息都问一次的前提。

```bash
cd AstrBot/data/plugins
git clone https://github.com/AstrBotDevs/astrbot_plugin_choice_gate
```

在 WebUI 插件页重载，然后填 `api_key`。

## 配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | `true` | 总开关 |
| `language` | `auto` | `auto` 跟随 AstrBot 的 `language` 设置，未设置用英文 |
| `api_key` | 空 | TypeSafe API Key |
| `model` | `jev-latest` | TypeSafe 模型 |
| `base_url` | `https://api.typesafe.ai/v1/systemone` | TypeSafe 端点 |
| `timeout_sec` / `retries` | `20` / `1` | 超时，以及 429/503/529 的重试次数 |
| `respond_threshold` | `0.5` | P(RESPOND) 低于该值则静默 |
| `min_confidence` | `0.0` | 置信度低于该值则静默 |
| `fail_mode` | `open` | 异常时 `open` 照常回复，`closed` 静默 |
| `scope_private` / `scope_group` | `true` | 生效范围 |
| `bypass_command` / `bypass_mention` / `bypass_quote_bot` / `bypass_admin` | `true` | 明确被点名时直接放行 |
| `debounce_enable` / `debounce_window_sec` / `debounce_max_replies` | `true` / `25` / `1` | 一轮刷屏只回一次 |
| `reply_target` / `inject_hint` | `true` | 是否多问一个 head、是否提示给模型 |
| `history_max_messages` | `12` | 传给决策模型的消息条数 |
| `log_decisions` | `false` | 每次决策打一行日志 |

## 指令

```
/choicegate                       查看状态、统计与最近决策
/choicegate on|off                开关
/choicegate reset                 清空去抖窗口与统计
/choicegate test <文本>           按真实请求与阈值干跑一次
/choicegate prompt                打印下一步 gate 请求体
```

`test` 复用生产路径，不写入转录、不消耗去抖额度；端点或密钥配错时会把异常和当前
`fail_mode` 下的实际行为一起打出来。

## 代价与边界

- 每条被门控的消息多花一次 TypeSafe 调用。对"每条都想回"的私聊是净亏，价值在群聊和
  多人共享的机器人。
- 只影响本来就会触发 LLM 的消息。普通群消息不会唤醒机器人，也就不经过本插件。
- 只门控真实用户消息；定时任务、其它插件、机器人自己的消息都不介入。
- 聊天文本是不可信输入：模型给出的 id 一律与候选集合比对，不符就拒绝并走 `fail_mode`。

## 国际化

插件页与聊天回复统一从 `.astrbot-plugin/i18n/<locale>.json` 读取，即 AstrBot 在
`docs/zh/dev/star/guides/plugin-i18n.md` 中约定的布局。已带 `en-US` 与 `zh-CN`，
schema 里的英文文案作为兜底。新增语言只需加一个文件。

## 开发

```bash
pip install -r requirements.txt pytest ruff
ruff check .
pytest -q          # 纯逻辑与语言查找，不需要 AstrBot
```

`choice_gate_core.py` 是决策逻辑，`i18n.py` 是语言查找，`main.py` 只负责把 AstrBot 事件
接到这两者上。
