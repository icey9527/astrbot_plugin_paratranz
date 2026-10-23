# ParaTranz 翻译项目助手 (astrbot_plugin_paratranz)

> **AI 编写声明**：本插件的代码与文档由 AI（DeepSeek）以 vibe coding 方式生成，可能存在疏漏，使用前请自行评估并测试。

基于 ParaTranz OpenAPI（`https://paratranz.cn/api-docs`）的查询插件。默认只读；写操作要过群白名单、授权等级、二次确认。

> 本插件为第三方作品，与 ParaTranz 官方无关，未获得官方背书。图标与横向 wordmark 使用 ParaTranz 官方标识，仅用于说明所对接的服务。

## 菜单

群里发 `/pt`（或 `/pt 菜单`），按平台自动选形态：

| 平台 | 形态 |
| --- | --- |
| qq_official（QQ 开放平台官方机器人） | markdown 正文 + **真按钮**，点一下等于自己发那条指令 |
| aiocqhttp（NapCat / LLOneBot 等协议端） | 图片菜单 |
| 其他 / 渲染失败 | 纯文本 |

按钮走 `/v2/groups/{openid}/messages` 的 `msg_type=2` + `keyboard`，每个按钮 `action.type=2`（指令按钮，官方语义：0 跳转 / 1 回调 / 2 指令）+ `action.enter=true`，点击后客户端自动 @bot 并直接发出 `data` 里的指令文本（比如 `/pt 项目`），所以插件不用处理交互回调。**千万别写成 `type=1`**，那是回调按钮，QQ 会去请求你配置的后端地址，内网部署必然弹「请求第三方失败」。

指令按钮会在群里留下一条用户消息，这是客户端行为，改不了。想做到「点击不产生群消息、机器人静默响应」，必须用回调按钮 `type=1` + `INTERACTION_CREATE` + 公网可达的 HTTP 回调地址，并调 `PUT /interactions/{id}` 回应；纯内网环境做不了。

按钮布局一行两个、共 5 行 10 个：项目进度 / 文件进度、术语查词 / 词条列表、成员贡献 / 我的权限、搜索项目 / 查看绑定、授权名单 / 完整指令。需要关键词的按钮（术语、词条、搜索）用 `enter=false`，点了只把指令填进输入框，用户补完参数再发；其余点了直接发送。完整指令清单走 `/pt 指令`（纯文本）。

协议端没有这个接口，客户端也渲染不出官方按钮，只能图片。配置项 `menu_buttons` / `menu_image` 可分别关掉。

图片菜单优先走本地 Pillow 渲染，失败再退 AstrBot 的 t2i 服务（远端接口偶尔会把一段 `Internal Server Error` 当图片存盘，所以要校验），都不行才退纯文本。图片按内容做 md5 缓存 10 分钟。

菜单图上会贴 ParaTranz 的横向 wordmark：`logo_wide.png` 是从官方 SVG 转出来的（800×180），渲染完用 PIL 贴到顶部并裁掉本地渲染器自带的 91px AstrBot 抬头。图片渲染全程离线，不依赖外链。官方机器人那条 markdown 里要的是**公网可访问的图**，本地宽图没法直接用，所以走 QQ 素材库：`POST /v2/media-images/prepare` 拿预签名 URL → PUT 二进制 → `POST /v2/media-images/complete` 换到 `qqbot-media://image/v1/<token>` 引用，随后 `![ParaTranz #240px #54px](qqbot-media://...)`。上传结果按 md5 缓存在 `plugin_data/astrbot_plugin_paratranz/media_logo.json`，logo 不变就不会重复上传（素材本身 90 天滑动过期）。上传不可用时退回正方形公链 `#96px #96px`。注意尺寸必须写，不写 `#宽px #高px` 客户端渲染不出来。

## 图片输出

`/pt 项目` 默认输出一张 720 宽的进度图，样式对齐官网项目概览：顶部横向 logo、`#ID 项目名`、文件数/有疑问/更新时间，中间 `总字数` `总条数` 两行纯数值，下面 `已翻译/已检查/已审核` 三条进度条（蓝/绿/灰，条内标百分比，右侧标绝对条数）。

