# auto-video-cut

把「一堆拍好的素材 + 一份文案」自动排成**剪映工程**：画面切分、字幕、配音、时长全自动，
你在剪映里微调后导出。

这是一个**可搬移的只读工具**，不绑定任何路径、不写自己的目录。用法是：

> 在**素材目录**里打开 AI，让它把 `auto-video-cut` 当作制作工具调用。

## 快速开始

```bash
# 1. 在放素材的目录里，看一眼素材清单
python ../auto-video-cut/scan.py .

# 2. 写 script.md（格式见下），然后试运行
python ../auto-video-cut/build.py . --dry-run

# 3. 完全退出剪映，正式生成
python ../auto-video-cut/build.py .

# 4. 结构自检
python ../auto-video-cut/validate.py .
```

## 目录

```
auto-video-cut/
├─ AGENTS.md            ← 给 AI 的完整操作手册（AI 只需要读这个）
├─ README.md            ← 本文件
├─ build.py             主程序：script.md → 剪映草稿
├─ scan.py              扫描素材，输出清单
├─ doctor.py            环境自检
├─ install.py           安装配音依赖到 .vendor/（绿色，不碰系统 Python）
├─ voices.py            列出可用音色
├─ validate.py          草稿结构自检
├─ jylib.py             剪映明文草稿生成库
├─ prep.py              素材规整（统一画幅/帧率，图片转静帧推拉）
├─ tts.py               配音合成（edge-tts）
├─ assets/              从剪映草稿提取的字段模板（别删，40KB 纯 JSON）
├─ templates/script.md  脚本模板，复制到素材目录改
├─ config.example.json  仅当自动探测不到剪映目录时才需要
├─ requirements.txt     配音依赖清单
└─ .vendor/             edge-tts 及其依赖（不入库，按 requirements.txt 重建）
```

## 工作目录长什么样

```
你的素材目录/
├─ script.md          ← 你写（或 AI 写）
├─ 片段1.mp4           ← 素材，随便放，支持子目录
├─ images/封面.png
└─ .jybuild/          ← 工具生成的缓存与分镜表，可整个删掉
```

## script.md

```markdown
---
标题: 第四期_开场
尺寸: 1920x1080
配音: edge
音色: zh-CN-XiaoyiNeural
语速: -10%
留白: 0.25
---

# 1
文案：这句话是口播，也会成为字幕。
素材：片段1.mp4
起点：2

# 2
文案：换一段素材。写两句也行，会自动切成两条字幕。
素材：片段2.mp4

# 3
文案：这一屏放图，自动加缓慢推拉。
图片：images/封面.png
```

分镜字段：`文案` `素材` `图片` `起点` `时长` `不字幕` `字号` `位置Y`。
全局配置项和写作规范见 `AGENTS.md` 第 4 节（那里写得更细，AI 和人看的都是同一套规则）。

## 配音

`配音: edge` 时用 edge-tts **逐句合成**，每句的真实音频时长决定字幕和画面时长，
所以字幕边界与语音边界严格重合，不会漂移。

常用音色：`zh-CN-XiaoyiNeural`（晓伊，少女音）、`zh-CN-XiaoxiaoNeural`（晓晓）、
`zh-CN-YunxiNeural`（云希，男声）、`zh-CN-YunjianNeural`（云健，解说腔）、
`zh-CN-YunyangNeural`（云扬，播报腔）。完整清单跑 `python ../auto-video-cut/voices.py`。

`配音: 剪映` 则不生成音频，按字数估时长排布，音频留给你在剪映里点「文本朗读」。

## 依赖

| 依赖 | 情况 |
|---|---|
| Python ≥ 3.8 | 用运行脚本的那个解释器，不要求它在系统 PATH 里 |
| **ffmpeg / ffprobe** | 需要已安装并在 PATH 里 |
| **edge-tts** | 绿色安装在 `.vendor/`（不入库），删掉即回退「剪映朗读」模式 |

不写系统 Python、不改注册表、不装服务。

从仓库克隆后重建配音依赖：

```bash
python install.py
```

脚本用**当前解释器**（`sys.executable`）的 pip 装进 `.vendor/`，只碰这个目录。
常用参数：

| 参数 | 作用 |
|---|---|
| `--check` | 只检查，不安装（退出码 0=已就绪） |
| `--force` | 删掉 `.vendor` 整份重装 |
| `--index-url tuna` | 换国内镜像，也认 `aliyun`/`ustc` 或完整 URL |
| `--pip-arg X` | 额外透传给 pip，可重复 |

装完用 `python doctor.py` 确认。换机器/换 Python 版本必须重装一次——
`.vendor` 里是平台绑定的编译扩展（`cp313-win_amd64.pyd`），不能跨版本复制。
`install.py` 会把当时用的解释器记在 `.vendor/.interpreter`，发现不一致自动重装。

## 作为 git 仓库

适合。纯文本部分只有 **138KB**，`git add` 之后仓库约 **43KB / 22 个文件**。

| 内容 | 大小 | 入库 | 理由 |
|---|---|---|---|
| 代码 + 文档 + `assets/` 字段模板 | 138KB | 是 | 纯文本，`assets/` 是运行必需的剪映字段结构 |
| `.vendor/`（edge-tts 等 9 个包） | 4.5MB | **否** | 含 9 个 `cp313-win_amd64.pyd`，平台+Python 版本绑定 |
| `config.json` | 千字节 | **否** | 本机剪映路径、字体路径，每台机器不同 |
| `.jybuild/` | 与素材等大 | **否** | 转码和配音缓存，随时可重建 |

`.gitignore` 已经配好。克隆后只差一步：

```bash
python install.py
```

没装也能跑：`doctor.py` 会报 WARN（不是错误），配音自动回退到「剪映朗读」模式，
按字数估算时长、音频留空，你在剪映里点一次「文本朗读」即可。这条路径已实测通过。

## 关键设计

**为什么能直接写剪映草稿**：剪映 11 把 `draft_content.json` 加密了，但同目录的
`Timelines/<id>/template.json` 是明文，暴露了 version 360000 的完整字段结构；
草稿索引 `root_meta_info.json` 也是明文。所以按同样字段写一份明文草稿并注册索引，
剪映就能正常打开（已在 11.3 实测）。

**为什么要先转码**：`prep.py` 把所有素材统一成目标画布尺寸和恒定帧率，
草稿里所有片段的缩放恒为 1，不会因为源素材画幅不一致而错位。

**为什么不写自己的目录**：配音和转码缓存全部落在工作目录的 `.jybuild/`，
所以本目录可以整体搬到任何位置、被多个项目同时引用，甚至设成只读。

## 已知限制

- 生成前**必须完全退出剪映**（含托盘 `JianyingProTray.exe`），否则草稿目录被占用。
- 草稿一旦在剪映里保存就会被重新加密，不能用本工具再读回；改结构要重跑 `build.py`。
- 不做转场、入场动画、花字、滤镜、BGM——这些在剪映里手工加。
- 不渲染 mp4，只产出工程。
