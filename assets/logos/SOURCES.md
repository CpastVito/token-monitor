# 品牌图标来源

`assets/logos/` 下的图用于余额卡与服务商列表的品牌标识。每个品牌一套：
`<name>.png`（64px 母版）+ `@32 / @24 / @20 / @16` 四个尺寸，全部为带透明通道的方形 PNG。

## 一、来源

| 品牌 | 取图地址 |
|---|---|
| deepseek / kimi / moonshot / openai / openrouter / newapi / custom | 原有资源（2026-10 制作，本次仅重新裁切居中） |
| linoroute | `linoroute.com/logo.png`（og:image `lino-mark-v2.png`，1024px） |
| lmu | `lmuai.ai/favicon.ico`（取 64px 帧；站点 og:image 已 404） |
| zhipu | `favicon.im/bigmodel.cn` |
| qwen | `favicon.im/dashscope.aliyun.com` |
| anthropic | `www.anthropic.com/favicon.ico`（48px 帧） |
| gemini | `favicon.im/gemini.google.com` |
| mistral | `favicon.im/mistral.ai` |
| groq | `favicon.im/groq.com` |
| together | `favicon.im/together.ai` |
| siliconflow | `favicon.im/siliconflow.cn` |
| ollama | `favicon.im/ollama.com` |
| xai | `favicon.im/x.ai` |
| yi | `favicon.im/platform.lingyiwanwu.com` |
| baichuan | `favicon.im/baichuan-ai.com` |
| minimax | `favicon.im/minimaxi.com` |
| volcengine | `favicon.im/volcengine.com` |
| stepfun | `favicon.im/platform.stepfun.com` |
| perplexity | `favicon.im/perplexity.ai` |
| fireworks | `favicon.im/fireworks.ai` |
| deepinfra | `favicon.im/deepinfra.com` |
| modelscope | `favicon.im/modelscope.cn` |
| cohere | `favicon.im/cohere.com` |
| novita | `favicon.im/novita.ai` |
| github | `github.com/apple-touch-icon.png` |
| vercel | `favicon.im/vercel.com` |
| aliyun | `favicon.im/aliyun.com` |
| tencent | `favicon.im/cloud.tencent.com` |

`favicon.im/<domain>?larger=true` 会回源抓取该站点的 favicon / apple-touch-icon；
Google 的 `s2/favicons` 与 DuckDuckGo 的 `ip3` 在本机网络下不可达，故未采用。

## 二、处理规则

所有源图走同一条流水线（`PIL`）：

1. 转 RGBA；
2. 按 alpha > 8 裁掉透明边；
3. 等比缩放到 **56×56** 以内（64 画布留 12.5% 呼吸位），小图最多放大 2× 以免糊；
4. 居中贴到 64×64 透明画布；
5. LANCZOS 下降采样出 32 / 24 / 20 / 16。

这样不同来源（有的带大片透明留白、有的是满幅色块）在 16–26px 下视觉尺寸一致，
也不会因为原图留白不均而"看起来没居中"。

## 三、品牌识别

`brands.json` 是唯一的事实来源，网页端与桌宠都读它：

```json
{ "brands": [ { "name": "zhipu", "label": "智谱 GLM",
               "keys": ["zhipu", "bigmodel", "glm", "智谱"] } ] }
```

匹配顺序：`label` / `id` / `base_url` 里的小写子串 → 命中的品牌 → 否则退回模板名
（deepseek / moonshot / openai / openrouter / newapi）→ 都没有则用 `custom` 字母徽章。

新增品牌只需：把 `<name>.png` 与 `@16/@20/@24/@32` 放进本目录，并在 `brands.json`
里加一条 `keys`，两端同时生效，无需改代码。

> 这些图标是各厂商的商标，仅用于在本工具里标识对应的服务商，不构成任何授权或背书。