数据直接取 `GET /projects/{id}` 返回的 `stats`（`total / translated / checked / reviewed / locked / disputed / words / tp / cp / rp`）和 `filesCount`，**一次请求就够**，不用再拉文件列表；只有 `stats` 缺失时才回退到 `/files` 累加。

`/pt 文件`、`/pt 术语`、`/pt 成员`、`/pt 词条`、`/pt 搜索` 同样出图：

| 命令 | 图 |
| --- | --- |
| `/pt 文件` | 总进度条 + 每文件一条进度条（完成绿、未完蓝）、条数与占比，底部提示总文件数 |
| `/pt 术语` | 两列表（原文 / 译文，备注接在译文后） |
| `/pt 成员` | 排行榜：奖牌/名次 + 头像 + 昵称（含 QQ 名）+ PP 徽标 + 在线状态 + 译/编/审 + 加入日期 |
| `/pt 词条` | 每条三段：`#ID 状态`、原文、译文 |
| `/pt 搜索` | 两列（`#ID 名称` / 简介） |

渲染全在 `render.py`，纯 PIL，不依赖 AstrBot 的 t2i：

- **配色照抄官网**：`assets/index-*.css` 里 `.progress{background:#e9ecef;border-radius:6px;height:1rem;font-size:.75rem}`、`.progress-bar{color:#fff;background:#007bff}`，绿色 `#28a745`，文字 `#212529` / `#6c757d` / 分割线 `#dee2e6`。
- **排行榜尺寸**：宽度按内容自动算（先量一遍名字/徽标/统计行/日期的最大宽度，加上名次列、头像、间距），不再固定 720 留一片空白；高度按人数逐行增长，一行一个人。
- **排行榜细节**：名次 1–3 用 FontAwesome 奖杯（金 `#f5a623` / 银 `#a8b0ba` / 铜 `#c97b3c`），4 以后橙色数字；头像方形裁切 + 15% 圆角（对应官网 `.avatar.square{border-radius:15%}`），下载失败退首字母占位；PP 徽标绿 `#23b26e`，在线徽标蓝 `#18b0f0` 带 wifi 图标、离线灰底带时钟 + 「x 前在线」；排序按 `totalPoints`（PP）。
- **字体**：官网字体栈是 `-apple-system,...,PingFang SC,...,Microsoft YaHei,sans-serif`，服务器没有苹方，退到 `NotoSansCJK-*.ttc` 的 **index=2（SC）**——注意 index 0 是 JP，直接用会渲染成日文字形。
- 3 倍画布超采样再缩回，字不发虚；列表项按像素宽度截断加省略号。
- 图标字体：FontAwesome 7 的 `fa-solid-900` 官方只给 woff2，PIL 读不了，构建时用 fontTools 转成 `assets/fa-solid-900.ttf` 随插件带（863 KB），运行时按需读。
- 图存 `plugin_data/astrbot_plugin_paratranz/cache/*.png`，头像存 `cache/avatars/<md5>.img`。失败或对应开关（`progress_image` / `list_image`）关掉时回退文本。

## 文本排版约定

- QQ 官方 markdown 消息（菜单）按官方支持的那套写：`###` 标题、`-` 无序列表、`**加粗**`、`![图片 #宽 #高](url)`。**不用反引号代码块**，官方 markdown 不认内联代码，会原样显示。
- 协议端（NapCat 等）看到的是纯文本，所以 `_plain_menu()` 会把 markdown 标记剥掉：列表项变 `· `，去掉 `**` 和反引号。
- 其他查询结果为纯文本时统一用 `· ` 项目符号、编号列表或分节标题（`【查询】【管理】【写操作】【说明】`），不再堆全角空格。

## 缓存清理

`_sweep_cache()` 在每次出列表图前跑一次，一小时最多扫一次：`cache/` 下普通图 3 天、`cache/avatars/` 下头像 7 天后自动删，删了会打日志。头像下载有 7 天 TTL，命中缓存不重复拉；`media_logo.json` 记录的素材库引用按 logo 的 md5 校验，logo 不改就不重传。

