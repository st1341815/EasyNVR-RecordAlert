# EasyNVR 录像监控（erm）· 飞牛 fnOS fpk 应用

监控 EasyNVR 录像目录：任一通道超过阈值没有新增录像就推送告警，每天固定时间推送前一天各通道录像数量统计。

**目录结构按 EasyNVR 的真实布局识别**（v1.0.5 起）：

```text
<录像根目录>/<摄像头随机ID>/profile_<通道号>/<YYYYMMDD>/20261029000315-62000.mp4
例：/vol3/1000/NVR/easynvr_rec/O27fqV6dYvs3T/profile_1/20261029/20261029000315-62000.mp4
```
支持钉钉（含「加签」）、飞书、企业微信；多通道与 `stream_*` 自动发现；配置改动即时生效。

本工程是「文档评审 → 重写 → 真机实测」的产物，**不是**原文档那份代码的补丁版：
原文档在平台接入层（manifest / wizard / privilege / 路径 / 状态返回码 / 依赖安装）和核心逻辑
（阈值类型、通道列表、日报调度）都有硬伤，逐条修掉后重写而成。评审清单见
`../EasyNVR-fpk文档评审_2026-10-29.md`，实测记录见 `docs/实测报告.md`。

> **下载安装**：到 [Releases](https://github.com/st1341815/EasyNVR-RecordAlert/releases) 下载
> `EasyNVR-RecordAlert-<版本>.fpk` → 飞牛「应用中心 → 手动安装」→ 填录像目录 → 到「设置 → 授权目录」授权该目录 → 重启应用。
> 版本变更见 [CHANGELOG.md](CHANGELOG.md)。


---

## 1. 目录结构

```text
erm-fpk/
├── build.sh               一键构建（权限校准 + JSON/manifest 校验 + py_compile + fnpack）
├── gen_icons.py           生成 64/256 图标（纯标准库，无 Pillow）
├── selftest.py            55 项断言的自检（不联网，除本地 mock）
├── mock_webhook.py        本地 mock 钉钉/企微/飞书（验收推送链路用）
├── preview_proxy.py       不登录 fnOS 也能看页面（只读转发，仅供调试）
├── docs/                  截图与实测报告
└── erm/                   ← 打包源，目录名 = manifest 的 appname
    ├── manifest           平铺 key = value（飞牛不认 [section]）
    ├── ICON.PNG           64×64
    ├── ICON_256.PNG       256×256
    ├── config/
    │   ├── privilege      {"defaults":{"run-as":"package"}}
    │   └── resource       {}
    ├── wizard/
    │   ├── install        JSON 数组（无扩展名！飞牛格式，不是 DSM 的 config.json）
    │   └── config         设置页（字段语义：留空 = 不修改）
    ├── cmd/               main / install_* / upgrade_* / config_* / uninstall_*
    └── app/               ← 内容直接铺到 $TRIM_APPDEST（没有 app/ 这一层）
        ├── server.py      网关（Unix Socket）+ 监控线程（同一进程）
        ├── monitor.py     扫描 / 判定 / 日报 / 调度
        ├── notify.py      推送（urllib，钉钉加签，校验 HTTP+业务码）
        ├── config.py      强类型配置与原子写
        ├── scripts/apply_wizard.py   向导值 → config.json
        ├── www/index.html 状态页 + 配置表单
        └── ui/config      微应用入口（gatewayPrefix/gatewaySocket）
```

## 2. 盘面约定（改代码别破）

| 位置 | 内容 | 说明 |
| --- | --- | --- |
| `$TRIM_APPDEST`（`/volX/@appcenter/erm`） | 程序本体 | **升级会整体替换**，不放任何用户数据 |
| `$TRIM_PKGVAR`（`/volX/@appdata/erm`） | `config.json`(0600) / `state.json`(0600) / `logs/` / `*.pid` | 升级与卸载都保留 |
| `/volX/@appdata/erm/logs/monitor.log` | 运行日志，5MB×3 轮转 | 原来是 `nohup >>` 永久追加 |

## 3. 构建

```bash
bash build.sh                 # 产出 dist/erm.fpk
PYTHONPATH= python3 selftest.py   # 55 项断言（先跑这个再打包）
bash gen_icons.py             # 只有换图标时才需要
```

`build.sh` 会卡住 4 类常见错误：JSON 非法、manifest 出现 `[section]`、缺必填字段、Python 语法错。
打包前会清 `__pycache__`（否则会被打进 app.tgz，包体虚胖）。

### 4.1 发版规范（GitHub Release）

| 项 | 规则 | 示例 |
| --- | --- | --- |
| git tag | `v<版本>` | `v1.0.6` |
| Release 标题 | `EasyNVR-RecordAlert v<版本>`（**项目名 + 版本号**，主题只写正文） | `EasyNVR-RecordAlert v1.0.6` |
| Release 附件 | `EasyNVR-RecordAlert-<版本>.fpk`（项目名 + 版本号） | `EasyNVR-RecordAlert-1.0.6.fpk` |
| Release 正文首行 | `## v<版本> · 主题` | `## v1.0.6 · 识别事件抓拍目录` |
| 仓库内构建产物 | 固定 `dist/erm.fpk`（不带版本号，`build.sh` 与安装命令依赖这个路径） | — |

发版五步：

1. bump 三处版本号：`erm/manifest` 的 `version`、`erm/app/server.py` 的 `VERSION`、`CHANGELOG.md` 新增条目；
2. `bash build.sh`（会打印建议的标题与附件名）；
3. `git tag -a v<版本> -m "..." && git push origin main v<版本>`；
4. 建 Release，标题/附件名按上表；**附件改名只在上传时指定**，不动 `dist/erm.fpk`；
5. 换附件名要"先传新名 → 回读比对 sha256 → 再删旧名"（GitHub 不支持重命名附件，中途失败会丢包）。

## 4. 安装（本机 / 任意 fnOS）

```bash
# 1) 装（向导值可用 -e 传，也可装完在应用里填）
sudo appcenter-cli install-fpk ./dist/erm.fpk -e ./install.env

# install.env 示例
# wizard_record_root=/vol1/1000/easynvr/record
# wizard_push_dingtalk=启用
# wizard_dingtalk_webhook=https://oapi.dingtalk.com/robot/send?access_token=xxx
# wizard_scan_interval=60
# wizard_alert_threshold_minutes=60
# wizard_daily_report_time=08:00

# 2) 授权录像目录（run-as=package，必须做）
trim-cli app config set-authorization-path erm /vol1/1000/easynvr --perm 6 --yes

# 3) 重启使授权生效
sudo appcenter-cli stop erm && sudo appcenter-cli start erm
```

装完在「应用中心 → EasyNVR 录像监控 → 打开」就是状态页。

## 5. 验证

```bash
sudo /var/apps/erm/cmd/main status; echo "rc=$?"     # rc=0 才算运行中（3 = 停止）
sudo ls -l /vol1/@appcenter/erm/erm.sock            # 660 erm:erm
sudo tail -20 /vol1/@appdata/erm/logs/monitor.log
sudo curl -s --unix-socket /vol1/@appcenter/erm/erm.sock \
     'http://localhost/app/erm/api?action=doctor'    # 一键自检（页面里也有按钮）
```

页面上的「测试推送」是最快的链路验收：它会真的发一条消息，并把每个渠道的
`HTTP 状态 + 平台错误码` 原文显示出来（钉钉常见 `310000 关键词不匹配`，说明
该填加签密钥或改关键词）。

## 5.1 通道识别（EasyNVR 三层目录）

| 配置项 | 取值 | 说明 |
| --- | --- | --- |
| `channel_layout` | `camera`（默认）/ `profile` / `flat` / `auto` | **camera** = 一个摄像头一个通道（聚合它所有 `profile_N`）—— 子码流不录制时不会误报；**profile** = 每个 `profile_N` 各算一个（能发现"主码流挂了但子码流还在"）；**flat** = 老式一层 `stream_*` |
| `monitor_channels` | `O27fqV6dYvs3T`、`O27fqV6dYvs3T/profile_1`、`stream_*` | 留空 = 自动发现；填了就只监控这些 |
| `channel_aliases` | `O27fqV6dYvs3T=大门` | 只影响显示与告警文案（页面上是多行文本框） |

几个实现细节：

- **扫描按日期目录逐天打开**，不递归整棵树：1.8 万个 mp4（60 天 × 300）一次扫描约 **0.06 秒**；只回溯最近 120 天（`LOOKBACK_DIRS`）。
- **缩略图 / 快照这类按日期分层的目录不会被当成通道**：只认带 `profile_N` 子目录的摄像头 ID 目录（`flat` 模式除外）。
- 全部 profile 都没文件 → 「无录像文件」告警；某个 profile 空了但别的还在 → 摄像头聚合口径视为正常（这就是默认用 camera 的原因）。

## 5.2 加入 FnDepot 外部源

仓库根有 `fnpack.json`（FnDepot V2 规范），两种加法：

| 方式 | 在 FnDepot 里填 |
| --- | --- |
| **GitHub 仓库模式**（推荐，读的是实时内容） | `https://github.com/st1341815/EasyNVR-RecordAlert` |
| JSON 直链（国内加速） | `https://cdn.jsdelivr.net/gh/st1341815/EasyNVR-RecordAlert@main/fnpack.json` |

> jsDelivr 对**已存在的文件**有约 12 小时缓存，改完 `fnpack.json` 后直链可能仍吐旧内容；
> 要看最新版本请用仓库模式（走 GitHub API，无缓存），或等缓存过期。

改 `fnpack.json` 后先跑自检（会真的去下载图标/预览/安装包并比对 sha256）：

```bash
PYTHONPATH= python3 check_fnpack.py     # 期望 FNPCK_OK（31 项）
```

发新版时在 `releases` 里**新增**版本节点（旧节点不要删、不要改，FnDepot 视为不可变）。

## 6. 几个设计决定（都是踩坑换来的）

1. **单进程 = 网关 + 监控线程**：`cmd/main` 只管一个 pid，避免"网关停了监控还在跑"；
   两者共享同一份 `STATE`（否则 API 触发的扫描与后台线程各持一份状态，会重复发恢复通知 —— v1.0.1 修的就是这个）。
2. **推送只用标准库 urllib**：飞牛系统 python3.11 是 PEP 668 `externally-managed`，
   `pip install requests` 必然失败；本机恰好自带 requests 属于运气，不是契约。依赖归零最省事。
3. **判定基准 = `now - 最新文件 mtime`**，不拿历史 state 当基准：EasyNVR 会轮转清理旧文件，
   用历史值会误报；目录被清空会漏报且状态卡死。
4. **`latest == 0` 也告警**，分「目录不存在 / 无读权限 / 目录空」三种文案：否则新装通道、
   目录名填错、录像根本没开——全都静默不报。
5. **日报「到点即发 + 日期落盘」**：`cur.minute == 目标分钟` 配 `sleep(300)` 永远命中不了；
   落盘去重还能保证重启/睡眠错过 08:00 也能补发当天一次、且不会重复发。
6. **设置页字段「留空 = 不修改」**：飞牛设置页的 `initValue` 是静态的，拿不到当前配置；
   若把空值当"清空"，用户只在设置页点一下保存就会把录像目录和 webhook 抹掉。
7. **配置热生效**：页面保存直接改内存配置 + 落盘，监控线程下一轮就用新值，不重启。

## 7. 已知限制

- **外接存储（U 盘 / SD 卡）**：`/vol00` 默认只有 root 可见，且 FAT 打不上 ACL，
  「授权目录」对它无效。必须到飞牛「文件管理 → 设置 → 外接存储权限」选「所有账户可见」。
- **HLS 模式**（`.m3u8` + 持续新增的 `.ts` 分片）：分片 mtime 一直很新，只能靠"最新分片是否停止增长"判断，
  阈值要调小（10 分钟左右），日报的"文件数"也不能当"录像条数"看。
- **摄像头名字**：EasyNVR 把通道名存在自己的数据库里，目录里只有随机 ID，应用读不到。
  想看到「大门」而不是 `O27fqV6dYvs3T`，就在页面上填「摄像头别名」。
- **升级路径**：`appcenter-cli install-fpk` 对已安装应用直接返回「已安装」，不走升级；
  命令行下要升级只能 `uninstall` + `install`（会清空授权目录，需重新授权）。应用中心 UI 的升级按钮未验证。
- **重装数据**：`$TRIM_PKGVAR` 保留（config.json 沿用），但 `state.json` 实测会消失 →
  重装后首轮按"首次发现"处理，不会补发历史告警。
- 桌面图标：本机 `autoCreateDesktopIcon` 未开启，第三方应用只在「应用中心」里出现（与其他第三方应用一致），
  需要桌面图标可手动把本应用的 `ui` 配上，或在系统设置里开启自动创建。

## 8. 自检覆盖

`selftest.py` 55 项断言，重点回归原文档的每个坑：

| 断言 | 对应问题 |
| --- | --- |
| `"60" * 60 == 2700` | 阈值字符串相乘 → TypeError → 永不告警 |
| `monitor_channels != list("stream_1,stream_2")` | 逗号字符串被拆成单字符 |
| 300s 采样一天命中 08:00 次数 = 0 | 旧日报调度的复现 |
| 清掉旧文件后立刻恢复正常 | 判定基准用历史值会误报 |
| 空目录/不存在目录 → 状态为告警 | `latest==0` 静默跳过 |
| HTTP 200 + errcode=310000 → 判失败 | "已推送"其实没发出去 |
| 加签 sign 与官方算法逐位一致 | 钉钉「加签」 |
| config 0600 / 原子写无残留 | webhook 泄露与半截 JSON |
