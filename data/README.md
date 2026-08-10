# Data

本目录只保留数据布局说明，不包含任何图片、标注或用户数据。实际数据集应放在
仓库外部，并通过 WebUI 选择绝对路径。

## 推荐布局

```text
dataset/
  001_角色名/
    image-001.png
    image-001.txt
    image-001.json
  002_另一个名称/
    image-002.jpg
```

TXT 和 JSON 都是与图片同名的可选 sidecar。同一数据集可以混合：

- 只有图片；
- 图片加 TXT；
- 图片加标准 JSON；
- 图片加原始 E621 分组 JSON；
- 图片同时带 TXT 和 JSON。

程序按样本分别判断已有标注，不要求整个文件夹使用同一种状态。具体行为由
Caption 的 `TXT 输入模式`、增量/重建模式和各模块覆盖开关共同决定。

## TXT 模式

- `Tag`：非空 TXT 作为标签输入并跳过该样本的 Tagger。缺失或空 TXT 默认由
  Tagger 补全。
- `NL`：非空 TXT 写入标准 JSON 的 `nl`，Tagger 仍负责生成分类标签。TXT
  必须是 UTF-8、不得包含 NUL，且不能超过 16 KiB。

在 `Tag` 模式关闭“缺失或空 TXT 时启用 Tagger 补全”后，相关样本会记录问题且
不会导出；修正源 TXT 后需要新建任务重新运行。

## JSON 模式

标准 JSON 可以只包含已有字段，增量流程会按配置保留或覆盖。Legacy
`{"tags": { ...九字段... }}` 结构也会被读取。

原始 E621 分组 JSON 必须严格包含以下数组字段：

```text
artist, character, contributor, copyright, general,
invalid, lore, meta, species
```

有效原始 E621 JSON 会转换为标准 JSON 并跳过 Caption。候选格式一旦被识别但
内容无效，会产生阻塞问题，不会回退到图片 Tagger。

## 目录名映射

开启 Policy 后，`数字_名称` 格式的一级目录可以写入标准 JSON 的 `artist`：

```text
001_角色名/image.png -> "artist": "@角色名"
```

该映射始终写入 `artist`，与 NL 的 `Character` 预设无关。Policy 在新任务中默认
关闭；其目录 artist 子开关默认开启，画师丢弃率默认 `0`。

## 隐私

不要把真实数据集放入本仓库。`data/` 除本说明外已被 `.gitignore` 排除；任务
overlay、备份、状态数据库和导出结果也不属于源码发布内容。