## 素材

| 文件 | 用途 |
| --- | --- |
| `logo.png` | 512×512 正方形，AstrBot 插件列表图标（StarManager 认插件根目录的 `logo.png`） |
| `logo_wide.png` | 横向 wordmark，菜单图顶部用 |

## 第三方资源与许可

- `assets/fa-solid-900.ttf`：Font Awesome Free 7.3.1（https://fontawesome.com），字体按 SIL OFL 1.1 授权，许可证全文见 `assets/LICENSE-FontAwesome.txt`；图标部分按 CC BY 4.0 授权。
- `logo.png` / `logo_wide.png`：ParaTranz 官方标识，仅用于识别本插件所对接的服务。本插件非 ParaTranz 官方出品，未获得官方背书。

## 指令

子命令支持中文，懒得记英文就直接写 `/pt 项目`、`/pt 绑定`、`/pt 授权`。完整映射：
项目/进度、文件、术语、词条、成员、搜索、我/权限、绑定、解绑、绑定列表、授权、撤销、名单、改/修改/设置、确认、取消。

### 查询（所有人在允许的群里都能用）

| 指令 | 说明 | 需要 Token |
| --- | --- | --- |
| `/pt project [@项目ID]` | 项目信息 + 总进度（本地画进度卡片图，仿官网样式） | 否 |
| `/pt files [数量]` | 文件进度排行 | 否 |
| `/pt terms [关键词]` | 术语表查询 | 否 |
| `/pt members [数量]` | 成员贡献排行（默认 `member_limit`=20，最多 100） | 否 |
| `/pt tasks [数量]` | 任务进度（`GET /projects/{id}/tasks`，默认 `task_limit`=10，最多 20） | 否 |
| `/pt strings [文件ID] [状态] [数量]` | 词条原文/译文（需授权）。状态可写名字：`未翻译/已翻译/有疑问/已检查/已审核/锁定`，数量默认 `strings_limit`=20、上限 50 | 是 |
| `/pt search <关键词>` | 按名称搜索项目 | 否 |
| `/pt me` | 看自己的权限、绑定项目、Token 状态 | 否 |

**一个群绑了多个项目时**：

- `/pt 项目` 不带参数 → 直接出「多项目总览」图，每个项目一张卡（审校模式不同就按各自的模式画进度条），最多列 8 个
- `/pt 文件`、`/pt 成员`、`/pt 任务`、`/pt 词条` 这类必须指定项目的 → 回一排项目按钮（按钮上只写项目名，超过 12 字截断；ID 放在按钮数据里），点哪个就带 `@项目ID` 重发同一条指令；按钮发不出去就退成文字列表

**普通成员只能查本群绑定的项目**，跨群/未绑定的项目一律拒绝；**管理员不受限制**，可以直接 `/pt 项目 @20374` 查任意项目（`/pt search` 对管理员是全站搜）。一个群可以绑多个项目：

- 只绑了一个 → 省略参数直接用它；
- 绑了多个 → 必须写 `@项目ID` 指定，例如 `/pt files @20374 5`；
- 一个都没绑 → 直接报错，让管理员先 `/pt bind <项目ID>`。

`/pt search` 和 LLM 工具同样只在绑定范围内找。

### 管理（仅管理员）

| 指令 | 说明 |
| --- | --- |
| `/pt bind <项目ID> [群号]` | 给当前群（或指定群）加一个项目绑定，可绑多个 |
| `/pt unbind [项目ID] [群号]` | 解绑单个项目；不给项目 ID 就清空该群 |
| `/pt binds` | 查看所有绑定 |
| `/pt grant <QQ号> [viewer\|translator]` | 给指定人赋权，默认 `translator` |
| `/pt revoke <QQ号>` | 撤销赋权 |
| `/pt grants` | 查看授权名单（含谁授权的、什么时候） |

### 写操作

