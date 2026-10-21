# 美术资源来源与许可（ATTRIBUTION）

本目录中的角色精灵表**不是原创素材**，按上游许可使用，义务如下。

## 使用到的文件

| 文件 | 来源 | 许可 |
|---|---|---|
| `shenshen-atlas.png` | [schrieeeffer/shenshen-codex-pet](https://github.com/schrieeeffer/shenshen-codex-pet) → `assets/spritesheet-v2.png` | CC BY-NC-SA 4.0 |
| `shenshen.manifest.json` | 同上 → `pet/pet.manifest.json` | 同上 |
| `shenshen-states.png` | 本项目生成（上表的预览拼图） | 同上（衍生） |

- 下载校验：SHA256 `0dd1c39f8333f73da19389b4cda3f62c5658468e93caa5401add19bca33b5f30`，与 manifest 声明**完全一致**；
- 尺寸 1536×2288、RGBA、8 列 × 11 行、单格 192×208。

## 许可：CC BY-NC-SA 4.0

[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-nc-sa/4.0/)

必须遵守：

1. **署名**：保留作者与来源链接、许可链接，并说明修改内容；
2. **非商业**：不得用于主要指向商业利益或金钱补偿的用途；
3. **相同方式共享**：对本素材的改编（含缩放、裁剪、派生图标）须继续以 CC BY-NC-SA 4.0 提供；
4. 不得附加额外法律或技术限制。

> 本项目**代码**（`collector.py` / `overlay.py` / `server.py` / `dashboard.html`）为本地自用工具，与素材许可相互独立；一旦涉及分发或商用，必须单独审视素材许可。

## 署名文本（转载/分发时请原样保留）

> 深深桌宠，基于**上善无形**的鲸鱼娘形象及 dsh-pet 社区项目谱系改编；本版本由 Shenshen Pet contributors 制作；视觉素材采用 CC BY-NC-SA 4.0，非官方、非商业使用。

- 原始鲸鱼娘形象：上善无形 — [公开来源](https://www.bilibili.com/opus/1231977657712771073)，CC BY-NC-SA 4.0
- 桌宠项目 / 动作参考：[PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)、[MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)、[ianlike-ui/dsh-pet-standalone](https://github.com/ianlike-ui/dsh-pet-standalone)
- 上游项目整理：`schrieeeffer/shenshen-codex-pet`（代码 MIT，视觉素材 CC BY-NC-SA 4.0）

## 本项目所做的修改

1. 原文件原样保存为 `shenshen-atlas.png`（未改动像素）；
2. 运行时按 manifest 的 `cellWidth/cellHeight` 裁切单帧，并缩放到 96–160 px 显示——属于**改编**，因此派生显示同样受 CC BY-NC-SA 4.0 约束；
3. 生成 `shenshen-states.png` 拼图用于文档预览；
4. `whale-*.png` 为 AI 生成的**自有**扁平图标（非上游素材），仅作无精灵表时的降级占位。

## 未使用的素材

- `isKage/desk-llm-pet`：其动画素材（WebM）**未随仓库分发**（作者注明体积过大、源自 dsh-pet），故本项目不从该仓库取图，仅借鉴其交互与插件架构。其代码为 MIT。
