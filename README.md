# astrbot_plugin_choice_gate

用「选择概率」决定一条消息**要不要触发 LLM**。灵感来自
[browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)：
一次请求扇出所有 choice head，严格校验答案，只消费被选中的那个 head，
校验不过就拒绝而不是猜。

和「在 system prompt 里写别乱回复」的区别：**被拒的消息根本不会进入 LLM 请求**，不占并发、
不花 token、也不写上下文。

## 工作方式

```
消息 → 会触发 LLM 吗？
        ├─ 指令 / @ / 引用我 / 管理员  → 直接放行（bypass）
        ├─ 去抖窗口内已回复够次数      → 静默
        └─ 一次请求问模型：
             decision: {RESPOND, IGNORE}
             reply_target: {1: ..., 2: ...}      ← 同一个请求里的投机 head
             校验（键集合相等、和为 1±0.02、argmax 一致）
             P(RESPOND) >= 阈值 ?  → 放行 : 静默（stop_event，不排队不调用）
```

放行时，选中的 `reply_target` 会作为 transient 提示注入本轮 LLM 请求，告诉模型该回哪条。

## 安装

把仓库放到 `AstrBot/data/plugins/` 下，然后在 WebUI「插件」页重载。

```bash
cd AstrBot/data/plugins
git clone https://github.com/AstrBotDevs/astrbot_plugin_choice_gate
```

## 配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | `true` | 总开关 |
| `backend` | `provider` | `provider` 复用 AstrBot 已配置的模型 / `openai` 任何 OpenAI 兼容端点 / `typesafe` TypeSafe 原生 questions 接口 |
| `provider_id` | 空 | 决策用哪个提供商；留空=当前会话的提供商 |
| `base_url` / `api_key` / `model` | DeepSeek 系 | `openai` / `typesafe` 后端的连接信息 |
| `disable_reasoning` | `true` | 选择题关掉推理，省掉大部分首 token 延迟 |
| `respond_threshold` | `0.5` | `P(RESPOND)` 低于它则静默，调高更保守 |
| `min_confidence` | `0.0` | 模型自报置信度下限 |
| `fail_mode` | `open` | 后端异常时放行（open）还是静默（closed） |
| `scope_private` / `scope_group` | `true` | 生效范围 |
| `bypass_command` / `bypass_mention` / `bypass_quote_bot` / `bypass_admin` | `true` | 明确被点名的场景直接放行 |
| `debounce_enable` / `debounce_window_sec` / `debounce_max_replies` | `true` / `25` / `1` | 一轮刷屏只回一次 |
| `reply_target` / `inject_hint` | `true` | 是否多问一个 head、以及是否把它提示给模型 |
| `history_max_messages` | `12` | 传给决策模型的消息条数 |
| `log_decisions` | `false` | 每次决策打一行日志 |

## 指令

```
/choicegate                      查看状态、统计与最近决策
/choicegate on|off               开关
/choicegate reset                清空去抖窗口与统计
/choicegate test <文本>          干跑一次：走完全相同的请求/校验/阈值路径并打印报告
/choicegate prompt               打印下一步 gate 请求的真实内容
```

`test` 是调参用的，不会写入真实转录、也不消耗去抖预算：

```
🧪 Choice Gate 测试
输入: 在吗
消息数: 4（未写入真实转录）
后端: openai / deepseek-chat
P(RESPOND)=0.130  confidence=0.900  target=-
→ 判定: 静默（p(RESPOND)=0.13 < 0.50）
阈值敏感性: >=0.30 静默 / >=0.50 静默 / >=0.70 静默 / >=0.90 静默
耗时: 412ms
head 概率:
  decision: RESPOND=0.130, IGNORE=0.870
去抖: 当前允许再回复（0/1 replies in window）
```

后端配错时 `test` 会直接把异常打出来（含 `fail_mode` 下的实际行为），省得去翻日志。

## 代价与边界（请先读）

- **它多花一次模型调用。** 对「每条消息本来就想回」的私聊场景，这是净亏；价值在群聊、多人
  共享的机器人、以及会被刷屏的场景。所以建议给决策单独配一个**便宜快的小模型**并关推理。
- **它只影响「本来就会触发 LLM」的消息。** 没被 @、没前缀、没引用机器人的群消息，AstrBot 本来
  就不会调用 LLM，不经过本插件。
- 只门控**真实用户消息**：机器人自己的消息、以及定时任务/其他插件直接发起的 LLM 请求一律不介入。
- 决策模型看到的是**聊天文本**，属于不可信输入；插件只把它当作判断依据，模型输出的 id 会与
  候选集合严格比对（编出来的 id 一律拒绝）。
- `fail_mode=open` 时后端故障不会影响可用性，只是退化成「不过滤」。

## 开发

```bash
pip install -r requirements.txt pytest ruff
ruff check .
pytest -q          # 纯逻辑测试，不需要安装 AstrBot
```

`choice_gate_core.py` 不依赖 AstrBot，决策路径可以独立测试；`main.py` 只负责把事件接到这些函数上。

## License

MIT