| 指令 | 说明 |
| --- | --- |
| `/pt set <词条ID> <译文>` | 改译文，触发二次确认（需 `enable_write` + `translator`） |
| `/pt ok <确认码>` | 执行待确认操作 |
| `/pt cancel` | 取消自己所有待确认操作 |

## 权限等级

| 等级 | 能干什么 | 怎么来 |
| --- | --- | --- |
| admin | 全部，含绑定与赋权 | AstrBot 管理员 / `admin_ids` |
| translator | 读词条 + 改译文 | `/pt grant <QQ>` |
| viewer | 只能读词条正文 | `/pt grant <QQ> viewer` |
| 未授权 | 只能看公开数据（项目、文件进度、术语、成员） | — |

`admin_only_write` 关掉的话，等于放弃这套等级，群里谁都能读写——不建议。

## 配置（插件管理面板里直接改）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `token` | 空 | ParaTranz API Token，面板里直接填。也可用环境变量 `PARATRANZ_TOKEN` 兜底 |
| `base_url` | `https://paratranz.cn` | 自建实例可改 |
| `use_card` | `true` | 是否尝试发 QQ JSON 卡片，失败自动回退纯文本 |
| `task_limit` | `10` | 任务进度默认显示几个（上限 20） |
| `list_limit` | `10` | 列表类默认条数（上限 30） |
| `records_per_page` | `10` | 提交记录每页条数（上限 20） |
| `diff_preview_max` | `12` | 差分图最多画几条（0=不画，上限 30） |
| `progress_image` | `true` | 项目进度是否画成图片（`render.py`，PIL 本地渲染，不依赖 t2i） |
| `list_image` | `true` | 文件排行 / 术语 / 成员 / 词条 / 搜索结果是否画成图片 |
| `enable_write` | `false` | 总开关，关闭时客户端拒绝一切非 GET |
| `admin_only_write` | `true` | 开启时按上面的权限等级把人分三六九等 |
| `admin_ids` | `[]` | 额外管理员 QQ 号列表 |
| `allowed_groups` | `[]` | 群白名单，空表示所有群可用，私聊不受限 |
| `confirm_ttl` | `600` | 确认码有效期（秒），一次性；确认必须由有写权限的人完成 |
| `min_interval` / `max_concurrency` | `0.2` / `1` | 限流保护 |

## 提交 / 确认 / 撤回

改词条不再是「谁发谁写」，而是走一条提交链：

1. **发起**：任何人在群里都能发起（`/pt 修改`、两个 propose 工具）。插件会先按词条 ID 把旧译文抓下来，生成一条 **8 位哈希 ID** 的待确认记录，按项目落盘到 `submissions/<项目ID>.json`。
2. **审核确认**：只有**有写权限的人**（translator / 管理员）能点确认按钮或发 `/pt 确认 <码>`，发起人自己没权限就点不动。确认后才真正调 `PUT /projects/{id}/strings/{sid}`。
3. **记录**：`/pt 记录` 列最近提交和状态（待确认/已写入/已撤回/已取消/已过期），`/pt 记录 <哈希ID>` 看明细（谁发起、谁确认、每条原值→新值）。
4. **记录查询**：`/pt 记录 [@项目ID] [页码]` 只看**本群绑定的项目**，多项目时先弹项目按钮选一个，再出图（每页 `records_per_page`=10 条，时间 / 条数 / 发起人→确认人 / 哈希），配键盘：每条哈希一颗按钮点开详情图，末行是「上一页 / 下一页」（首页没有上一页、尾页没有下一页）。`/pt 记录 <页码>` 直接翻页。
5. **撤回**：`/pt 撤回 <哈希ID>`（仅管理员）把那次提交的旧译文全部写回去，状态记 `reverted`。写入成功后管理员会收到一条带「撤回这次提交」按钮的消息，点一下就是撤回。

留存策略：**待确认的暂存 3 天**（`PENDING_TTL`，随缓存清理一起扫掉），**已写入的记录永久保留**，方便随时回滚。确认码有效期仍是 `confirm_ttl`（600 秒）。

## 校对模式适配

项目字段 `reviewMode` 决定显示哪几段进度（进度图、任务图都跟着变）：

| reviewMode | 含义 | 显示的进度 |
| --- | --- | --- |
| 0 | 无需校对 | 只显示「已翻译条数」（蓝） |
| 1 | 一次校对 | 已翻译（蓝）+ 已审核（绿，此时 checked == reviewed，取 reviewed、为 0 时退 checked） |
| 2 | 二次校对 | 已翻译（蓝）+ 已检查（青）+ 已审核（绿） |

配色直接照官网 `stage-badge` 那套：stage 1 已翻译 `#007bff`、stage 3 已检查 `#20c997`（青）、stage 5 已审核 `#28a745`（绿）、stage 2 有疑问 `#ffc107`、stage 0 未翻译 `#ccc`。之前把检查和审核的绿/青搞反了，`v0.15.4` 已按 CSS 实锤纠正。

未知或缺失时按 2 处理。

## 任务进度

`/pt 任务` 拉 `GET /projects/{id}/tasks`，按任务聚合 `taskFiles`（文件数 / 词条数 / 字数 / 已翻译·已检查·已审核百分比），渲染成官网任务页那种卡片：⭐ + 标题 + 创建日期 + 「进行中」红标 + 三个百分比徽标 + 底部统计。

## 词条卡片（通用）

`render_strings_card()` 是全插件唯一的词条展示组件，`/pt 词条`、`paratranz_render_strings`、`paratranz_disputed` 都走它：

- 一条一张白卡，头部是**状态徽标**（配色照官网 `.stage-badge[data-stage]`：0 未翻译 `#ccc`、1 已翻译 `#007bff`、2 有疑问 `#ffc107`、3 已检查 `#20c997`、5 已审核 `#28a745`、9 锁定 `#212529`），后面跟 `#ID` 和文件名
- 卡内两行：**原文灰色 `#808892`**（表示只读不改），**译文正文色**（可改）
- 原文/译文都按像素宽度折行，最多各 3 行

## 渲染性能

- **不做超采样**（`SS=1`）：PIL 是按像素直接画字的，1:1 就是最锐利的；先画大图再缩反而糊，还多花一倍时间。现在 `_save()` 只在需要缩放时才 resize，PNG 用 `compress_level=1`（发完就删，不追求体积）。
- 字符宽度按 (字体, 字) 缓存，折行不再每格调一次 `textlength`。
- 发起提交时抓旧译文改成 `bulk_get_strings()`：并发 6，不再一条条 0.2 秒串行，48 条从 ~10 秒降到 1–2 秒。
- 本机实测（720 宽）：任务 10 条约 460ms、差分 12 条约 420ms、进度图约 300ms、成员榜约 290ms、文件/记录约 200ms。

## 图片清理

所有临时图（差分图、提交记录图、任务图、成员榜）发完 **60 秒自动删**（`TEMP_IMAGE_TTL`），发送失败也会走清理；`cache/` 里的列表图 3 天、头像 7 天由 `_sweep_cache()` 定期扫掉；QQ 素材库引用记录 30 天没用到就从 `media_logo.json` 里剔除（服务端素材本身 90 天滑动过期）。

## 确认按钮

`/pt 修改`、`paratranz_propose_translation`、`paratranz_propose_batch` 生成确认码后，会先尝试发一条 **markdown + 按钮** 的消息：`确认写入` / `取消` 两颗，点了等于发 `/pt 确认 <码>` / `/pt 取消`。发不出去（协议端、官方接口报错）就退成纯文本 + 确认码。

批量提交（`paratranz_propose_batch`）还会把**改动汇报图**直接塞进确认消息，版式照官网「历史记录」页：灰底、一条一张白卡、卡片间隔当分隔，卡片头是「✎ 谁 编辑了词条 ID」，卡内浅灰圆角块里是行内红绿差分（`difflib` 字符级，删除片段红底+删除线、新增片段绿底，跟官网历史记录一个路数），最多 `diff_preview_max`（默认 12）条，超出的在脚注写总数。图存在 `cache/diff_*.png`，`TEMP_IMAGE_TTL`（60 秒）后自动删除，不占盘。

注意群聊限制：`action.enter=true` 官方**只在单聊**生效，群里点了按钮只会把指令填进输入框，还得点一次发送；要「点一下就写」只能上回调按钮（`type=1`）+ 公网回调地址，内网做不了。

## LLM 工具（插件内注册，不是 MCP）

插件启动时会往 AstrBot 的 function-calling 里注册这几个工具，模型在群里被问到相关内容时可以直接查：

| 工具 | 用途 | 权限 |
| --- | --- | --- |
| `paratranz_project_progress` | 项目总进度 | 公开 |
| `paratranz_search_terms` | 术语表查词 | 公开 |
| `paratranz_get_strings` | 批量读词条原文/译文（默认 `strings_limit` 条，可按文件/状态过滤） | Token + viewer 以上 |
| `paratranz_get_string` | 按词条 ID 读单条（`GET /projects/{id}/strings/{sid}`），改完复查用 | Token + viewer 以上 |
| `paratranz_render_strings` | 通用词条卡片图：可指定状态/文件/条数，一次看多条 | Token + viewer 以上 |
| `paratranz_disputed` | 列「有疑问」(stage 2) 的词条，方便跟人讨论怎么改，可顺带发一张清单图 | Token + viewer 以上 |
| `paratranz_string_context` | 看某条词条在同一文件里的上下文（前后各 N 条），讨论剧情/人称用 | Token + viewer 以上 |
| `paratranz_propose_translation` | 发起单条修改（谁都能发起），生成带哈希ID的待确认记录 | Token（确认需有写权限的人） |
| `paratranz_propose_batch` | 批量发起修改（`edits` 每行 `词条ID|新译文`，或 JSON 数组），上限 100 条，生成一条待确认记录 + 红绿差分图 | Token（确认需有写权限的人） |

`propose_translation` 只生成确认码，不会直接落库，必须有人回 `/pt 确认 <码>`（手动输入，模型没法自己确认），
这就是给「AI 改稿」留的安全阀。

## 数据落盘

绑定和授权存在 `<AstrBot 数据目录>/plugin_data/astrbot_plugin_paratranz/state.json`：

```json
{
  "binds": {"群号": [12352, 20374]},
  "grants": {"QQ号": {"role": "translator", "by": "管理员QQ", "at": "2026-10-23T17:20:00"}},
  "projects": {"20374": {"name": "女皇之刃＆女皇之门", "at": "2026-10-23T19:56:00"}}
}
```

提交记录不在这里，按项目分开存：`submissions/<项目ID>.json`（hash → 记录）。这样各项目的记录互不干扰，卸载/清项目直接删对应文件即可。**不兼容旧格式**：早期塞在 `state.json` 里 `submissions` 那一坨不会再读，也不会迁移。

`binds` 的每个群是项目 ID 列表；老版本存的单个整数会在读取时自动升级成列表。`projects` 只是项目名缓存，菜单和指令回显用名字而不是裸 ID，缓存里没有时会自动拉一次并写回。

原子写入（临时文件 + `os.replace`），删掉文件即清空全部绑定和授权。

**Token 只在插件配置里**，代码只把它交给 `ParaTranzClient`，不落日志、不回显、不通过 LLM 工具或聊天暴露。

## 安全设计

1. 群白名单 —— 不在 `allowed_groups` 里的群直接不理。
2. 管理员校验 —— 管理指令、写指令默认只认管理员；普通成员要靠 `grant` 赋权。
3. 二次确认 —— 一次性确认码（默认 600 秒），用过即焚；发起谁都能发，但确认必须由有写权限的人完成，发起人自己没权限也点不动。

Token 是账号级 apiKey，官方没有只读 scope。若持有者同时是项目管理员，这个 Token 理论上能删项目。方案：单独开个小号只给翻译权限，Token 不外泄，定期 `POST /artifacts` 导出留底。
